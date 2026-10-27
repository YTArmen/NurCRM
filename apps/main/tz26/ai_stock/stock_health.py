"""Б-66: отчёт по товарам для рекомендаций (analytics/market/?tab=stock_health и analytics/stock-health/).

Быстро на 15 тыс. товаров: два запроса — (1) агрегаты продаж по всем товарам одним GROUP BY
с условными суммами (last_sold_at, sold_30d, sold_90d), (2) список товаров. Дальше считаем в памяти.
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Max, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ApiError

from apps.users.permissions_flags import flag_guard, is_privileged

ORDERING_FIELDS = {
    "name", "code", "quantity", "last_sold_at", "sold_30d", "sold_90d", "avg_per_day_30d",
    "days_of_stock", "stock_value_purchase", "markup_percent",
}
PURCHASE_ORDERING = {"stock_value_purchase", "markup_percent"}
MAX_LIMIT = 20000
DEFAULT_LIMIT = 50
Q2 = Decimal("0.01")
Q3 = Decimal("0.001")


def can_view_purchase(user) -> bool:
    """Закупочные поля: владелец/админ или флаг «Маркет: доступ к закупкам»."""
    return is_privileged(user) or getattr(user, "can_view_market_procurement", None) is True


def _int_param(qp, name):
    raw = qp.get(name)
    if raw in (None, ""):
        return None
    try:
        v = int(raw)
    except ValueError:
        raise ApiError("validation_error", f"{name}: нужно целое число.", fields={name: ["Нужно целое число"]})
    if v < 0:
        raise ApiError("validation_error", f"{name}: не меньше 0.", fields={name: ["Не меньше 0"]})
    return v


def _dec_param(qp, name):
    raw = qp.get(name)
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw).replace(",", "."))
    except InvalidOperation:
        raise ApiError("validation_error", f"{name}: нужно число.", fields={name: ["Нужно число"]})


def _sort_key(field):
    def key(row):
        v = row[field]
        if field == "last_sold_at" and v is not None:
            v = v.timestamp()
        return v
    return key


def stock_health_data(request, company, branch, include_global=False) -> dict:
    from apps.main.models import Product, Sale, SaleItem

    qp = request.query_params
    purchase_ok = can_view_purchase(request.user)
    no_sales_days = _int_param(qp, "no_sales_days")
    markup_below = _dec_param(qp, "markup_below")
    dos_below = _dec_param(qp, "days_of_stock_below")
    if markup_below is not None and not purchase_ok:
        raise ApiError("permission_denied", "Наценка считается от закупки: нужно право на закупки.",
                       status_code=403, data={"required": "can_view_market_procurement"})

    ordering = (qp.get("ordering") or "name").strip()
    desc = ordering.startswith("-")
    ofield = ordering.lstrip("-")
    if ofield not in ORDERING_FIELDS:
        raise ApiError("validation_error", f"ordering: допустимо {', '.join(sorted(ORDERING_FIELDS))}.",
                       fields={"ordering": ["Недопустимое значение"]})
    if ofield in PURCHASE_ORDERING and not purchase_ok:
        raise ApiError("permission_denied", "Сортировка по закупочным полям недоступна.", status_code=403,
                       data={"required": "can_view_market_procurement"})

    limit_raw = (qp.get("limit") or "").strip().lower()
    if limit_raw in ("all", "0", "-1"):
        limit = None
    elif limit_raw.isdigit():
        limit = min(int(limit_raw), MAX_LIMIT)
    else:
        limit = DEFAULT_LIMIT
    offset = _int_param(qp, "offset") or 0

    now = timezone.now()
    d30 = now - timedelta(days=30)
    d90 = now - timedelta(days=90)

    # (1) продажи по товарам: один GROUP BY на всю компанию
    sales_dt = Coalesce("sale__paid_at", "sale__created_at")
    items = SaleItem.objects.filter(
        company=company, product__isnull=False,
        sale__status__in=(Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED),
    )
    if branch is not None:
        bq = Q(sale__branch=branch) | Q(sale__branch__isnull=True) if include_global else Q(sale__branch=branch)
        items = items.filter(bq)
    agg = {
        r["product_id"]: r
        for r in items.annotate(_dt=sales_dt).values("product_id").annotate(
            last=Max("_dt"),
            s30=Sum("quantity", filter=Q(_dt__gte=d30)),
            s90=Sum("quantity", filter=Q(_dt__gte=d90)),
        )
    }

    # (2) товары
    pqs = Product.objects.filter(company=company).exclude(kind="service").exclude(status__in=("rejected", "archived"))
    if branch is not None:
        pqs = pqs.filter(Q(branch=branch) | Q(branch__isnull=True)) if include_global else pqs.filter(branch=branch)
    products = pqs.values("id", "name", "code", "article", "barcode", "category__name", "quantity",
                          "price", "purchase_price", "created_at")

    zero = Decimal("0")
    no_sales_edge = now - timedelta(days=no_sales_days) if no_sales_days is not None else None
    rows = []
    for p in products.iterator(chunk_size=5000):
        a = agg.get(p["id"])
        last = a["last"] if a else None
        s30 = (a["s30"] or zero) if a else zero
        s90 = (a["s90"] or zero) if a else zero
        qty = p["quantity"] or zero
        if no_sales_edge is not None:
            if last is not None:
                if last >= no_sales_edge:
                    continue
            elif p["created_at"] is not None and p["created_at"] >= no_sales_edge:
                continue  # товар новее порога: «не продаётся» говорить рано
        avg30 = (s30 / Decimal(30))
        dos = (qty / avg30) if avg30 > 0 else None
        if dos_below is not None and (dos is None or dos >= dos_below):
            continue
        price = p["price"] or zero
        pp = p["purchase_price"] or zero
        markup = ((price / pp - 1) * 100) if pp > 0 else None
        if markup_below is not None and (markup is None or markup >= markup_below):
            continue
        row = {
            "product_id": str(p["id"]),
            "name": p["name"],
            "code": p["code"],
            "article": p["article"],
            "barcode": p["barcode"],
            "category": p["category__name"],
            "quantity": qty.quantize(Q3),
            "price": price.quantize(Q2),
            "last_sold_at": last,
            "sold_30d": s30.quantize(Q3),
            "sold_90d": s90.quantize(Q3),
            "avg_per_day_30d": avg30.quantize(Q3),
            "days_of_stock": dos.quantize(Q2) if dos is not None else None,
        }
        if purchase_ok:
            row["purchase_price"] = pp.quantize(Q2)
            row["stock_value_purchase"] = (qty * pp).quantize(Q2)
            row["markup_percent"] = markup.quantize(Q2) if markup is not None else None
        rows.append(row)

    # сортировка: пустые значения всегда в конце
    full = [r for r in rows if r[ofield] is not None]
    empty = [r for r in rows if r[ofield] is None]
    key = _sort_key(ofield)
    if ofield in ("name", "code"):
        full.sort(key=lambda r: (r[ofield] or "").lower(), reverse=desc)
    else:
        full.sort(key=key, reverse=desc)
    rows = full + empty
    total = len(rows)
    page = rows[offset:] if limit is None else rows[offset:offset + limit]
    return {
        "tab": "stock_health",
        "generated_at": now,
        "purchase_fields": purchase_ok,
        "count": total,
        "limit": limit,
        "offset": offset,
        "results": page,
    }


@flag_guard("can_view_analytics", read=True)
class StockHealthView(APIView):
    """GET api/main/analytics/stock-health/ — то же, что analytics/market/?tab=stock_health."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.analytics_market import _get_active_branch, _get_company

        company = _get_company(request.user)
        if not company:
            raise ApiError("permission_denied", "У пользователя не настроена компания.", status_code=403)
        branch = _get_active_branch(request)
        include_global = (request.query_params.get("include_global") or "").strip() in ("1", "true", "yes", "on")
        return Response(stock_health_data(request, company, branch, include_global))
