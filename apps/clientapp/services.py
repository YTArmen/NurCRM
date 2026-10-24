"""Бизнес-логика приложения клиентов: магазины, связь с main.Client, баланс, история, приглашения."""
import base64
import hashlib
import json
import logging
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from apps.main.models import Client, ClientBonusTransaction, Sale
from apps.main.phone_utils import normalize_phone_e164, phone_search_suffix
from apps.users.models import Branch

from .models import AppCustomer, AppShopSettings, ClientAppConfig, Referral, ReferralRule, hash_secret

logger = logging.getLogger("clientapp")

ZERO = Decimal("0.00")
SHOPS_CACHE_KEY = "clientapp:shops:v1"
SHOPS_CACHE_SECONDS = 300
PURCHASE_STATUSES = (
    Sale.Status.PAID,
    Sale.Status.DEBT,
    Sale.Status.PARTIALLY_RETURNED,
    Sale.Status.CANCELED,
)


def num(value):
    """Decimal → число JSON (int, если без дробной части)."""
    if value is None:
        return None
    d = Decimal(str(value))
    if d == d.to_integral_value():
        return int(d)
    return float(d.normalize())


def iso(dt):
    if dt is None:
        return None
    if timezone.is_aware(dt):
        dt = dt.astimezone(dt_timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.isoformat()


def phone_hash(phone: str) -> str:
    return hash_secret(normalize_phone_e164(phone) or phone or "")


# ======================================================================
# Связь AppCustomer ↔ main.Client (по телефону)
# ======================================================================


def clients_for_phone_qs(phone_e164: str, company=None):
    """Клиенты компаний с этим телефоном (по нормализованному номеру + запасной вариант для старых записей)."""
    if not phone_e164:
        return Client.objects.none()
    suffix = phone_search_suffix(phone_e164)
    q = Q(phone_normalized=phone_e164)
    if suffix and len(suffix) == 9:
        # записи до заполнения phone_normalized (бэкфилл) — по хвосту номера
        q |= Q(phone_normalized="", phone__endswith=suffix)
    qs = Client.objects.filter(q)
    if company is not None:
        qs = qs.filter(company=company)
    return qs


def linked_client_ids(customer: AppCustomer):
    """id main.Client всех компаний с телефоном клиента (поставщики/подрядчики с тем же номером — нет)."""
    if not customer.phone:
        return []
    qs = clients_for_phone_qs(customer.phone).exclude(
        type__in=[Client.StatusClient.SUPPLIERS, Client.StatusClient.IMPLEMENTERS, Client.StatusClient.CONTRACTOR]
    )
    return [
        c.id
        for c in qs.only("id", "phone", "phone_normalized")
        if c.phone_normalized == customer.phone or normalize_phone_e164(c.phone) == customer.phone
    ]


def pick_client_for_kassa(company, branch, phone_e164):
    """
    Клиент компании для кассы: сначала запись этого филиала, затем общая (без филиала), затем любая.
    """
    qs = clients_for_phone_qs(phone_e164, company=company).order_by("created_at")
    candidates = [c for c in qs[:50] if normalize_phone_e164(c.phone) == phone_e164]
    if not candidates:
        return None
    if branch is not None:
        for c in candidates:
            if c.branch_id == branch.id:
                return c
    for c in candidates:
        if c.branch_id is None:
            return c
    return candidates[0] if branch is None else None


def confirmed_customer_for_phone(phone_e164):
    """Профиль приложения с подтверждённым (через Telegram) номером, или None."""
    if not phone_e164:
        return None
    return AppCustomer.objects.filter(phone=phone_e164, deleted_at__isnull=True).first()


def client_for_kassa_or_from_app(company, branch, phone_e164):
    """
    (client, created). Клиент компании по номеру; если его нет, но есть подтверждённый профиль приложения —
    заводим клиента из профиля (ФИО, телефон, дата рождения), как resolve-qr делает для NURCRMT (ТЗ 14.10, п. 2.3).
    Без профиля — (None, False). Повторный вызов клиента не дублирует: создание под блокировкой профиля.
    """
    client = pick_client_for_kassa(company, branch, phone_e164)
    if client is not None:
        return client, False
    customer = confirmed_customer_for_phone(phone_e164)
    if customer is None:
        return None, False
    with transaction.atomic():
        locked = AppCustomer.objects.select_for_update().filter(pk=customer.pk, deleted_at__isnull=True).first()
        if locked is None or not locked.phone:
            return None, False
        client = pick_client_for_kassa(company, branch, phone_e164)
        if client is not None:
            return client, False
        return create_client_from_customer(company, branch, locked), True


def create_client_from_customer(company, branch, customer: AppCustomer, user=None):
    client = Client(
        company=company,
        branch=branch,
        full_name=(customer.full_name or "Клиент приложения")[:255],
        phone=customer.phone,
        sector=Client.Sector.MARKET,
        type=Client.StatusClient.CLIENT,
    )
    if customer.birth_date:
        client.date = customer.birth_date
    client.save()
    return client


# ======================================================================
# Магазины
# ======================================================================


def _decimal_or_none(v):
    return num(v) if v is not None else None


def _company_has_showcase(company) -> bool:
    """
    catalogSlug — slug публичной витрины (ТЗ 14.10, п. 2.1). Публичный адрес
    /api/main/public/companies/<slug>/showcase/ открывается для любой компании со slug
    (resolve_public_company ищет только по slug; без макета отдаётся документ по умолчанию),
    поэтому can_view_showcase и факт публикации макета на ссылку «Цены» не влияют.
    """
    return bool(company.slug) and bool(getattr(company, "is_active", True))


# ----------------------------------------------------------------------
# Бесплатный период и оплата (настройка ClientAppConfig в админке)
# ----------------------------------------------------------------------


def app_free_until():
    """(is_free, free_until): пока бесплатно — любой магазин может быть в приложении."""
    cfg = ClientAppConfig.get()
    today = timezone.localdate()
    return (cfg.free_until is None or today <= cfg.free_until), cfg.free_until


def paid_company_ids():
    """Компании с платной функцией приложения (тариф или действующая платная функция)."""
    from apps.users.models import Company, CompanyAddon

    code = ClientAppConfig.get().paid_feature_code
    today = timezone.localdate()
    by_addon = CompanyAddon.objects.filter(code=code, active=True).filter(
        Q(until__isnull=True) | Q(until__gte=today)
    ).values_list("company_id", flat=True)
    by_plan = Company.objects.filter(subscription_plan__features__name=code).values_list("id", flat=True)
    return set(by_addon) | set(by_plan)


def company_app_access(company):
    """{"allowed", "free", "free_until"} — можно ли магазину компании быть в приложении."""
    free, until = app_free_until()
    allowed = free or company.id in paid_company_ids()
    return {"allowed": allowed, "free": free, "free_until": until.isoformat() if until else None}


def _has_coords(row):
    return row is not None and row.latitude is not None and row.longitude is not None


def _listable(row):
    """
    Магазин попадает в список, если координаты найдены, либо адрес только что задан/изменён и геокодер
    ещё не прошёл (status пустой, попыток 0) — тогда он отдаётся без координат, приложение найдёт их само
    (ТЗ 14.10, п. 2.2). После неудачи геокодера (not_found/error) магазин скрыт — защита карты от мусора.
    """
    if row is None:
        return False
    if _has_coords(row):
        return True
    return row.geocode_status == "" and (row.geocode_attempts or 0) == 0


def build_shops():
    """
    Список магазинов для GET /api/v1/shops (без кэша).
    Поля name/address/phone/hours: значение из app-shop-settings, иначе из филиала, иначе из компании.
    На карту попадает магазин с адресом и координатами (или ожидающий геокодер), не скрытый администратором,
    у компании, которой приложение доступно (бесплатный период или оплата).
    """
    free, _until = app_free_until()
    allowed_ids = None if free else paid_company_ids()
    company_rows = {
        s.company_id: s
        for s in AppShopSettings.objects.filter(branch__isnull=True, show_in_app=True, hidden_by_admin=False)
        .select_related("company")
        .filter(company__is_active=True)
        if allowed_ids is None or s.company_id in allowed_ids
    }
    if not company_rows:
        return []
    branch_rows = {}
    for s in AppShopSettings.objects.filter(company_id__in=company_rows.keys(), branch__isnull=False):
        branch_rows[s.branch_id] = s
    branches_by_company = {}
    for b in Branch.objects.filter(company_id__in=company_rows.keys(), is_active=True).order_by("name"):
        branches_by_company.setdefault(b.company_id, []).append(b)

    shops = []
    for company_id, crow in company_rows.items():
        company = crow.company
        catalog_slug = company.slug if _company_has_showcase(company) else None
        c_points_enabled = bool(crow.points_enabled)
        c_points_percent = crow.points_percent

        branch_shops = []
        for b in branches_by_company.get(company_id, []):
            brow = branch_rows.get(b.id)
            if brow is not None and (not brow.show_in_app or brow.hidden_by_admin):
                continue
            address = (brow.address if brow and brow.address else (b.address or "")).strip()
            if not address or not _listable(brow):
                continue
            p_enabled = brow.points_enabled if brow and brow.points_enabled is not None else c_points_enabled
            p_percent = brow.points_percent if brow and brow.points_percent is not None else c_points_percent
            name = (brow.display_name if brow and brow.display_name else "") or (
                f"{crow.display_name or company.name} — {b.name}"
            )
            branch_shops.append({
                "id": str(b.id),
                "companyId": str(company.id),
                "branchId": str(b.id),
                "name": name,
                "address": address,
                "phone": (brow.phone if brow and brow.phone else "") or b.phone or crow.phone or company.phone or "",
                "hours": (brow.hours if brow and brow.hours else "") or crow.hours or "",
                "latitude": _decimal_or_none(brow.latitude) if brow else None,
                "longitude": _decimal_or_none(brow.longitude) if brow else None,
                "pointsEnabled": bool(p_enabled),
                "pointsPercent": num(p_percent) if p_enabled and p_percent is not None else None,
                "catalogSlug": catalog_slug,
            })
        if branch_shops:
            shops.extend(branch_shops)
            continue
        address = (crow.address or company.address or "").strip()
        if not address or not _listable(crow):
            continue
        shops.append({
            "id": str(company.id),
            "companyId": str(company.id),
            "branchId": None,
            "name": crow.display_name or company.name,
            "address": address,
            "phone": crow.phone or company.phone or "",
            "hours": crow.hours or "",
            "latitude": _decimal_or_none(crow.latitude),
            "longitude": _decimal_or_none(crow.longitude),
            "pointsEnabled": c_points_enabled,
            "pointsPercent": num(c_points_percent) if c_points_enabled and c_points_percent is not None else None,
            "catalogSlug": catalog_slug,
        })
    shops.sort(key=lambda s: (s["name"].lower(), s["id"]))
    return shops


def shops_version() -> str:
    """
    Отпечаток источников /shops (ТЗ 14.10, п. 2.2): настройки магазинов, филиалы, компании (у Company нет
    updated_at — берём сами поля), конфиг «Бесплатно до». Любое изменение, даже через queryset.update()
    без сигналов, даёт новый отпечаток → кэш перестраивается и ETag меняется не позже чем за минуту.
    Четыре лёгких запроса; /shops и так отдаётся с Cache-Control max-age=60.
    """
    from django.db.models import Count

    from apps.users.models import Company

    enabled_companies = AppShopSettings.objects.filter(branch__isnull=True).values("company_id")
    # geocode_row пишет через update(): updated_at не меняется, поэтому учитываем и результат геокодера
    s = AppShopSettings.objects.aggregate(
        m=Max("updated_at"), n=Count("id"), g=Max("geocoded_at"), a=Sum("geocode_attempts")
    )
    companies = list(
        Company.objects.filter(id__in=enabled_companies)
        .order_by("id")
        .values_list("id", "name", "address", "phone", "slug", "is_active")
    )
    # у филиала updated_at есть, но queryset.update() его не трогает — берём и сами поля
    branches = list(
        Branch.objects.filter(company_id__in=enabled_companies)
        .order_by("id")
        .values_list("id", "name", "address", "phone", "is_active", "updated_at")
    )
    cfg = ClientAppConfig.objects.filter(pk=1).values_list("free_until", "paid_feature_code").first()
    raw = json.dumps(
        [str(s["m"]), s["n"], str(s["g"]), s["a"], [list(map(str, c)) for c in companies],
         [list(map(str, b)) for b in branches], str(cfg)]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def get_shops_cached():
    """
    (shops, etag). Кэш в Redis на SHOPS_CACHE_SECONDS, но живёт только пока совпадает отпечаток источников
    (shops_version); плюс сигналы сбрасывают его сразу при save() настроек/компаний/филиалов.
    """
    try:
        version = shops_version()
    except Exception:  # база недоступна — пусть упадёт ниже обычным путём, без кэша
        logger.debug("shops_version failed", exc_info=True)
        version = None
    cached = cache.get(SHOPS_CACHE_KEY)
    if cached and version is not None and cached.get("version") == version:
        return cached["shops"], cached["etag"]
    shops = build_shops()
    body = json.dumps(shops, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    etag = '"' + hashlib.sha1(body.encode("utf-8")).hexdigest() + '"'
    if version is not None:
        cache.set(SHOPS_CACHE_KEY, {"shops": shops, "etag": etag, "version": version}, SHOPS_CACHE_SECONDS)
    return shops, etag


def invalidate_shops_cache():
    try:
        cache.delete(SHOPS_CACHE_KEY)
    except Exception:  # кэш недоступен — не мешаем сохранению
        logger.debug("shops cache delete failed", exc_info=True)


def shop_ref(company, branch):
    """(shopId, shopName) для продажи/клиента."""
    if branch is not None:
        return str(branch.id), f"{company.name} — {branch.name}"
    return str(company.id), company.name


def company_points_settings(company_id, branch_id=None):
    rows = {
        s.branch_id: s
        for s in AppShopSettings.objects.filter(company_id=company_id).filter(
            Q(branch__isnull=True) | Q(branch_id=branch_id)
        )
    }
    crow = rows.get(None)
    brow = rows.get(branch_id) if branch_id else None
    enabled = bool(crow.points_enabled) if crow else False
    percent = crow.points_percent if crow else None
    if brow is not None:
        if brow.points_enabled is not None:
            enabled = brow.points_enabled
        if brow.points_percent is not None:
            percent = brow.points_percent
    return enabled, percent


# ======================================================================
# Баланс и история
# ======================================================================


def customer_balances(customer: AppCustomer):
    ids = linked_client_ids(customer)
    if not ids:
        return []
    clients = list(Client.objects.filter(id__in=ids).select_related("company", "branch"))
    last_dates = {
        row["client_id"]: row["last"]
        for row in ClientBonusTransaction.objects.filter(client_id__in=ids)
        .values("client_id")
        .annotate(last=Max("created_at"))
    }
    result = []
    for c in clients:
        balance = c.bonus_balance or ZERO
        if balance == 0 and c.id not in last_dates:
            continue
        shop_id, shop_name = shop_ref(c.company, c.branch)
        enabled, percent = company_points_settings(c.company_id, c.branch_id)
        result.append({
            "companyId": str(c.company_id),
            "shopId": shop_id,
            "shopName": shop_name,
            "clientId": str(c.id),
            "points": num(balance),
            "pointsEnabled": enabled,
            "pointsPercent": num(percent) if enabled and percent is not None else None,
            "updatedAt": iso(last_dates.get(c.id) or c.updated_at),
        })
    result.sort(key=lambda r: r["updatedAt"] or "", reverse=True)
    return result


def encode_cursor(dt, pk) -> str:
    raw = f"{dt.isoformat()}|{pk}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        dt_s, pk = raw.split("|", 1)
        dt = datetime.fromisoformat(dt_s)
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, dt_timezone.utc)
        return dt, pk
    except Exception:
        return None


def _sale_items(sale):
    items = []
    for it in sale.items.all():
        qty = it.quantity or ZERO
        price = it.unit_price or ZERO
        line = (price * qty - (it.line_discount or ZERO)).quantize(Decimal("0.01"))
        name = it.name_snapshot
        variant = getattr(it, "variant", None)
        if variant is not None:
            extra = " ".join(x for x in (variant.size, variant.color) if x)
            if extra:
                name = f"{name} ({extra})"
        items.append({"name": name, "qty": num(qty), "price": num(price), "sum": num(line)})
    return items


def sale_payload(sale, txs, with_items=True):
    earned = sum((t.delta for t in txs if t.reason == ClientBonusTransaction.Reason.EARN), ZERO)
    spent_tx = sum((-t.delta for t in txs if t.reason == ClientBonusTransaction.Reason.REDEEM), ZERO)
    reversed_points = sum(
        (t.delta for t in txs if t.reason == ClientBonusTransaction.Reason.MANUAL), ZERO
    )
    spent = spent_tx if spent_tx > 0 else (sale.bonus_redeemed or ZERO)
    returns = list(sale.returns.all())
    returned_amount = sum((r.returned_amount or ZERO for r in returns), ZERO)
    fully_returned = sale.status == Sale.Status.CANCELED and bool(returns) or any(r.is_full for r in returns)
    shop_id, shop_name = shop_ref(sale.company, sale.branch)
    data = {
        "id": str(sale.id),
        "kind": "purchase",
        "companyId": str(sale.company_id),
        "shopId": shop_id,
        "shopName": shop_name,
        "date": iso(sale.paid_at or sale.created_at),
        "number": sale.doc_number,
        "total": num(sale.total or ZERO),
        "pointsEarned": num(earned),
        "pointsSpent": num(spent),
        "pointsAdjusted": num(reversed_points) if reversed_points else 0,
        "status": "returned" if fully_returned else (
            "partially_returned" if sale.status == Sale.Status.PARTIALLY_RETURNED or returns else
            ("canceled" if sale.status == Sale.Status.CANCELED else "paid")
        ),
        "returned": bool(fully_returned),
        "returnedAmount": num(returned_amount),
    }
    if with_items:
        data["items"] = _sale_items(sale)
    return data


def bonus_payload(tx):
    shop_id, shop_name = shop_ref(tx.company, tx.client.branch if tx.client_id else None)
    delta = tx.delta or ZERO
    return {
        "id": f"b-{tx.id}",
        "kind": "bonus",
        "companyId": str(tx.company_id),
        "shopId": shop_id,
        "shopName": shop_name,
        "date": iso(tx.created_at),
        "number": None,
        "total": None,
        "pointsEarned": num(delta) if delta > 0 else 0,
        "pointsSpent": num(-delta) if delta < 0 else 0,
        "note": tx.note or "",
        "status": "bonus",
        "returned": False,
        "returnedAmount": 0,
        "items": [],
    }


def _sales_qs(client_ids):
    return (
        Sale.objects.filter(client_id__in=client_ids, status__in=PURCHASE_STATUSES)
        .select_related("company", "branch")
    )


def customer_purchases(customer: AppCustomer, cursor=None, limit=20):
    """Лента: продажи связанных клиентов + движения бонусов без продажи (приглашения, перенос, ручные)."""
    ids = linked_client_ids(customer)
    if not ids:
        return [], None
    pos = decode_cursor(cursor) if cursor else None
    sales = _sales_qs(ids)
    bonus = ClientBonusTransaction.objects.filter(client_id__in=ids, sale__isnull=True).select_related(
        "company", "client__branch"
    )
    if pos:
        dt, pk = pos
        sales = sales.filter(Q(created_at__lt=dt) | Q(created_at=dt, id__lt=_uuid_or_none(pk) or pk))
        bonus = bonus.filter(Q(created_at__lt=dt) | Q(created_at=dt, id__lt=_uuid_or_none(pk) or pk))
    sales = list(
        sales.order_by("-created_at", "-id").prefetch_related("items__variant", "returns", "bonus_transactions")[
            : limit + 1
        ]
    )
    bonus = list(bonus.order_by("-created_at", "-id")[: limit + 1])
    merged = [(s.created_at, str(s.id), "s", s) for s in sales] + [(b.created_at, str(b.id), "b", b) for b in bonus]
    merged.sort(key=lambda x: (x[0], x[1]), reverse=True)
    page = merged[:limit]
    next_cursor = encode_cursor(page[-1][0], page[-1][1]) if len(merged) > limit and page else None
    items = []
    for _, _, kind, obj in page:
        if kind == "s":
            items.append(sale_payload(obj, list(obj.bonus_transactions.all())))
        else:
            items.append(bonus_payload(obj))
    return items, next_cursor


def _uuid_or_none(v):
    import uuid as _uuid

    try:
        return _uuid.UUID(str(v))
    except (ValueError, TypeError):
        return None


def customer_purchase_detail(customer: AppCustomer, purchase_id: str):
    ids = linked_client_ids(customer)
    if not ids:
        return None
    if str(purchase_id).startswith("b-"):
        pk = _uuid_or_none(str(purchase_id)[2:])
        tx = (
            ClientBonusTransaction.objects.filter(pk=pk, client_id__in=ids, sale__isnull=True)
            .select_related("company", "client__branch")
            .first()
            if pk
            else None
        )
        return bonus_payload(tx) if tx else None
    pk = _uuid_or_none(purchase_id)
    if pk is None:
        return None
    sale = (
        _sales_qs(ids).filter(pk=pk).prefetch_related("items__variant", "returns", "bonus_transactions").first()
    )
    if sale is None:
        return None
    return sale_payload(sale, list(sale.bonus_transactions.all()))


# ======================================================================
# Приглашения
# ======================================================================


def customer_has_purchases(customer: AppCustomer) -> bool:
    ids = linked_client_ids(customer)
    if not ids:
        return False
    return Sale.objects.filter(client_id__in=ids, status__in=PURCHASE_STATUSES).exists()


def referral_summary(customer: AppCustomer):
    code = customer.ensure_referral_code()
    base = (getattr(settings, "CLIENT_APP_REFERRAL_LINK_BASE", "") or "https://app.nurcrm.kg/r/").rstrip("/")
    invited = Referral.objects.filter(inviter=customer).count()
    earned = Referral.objects.filter(inviter=customer, rewarded_at__isnull=False).aggregate(s=Sum("inviter_points"))[
        "s"
    ] or ZERO
    return {
        "code": code,
        "link": f"{base}/{code}",
        "invited": invited,
        "earned": num(earned),
        "referredBy": bool(customer.referred_by_id),
    }


class ReferralError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def apply_referral_code(customer: AppCustomer, code: str):
    code = (code or "").strip().upper()
    if not code:
        raise ReferralError("referral_not_found", "Укажите код приглашения.")
    if customer.referred_by_id or Referral.objects.filter(invitee=customer).exists():
        raise ReferralError("referral_already_applied", "Код приглашения уже указан.")
    inviter = AppCustomer.objects.filter(referral_code=code, deleted_at__isnull=True).first()
    if inviter is None:
        raise ReferralError("referral_not_found", "Код приглашения не найден.")
    if inviter.pk == customer.pk or (inviter.phone and inviter.phone == customer.phone):
        raise ReferralError("referral_self", "Нельзя пригласить самого себя.")
    p_hash = customer.phone_hash or phone_hash(customer.phone)
    if p_hash and Referral.objects.filter(invitee_phone_hash=p_hash).exclude(invitee=customer).exists():
        raise ReferralError("referral_already_applied", "Для этого номера код приглашения уже использовался.")
    if customer_has_purchases(customer):
        raise ReferralError("referral_too_late", "Код можно указать только до первой покупки.")
    window_days = int(getattr(settings, "CLIENT_APP_REFERRAL_WINDOW_DAYS", 7) or 7)
    if customer.created_at and (timezone.now() - customer.created_at).days > window_days:
        raise ReferralError("referral_too_late", "Код приглашения указывается при первом входе.")
    try:
        with transaction.atomic():
            ref = Referral.objects.create(inviter=inviter, invitee=customer, invitee_phone_hash=p_hash)
            AppCustomer.objects.filter(pk=customer.pk).update(referred_by=inviter)
            customer.referred_by = inviter
    except IntegrityError:
        raise ReferralError("referral_already_applied", "Код приглашения уже указан.")
    return ref


def process_referral_for_sale(sale_id):
    """
    Первая оплаченная покупка приглашённого в компании с включённым правилом → баллы обоим
    (через change_bonus, идемпотентно: ключи referral:<id>:invitee / :inviter). Возвращает Referral или None.
    """
    from apps.main.kassa_views import change_bonus

    sale = Sale.objects.select_related("client", "company", "branch").filter(pk=sale_id).first()
    if sale is None or sale.client_id is None or sale.status not in (Sale.Status.PAID, Sale.Status.DEBT):
        return None
    phone = normalize_phone_e164(sale.client.phone)
    if not phone:
        return None
    invitee = AppCustomer.objects.filter(phone=phone, deleted_at__isnull=True).first()
    if invitee is None:
        return None
    rule = ReferralRule.objects.filter(company_id=sale.company_id, enabled=True).first()
    if rule is None or (rule.inviter_points <= 0 and rule.invitee_points <= 0):
        return None
    with transaction.atomic():
        ref = (
            Referral.objects.select_for_update(of=("self",))
            .filter(invitee=invitee, rewarded_at__isnull=True)
            .select_related("inviter")
            .first()
        )
        if ref is None:
            return None
        # только первая оплаченная покупка в этой компании
        company_client_ids = list(
            clients_for_phone_qs(phone, company=sale.company).values_list("id", flat=True)
        )
        earlier = (
            Sale.objects.filter(
                client_id__in=company_client_ids,
                status__in=PURCHASE_STATUSES,
                created_at__lt=sale.created_at,
            )
            .exclude(pk=sale.pk)
            .exists()
        )
        if earlier:
            return None
        if rule.invitee_points > 0:
            change_bonus(
                client=sale.client,
                delta=rule.invitee_points,
                reason=ClientBonusTransaction.Reason.MANUAL,
                note="Бонус за регистрацию по приглашению",
                idempotency_key=f"referral:{ref.pk}:invitee",
            )
        inviter = ref.inviter
        if rule.inviter_points > 0 and inviter.deleted_at is None and inviter.phone:
            inviter_client = pick_client_for_kassa(sale.company, sale.branch, inviter.phone) or pick_client_for_kassa(
                sale.company, None, inviter.phone
            )
            if inviter_client is None:
                # программа компании начисляет приглашающему — заводим ему клиента в этой компании
                inviter_client = create_client_from_customer(sale.company, sale.branch, inviter)
            change_bonus(
                client=inviter_client,
                delta=rule.inviter_points,
                reason=ClientBonusTransaction.Reason.MANUAL,
                note="Бонус за приглашённого друга",
                idempotency_key=f"referral:{ref.pk}:inviter",
            )
        Referral.objects.filter(pk=ref.pk).update(
            company=sale.company,
            sale_id=sale.pk,
            inviter_points=rule.inviter_points if inviter.deleted_at is None else ZERO,
            invitee_points=rule.invitee_points,
            rewarded_at=timezone.now(),
        )
        ref.refresh_from_db()
        notify = (inviter.pk, rule.inviter_points, sale.company_id) if (
            rule.inviter_points > 0 and inviter.deleted_at is None
        ) else None
    # пуш пригласившему — уже после фиксации начислений; ошибки Expo начисление не отменяют
    if notify:
        try:
            from .push import push_referral_reward

            push_referral_reward(*notify)
        except Exception:
            logger.warning("referral push failed", exc_info=True)
    return ref


def has_pending_referral_for_client(client) -> bool:
    phone = normalize_phone_e164(getattr(client, "phone", ""))
    if not phone:
        return False
    return Referral.objects.filter(
        invitee__phone=phone, invitee__deleted_at__isnull=True, rewarded_at__isnull=True
    ).exists()


# ----------------------------------------------------------------------
# ФИО покупателя у клиента кассы (старые кассы заводят клиента по телефону из QR без имени)
# ----------------------------------------------------------------------

PLACEHOLDER_NAMES = {"", "клиент", "покупатель", "клиент приложения", "без имени", "гость", "client", "customer"}


def is_placeholder_name(name, phone=None) -> bool:
    """Пусто, «Клиент»/«Покупатель» или сам номер телефона вместо имени."""
    n = (name or "").strip().lower()
    if n in PLACEHOLDER_NAMES:
        return True
    digits = "".join(ch for ch in n if ch.isdigit())
    rest = "".join(ch for ch in n if not ch.isdigit() and ch not in " +-()")
    return bool(digits) and len(digits) >= 6 and not rest


def app_name_for_phone(phone):
    """ФИО из профиля приложения для этого телефона (или "")."""
    e164 = normalize_phone_e164(phone)
    if not e164:
        return ""
    return (
        AppCustomer.objects.filter(phone=e164, deleted_at__isnull=True)
        .exclude(full_name="")
        .values_list("full_name", flat=True)
        .first()
        or ""
    )


def fill_client_names_from_customer(customer: AppCustomer) -> int:
    """Клиентам касс с этим телефоном и именем-заглушкой ставит ФИО из приложения. → сколько обновлено."""
    name = (customer.full_name or "").strip()
    if not name or not customer.phone:
        return 0
    ids = [
        c.id
        for c in Client.objects.filter(id__in=linked_client_ids(customer)).only("id", "full_name", "phone")
        if is_placeholder_name(c.full_name, c.phone)
    ]
    if not ids:
        return 0
    return Client.objects.filter(id__in=ids).update(full_name=name[:255])
