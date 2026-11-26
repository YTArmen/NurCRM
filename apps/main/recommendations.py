"""
Умная допродажа: серверная часть (ТЗ, часть 7, раздел 2).

- POST  /api/main/recommendations/events/           — журнал показов/нажатий (пачка ≤ 200)
- PATCH /api/main/recommendations/events/link-sale/ — привязать события корзины к продаже
- GET   /api/main/recommendations/stats/            — статистика для владельца
- GET   /api/main/recommendations/pairs/            — готовые пары (считаются ночью)
- GET/PUT /api/main/sales-targets/                  — план продаж
"""
import hashlib
import json
import logging
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date

from rest_framework import permissions, serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import (
    Branch,
    Product,
    Sale,
    SaleItem,
    SaleReturn,
    RecommendationEvent,
    RecommendationPairsCache,
    SalesTarget,
)
from apps.users.models import User

logger = logging.getLogger("main.recommendations")

ZERO_MONEY = Decimal("0.00")
MONEY_Q = Decimal("0.01")
MAX_EVENTS_PER_BATCH = 200

PAIRS_DAYS = 90
PAIRS_STORED_LIMIT = 50   # храним до 50 «соседей» на товар, режем по ?limit= при чтении
PAIRS_DEFAULT_LIMIT = 10
PAIRS_MIN_COUNT = 3

# Продажа считается оплаченной (в т.ч. в долг и частично возвращённая).
PAID_SALE_STATUSES = (
    Sale.Status.PAID,
    Sale.Status.DEBT,
    Sale.Status.PARTIALLY_RETURNED,
)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# =========================================================================
# Helpers: компания, права, филиал, период
# =========================================================================

def _get_company(request):
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        raise PermissionDenied("Требуется авторизация.")
    company = getattr(user, "company", None) or getattr(user, "owned_company", None)
    if not company:
        raise PermissionDenied("У пользователя не найдена компания.")
    return company


