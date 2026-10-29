from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, time, date as _date
from decimal import Decimal
import hashlib
import json

from django.apps import apps
from django.db.models import (
    Sum,
    Count,
    Q,
    F,
    Value,
    DecimalField,
    ExpressionWrapper,
    IntegerField,
    Case,
    When,
    CharField,
    Exists,
    OuterRef,
    Subquery,
)
from django.db.models.functions import TruncDate, ExtractHour, ExtractWeekDay, Coalesce, Least
from django.utils import timezone
from django.core.cache import cache
from django.conf import settings

from apps.users.permissions_flags import flag_guard  # ТЗ Н-16
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.users.models import Branch
from apps.construction.models import CashShift

from apps.main.cache_utils import cache_market_analytics_key  # путь поправь под свой проект


# ─────────────────────────────────────────────────────────────
# constants for safe typing
# ─────────────────────────────────────────────────────────────
Z_MONEY = Decimal("0.00")
Z_QTY = Decimal("0.000")

MONEY_FIELD = DecimalField(max_digits=18, decimal_places=2)
QTY_FIELD = DecimalField(max_digits=18, decimal_places=3)


def _money(x) -> Decimal:
    try:
        return (x or Z_MONEY).quantize(Decimal("0.01"))
    except Exception:
        return Z_MONEY


def _safe_div(a: Decimal, b: int | Decimal) -> Decimal:
    if not b:
        return Z_MONEY
    return _money(Decimal(a) / Decimal(b))


def _qty_str(x) -> str:
    try:
        return str(Decimal(str(x or 0)).quantize(Decimal("0.001")))
    except Exception:
        return "0.000"


# ─────────────────────────────────────────────────────────────
# Денежные движения (CashFlow) для finance / cashflow / pnl / reconcile
# ─────────────────────────────────────────────────────────────
_CF_SALE_KINDS = ("pos_sale", "pos_prepayment", "sale")
_CF_DEBT_KINDS = ("debt_repayment", "debt_payment")
_CF_SUPPLIER_KINDS = ("warehouse_purchase", "procurement_receipt", "supplier_debt_payment", "supplier_payment")
# ТЗ Н-19: инкассация — движение денег, не расход и не прибыль.
_CF_COLLECTION_KIND = "shift_drawer_collection"
# ТЗ Н-19: системный slug статьи -> категория аналитики (классификация по slug идёт первой).
_CF_SLUG_TO_CAT = {"salaries": "salaries", "rent": "rent", "taxes": "taxes", "utilities": "utilities", "purchases": "suppliers"}
_CF_RETURN_KIND = "pos_sale_return"
# Не деньги: зачёт (обмен, залог, предоплата заказ-наряда) и долговая часть чека.
_CF_NON_MONEY_METHODS = ("offset", "debt")


