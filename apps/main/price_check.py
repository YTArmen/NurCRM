"""
«Калькуляция» → «Проверка цен»: итоги склада и проблемные товары считает сервер.

GET /api/main/products/price-check/
    ?filter=all|loss|lowMargin|noCost|negativeStock   (по умолчанию all)
    &threshold=15         порог «низкой маржи», %, 0–100
    &search=...           по названию, артикулу и штрихкоду (в т.ч. дополнительному)
    &page=1&page_size=200 (до 500)
    &ordering=name|margin|-margin|stock_profit|-stock_profit
    &branch=<uuid>|main   как в /main/products/list/

Правила расчёта совпадают с checkCatalogPrices (фронт, src/shared/lib/pricing.ts),
см. calculator-after-stress-test/01-price-check-endpoint.md §1.3:
  cost = purchase_price, «есть закупка» = cost > 0, отрицательный остаток в итогах = 0;
  markup = (price − cost)/cost×100; margin = (price − cost)/price×100;
  stock_profit = (price − cost)×max(qty, 0); метки loss, noMarkup, lowMargin, noCost, negativeStock.
Услуги (kind=service) и архивные товары не участвуют.
Итоги и счётчики — один агрегирующий запрос, строки — одна страница; товары в Python не выгружаются.
"""
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db.models import Case, Count, DecimalField, ExpressionWrapper, F, Max, Q, Sum, Value, When
from django.http import HttpResponse
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Product
from core.http_cache import ETagMixin
from .views import CompanyBranchRestrictedMixin, _barcode_lookup_variants, _filter_products_company_only

FILTERS = ("all", "loss", "lowMargin", "noCost", "negativeStock")
ORDERINGS = ("name", "margin", "-margin", "stock_profit", "-stock_profit")
FLAG_ORDER = ("loss", "noMarkup", "lowMargin", "noCost", "negativeStock")
DEFAULT_THRESHOLD = Decimal("15")

_DEC = DecimalField(max_digits=30, decimal_places=6)
_ZERO = Value(Decimal("0"), output_field=_DEC)
_HUNDRED = Value(Decimal("100"), output_field=_DEC)

COST = Coalesce(F("purchase_price"), _ZERO, output_field=_DEC)
PRICE = Coalesce(F("price"), _ZERO, output_field=_DEC)
QTY = Coalesce(F("quantity"), _ZERO, output_field=_DEC)
POS_QTY = Greatest(QTY, _ZERO, output_field=_DEC)

HAS_COST = Q(purchase_price__gt=0)

# Условия меток. lowMargin: margin < threshold ⇔ (price − cost)×100 − threshold×price < 0
# (при price > cost > 0 цена положительна) — через аннотацию pc_lm_diff.
FLAG_Q = {
    "loss": HAS_COST & Q(price__lt=F("purchase_price")),
    "noMarkup": HAS_COST & Q(price=F("purchase_price")),
    "lowMargin": HAS_COST & Q(price__gt=F("purchase_price")) & Q(pc_lm_diff__lt=0),
    "noCost": ~HAS_COST,
    "negativeStock": Q(quantity__lt=0),
}
FILTER_Q = {
    "all": Q(),
    "loss": FLAG_Q["loss"] | FLAG_Q["noMarkup"],
    "lowMargin": FLAG_Q["lowMargin"],
    "noCost": FLAG_Q["noCost"],
    "negativeStock": FLAG_Q["negativeStock"],
}