def _is_owner_like(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if getattr(user, "owned_company", None):
        return True
    if getattr(user, "is_admin", False):
        return True
    role = str(getattr(user, "role", "") or "").lower()
    return role in ("owner", "admin", "владелец", "администратор")


def _is_owner(user, company) -> bool:
    """Запись плана продаж — только владелец (или суперпользователь)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if getattr(company, "owner_id", None) and company.owner_id == user.id:
        return True
    return str(getattr(user, "role", "") or "").lower() in ("owner", "владелец")


def _can_view_analytics(user) -> bool:
    return _is_owner_like(user) or bool(getattr(user, "can_view_analytics", False))


def _fixed_branch_for_user(user, company):
    """Филиал сотрудника: основной membership → любой membership компании → None."""
    if not user or not company:
        return None
    memberships = getattr(user, "branch_memberships", None)
    if memberships is None:
        return None
    m = (
        memberships.filter(branch__company_id=company.id)
        .select_related("branch")
        .order_by("-is_primary", "created_at")
        .first()
    )
    return m.branch if m else None


def _parse_uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ValidationError({field: "Некорректный идентификатор."})


def _resolve_branch(request, company, raw_branch):
    """
    Владелец/админ: может выбрать филиал (?branch=) или смотреть всю компанию (None).
    Сотрудник: всегда свой филиал (если он у него есть), параметр игнорируется.
    """
    user = request.user
    if not _is_owner_like(user):
        fixed = _fixed_branch_for_user(user, company)
        if fixed is not None:
            return fixed
    if raw_branch in (None, ""):
        return None
    branch_id = _parse_uuid(raw_branch, "branch")
    br = Branch.objects.filter(id=branch_id, company=company).first()
    if br is None:
        raise ValidationError({"branch": "Филиал не найден."})
    return br


def _local_range(d_from, d_to):
    """[начало d_from; начало d_to+1) в часовом поясе проекта — индекс-дружелюбно."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(d_from, time.min), tz) if d_from else None
    end = timezone.make_aware(datetime.combine(d_to + timedelta(days=1), time.min), tz) if d_to else None
    return start, end


def _parse_date_param(params, name):
    raw = params.get(name)
    if raw in (None, ""):
        return None
    try:
        d = parse_date(str(raw))
    except ValueError:  # «2026-13-01» — формат верный, дата нет
        d = None
    if d is None:
        raise ValidationError({name: "Формат даты YYYY-MM-DD."})
    return d


def parse_upsell_fields(data) -> dict:
    """
    upsell_priority / upsell_excluded из «ручных» create-эндпоинтов товара
    (create-manual, create-by-barcode). Возвращает только переданные поля.
    """
    out = {}
    if not hasattr(data, "get"):
        return out
    raw_p = data.get("upsell_priority")
    if raw_p not in (None, ""):
        try:
            p = int(raw_p)
        except (TypeError, ValueError):
            raise ValidationError({"upsell_priority": "Целое число 0–100."})
        if not 0 <= p <= 100:
            raise ValidationError({"upsell_priority": "Целое число 0–100."})
        out["upsell_priority"] = p
    raw_e = data.get("upsell_excluded")
    if raw_e not in (None, ""):
        out["upsell_excluded"] = str(raw_e).strip().lower() in ("1", "true", "yes", "on", "да")
    return out


def _money(v) -> str:
    return f"{(v or ZERO_MONEY).quantize(MONEY_Q):.2f}"


def _user_name(first_name, last_name, email):
    full = f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
    return full or (email or "") or "Кассир"


# =========================================================================
# Serializers
# =========================================================================

class RecommendationEventItemSerializer(serializers.Serializer):
    client_event_id = serializers.UUIDField()
    event = serializers.ChoiceField(choices=RecommendationEvent.EventType.choices)
    product_id = serializers.UUIDField()
    trigger_product_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    cart_id = serializers.CharField(max_length=64, required=False, allow_null=True, allow_blank=True, default=None)
    sale_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    price = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=ZERO_MONEY)
    score = serializers.FloatField(required=False, default=0.0)
    cashier_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    device_id = serializers.CharField(max_length=128, required=False, allow_null=True, allow_blank=True, default=None)
    occurred_at = serializers.DateTimeField()


class RecommendationEventsBatchSerializer(serializers.Serializer):
    events = serializers.ListField(
        child=RecommendationEventItemSerializer(),
        max_length=MAX_EVENTS_PER_BATCH,
        allow_empty=False,
    )


class RecommendationLinkSaleSerializer(serializers.Serializer):
    cart_id = serializers.CharField(max_length=64)
    sale_id = serializers.UUIDField()


class SalesTargetWriteSerializer(serializers.Serializer):
    month = serializers.RegexField(MONTH_RE, error_messages={"invalid": "Формат месяца YYYY-MM."})
    revenue_target = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"))
    branch = serializers.UUIDField(required=False, allow_null=True, default=None)


# =========================================================================
# Привязка событий к продажам (в т.ч. продажи, пришедшие позже — офлайн)
# =========================================================================

def resolve_pending_sales(company_id, qs=None):
    """
    События с sale_ref, но без sale (продажа ещё не была на сервере в момент link-sale /
    отправки событий): если продажа уже появилась — проставляем FK и филиал.
    """
    base = qs if qs is not None else RecommendationEvent.objects.filter(company_id=company_id)
    refs = set(
        base.filter(sale__isnull=True, sale_ref__isnull=False)
        .values_list("sale_ref", flat=True)
        .distinct()[:5000]
    )
    if not refs:
        return 0
    sales = Sale.objects.filter(company_id=company_id, id__in=refs).values_list("id", "branch_id")
    updated = 0
    for sale_id, branch_id in sales:
        ev_qs = RecommendationEvent.objects.filter(
            company_id=company_id, sale_ref=sale_id, sale__isnull=True
        )
        updated += ev_qs.update(sale_id=sale_id)
        if branch_id:
            RecommendationEvent.objects.filter(
                company_id=company_id, sale_ref=sale_id, branch__isnull=True
            ).update(branch_id=branch_id)
    return updated