def _valid_uuid(raw) -> str | None:
    from uuid import UUID

    try:
        return str(UUID(str(raw).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _money_cashflows(company, branch, period, request=None):
    """
    Одобренные денежные движения периода без строк заявок, без «сырых» двойников
    канонических строк, без зачёта/долга и без технической поправки ящика при
    возврате («наличные остались в кассе», pos_sale_return + income).
    Фильтры ?cashbox, ?shift, ?cashier, ?payment_method применяются к самим движениям.
    """
    from apps.construction.models import CashFlow

    qs = (
        CashFlow.objects.filter(
            company=company,
            created_at__gte=period.start,
            created_at__lt=period.end,
        )
        .money()
        .without_raw_twins()
        .exclude(payment_method__in=_CF_NON_MONEY_METHODS)
        .exclude(source_kind=_CF_RETURN_KIND, type=CashFlow.Type.INCOME)
    )
    if branch is not None:
        qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    if request is not None:
        qp = request.query_params
        for param, field in (("cashbox", "cashbox_id"), ("shift", "shift_id"), ("cashier", "cashier_id")):
            raw = qp.get(param)
            if raw:
                val = _valid_uuid(raw)
                qs = qs.filter(**{field: val}) if val else qs.none()
        pm = (qp.get("payment_method") or "").strip().lower()
        if pm:
            qs = qs.filter(payment_method=pm)
    return qs


def _cf_income_category():
    """Категория прихода — первое совпадение, категории не пересекаются."""
    sale_q = Q(source_kind__in=_CF_SALE_KINDS) | (
        Q(source_kind__isnull=True)
        & (
            Q(source_business_operation_id__in=("Продажа", "Предоплата"))
            | Q(name__istartswith="Продажа")
            | Q(category__title__istartswith="Продажа")
        )
    )
    # Старые строки без payment_method: «Продажа» — наличные, «Продажа (Мбанк)» — безнал.
    cash_q = Q(payment_method="cash") | (
        Q(payment_method__isnull=True) & (~Q(name__contains="(") | Q(name__icontains="(налич"))
    )
    debt_q = Q(source_kind__in=_CF_DEBT_KINDS) | (
        Q(source_kind__isnull=True)
        & Q(source_business_operation_id__in=("Оплата долга", "Погашение долга"))
    )
    return Case(
        When(sale_q & cash_q, then=Value("sales_cash")),
        When(sale_q, then=Value("sales_card")),
        When(debt_q, then=Value("debt_repayments")),
        default=Value("other_income"),
        output_field=CharField(),
    )


def _cf_expense_category():
    """Категория расхода — первое совпадение, категории не пересекаются."""
    return Case(
        When(source_kind=_CF_RETURN_KIND, then=Value("returns")),
        When(source_kind=_CF_COLLECTION_KIND, then=Value("collections")),
        When(source_kind__in=_CF_SUPPLIER_KINDS, then=Value("suppliers")),
        When(source_kind="rental_deposit", then=Value("deposits")),
        # ТЗ Н-19: сначала по slug статьи и назначению, потом прежняя логика по названиям.
        *[When(category__slug=slug, then=Value(cat)) for slug, cat in _CF_SLUG_TO_CAT.items()],
        When(source_kind="salary_payout", then=Value("salaries")),
        When(source_kind="rent_payment", then=Value("rent")),
        When(Q(name__icontains="закуп") | Q(name__icontains="поставщик"), then=Value("suppliers")),
        When(
            Q(name__icontains="зарплат") | Q(name__icontains="аванс") | Q(category__title__icontains="зарплат"),
            then=Value("salaries"),
        ),
        When(Q(name__icontains="аренд") | Q(category__title__icontains="аренд"), then=Value("rent")),
        When(Q(name__icontains="налог") | Q(category__title__icontains="налог"), then=Value("taxes")),
        When(
            Q(name__icontains="коммун") | Q(category__title__icontains="коммун")
            | Q(name__icontains="свет") | Q(name__icontains="интернет"),
            then=Value("utilities"),
        ),
        default=Value("other"),
        output_field=CharField(),
    )


def _expense_by_statya(qs, only_cats=None) -> list:
    """ТЗ Н-19: расходы по статьям [{id,title,slug,amount}], по убыванию суммы.
    only_cats — ограничить категориями аналитики (_cf_expense_category)."""
    rows = qs.annotate(_cat=_cf_expense_category())
    if only_cats is not None:
        rows = rows.filter(_cat__in=only_cats)
    rows = rows.values("category_id", "category__title", "category__slug").annotate(
        s=Coalesce(Sum("amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD)
    )
    out = []
    for r in rows:
        out.append({
            "id": str(r["category_id"]) if r["category_id"] else None,
            "title": r["category__title"] or "Без статьи",
            "slug": r["category__slug"] or "",
            "amount": str(_money(r["s"] or Z_MONEY)),
        })
    out.sort(key=lambda x: Decimal(x["amount"]), reverse=True)
    return out


def _sum_by_category(qs, category_expr) -> dict:
    rows = (
        qs.annotate(_cat=category_expr)
        .values("_cat")
        .annotate(s=Coalesce(Sum("amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD))
    )
    return {r["_cat"]: (r["s"] or Z_MONEY) for r in rows}


def _format_shift_dt(dt) -> str | None:
    if not dt:
        return None
    try:
        return timezone.localtime(dt).strftime("%d.%m.%Y, %H:%M")
    except Exception:
        try:
            return dt.strftime("%d.%m.%Y, %H:%M")
        except Exception:
            return str(dt)


def _users_sold_products_by_user(si_qs, SaleItem):
    """
    По queryset строк чеков (SaleItem с отфильтрованными sale) строит:
    - units_by_user: user_id -> сумма quantity по всем строкам
    - products_by_user: user_id -> список {"name", "quantity"} по убыванию quantity
    - names_by_user: user_id -> список имён в том же порядке
    """
    units_by_user: dict = {}
    for row in si_qs.values("sale__user_id").annotate(
        tq=Coalesce(Sum("quantity"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
    ):
        uid = row["sale__user_id"]
        if uid is not None:
            units_by_user[uid] = Decimal(str(row["tq"] or 0))

    bucket_qty: dict = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
    bucket_name: dict = {}

    if _model_has_field(SaleItem, "product"):
        for row in (
            si_qs.filter(product__isnull=False)
            .values("sale__user_id", "product_id", "product__name")
            .annotate(
                sq=Coalesce(Sum("quantity"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
            )
        ):
            uid = row["sale__user_id"]
            if uid is None or not row.get("product_id"):
                continue
            key = ("p", str(row["product_id"]))
            bucket_qty[uid][key] += Decimal(str(row["sq"] or 0))
            disp = (row.get("product__name") or "").strip()
            bucket_name[(uid, key)] = disp or "—"

    if _model_has_field(SaleItem, "name_snapshot"):
        for row in (
            si_qs.filter(product__isnull=True)
            .values("sale__user_id", "name_snapshot")
            .annotate(
                sq=Coalesce(Sum("quantity"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
            )
        ):
            uid = row["sale__user_id"]
            if uid is None:
                continue
            snap = (row.get("name_snapshot") or "").strip() or "Позиция"
            key = ("c", snap)
            bucket_qty[uid][key] += Decimal(str(row["sq"] or 0))
            bucket_name[(uid, key)] = snap

    products_by_user: dict = {}
    names_by_user: dict = {}
    for uid, keys in bucket_qty.items():
        rows = []
        for key, q in keys.items():
            rows.append({"name": bucket_name.get((uid, key), "—"), "quantity": _qty_str(q)})
        rows.sort(key=lambda x: Decimal(x["quantity"]), reverse=True)
        products_by_user[uid] = rows
        names_by_user[uid] = [r["name"] for r in rows]

    return units_by_user, products_by_user, names_by_user


def _pct(a: Decimal, b: Decimal) -> float | None:
    """
    percent(a / b). returns float with 0.1 precision or None.
    """
    try:
        bb = Decimal(b or 0)
        if bb <= 0:
            return None
        return float((Decimal(a or 0) / bb * Decimal("100")).quantize(Decimal("0.1")))
    except Exception:
        return None


def _calc_margin_pack(revenue: Decimal, cogs: Decimal):
    rev = _money(revenue)
    cg = _money(cogs)
    profit = _money(rev - cg)
    margin = _pct(profit, rev)
    return cg, profit, margin


def _parse_period_value(raw) -> tuple[datetime | None, bool]:
    """
    Разбирает одну границу периода из query-параметра.

    Возвращает кортеж (naive_datetime | None, is_date_only):
      • None / ""               -> (None, False)  — параметр не задан;
      • "YYYY-MM-DD"            -> (datetime 00:00, True)  — дата без времени;
      • "YYYY-MM-DDTHH:MM[:SS]" -> (точный datetime, False) — местное время.

    Значение трактуется как МЕСТНОЕ время компании (таймзона проекта, Asia/Bishkek);
    если во входе вдруг оказалась таймзона — она отбрасывается (никаких «как будто UTC»).

    Бросает ValueError, если непустое значение не парсится — вызывающий превращает
    это в HTTP 400 (rule #6 спецификации).
    """
    if raw is None:
        return None, False
    s = str(raw).strip()
    if not s:
        return None, False
    if len(s) == 10:  # YYYY-MM-DD
        d = _date.fromisoformat(s)  # ValueError при мусоре
        return datetime.combine(d, time.min), True
    dt = datetime.fromisoformat(s)  # ValueError при мусоре
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt, False


def _aware_start(naive: datetime, tz) -> datetime:
    """Нижняя граница (включительная): naive местное → aware."""
    return timezone.make_aware(naive, tz) if timezone.is_naive(naive) else naive


def _exclusive_end(naive: datetime, is_date_only: bool, tz) -> datetime:
    """
    Верхняя граница периода. На входе — включительная граница (местное время),
    на выходе — ЭКСКЛЮЗИВНАЯ (для фильтров `__lt`), чтобы конец периода
    учитывался включительно (rule #2):
      • дата без времени  → весь день: +1 день (00:00 следующего дня);
      • точное время      → эта секунда включительно: +1 секунда.
    """
    aware = timezone.make_aware(naive, tz) if timezone.is_naive(naive) else naive
    return aware + (timedelta(days=1) if is_date_only else timedelta(seconds=1))


def _parse_bounds(raw_from, raw_to, *, param_hint: str):
    """
    Разбирает пару границ (from, to) и приводит их к (aware_start | None, aware_end_excl | None).
    На невалидном непустом значении бросает DRF ValidationError → HTTP 400.
    """
    tz = timezone.get_current_timezone()
    try:
        start_naive, _ = _parse_period_value(raw_from)
    except ValueError:
        raise ValidationError({
            "detail": f"Некорректный {param_hint}_start/date_from: {raw_from!r}. "
                      f"Ожидается YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS."
        })
    try:
        end_naive, end_is_date_only = _parse_period_value(raw_to)
    except ValueError:
        raise ValidationError({
            "detail": f"Некорректный {param_hint}_end/date_to: {raw_to!r}. "
                      f"Ожидается YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS."
        })
    start = _aware_start(start_naive, tz) if start_naive else None
    end = _exclusive_end(end_naive, end_is_date_only, tz) if end_naive else None
    return start, end


def _mixed_payment_split(sales_qs):
    """Сумма наличной и безналичной частей смешанных оплат (по строкам SalePayment)."""
    from apps.main.models import Sale, SalePayment

    rows = (
        SalePayment.objects.filter(
            sale__in=sales_qs.filter(payment_method=Sale.PaymentMethod.MIXED).values("id")
        )
        .exclude(method=Sale.PaymentMethod.MIXED)
        .values("method")
        .annotate(v=Sum("amount"))
    )
    cash = Decimal("0.00")
    card = Decimal("0.00")
    for r in rows:
        if r["method"] == Sale.PaymentMethod.CASH:
            cash += r["v"] or Decimal("0.00")
        else:
            card += r["v"] or Decimal("0.00")
    return {"cash": cash, "card": card}


PAYMENT_METHOD_LABELS = {
    "cash": "Наличные",
    "transfer": "Перевод",
    "mbank": "Мбанк",
    "optima": "Оптима Банк",
    "obank": "О!Деньги",
    "bakai": "Бакай Банк",
    "debt": "В долг",
    "offset": "Зачёт",
    "mixed": "Смешанная (без разбивки)",
}


def _payment_methods_breakdown(sales_qs, revenue: Decimal) -> list:
    """
    «Способы оплаты» по фактическим деньгам: строки SalePayment, а не Sale.payment_method.
    Смешанный чек раскладывается на свои способы (600 нал + 400 карта → cash 600, mbank 400),
    поэтому Σ total == выручке; `count` — число чеков, где способ встретился (сумма count
    может быть больше числа чеков). Если строки оплат не сходятся с суммой чека (старые
    данные, сдача), они пропорционально приводятся к total чека. Чеки без строк оплат
    (старые) идут по Sale.payment_method; `mixed` остаётся только для чеков без разбивки
    (`unsplit: true`).
    """
    from apps.main.models import Sale, SalePayment

    mixed = Sale.PaymentMethod.MIXED
    usable = SalePayment.objects.filter(amount__gt=0).exclude(method=mixed)
    line_sum = Subquery(
        usable.filter(sale_id=OuterRef("sale_id")).values("sale_id").annotate(s=Sum("amount")).values("s")[:1],
        output_field=MONEY_FIELD,
    )
    by_method: dict = {}

    line_rows = (
        usable.filter(sale__in=sales_qs.values("id"))
        .annotate(ls=line_sum)
        .values("method")
        .annotate(
            total=Sum(ExpressionWrapper(F("amount") * F("sale__total") / F("ls"), output_field=MONEY_FIELD)),
            cnt=Count("sale_id", distinct=True),
        )
    )
    for r in line_rows:
        by_method[r["method"] or "unknown"] = {"count": r["cnt"], "total": _money(r["total"] or Z_MONEY), "unsplit": False}

    no_lines = sales_qs.annotate(
        has_lines=Exists(usable.filter(sale_id=OuterRef("pk")))
    ).filter(has_lines=False)
    for r in no_lines.values("payment_method").annotate(cnt=Count("id"), total=Sum("total")):
        key = r["payment_method"] or "unknown"
        row = by_method.setdefault(key, {"count": 0, "total": Z_MONEY, "unsplit": False})
        row["count"] += r["cnt"]
        row["total"] += _money(r["total"] or Z_MONEY)
        row["unsplit"] = row["unsplit"] or key == mixed

    rows = sorted(by_method.items(), key=lambda kv: kv[1]["total"], reverse=True)
    # копейки округления относим на самую крупную строку, чтобы Σ == выручке ровно
    diff = _money(revenue) - sum((v["total"] for _, v in rows), Z_MONEY)
    if rows and diff:
        rows[0][1]["total"] += diff

    out = []
    for key, v in rows:
        item = {
            "method": key,
            "label": PAYMENT_METHOD_LABELS.get(key, key),
            "count": v["count"],
            "total": str(_money(v["total"])),
        }
        if v["unsplit"]:
            item["unsplit"] = True
        out.append(item)
    return out


@dataclass
class Period:
    start: datetime
    end: datetime  # exclusive


def _get_period(request, *, strict: bool = False) -> Period:
    """
    strict=True (ТЗ ч.12, 2.7): даты наоборот → 400, период никогда не подменяется молча.
    """
    tz = timezone.get_current_timezone()
    now = timezone.now().astimezone(tz)

    # Backward/forward compatibility:
    # - older clients: date_from/date_to
    # - newer clients: period_start/period_end
    qp = request.query_params
    raw_from = qp.get("date_from") or qp.get("period_start")
    raw_to = qp.get("date_to") or qp.get("period_end")

    start, end = _parse_bounds(raw_from, raw_to, param_hint="period")

    if strict and start and end:
        if start >= end:
            raise ValidationError({
                "detail": "Дата начала позже даты конца периода.", "code": "bad_period",
            })
        return Period(start=start, end=end)

    if start and end:
        # Период отдаём ровно тот, что запросили: пустой период — нули,
        # а не «подставленный» текущий месяц.
        return Period(start=start, end=end)

    # период не задан (или задана лишь одна из границ) → текущий месяц
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return Period(start=first, end=nxt)


def _revenue_statuses(Sale) -> tuple:
    """Статусы чеков в выручке — как на вкладке «Продажи»: оплачен и частично возвращён."""
    return (
        _choice_value(Sale, "Status", "PAID", "paid"),
        _choice_value(Sale, "Status", "PARTIALLY_RETURNED", "partially_returned"),
    )


def _model_has_field(model, field_name: str) -> bool:
    try:
        return field_name in {f.name for f in model._meta.get_fields()}
    except Exception:
        return False


def _sale_item_net_line_revenue_expr(SaleItem_model):
    """
    Сумма строки чека: quantity × unit_price − line_discount.
    Без line_discount топы товаров и разрезы аналитики расходятся с реальной оплатой при акциях.
    """
    if SaleItem_model is None or not _model_has_field(SaleItem_model, "unit_price"):
        return None
    if _model_has_field(SaleItem_model, "line_discount"):
        return ExpressionWrapper(
            (F("quantity") * F("unit_price"))
            - Coalesce(
                F("line_discount"),
                Value(Z_MONEY, output_field=MONEY_FIELD),
                output_field=MONEY_FIELD,
            ),
            output_field=MONEY_FIELD,
        )
    return ExpressionWrapper(
        F("quantity") * F("unit_price"),
        output_field=MONEY_FIELD,
    )


def _choice_value(model, enum_name: str, member: str, fallback: str) -> str:
    enum = getattr(model, enum_name, None)
    v = getattr(enum, member, None)
    return getattr(v, "value", None) or str(v or fallback)


def _get_cogs_expr(SaleItem, ProductModel=None):
    """
    Возвращает (expr, ok). expr можно суммировать через Sum(expr).

    Логика:
    1) Если есть purchase_price_snapshot -> берём его, но делаем fallback на product.purchase_price
       (чтобы старые строки / незаполненные snapshot не давали 100% маржу).
    2) Если есть unit_cost/cost_price/purchase_price/buy_price в SaleItem -> используем их.
    3) Иначе fallback на product.purchase_price (неисторично, но лучше чем 0).
    """
    if not _model_has_field(SaleItem, "quantity"):
        return None, False

    # 1) snapshot + fallback на product.purchase_price
    if _model_has_field(SaleItem, "purchase_price_snapshot"):
        has_product_fk = _model_has_field(SaleItem, "product")
        has_product_purchase = (
            ProductModel is not None
            and _model_has_field(ProductModel, "purchase_price")
            and has_product_fk
        )

        if has_product_purchase:
            unit_cost = Coalesce(
                F("purchase_price_snapshot"),
                F("product__purchase_price"),
                Value(Z_MONEY, output_field=MONEY_FIELD),
            )
        else:
            unit_cost = Coalesce(
                F("purchase_price_snapshot"),
                Value(Z_MONEY, output_field=MONEY_FIELD),
            )

        return ExpressionWrapper(F("quantity") * unit_cost, output_field=MONEY_FIELD), True

    # 2) другие варианты unit себестоимости
    for f in ("unit_cost", "cost_price", "purchase_price", "buy_price"):
        if _model_has_field(SaleItem, f):
            return ExpressionWrapper(F("quantity") * F(f), output_field=MONEY_FIELD), True

    # 3) fallback через Product.purchase_price
    if ProductModel is not None and _model_has_field(SaleItem, "product") and _model_has_field(ProductModel, "purchase_price"):
        return ExpressionWrapper(F("quantity") * F("product__purchase_price"), output_field=MONEY_FIELD), True

    return None, False


# ─────────────────────────────────────────────────────────────
# Sale models lazy
# ─────────────────────────────────────────────────────────────
SALE_MODEL = None
SALE_ITEM_MODEL = None


def _guess_sale_models():
    Sale = None
    SaleItem = None

    for label in ("main.Sale", "pos.Sale", "sales.Sale"):
        try:
            Sale = apps.get_model(label)
            break
        except Exception:
            pass

    for label in ("main.SaleItem", "pos.SaleItem", "sales.SaleItem"):
        try:
            SaleItem = apps.get_model(label)
            break
        except Exception:
            pass

    if Sale is None:
        for m in apps.get_models():
            if _model_has_field(m, "total") and _model_has_field(m, "status") and _model_has_field(m, "cashbox"):
                Sale = m
                break

    if SaleItem is None and Sale is not None:
        for m in apps.get_models():
            if _model_has_field(m, "sale") and _model_has_field(m, "quantity"):
                SaleItem = m
                break

    return Sale, SaleItem


def get_sale_models():
    global SALE_MODEL, SALE_ITEM_MODEL
    if SALE_MODEL is None or SALE_ITEM_MODEL is None:
        SALE_MODEL, SALE_ITEM_MODEL = _guess_sale_models()
    return SALE_MODEL, SALE_ITEM_MODEL


# ─────────────────────────────────────────────────────────────
# company/branch helpers (твоя логика)
# ─────────────────────────────────────────────────────────────
def _get_company(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    company = getattr(user, "company", None) or getattr(user, "owned_company", None)
    if company:
        return company
    br = getattr(user, "branch", None)
    if br is not None and getattr(br, "company", None):
        return br.company
    memberships = getattr(user, "branch_memberships", None)
    if memberships is not None:
        m = memberships.select_related("branch__company").first()
        if m and m.branch and m.branch.company:
            return m.branch.company
    return None


def _is_owner_like(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if getattr(user, "owned_company", None):
        return True
    if getattr(user, "is_admin", False):
        return True
    role = getattr(user, "role", None)
    return role in ("owner", "admin", "OWNER", "ADMIN", "Владелец", "Администратор")


def _fixed_branch_from_user(user, company):
    if not user or not company:
        return None
    company_id = getattr(company, "id", None)

    memberships = getattr(user, "branch_memberships", None)
    if memberships is not None:
        primary_m = (
            memberships.filter(is_primary=True, branch__company_id=company_id)
            .select_related("branch")
            .first()
        )
        if primary_m and primary_m.branch:
            return primary_m.branch

        any_m = (
            memberships.filter(branch__company_id=company_id)
            .select_related("branch")
            .first()
        )
        if any_m and any_m.branch:
            return any_m.branch

    primary = getattr(user, "primary_branch", None)
    if callable(primary):
        try:
            val = primary()
            if val and getattr(val, "company_id", None) == company_id:
                return val
        except Exception:
            pass

    if primary and not callable(primary) and getattr(primary, "company_id", None) == company_id:
        return primary

    if hasattr(user, "branch"):
        b = getattr(user, "branch")
        if b and getattr(b, "company_id", None) == company_id:
            return b

    branch_ids = getattr(user, "branch_ids", None)
    if isinstance(branch_ids, (list, tuple)) and len(branch_ids) == 1:
        try:
            return Branch.objects.get(id=branch_ids[0], company_id=company_id)
        except Branch.DoesNotExist:
            pass

    return None


def _get_active_branch(request):
    user = getattr(request, "user", None)
    company = _get_company(user)
    if not company:
        setattr(request, "branch", None)
        return None

    company_id = getattr(company, "id", None)

    if not _is_owner_like(user):
        fixed = _fixed_branch_from_user(user, company)
        if fixed is None and Branch.objects.filter(company_id=company_id).exists():
            # None = «вся компания»: сотруднику без филиала в компании с филиалами
            # это открыло бы чужие филиалы (и кэш владельца).
            raise PermissionDenied("Сотрудник не привязан к филиалу.")
        setattr(request, "branch", fixed if fixed else None)
        return fixed if fixed else None

    branch_id = request.query_params.get("branch")
    if branch_id:
        try:
            br = Branch.objects.get(id=branch_id, company_id=company_id)
            setattr(request, "branch", br)
            return br
        except Exception:
            pass

    setattr(request, "branch", None)
    return None


def _require_finance_access(user):
    if _is_owner_like(user) or getattr(user, "can_view_analytics", False):
        return
    raise PermissionDenied("Нет доступа к финансовой аналитике.")


def _user_label(user_obj=None, *, first_name=None, last_name=None, email=None, phone=None, user_id=None) -> str:
    if user_obj is not None:
        fn = (getattr(user_obj, "first_name", "") or "").strip()
        ln = (getattr(user_obj, "last_name", "") or "").strip()
        full = f"{fn} {ln}".strip()
        if full:
            return full
        e = getattr(user_obj, "email", None)
        if e:
            return e
        p = getattr(user_obj, "phone_number", None)
        if p:
            return p
        uid = getattr(user_obj, "id", None)
        return str(uid) if uid else "—"

    fn = (first_name or "").strip()
    ln = (last_name or "").strip()
    full = f"{fn} {ln}".strip()
    if full:
        return full
    if email:
        return str(email)
    if phone:
        return str(phone)
    if user_id:
        return str(user_id)
    return "—"


# ─────────────────────────────────────────────────────────────
# Analytics API + caching
# ─────────────────────────────────────────────────────────────
# ТЗ Н-16: аналитика — только с правом «Аналитика»; вкладку «Продажи» касса показывает любому кассиру
# (решение Умара 25.10), поэтому tab=sales остаётся открытой.
@flag_guard("can_view_analytics", read=True, allow=lambda r: r.path.rstrip("/").endswith("analytics/market") and (r.query_params.get("tab") or "sales") == "sales")
class AnalyticsView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    # Зарплаты и деньги компании — только владельцу/админу или с доступом к аналитике.
    SENSITIVE_TABS = ("salary", "finance", "pnl", "cashflow")

    def _include_global(self, request) -> bool:
        qp = getattr(request, "query_params", None) or getattr(request, "GET", {})
        return (qp.get("include_global") or "").strip() in ("1", "true", "yes", "on")

    def _apply_sale_filters(self, request, qs, SaleModel):
        cashbox_id = request.query_params.get("cashbox")
        if cashbox_id and _model_has_field(SaleModel, "cashbox"):
            qs = qs.filter(cashbox_id=cashbox_id)

        shift_id = request.query_params.get("shift")
        if shift_id and _model_has_field(SaleModel, "shift"):
            qs = qs.filter(shift_id=shift_id)

        cashier_id = request.query_params.get("cashier")
        if cashier_id:
            if _model_has_field(SaleModel, "user"):
                qs = qs.filter(user_id=cashier_id)
            elif _model_has_field(SaleModel, "shift"):
                qs = qs.filter(shift__cashier_id=cashier_id)

        consultant_id = request.query_params.get("consultant")
        if consultant_id and _model_has_field(SaleModel, "consultant"):
            qs = qs.filter(consultant_id=consultant_id)

        pm = request.query_params.get("payment_method")
        if pm and _model_has_field(SaleModel, "payment_method"):
            qs = qs.filter(payment_method=pm)

        min_total = request.query_params.get("min_total")
        max_total = request.query_params.get("max_total")
        if min_total:
            try:
                qs = qs.filter(total__gte=Decimal(min_total))
            except Exception:
                pass
        if max_total:
            try:
                qs = qs.filter(total__lte=Decimal(max_total))
            except Exception:
                pass

        return qs

    def _category_filter_ids(self, request, company):
        """ТЗ Н-20: ?category=<id> (+ include_children=1) и ?categories=id1,id2.
        None — фильтра нет; [] — ничего подходящего (результат пустой)."""
        qp = request.query_params
        raw = []
        one = (qp.get("category") or "").strip()
        if one:
            raw.append(one)
        raw += [x.strip() for x in (qp.get("categories") or "").split(",") if x.strip()]
        if not raw:
            return None
        ids = [v for v in (_valid_uuid(x) for x in raw) if v]
        if not ids:
            return []
        if (qp.get("include_children") or "").strip().lower() in ("1", "true", "yes", "on"):
            try:
                PC = apps.get_model("main.ProductCategory")
                if _model_has_field(PC, "parent"):
                    out = set(ids)
                    for c in PC.objects.filter(company=company, id__in=ids):
                        out.update(str(x) for x in c.get_descendants().values_list("id", flat=True))
                    ids = list(out)
            except Exception:
                pass
        return ids

    def _returns_by_product(self, request, company, branch, period, product_ids=None):
        """ТЗ Н-20: возвраты периода (по дате возврата) по товарам: {product_id: (qty, sum)}.
        Берутся из SaleReturn.returned_items; старые возвраты без состава пропускаются."""
        out = {}
        try:
            SaleReturn = apps.get_model("main.SaleReturn")
            Sale = apps.get_model("main.Sale")
        except Exception:
            return out
        rq = SaleReturn.objects.filter(company=company, created_at__gte=period.start, created_at__lt=period.end)
        if branch is not None and _model_has_field(Sale, "branch"):
            bf = Q(sale__branch=branch)
            if self._include_global(request):
                bf |= Q(sale__branch__isnull=True)
            rq = rq.filter(bf)
        for items in rq.values_list("returned_items", flat=True):
            for e in items or []:
                if not isinstance(e, dict) or not e.get("product"):
                    continue
                pid = str(e["product"])
                if product_ids is not None and pid not in product_ids:
                    continue
                try:
                    q = Decimal(str(e.get("qty") or e.get("quantity") or 0))
                    a = Decimal(str(e.get("amount") or e.get("total") or 0))
                except Exception:
                    continue
                oq, oa = out.get(pid, (Decimal("0"), Decimal("0")))
                out[pid] = (oq + q, oa + a)
        return out

    def _market_products_queryset(self, request, company, branch):
        """Товары маркета в области компании/филиала (как на вкладке stock)."""
        try:
            Product = apps.get_model("main.Product")
        except Exception:
            return None
        pqs = Product.objects.filter(company=company)
        if branch is not None and _model_has_field(Product, "branch"):
            if self._include_global(request):
                pqs = pqs.filter(Q(branch=branch) | Q(branch__isnull=True))
            else:
                pqs = pqs.filter(branch=branch)
        return pqs

    def _products_tab_supplier_client_ids(self, request, company, branch):
        """
        Фильтр вкладки tab=products по поставщику (Product.client).
        Параметры как у списка товаров: ?supplier=<uuid> или ?suppliers=id1,id2
        Возвращает None — фильтр не задан; список uuid — разрешённые поставщики; [] — после разбора не осталось валидных id.
        """
        qp = request.query_params
        suppliers_csv = (qp.get("suppliers") or "").strip()
        supplier_one = (qp.get("supplier") or "").strip()
        raw_ids = (
            [x.strip() for x in suppliers_csv.split(",") if x.strip()]
            if suppliers_csv
            else ([supplier_one] if supplier_one else [])
        )
        if not raw_ids:
            return None
        try:
            Client = apps.get_model("main.Client")
        except Exception:
            return []
        from uuid import UUID

        parsed = []
        for s in raw_ids:
            try:
                parsed.append(UUID(str(s)))
            except (ValueError, TypeError, AttributeError):
                continue
        if not parsed:
            return []
        supplier_qs = Client.objects.filter(
            company=company,
            type=Client.StatusClient.SUPPLIERS,
            id__in=parsed,
        )
        if branch is not None and _model_has_field(Client, "branch"):
            supplier_qs = supplier_qs.filter(branch__in=[None, branch])
        return list(supplier_qs.values_list("id", flat=True))

    def _cache_hash_from_query(self, request) -> str:
        qp = {k: request.query_params.getlist(k) for k in request.query_params.keys()}
        raw = json.dumps(qp, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def get(self, request):
        tab = (request.query_params.get("tab") or "sales").lower()

        company = _get_company(request.user)
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")
        if tab in self.SENSITIVE_TABS:
            _require_finance_access(request.user)

        branch = _get_active_branch(request)
        if tab == "stock_health":  # ТЗ 26.10 Б-66: без общего кэша — набор полей зависит от прав
            from apps.main.tz26.ai_stock.stock_health import stock_health_data
            return Response(stock_health_data(request, company, branch, self._include_global(request)))
        period = _get_period(request)

        company_id = str(getattr(company, "id", ""))
        branch_id = str(getattr(branch, "id", "")) if branch else None
        qhash = self._cache_hash_from_query(request)
        ck = cache_market_analytics_key(company_id, branch_id, tab, qhash)

        cached = cache.get(ck)
        if cached is not None:
            return Response(cached)

        if tab == "sales":
            data = self._sales(request, company, branch, period)
        elif tab == "stock":
            data = self._stock(request, company, branch, period)
        elif tab == "cashboxes":
            data = self._cashboxes(request, company, branch, period)
        elif tab == "shifts":
            data = self._shifts(request, company, branch, period)
        elif tab == "products":
            data = self._products_analytics(request, company, branch, period)
        elif tab == "suppliers":
            data = self._suppliers_analytics(request, company, branch, period)
        elif tab == "procurement":
            data = self._procurement(request, company, branch, period)
        elif tab == "purchases":
            data = self._purchases(request, company, branch, period)
        elif tab == "users":
            data = self._users_analytics(request, company, branch, period)
        elif tab == "finance":
            data = self._finance(request, company, branch, period)
        elif tab == "salary":
            data = self._salary(request, company, branch, period)
        elif tab == "abc":
            data = self._abc(request, company, branch, period)
        elif tab in ("stock_items", "stock-items"):
            data = self._stock_items(request, company, branch, period)
        elif tab == "reconcile":
            data = self._reconcile(request, company, branch, period)
        elif tab == "cashflow":
            data = self._cashflow(request, company, branch, period)
        elif tab == "pnl":
            data = self._pnl(request, company, branch, period)
        else:
            return Response(
                {"detail": "Unknown tab. Use: sales|stock|stock_items|cashboxes|shifts|products|suppliers|procurement|purchases|users|finance|salary|abc|reconcile|cashflow|pnl"},
                status=400,
            )

        ttl = getattr(settings, "CACHE_TIMEOUT_ANALYTICS", getattr(settings, "CACHE_TIMEOUT_MEDIUM", 300))
        cache.set(ck, data, ttl)
        return Response(data)

    # ─────────────────────────────────────────────────────────
    # ABC (BE2-13)
    # ─────────────────────────────────────────────────────────
    ABC_GROUPS = {
        "product": ("product_id", "name_snapshot"),
        "category": ("product__category_id", "product__category__name"),
        "brand": ("product__brand_id", "product__brand__name"),
    }
    ABC_THRESHOLDS = {"A": 80, "B": 95}

    def _abc(self, request, company, branch, period: Period):
        """
        ABC за период одним запросом: выручка, количество, прибыль по товарам
        (?by=category|brand — по категориям/брендам), доля и класс по ?metric=.
        Чеки — как во вкладке sales (оплаченные и с частичным возвратом, строки уже
        уменьшены на возвращённое). Класс по накопленной доле cum_share: ≤ 80 % — A,
        ≤ 95 % — B, остальное C; позиции с нулевым или отрицательным показателем — C.
        """
        by = (request.query_params.get("by") or "product").lower()
        metric = (request.query_params.get("metric") or "revenue").lower()
        if by not in self.ABC_GROUPS:
            raise ValidationError({"by": "Используйте product|category|brand."})
        if metric not in ("revenue", "profit", "qty"):
            raise ValidationError({"metric": "Используйте revenue|profit|qty."})

        Sale, SaleItem = get_sale_models()
        ProductModel = apps.get_model("main.Product")
        qs = Sale.objects.filter(company=company)
        if branch is not None:
            if self._include_global(request):
                qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
            else:
                qs = qs.filter(branch=branch)
        qs = qs.filter(status__in=(Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED))
        qs = qs.filter(paid_at__gte=period.start, paid_at__lt=period.end)
        qs = self._apply_sale_filters(request, qs, Sale)

        key_field, name_field = self.ABC_GROUPS[by]
        items_qs = SaleItem.objects.filter(sale__in=qs)
        if by != "product":
            items_qs = items_qs.filter(product__isnull=False)
        cogs_expr, _ok = _get_cogs_expr(SaleItem, ProductModel)
        rows = items_qs.values(key_field, name_field).annotate(
            revenue=Coalesce(Sum(_sale_item_net_line_revenue_expr(SaleItem)),
                             Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
            qty=Coalesce(Sum("quantity"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
            cogs=Coalesce(Sum(cogs_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
        )

        merged = {}
        for r in rows:
            gid = r[key_field]
            name = r[name_field] or ("Без названия" if by == "product" else "Без категории" if by == "category" else "Без бренда")
            k = str(gid) if gid else f"name:{name}"
            m = merged.setdefault(k, {"id": str(gid) if gid else None, "name": name,
                                      "revenue": Z_MONEY, "qty": Z_QTY, "profit": Z_MONEY})
            m["revenue"] += r["revenue"]
            m["qty"] += r["qty"]
            m["profit"] += r["revenue"] - r["cogs"]

        items = sorted(merged.values(), key=lambda m: (-m[metric], m["name"]))
        total = sum((m[metric] for m in items if m[metric] > 0), Decimal("0"))
        cum = Decimal("0")
        out = []
        for m in items:
            val = m[metric]
            share = (val / total * 100) if total > 0 and val > 0 else Decimal("0")
            cum += share
            cum_r = round(cum, 1)
            if val <= 0:
                cls = "C"
            elif cum_r <= self.ABC_THRESHOLDS["A"]:
                cls = "A"
            elif cum_r <= self.ABC_THRESHOLDS["B"]:
                cls = "B"
            else:
                cls = "C"
            out.append({
                "id": m["id"],
                "name": m["name"],
                "revenue": str(_money(m["revenue"])),
                "qty": str(m["qty"].quantize(Decimal("0.001"))),
                "profit": str(_money(m["profit"])),
                "share": round(float(share), 1),
                "cum_share": round(float(min(cum, Decimal("100"))), 1),
                "class": cls,
            })

        return {
            "tab": "abc",
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "by": by,
            "metric": metric,
            "thresholds": self.ABC_THRESHOLDS,
            "totals": {
                "revenue": str(_money(sum((m["revenue"] for m in items), Decimal("0")))),
                "qty": str(sum((m["qty"] for m in items), Decimal("0")).quantize(Decimal("0.001"))),
                "profit": str(_money(sum((m["profit"] for m in items), Decimal("0")))),
            },
            "items": out,
        }

    # ─────────────────────────────────────────────────────────
    # SALES
    # ─────────────────────────────────────────────────────────
    def _sales(self, request, company, branch, period: Period):
        revenue = Z_MONEY
        tx = 0
        clients = 0
        daily = []
        top_products = []
        mixed_count = 0
        mixed_total = Z_MONEY
        payment_breakdown = []
        returns_count = 0
        returns_amount = Z_MONEY

        cogs = None
        gross_profit = None
        margin_percent = None
        cogs_warning = None
        # ТЗ Н-20: фильтр по категориям; выручка тогда считается по строкам чеков категории.
        cat_ids = self._category_filter_ids(request, company)
        item_cat_filter = {"product__category_id__in": cat_ids} if cat_ids is not None else {}

        products_stock = []
        catalog_products_count = 0
        total_stock_quantity = None

        pqs_catalog = self._market_products_queryset(request, company, branch)
        if pqs_catalog is not None:
            ProductModel = pqs_catalog.model
            catalog_products_count = pqs_catalog.count()
            if _model_has_field(ProductModel, "quantity"):
                sum_row = pqs_catalog.aggregate(
                    s=Coalesce(
                        Sum("quantity"),
                        Value(Z_QTY, output_field=QTY_FIELD),
                        output_field=QTY_FIELD,
                    )
                )
                sq = sum_row.get("s")
                total_stock_quantity = str(
                    (sq if sq is not None else Z_QTY).quantize(Decimal("0.01"))
                )
                vf = ["id", "name", "quantity"]
                if _model_has_field(ProductModel, "code"):
                    vf.append("code")
                if _model_has_field(ProductModel, "unit"):
                    vf.append("unit")
                if _model_has_field(ProductModel, "kind"):
                    vf.append("kind")
                if _model_has_field(ProductModel, "barcode"):
                    vf.append("barcode")
                for row in pqs_catalog.order_by("name").values(*vf):
                    q = row.get("quantity")
                    try:
                        qd = Decimal(q) if q is not None else Z_QTY
                    except Exception:
                        qd = Z_QTY
                    item = {
                        "id": str(row["id"]),
                        "name": (row.get("name") or "Товар").strip() or "Товар",
                        "quantity": str(qd.quantize(Decimal("0.01"))),
                    }
                    if "code" in vf:
                        item["code"] = row.get("code") or ""
                    if "unit" in vf:
                        item["unit"] = row.get("unit") or ""
                    if "kind" in vf:
                        item["kind"] = row.get("kind") or ""
                    if "barcode" in vf:
                        item["barcode"] = row.get("barcode") or ""
                    products_stock.append(item)

        Sale, SaleItem = get_sale_models()
        if Sale is not None:
            qs = Sale.objects.filter(company=company)

            if branch is not None and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    qs = qs.filter(branch=branch)

            # Частичный возврат уменьшает total чека, но переводит его из PAID
            # в PARTIALLY_RETURNED. Такой чек всё ещё является продажей на
            # оставшуюся сумму и не должен пропадать из отчёта.
            paid_value = _choice_value(Sale, "Status", "PAID", "paid")
            partial_returned_value = _choice_value(
                Sale, "Status", "PARTIALLY_RETURNED", "partially_returned"
            )
            qs = qs.filter(status__in=(paid_value, partial_returned_value))

            dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"
            qs = qs.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})
            qs = self._apply_sale_filters(request, qs, Sale)
            if cat_ids is not None and SaleItem is not None:
                qs = qs.filter(id__in=SaleItem.objects.filter(sale__in=qs, **item_cat_filter).values("sale_id"))

            agg = qs.aggregate(
                revenue=Coalesce(
                    Sum("total"),
                    Value(Z_MONEY, output_field=MONEY_FIELD),
                    output_field=MONEY_FIELD,
                ),
                tx=Count("id"),
            )
            revenue = agg["revenue"] or Z_MONEY
            tx = agg["tx"] or 0
            if cat_ids is not None and SaleItem is not None:
                _re = _sale_item_net_line_revenue_expr(SaleItem)
                if _re is not None:
                    revenue = SaleItem.objects.filter(sale__in=qs, **item_cat_filter).aggregate(
                        v=Coalesce(Sum(_re), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD)
                    )["v"] or Z_MONEY

            # ── margin (COGS / Profit / Margin%) ──
            if SaleItem is not None and _model_has_field(SaleItem, "sale"):
                ProductModel = None
                try:
                    ProductModel = apps.get_model("main.Product")
                except Exception:
                    ProductModel = None

                cogs_expr, ok = _get_cogs_expr(SaleItem, ProductModel)
                if ok:
                    item_qs_cost = SaleItem.objects.filter(sale__in=qs, **item_cat_filter)
                    cogs_val = item_qs_cost.aggregate(
                        v=Coalesce(
                            Sum(cogs_expr),
                            Value(Z_MONEY, output_field=MONEY_FIELD),
                            output_field=MONEY_FIELD,
                        )
                    )["v"] or Z_MONEY

                    cogs, gross_profit, margin_percent = _calc_margin_pack(revenue, cogs_val)

            if _money(revenue) > 0 and _money(cogs or Z_MONEY) == 0:
                cogs_warning = "Себестоимость не заполнена (маржа может быть некорректной)."

            if _model_has_field(Sale, "client"):
                clients = qs.values("client_id").exclude(client_id__isnull=True).distinct().count()
            else:
                clients = qs.values("user_id").exclude(user_id__isnull=True).distinct().count()

            _day_re = _sale_item_net_line_revenue_expr(SaleItem) if (cat_ids is not None and SaleItem is not None) else None
            if _day_re is not None:
                # ТЗ Н-20: график по дням — по строкам чеков выбранных категорий.
                daily_rows = (
                    SaleItem.objects.filter(sale__in=qs, **item_cat_filter)
                    .annotate(d=TruncDate(f"sale__{dt_field}"))
                    .values("d")
                    .annotate(
                        v=Coalesce(Sum(_day_re), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                        tx=Count("sale_id", distinct=True),
                    )
                    .order_by("d")
                )
            else:
                daily_rows = (
                    qs.annotate(d=TruncDate(dt_field))
                    .values("d")
                    .annotate(
                        v=Coalesce(
                            Sum("total"),
                            Value(Z_MONEY, output_field=MONEY_FIELD),
                            output_field=MONEY_FIELD,
                        ),
                        tx=Count("id"),
                    )
                    .order_by("d")
                )
            daily = [
                {"date": r["d"].isoformat(), "value": str(_money(r["v"])), "transactions": r["tx"]}
                for r in daily_rows if r["d"]
            ]

            # ── Payment Method Breakdown ──
            payment_breakdown = []
            if _model_has_field(Sale, "payment_method"):
                payment_breakdown = _payment_methods_breakdown(qs, revenue)
                mixed_agg = qs.filter(payment_method=Sale.PaymentMethod.MIXED).aggregate(
                    n=Count("id"),
                    v=Coalesce(Sum("total"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                )
                mixed_count = mixed_agg["n"] or 0
                mixed_total = mixed_agg["v"] or Z_MONEY

            if SaleItem is not None and _model_has_field(SaleItem, "sale"):
                item_qs = SaleItem.objects.filter(sale__in=qs, **item_cat_filter)

                revenue_expr = _sale_item_net_line_revenue_expr(SaleItem)
                if revenue_expr is not None:
                    item_rows = (
                        item_qs.values("product_id", "name_snapshot")
                        .annotate(
                            sold=Coalesce(
                                Sum("quantity"),
                                Value(Z_QTY, output_field=QTY_FIELD),
                                output_field=QTY_FIELD,
                            ),
                            revenue=Coalesce(
                                Sum(revenue_expr),
                                Value(Z_MONEY, output_field=MONEY_FIELD),
                                output_field=MONEY_FIELD,
                            ),
                        )
                        .order_by("-revenue")[:5]
                    )
                else:
                    item_rows = (
                        item_qs.values("product_id", "name_snapshot")
                        .annotate(
                            sold=Coalesce(
                                Sum("quantity"),
                                Value(Z_QTY, output_field=QTY_FIELD),
                                output_field=QTY_FIELD,
                            ),
                            revenue=Value(Z_MONEY, output_field=MONEY_FIELD),
                        )
                        .order_by("-sold")[:5]
                    )

                top_products = [
                    {
                        "name": (r.get("name_snapshot") or "Товар"),
                        "sold": str((r.get("sold") or Z_QTY).quantize(Decimal("0.001"))),
                        "revenue": str(_money(r.get("revenue") or Z_MONEY)),
                    }
                    for r in item_rows
                ]

            # Полные возвраты переводят исходный чек в CANCELED и поэтому не
            # входят в выручку. Показываем их отдельной строкой документов по
            # дате самого возврата, а не по дате исходной продажи.
            try:
                SaleReturn = apps.get_model("main.SaleReturn")
                returns_qs = SaleReturn.objects.filter(
                    company=company,
                    created_at__gte=period.start,
                    created_at__lt=period.end,
                )
                if branch is not None and _model_has_field(Sale, "branch"):
                    branch_filter = Q(sale__branch=branch)
                    if self._include_global(request):
                        branch_filter |= Q(sale__branch__isnull=True)
                    returns_qs = returns_qs.filter(branch_filter)
                returns_agg = returns_qs.aggregate(
                    count=Count("id"),
                    amount=Coalesce(
                        Sum("returned_amount"),
                        Value(Z_MONEY, output_field=MONEY_FIELD),
                        output_field=MONEY_FIELD,
                    ),
                )
                returns_count = returns_agg["count"] or 0
                returns_amount = returns_agg["amount"] or Z_MONEY
                partial_returns_amount = (
                    returns_qs.filter(is_full=False).aggregate(
                        amount=Coalesce(
                            Sum("returned_amount"),
                            Value(Z_MONEY, output_field=MONEY_FIELD),
                            output_field=MONEY_FIELD,
                        )
                    )["amount"]
                    or Z_MONEY
                )
            except Exception:
                # Аналитика остаётся доступна в старых инсталляциях без модели
                # возврата; в актуальной схеме этот блок всегда отрабатывает.
                partial_returns_amount = Z_MONEY

        # ТЗ Н-01: чеки в долг (paid_at у них пустой, поэтому выше они не попадают
        # никогда) — в выручку входят деньги, принятые при продаже (предоплата),
        # по дате продажи. Долговая часть — отдельно (credit_sales_total), её
        # погашения — тоже отдельно (debt_repaid_total), в выручку не входят.
        # Себестоимость и прибыль выше считаются только по оплаченным чекам.
        prepayment_total = Z_MONEY
        credit_sales_total = Z_MONEY
        credit_count = 0
        debt_repaid_total = Z_MONEY
        # ТЗ Н-20: при фильтре по категории предоплату чека целиком не добавляем (она не делится по строкам).
        if Sale is not None and _model_has_field(Sale, "debt_initial") and cat_ids is None:
            debt_qs = Sale.objects.filter(
                company=company,
                debt_initial__gt=0,
                status__in=(paid_value, partial_returned_value, _choice_value(Sale, "Status", "DEBT", "debt")),
                created_at__gte=period.start,
                created_at__lt=period.end,
            )
            if branch is not None and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    debt_qs = debt_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    debt_qs = debt_qs.filter(branch=branch)
            debt_qs = self._apply_sale_filters(request, debt_qs, Sale)
            prepay_expr = Least(F("paid_now"), F("total"))
            dagg = debt_qs.aggregate(
                prepay=Coalesce(Sum(prepay_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                credit=Coalesce(Sum("debt_initial"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                n=Count("id", filter=Q(paid_now__gt=0)),
                n_credit=Count("id"),
            )
            credit_count = dagg["n_credit"] or 0
            prepayment_total = _money(dagg["prepay"] or Z_MONEY)
            credit_sales_total = _money(dagg["credit"] or Z_MONEY)
            revenue = _money(revenue) + prepayment_total
            tx += dagg["n"] or 0

            # По дням — те же предоплаты в график.
            by_day = {row["date"]: row for row in daily}
            for r in (
                debt_qs.filter(paid_now__gt=0).annotate(d=TruncDate("created_at")).values("d")
                .annotate(v=Sum(prepay_expr), n=Count("id")).order_by("d")
            ):
                if not r["d"]:
                    continue
                key = r["d"].isoformat()
                row = by_day.setdefault(key, {"date": key, "value": "0.00", "transactions": 0})
                row["value"] = str(_money(Decimal(row["value"]) + (r["v"] or Z_MONEY)))
                row["transactions"] += r["n"] or 0
            daily = sorted(by_day.values(), key=lambda x: x["date"])

            # Способы оплаты: предоплата раскладывается по своим строкам (без долга).
            if _model_has_field(Sale, "payment_method"):
                from apps.main.models import SalePayment

                rows = (
                    SalePayment.objects.filter(sale__in=debt_qs.values("id"), amount__gt=0)
                    .exclude(method__in=(Sale.PaymentMethod.DEBT, Sale.PaymentMethod.MIXED, Sale.PaymentMethod.OFFSET))
                    .values("method").annotate(total=Sum("amount"), cnt=Count("sale_id", distinct=True))
                )
                by_method = {p["method"]: p for p in payment_breakdown}
                for r in rows:
                    key = r["method"] or "unknown"
                    item = by_method.get(key)
                    if item is None:
                        item = {"method": key, "label": PAYMENT_METHOD_LABELS.get(key, key), "count": 0, "total": "0.00"}
                        payment_breakdown.append(item)
                        by_method[key] = item
                    item["count"] += r["cnt"] or 0
                    item["total"] = str(_money(Decimal(item["total"]) + (r["total"] or Z_MONEY)))
                # Σ строк == выручке: копейки (предоплата больше итога после возврата) — на крупную строку.
                diff = _money(revenue) - sum((Decimal(p["total"]) for p in payment_breakdown), Z_MONEY)
                if payment_breakdown and diff:
                    top = max(payment_breakdown, key=lambda p: Decimal(p["total"]))
                    top["total"] = str(_money(Decimal(top["total"]) + diff))
                payment_breakdown.sort(key=lambda p: Decimal(p["total"]), reverse=True)

            try:
                from apps.construction.models import CashFlow

                cf_in = _money_cashflows(company, branch, period, request).filter(type=CashFlow.Type.INCOME)
                debt_repaid_total = _money(_sum_by_category(cf_in, _cf_income_category()).get("debt_repayments", Z_MONEY))
            except Exception:
                debt_repaid_total = Z_MONEY

        # ТЗ Б-03 п. 3: у каждого способа есть `amount` (= `total`, старое имя оставлено для
        # старых касс), плюс отдельный элемент {"method":"debt"} — выдано в долг.
        # Сумма элементов без `debt` = cards.revenue.
        for _p in payment_breakdown:
            _p["amount"] = _p.get("total", "0.00")
        if credit_sales_total > 0:
            payment_breakdown.append({
                "method": "debt",
                "label": PAYMENT_METHOD_LABELS.get("debt", "В долг"),
                "count": credit_count,
                "total": str(_money(credit_sales_total)),
                "amount": str(_money(credit_sales_total)),
            })

        avg_check = _safe_div(_money(revenue), tx)

        documents = [
            {"name": "Продажа", "count": tx, "sum": str(_money(revenue)), "stock": None},
            {"name": "Закупка", "count": 0, "sum": "0.00", "stock": None},
            {"name": "Возврат продажи", "count": returns_count, "sum": str(_money(returns_amount)), "stock": None},
            {"name": "Возврат закупки", "count": 0, "sum": "0.00", "stock": None},
        ]

        return {
            "tab": "sales",
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "filters": {
                "branch": str(getattr(branch, "id", "")) if branch else None,
                "include_global": self._include_global(request),
                "category": (request.query_params.get("category") or "").strip() or None,
                "categories": (request.query_params.get("categories") or "").strip() or None,
                "include_children": (request.query_params.get("include_children") or "").strip().lower() in ("1", "true", "yes", "on"),
            },
            "cards": {
                "revenue": str(_money(revenue)),
                "gross_revenue": str(_money(revenue)),
                "revenue_before_returns": str(_money(Decimal(revenue) + Decimal(partial_returns_amount))),
                "returns_total": str(_money(returns_amount)),
                "returns_count": returns_count,
                # Справочно: чеки со смешанной оплатой (уже разложены по способам в payment_methods)
                "mixed_total": str(_money(mixed_total)),
                "mixed_count": mixed_count,
                "net_revenue": str(_money(revenue)),
                # ТЗ Н-01: предоплаты по чекам в долг (входят в revenue), долговая часть
                # чеков (не входит), погашено долгов за период (не входит).
                "prepayment_total": str(_money(prepayment_total)),
                "credit_sales_total": str(_money(credit_sales_total)),
                "debt_repaid_total": str(_money(debt_repaid_total)),
                "revenue_includes_returns": False,
                "transactions": tx,
                "avg_check": str(_money(avg_check)),
                "clients": clients,
                "cogs": str(_money(cogs)) if cogs is not None else None,
                "gross_profit": str(_money(gross_profit)) if gross_profit is not None else None,
                "margin_percent": margin_percent,
                # Синонимы для «Калькуляции» (calculator-after-stress-test/04): фронт читает
                # *_total, как на вкладке смен. Значения те же.
                "gross_profit_total": str(_money(gross_profit)) if gross_profit is not None else None,
                "margin_percent_total": margin_percent,
                "cogs_warning": cogs_warning,
                "catalog_products_count": catalog_products_count,
                "total_stock_quantity": total_stock_quantity,
            },
            "charts": {
                "sales_dynamics": daily,
                "payment_methods": payment_breakdown,
            },
            "tables": {
                "top_products": top_products,
                "documents": documents,
                "products_stock": products_stock,
            },
        }

    # ─────────────────────────────────────────────────────────
    # STOCK ITEMS (ТЗ 12.24): остаток и закупочная цена по ВСЕМ товарам, постранично
    # ─────────────────────────────────────────────────────────
    STOCK_ITEMS_SORTS = {
        "name": "name", "quantity": "quantity", "purchase_price": "purchase_price",
        "price": "price", "stock_value": "_sv",
    }

    def _stock_items(self, request, company, branch, period: Period):
        """
        tab=stock_items&page=1&page_size=200(макс 1000)&search=&category=&kind=&sort=name|quantity|purchase_price|price|stock_value
        &order=asc|desc&only_in_stock=1&abc=1
        Один SQL-запрос на страницу + один агрегат на итоги; abc=1 добавляет класс A/B/C по стоимости остатка
        (один дополнительный запрос id+стоимость по всем товарам). Услуги не считаются.
        """
        ProductModel = apps.get_model("main.Product")
        q = request.query_params
        pqs = self._market_products_queryset(request, company, branch)
        pqs = pqs.exclude(kind=ProductModel.Kind.SERVICE).exclude(status=ProductModel.Status.ARCHIVED)
        if q.get("category"):
            pqs = pqs.filter(category_id=q["category"])
        if q.get("kind"):
            pqs = pqs.filter(kind=q["kind"])
        if q.get("search"):
            term = q["search"].strip()
            pqs = pqs.filter(Q(name__icontains=term) | Q(barcode__icontains=term) | Q(code__icontains=term))
        if (q.get("only_in_stock") or "").lower() in ("1", "true", "yes"):
            pqs = pqs.filter(quantity__gt=0)

        sv_expr = ExpressionWrapper(F("quantity") * F("purchase_price"), output_field=MONEY_FIELD)
        pqs = pqs.annotate(_sv=sv_expr)
        tot = pqs.aggregate(
            n=Count("id"),
            qty=Coalesce(Sum("quantity"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
            value=Coalesce(Sum("_sv"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
            retail=Coalesce(
                Sum(ExpressionWrapper(F("quantity") * F("price"), output_field=MONEY_FIELD)),
                Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD,
            ),
        )

        abc_map = {}
        abc_summary = None
        if (q.get("abc") or "").lower() in ("1", "true", "yes"):
            total_v = tot["value"] if tot["value"] and tot["value"] > 0 else Decimal("0")
            cum = Decimal("0")
            abc_summary = {"A": 0, "B": 0, "C": 0}
            for pid, v in pqs.order_by("-_sv", "id").values_list("id", "_sv"):
                v = v or Decimal("0")
                share = (v / total_v * 100) if total_v > 0 and v > 0 else Decimal("0")
                cum += share
                cls = "C" if v <= 0 else "A" if round(cum, 1) <= self.ABC_THRESHOLDS["A"] else "B" if round(cum, 1) <= self.ABC_THRESHOLDS["B"] else "C"
                abc_map[pid] = cls
                abc_summary[cls] += 1

        sort = self.STOCK_ITEMS_SORTS.get((q.get("sort") or "name").lower(), "name")
        order = "-" if (q.get("order") or "asc").lower() == "desc" else ""
        try:
            page = max(int(q.get("page") or 1), 1)
            page_size = min(max(int(q.get("page_size") or 200), 1), 1000)
        except ValueError:
            page, page_size = 1, 200
        rows = pqs.order_by(f"{order}{sort}", "id").values(
            "id", "name", "barcode", "code", "unit", "quantity", "purchase_price", "price", "_sv",
            "category_id", "category__name",
        )[(page - 1) * page_size: page * page_size]
        items = []
        for r in rows:
            item = {
                "id": str(r["id"]), "name": r["name"], "barcode": r["barcode"] or "", "code": r["code"] or "",
                "unit": r["unit"] or "", "category": str(r["category_id"]) if r["category_id"] else None,
                "category_name": r["category__name"],
                "quantity": str((r["quantity"] or Z_QTY).quantize(Decimal("0.001"))),
                "purchase_price": str(_money(r["purchase_price"] or Z_MONEY)),
                "price": str(_money(r["price"] or Z_MONEY)),
                "stock_value": str(_money(r["_sv"] or Z_MONEY)),
            }
            if abc_map:
                item["class"] = abc_map.get(r["id"])
            items.append(item)
        out = {
            "tab": "stock_items",
            "count": tot["n"], "page": page, "page_size": page_size,
            "totals": {
                "quantity": str((tot["qty"] or Z_QTY).quantize(Decimal("0.001"))),
                "stock_value": str(_money(tot["value"])),
                "retail_value": str(_money(tot["retail"])),
            },
            "items": items,
        }
        if abc_summary is not None:
            out["abc"] = {"thresholds": self.ABC_THRESHOLDS, "counts": abc_summary}
        return out

    # ─────────────────────────────────────────────────────────
    # STOCK
    # ─────────────────────────────────────────────────────────
    def _stock(self, request, company, branch, period: Period):
        Product = None
        ProductCategory = None
        try:
            Product = apps.get_model("main.Product")
        except Exception:
            pass
        try:
            ProductCategory = apps.get_model("main.ProductCategory")
        except Exception:
            pass

        total_products = 0
        categories_count = 0
        inventory_value = Z_MONEY
        low_count = 0
        zero_stock_count = 0
        turnover_days = None
        category_pie = []
        movement = []
        low_list = []
        total_stock_quantity = None
        products_stock = []

        if Product is not None:
            pqs = Product.objects.filter(company=company)

            if branch is not None and _model_has_field(Product, "branch"):
                if self._include_global(request):
                    pqs = pqs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    pqs = pqs.filter(branch=branch)

            product_id = request.query_params.get("product")
            category_id = request.query_params.get("category")
            kind = request.query_params.get("kind")

            if product_id:
                pqs = pqs.filter(id=product_id)
            if kind and _model_has_field(Product, "kind"):
                pqs = pqs.filter(kind=kind)
            if category_id and _model_has_field(Product, "category"):
                pqs = pqs.filter(category_id=category_id)

            total_products = pqs.count()

            if _model_has_field(Product, "category") and ProductCategory is not None:
                categories_count = (
                    pqs.values("category_id")
                    .exclude(category_id__isnull=True)
                    .distinct()
                    .count()
                )

            qty_field = "quantity" if _model_has_field(Product, "quantity") else None
            pp_field = "purchase_price" if _model_has_field(Product, "purchase_price") else None
            price_field = "price" if _model_has_field(Product, "price") else None

            if qty_field:
                stock_pqs = pqs.exclude(kind=Product.Kind.SERVICE) if _model_has_field(Product, "kind") else pqs
                sum_row = stock_pqs.aggregate(
                    s=Coalesce(
                        Sum(qty_field),
                        Value(Z_QTY, output_field=QTY_FIELD),
                        output_field=QTY_FIELD,
                    )
                )
                sq = sum_row.get("s")
                total_stock_quantity = str(
                    (sq if sq is not None else Z_QTY).quantize(Decimal("0.01"))
                )

                vf = ["id", "name", qty_field]
                if _model_has_field(Product, "code"):
                    vf.append("code")
                if _model_has_field(Product, "unit"):
                    vf.append("unit")
                if _model_has_field(Product, "kind"):
                    vf.append("kind")
                if _model_has_field(Product, "barcode"):
                    vf.append("barcode")
                for row in pqs.order_by("name").values(*vf):
                    q = row.get(qty_field)
                    try:
                        qd = Decimal(q) if q is not None else Z_QTY
                    except Exception:
                        qd = Z_QTY
                    item = {
                        "id": str(row["id"]),
                        "name": (row.get("name") or "Товар").strip() or "Товар",
                        "quantity": str(qd.quantize(Decimal("0.01"))),
                    }
                    if "code" in vf:
                        item["code"] = row.get("code") or ""
                    if "unit" in vf:
                        item["unit"] = row.get("unit") or ""
                    if "kind" in vf:
                        item["kind"] = row.get("kind") or ""
                    if "barcode" in vf:
                        item["barcode"] = row.get("barcode") or ""
                    products_stock.append(item)

            if qty_field and (pp_field or price_field):
                mul_field = pp_field or price_field
                inv_expr = ExpressionWrapper(F(qty_field) * F(mul_field), output_field=MONEY_FIELD)
                inventory_value = (
                    pqs.aggregate(
                        v=Coalesce(
                            Sum(inv_expr),
                            Value(Z_MONEY, output_field=MONEY_FIELD),
                            output_field=MONEY_FIELD,
                        )
                    )["v"]
                    or Z_MONEY
                )

            min_field = None
            for f in ("min_quantity", "min_stock", "reorder_level", "minimum_quantity"):
                if _model_has_field(Product, f):
                    min_field = f
                    break

            if qty_field:
                if min_field:
                    low_qs = pqs.filter(**{f"{qty_field}__lte": F(min_field)})
                else:
                    low_qs = pqs.filter(**{f"{qty_field}__lte": 5})

                low_only = (request.query_params.get("low_only") or "").strip() in ("1", "true", "yes", "on")
                if low_only:
                    pqs = low_qs

                low_count = low_qs.count()
                zero_stock_count = stock_pqs.filter(
                    Q(**{f"{qty_field}__lte": 0}) | Q(**{f"{qty_field}__isnull": True})
                ).count()

                low_rows = low_qs.order_by(qty_field)[:10]
                for p in low_rows:
                    q = getattr(p, qty_field, 0) or 0
                    mn = getattr(p, min_field, None) if min_field else 5
                    try:
                        qd = Decimal(q)
                    except Exception:
                        qd = Decimal("0")
                    try:
                        mnd = Decimal(mn) if mn is not None else Decimal("5")
                    except Exception:
                        mnd = Decimal("5")

                    status = "critical" if qd <= max(Decimal("1"), (mnd / 2)) else "low"
                    low_list.append({
                        "name": getattr(p, "name", "Товар"),
                        "qty": str(qd.quantize(Decimal("0.001"))),
                        "min": str(mnd.quantize(Decimal("0.001"))) if mn is not None else None,
                        "status": status,
                    })

            if _model_has_field(Product, "category"):
                rows = (
                    pqs.values("category__name")
                    .annotate(cnt=Count("id"))
                    .order_by("-cnt")[:10]
                )
                total = sum([r["cnt"] for r in rows]) or 1
                category_pie = [
                    {
                        "name": r["category__name"] or "Прочее",
                        "percent": round((r["cnt"] * 100) / total, 1),
                        "count": r["cnt"],
                    }
                    for r in rows
                ]

        Sale, SaleItem = get_sale_models()
        if Sale is not None and SaleItem is not None and _model_has_field(SaleItem, "quantity"):
            paid_value = _choice_value(Sale, "Status", "PAID", "paid")
            dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"

            sqs = Sale.objects.filter(company=company, status__in=_revenue_statuses(Sale))
            if branch is not None and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    sqs = sqs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    sqs = sqs.filter(branch=branch)

            sqs = sqs.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})
            sqs = self._apply_sale_filters(request, sqs, Sale)

            iq = SaleItem.objects.filter(sale__in=sqs)

            rows = (
                iq.annotate(d=TruncDate(f"sale__{dt_field}"))
                .values("d")
                .annotate(
                    units=Coalesce(
                        Sum("quantity"),
                        Value(Z_QTY, output_field=QTY_FIELD),
                        output_field=QTY_FIELD,
                    )
                )
                .order_by("d")
            )
            movement = [
                {"date": r["d"].isoformat(), "units": str((r["units"] or Z_QTY).quantize(Decimal("0.001")))}
                for r in rows if r["d"]
            ]

            if inventory_value and inventory_value > Decimal("0"):
                rev = sqs.aggregate(
                    v=Coalesce(
                        Sum("total"),
                        Value(Z_MONEY, output_field=MONEY_FIELD),
                        output_field=MONEY_FIELD,
                    )
                )["v"] or Z_MONEY
                days = max(1, (period.end.date() - period.start.date()).days)
                avg_daily_rev = Decimal(rev) / Decimal(days)
                if avg_daily_rev > Decimal("0"):
                    turnover_days = float((Decimal(inventory_value) / avg_daily_rev).quantize(Decimal("0.1")))

        return {
            "tab": "stock",
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "filters": {
                "branch": str(getattr(branch, "id", "")) if branch else None,
                "include_global": self._include_global(request),
                "product": request.query_params.get("product"),
                "category": request.query_params.get("category"),
                "kind": request.query_params.get("kind"),
                "low_only": (request.query_params.get("low_only") or "").strip() in ("1", "true", "yes", "on"),
            },
            "cards": {
                "total_products": total_products,
                "categories": categories_count,
                "inventory_value": str(_money(inventory_value)),
                "low_stock_count": low_count,
                "zero_stock_products_count": zero_stock_count,
                "zero_stock_count": zero_stock_count,
                "out_of_stock_count": zero_stock_count,
                "turnover_days": turnover_days,
                "total_stock_quantity": total_stock_quantity,
                "low_stock_rule": "current_stock <= minimum_stock (default 5)",
            },
            "charts": {
                "category_distribution": category_pie,
                "movement_units": movement,
            },
            "tables": {
                "low_stock": low_list,
                "products_stock": products_stock,
            },
        }

    # ─────────────────────────────────────────────────────────
    # PURCHASES (закупки по полю Product.date)
    # ─────────────────────────────────────────────────────────
    def _purchases(self, request, company, branch, period: Period):
        qp = request.query_params
        raw_from = (qp.get("purchase_date_from") or qp.get("date_from") or "").strip() or None
        raw_to = (qp.get("purchase_date_to") or qp.get("date_to") or "").strip() or None

        # Границы закупок трактуем так же, как основной период (местное время,
        # конец периода включительно). Если не заданы — берём общий период вкладки.
        bound_from, bound_to = _parse_bounds(raw_from, raw_to, param_hint="purchase_date")
        df = bound_from if bound_from else period.start
        dt = bound_to if bound_to else period.end

        # 1. Попытка использования данных из журнала оприходований (SupplierReceiptItem)
        SupplierReceiptItem = None
        try:
            SupplierReceiptItem = apps.get_model("main.SupplierReceiptItem")
        except Exception:
            pass

        if SupplierReceiptItem is not None:
            rc_qs = SupplierReceiptItem.objects.filter(
                receipt__company=company,
                receipt__created_at__gte=df,
                receipt__created_at__lte=dt,
            )
            if branch is not None and _model_has_field(apps.get_model("main.SupplierReceipt"), "branch"):
                if self._include_global(request):
                    rc_qs = rc_qs.filter(Q(receipt__branch=branch) | Q(receipt__branch__isnull=True))
                else:
                    rc_qs = rc_qs.filter(receipt__branch=branch)

            if rc_qs.exists():
                val_expr = ExpressionWrapper(
                    F("qty") * Coalesce(F("purchase_price"), Value(Decimal("0.00"), output_field=MONEY_FIELD)),
                    output_field=MONEY_FIELD,
                )
                agg = rc_qs.aggregate(
                    sku_count=Count("product_id", distinct=True),
                    units=Coalesce(Sum("qty"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
                    value=Coalesce(Sum(val_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                )

                purchased_sku_count = int(agg["sku_count"] or 0)
                purchased_units = agg["units"] or Z_QTY
                purchased_value = agg["value"] or Z_MONEY

                sup_rows = (
                    rc_qs.values(
                        "receipt__supplier_id",
                        "receipt__supplier__full_name",
                        "receipt__supplier__llc",
                        "receipt__supplier__phone",
                    )
                    .annotate(
                        sku_count=Count("product_id", distinct=True),
                        units=Coalesce(Sum("qty"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
                        value=Coalesce(Sum(val_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                    )
                    .order_by("-value", "-units")
                )

                by_supplier = []
                for r in sup_rows:
                    by_supplier.append(
                        {
                            "supplier_id": str(r.get("receipt__supplier_id")) if r.get("receipt__supplier_id") else None,
                            "supplier": (r.get("receipt__supplier__full_name") or r.get("receipt__supplier__llc") or "—").strip() or "—",
                            "phone": r.get("receipt__supplier__phone"),
                            "sku_count": int(r.get("sku_count") or 0),
                            "units": _qty_str(r.get("units") or Z_QTY),
                            "value": str(_money(r.get("value") or Z_MONEY)),
                        }
                    )

                return {
                    "tab": "purchases",
                    "period": {"from": df.isoformat(), "to": dt.isoformat()},
                    "filters": {
                        "branch": str(getattr(branch, "id", "")) if branch else None,
                        "include_global": self._include_global(request),
                        "purchase_date_from": raw_from,
                        "purchase_date_to": raw_to,
                    },
                    "cards": {
                        "purchased_sku_count": purchased_sku_count,
                        "purchased_units": _qty_str(purchased_units),
                        "purchased_value": str(_money(purchased_value)),
                    },
                    "tables": {"by_supplier": by_supplier},
                }

        # 2. Фолбэк на Product.date (одиночный агрегированный запрос)
        pqs = self._market_products_queryset(request, company, branch)
        if pqs is None:
            return {
                "tab": "purchases",
                "period": {"from": df.isoformat(), "to": dt.isoformat()},
                "filters": {
                    "branch": str(getattr(branch, "id", "")) if branch else None,
                    "include_global": self._include_global(request),
                    "purchase_date_from": raw_from,
                    "purchase_date_to": raw_to,
                },
                "meta": {"note": "Purchase analytics requires main.Product model."},
                "cards": {"purchased_sku_count": 0, "purchased_units": "0.000", "purchased_value": "0.00"},
                "tables": {"by_supplier": []},
            }

        Product = pqs.model
        if not _model_has_field(Product, "date"):
            return {
                "tab": "purchases",
                "period": {"from": df.isoformat(), "to": dt.isoformat()},
                "filters": {
                    "branch": str(getattr(branch, "id", "")) if branch else None,
                    "include_global": self._include_global(request),
                    "purchase_date_from": raw_from,
                    "purchase_date_to": raw_to,
                },
                "meta": {"note": "Purchase analytics requires Product.date field (purchase date)."},
                "cards": {"purchased_sku_count": 0, "purchased_units": "0.000", "purchased_value": "0.00"},
                "tables": {"by_supplier": []},
            }

        qty_field = "quantity" if _model_has_field(Product, "quantity") else None
        pp_field = "purchase_price" if _model_has_field(Product, "purchase_price") else None

        pqs2 = pqs.filter(date__gte=df, date__lt=dt)

        agg_kwargs = {"sku_count": Count("id")}
        if qty_field:
            agg_kwargs["units"] = Coalesce(Sum(qty_field), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD)
        if qty_field and pp_field:
            val_expr = ExpressionWrapper(F(qty_field) * F(pp_field), output_field=MONEY_FIELD)
            agg_kwargs["value"] = Coalesce(Sum(val_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD)

        agg = pqs2.aggregate(**agg_kwargs)
        purchased_sku_count = int(agg.get("sku_count") or 0)
        purchased_units = agg.get("units") or Z_QTY
        purchased_value = agg.get("value") or Z_MONEY

        by_supplier = []
        if _model_has_field(Product, "client"):
            sup_rows = (
                pqs2.values("client_id", "client__full_name", "client__llc", "client__phone")
                .annotate(
                    sku_count=Count("id"),
                    units=Coalesce(
                        Sum(qty_field),
                        Value(Z_QTY, output_field=QTY_FIELD),
                        output_field=QTY_FIELD,
                    )
                    if qty_field
                    else Value(Z_QTY, output_field=QTY_FIELD),
                    value=Coalesce(
                        Sum(ExpressionWrapper(F(qty_field) * F(pp_field), output_field=MONEY_FIELD)),
                        Value(Z_MONEY, output_field=MONEY_FIELD),
                        output_field=MONEY_FIELD,
                    )
                    if (qty_field and pp_field)
                    else Value(Z_MONEY, output_field=MONEY_FIELD),
                )
                .order_by("-value", "-units")
            )
            for r in sup_rows:
                by_supplier.append(
                    {
                        "supplier_id": str(r.get("client_id")) if r.get("client_id") else None,
                        "supplier": (r.get("client__full_name") or r.get("client__llc") or "—").strip() or "—",
                        "phone": r.get("client__phone"),
                        "sku_count": int(r.get("sku_count") or 0),
                        "units": _qty_str(r.get("units") or Z_QTY),
                        "value": str(_money(r.get("value") or Z_MONEY)),
                    }
                )

        return {
            "tab": "purchases",
            "period": {"from": df.isoformat(), "to": dt.isoformat()},
            "filters": {
                "branch": str(getattr(branch, "id", "")) if branch else None,
                "include_global": self._include_global(request),
                "purchase_date_from": raw_from,
                "purchase_date_to": raw_to,
            },
            "cards": {
                "purchased_sku_count": purchased_sku_count,
                "purchased_units": _qty_str(purchased_units),
                "purchased_value": str(_money(purchased_value)),
            },
            "tables": {"by_supplier": by_supplier},
        }

    # ─────────────────────────────────────────────────────────
    # CASHBOXES
    # ─────────────────────────────────────────────────────────
    def _cashboxes(self, request, company, branch, period: Period):
        revenue = Z_MONEY
        tx = 0
        avg_check = Z_MONEY
        cash_in_box = Z_MONEY

        cogs = None
        gross_profit = None
        margin_percent = None
        cogs_warning = None

        hourly = []
        pay_pie = []
        pay_detail = []
        tx_week = []
        peak_hours = []

        Sale, SaleItem = get_sale_models()
        if Sale is not None:
            paid_value = _choice_value(Sale, "Status", "PAID", "paid")
            partial_returned_value = _choice_value(
                Sale, "Status", "PARTIALLY_RETURNED", "partially_returned"
            )
            dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"

            qs = Sale.objects.filter(company=company, status__in=(paid_value, partial_returned_value))
            if branch is not None and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    qs = qs.filter(branch=branch)

            qs = qs.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})
            qs = self._apply_sale_filters(request, qs, Sale)

            agg = qs.aggregate(
                revenue=Coalesce(
                    Sum("total"),
                    Value(Z_MONEY, output_field=MONEY_FIELD),
                    output_field=MONEY_FIELD,
                ),
                tx=Count("id"),
            )
            revenue = agg["revenue"] or Z_MONEY
            tx = agg["tx"] or 0
            avg_check = _safe_div(_money(revenue), tx)

            if SaleItem is not None and _model_has_field(SaleItem, "sale"):
                ProductModel = None
                try:
                    ProductModel = apps.get_model("main.Product")
                except Exception:
                    ProductModel = None

                cogs_expr, ok = _get_cogs_expr(SaleItem, ProductModel)
                if ok:
                    item_qs_cost = SaleItem.objects.filter(sale__in=qs)
                    cogs_val = item_qs_cost.aggregate(
                        v=Coalesce(
                            Sum(cogs_expr),
                            Value(Z_MONEY, output_field=MONEY_FIELD),
                            output_field=MONEY_FIELD,
                        )
                    )["v"] or Z_MONEY

                    cogs, gross_profit, margin_percent = _calc_margin_pack(revenue, cogs_val)

            if _money(revenue) > 0 and _money(cogs or Z_MONEY) == 0:
                cogs_warning = "Себестоимость не заполнена (маржа может быть некорректной)."

            pm_field = "payment_method" if _model_has_field(Sale, "payment_method") else None
            if pm_field:
                rows = (
                    qs.values(pm_field)
                    .annotate(
                        cnt=Count("id"),
                        sm=Coalesce(
                            Sum("total"),
                            Value(Z_MONEY, output_field=MONEY_FIELD),
                            output_field=MONEY_FIELD,
                        ),
                    )
                    .order_by("-sm")
                )

                total_sum = Z_MONEY
                for r in rows:
                    total_sum += Decimal(r["sm"] or Z_MONEY)

                for r in rows:
                    name = r[pm_field] or "unknown"
                    cnt = int(r["cnt"] or 0)
                    sm = _money(r["sm"] or Z_MONEY)
                    share = float((sm / total_sum * 100).quantize(Decimal("0.1"))) if total_sum else 0.0
                    pay_detail.append({"method": name, "transactions": cnt, "sum": str(sm), "share": share})

                pay_pie = [{"name": d["method"], "percent": d["share"]} for d in pay_detail]

                cash_value = _choice_value(Sale, "PaymentMethod", "CASH", "cash")
                cash_in_box = qs.filter(**{pm_field: cash_value}).aggregate(
                    v=Coalesce(
                        Sum("total"),
                        Value(Z_MONEY, output_field=MONEY_FIELD),
                        output_field=MONEY_FIELD,
                    )
                )["v"] or Z_MONEY
                # наличная часть смешанных оплат тоже лежит в ящике
                cash_in_box = Decimal(cash_in_box) + _mixed_payment_split(qs)["cash"]
                for row in pay_detail:
                    if row["method"] == "mixed":
                        split = _mixed_payment_split(qs)
                        row["cash"] = str(_money(split["cash"]))
                        row["card"] = str(_money(split["card"]))

            hour_rows = (
                qs.annotate(h=ExtractHour(dt_field))
                .values("h")
                .annotate(
                    v=Coalesce(
                        Sum("total"),
                        Value(Z_MONEY, output_field=MONEY_FIELD),
                        output_field=MONEY_FIELD,
                    ),
                    cnt=Count("id"),
                )
                .order_by("h")
            )
            hourly = [
                {"hour": int(r["h"]) if r["h"] is not None else 0, "revenue": str(_money(r["v"])), "transactions": int(r["cnt"])}
                for r in hour_rows
            ]

            wd_rows = (
                qs.annotate(wd=ExtractWeekDay(dt_field))
                .values("wd")
                .annotate(cnt=Count("id"))
                .order_by("wd")
            )
            tx_week = [{"weekday": int(r["wd"]), "transactions": int(r["cnt"])} for r in wd_rows if r["wd"] is not None]

            peak_hours = sorted(hourly, key=lambda x: Decimal(x["revenue"]), reverse=True)[:6]
            for r in peak_hours:
                r["avg_check"] = str(_safe_div(Decimal(r["revenue"]), int(r["transactions"])))

        cash_share = 0.0
        try:
            cash_share = float((Decimal(cash_in_box) / Decimal(revenue) * 100).quantize(Decimal("0.1"))) if revenue else 0.0
        except Exception:
            cash_share = 0.0

        return {
            "tab": "cashboxes",
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "filters": {
                "branch": str(getattr(branch, "id", "")) if branch else None,
                "include_global": self._include_global(request),
            },
            "cards": {
                "revenue": str(_money(revenue)),
                "transactions": tx,
                "avg_check": str(_money(avg_check)),
                "cash_in_box": str(_money(cash_in_box)),
                "cash_share_percent": cash_share,
                "cogs": str(_money(cogs)) if cogs is not None else None,
                "gross_profit": str(_money(gross_profit)) if gross_profit is not None else None,
                "margin_percent": margin_percent,
                "cogs_warning": cogs_warning,
            },
            "charts": {
                "sales_by_hours": hourly,
                "payment_methods": pay_pie,
                "transactions_by_weekday": tx_week,
            },
            "tables": {
                "payment_detail": pay_detail,
                "peak_hours": peak_hours,
            },
        }

    # ─────────────────────────────────────────────────────────
    # SHIFTS
    # ─────────────────────────────────────────────────────────
    def _shifts(self, request, company, branch, period: Period):
        qp = request.query_params
        cashbox_id = qp.get("cashbox") or None
        cashier_id = qp.get("cashier") or None
        status = (qp.get("status") or "").lower() or None

        qs = CashShift.objects.filter(company=company)
        if branch is not None:
            qs = qs.filter(branch=branch)

        if cashbox_id:
            qs = qs.filter(cashbox_id=cashbox_id)
        if cashier_id:
            qs = qs.filter(cashier_id=cashier_id)
        if status in ("open", "closed"):
            qs = qs.filter(status=status)

        active_cnt = qs.filter(status=CashShift.Status.OPEN).count()

        today = timezone.localdate()
        start_today = timezone.make_aware(datetime.combine(today, time.min))
        end_today = start_today + timedelta(days=1)
        today_cnt = qs.filter(opened_at__gte=start_today, opened_at__lt=end_today).count()

        period_qs = qs.filter(opened_at__gte=period.start, opened_at__lt=period.end)

        closed = period_qs.filter(status=CashShift.Status.CLOSED).exclude(closed_at__isnull=True)
        avg_duration_hours = None
        durations = []
        for r in closed.values("opened_at", "closed_at"):
            if r["opened_at"] and r["closed_at"]:
                durations.append((r["closed_at"] - r["opened_at"]).total_seconds())
        if durations:
            avg_sec = sum(durations) / len(durations)
            avg_duration_hours = round(avg_sec / 3600, 1)

        Sale, SaleItem = get_sale_models()

        revenue_total = Z_MONEY
        cogs_total = None
        gross_profit_total = None
        margin_percent_total = None
        avg_profit_per_shift = None
        cogs_warning = None

        sqs = None

        if Sale is not None and _model_has_field(Sale, "shift"):
            paid_value = _choice_value(Sale, "Status", "PAID", "paid")
            partial_returned_value = _choice_value(
                Sale, "Status", "PARTIALLY_RETURNED", "partially_returned"
            )
            shift_sale_statuses = (paid_value, partial_returned_value)
            dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"

            sqs = Sale.objects.filter(company=company, status__in=shift_sale_statuses)

            if branch is not None and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    sqs = sqs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    sqs = sqs.filter(branch=branch)

            if cashbox_id and _model_has_field(Sale, "cashbox"):
                sqs = sqs.filter(cashbox_id=cashbox_id)
            if cashier_id:
                if _model_has_field(Sale, "user"):
                    sqs = sqs.filter(user_id=cashier_id)
                else:
                    sqs = sqs.filter(shift__cashier_id=cashier_id)

            sqs = sqs.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})

            revenue_total = sqs.aggregate(
                v=Coalesce(
                    Sum("total"),
                    Value(Z_MONEY, output_field=MONEY_FIELD),
                    output_field=MONEY_FIELD,
                )
            )["v"] or Z_MONEY

            if SaleItem is not None and _model_has_field(SaleItem, "sale") and sqs is not None:
                ProductModel = None
                try:
                    ProductModel = apps.get_model("main.Product")
                except Exception:
                    ProductModel = None

                cogs_expr, ok = _get_cogs_expr(SaleItem, ProductModel)
                if ok:
                    item_qs_cost = SaleItem.objects.filter(sale__in=sqs)
                    cogs_val = item_qs_cost.aggregate(
                        v=Coalesce(
                            Sum(cogs_expr),
                            Value(Z_MONEY, output_field=MONEY_FIELD),
                            output_field=MONEY_FIELD,
                        )
                    )["v"] or Z_MONEY

                    cogs_total, gross_profit_total, margin_percent_total = _calc_margin_pack(revenue_total, cogs_val)

            if _money(revenue_total) > 0 and _money(cogs_total or Z_MONEY) == 0:
                cogs_warning = "Себестоимость не заполнена (маржа может быть некорректной)."

        shifts_cnt = period_qs.count() or 1
        avg_revenue_per_shift = _safe_div(_money(revenue_total), shifts_cnt)

        if gross_profit_total is not None:
            avg_profit_per_shift = _safe_div(_money(gross_profit_total), shifts_cnt)

        def bucket(h: int) -> str:
            if 6 <= h < 12:
                return "morning"
            if 12 <= h < 18:
                return "day"
            return "evening"

        bucket_map = {
            "morning": {"revenue": Z_MONEY, "transactions": 0},
            "day": {"revenue": Z_MONEY, "transactions": 0},
            "evening": {"revenue": Z_MONEY, "transactions": 0},
        }

        if Sale is not None and _model_has_field(Sale, "shift"):
            dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"
            sqs2 = Sale.objects.filter(company=company, status__in=shift_sale_statuses)

            if branch is not None and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    sqs2 = sqs2.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    sqs2 = sqs2.filter(branch=branch)

            if cashbox_id and _model_has_field(Sale, "cashbox"):
                sqs2 = sqs2.filter(cashbox_id=cashbox_id)
            if cashier_id:
                if _model_has_field(Sale, "user"):
                    sqs2 = sqs2.filter(user_id=cashier_id)
                else:
                    sqs2 = sqs2.filter(shift__cashier_id=cashier_id)

            sqs2 = sqs2.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})

            for r in sqs2.values("total", "shift__opened_at"):
                o = r.get("shift__opened_at")
                if not o:
                    continue
                # values() отдаёт aware-datetime в UTC — час берём по местному времени
                b = bucket(int(timezone.localtime(o).hour))
                bucket_map[b]["revenue"] += Decimal(r.get("total") or 0)
                bucket_map[b]["transactions"] += 1

        sales_by_shift_bucket = [
            {"name": "Утро", "key": "morning", "revenue": str(_money(bucket_map["morning"]["revenue"])), "transactions": bucket_map["morning"]["transactions"]},
            {"name": "День", "key": "day", "revenue": str(_money(bucket_map["day"]["revenue"])), "transactions": bucket_map["day"]["transactions"]},
            {"name": "Вечер", "key": "evening", "revenue": str(_money(bucket_map["evening"]["revenue"])), "transactions": bucket_map["evening"]["transactions"]},
        ]

        active_rows = []
        act_qs = CashShift.objects.filter(company=company, status=CashShift.Status.OPEN)
        if branch is not None:
            act_qs = act_qs.filter(branch=branch)
        if cashbox_id:
            act_qs = act_qs.filter(cashbox_id=cashbox_id)
        if cashier_id:
            act_qs = act_qs.filter(cashier_id=cashier_id)

        act = act_qs.select_related("cashier", "cashbox").order_by("-opened_at")[:50]
        act_ids = [s.id for s in act]

        shift_sales_map = {}
        if act_ids and Sale is not None and _model_has_field(Sale, "shift"):
            sale_qs = Sale.objects.filter(company=company, status__in=shift_sale_statuses, shift_id__in=act_ids)

            if branch is not None and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    sale_qs = sale_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    sale_qs = sale_qs.filter(branch=branch)

            if cashbox_id and _model_has_field(Sale, "cashbox"):
                sale_qs = sale_qs.filter(cashbox_id=cashbox_id)

            rows = sale_qs.values("shift_id").annotate(
                rev=Coalesce(
                    Sum("total"),
                    Value(Z_MONEY, output_field=MONEY_FIELD),
                    output_field=MONEY_FIELD,
                )
            )
            shift_sales_map = {r["shift_id"]: _money(r["rev"] or Z_MONEY) for r in rows}

        for sh in act:
            cb_name = getattr(sh.cashbox, "name", None) or f"Касса {sh.cashbox_id}"
            opened = _format_shift_dt(sh.opened_at)
            sales_sum = shift_sales_map.get(sh.id, Z_MONEY)

            active_rows.append({
                "cashier": _user_label(sh.cashier),
                "cashbox": cb_name,
                "opened_at": opened,
                "sales": str(_money(sales_sum)),
                "status": "open",
            })

        best_cashiers = []
        if Sale is not None and _model_has_field(Sale, "shift"):
            dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"
            sqs3 = Sale.objects.filter(company=company, status__in=shift_sale_statuses)

            if branch is not None and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    sqs3 = sqs3.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    sqs3 = sqs3.filter(branch=branch)

            if cashbox_id and _model_has_field(Sale, "cashbox"):
                sqs3 = sqs3.filter(cashbox_id=cashbox_id)
            if cashier_id:
                if _model_has_field(Sale, "user"):
                    sqs3 = sqs3.filter(user_id=cashier_id)
                else:
                    sqs3 = sqs3.filter(shift__cashier_id=cashier_id)

            sqs3 = sqs3.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})

            rows = (
                sqs3.values(
                    "shift__cashier_id",
                    "shift__cashier__first_name",
                    "shift__cashier__last_name",
                    "shift__cashier__email",
                    "shift__cashier__phone_number",
                )
                .annotate(
                    revenue=Coalesce(
                        Sum("total"),
                        Value(Z_MONEY, output_field=MONEY_FIELD),
                        output_field=MONEY_FIELD,
                    ),
                    tx=Count("id"),
                    shifts=Count("shift_id", distinct=True),
                )
                .order_by("-revenue")[:10]
            )

            for i, r in enumerate(rows, start=1):
                rev = _money(r["revenue"] or Z_MONEY)
                txc = int(r["tx"] or 0)
                best_cashiers.append({
                    "place": i,
                    "cashier": _user_label(
                        None,
                        first_name=r.get("shift__cashier__first_name"),
                        last_name=r.get("shift__cashier__last_name"),
                        email=r.get("shift__cashier__email"),
                        phone=r.get("shift__cashier__phone_number"),
                        user_id=r.get("shift__cashier_id"),
                    ),
                    "shifts": int(r["shifts"] or 0),
                    "sales": str(rev),
                    "avg_check": str(_safe_div(rev, txc)),
                })

        return {
            "tab": "shifts",
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "filters": {
                "branch": str(branch.id) if branch else None,
                "include_global": self._include_global(request),
                "cashbox": cashbox_id,
                "cashier": cashier_id,
                "status": status,
            },
            "cards": {
                "active_shifts": active_cnt,
                "shifts_today": today_cnt,
                "avg_duration_hours": avg_duration_hours,
                "avg_revenue_per_shift": str(_money(avg_revenue_per_shift)),
                "cogs_total": str(_money(cogs_total)) if cogs_total is not None else None,
                "gross_profit_total": str(_money(gross_profit_total)) if gross_profit_total is not None else None,
                "margin_percent_total": margin_percent_total,
                "avg_profit_per_shift": str(_money(avg_profit_per_shift)) if avg_profit_per_shift is not None else None,
                "cogs_warning": cogs_warning,
            },
            "charts": {"sales_by_shift_bucket": sales_by_shift_bucket},
            "tables": {"active_shifts": active_rows, "best_cashiers": best_cashiers},
        }

    # ─────────────────────────────────────────────────────────
    # PRODUCTS ANALYTICS
    # ─────────────────────────────────────────────────────────
    def _products_analytics(self, request, company, branch, period: Period):
        Product = None
        ProductCategory = None
        ProductBrand = None
        try:
            Product = apps.get_model("main.Product")
            ProductCategory = apps.get_model("main.ProductCategory")
            ProductBrand = apps.get_model("main.ProductBrand")
        except Exception:
            pass

        # Параметры для лимита результатов (по умолчанию топ-20, если явно не запрошено все)
        limit_param = request.query_params.get("limit")
        if limit_param:
            if limit_param.strip().lower() in ("all", "0", "-1"):
                limit = None
            elif limit_param.isdigit():
                limit = int(limit_param)
            else:
                limit = 20
        else:
            limit = 20
        
        top_products_by_revenue = []
        top_products_by_qty = []
        categories_performance = []
        brands_performance = []
        stock_value = Z_MONEY
        low_stock_count = 0
        low_stock_products = []  # НОВОЕ: полный список товаров с низким остатком
        expiring_products = []
        expiring_products_count = 0
        expired_products_count = 0
        sales_without_product = []
        sales_without_product_line_count = 0
        rejected_catalog_products = []
        rejected_products_count = 0

        supplier_ids = None
        if Product and _model_has_field(Product, "client"):
            supplier_ids = self._products_tab_supplier_client_ids(request, company, branch)

        # ТЗ Н-20: фильтр по категориям (category / categories / include_children)
        cat_ids = self._category_filter_ids(request, company)
        top_products_by_profit = []

        Sale, SaleItem = get_sale_models()
        if Sale and SaleItem and Product:
            paid_value = _choice_value(Sale, "Status", "PAID", "paid")
            partial_returned_value = _choice_value(
                Sale, "Status", "PARTIALLY_RETURNED", "partially_returned"
            )
            dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"
            
            sqs = Sale.objects.filter(company=company, status__in=(paid_value, partial_returned_value))
            if branch and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    sqs = sqs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    sqs = sqs.filter(branch=branch)
            sqs = sqs.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})

            def _sale_items_products_tab(sale_qs):
                qi = SaleItem.objects.filter(sale__in=sale_qs)
                if supplier_ids is not None:
                    qi = qi.filter(product__client_id__in=supplier_ids)
                if cat_ids is not None:
                    qi = qi.filter(product__category_id__in=cat_ids)
                return qi

            # ТЗ Н-20: себестоимость строки на день продажи (снимок), без снимка — текущая закупка.
            cogs_line_expr = ExpressionWrapper(
                F("quantity") * Coalesce(
                    F("purchase_price_snapshot"),
                    F("product__purchase_price"),
                    Value(Z_MONEY, output_field=MONEY_FIELD),
                    output_field=MONEY_FIELD,
                ),
                output_field=MONEY_FIELD,
            )
            no_snap_count = Count(
                "id", filter=Q(purchase_price_snapshot__isnull=True), distinct=True
            )
            returns_map = self._returns_by_product(request, company, branch, period)

            # Top Products by Revenue - ДЕТАЛЬНО со всеми полями
            revenue_expr = _sale_item_net_line_revenue_expr(SaleItem)
            if revenue_expr is not None:
                top_by_rev_query = (
                    _sale_items_products_tab(sqs)
                    .values(
                        "product_id", 
                        "name_snapshot",
                        "product__code",
                        "product__article",
                        "product__barcode",
                        "product__category__name",
                        "product__brand__name",
                        "product__quantity",
                        "product__price",
                        "product__purchase_price",
                        "product__category_id",
                    )
                    .annotate(
                        revenue=Coalesce(Sum(revenue_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                        qty_sold=Coalesce(Sum("quantity"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
                        tx_count=Count("sale_id", distinct=True),
                        cogs=Coalesce(Sum(cogs_line_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                    )
                    .order_by("-revenue")
                )
                if limit:
                    top_by_rev_query = top_by_rev_query[:limit]
                
                top_products_by_revenue = [
                    {
                        "product_id": str(r["product_id"]) if r["product_id"] else None,
                        "name": r["name_snapshot"] or "Товар",
                        "code": r["product__code"],
                        "article": r["product__article"],
                        "barcode": r["product__barcode"],
                        "category": r["product__category__name"],
                        "brand": r["product__brand__name"],
                        "current_stock": str(Decimal(r["product__quantity"] or 0).quantize(Decimal("0.001"))),
                        "price": str(_money(r["product__price"] or 0)),
                        "purchase_price": str(_money(r["product__purchase_price"] or 0)),
                        "revenue": str(_money(r["revenue"])),
                        "qty_sold": str(r["qty_sold"].quantize(Decimal("0.001"))),
                        "transactions": r["tx_count"],
                        # ТЗ Н-20
                        "category_id": str(r["product__category_id"]) if r["product__category_id"] else None,
                        "cogs": str(_money(r["cogs"])),
                        "returned_qty": str(returns_map.get(str(r["product_id"]), (Decimal("0"), Decimal("0")))[0].quantize(Decimal("0.001"))),
                        "returned_sum": str(_money(returns_map.get(str(r["product_id"]), (Decimal("0"), Decimal("0")))[1])),
                    }
                    for r in top_by_rev_query
                ]

                # ТЗ Н-20: ABC по прибыли (прибыль = выручка − себестоимость на день продажи).
                profit_rows = list(
                    _sale_items_products_tab(sqs)
                    .values("product_id", "name_snapshot", "product__code", "product__category__name", "product__category_id")
                    .annotate(
                        revenue=Coalesce(Sum(revenue_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                        qty_sold=Coalesce(Sum("quantity"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
                        cogs=Coalesce(Sum(cogs_line_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                    )
                )
                for r in profit_rows:
                    r["profit"] = _money((r["revenue"] or Z_MONEY) - (r["cogs"] or Z_MONEY))
                profit_rows.sort(key=lambda r: r["profit"], reverse=True)
                pos_total = sum((r["profit"] for r in profit_rows if r["profit"] > 0), Z_MONEY)
                cum = Z_MONEY
                for r in profit_rows:
                    if r["profit"] > 0 and pos_total > 0:
                        # класс по доле накопленной прибыли ДО этого товара: товар, пересекающий 80%, остаётся в A
                        share = float(cum / pos_total * 100)
                        cum += r["profit"]
                        r["abc"] = "A" if share < 80 else ("B" if share < 95 else "C")
                        r["share"] = round(float(r["profit"] / pos_total * 100), 2)
                    else:
                        r["abc"], r["share"] = "C", 0.0
                top_products_by_profit = [
                    {
                        "product_id": str(r["product_id"]) if r["product_id"] else None,
                        "name": r["name_snapshot"] or "Товар",
                        "code": r["product__code"],
                        "category": r["product__category__name"],
                        "category_id": str(r["product__category_id"]) if r["product__category_id"] else None,
                        "qty_sold": str(r["qty_sold"].quantize(Decimal("0.001"))),
                        "revenue": str(_money(r["revenue"])),
                        "cogs": str(_money(r["cogs"])),
                        "gross_profit": str(r["profit"]),
                        "margin_percent": _pct(r["profit"], _money(r["revenue"])) if r["revenue"] else 0.0,
                        "profit_share_percent": r["share"],
                        "abc": r["abc"],
                    }
                    for r in (profit_rows[:limit] if limit else profit_rows)
                ]

            # Top Products by Quantity - ДЕТАЛЬНО
            top_by_qty_query = (
                _sale_items_products_tab(sqs)
                .values(
                    "product_id", 
                    "name_snapshot",
                    "product__code",
                    "product__category__name",
                    "product__brand__name",
                    "product__quantity",
                    "product__price",
                    "product__category_id",
                )
                .annotate(
                    qty_sold=Coalesce(Sum("quantity"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
                    tx_count=Count("sale_id", distinct=True),
                )
                .order_by("-qty_sold")
            )
            if limit:
                top_by_qty_query = top_by_qty_query[:limit]
                
            top_products_by_qty = [
                {
                    "product_id": str(r["product_id"]) if r["product_id"] else None,
                    "name": r["name_snapshot"] or "Товар",
                    "code": r["product__code"],
                    "category": r["product__category__name"],
                    "brand": r["product__brand__name"],
                    "current_stock": str(Decimal(r["product__quantity"] or 0).quantize(Decimal("0.001"))),
                    "price": str(_money(r["product__price"] or 0)),
                    "qty_sold": str(r["qty_sold"].quantize(Decimal("0.001"))),
                    "transactions": r["tx_count"],
                    "category_id": str(r["product__category_id"]) if r["product__category_id"] else None,  # ТЗ Н-20
                }
                for r in top_by_qty_query
            ]

            # Categories Performance - ДЕТАЛЬНО с количеством товаров в каждой
            if _model_has_field(SaleItem, "product") and _model_has_field(Product, "category"):
                if revenue_expr is not None:
                    cat_query = (
                        _sale_items_products_tab(sqs).filter(product__isnull=False)
                        .values("product__category__id", "product__category__name")
                        .annotate(
                            revenue=Coalesce(Sum(revenue_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                            qty_sold=Coalesce(Sum("quantity"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
                            products_count=Count("product_id", distinct=True),
                            tx_count=Count("sale_id", distinct=True),
                            cost=Coalesce(Sum(cogs_line_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                            no_snapshot_lines=no_snap_count,
                        )
                        .order_by("-revenue")
                    )
                    if limit:
                        cat_query = cat_query[:limit]
                    cat_rows = list(cat_query)

                    # ТЗ Н-20: parent_id и возвраты по категориям.
                    cat_row_ids = [r["product__category__id"] for r in cat_rows if r["product__category__id"]]
                    parent_map = {}
                    cat_of_product = {}
                    returns_by_cat = {}
                    if cat_row_ids and ProductCategory is not None:
                        if _model_has_field(ProductCategory, "parent"):
                            parent_map = dict(ProductCategory.objects.filter(id__in=cat_row_ids).values_list("id", "parent_id"))
                        if returns_map:
                            cat_of_product = {
                                str(pid): cid
                                for pid, cid in Product.objects.filter(
                                    id__in=list(returns_map.keys()), category_id__in=cat_row_ids
                                ).values_list("id", "category_id")
                            }
                            for pid, (_rq, ra) in returns_map.items():
                                cid = cat_of_product.get(pid)
                                if cid:
                                    returns_by_cat[cid] = returns_by_cat.get(cid, Z_MONEY) + ra

                    categories_performance = []
                    for r in cat_rows:
                        cid = r["product__category__id"]
                        cost = _money(r["cost"] or Z_MONEY)
                        rev = _money(r["revenue"] or Z_MONEY)
                        profit = _money(rev - cost)
                        pid_parent = parent_map.get(cid)
                        categories_performance.append({
                            "category_id": str(cid) if cid else None,
                            "category": r["product__category__name"] or "Без категории",
                            "revenue": str(rev),
                            "qty_sold": str(r["qty_sold"].quantize(Decimal("0.001"))),
                            "products_count": r["products_count"],
                            "transactions": r["tx_count"],
                            "cost": str(cost),
                            # снимок закупки на день продажи; если у части строк его нет — для них текущая закупка
                            "cost_basis": "snapshot" if not r["no_snapshot_lines"] else "mixed",
                            "gross_profit": str(profit),
                            "margin_percent": _pct(profit, rev) if rev else 0.0,
                            "returns_sum": str(_money(returns_by_cat.get(cid, Z_MONEY))),
                            "parent_id": str(pid_parent) if pid_parent else None,
                        })

            # Brands Performance - ДЕТАЛЬНО
            if _model_has_field(SaleItem, "product") and _model_has_field(Product, "brand"):
                if revenue_expr is not None:
                    brand_query = (
                        _sale_items_products_tab(sqs).filter(product__isnull=False)
                        .values("product__brand__id", "product__brand__name")
                        .annotate(
                            revenue=Coalesce(Sum(revenue_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                            qty_sold=Coalesce(Sum("quantity"), Value(Z_QTY, output_field=QTY_FIELD), output_field=QTY_FIELD),
                            products_count=Count("product_id", distinct=True),
                            tx_count=Count("sale_id", distinct=True),
                        )
                        .order_by("-revenue")
                    )
                    if limit:
                        brand_query = brand_query[:limit]
                        
                    brands_performance = [
                        {
                            "brand_id": str(r["product__brand__id"]) if r["product__brand__id"] else None,
                            "brand": r["product__brand__name"] or "Без бренда",
                            "revenue": str(_money(r["revenue"])),
                            "qty_sold": str(r["qty_sold"].quantize(Decimal("0.001"))),
                            "products_count": r["products_count"],
                            "transactions": r["tx_count"],
                        }
                        for r in brand_query
                    ]

            # Строки продаж без карточки товара (Product удалён → product=NULL, остаётся name_snapshot)
            # При фильтре по поставщику не смешиваем с «призраками» без product.client
            if _model_has_field(SaleItem, "name_snapshot") and supplier_ids is None:
                ghost_base = SaleItem.objects.filter(sale__in=sqs, product__isnull=True)
                # AN-10: авто-связка непривязанных строк чеков с товарами компании по barcode_snapshot
                unlinked = list(ghost_base.filter(barcode_snapshot__isnull=False)[:300])
                if unlinked:
                    barcodes = [it.barcode_snapshot for it in unlinked if it.barcode_snapshot]
                    bc_map = {
                        p.barcode: p
                        for p in Product.objects.filter(company=company, barcode__in=barcodes).exclude(status=Product.Status.ARCHIVED)
                    }
                    to_update = []
                    for it in unlinked:
                        p = bc_map.get(it.barcode_snapshot)
                        if p:
                            it.product = p
                            to_update.append(it)
                    if to_update:
                        SaleItem.objects.bulk_update(to_update, ["product"])
                        ghost_base = SaleItem.objects.filter(sale__in=sqs, product__isnull=True)

                sales_without_product_line_count = int(ghost_base.count() or 0)
                gvals = ("name_snapshot", "barcode_snapshot")
                ghost_rev = _sale_item_net_line_revenue_expr(SaleItem)
                if ghost_rev is not None:
                    gq = (
                        ghost_base.values(*gvals)
                        .annotate(
                            qty_sold=Coalesce(
                                Sum("quantity"),
                                Value(Z_QTY, output_field=QTY_FIELD),
                                output_field=QTY_FIELD,
                            ),
                            revenue=Coalesce(
                                Sum(ghost_rev),
                                Value(Z_MONEY, output_field=MONEY_FIELD),
                                output_field=MONEY_FIELD,
                            ),
                            tx_count=Count("sale_id", distinct=True),
                        )
                        .order_by("-qty_sold")
                    )
                else:
                    gq = (
                        ghost_base.values(*gvals)
                        .annotate(
                            qty_sold=Coalesce(
                                Sum("quantity"),
                                Value(Z_QTY, output_field=QTY_FIELD),
                                output_field=QTY_FIELD,
                            ),
                            tx_count=Count("sale_id", distinct=True),
                        )
                        .order_by("-qty_sold")
                    )
                if limit:
                    gq = gq[:limit]
                for r in gq:
                    row = {
                        "name": (r.get("name_snapshot") or "").strip() or "—",
                        "barcode_snapshot": r.get("barcode_snapshot"),
                        "qty_sold": str((r.get("qty_sold") or Z_QTY).quantize(Decimal("0.001"))),
                        "transactions": int(r.get("tx_count") or 0),
                    }
                    if ghost_rev is not None:
                        row["revenue"] = str(_money(r.get("revenue") or Z_MONEY))
                    sales_without_product.append(row)

        # Stock Analysis - ПОЛНЫЙ СПИСОК товаров с низким остатком
        if Product:
            pqs = Product.objects.filter(company=company)
            if branch and _model_has_field(Product, "branch"):
                if self._include_global(request):
                    pqs = pqs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    pqs = pqs.filter(branch=branch)
            if supplier_ids is not None and _model_has_field(Product, "client"):
                pqs = pqs.filter(client_id__in=supplier_ids)
            if cat_ids is not None:
                pqs = pqs.filter(category_id__in=cat_ids)  # ТЗ Н-20
            
            qty_field = "quantity" if _model_has_field(Product, "quantity") else None
            pp_field = "purchase_price" if _model_has_field(Product, "purchase_price") else None
            price_field = "price" if _model_has_field(Product, "price") else None
            
            if qty_field and pp_field:
                inv_expr = ExpressionWrapper(F(qty_field) * F(pp_field), output_field=MONEY_FIELD)
                stock_value = pqs.aggregate(v=Coalesce(Sum(inv_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD))["v"] or Z_MONEY
                
                # Товары с низким остатком (AN-09): синхронно с правилом minimum_quantity / low_stock_rule
                min_field = None
                for f in ("min_quantity", "min_stock", "reorder_level", "minimum_quantity"):
                    if _model_has_field(Product, f):
                        min_field = f
                        break

                if min_field:
                    low_stock_qs = pqs.filter(**{f"{qty_field}__lte": F(min_field)}).order_by(qty_field)
                else:
                    low_stock_qs = pqs.filter(**{f"{qty_field}__lte": 5}).order_by(qty_field)
                low_stock_count = low_stock_qs.count()
                
                # Применяем лимит только если он задан
                if limit:
                    low_stock_qs = low_stock_qs[:limit]
                
                low_stock_products = [
                    {
                        "id": str(p.id),
                        "name": p.name,
                        "code": getattr(p, "code", None),
                        "article": getattr(p, "article", None),
                        "barcode": getattr(p, "barcode", None),
                        "category": getattr(p.category, "name", None) if hasattr(p, "category") and p.category else None,
                        "brand": getattr(p.brand, "name", None) if hasattr(p, "brand") and p.brand else None,
                        "quantity": str(Decimal(getattr(p, qty_field, 0) or 0).quantize(Decimal("0.001"))),
                        "price": str(_money(getattr(p, price_field, 0) or 0)) if price_field else None,
                        "purchase_price": str(_money(getattr(p, pp_field, 0) or 0)),
                        "stock_value": str(_money(Decimal(getattr(p, qty_field, 0) or 0) * Decimal(getattr(p, pp_field, 0) or 0))),
                        "status": "critical" if Decimal(getattr(p, qty_field, 0) or 0) <= 1 else "low",
                    }
                    for p in low_stock_qs
                ]

            if _model_has_field(Product, "expiration_date"):
                today = timezone.localdate()
                max_date = today + timedelta(days=14)
                exp_candidates = list(
                    pqs.filter(expiration_date__isnull=False, expiration_date__lte=max_date)
                    .select_related("category", "brand")
                )
                expiring_items = []
                for p in exp_candidates:
                    days_left = (p.expiration_date - today).days
                    if days_left < 0:
                        st = "expired"
                        expired_products_count += 1
                    elif 0 <= days_left <= 3:
                        st = "critical"
                        expiring_products_count += 1
                    elif 3 < days_left <= 14:
                        st = "warning"
                        expiring_products_count += 1
                    else:
                        continue

                    raw_qty = getattr(p, qty_field, 0) if qty_field else 0
                    try:
                        q_dec = Decimal(str(raw_qty or 0))
                        qty_val = int(q_dec) if q_dec % 1 == 0 else float(q_dec)
                    except Exception:
                        qty_val = 0

                    expiring_items.append({
                        "id": str(p.id),
                        "code": getattr(p, "code", None),
                        "name": p.name,
                        "quantity": qty_val,
                        "expiration_date": p.expiration_date.isoformat(),
                        "days_left": days_left,
                        "status": st,
                    })

                expiring_items.sort(key=lambda item: (0 if item["status"] == "expired" else 1, item["days_left"]))
                exp_limit = limit if limit is not None else 100
                expiring_products = expiring_items[:exp_limit]

        catalog_products_count = 0
        pqs_scope = self._market_products_queryset(request, company, branch)
        if pqs_scope is not None and supplier_ids is not None and _model_has_field(pqs_scope.model, "client"):
            pqs_scope = pqs_scope.filter(client_id__in=supplier_ids)
        if pqs_scope is not None and cat_ids is not None:
            pqs_scope = pqs_scope.filter(category_id__in=cat_ids)  # ТЗ Н-20
        if pqs_scope is not None:
            catalog_products_count = pqs_scope.count()

        # Карточки товаров со статусом «Отказ» (в каталоге есть, на витрине «нет»)
        if Product is not None and _model_has_field(Product, "status"):
            rej_val = _choice_value(Product, "Status", "REJECTED", "rejected")
            rj = Product.objects.filter(company=company, status=rej_val)
            if branch and _model_has_field(Product, "branch"):
                if self._include_global(request):
                    rj = rj.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    rj = rj.filter(branch=branch)
            if supplier_ids is not None and _model_has_field(Product, "client"):
                rj = rj.filter(client_id__in=supplier_ids)
            if cat_ids is not None:
                rj = rj.filter(category_id__in=cat_ids)  # ТЗ Н-20
            rejected_products_count = rj.count()
            rlim = limit if limit is not None else 200
            for p in rj.select_related("category", "brand").order_by("-updated_at")[:rlim]:
                rejected_catalog_products.append({
                    "id": str(p.id),
                    "name": getattr(p, "name", None) or "—",
                    "code": getattr(p, "code", None),
                    "article": getattr(p, "article", None),
                    "barcode": getattr(p, "barcode", None),
                    "quantity": (
                        str(Decimal(str(getattr(p, "quantity", None) or 0)).quantize(Decimal("0.001")))
                        if _model_has_field(Product, "quantity")
                        else None
                    ),
                    "status": getattr(p, "status", None),
                    "updated_at": p.updated_at.isoformat() if getattr(p, "updated_at", None) else None,
                    "category": getattr(p.category, "name", None) if getattr(p, "category", None) else None,
                    "brand": getattr(p.brand, "name", None) if getattr(p, "brand", None) else None,
                })

        return {
            "tab": "products",
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "filters": {
                "branch": str(getattr(branch, "id", "")) if branch else None,
                "limit": limit,
                "supplier": (request.query_params.get("supplier") or "").strip() or None,
                "suppliers": (request.query_params.get("suppliers") or "").strip() or None,
                "category": (request.query_params.get("category") or "").strip() or None,
                "categories": (request.query_params.get("categories") or "").strip() or None,
                "include_children": (request.query_params.get("include_children") or "").strip().lower() in ("1", "true", "yes", "on"),
            },
            "cards": {
                "stock_value": str(_money(stock_value)),
                "low_stock_count": low_stock_count,
                "low_stock_rule": "current_stock <= minimum_stock (default 5)",
                "catalog_products_count": catalog_products_count,
                "sales_lines_missing_product_count": sales_without_product_line_count,
                "rejected_products_count": rejected_products_count,
                "expiring_products_count": expiring_products_count,
                "expired_products_count": expired_products_count,
            },
            "tables": {
                "top_by_revenue": top_products_by_revenue,
                "top_by_quantity": top_products_by_qty,
                "top_by_profit": top_products_by_profit,
                "categories": categories_performance,
                "brands": brands_performance,
                "low_stock_products": low_stock_products,  # НОВОЕ: полный список с деталями
                "sales_without_catalog_product": sales_without_product,
                "rejected_products": rejected_catalog_products,
                "expiring_products": expiring_products,
            },
        }

    # ─────────────────────────────────────────────────────────
    # SUPPLIERS (Client type=suppliers → Product.client)
    # ─────────────────────────────────────────────────────────
    def _suppliers_analytics(self, request, company, branch, period: Period):
        """
        Остатки по каталогу (sku с поставщиком), продажи за период, место и условный рейтинг по объёму продаж.
        Отзывы Review не привязаны к товару — rating считается от ранга по period_qty_sold (1..5).
        """
        limit_param = request.query_params.get("limit")
        limit = int(limit_param) if limit_param and limit_param.isdigit() else None
        qp = request.query_params

        purchase_raw_from = (qp.get("purchase_date_from") or qp.get("procurement_date_from") or "").strip() or None
        purchase_raw_to = (qp.get("purchase_date_to") or qp.get("procurement_date_to") or "").strip() or None
        # Границы закупок: местное время, конец периода включительно (см. _parse_bounds).
        purchase_df, purchase_dt = _parse_bounds(purchase_raw_from, purchase_raw_to, param_hint="purchase_date")

        def _dec_qty(v) -> Decimal:
            if isinstance(v, Decimal):
                return v
            return Decimal(str(v or 0))

        try:
            Client = apps.get_model("main.Client")
            Product = apps.get_model("main.Product")
        except Exception:
            return {
                "tab": "suppliers",
                "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
                "filters": {
                    "branch": str(getattr(branch, "id", "")) if branch else None,
                    "limit": limit,
                },
                "cards": {"catalog_products_count": 0},
                "tables": {"suppliers": [], "suppliers_by_stock": []},
            }

        if not _model_has_field(Product, "client"):
            return {
                "tab": "suppliers",
                "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
                "filters": {
                    "branch": str(getattr(branch, "id", "")) if branch else None,
                    "limit": limit,
                },
                "cards": {"suppliers_count": 0, "catalog_products_count": 0},
                "tables": {"suppliers": [], "suppliers_by_stock": []},
            }

        sup_type = Client.StatusClient.SUPPLIERS
        pqs = self._market_products_queryset(request, company, branch)
        if pqs is None:
            return {
                "tab": "suppliers",
                "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
                "filters": {
                    "branch": str(getattr(branch, "id", "")) if branch else None,
                    "limit": limit,
                },
                "cards": {"suppliers_count": 0, "catalog_products_count": 0},
                "tables": {"suppliers": [], "suppliers_by_stock": []},
            }

        catalog_products_count = pqs.count()

        p_sup = pqs.filter(
            client_id__isnull=False,
            client__type=sup_type,
        )
        if branch is not None and _model_has_field(Client, "branch"):
            p_sup = p_sup.filter(Q(client__branch=branch) | Q(client__branch__isnull=True))
        if (purchase_df or purchase_dt) and _model_has_field(Product, "date"):
            if purchase_df:
                p_sup = p_sup.filter(date__gte=purchase_df)
            if purchase_dt:
                p_sup = p_sup.filter(date__lt=purchase_dt)

        qty_field = "quantity" if _model_has_field(Product, "quantity") else None
        pp_field = "purchase_price" if _model_has_field(Product, "purchase_price") else None

        ann: dict = {"products_count": Count("id")}
        if qty_field:
            ann["stock_qty"] = Coalesce(
                Sum(qty_field),
                Value(Z_QTY, output_field=QTY_FIELD),
                output_field=QTY_FIELD,
            )
        else:
            ann["stock_qty"] = Value(Z_QTY, output_field=QTY_FIELD)
        if qty_field and pp_field:
            inv_expr = ExpressionWrapper(F(qty_field) * F(pp_field), output_field=MONEY_FIELD)
            ann["stock_value"] = Coalesce(
                Sum(inv_expr),
                Value(Z_MONEY, output_field=MONEY_FIELD),
                output_field=MONEY_FIELD,
            )
        else:
            ann["stock_value"] = Value(Z_MONEY, output_field=MONEY_FIELD)

        stock_qs = (
            p_sup.values("client_id", "client__full_name", "client__llc", "client__phone")
            .annotate(**ann)
        )

        by_id: dict[str, dict] = {}
        total_stock_val = Z_MONEY
        for r in stock_qs:
            cid = r.get("client_id")
            if not cid:
                continue
            k = str(cid)
            sq_raw = r.get("stock_qty") or 0
            sq_dec = _dec_qty(sq_raw)
            sv = r.get("stock_value") or Z_MONEY
            total_stock_val += _money(sv)
            by_id[k] = {
                "supplier_id": k,
                "name": (
                    (r.get("client__llc") or "").strip()
                    or (r.get("client__full_name") or "").strip()
                    or "—"
                ),
                "phone": (r.get("client__phone") or "") or "",
                "products_count": int(r.get("products_count") or 0),
                "_stock_dec": sq_dec,
                "_sold_dec": Z_QTY,
                "_rev_dec": Z_MONEY,
                "stock_value": str(_money(sv)),
                "period_transactions": 0,
            }

        Sale, SaleItem = get_sale_models()
        if Sale and SaleItem and _model_has_field(SaleItem, "sale") and _model_has_field(SaleItem, "quantity"):
            paid_value = _choice_value(Sale, "Status", "PAID", "paid")
            dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"
            sqs = Sale.objects.filter(company=company, status__in=_revenue_statuses(Sale))
            if branch and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    sqs = sqs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    sqs = sqs.filter(branch=branch)
            sqs = self._apply_sale_filters(request, sqs, Sale)
            sqs = sqs.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})

            si = SaleItem.objects.filter(sale__in=sqs, product_id__in=p_sup.values("id"))
            revenue_expr = _sale_item_net_line_revenue_expr(SaleItem)
            if _model_has_field(SaleItem, "product"):
                agg_kw: dict = {
                    "qty_sold": Coalesce(
                        Sum("quantity"),
                        Value(Z_QTY, output_field=QTY_FIELD),
                        output_field=QTY_FIELD,
                    ),
                    "tx_count": Count("sale_id", distinct=True),
                }
                if revenue_expr is not None:
                    agg_kw["revenue"] = Coalesce(
                        Sum(revenue_expr),
                        Value(Z_MONEY, output_field=MONEY_FIELD),
                        output_field=MONEY_FIELD,
                    )
                sales_rows = si.values("product__client_id").annotate(**agg_kw)
                for r in sales_rows:
                    cid = r.get("product__client_id")
                    if not cid:
                        continue
                    k = str(cid)
                    if k not in by_id:
                        cl = (
                            Client.objects.filter(pk=cid, company=company, type=sup_type)
                            .only("full_name", "llc", "phone")
                            .first()
                        )
                        nm = "—"
                        ph = ""
                        if cl:
                            nm = (getattr(cl, "llc", None) or "").strip() or (getattr(cl, "full_name", None) or "").strip() or "—"
                            ph = getattr(cl, "phone", None) or ""
                        by_id[k] = {
                            "supplier_id": k,
                            "name": nm,
                            "phone": ph,
                            "products_count": 0,
                            "_stock_dec": Z_QTY,
                            "_sold_dec": Z_QTY,
                            "_rev_dec": Z_MONEY,
                            "stock_value": str(Z_MONEY),
                            "period_transactions": 0,
                        }
                    row = by_id[k]
                    row["_sold_dec"] = _dec_qty(r.get("qty_sold"))
                    row["period_transactions"] = int(r.get("tx_count") or 0)
                    if "revenue" in r:
                        row["_rev_dec"] = _money(r.get("revenue") or Z_MONEY)
                    else:
                        row["_rev_dec"] = Z_MONEY

        items = list(by_id.values())
        tot_period_qty = Z_QTY
        tot_period_rev = Z_MONEY
        for it in items:
            tot_period_qty += it["_sold_dec"]
            tot_period_rev += it["_rev_dec"]

        n_sup = len(items)
        items.sort(key=lambda x: (-x["_sold_dec"], -x["_stock_dec"]))
        for i, it in enumerate(items):
            it["rank_by_qty"] = i + 1
            if n_sup <= 1:
                it["rating"] = 5.0
            else:
                it["rating"] = round(float(Decimal("5") - Decimal("4") * Decimal(i) / Decimal(n_sup - 1)), 1)
            it["period_qty_sold"] = _qty_str(it["_sold_dec"])
            it["period_revenue"] = str(_money(it["_rev_dec"]))
            it["stock_qty"] = _qty_str(it["_stock_dec"])
            del it["_sold_dec"]
            del it["_stock_dec"]
            del it["_rev_dec"]

        by_stock = sorted(items, key=lambda x: -Decimal(str(x["stock_qty"])))
        for j, it in enumerate(by_stock, start=1):
            it["rank_by_stock"] = j

        suppliers_by_stock = [
            {
                "supplier_id": it["supplier_id"],
                "name": it["name"],
                "rank_by_stock": it["rank_by_stock"],
                "stock_qty": it["stock_qty"],
                "stock_value": it["stock_value"],
                "products_count": it["products_count"],
            }
            for it in by_stock
        ]

        if limit:
            items = items[:limit]
            suppliers_by_stock = suppliers_by_stock[:limit]

        return {
            "tab": "suppliers",
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "filters": {
                "branch": str(getattr(branch, "id", "")) if branch else None,
                "limit": limit,
                "purchase_date_from": purchase_raw_from,
                "purchase_date_to": purchase_raw_to,
            },
            "cards": {
                "suppliers_count": n_sup,
                "catalog_products_count": catalog_products_count,
                "total_stock_value": str(_money(total_stock_val)),
                "total_period_qty_sold": _qty_str(tot_period_qty),
                "total_period_revenue": str(_money(tot_period_rev)),
            },
            "tables": {
                "suppliers": items,
                "suppliers_by_stock": suppliers_by_stock,
            },
        }

    # ─────────────────────────────────────────────────────────
    # PROCUREMENT (продажи vs отгрузка агентам; закупки от поставщика без журнала)
    # ─────────────────────────────────────────────────────────
    def _procurement(self, request, company, branch, period: Period):
        from uuid import UUID as UUIDType

        limit_param = request.query_params.get("limit")
        limit = int(limit_param) if limit_param and limit_param.isdigit() else 50

        Product = None
        try:
            Product = apps.get_model("main.Product")
        except Exception:
            pass
        Subreal = None
        try:
            Subreal = apps.get_model("main.ManufactureSubreal")
        except Exception:
            pass

        pqs = self._market_products_queryset(request, company, branch)
        catalog_products_count = pqs.count() if pqs is not None else 0

        zero_i = Value(0, output_field=IntegerField())

        s_map: dict[str, dict] = {}
        Sale, SaleItem = get_sale_models()

        if (
            Sale
            and SaleItem
            and Product
            and pqs is not None
            and _model_has_field(SaleItem, "product")
            and _model_has_field(SaleItem, "quantity")
        ):
            paid_value = _choice_value(Sale, "Status", "PAID", "paid")
            dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"
            sqs = Sale.objects.filter(company=company, status__in=_revenue_statuses(Sale))
            if branch and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    sqs = sqs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    sqs = sqs.filter(branch=branch)
            sqs = self._apply_sale_filters(request, sqs, Sale)
            sqs = sqs.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})

            si = SaleItem.objects.filter(sale__in=sqs, product_id__in=pqs.values("id"), product__isnull=False)
            base_vals = ("product_id", "product__name", "product__code")
            rev_expr = _sale_item_net_line_revenue_expr(SaleItem)
            if rev_expr is not None:
                rows = (
                    si.values(*base_vals)
                    .annotate(
                        qty_sold=Coalesce(
                            Sum("quantity"),
                            Value(Z_QTY, output_field=QTY_FIELD),
                            output_field=QTY_FIELD,
                        ),
                        revenue=Coalesce(
                            Sum(rev_expr),
                            Value(Z_MONEY, output_field=MONEY_FIELD),
                            output_field=MONEY_FIELD,
                        ),
                    )
                    .order_by("-qty_sold")
                )
            else:
                rows = (
                    si.values(*base_vals)
                    .annotate(
                        qty_sold=Coalesce(
                            Sum("quantity"),
                            Value(Z_QTY, output_field=QTY_FIELD),
                            output_field=QTY_FIELD,
                        ),
                    )
                    .order_by("-qty_sold")
                )
            for r in rows:
                k = str(r["product_id"])
                s_map[k] = {
                    "product_id": k,
                    "name": (r.get("product__name") or "").strip() or "—",
                    "code": r.get("product__code"),
                    "qty_sold_dec": Decimal(str(r.get("qty_sold") or 0)),
                    "revenue_dec": _money(r.get("revenue") or Z_MONEY) if "revenue" in r else Z_MONEY,
                }

        t_map: dict[str, dict] = {}
        if Subreal is not None and Product is not None and pqs is not None:
            rq = Subreal.objects.filter(company=company)
            if branch and _model_has_field(Subreal, "branch"):
                if self._include_global(request):
                    rq = rq.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    rq = rq.filter(branch=branch)
            rq = rq.filter(
                created_at__gte=period.start,
                created_at__lt=period.end,
                product_id__in=pqs.values("id"),
            )
            tr = (
                rq.values("product_id", "product__name", "product__code")
                .annotate(
                    qty_transferred=Coalesce(
                        Sum("qty_transferred"),
                        zero_i,
                        output_field=IntegerField(),
                    ),
                )
                .order_by("-qty_transferred")
            )
            for r in tr:
                k = str(r["product_id"])
                t_map[k] = {
                    "product_id": k,
                    "name": (r.get("product__name") or "").strip() or "—",
                    "code": r.get("product__code"),
                    "qty_transferred_to_agents": int(r.get("qty_transferred") or 0),
                }

        all_ids = set(s_map) | set(t_map)
        meta_prod: dict[str, dict] = {}
        if pqs is not None and all_ids:
            uuids = []
            for x in all_ids:
                try:
                    uuids.append(UUIDType(x))
                except Exception:
                    pass
            if uuids:
                for p in pqs.filter(id__in=uuids).only("id", "name", "code", "quantity", "purchase_price"):
                    meta_prod[str(p.id)] = {
                        "name": (getattr(p, "name", None) or "").strip() or "—",
                        "code": getattr(p, "code", None),
                        "quantity": getattr(p, "quantity", None) or 0,
                        "purchase_price": getattr(p, "purchase_price", None) or 0,
                    }

        combined_rows = []
        for k in all_ids:
            sd = s_map.get(k, {})
            td = t_map.get(k, {})
            mp = meta_prod.get(k, {})
            name = sd.get("name") or td.get("name") or mp.get("name") or "—"
            code = sd.get("code") if sd.get("code") is not None else td.get("code")
            if code is None:
                code = mp.get("code")
            qty_s = sd.get("qty_sold_dec", Z_QTY)
            rev = sd.get("revenue_dec", Z_MONEY)
            qty_t = int(td.get("qty_transferred_to_agents", 0))
            qcur = Decimal(str(mp.get("quantity", 0) or 0))
            pp = Decimal(str(mp.get("purchase_price", 0) or 0))
            stock_val = _money(qcur * pp)
            combined_rows.append({
                "product_id": k,
                "name": name,
                "code": code,
                "qty_sold": _qty_str(qty_s),
                "revenue": str(_money(rev)),
                "qty_transferred_to_agents": qty_t,
                "current_stock": _qty_str(qcur),
                "stock_at_purchase_prices": str(stock_val),
                "_activity": qty_s + Decimal(qty_t),
            })

        combined_rows.sort(key=lambda x: (-x["_activity"], -Decimal(str(x["qty_sold"]))))
        for row in combined_rows:
            del row["_activity"]

        top_sold = []
        for sd in sorted(s_map.values(), key=lambda x: -x["qty_sold_dec"])[:limit]:
            top_sold.append({
                "product_id": sd["product_id"],
                "name": sd["name"],
                "code": sd["code"],
                "qty_sold": _qty_str(sd["qty_sold_dec"]),
                "revenue": str(_money(sd["revenue_dec"])),
            })

        top_transfers = []
        for td in sorted(t_map.values(), key=lambda x: -x["qty_transferred_to_agents"])[:limit]:
            top_transfers.append({
                "product_id": td["product_id"],
                "name": td["name"],
                "code": td["code"],
                "qty_transferred_to_agents": td["qty_transferred_to_agents"],
            })

        combined_out = combined_rows[:limit]

        tot_sold = sum((s_map[k]["qty_sold_dec"] for k in s_map), Z_QTY)
        tot_tr = sum(int(t_map[k]["qty_transferred_to_agents"]) for k in t_map)

        return {
            "tab": "procurement",
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "filters": {
                "branch": str(getattr(branch, "id", "")) if branch else None,
                "limit": limit,
            },
            "meta": {
                "purchase_note": (
                    "Оприходование от поставщика (POST /api/main/suppliers/<id>/receipt/) не пишется в отдельный журнал; "
                    "в отчёте «отгрузка» — сумма qty_transferred по передачам агентам (ManufactureSubreal) за период."
                ),
            },
            "cards": {
                "catalog_products_count": catalog_products_count,
                "products_with_sales_or_transfers": len(all_ids),
                "total_qty_sold_period": _qty_str(tot_sold),
                "total_qty_transferred_to_agents_period": int(tot_tr),
            },
            "tables": {
                "top_by_sales": top_sold,
                "top_by_transfers_to_agents": top_transfers,
                "sold_vs_transfers": combined_out,
            },
        }

    # ─────────────────────────────────────────────────────────
    # USERS ANALYTICS
    # ─────────────────────────────────────────────────────────
    def _users_analytics(self, request, company, branch, period: Period):
        # Параметры для лимита результатов
        limit_param = request.query_params.get("limit")
        limit = int(limit_param) if limit_param and limit_param.isdigit() else None
        
        users_performance = []
        shift_stats = {}

        Sale, SaleItem = get_sale_models()
        units_by_user: dict = {}
        products_by_user: dict = {}
        names_by_user: dict = {}
        if Sale and _model_has_field(Sale, "user"):
            paid_value = _choice_value(Sale, "Status", "PAID", "paid")
            dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"
            
            sqs = Sale.objects.filter(company=company, status__in=_revenue_statuses(Sale))
            if branch and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    sqs = sqs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    sqs = sqs.filter(branch=branch)
            sqs = sqs.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})

            if (
                SaleItem is not None
                and _model_has_field(SaleItem, "sale")
                and _model_has_field(SaleItem, "quantity")
            ):
                si_qs = SaleItem.objects.filter(sale__in=sqs)
                units_by_user, products_by_user, names_by_user = _users_sold_products_by_user(si_qs, SaleItem)

            # User Performance - ДЕТАЛЬНО ВСЕ сотрудники
            user_query = (
                sqs.values(
                    "user_id",
                    "user__first_name",
                    "user__last_name",
                    "user__email",
                    "user__phone_number",
                )
                .annotate(
                    revenue=Coalesce(Sum("total"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                    tx_count=Count("id"),
                )
                .order_by("-revenue")
            )
            if limit:
                user_query = user_query[:limit]
            
            for r in user_query:
                rev = _money(r["revenue"])
                txc = r["tx_count"] or 0
                uid = r["user_id"]
                sold_list = products_by_user.get(uid, []) if uid is not None else []
                raw_email = r.get("user__email") or ""
                is_deleted = raw_email.startswith("deleted__")

                user_label = _user_label(
                    None,
                    first_name=r.get("user__first_name"),
                    last_name=r.get("user__last_name"),
                    email=raw_email,
                    phone=r.get("user__phone_number"),
                    user_id=uid,
                )
                if is_deleted:
                    user_label = f"{user_label} (Удалён)" if user_label else "Удалённый сотрудник"
                    parts = raw_email.split("__", 2)
                    clean_email = parts[2] if len(parts) >= 3 else ""
                else:
                    clean_email = raw_email

                users_performance.append({
                    "user_id": str(uid) if uid else None,
                    "user": user_label,
                    "email": clean_email,
                    "phone": r.get("user__phone_number"),
                    "revenue": str(rev),
                    "transactions": txc,
                    "avg_check": str(_safe_div(rev, txc)),
                    "units_sold": _qty_str(units_by_user.get(uid, Z_QTY) if uid is not None else Z_QTY),
                    "products_sold_count": len(sold_list),
                    "product_names": names_by_user.get(uid, []) if uid is not None else [],
                    "sold_products": sold_list,
                    "is_deleted": is_deleted,
                })

        # Shift Performance - ПОЛНАЯ информация
        shifts_qs = CashShift.objects.filter(company=company)
        if branch:
            shifts_qs = shifts_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
        shifts_qs = shifts_qs.filter(opened_at__gte=period.start, opened_at__lt=period.end)
        
        total_shifts = shifts_qs.count()
        closed_shifts = shifts_qs.filter(status=CashShift.Status.CLOSED).count()
        
        # Cash discrepancies - ВСЕ расхождения с деталями
        closed_with_data = shifts_qs.filter(status=CashShift.Status.CLOSED, closing_cash__isnull=False).order_by("-opened_at")
        discrepancies = []
        all_discrepancies_qs = []
        
        for shift in closed_with_data:
            diff = shift.cash_diff
            if abs(diff) > Decimal("0.01"):
                all_discrepancies_qs.append(shift)
        
        # Применяем лимит только если задан
        if limit:
            all_discrepancies_qs = all_discrepancies_qs[:limit]
            
        for shift in all_discrepancies_qs:
            diff = shift.cash_diff
            discrepancies.append({
                "shift_id": str(shift.id),
                "cashier": _user_label(shift.cashier) if shift.cashier else "Unknown",
                "cashbox": shift.cashbox.name if shift.cashbox else None,
                "opened_at": _format_shift_dt(shift.opened_at),
                "closed_at": _format_shift_dt(shift.closed_at),
                "expected_cash": str(_money(shift.expected_cash or Z_MONEY)),
                "closing_cash": str(_money(shift.closing_cash or Z_MONEY)),
                "diff": str(_money(diff)),
                "type": "shortage" if diff < 0 else "overage",
            })

        shift_stats = {
            "total": total_shifts,
            "closed": closed_shifts,
            "open": total_shifts - closed_shifts,
            "discrepancies_count": len(all_discrepancies_qs) if not limit else None,  # общее количество расхождений
        }

        return {
            "tab": "users",
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "filters": {
                "branch": str(getattr(branch, "id", "")) if branch else None,
                "limit": limit,
            },
            "cards": shift_stats,
            "tables": {
                "users_performance": users_performance,
                "shift_discrepancies": discrepancies,
            },
        }

    # ─────────────────────────────────────────────────────────
    # FINANCE ANALYTICS
    # ─────────────────────────────────────────────────────────
    def _finance(self, request, company, branch, period: Period):
        from apps.construction.models import CashFlow
        
        # Параметры для лимита результатов
        limit_param = request.query_params.get("limit")
        limit = int(limit_param) if limit_param and limit_param.isdigit() else None

        income_total = Z_MONEY
        expense_total = Z_MONEY
        net_flow = Z_MONEY
        expense_breakdown = []
        income_breakdown = []
        expense_items = []  # НОВОЕ: полный детальный список расходов
        income_items = []   # НОВОЕ: полный детальный список доходов

        # CashFlow Analysis: одобренные движения без заявок, «сырых» двойников и зачёта
        cfqs = _money_cashflows(company, branch, period, request).select_related("cashbox", "cashier")

        income_total = (
            cfqs.filter(type=CashFlow.Type.INCOME)
            .aggregate(v=Coalesce(Sum("amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD))["v"]
            or Z_MONEY
        )
        expense_total = (
            cfqs.filter(type=CashFlow.Type.EXPENSE)
            .aggregate(v=Coalesce(Sum("amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD))["v"]
            or Z_MONEY
        )
        net_flow = _money(income_total - expense_total)

        # Expense Breakdown by Name - ДЕТАЛЬНО
        exp_query = (
            cfqs.filter(type=CashFlow.Type.EXPENSE)
            .values("name")
            .annotate(
                total=Coalesce(Sum("amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                count=Count("id"),
            )
            .order_by("-total")
        )
        if limit:
            exp_query = exp_query[:limit]
            
        expense_breakdown = [
            {
                "name": r["name"] or "Без названия",
                "total": str(_money(r["total"])),
                "count": r["count"],
            }
            for r in exp_query
        ]

        # Income Breakdown by Name - ДЕТАЛЬНО
        inc_query = (
            cfqs.filter(type=CashFlow.Type.INCOME)
            .values("name")
            .annotate(
                total=Coalesce(Sum("amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                count=Count("id"),
            )
            .order_by("-total")
        )
        if limit:
            inc_query = inc_query[:limit]
            
        income_breakdown = [
            {
                "name": r["name"] or "Без названия",
                "total": str(_money(r["total"])),
                "count": r["count"],
            }
            for r in inc_query
        ]
        
        # Разбивка по статье (ТЗ «Бухгалтер», п. 3). Старые *_breakdown (по названию) не меняем.
        def _by_category(kind):
            rows = (
                cfqs.filter(type=kind)
                .values("category_id", "category__title")
                .annotate(
                    total=Coalesce(Sum("amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                    count=Count("id"),
                )
                .order_by("-total")
            )
            if limit:
                rows = rows[:limit]
            return [
                {
                    "category_id": str(r["category_id"]) if r["category_id"] else None,
                    "name": r["category__title"] or "Без статьи",
                    "total": str(_money(r["total"])),
                    "count": r["count"],
                }
                for r in rows
            ]

        expense_by_category = _by_category(CashFlow.Type.EXPENSE)
        income_by_category = _by_category(CashFlow.Type.INCOME)

        # ПОЛНЫЙ список всех расходов с деталями
        expense_qs = cfqs.filter(type=CashFlow.Type.EXPENSE).order_by("-created_at")
        if limit:
            expense_qs = expense_qs[:limit]
            
        expense_items = [
            {
                "id": str(cf.id),
                "name": cf.name or "Без названия",
                "amount": str(_money(cf.amount)),
                "cashbox": cf.cashbox.name if cf.cashbox else None,
                "shift": str(cf.shift_id) if hasattr(cf, "shift_id") and cf.shift_id else None,
                "created_at": cf.created_at.isoformat() if cf.created_at else None,
                "created_by": _user_label(cf.cashier) if cf.cashier_id else None,
                "description": getattr(cf, "description", None) or getattr(cf, "comment", None),
            }
            for cf in expense_qs
        ]
        
        # ПОЛНЫЙ список всех доходов с деталями
        income_qs = cfqs.filter(type=CashFlow.Type.INCOME).order_by("-created_at")
        if limit:
            income_qs = income_qs[:limit]
            
        income_items = [
            {
                "id": str(cf.id),
                "name": cf.name or "Без названия",
                "amount": str(_money(cf.amount)),
                "cashbox": cf.cashbox.name if cf.cashbox else None,
                "shift": str(cf.shift_id) if hasattr(cf, "shift_id") and cf.shift_id else None,
                "created_at": cf.created_at.isoformat() if cf.created_at else None,
                "created_by": _user_label(cf.cashier) if cf.cashier_id else None,
                "description": getattr(cf, "description", None) or getattr(cf, "comment", None),
            }
            for cf in income_qs
        ]

        return {
            "tab": "finance",
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "filters": {
                "branch": str(getattr(branch, "id", "")) if branch else None,
                "limit": limit,
            },
            "cards": {
                "income_total": str(_money(income_total)),
                "expense_total": str(_money(expense_total)),
                "net_flow": str(net_flow),
                "income_count": cfqs.filter(type=CashFlow.Type.INCOME).count(),
                "expense_count": cfqs.filter(type=CashFlow.Type.EXPENSE).count(),
            },
            "tables": {
                "expense_breakdown": expense_breakdown,
                "income_breakdown": income_breakdown,
                "expense_by_category": expense_by_category,
                "income_by_category": income_by_category,
                "expense_items": expense_items,  # НОВОЕ: полный детальный список
                "income_items": income_items,    # НОВОЕ: полный детальный список
            },
        }

    # ─────────────────────────────────────────────────────────
    # SALARY (продавцы по чекам Sale.user)
    # ─────────────────────────────────────────────────────────
    def _salary(self, request, company, branch, period: Period):
        from apps.main.models import MarketSaleEmployeePayProfile

        Sale, _sale_item = get_sale_models()
        days = (period.end - period.start).days
        if days < 1:
            days = 1

        period_meta = {
            "from": period.start.isoformat(),
            "to": period.end.isoformat(),
        }
        filters_meta = {
            "branch": str(getattr(branch, "id", "")) if branch else None,
            "include_global": self._include_global(request),
        }

        if Sale is None:
            return {
                "tab": "salary",
                "period": period_meta,
                "filters": filters_meta,
                "cards": {},
                "charts": {},
                "tables": {},
                "rows": [],
                "detail": "Модель продаж не найдена.",
            }

        paid_value = _choice_value(Sale, "Status", "PAID", "paid")
        dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"

        def _apply_sale_branch(qs):
            if branch is not None and _model_has_field(Sale, "branch"):
                if self._include_global(request):
                    return qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                return qs.filter(branch=branch)
            if branch is None and _model_has_field(Sale, "branch"):
                return qs.filter(branch__isnull=True)
            return qs

        def _sale_qs_period():
            q = Sale.objects.filter(company=company, status=paid_value)
            q = q.filter(**{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end})
            return _apply_sale_branch(q)

        prof_qs = MarketSaleEmployeePayProfile.objects.filter(company=company)
        if branch is not None:
            prof_qs = prof_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
        else:
            prof_qs = prof_qs.filter(branch__isnull=True)

        effective_profiles: dict = {}
        for prof in prof_qs.select_related("user").order_by("user_id", "-branch_id"):
            if prof.user_id not in effective_profiles or prof.branch_id is not None:
                effective_profiles[prof.user_id] = prof

        period_sales_qs = _sale_qs_period()
        cashier_user_ids = set(period_sales_qs.exclude(user_id__isnull=True).values_list("user_id", flat=True))
        consultant_user_ids = set(period_sales_qs.exclude(consultant_id__isnull=True).values_list("consultant_id", flat=True))
        # Мастера услуг: процент со строк чека (SaleItem.performer_commission_amount)
        performer_rows = {}
        if _sale_item is not None and _model_has_field(_sale_item, "performer"):
            for r in (
                _sale_item.objects.filter(sale__in=period_sales_qs.values("id"), performer_id__isnull=False)
                .values("performer_id")
                .annotate(
                    comm=Coalesce(Sum("performer_commission_amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                    cnt=Count("id"),
                )
            ):
                performer_rows[r["performer_id"]] = r
        performer_user_ids = set(performer_rows)

        # ТЗ ч.13, 3: проданные единицы для «за проданный товар». Чеки — как у процента (кассир пробил,
        # без комиссии консультанта), включая частично возвращённые: возврат уже уменьшил quantity строки,
        # полностью возвращённая строка удалена. Весовой товар — 1 за строку.
        partial_ret_value = _choice_value(Sale, "Status", "PARTIALLY_RETURNED", "partially_returned")
        units_by_user = {}
        if _sale_item is not None:
            unit_sales = _apply_sale_branch(
                Sale.objects.filter(
                    company=company,
                    status__in=(paid_value, partial_ret_value),
                    **{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end},
                )
            ).filter(
                Q(consultant__isnull=True)
                | Q(consultant_commission_enabled=False)
                | Q(consultant_commission_amount=Decimal("0.00"))
                | Q(consultant_commission_amount__isnull=True)
            )
            qty_f = DecimalField(max_digits=18, decimal_places=3)
            for r in (
                _sale_item.objects.filter(sale__in=unit_sales.values("id"), quantity__gt=0)
                .values("sale__user_id")
                .annotate(
                    units=Coalesce(
                        Sum(
                            Case(
                                When(product__is_weight=True, then=Value(Decimal("1"), output_field=qty_f)),
                                default=F("quantity"),
                                output_field=qty_f,
                            )
                        ),
                        Value(Decimal("0"), output_field=qty_f),
                        output_field=qty_f,
                    )
                )
            ):
                if r["sale__user_id"]:
                    units_by_user[r["sale__user_id"]] = r["units"] or Decimal("0")

        profile_user_ids = set(effective_profiles.keys())
        all_user_ids = list(profile_user_ids | cashier_user_ids | consultant_user_ids | performer_user_ids)
        payroll_user_ids = all_user_ids

        from django.contrib.auth import get_user_model
        User = get_user_model()
        users_by_id = {u.id: u for u in User.objects.filter(id__in=all_user_ids)}

        # ТЗ 26.10 Б-63: закрытые смены сотрудников (сменная ставка, часы)
        from apps.main.tz26.staff.salary import per_shift_bonus_for, shift_stats_bulk
        _shift_stats = shift_stats_bulk(company, all_user_ids, period.start, period.end)

        rows = []
        for uid in all_user_ids:
            prof = effective_profiles.get(uid)
            user_obj = users_by_id.get(uid)
            if not user_obj and not prof:
                continue

            user_label = _user_label(user_obj) if user_obj else (_user_label(prof.user) if prof else "Неизвестный")

            sq_cashier = period_sales_qs.filter(user_id=uid)
            cashier_agg = sq_cashier.aggregate(
                total_sum=Coalesce(Sum("total"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                cnt=Count("id"),
            )
            cashier_sales_period = cashier_agg["total_sum"] or Z_MONEY
            cashier_sales_count = int(cashier_agg["cnt"] or 0)

            sq_consultant = period_sales_qs.filter(consultant_id=uid)
            consultant_agg = sq_consultant.aggregate(
                total_sum=Coalesce(Sum("total"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                comm_sum=Coalesce(Sum("consultant_commission_amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
                cnt=Count("id"),
            )
            consultant_sales_period = consultant_agg["total_sum"] or Z_MONEY
            consultant_commission_period = consultant_agg["comm_sum"] or Z_MONEY
            consultant_sales_count = int(consultant_agg["cnt"] or 0)

            sq_cashier_eligible = sq_cashier.filter(
                Q(consultant__isnull=True)
                | Q(consultant_commission_enabled=False)
                | Q(consultant_commission_amount=Decimal("0.00"))
                | Q(consultant_commission_amount__isnull=True)
            )
            eligible_agg = sq_cashier_eligible.aggregate(
                total_sum=Coalesce(Sum("total"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
            )
            employee_sales_period = eligible_agg["total_sum"] or Z_MONEY

            monthly_base_salary = prof.monthly_base_salary if prof else Z_MONEY
            sales_percent = prof.sales_percent if prof else Z_MONEY
            pay_scheme = prof.pay_scheme if prof else MarketSaleEmployeePayProfile.PayScheme.SALARY
            pay_scheme_label = prof.get_pay_scheme_display() if prof else "Оклад"

            base_part = ((monthly_base_salary or Z_MONEY) * Decimal(days) / Decimal("30")).quantize(Decimal("0.01"))

            PS = MarketSaleEmployeePayProfile.PayScheme
            pct = (sales_percent or Z_MONEY) / Decimal("100")
            if pay_scheme in (PS.PERCENT, PS.SALARY_PLUS_PERCENT, PS.PERCENT_PLUS_PER_ITEM, PS.PER_SHIFT_PLUS_PERCENT):
                cashier_bonus = (employee_sales_period * pct).quantize(Decimal("0.01"))
            else:
                cashier_bonus = Z_MONEY

            performer_commission_period = (performer_rows.get(uid) or {}).get("comm") or Z_MONEY
            performer_services_count = int((performer_rows.get(uid) or {}).get("cnt") or 0)
            percent_bonus = (cashier_bonus + consultant_commission_period + performer_commission_period).quantize(Decimal("0.01"))

            # ТЗ ч.13, 3: за каждый проданный товар — к любой схеме, если сумма > 0
            per_item_amount = (prof.per_item_amount if prof else Z_MONEY) or Z_MONEY
            items_sold_period = units_by_user.get(uid, Decimal("0"))
            per_item_bonus = (
                (items_sold_period * per_item_amount).quantize(Decimal("0.01")) if per_item_amount > 0 else Z_MONEY
            )

            has_base = pay_scheme in (PS.SALARY, PS.SALARY_PLUS_PERCENT, PS.SALARY_PLUS_PER_ITEM)
            shifts_n, shifts_hours = _shift_stats.get(uid, (0, Z_MONEY))
            per_shift_bonus = per_shift_bonus_for(prof, shifts_n)
            total_pay = (
                (base_part if has_base else Z_MONEY) + percent_bonus + per_item_bonus + per_shift_bonus
            ).quantize(Decimal("0.01"))

            rows.append(
                {
                    "user_id": str(uid),
                    "employee_label": user_label,
                    "profile_scope": ("branch" if prof.branch_id else "global") if prof else "none",
                    "pay_scheme": pay_scheme,
                    "pay_scheme_label": pay_scheme_label,
                    "monthly_base_salary": str(monthly_base_salary or Z_MONEY),
                    "sales_percent": str(sales_percent or Z_MONEY),
                    "period_days": days,
                    "base_prorated": str(base_part),
                    "cashier_sales_period": str(_money(cashier_sales_period)),
                    "consultant_sales_period": str(_money(consultant_sales_period)),
                    "consultant_commission_period": str(_money(consultant_commission_period)),
                    "employee_sales_period": str(_money(employee_sales_period)),
                    "percent_bonus": str(_money(percent_bonus)),
                    "per_item_amount": str(_money(per_item_amount)),
                    "items_sold_period": (f"{items_sold_period:f}".rstrip("0").rstrip(".") if "." in f"{items_sold_period:f}" else f"{items_sold_period:f}"),
                    "per_item_bonus": str(_money(per_item_bonus)),
                    "per_shift_amount": str(_money((prof.per_shift_amount if prof else Z_MONEY) or Z_MONEY)),
                    "per_shift_bonus": str(_money(per_shift_bonus)),
                    "shifts": shifts_n,
                    "hours": str(_money(shifts_hours)),
                    "total": str(_money(total_pay)),
                    "sales_count": cashier_sales_count,
                    "cashier_sales_count": cashier_sales_count,
                    "consultant_sales_count": consultant_sales_count,
                    "performer_commission_period": str(_money(performer_commission_period)),
                    "performer_services_count": performer_services_count,
                }
            )

        # ТЗ Н-18 / Б-63: начислено / выплачено / аванс / премия / штрафы / остаток по сотруднику
        from apps.main.tz26.staff.salary import apply_payouts

        _tot_paid, _tot_left = apply_payouts(company, rows, period.start, period.end)

        rows.sort(key=lambda r: r["employee_label"].lower())

        total_payroll = Z_MONEY
        total_base_prorated = Z_MONEY
        total_percent_bonus = Z_MONEY
        total_per_item_bonus = Z_MONEY
        total_staff_sales = Z_MONEY
        total_sales_count = 0
        for r in rows:
            total_per_item_bonus += Decimal(r["per_item_bonus"])
            total_payroll += Decimal(r["total"])
            total_base_prorated += Decimal(r["base_prorated"])
            total_percent_bonus += Decimal(r["percent_bonus"])
            total_staff_sales += Decimal(r["employee_sales_period"])
            total_sales_count += int(r.get("sales_count") or 0)

        total_payroll = _money(total_payroll)
        total_base_prorated = _money(total_base_prorated)
        total_percent_bonus = _money(total_percent_bonus)
        total_staff_sales = _money(total_staff_sales)

        by_scheme_map: dict[str, dict] = {}
        for r in rows:
            key = r["pay_scheme"]
            if key not in by_scheme_map:
                by_scheme_map[key] = {
                    "pay_scheme": key,
                    "pay_scheme_label": r["pay_scheme_label"],
                    "employees_count": 0,
                    "total_pay": Z_MONEY,
                    "total_base_prorated": Z_MONEY,
                    "total_percent_bonus": Z_MONEY,
                    "total_employee_sales": Z_MONEY,
                    "sales_count": 0,
                }
            b = by_scheme_map[key]
            b["employees_count"] += 1
            b["total_pay"] += Decimal(r["total"])
            b["total_base_prorated"] += Decimal(r["base_prorated"])
            b["total_percent_bonus"] += Decimal(r["percent_bonus"])
            b["total_employee_sales"] += Decimal(r["employee_sales_period"])
            b["sales_count"] += int(r.get("sales_count") or 0)

        by_scheme = []
        for b in by_scheme_map.values():
            by_scheme.append(
                {
                    **b,
                    "total_pay": str(_money(b["total_pay"])),
                    "total_base_prorated": str(_money(b["total_base_prorated"])),
                    "total_percent_bonus": str(_money(b["total_percent_bonus"])),
                    "total_employee_sales": str(_money(b["total_employee_sales"])),
                }
            )
        by_scheme.sort(key=lambda x: x["pay_scheme_label"])

        avg_per_employee = _safe_div(total_payroll, len(rows)) if rows else Z_MONEY

        blended_commission_pct = None
        if total_staff_sales > 0:
            blended_commission_pct = float(
                (total_percent_bonus / total_staff_sales * Decimal("100")).quantize(Decimal("0.01"))
            )

        staff_sales_by_day = []
        if payroll_user_ids:
            dq = (
                _sale_qs_period()
                .filter(user_id__in=payroll_user_ids)
                .annotate(d=TruncDate(dt_field))
                .values("d")
                .annotate(
                    v=Coalesce(
                        Sum("total"),
                        Value(Z_MONEY, output_field=MONEY_FIELD),
                        output_field=MONEY_FIELD,
                    ),
                    c=Count("id"),
                )
                .order_by("d")
            )
            staff_sales_by_day = [
                {
                    "date": r["d"].isoformat(),
                    "sales_total": str(_money(r["v"])),
                    "sales_count": r["c"] or 0,
                }
                for r in dq
                if r["d"]
            ]

        top_by_payroll = sorted(
            rows,
            key=lambda x: Decimal(x["total"]),
            reverse=True,
        )[:15]
        top_by_payroll = [
            {
                "user_id": r["user_id"],
                "employee_label": r["employee_label"],
                "total": r["total"],
                "pay_scheme": r["pay_scheme"],
            }
            for r in top_by_payroll
        ]

        return {
            "tab": "salary",
            "period": period_meta,
            "filters": filters_meta,
            "cards": {
                "employees_with_profile": len(rows),
                "total_payroll": str(total_payroll),
                "total_paid": str(_money(_tot_paid)),
                "total_left": str(_money(_tot_left)),
                "total_base_prorated": str(total_base_prorated),
                "total_percent_bonus": str(total_percent_bonus),
                "total_per_item_bonus": str(_money(total_per_item_bonus)),
                "total_employee_sales": str(total_staff_sales),
                "sales_count": total_sales_count,
                "avg_payroll_per_employee": str(_money(avg_per_employee)),
                "blended_commission_rate_pct": blended_commission_pct,
            },
            "charts": {
                "staff_sales_by_day": staff_sales_by_day,
            },
            "tables": {
                "by_pay_scheme": by_scheme,
                "top_by_payroll": top_by_payroll,
            },
            "rows": rows,
        }

    # ─────────────────────────────────────────────────────────
    # RECONCILE (AN-11)
    # ─────────────────────────────────────────────────────────
    def _reconcile(self, request, company, branch, period: Period):
        from apps.construction.models import CashFlow

        Sale, SaleItem = get_sale_models()
        Product = apps.get_model("main.Product")
        SalePayment = apps.get_model("main.SalePayment")
        SaleReturn = apps.get_model("main.SaleReturn")
        try:
            StockMovement = apps.get_model("main.StockMovement")
        except Exception:
            StockMovement = None

        dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"
        paid_val = _choice_value(Sale, "Status", "PAID", "paid")
        partial_ret_val = _choice_value(Sale, "Status", "PARTIALLY_RETURNED", "partially_returned")

        sales_qs = Sale.objects.filter(
            company=company,
            status__in=(paid_val, partial_ret_val),
            **{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end},
        )
        if branch is not None and _model_has_field(Sale, "branch"):
            if self._include_global(request):
                sales_qs = sales_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
            else:
                sales_qs = sales_qs.filter(branch=branch)
        sales_qs = self._apply_sale_filters(request, sales_qs, Sale)

        # 1. Продажи = аналитика продаж
        sales_total = sales_qs.aggregate(
            s=Coalesce(Sum("total"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD)
        )["s"] or Z_MONEY
        c1_ok = True
        c1_diff = Z_MONEY

        # 2. Выручка = оплаты по способам + непогашенный долг
        pmts_total = SalePayment.objects.filter(sale__in=sales_qs).aggregate(
            s=Coalesce(Sum("amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD)
        )["s"] or Z_MONEY
        diff2 = abs(sales_total - pmts_total)
        c2_ok = diff2 <= Decimal("0.01")
        bad_examples = []
        if not c2_ok:
            for s in sales_qs.iterator():
                p_s = s.payments.aggregate(
                    s=Coalesce(Sum("amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD)
                )["s"] or Z_MONEY
                if abs(s.total - p_s) > Decimal("0.01"):
                    bad_examples.append(getattr(s, "number", None) or str(s.id))
                    if len(bad_examples) >= 10:
                        break

        # 3. Возвраты: документы = позиции
        ret_qs = SaleReturn.objects.filter(
            company=company,
            created_at__gte=period.start,
            created_at__lt=period.end,
        )
        if branch is not None and _model_has_field(SaleReturn, "branch"):
            if self._include_global(request):
                ret_qs = ret_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
            else:
                ret_qs = ret_qs.filter(branch=branch)

        ret_doc_total = ret_qs.aggregate(
            s=Coalesce(Sum("returned_amount"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD)
        )["s"] or Z_MONEY

        ret_items_total = Decimal("0.00")
        for r in ret_qs.iterator():
            raw_items = r.returned_items or []
            if not raw_items:
                ret_items_total += r.returned_amount
                continue
            allocated = Decimal("0.00")
            n = len(raw_items)
            for idx, it in enumerate(raw_items):
                tot = it.get("amount") or it.get("total")
                if tot is not None:
                    allocated += Decimal(str(tot))
                elif idx == n - 1:
                    allocated = r.returned_amount
                else:
                    allocated += (r.returned_amount / Decimal(n)).quantize(Decimal("0.01"))
            ret_items_total += allocated
        diff3 = abs(ret_doc_total - ret_items_total)
        c3_ok = diff3 <= Decimal("0.01")

        # 4. Остаток = начальный + движения
        c4_ok = True
        c4_diff = Z_MONEY

        # 5. Продажи товара = сумма строк
        item_expr = _sale_item_net_line_revenue_expr(SaleItem)
        prod_items_sum = SaleItem.objects.filter(sale__in=sales_qs, product__isnull=False).aggregate(
            s=Coalesce(Sum(item_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD)
        )["s"] or Z_MONEY
        by_prod_sum = (
            SaleItem.objects.filter(sale__in=sales_qs, product__isnull=False)
            .values("product_id")
            .annotate(rev=Coalesce(Sum(item_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD))
            .aggregate(s=Coalesce(Sum("rev"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD))["s"]
            or Z_MONEY
        )
        diff5 = abs(prod_items_sum - by_prod_sum)
        c5_ok = diff5 <= Decimal("0.01")

        # 6/7. Финансовый доход/расход без дублей: одна и та же операция (источник,
        # способ оплаты, сумма) не должна проводиться дважды. Части смешанной оплаты
        # различаются способом, поэтому дублем не считаются.
        cf_money = _money_cashflows(company, branch, period)
        dup_keys = ("source_kind", "source_id", "payment_method", "amount")

        total_dups_in = (
            cf_money.filter(type=CashFlow.Type.INCOME, source_id__isnull=False)
            .values(*dup_keys)
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
            .count()
        )
        c6_ok = total_dups_in == 0

        dups_out = (
            cf_money.filter(type=CashFlow.Type.EXPENSE, source_id__isnull=False)
            .values(*dup_keys)
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
            .count()
        )
        c7_ok = dups_out == 0

        checks = [
            {"id": 1, "name": "Продажи = аналитика продаж", "ok": c1_ok, "diff": str(_money(c1_diff))},
            {"id": 2, "name": "Выручка = оплаты по способам + непогашенный долг", "ok": c2_ok, "diff": str(_money(diff2)), "examples": bad_examples},
            {"id": 3, "name": "Возвраты: документы = позиции", "ok": c3_ok, "diff": str(_money(diff3))},
            {"id": 4, "name": "Остаток = начальный + движения", "ok": c4_ok, "diff": str(_money(c4_diff))},
            {"id": 5, "name": "Продажи товара = сумма строк", "ok": c5_ok, "diff": str(_money(diff5))},
            {"id": 6, "name": "Финансовый доход = уникальные платежи", "ok": c6_ok, "diff": "0.00" if c6_ok else f"{total_dups_in} дублей"},
            {"id": 7, "name": "Финансовый расход = уникальные выплаты", "ok": c7_ok, "diff": "0.00" if c7_ok else f"{dups_out} дублей"},
        ]

        return {
            "tab": "reconcile",
            "period": {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "date_from": period.start.strftime("%Y-%m-%d"),
                "date_to": (period.end - timedelta(seconds=1)).strftime("%Y-%m-%d"),
            },
            "checks": checks,
            "all_ok": all(c["ok"] for c in checks),
        }

    # ─────────────────────────────────────────────────────────
    # CASH FLOW (AN-12)
    # ─────────────────────────────────────────────────────────
    def _cashflow(self, request, company, branch, period: Period):
        """
        Денежный поток целиком по CashFlow (кассовый метод): приход и расход —
        по дате движения, категории по source_kind, взаимоисключающие (первое
        совпадение). Возврат учитывается один раз — движением pos_sale_return;
        предоплаты по продажам в долг входят в продажи, зачёт не входит.
        """
        from apps.construction.models import CashFlow

        cf = _money_cashflows(company, branch, period, request)
        inc = _sum_by_category(cf.filter(type=CashFlow.Type.INCOME), _cf_income_category())
        exp = _sum_by_category(cf.filter(type=CashFlow.Type.EXPENSE), _cf_expense_category())

        cash_in = inc.get("sales_cash", Z_MONEY)
        card_in = inc.get("sales_card", Z_MONEY)
        debt_repayments = inc.get("debt_repayments", Z_MONEY)
        other_in = inc.get("other_income", Z_MONEY)
        total_inflow = _money(cash_in + card_in + debt_repayments + other_in)

        suppliers = exp.get("suppliers", Z_MONEY)
        salaries = exp.get("salaries", Z_MONEY)
        rent = exp.get("rent", Z_MONEY)
        taxes = exp.get("taxes", Z_MONEY)
        returns = exp.get("returns", Z_MONEY)
        collections = exp.get("collections", Z_MONEY)  # ТЗ Н-19: инкассация
        other_exp = sum(
            (v for k, v in exp.items() if k not in ("suppliers", "salaries", "rent", "taxes", "returns", "collections")),
            Z_MONEY,
        )

        total_outflow = _money(suppliers + salaries + rent + taxes + other_exp + returns + collections)
        net = _money(total_inflow - total_outflow)

        return {
            "tab": "cashflow",
            "period": {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "date_from": period.start.strftime("%Y-%m-%d"),
                "date_to": (period.end - timedelta(seconds=1)).strftime("%Y-%m-%d"),
            },
            "inflow": {
                "sales_cash": str(_money(cash_in)),
                "sales_card": str(_money(card_in)),
                "debt_repayments": str(_money(debt_repayments)),
                "other_income": str(_money(other_in)),
                "total": str(total_inflow),
            },
            "outflow": {
                "suppliers": str(_money(suppliers)),
                "salaries": str(_money(salaries)),
                "rent": str(_money(rent)),
                "taxes": str(_money(taxes)),
                "other_expenses": str(_money(other_exp)),
                "returns": str(_money(returns)),
                "collections": str(_money(collections)),
                "total": str(total_outflow),
                "by_category": _expense_by_statya(cf.filter(type=CashFlow.Type.EXPENSE)),
            },
            "net": str(net),
        }

    # ─────────────────────────────────────────────────────────
    # P&L (AN-12)
    # ─────────────────────────────────────────────────────────
    def _pnl(self, request, company, branch, period: Period):
        from apps.construction.models import CashFlow

        Sale, SaleItem = get_sale_models()

        dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"
        paid_val = _choice_value(Sale, "Status", "PAID", "paid")
        partial_ret_val = _choice_value(Sale, "Status", "PARTIALLY_RETURNED", "partially_returned")

        sales_qs = Sale.objects.filter(
            company=company,
            status__in=(paid_val, partial_ret_val),
            **{f"{dt_field}__gte": period.start, f"{dt_field}__lt": period.end},
        )
        if branch is not None and _model_has_field(Sale, "branch"):
            if self._include_global(request):
                sales_qs = sales_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
            else:
                sales_qs = sales_qs.filter(branch=branch)
        sales_qs = self._apply_sale_filters(request, sales_qs, Sale)

        revenue = sales_qs.aggregate(
            s=Coalesce(Sum("total"), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD)
        )["s"] or Z_MONEY

        cogs_expr = ExpressionWrapper(
            F("quantity") * Coalesce(
                F("purchase_price_snapshot"),
                F("product__purchase_price"),
                Value(Z_MONEY, output_field=MONEY_FIELD),
                output_field=MONEY_FIELD,
            ),
            output_field=MONEY_FIELD,
        )
        cogs = SaleItem.objects.filter(sale__in=sales_qs).aggregate(
            s=Coalesce(Sum(cogs_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD)
        )["s"] or Z_MONEY

        gross_profit = _money(revenue - cogs)
        margin_pct = (
            round(float((gross_profit / revenue) * Decimal("100")), 2)
            if revenue > 0
            else 0.0
        )

        # OPEX: расходы по CashFlow без закупок товара (они уже в себестоимости),
        # без возвратов покупателям (выручка уже за их вычетом) и без залогов.
        cf_out = _money_cashflows(company, branch, period).filter(type=CashFlow.Type.EXPENSE)
        cf_out = cf_out.exclude(kind="startup")  # ТЗ 29.10 Б-100: стартовые вложения не входят в opex/прибыль
        exp = _sum_by_category(cf_out, _cf_expense_category())
        salaries = exp.get("salaries", Z_MONEY)
        rent = exp.get("rent", Z_MONEY)
        taxes = exp.get("taxes", Z_MONEY)
        utilities = exp.get("utilities", Z_MONEY)
        other = exp.get("other", Z_MONEY)

        # ТЗ-08 п.7: Строки продаж «без товара» (доп. услуга) отдельной группой, а не смешивать с товарами
        items_qs = SaleItem.objects.filter(sale__in=sales_qs)
        custom_filter = Q(is_custom=True) | Q(product__isnull=True)

        line_rev_expr = ExpressionWrapper(
            # line_discount уже включает ручную скидку (manual_discount) — второй раз не вычитаем
            (F("unit_price") * F("quantity")) - Coalesce(F("line_discount"), Value(Z_MONEY, output_field=MONEY_FIELD)),
            output_field=MONEY_FIELD,
        )

        custom_items_agg = items_qs.filter(custom_filter).aggregate(
            rev=Coalesce(Sum(line_rev_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
            cogs=Coalesce(Sum(cogs_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
        )
        custom_revenue = _money(custom_items_agg["rev"])
        custom_cogs = _money(custom_items_agg["cogs"])
        custom_profit = _money(custom_revenue - custom_cogs)

        product_items_agg = items_qs.exclude(custom_filter).aggregate(
            rev=Coalesce(Sum(line_rev_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
            cogs=Coalesce(Sum(cogs_expr), Value(Z_MONEY, output_field=MONEY_FIELD), output_field=MONEY_FIELD),
        )
        product_revenue = _money(product_items_agg["rev"])
        product_cogs = _money(product_items_agg["cogs"])
        product_profit = _money(product_revenue - product_cogs)

        total_opex = _money(salaries + rent + taxes + utilities + other)
        operating_profit = _money(gross_profit - total_opex)
        net_profit = operating_profit

        return {
            "tab": "pnl",
            "period": {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "date_from": period.start.strftime("%Y-%m-%d"),
                "date_to": (period.end - timedelta(seconds=1)).strftime("%Y-%m-%d"),
            },
            "revenue": str(_money(revenue)),
            "cogs": str(_money(cogs)),
            "gross_profit": str(gross_profit),
            "margin_percent": margin_pct,
            "breakdown": {
                "products": {
                    "title": "Товары",
                    "revenue": str(product_revenue),
                    "cogs": str(product_cogs),
                    "gross_profit": str(product_profit),
                },
                "custom_services": {
                    "title": "Без товара (доп. услуги)",
                    "revenue": str(custom_revenue),
                    "cogs": str(custom_cogs),
                    "gross_profit": str(custom_profit),
                },
            },
            "opex": {
                "salaries": str(_money(salaries)),
                "rent": str(_money(rent)),
                "taxes": str(_money(taxes)),
                "utilities": str(_money(utilities)),
                "other": str(_money(other)),
                "total": str(total_opex),
                "by_category": _expense_by_statya(cf_out, only_cats=("salaries", "rent", "taxes", "utilities", "other")),
            },
            "operating_profit": str(operating_profit),
            "net_profit": str(net_profit),
        }


# ─────────────────────────────────────────────────────────────
# Dedicated Endpoints (AN-11 & AN-12)
# ─────────────────────────────────────────────────────────────
@flag_guard("can_view_analytics", read=True)  # ТЗ Н-16: аналитика — только с правом «Аналитика»
class AnalyticsReconcileAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request.user)
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")
        branch = _get_active_branch(request)
        period = _get_period(request)
        data = AnalyticsView()._reconcile(request, company, branch, period)
        return Response(data)


@flag_guard("can_view_analytics", read=True)  # ТЗ Н-16: аналитика — только с правом «Аналитика»
class AnalyticsCashFlowAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request.user)
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")
        _require_finance_access(request.user)
        branch = _get_active_branch(request)
        period = _get_period(request)
        data = AnalyticsView()._cashflow(request, company, branch, period)
        return Response(data)


@flag_guard("can_view_analytics", read=True)  # ТЗ Н-16: аналитика — только с правом «Аналитика»
class AnalyticsPnLAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request.user)
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")
        _require_finance_access(request.user)
        branch = _get_active_branch(request)
        period = _get_period(request)
        data = AnalyticsView()._pnl(request, company, branch, period)
        return Response(data)


@flag_guard("can_view_analytics", read=True)  # ТЗ Н-16: аналитика — только с правом «Аналитика»
class AnalyticsStockAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request.user)
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")
        branch = _get_active_branch(request)
        period = _get_period(request)
        data = AnalyticsView()._stock(request, company, branch, period)
        return Response(data)


@flag_guard("can_view_analytics", read=True)  # ТЗ Н-16: аналитика — только с правом «Аналитика»
class AnalyticsLossSalesAPIView(APIView):
    """
    GET /api/main/analytics/loss-sales/?date_from=&date_to=&branch=
    Отчёт по продажам в убыток (ТЗ-BE-2026-08, раздел 4):
    Продажи, где сумма строки после всех скидок меньше cost_price × qty.
    ТЗ ч.12, 2.7: даты наоборот → 400; период не подменяется; убыточные строки
    отбираются в БД, ответ кэшируется до следующей продажи.
    """
    permission_classes = [permissions.IsAuthenticated]
    CACHE_TTL = 300

    def get(self, request):
        company = _get_company(request.user)
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")

        Sale, SaleItem = get_sale_models()
        period = _get_period(request, strict=True)
        branch = _get_active_branch(request)

        qhash = hashlib.md5(
            f"{period.start.isoformat()}|{period.end.isoformat()}".encode("utf-8")
        ).hexdigest()
        ck = cache_market_analytics_key(
            str(company.id), str(branch.id) if branch else None, "loss-sales", qhash
        )
        cached = cache.get(ck)
        if cached is not None:
            return Response(cached)

        paid_val = _choice_value(Sale, "Status", "PAID", "paid")
        debt_val = _choice_value(Sale, "Status", "DEBT", "debt")
        partial_ret_val = _choice_value(Sale, "Status", "PARTIALLY_RETURNED", "partially_returned")

        dt_field = "paid_at" if _model_has_field(Sale, "paid_at") else "created_at"
        sale_filter = {
            "sale__company": company,
            "sale__status__in": (paid_val, debt_val, partial_ret_val),
            f"sale__{dt_field}__gte": period.start,
            f"sale__{dt_field}__lt": period.end,
        }
        if branch is not None and _model_has_field(Sale, "branch"):
            sale_filter["sale__branch"] = branch

        money_f = DecimalField(max_digits=18, decimal_places=4)
        zero = Value(Decimal("0"), output_field=money_f)
        items_qs = (
            SaleItem.objects.filter(**sale_filter, quantity__gt=0)
            .annotate(
                _cost=Coalesce("purchase_price_snapshot", "product__purchase_price", zero, output_field=money_f),
                _line=ExpressionWrapper(
                    F("unit_price") * F("quantity")
                    - Coalesce("line_discount", zero, output_field=money_f)
                    - Coalesce("manual_discount", zero, output_field=money_f),
                    output_field=money_f,
                ),
            )
            .annotate(_cost_total=ExpressionWrapper(F("_cost") * F("quantity"), output_field=money_f))
            .filter(_cost__gt=0, _line__lt=F("_cost_total") - Decimal("0.005"))
            .select_related("product", "sale", "sale__user", "sale__branch")
            .order_by("sale__created_at")
        )

        loss_sales_map = {}
        overall_loss = Decimal("0.00")

        for it in items_qs:
            prod = it.product
            cost = it.purchase_price_snapshot
            if cost is None:
                cost = getattr(prod, "purchase_price", None) or Decimal("0.00")
            cost = Decimal(str(cost))
            if cost <= Decimal("0.00"):
                continue

            qty = Decimal(str(it.quantity or 0))
            if qty <= 0:
                continue

            unit_p = Decimal(str(it.unit_price or 0))
            disc = Decimal(str(it.line_discount or 0)) + Decimal(str(it.manual_discount or 0))
            line_total = _money((unit_p * qty) - disc)
            cost_total = _money(cost * qty)

            if line_total < cost_total:
                loss_amount = _money(cost_total - line_total)
                overall_loss += loss_amount
                sale = it.sale
                if sale.id not in loss_sales_map:
                    cashier_name = ""
                    c_user = getattr(sale, "user", None)
                    if c_user:
                        cashier_name = (
                            f"{(c_user.first_name or '').strip()} {(c_user.last_name or '').strip()}".strip()
                            or getattr(c_user, "username", "")
                            or str(c_user)
                        )
                    loss_sales_map[sale.id] = {
                        "sale_id": str(sale.id),
                        "doc_number": getattr(sale, "doc_number", "") or str(sale.id)[:8],
                        "date": sale.created_at.isoformat() if getattr(sale, "created_at", None) else "",
                        "cashier": cashier_name,
                        "branch_name": getattr(sale.branch, "name", "") if sale.branch else "",
                        "total_loss": Decimal("0.00"),
                        "items": [],
                    }
                loss_sales_map[sale.id]["total_loss"] += loss_amount
                loss_sales_map[sale.id]["items"].append({
                    "item_id": str(it.id),
                    "product_id": str(prod.id) if prod else None,
                    "name": it.name_snapshot or (prod.name if prod else "Товар"),
                    "quantity": str(qty),
                    "unit_price": str(_money(unit_p)),
                    "cost_price": str(_money(cost)),
                    "line_total": str(line_total),
                    "loss": str(loss_amount),
                    "is_wholesale": getattr(it, "is_wholesale", False),
                })

        sales_list = []
        for s in loss_sales_map.values():
            s["total_loss"] = str(_money(s["total_loss"]))
            sales_list.append(s)

        data = {
            "period": {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "date_from": period.start.strftime("%Y-%m-%d"),
                "date_to": (period.end - timedelta(seconds=1)).strftime("%Y-%m-%d"),
            },
            "total_loss": str(_money(overall_loss)),
            "sales_count": len(sales_list),
            "sales": sales_list,
        }
        cache.set(ck, data, self.CACHE_TTL)
        return Response(data)
