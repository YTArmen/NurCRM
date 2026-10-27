"""ТЗ 26.10, Б-53: единицы продажи и закупки (sale_units): запись, чтение, цена, штрихкод."""
from decimal import Decimal, ROUND_HALF_UP

from core.exceptions import ApiError

from .services import (
    Q4,
    UNIT_CODES,
    _dec,
    apply_quantity_fields,
    q3,
)

Q2 = Decimal("0.01")


def _fmt(d, places=2):
    return format(Decimal(str(d)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f")


def serialize_sale_unit(u, *, public=False) -> dict:
    row = {
        "id": str(u.id),
        "name": u.name,
        "unit_code": u.unit_code,
        "factor": _fmt(u.factor, 4),
        "barcode": u.barcode or None,
        "price": _fmt(u.price) if u.price is not None else None,
        "for_sale": bool(u.for_sale),
        "is_default_sale": bool(u.is_default_sale),
        "sort_order": u.sort_order,
    }
    if not public:
        row.update({
            "wholesale_price": _fmt(u.wholesale_price) if u.wholesale_price is not None else None,
            "for_receipt": bool(u.for_receipt),
            "is_default_receipt": bool(u.is_default_receipt),
        })
    return row


def serialize_sale_units(product, *, public=False) -> list:
    units = list(product.sale_units.all())  # в списках товаров идёт prefetch_related("sale_units")
    if public:
        units = [u for u in units if u.for_sale]
    return [serialize_sale_unit(u, public=public) for u in units]


def sale_unit_brief(u) -> dict:
    """Короткая карточка единицы для строки продажи: {id, name, factor}."""
    return {"id": str(u.id), "name": u.name, "factor": _fmt(u.factor, 4)}


def effective_unit_price(product, unit, *, wholesale=False) -> Decimal:
    """Цена одной единицы продажи: своя `price`, иначе factor x цена базовой (с учётом оптового режима)."""
    if wholesale and unit.wholesale_price is not None:
        return Decimal(str(unit.wholesale_price)).quantize(Q2, rounding=ROUND_HALF_UP)
    if not wholesale and unit.price is not None:
        return Decimal(str(unit.price)).quantize(Q2, rounding=ROUND_HALF_UP)
    base = product.price
    if wholesale and product.wholesale_price not in (None, 0):
        base = product.wholesale_price
    return (Decimal(str(base or 0)) * Decimal(str(unit.factor))).quantize(Q2, rounding=ROUND_HALF_UP)


def barcode_owner(company_id, barcode, *, exclude_product_id=None):
    """Кто уже занимает штрихкод в компании: ('product'|'alternate'|'variant'|'sale_unit', объект) или None."""
    from apps.main.models import Product, ProductAlternateBarcode, ProductSaleUnit, ProductVariant

    b = (barcode or "").strip()
    if not b:
        return None
    for kind, qs, pid in (
        ("product", Product.objects.filter(company_id=company_id, barcode=b), "pk"),
        ("alternate", ProductAlternateBarcode.objects.filter(company_id=company_id, barcode=b), "product_id"),
        ("variant", ProductVariant.objects.filter(company_id=company_id, barcode=b), "product_id"),
        ("sale_unit", ProductSaleUnit.objects.filter(product__company_id=company_id, barcode=b), "product_id"),
    ):
        if exclude_product_id:
            qs = qs.exclude(**{pid: exclude_product_id})
        obj = qs.first()
        if obj:
            return kind, obj
    return None


def find_sale_unit_by_barcode(company_id, barcode):
    """Единица продажи по её штрихкоду (для скана) или None."""
    from apps.main.models import ProductSaleUnit

    b = (barcode or "").strip()
    if not b:
        return None
    return (
        ProductSaleUnit.objects.select_related("product")
        .filter(product__company_id=company_id, barcode=b, for_sale=True)
        .first()
    )


def _bad(field, msg):
    return ApiError("validation_error", msg, fields={field: [msg]}, top={field: [msg]})


def _receipt_uses(unit) -> bool:
    """Была ли единица в приходе поставщика (подпункт 5; пока приходов с единицей нет)."""
    try:
        from .receipts import unit_in_receipts

        return unit_in_receipts(unit)
    except ImportError:
        return False


def sync_sale_units(product, raw):
    """Б-53 п.2: `sale_units_input` — полная замена списка единиц товара.

    Строка с `id` обновляет существующую единицу, без `id` создаёт новую. Единицы, которых нет в списке,
    удаляются; если единица лежит в открытой корзине — 409 `sale_unit_in_use`; если её уже продавали или
    приходовали — не удаляется, а выключается (for_sale/for_receipt=false, штрихкод освобождается),
    чтобы история сохранила коэффициент. Штрихкод уникален в компании вместе с barcode и
    alternate_barcodes — 409 `barcode_taken`.
    """
    from apps.main.models import CartItem, ProductSaleUnit, SaleItem

    if raw is None:
        return
    if not isinstance(raw, (list, tuple)):
        raise _bad("sale_units_input", "Ожидается массив единиц продажи.")
    existing = {str(u.id): u for u in ProductSaleUnit.objects.filter(product=product)}
    keep, seen_codes, prepared = set(), set(), []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise _bad("sale_units_input", f"Единица №{i + 1}: ожидается объект.")
        name = str(row.get("name") or "").strip()
        if not name:
            raise _bad("sale_units_input", f"Единица №{i + 1}: укажите name.")
        if row.get("factor") in (None, ""):
            raise _bad("sale_units_input", f"Единица «{name}»: укажите factor.")
        factor = _dec(row.get("factor"), "factor").quantize(Q4, rounding=ROUND_HALF_UP)
        if factor <= 0:
            raise _bad("sale_units_input", f"Единица «{name}»: factor должен быть больше нуля.")
        code = str(row.get("unit_code") or "").strip()
        if code and code not in UNIT_CODES:
            raise _bad("sale_units_input", f"Единица «{name}»: неизвестный unit_code.")
        barcode = str(row.get("barcode") or "").strip()
        if barcode:
            if barcode in seen_codes:
                raise _bad("sale_units_input", f"Штрихкод «{barcode}» повторяется в списке.")
            seen_codes.add(barcode)
            if (
                barcode == (product.barcode or "")
                or product.alternate_barcodes.filter(barcode=barcode).exists()
                or barcode_owner(product.company_id, barcode, exclude_product_id=product.pk)
            ):
                raise ApiError("barcode_taken", f"Штрихкод «{barcode}» уже занят.", status_code=409,
                               data={"barcode": barcode})
        prices = {}
        for f in ("price", "wholesale_price"):
            v = row.get(f)
            if v in (None, ""):
                prices[f] = None
                continue
            d = _dec(v, f)
            if d < 0:
                raise _bad("sale_units_input", f"Единица «{name}»: {f} не может быть отрицательной.")
            prices[f] = d.quantize(Q2, rounding=ROUND_HALF_UP)
        uid = str(row.get("id") or "")
        if uid:
            if uid not in existing:
                raise _bad("sale_units_input", f"Единица {uid} не принадлежит этому товару.")
            keep.add(uid)
        so = row.get("sort_order")
        prepared.append((uid, {
            "name": name[:64], "unit_code": code, "factor": factor, "barcode": barcode, **prices,
            "for_sale": bool(row.get("for_sale", True)),
            "for_receipt": bool(row.get("for_receipt", True)),
            "is_default_sale": bool(row.get("is_default_sale", False)),
            "is_default_receipt": bool(row.get("is_default_receipt", False)),
            "sort_order": int(so) if so not in (None, "") else i,
        }))
    if (sum(d["is_default_sale"] for _u, d in prepared) > 1
            or sum(d["is_default_receipt"] for _u, d in prepared) > 1):
        raise _bad("sale_units_input", "По умолчанию может быть только одна единица (для продажи и для прихода отдельно).")

    gone = [u for uid, u in existing.items() if uid not in keep]
    for u in gone:
        if CartItem.objects.filter(sale_unit=u).exists():
            raise ApiError("sale_unit_in_use", f"Единица «{u.name}» лежит в открытой корзине.", status_code=409,
                           data={"sale_unit": str(u.id)})
    for u in gone:
        if SaleItem.objects.filter(sale_unit=u).exists() or _receipt_uses(u):
            ProductSaleUnit.objects.filter(pk=u.pk).update(
                for_sale=False, for_receipt=False, is_default_sale=False, is_default_receipt=False, barcode="")
        else:
            u.delete()
    for uid, data in prepared:
        if uid:
            ProductSaleUnit.objects.filter(pk=uid).update(**data)
        else:
            ProductSaleUnit.objects.create(product=product, **data)
    cache = getattr(product, "_prefetched_objects_cache", None)
    if cache:
        cache.pop("sale_units", None)


def apply_product_extras(product, data, *, units_raw=None):
    """Крючок создания/правки товара: поля Б-52 и `sale_units_input` Б-53."""
    apply_quantity_fields(product, data)
    if units_raw is None and hasattr(data, "get") and "sale_units_input" in data:
        units_raw = data.get("sale_units_input")
    if units_raw is not None:
        sync_sale_units(product, units_raw)