# =========================================================================
# 2.2 Журнал событий
# =========================================================================

class RecommendationEventsAPIView(APIView):
    """
    POST /api/main/recommendations/events/
    Ответ: {"accepted": N, "duplicates": M, "rejected": K}
    Уникальность (company_id, client_event_id): повторная пачка → всё в duplicates.
    rejected — события с товаром, которого нет в компании (не сохраняются).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _get_company(request)
        ser = RecommendationEventsBatchSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        raw_events = ser.validated_data["events"]

        # 1) Дубли внутри пачки
        events_data, seen = [], set()
        for ed in raw_events:
            if ed["client_event_id"] in seen:
                continue
            seen.add(ed["client_event_id"])
            events_data.append(ed)
        in_batch_dups = len(raw_events) - len(events_data)

        # 2) Уже сохранённые
        existing_ids = set(
            RecommendationEvent.objects.filter(
                company=company, client_event_id__in=list(seen)
            ).values_list("client_event_id", flat=True)
        )
        fresh = [ed for ed in events_data if ed["client_event_id"] not in existing_ids]

        # 3) Справочники одним запросом каждый
        product_ids = {ed["product_id"] for ed in fresh}
        valid_products = set(
            Product.objects.filter(company=company, id__in=product_ids).values_list("id", flat=True)
        )

        cashier_ids = {ed["cashier_id"] for ed in fresh if ed.get("cashier_id")}
        valid_cashiers = set(
            User.objects.filter(
                Q(company=company) | Q(owned_company=company), id__in=cashier_ids
            ).values_list("id", flat=True)
        ) if cashier_ids else set()

        sale_ids = {ed["sale_id"] for ed in fresh if ed.get("sale_id")}
        sales_map = {
            s["id"]: s["branch_id"]
            for s in Sale.objects.filter(company=company, id__in=sale_ids).values("id", "branch_id")
        } if sale_ids else {}

        # Корзина уже привязана к продаже (link-sale пришёл раньше событий)
        carts_wo_sale = {ed["cart_id"] for ed in fresh if ed.get("cart_id") and not ed.get("sale_id")}
        cart_sale = {}
        if carts_wo_sale:
            for cart_id, sale_ref in (
                RecommendationEvent.objects.filter(
                    company=company, cart_id__in=carts_wo_sale, sale_ref__isnull=False
                ).values_list("cart_id", "sale_ref")
            ):
                cart_sale[cart_id] = sale_ref
            missing = set(cart_sale.values()) - set(sales_map)
            if missing:
                for s in Sale.objects.filter(company=company, id__in=missing).values("id", "branch_id"):
                    sales_map[s["id"]] = s["branch_id"]

        user = request.user
        user_branch = _fixed_branch_for_user(user, company)
        user_branch_id = user_branch.id if user_branch else None
        fallback_cashier = user.id if user.id in valid_cashiers or getattr(user, "company_id", None) == company.id \
            or getattr(company, "owner_id", None) == user.id else None

        to_create, rejected = [], 0
        for ed in fresh:
            if ed["product_id"] not in valid_products:
                rejected += 1
                continue
            cart_id = ed.get("cart_id") or None
            sale_ref = ed.get("sale_id") or (cart_sale.get(cart_id) if cart_id else None)
            sale_exists = sale_ref in sales_map if sale_ref else False
            cashier_id = ed.get("cashier_id")
            if cashier_id not in valid_cashiers:
                cashier_id = fallback_cashier
            to_create.append(
                RecommendationEvent(
                    id=uuid.uuid4(),
                    company=company,
                    branch_id=(sales_map.get(sale_ref) if sale_exists else None) or user_branch_id,
                    client_event_id=ed["client_event_id"],
                    event=ed["event"],
                    product_id=ed["product_id"],
                    trigger_product_ids=[str(pid) for pid in ed.get("trigger_product_ids") or []],
                    cart_id=cart_id,
                    sale_id=sale_ref if sale_exists else None,
                    sale_ref=sale_ref,
                    price=ed.get("price") or ZERO_MONEY,
                    score=ed.get("score") or 0.0,
                    cashier_id=cashier_id,
                    device_id=ed.get("device_id") or None,
                    occurred_at=ed["occurred_at"],
                )
            )

        accepted = 0
        if to_create:
            RecommendationEvent.objects.bulk_create(to_create, ignore_conflicts=True)
            # Точное число вставленных именно этим запросом (параллельный повтор той же пачки
            # уйдёт в конфликт и не попадёт сюда): id генерируем сами.
            accepted = RecommendationEvent.objects.filter(id__in=[o.id for o in to_create]).count()

        duplicates = in_batch_dups + len(existing_ids) + (len(to_create) - accepted)
        return Response(
            {"accepted": accepted, "duplicates": duplicates, "rejected": rejected},
            status=status.HTTP_200_OK,
        )


class RecommendationLinkSaleAPIView(APIView):
    """
    PATCH /api/main/recommendations/events/link-sale/  {"cart_id": "...", "sale_id": "..."}
    Проставляет sale всем событиям корзины. Если продажа ещё не на сервере (офлайн-чек),
    запоминаем sale_ref — FK проставится, когда продажа синхронизируется.
    """
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        company = _get_company(request)
        ser = RecommendationLinkSaleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        cart_id = ser.validated_data["cart_id"]
        sale_id = ser.validated_data["sale_id"]

        sale = Sale.objects.filter(id=sale_id, company=company).only("id", "branch_id").first()
        qs = RecommendationEvent.objects.filter(company=company, cart_id=cart_id)
        updates = {"sale_ref": sale_id}
        if sale is not None:
            updates["sale_id"] = sale.id
        linked = qs.update(**updates)
        if sale is not None and sale.branch_id:
            qs.update(branch_id=sale.branch_id)

        return Response(
            {"linked_events": linked, "sale_found": sale is not None},
            status=status.HTTP_200_OK,
        )


# =========================================================================
# 2.3 Статистика
# =========================================================================

def compute_upsell_revenue(company, branch=None, start=None, end=None):
    """
    Выручка/прибыль допродажи: принятые подсказки с оплаченной продажей, где товар
    остался в чеке. Выручка = (кол-во − возвращено) × цена строки (с учётом скидки строки),
    прибыль = выручка − себестоимость × кол-во. Возвраты (SaleReturn.returned_items) вычитаются.

    Возвращает (revenue, profit, by_product{pid: rev}, by_cashier{cid: rev}, by_day{date: rev}).
    """
    qs = RecommendationEvent.objects.filter(company=company, event=RecommendationEvent.EventType.ACCEPTED)
    if branch is not None:
        qs = qs.filter(branch=branch)
    if start is not None:
        qs = qs.filter(occurred_at__gte=start)
    if end is not None:
        qs = qs.filter(occurred_at__lt=end)

    resolve_pending_sales(company.id, qs)

    accepted = list(
        qs.filter(sale__isnull=False, sale__status__in=PAID_SALE_STATUSES)
        .order_by("occurred_at")
        .values_list("sale_id", "product_id", "cashier_id", "occurred_at")
    )
    by_product, by_cashier, by_day = defaultdict(lambda: ZERO_MONEY), defaultdict(lambda: ZERO_MONEY), defaultdict(lambda: ZERO_MONEY)
    if not accepted:
        return ZERO_MONEY, ZERO_MONEY, by_product, by_cashier, by_day

    # Одна (продажа, товар) — одна допродажа; атрибутируем первому принятию.
    attribution = {}
    for sale_id, product_id, cashier_id, occurred_at in accepted:
        attribution.setdefault((sale_id, product_id), (cashier_id, occurred_at))

    sale_ids = {k[0] for k in attribution}
    product_ids = {k[1] for k in attribution}

    lines = list(
        SaleItem.objects.filter(sale__company=company, sale_id__in=sale_ids, product_id__in=product_ids)
        .values_list(
            "id", "sale_id", "product_id", "unit_price", "quantity", "line_discount",
            "purchase_price_snapshot", "product__purchase_price",
        )
    )

    # Возвраты: по id строки чека
    returned_qty = defaultdict(lambda: Decimal("0"))
    fully_returned_sales = set()
    for sale_id, items, is_full in SaleReturn.objects.filter(
        company=company, sale_id__in=sale_ids
    ).values_list("sale_id", "returned_items", "is_full"):
        if not items:
            # Старые возвраты без состава: полный возврат обнуляет чек целиком.
            if is_full:
                fully_returned_sales.add(sale_id)
            continue
        for entry in items:
            if not isinstance(entry, dict):
                continue
            item_id = entry.get("sale_item")
            if not item_id:
                continue
            try:
                returned_qty[str(item_id)] += Decimal(str(entry.get("qty") or entry.get("quantity") or 0))
            except (InvalidOperation, ValueError):
                continue

    revenue = profit = ZERO_MONEY
    tz = timezone.get_current_timezone()
    for item_id, sale_id, product_id, unit_price, qty, line_discount, cost_snap, cost_prod in lines:
        key = (sale_id, product_id)
        if key not in attribution or sale_id in fully_returned_sales:
            continue
        qty = Decimal(qty or 0)
        if qty <= 0:
            continue
        net_qty = max(Decimal("0"), qty - returned_qty.get(str(item_id), Decimal("0")))
        if net_qty <= 0:
            continue
        unit_price = Decimal(unit_price or 0)
        discount_part = Decimal(line_discount or 0) * net_qty / qty
        line_rev = (unit_price * net_qty - discount_part).quantize(MONEY_Q)
        cost = Decimal(cost_snap if cost_snap is not None else (cost_prod or 0))
        line_profit = (line_rev - cost * net_qty).quantize(MONEY_Q)

        revenue += line_rev
        profit += line_profit
        cashier_id, occurred_at = attribution[key]
        by_product[str(product_id)] += line_rev
        by_cashier[str(cashier_id) if cashier_id else "unknown"] += line_rev
        by_day[timezone.localtime(occurred_at, tz).date().isoformat()] += line_rev

    return revenue, profit, by_product, by_cashier, by_day


class RecommendationStatsAPIView(APIView):
    """
    GET /api/main/recommendations/stats/?date_from=&date_to=&branch=
    Права: can_view_analytics (владелец/админ — всегда).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request)
        if not _can_view_analytics(request.user):
            raise PermissionDenied("Нет доступа к аналитике.")

        params = request.query_params
        branch = _resolve_branch(request, company, params.get("branch"))
        d_from = _parse_date_param(params, "date_from")
        d_to = _parse_date_param(params, "date_to")
        start, end = _local_range(d_from, d_to)

        qs = RecommendationEvent.objects.filter(company=company)
        if branch is not None:
            qs = qs.filter(branch=branch)
        if start is not None:
            qs = qs.filter(occurred_at__gte=start)
        if end is not None:
            qs = qs.filter(occurred_at__lt=end)

        shown_f = Count("id", filter=Q(event=RecommendationEvent.EventType.SHOWN))
        accepted_f = Count("id", filter=Q(event=RecommendationEvent.EventType.ACCEPTED))
        skipped_f = Count("id", filter=Q(event=RecommendationEvent.EventType.SKIPPED))

        totals = qs.aggregate(shown=shown_f, accepted=accepted_f, skipped=skipped_f)
        shown = totals["shown"] or 0
        accepted = totals["accepted"] or 0
        skipped = totals["skipped"] or 0
        acceptance_rate = round(accepted / shown * 100, 1) if shown else 0.0

        revenue, profit, rev_product, rev_cashier, rev_day = compute_upsell_revenue(company, branch, start, end)

        by_product = [
            {
                "product_id": str(r["product_id"]),
                "name": r["product__name"] or "",
                "shown": r["shown"],
                "accepted": r["accepted"],
                "revenue": _money(rev_product.get(str(r["product_id"]))),
            }
            for r in qs.values("product_id", "product__name")
            .annotate(shown=shown_f, accepted=accepted_f)
            .order_by()
        ]
        by_product.sort(key=lambda x: (x["accepted"], x["shown"]), reverse=True)

        by_cashier = []
        for r in (
            qs.values("cashier_id", "cashier__first_name", "cashier__last_name", "cashier__email")
            .annotate(shown=shown_f, accepted=accepted_f)
            .order_by()
        ):
            cid = str(r["cashier_id"]) if r["cashier_id"] else "unknown"
            by_cashier.append({
                "cashier_id": cid if r["cashier_id"] else None,
                "name": _user_name(r["cashier__first_name"], r["cashier__last_name"], r["cashier__email"]),
                "shown": r["shown"],
                "accepted": r["accepted"],
                "revenue": _money(rev_cashier.get(cid)),
            })
        by_cashier.sort(key=lambda x: (x["accepted"], x["shown"]), reverse=True)

        by_day = []
        for r in (
            qs.annotate(d=TruncDate("occurred_at", tzinfo=timezone.get_current_timezone()))
            .values("d")
            .annotate(shown=shown_f, accepted=accepted_f)
            .order_by("d")
        ):
            d = r["d"].isoformat() if r["d"] else None
            by_day.append({
                "date": d,
                "shown": r["shown"],
                "accepted": r["accepted"],
                "revenue": _money(rev_day.get(d)),
            })

        return Response({
            "shown": shown,
            "accepted": accepted,
            "skipped": skipped,
            "acceptance_rate": acceptance_rate,
            "revenue": _money(revenue),
            "profit": _money(profit),
            "by_product": by_product,
            "by_cashier": by_cashier,
            "by_day": by_day,
        })


