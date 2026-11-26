"""
Фильтры GET /api/main/products/list/ (склад маркета, модалка «Фильтры»).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from apps.main.models import Product, ProductAlternateBarcode, ProductPackage, ProductPromotionTier, Sale, SaleItem

VALID_KINDS = {c[0] for c in Product.Kind.choices}
VALID_PRESETS = {
    "discounted",
    "shelf_life_expires_7d",
    "zero_cost",
    "shelf_life_expired",
    "out_of_stock",
    "not_sold_90d",
    "negative_stock",
    "stock_below_min",
    "low_stock",
}

# Порог «мало» для товаров без своего minimum_quantity (preset=low_stock).
LOW_STOCK_DEFAULT_THRESHOLD = Decimal("5")

# Обратная совместимость с кириллическими slug (старый фронт)
_PRESET_ALIASES = {
    "товары_со_скидкой": "discounted",
    "срок_годности_7д": "shelf_life_expires_7d",
    "срок_годности_истекает_7д": "shelf_life_expires_7d",
    "нулевая_себестоимость": "zero_cost",
    "истёк_срок_годности": "shelf_life_expired",
    "истек_срок_годности": "shelf_life_expired",
    "нет_в_наличии": "out_of_stock",
    "не_продаются_3_месяца": "not_sold_90d",
    "не_продаются_90д": "not_sold_90d",
    "отрицательный_остаток": "negative_stock",
    "остаток_ниже_минимума": "stock_below_min",
    "общий_остаток_меньше_минимального": "stock_below_min",
}


def _parse_decimal(raw) -> Decimal | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", ".")
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _parse_int(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _parse_bool(raw) -> bool | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def _parse_kind_list(query_params) -> list[str]:
    raw: list[str] = []
    if hasattr(query_params, "getlist"):
        raw.extend(query_params.getlist("kind"))
        raw.extend(query_params.getlist("kind[]"))
    single = (query_params.get("kind") or "").strip()
    if single:
        if "," in single:
            raw.extend(x.strip() for x in single.split(",") if x.strip())
        else:
            raw.append(single)
    kinds = []
    for k in raw:
        v = str(k).strip().lower()
        if v in VALID_KINDS:
            kinds.append(v)
    return list(dict.fromkeys(kinds))


def _normalize_preset(raw: str | None) -> str | None:
    if not raw:
        return None
    p = str(raw).strip().lower()
    p = _PRESET_ALIASES.get(p, p)
    return p if p in VALID_PRESETS else None


def _compare_field(qs, field_name: str, condition: str, value: Decimal):
    if condition == "gt":
        return qs.filter(**{f"{field_name}__gt": value})
    if condition == "lt":
        return qs.filter(**{f"{field_name}__lt": value})
    return qs.filter(**{f"{field_name}": value})


def _price_field(price_type: str) -> str | None:
    mapping = {
        "base": "price",
        "purchase": "purchase_price",
        "cost": "purchase_price",
        "discount": "discount_percent",
    }
    return mapping.get((price_type or "").strip().lower())


def _apply_preset(qs, preset: str, low_stock_threshold=None):
    today = timezone.localdate()

    if preset == "discounted":
        return qs.filter(Q(discount_percent__gt=0) | Q(stock=True))
    if preset in (
        "shelf_life_expires_7d",
        "zero_cost",
        "shelf_life_expired",
        "out_of_stock",
        "negative_stock",
        "stock_below_min",
        "low_stock",
    ):
        qs = qs.exclude(kind=Product.Kind.SERVICE)

    if preset == "shelf_life_expires_7d":
        end = today + timedelta(days=7)
        return qs.filter(
            expiration_date__isnull=False,
            expiration_date__gte=today,
            expiration_date__lte=end,
        )
    if preset == "zero_cost":
        return qs.filter(Q(purchase_price__isnull=True) | Q(purchase_price=0))
    if preset == "shelf_life_expired":
        return qs.filter(expiration_date__isnull=False, expiration_date__lt=today)
    if preset == "out_of_stock":
        return qs.filter(Q(quantity__isnull=True) | Q(quantity__lte=0))
    if preset == "negative_stock":
        return qs.filter(quantity__lt=0)
    if preset == "not_sold_90d":
        return _apply_not_sold_within(qs, 90)
    if preset == "low_stock":
        # «Заканчивается»: остаток ≤ своего минимума, а если минимум не задан — ≤ порога (по умолчанию 5).
        threshold = _parse_decimal(low_stock_threshold)
        if threshold is None or threshold < 0:
            threshold = LOW_STOCK_DEFAULT_THRESHOLD
        has_min = Q(minimum_quantity__isnull=False, minimum_quantity__gt=0)
        return qs.filter(
            (has_min & Q(quantity__lte=F("minimum_quantity")))
            | (~has_min & (Q(quantity__isnull=True) | Q(quantity__lte=threshold)))
        )
    if preset == "stock_below_min":
        return qs.filter(
            minimum_quantity__isnull=False,
            minimum_quantity__gt=0,
        ).filter(quantity__lt=F("minimum_quantity"))
    return qs


def _paid_sale_items_since(days: int):
    since = timezone.now() - timedelta(days=days)
    return SaleItem.objects.filter(
        product_id=OuterRef("pk"),
        sale__status=Sale.Status.PAID,
        sale__paid_at__gte=since,
    )


def _apply_not_sold_within(qs, days: int):
    return qs.exclude(Exists(_paid_sale_items_since(days)))


def _apply_sold_within(qs, days: int):
    return qs.filter(Exists(_paid_sale_items_since(days)))


MAX_BARCODES = 500


def parse_barcodes(query_params):
    """barcodes=a,b,c и/или barcodes[]=a&barcodes[]=b: без пробелов, без повторов, не больше MAX_BARCODES."""
    raw = []
    for key in ("barcodes", "barcodes[]"):
        for v in query_params.getlist(key) if hasattr(query_params, "getlist") else [query_params.get(key)]:
            raw.extend(str(v or "").split(","))
    out = list(dict.fromkeys(c.strip() for c in raw if c and c.strip()))
    if len(out) > MAX_BARCODES:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"barcodes": f"Не больше {MAX_BARCODES} штрихкодов за запрос."})
    return out


def apply_product_list_filters(qs, query_params):
    """Применяет query-параметры модалки фильтров склада к queryset Product."""
    kinds = _parse_kind_list(query_params)
    if kinds:
        qs = qs.filter(kind__in=kinds)

    category_id = (query_params.get("category") or "").strip()
    if category_id:
        qs = qs.filter(category_id=category_id)

    brand_id = (query_params.get("brand") or "").strip()
    if brand_id:
        qs = qs.filter(brand_id=brand_id)

    is_weight = _parse_bool(query_params.get("is_weight"))
    if is_weight is not None:
        qs = qs.filter(is_weight=is_weight)

    is_adult = _parse_bool(query_params.get("is_adult"))
    if is_adult is not None:
        qs = qs.filter(is_adult=is_adult)

    # Касса/бот: ?is_service=false&quantity_lte=5
    is_service = _parse_bool(query_params.get("is_service"))
    if is_service is True:
        qs = qs.filter(kind="service")
    elif is_service is False:
        qs = qs.exclude(kind="service")

    for param, lookup in (("quantity_lte", "quantity__lte"), ("quantity_gte", "quantity__gte")):
        raw = query_params.get(param)
        if raw not in (None, ""):
            value = _parse_decimal(raw)
            if value is None:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({param: "Число."})
            qs = qs.filter(**{lookup: value})

    # G4 (аудит 03.11): ?has_promotion=true — акционные (галочка «Акционный товар» и есть ступени скидки);
    # false — все остальные
    has_promotion = _parse_bool(query_params.get("has_promotion"))
    if has_promotion is not None:
        promo = Q(stock=True) & Exists(ProductPromotionTier.objects.filter(product=OuterRef("pk"), discount_percent__gt=0))
        qs = qs.filter(promo) if has_promotion else qs.exclude(promo)

    # G5 (аудит 03.11): ?piece_sale=true — продаются поштучно из упаковки (есть упаковки); у комплекта
    # (kind=bundle) packages — это состав, поэтому комплекты сюда не входят. false — без упаковок.
    piece_sale = _parse_bool(query_params.get("piece_sale"))
    if piece_sale is not None:
        packs = Exists(ProductPackage.objects.filter(product=OuterRef("pk"))) & ~Q(kind=Product.Kind.BUNDLE)
        qs = qs.filter(packs) if piece_sale else qs.exclude(packs)

    # G6 (аудит 03.11): ?barcodes=a,b,c — товары по списку штрихкодов одним запросом (основной или доп. код)
    codes = parse_barcodes(query_params)
    if codes:
        alt = ProductAlternateBarcode.objects.filter(product=OuterRef("pk"), barcode__in=codes)
        qs = qs.filter(Q(barcode__in=codes) | Exists(alt))

    preset = _normalize_preset(query_params.get("preset"))
    if preset:
        qs = _apply_preset(qs, preset, query_params.get("low_stock_threshold"))

    price_type = (query_params.get("price_type") or "").strip().lower()
    price_condition = (query_params.get("price_condition") or "eq").strip().lower()
    price_value = _parse_decimal(query_params.get("price_value"))
    if price_value is not None and price_value != 0:
        field = _price_field(price_type)
        if field and price_condition in ("gt", "lt", "eq"):
            qs = _compare_field(qs, field, price_condition, price_value)

    stock_type = (query_params.get("stock_type") or "").strip().lower()
    stock_condition = (query_params.get("stock_condition") or "eq").strip().lower()
    stock_value = _parse_decimal(query_params.get("stock_value"))
    if stock_type == "total" and stock_value is not None and stock_value != 0:
        if stock_condition in ("gt", "lt", "eq"):
            qs = _compare_field(qs, "quantity", stock_condition, stock_value)

    shelf_cond = (query_params.get("shelf_life_condition") or "").strip().lower()
    shelf_days = _parse_int(query_params.get("shelf_life_value"))
    today = timezone.localdate()
    expiring_only = _parse_bool(query_params.get("expiring_only"))
    expired_only = _parse_bool(query_params.get("expired_only"))

    if expired_only:
        qs = qs.filter(expiration_date__isnull=False, expiration_date__lt=today)
    elif expiring_only:
        qs = qs.filter(
            expiration_date__isnull=False,
            expiration_date__lte=today + timedelta(days=14),
        )
        if _parse_bool(query_params.get("exclude_expired")):
            qs = qs.filter(expiration_date__gte=today)
    elif shelf_cond == "expires_within" and shelf_days is not None and shelf_days >= 0:
        end = today + timedelta(days=shelf_days)
        qs = qs.filter(
            expiration_date__isnull=False,
            expiration_date__gte=today,
            expiration_date__lte=end,
        )
    elif shelf_cond == "expired":
        qs = qs.filter(expiration_date__isnull=False, expiration_date__lt=today)

    changes_cond = (query_params.get("changes_condition") or "eq").strip().lower()
    changes_days = _parse_int(query_params.get("changes_value"))
    if changes_days is not None and changes_days > 0:
        threshold = timezone.now() - timedelta(days=changes_days)
        if changes_cond == "gt":
            qs = qs.filter(updated_at__lt=threshold)
        elif changes_cond == "lt":
            qs = qs.filter(updated_at__gt=threshold)
        else:
            qs = qs.filter(updated_at__gte=threshold)

    sell_cond = (query_params.get("sellability_condition") or "").strip().lower()
    sell_days = _parse_int(query_params.get("sellability_value"))
    if sell_days is not None and sell_days > 0:
        if sell_cond == "sold_within":
            qs = _apply_sold_within(qs, sell_days)
        elif sell_cond == "not_sold_within":
            qs = _apply_not_sold_within(qs, sell_days)

    return qs