def _q2(value):
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _q3(value):
    return str(Decimal(value or 0).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def annotate_price_check(qs, threshold: Decimal):
    t = Value(threshold, output_field=_DEC)
    return qs.annotate(
        pc_lm_diff=ExpressionWrapper((PRICE - COST) * _HUNDRED - t * PRICE, output_field=_DEC),
        pc_markup=Case(
            When(HAS_COST, then=ExpressionWrapper((PRICE - COST) * _HUNDRED / COST, output_field=_DEC)),
            default=None,
            output_field=_DEC,
        ),
        pc_margin=Case(
            When(HAS_COST & Q(price__gt=0), then=ExpressionWrapper((PRICE - COST) * _HUNDRED / PRICE, output_field=_DEC)),
            default=None,
            output_field=_DEC,
        ),
        pc_stock_profit=Case(
            When(HAS_COST, then=ExpressionWrapper((PRICE - COST) * POS_QTY, output_field=_DEC)),
            default=None,
            output_field=_DEC,
        ),
    )


def compute_summary(qs) -> dict:
    """Итоги по всему складу (без search и filter)."""
    agg = qs.aggregate(
        stock_at_cost=Coalesce(Sum(POS_QTY * Greatest(COST, _ZERO, output_field=_DEC), output_field=_DEC), _ZERO),
        stock_at_price=Coalesce(Sum(POS_QTY * Greatest(PRICE, _ZERO, output_field=_DEC), output_field=_DEC), _ZERO),
        future_profit=Coalesce(Sum((PRICE - COST) * POS_QTY, filter=HAS_COST, output_field=_DEC), _ZERO),
        revenue_with_cost=Coalesce(Sum(PRICE * POS_QTY, filter=HAS_COST, output_field=_DEC), _ZERO),
        products_total=Count("id"),
    )
    den = Decimal(agg["revenue_with_cost"] or 0)
    avg = (Decimal(agg["future_profit"]) / den * 100) if den != 0 else None
    return {
        "stock_at_cost": _q2(agg["stock_at_cost"]),
        "stock_at_price": _q2(agg["stock_at_price"]),
        "future_profit": _q2(agg["future_profit"]),
        "average_margin": _q2(avg),
        "products_total": agg["products_total"],
    }


def compute_counts(qs) -> dict:
    """Счётчики на кнопках фильтров (с учётом search, без filter); qs уже аннотирован."""
    agg = qs.aggregate(**{name: Count("id", filter=cond) if name != "all" else Count("id") for name, cond in FILTER_Q.items()})
    return {name: agg[name] for name in FILTERS}


def row_flags(p) -> list:
    flags = []
    cost = Decimal(p.purchase_price or 0)
    price = Decimal(p.price or 0)
    if cost > 0:
        if price < cost:
            flags.append("loss")
        elif price == cost:
            flags.append("noMarkup")
        elif p.pc_lm_diff is not None and Decimal(p.pc_lm_diff) < 0:
            flags.append("lowMargin")
    else:
        flags.append("noCost")
    if Decimal(p.quantity or 0) < 0:
        flags.append("negativeStock")
    return flags


class PriceCheckPagination(PageNumberPagination):
    page_size = 200
    page_size_query_param = "page_size"
    max_page_size = 500


def _parse_threshold(raw):
    if raw in (None, ""):
        return DEFAULT_THRESHOLD
    try:
        value = Decimal(str(raw).replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValidationError({"threshold": "Ожидается число от 0 до 100."})
    if not value.is_finite() or value < 0 or value > 100:
        raise ValidationError({"threshold": "Ожидается число от 0 до 100."})
    return value


def _search_q(term: str):
    term = (term or "").strip()
    if not term:
        return None
    q = (
        Q(name__icontains=term)
        | Q(article__icontains=term)
        | Q(barcode__icontains=term)
        | Q(alternate_barcodes__barcode__icontains=term)
    )
    if term.isdigit() and len(term) >= 8:
        variants = _barcode_lookup_variants(term) or []
        if variants:
            q |= Q(barcode__in=variants) | Q(alternate_barcodes__barcode__in=variants)
    return q


# ---------------------------------------------------------------------------
# Версия каталога (02-catalog-change-marker.md): ETag/304 и GET catalog-version/
# ---------------------------------------------------------------------------

_BIG = DecimalField(max_digits=40, decimal_places=6)


def catalog_version(qs) -> dict:
    """
    Версия каталога, вычисленная из самих данных одним агрегатом: количество товаров,
    max(updated_at) и контрольные суммы остатка, цены и закупки (в т.ч. взвешенные
    по seq). Меняется при любом изменении товара, цены, закупки, остатка, при
    добавлении и удалении — включая обновления остатка через queryset.update(),
    которые не трогают updated_at. Без сигналов и без сброса кэша.
    """
    seq = Coalesce(F("seq"), Value(0), output_field=_BIG)
    agg = qs.aggregate(
        n=Count("id"),
        last=Max("updated_at"),
        q=Coalesce(Sum(QTY, output_field=_BIG), _ZERO),
        p=Coalesce(Sum(PRICE, output_field=_BIG), _ZERO),
        c=Coalesce(Sum(COST, output_field=_BIG), _ZERO),
        wq=Coalesce(Sum(QTY * seq, output_field=_BIG), _ZERO),
        wp=Coalesce(Sum((PRICE + COST * Value(Decimal("7"), output_field=_BIG)) * seq, output_field=_BIG), _ZERO),
    )
    last = agg["last"]
    raw = "|".join(str(agg[k]) for k in ("n", "last", "q", "p", "c", "wq", "wp"))
    return {
        "version": hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20],
        "updated_at": timezone.localtime(last).isoformat() if last else None,
        "products_total": agg["n"],
    }


def _etag_matches(header_value, etag) -> bool:
    if not header_value:
        return False
    if header_value.strip() == "*":
        return True
    bare = etag.strip('"')
    for token in header_value.split(","):
        t = token.strip()
        if t.startswith("W/"):
            t = t[2:]
        if t.strip('"') == bare:
            return True
    return False


def _base_queryset(view):
    qs = _filter_products_company_only(view, Product.objects.all())
    return qs.exclude(kind=Product.Kind.SERVICE)


class ProductPriceCheckView(CompanyBranchRestrictedMixin, APIView):
    """Вкладка «Проверка цен» калькуляции: итоги, счётчики и страница строк одним запросом."""

    pagination_class = PriceCheckPagination

    def get(self, request, *args, **kwargs):
        qp = request.query_params
        flt = (qp.get("filter") or "all").strip()
        if flt not in FILTERS:
            raise ValidationError({"filter": f"Допустимо: {', '.join(FILTERS)}."})
        ordering = (qp.get("ordering") or "name").strip()
        if ordering not in ORDERINGS:
            raise ValidationError({"ordering": f"Допустимо: {', '.join(ORDERINGS)}."})
        threshold = _parse_threshold(qp.get("threshold"))

        calculated_at = timezone.localtime()
        base = _base_queryset(self)

        # ETag = версия каталога + параметры запроса + область (компания/филиал).
        # Совпал с If-None-Match → 304 без пересчёта.
        version = catalog_version(base)["version"]
        params = "&".join(f"{k}={v}" for k, v in sorted(qp.items()))
        scope = f"{getattr(self._company(), 'pk', '')}|{getattr(self._auto_branch(), 'pk', '')}"
        etag = '"pc-' + hashlib.sha1(f"{version}|{scope}|{params}".encode("utf-8")).hexdigest()[:24] + '"'
        if _etag_matches(request.headers.get("If-None-Match"), etag):
            resp = HttpResponse(status=304)
            resp["ETag"] = etag
            resp["Cache-Control"] = "private, no-cache"
            return resp

        summary = compute_summary(base)

        searched = annotate_price_check(base, threshold)
        sq = _search_q(qp.get("search"))
        if sq is not None:
            # Поиск по доп. штрихкодам даёт JOIN — сужаем по id, чтобы не задваивать товары.
            ids = Product.objects.filter(pk__in=base.values("pk")).filter(sq).values("pk")
            searched = searched.filter(pk__in=ids)
        counts = compute_counts(searched)

        rows = searched.filter(FILTER_Q[flt])
        if ordering == "name":
            rows = rows.order_by("name", "id")
        else:
            field = "pc_" + ordering.lstrip("-")
            expr = F(field).desc(nulls_last=True) if ordering.startswith("-") else F(field).asc(nulls_last=True)
            rows = rows.order_by(expr, "name", "id")
        rows = rows.only("id", "name", "barcode", "purchase_price", "price", "quantity")

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(rows, request, view=self)
        results = [
            {
                "id": str(p.id),
                "name": p.name,
                "barcode": p.barcode,
                "purchase_price": _q2(p.purchase_price or 0),
                "price": _q2(p.price or 0),
                "quantity": _q3(p.quantity),
                "markup": _q2(p.pc_markup),
                "margin": _q2(p.pc_margin),
                "stock_profit": _q2(p.pc_stock_profit),
                "flags": row_flags(p),
            }
            for p in page
        ]
        resp = Response({
            "catalog_version": version,
            "summary": summary,
            "counts": counts,
            "count": paginator.page.paginator.count,
            "next": paginator.get_next_link(),
            "previous": paginator.get_previous_link(),
            "results": results,
            "threshold": _q2(threshold),
            "calculated_at": calculated_at.isoformat(),
        })
        resp["ETag"] = etag
        resp["Cache-Control"] = "private, no-cache"
        return resp


class ProductCatalogVersionView(ETagMixin, CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/products/catalog-version/ → {"version", "updated_at", "products_total"}

    Лёгкая проверка «изменился ли каталог» для /main/products/list/: та же область,
    что у списка (компания, ?branch=, без архивных). Фронт перезагружает каталог,
    только если version поменялась.
    """

    def get(self, request, *args, **kwargs):
        qs = _filter_products_company_only(self, Product.objects.all())
        data = catalog_version(qs)
        resp = Response(data)
        resp["Cache-Control"] = "private, no-cache"
        return resp