# =========================================================================
# 2.4 Пары товаров (считаются ночной задачей, запрос только читает)
# =========================================================================

def compute_company_recommendation_pairs(company_id, days=PAIRS_DAYS, limit=PAIRS_STORED_LIMIT):
    """
    Совместные покупки по оплаченным продажам за `days` дней.
    count — чеков с A и B; confidence = count / чеков с A; lift = confidence / доля чеков с B.
    Только count ≥ 3; товары с upsell_excluded не предлагаются (не попадают в together).
    """
    since = timezone.now() - timedelta(days=days)
    excluded_ids = {
        str(pid) for pid in
        Product.objects.filter(company_id=company_id, upsell_excluded=True).values_list("id", flat=True)
    }

    rows = (
        SaleItem.objects.filter(
            sale__company_id=company_id,
            sale__created_at__gte=since,
            sale__status__in=PAID_SALE_STATUSES,
            product__isnull=False,
        )
        .values_list("sale_id", "product_id")
        .iterator(chunk_size=5000)
    )

    sale_to_products = defaultdict(set)
    for sale_id, product_id in rows:
        sale_to_products[sale_id].add(str(product_id))

    total_sales = len(sale_to_products)
    if total_sales == 0:
        return []

    product_sale_counts = defaultdict(int)
    co = defaultdict(lambda: defaultdict(int))
    for pids in sale_to_products.values():
        p_list = sorted(pids)
        for p in p_list:
            product_sale_counts[p] += 1
        n = len(p_list)
        for i in range(n):
            a = p_list[i]
            for j in range(i + 1, n):
                b = p_list[j]
                co[a][b] += 1
                co[b][a] += 1

    result = []
    for prod_a in sorted(co):
        sales_a = product_sale_counts[prod_a]
        together = []
        for prod_b, count in co[prod_a].items():
            if count < PAIRS_MIN_COUNT or prod_b in excluded_ids:
                continue
            confidence = count / sales_a
            share_b = product_sale_counts[prod_b] / total_sales
            lift = confidence / share_b if share_b else 0.0
            together.append({
                "product_id": prod_b,
                "count": count,
                "confidence": round(confidence, 4),
                "lift": round(lift, 2),
            })
        if together:
            together.sort(key=lambda x: (-x["count"], -x["lift"], x["product_id"]))
            result.append({"product_id": prod_a, "together": together[:limit]})
    return result


