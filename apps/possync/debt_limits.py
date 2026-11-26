"""
Проверка лимита долга на сервере (ТЗ веб-кассы, п. 2).

Лимит кассира — настройки кассы `main/app-settings/nurmarket-mobile/` → ключ `DebtAccess`:
    [{"user": "<id>", "mode": "allow" | "deny" | "limit", "max_receipts": 5, "max_sum": 20000}]
Лимит клиента — `ClientDebtLimit.limit` (пусто — без лимита).

Проверка вызывается из pos/checkout/ ВНУТРИ транзакции продажи, после создания чека, под блокировкой
строк клиента и кассира (select_for_update): два параллельных чека выстраиваются в очередь, второй
видит долг первого и получает 400. Исключение откатывает всю продажу.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Q

from apps.main.kassa_views import _open_debts
from apps.main.models import Client, Sale
from apps.main.pos_utils import money
from apps.users.models import CompanyAppSettings
from core.exceptions import ApiError

from .models import ClientDebtLimit, CompanyDebtLimits

ZERO = Decimal("0.00")
SETTINGS_APP = "nurmarket-mobile"


def _fmt(x) -> str:
    x = money(x)
    return f"{x:,.2f}".replace(",", " ").replace(".00", "")


def _num(x):
    """Число для JSON: целое, если без копеек."""
    x = money(x)
    return int(x) if x == x.to_integral_value() else float(x)


def client_debt_limit(client) -> Decimal | None:
    row = ClientDebtLimit.objects.filter(client_id=client.pk).first()
    return row.limit if row is not None else None


def client_current_debt(client, *, exclude_sale_id=None) -> Decimal:
    total = ZERO
    for _cid, sale_id, remaining, _created in _open_debts(
        [client.company_id], Client.objects.filter(pk=client.pk).values("id")
    ):
        if exclude_sale_id and sale_id == exclude_sale_id:
            continue
        total += remaining
    return money(total)


def client_debt_payload(client) -> dict:
    limit = client_debt_limit(client)
    if limit is None:
        return {"debt_limit": None, "debt_available": None}
    avail = max(ZERO, money(limit - client_current_debt(client)))
    return {"debt_limit": str(money(limit)), "debt_available": str(avail)}


def cashier_rule(company_id, branch_id, user_id) -> dict | None:
    """Правило DebtAccess для кассира: настройки филиала перекрывают настройки компании."""
    rows = list(
        CompanyAppSettings.objects.filter(company_id=company_id, app=SETTINGS_APP)
        .filter(Q(branch__isnull=True) | Q(branch_id=branch_id))
        .order_by("branch_id")  # NULL (компания) раньше филиала
    )
    access = None
    for row in rows:
        val = (row.settings or {}).get("DebtAccess")
        if isinstance(val, list):
            access = val  # филиал перезаписывает компанию
    common = None
    for item in access or []:
        if not isinstance(item, dict):
            continue
        u = item.get("user")
        if isinstance(u, dict):
            u = u.get("id")
        if u is None:
            continue
        if str(u) == str(user_id):
            return item  # своё правило сотрудника
        if str(u) == "*" and common is None:
            common = item  # общее правило для всех остальных
    return common


def _dec(v):
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def cashier_open_debts(company_id, user_id, *, exclude_sale_id=None):
    """(число открытых долговых чеков, сумма остатка) кассира."""
    sale_ids = set(
        Sale.objects.filter(company_id=company_id, user_id=user_id, status=Sale.Status.DEBT)
        .exclude(pk=exclude_sale_id)
        .values_list("id", flat=True)
    )
    if not sale_ids:
        return 0, ZERO
    count, total = 0, ZERO
    seen = set()
    for _cid, sale_id, remaining, _c in _open_debts([company_id], Client.objects.filter(company_id=company_id).values("id")):
        if sale_id in sale_ids and sale_id not in seen:
            seen.add(sale_id)
            count += 1
        if sale_id in sale_ids:
            total += remaining
    return count, money(total)


def check_debt_limits(request, sale, client, data) -> None:
    """Вызывается из pos/checkout/ после создания чека. Бросает ApiError(debt_limit_exceeded)."""
    debt = money(sale.debt_initial or ZERO)
    if debt <= 0:
        return

    # 1) клиент — блокируем строку, чтобы параллельный чек подождал
    if client is not None:
        locked = Client.objects.select_for_update().get(pk=client.pk)
        limit = client_debt_limit(locked)
        if limit is not None:
            current = client_current_debt(locked, exclude_sale_id=sale.pk)
            if current + debt > limit:
                raise ApiError(
                    "debt_limit_exceeded",
                    f"Лимит долга клиента {_fmt(limit)} сом, текущий долг {_fmt(current)} сом",
                    top={"limit": _num(limit), "current": _num(current), "scope": "client"},
                )

    # 2) кассир — блокируем строку пользователя
    user = request.user
    from django.contrib.auth import get_user_model

    get_user_model().objects.select_for_update().filter(pk=user.pk).first()
    from apps.main.pos_serializers import _is_owner_like

    if _is_owner_like(user):
        return  # владелец и администратор — без лимитов кассира (лимит клиента выше уже проверен)
    rule = cashier_rule(sale.company_id, sale.branch_id, user.pk)
    if rule is None:
        return
    mode = str(rule.get("mode") or "allow").lower()
    if mode == "deny":
        raise ApiError(
            "debt_forbidden",
            "Этому кассиру нельзя продавать в долг",
            top={"limit": 0, "current": 0, "scope": "cashier"},
        )
    if mode != "limit":
        return
    count, total = cashier_open_debts(sale.company_id, user.pk, exclude_sale_id=sale.pk)
    max_receipts = _dec(rule.get("max_receipts"))
    max_sum = _dec(rule.get("max_sum"))
    if max_receipts is not None and max_receipts > 0 and count + 1 > max_receipts:
        raise ApiError(
            "debt_limit_exceeded",
            f"Лимит кассира: {int(max_receipts)} открытых долговых чеков, сейчас {count}",
            top={"limit": int(max_receipts), "current": count, "scope": "cashier_receipts"},
        )
    if max_sum is not None and max_sum > 0 and total + debt > max_sum:
        raise ApiError(
            "debt_limit_exceeded",
            f"Лимит долга кассира {_fmt(max_sum)} сом, текущий долг {_fmt(total)} сом",
            top={"limit": _num(max_sum), "current": _num(total), "scope": "cashier_sum"},
        )


# --- Общий лимит компании (ТЗ 26.10, Б-14 п. 3): один на все кассы магазина ---

def company_limits(company_id) -> CompanyDebtLimits | None:
    return CompanyDebtLimits.objects.filter(company_id=company_id).first()


def _company_tz(company_id, branch_id=None):
    from zoneinfo import ZoneInfo

    from apps.users.models import Branch

    name = "Asia/Bishkek"
    if branch_id:
        name = Branch.objects.filter(pk=branch_id).values_list("timezone", flat=True).first() or name
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Bishkek")


def day_usage(company_id, day=None, *, branch_id=None, exclude_sale_id=None, cashiers_only=False):
    """(число долговых чеков, сумма долга) за сутки компании в часовом поясе филиала."""
    from datetime import datetime, time, timedelta

    from django.utils import timezone

    tz = _company_tz(company_id, branch_id)
    day = day or timezone.now().astimezone(tz).date()
    start = datetime.combine(day, time.min, tzinfo=tz)
    qs = Sale.objects.filter(
        company_id=company_id, debt_initial__gt=0, created_at__gte=start, created_at__lt=start + timedelta(days=1)
    )
    if exclude_sale_id:
        qs = qs.exclude(pk=exclude_sale_id)
    count, total = 0, ZERO
    for sale_user_id, debt in qs.values_list("user_id", "debt_initial"):
        if cashiers_only and _user_is_owner_like(sale_user_id):
            continue
        count += 1
        total += debt or ZERO
    return count, money(total)


def _user_is_owner_like(user_id) -> bool:
    from django.contrib.auth import get_user_model

    from apps.main.pos_serializers import _is_owner_like

    u = get_user_model().objects.filter(pk=user_id).first()
    return bool(u and _is_owner_like(u))


def check_company_debt_limits(request, sale, client, *, offline=False) -> list:
    """
    Общий лимит компании. Возвращает warnings (для офлайн-досылки), иначе бросает ApiError.
    Владелец и администратор под лимит при applies_to=cashiers не попадают.
    """
    debt = money(sale.debt_initial or ZERO)
    if debt <= 0:
        return []
    lim = company_limits(sale.company_id)
    if lim is None:
        return []
    cashiers_only = lim.applies_to == CompanyDebtLimits.AppliesTo.CASHIERS
    if cashiers_only:
        from apps.main.pos_serializers import _is_owner_like

        if _is_owner_like(request.user):
            return []
    # блокировка строки лимита: параллельные чеки разных касс выстраиваются в очередь
    CompanyDebtLimits.objects.select_for_update().filter(pk=lim.pk).first()
    warnings = []

    def fail(code, detail, data):
        if offline:
            warnings.append({"code": code, "detail": detail, **data})
            return
        raise ApiError(code, detail, data=data)

    if lim.max_sales_per_day or lim.max_amount_per_day:
        count, total = day_usage(
            sale.company_id, branch_id=sale.branch_id, exclude_sale_id=sale.pk, cashiers_only=cashiers_only
        )
        if lim.max_sales_per_day and count + 1 > lim.max_sales_per_day:
            fail(
                "debt_daily_limit_exceeded",
                f"Дневной лимит: {lim.max_sales_per_day} долговых чеков, сегодня уже {count}",
                {"limit": lim.max_sales_per_day, "current": count, "scope": "sales_per_day"},
            )
        if lim.max_amount_per_day and total + debt > lim.max_amount_per_day:
            fail(
                "debt_daily_limit_exceeded",
                f"Дневной лимит долга {_fmt(lim.max_amount_per_day)} сом, сегодня уже {_fmt(total)} сом",
                {"limit": str(money(lim.max_amount_per_day)), "current": str(total), "scope": "amount_per_day"},
            )
    if lim.max_debt_per_client and client is not None:
        current = client_current_debt(client, exclude_sale_id=sale.pk)
        if current + debt > lim.max_debt_per_client:
            fail(
                "debt_client_limit_exceeded",
                f"Лимит долга на клиента {_fmt(lim.max_debt_per_client)} сом, текущий долг {_fmt(current)} сом",
                {"limit": str(money(lim.max_debt_per_client)), "current": str(current), "scope": "client"},
            )
    return warnings