def pairs_content_hash(pairs_data) -> str:
    return hashlib.sha1(
        json.dumps(pairs_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def store_company_recommendation_pairs(company_id):
    """Пересчитать и сохранить пары компании. Вызывается только из ночной задачи."""
    pairs_data = compute_company_recommendation_pairs(company_id)
    etag = pairs_content_hash(pairs_data)
    now = timezone.now()
    obj = RecommendationPairsCache.objects.filter(company_id=company_id).first()
    if obj is None:
        RecommendationPairsCache.objects.create(
            company_id=company_id, computed_at=now, pairs_data=pairs_data, etag=etag
        )
    elif obj.etag != etag:
        obj.pairs_data = pairs_data
        obj.etag = etag
        obj.computed_at = now
        obj.save(update_fields=["pairs_data", "etag", "computed_at", "updated_at"])
    else:
        # Данные не изменились — ETag прежний, касса получит 304.
        obj.computed_at = now
        obj.save(update_fields=["computed_at", "updated_at"])
    return len(pairs_data)


def _etag_matches(header_value, etag_value) -> bool:
    if not header_value:
        return False
    header_value = header_value.strip()
    if header_value == "*":
        return True
    for token in header_value.split(","):
        t = token.strip()
        if t.startswith("W/"):
            t = t[2:]
        if t.strip('"') == etag_value:
            return True
    return False


def _int_param(params, name, default, lo, hi):
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: "Ожидается целое число."})
    return max(lo, min(hi, v))


class RecommendationPairsAPIView(APIView):
    """
    GET /api/main/recommendations/pairs/?days=90&limit=10
    Только чтение готового результата ночной задачи. ETag / If-None-Match → 304.
    days фиксирован (90): пары считаются ночью по 90 дням.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request)
        limit = _int_param(request.query_params, "limit", PAIRS_DEFAULT_LIMIT, 1, PAIRS_STORED_LIMIT)
        _int_param(request.query_params, "days", PAIRS_DAYS, 1, 365)  # валидация; значение фиксировано

        cache_obj = RecommendationPairsCache.objects.filter(company=company).first()
        if cache_obj is None:
            self._schedule_first_compute(company.id)
            pairs, computed_at, base_etag = [], None, "empty"
        else:
            pairs = [
                {"product_id": p["product_id"], "together": (p.get("together") or [])[:limit]}
                for p in (cache_obj.pairs_data or [])
            ]
            computed_at = cache_obj.computed_at.isoformat()
            base_etag = cache_obj.etag or pairs_content_hash(cache_obj.pairs_data or [])

        etag_value = hashlib.sha1(f"{base_etag}:{limit}".encode()).hexdigest()
        etag = f'"{etag_value}"'
        if _etag_matches(request.headers.get("If-None-Match"), etag_value):
            res = Response(status=status.HTTP_304_NOT_MODIFIED)
            res["ETag"] = etag
            return res

        res = Response({"computed_at": computed_at, "pairs": pairs}, status=status.HTTP_200_OK)
        res["ETag"] = etag
        return res

    @staticmethod
    def _schedule_first_compute(company_id):
        """Новая компания без ночного результата: ставим разовый расчёт в очередь (не чаще раза в час)."""
        try:
            if not cache.add(f"reco_pairs_first:{company_id}", 1, 3600):
                return
            from apps.main.recommendations_tasks import compute_company_recommendation_pairs_task
            compute_company_recommendation_pairs_task.apply_async(args=[str(company_id)], countdown=60)
        except Exception:  # брокер недоступен — не роняем запрос
            logger.warning("Failed to enqueue first pairs compute for %s", company_id, exc_info=True)


# =========================================================================
# 2.5 План продаж
# =========================================================================

def _month_bounds(month):
    year, mo = int(month[:4]), int(month[5:7])
    start = date(year, mo, 1)
    end = date(year + 1, 1, 1) if mo == 12 else date(year, mo + 1, 1)
    return start, end


class SalesTargetAPIView(APIView):
    """
    GET /api/main/sales-targets/?month=2026-10&branch=
    PUT /api/main/sales-targets/  {"month": "2026-10", "revenue_target": "1500000.00", "branch": null}
    Запись — только владелец.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request)
        month = request.query_params.get("month") or timezone.localdate().strftime("%Y-%m")
        if not MONTH_RE.match(month):
            raise ValidationError({"month": "Формат месяца YYYY-MM."})
        branch = _resolve_branch(request, company, request.query_params.get("branch"))

        target = SalesTarget.objects.filter(company=company, month=month, branch=branch).first()
        revenue_target = target.revenue_target if target else ZERO_MONEY

        start_date, end_date = _month_bounds(month)
        start, end = _local_range(start_date, end_date - timedelta(days=1))

        sales_qs = Sale.objects.filter(
            company=company, created_at__gte=start, created_at__lt=end, status__in=PAID_SALE_STATUSES
        )
        returns_qs = SaleReturn.objects.filter(company=company, created_at__gte=start, created_at__lt=end)
        if branch is not None:
            sales_qs = sales_qs.filter(branch=branch)
            returns_qs = returns_qs.filter(sale__branch=branch)
        # Аудит 12.11: total частично возвращённого чека уже уменьшен, полностью возвращённый — canceled
        # и сюда не попадает; вычитать SaleReturn второй раз нельзя.
        sold = sales_qs.aggregate(s=Sum("total"))["s"] or ZERO_MONEY

        upsell_revenue = compute_upsell_revenue(company, branch, start, end)[0]

        today = timezone.localdate()
        days_in_month = (end_date - start_date).days
        if today < start_date:
            days_passed = 0
        elif today >= end_date:
            days_passed = days_in_month
        else:
            days_passed = (today - start_date).days  # полностью прошедшие дни
        days_remaining = days_in_month - days_passed   # включая сегодня

        needed = max(ZERO_MONEY, revenue_target - sold)
        if revenue_target > 0 and days_remaining > 0:
            daily_pace = (needed / Decimal(days_remaining)).quantize(MONEY_Q)
        else:
            daily_pace = ZERO_MONEY
        expected_so_far = revenue_target * Decimal(days_passed) / Decimal(days_in_month)
        is_on_track = sold >= expected_so_far if revenue_target > 0 else True

        return Response({
            "month": month,
            "branch": str(branch.id) if branch else None,
            "revenue_target": _money(revenue_target),
            "revenue_actual": _money(sold),
            "remaining": _money(needed),
            "days_in_month": days_in_month,
            "days_remaining": days_remaining,
            "daily_pace_required": _money(daily_pace),
            "is_on_track": is_on_track,
            "upsell_revenue": _money(upsell_revenue),
        })

    def put(self, request):
        company = _get_company(request)
        if not _is_owner(request.user, company):
            raise PermissionDenied("Изменять план продаж может только владелец.")
        ser = SalesTargetWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        branch = None
        if data.get("branch"):
            branch = Branch.objects.filter(id=data["branch"], company=company).first()
            if branch is None:
                raise ValidationError({"branch": "Филиал не найден."})

        try:
            with transaction.atomic():
                target, _ = SalesTarget.objects.update_or_create(
                    company=company, branch=branch, month=data["month"],
                    defaults={"revenue_target": data["revenue_target"]},
                )
        except IntegrityError:  # параллельная запись того же месяца
            target = SalesTarget.objects.get(company=company, branch=branch, month=data["month"])
            target.revenue_target = data["revenue_target"]
            target.save(update_fields=["revenue_target", "updated_at"])

        return Response({
            "month": target.month,
            "revenue_target": _money(target.revenue_target),
            "branch": str(target.branch_id) if target.branch_id else None,
        }, status=status.HTTP_200_OK)
