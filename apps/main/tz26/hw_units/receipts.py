"""ТЗ 26.10, Б-53 п.5: приход и заказ поставщику в единицах закупки (sale_unit_id в строке).

Остаток и закупочная цена хранятся в базовых единицах товара: количество строки умножается на factor,
цена строки делится на factor. Строка прихода хранит целое количество (SupplierReceiptItem.qty), поэтому
единица, дающая дробное базовое количество, в приход не принимается (400 receipt_qty_not_integer).
"""
from decimal import Decimal, ROUND_HALF_UP

from core.exceptions import ApiError

Q3 = Decimal("0.001")


def _unit_for(product, unit_id, *, what):
    from apps.main.models import ProductSaleUnit

    try:
        unit = ProductSaleUnit.objects.filter(pk=int(unit_id), product_id=product.id).first()
    except (TypeError, ValueError):
        unit = None
    if unit is None or not unit.for_receipt:
        raise ApiError("sale_unit_not_found", f"{what}: единица закупки не найдена или не разрешена к приходу.",
                       status_code=404)
    return unit


def convert_receipt_items(items):
    """Крючок прихода поставщика: строки с sale_unit_id переводятся в базовые единицы (на месте)."""
    for i, it in enumerate(items):
        uid = it.get("sale_unit_id")
        if uid in (None, ""):
            continue
        unit = _unit_for(it["product"], uid, what=f"Строка {i + 1}")
        base = Decimal(str(it["qty"])) * Decimal(str(unit.factor))
        if base != base.to_integral_value():
            raise ApiError("receipt_qty_not_integer",
                           f"Строка {i + 1}: {it['qty']} x {unit.factor} даёт дробное количество, приход хранит целое.",
                           fields={"items": [f"Строка {i + 1}: дробное количество в базовых единицах."]})
        it["unit_qty"] = it["qty"]
        it["qty"] = int(base)
        for f in ("purchase_price",):
            if it.get(f) is not None:
                it[f] = (Decimal(str(it[f])) / Decimal(str(unit.factor))).quantize(Q3, rounding=ROUND_HALF_UP)
        # price / selling_price (розничная цена) относятся к базовой единице товара и не пересчитываются


def convert_order_lines(lines):
    """Крючок заказа поставщику: строки с sale_unit_id -> базовые единицы. Возвращает новый список."""
    out = []
    for i, ln in enumerate(lines):
        uid = ln.get("sale_unit_id")
        if uid in (None, ""):
            out.append(ln)
            continue
        unit = _unit_for(ln["product"], uid, what=f"Строка {i + 1}")
        factor = Decimal(str(unit.factor))
        ln = dict(ln)
        ln["qty"] = (Decimal(str(ln["qty"])) * factor).quantize(Q3, rounding=ROUND_HALF_UP)
        if ln.get("purchase_price") is not None:
            ln["purchase_price"] = (Decimal(str(ln["purchase_price"])) / factor).quantize(Q3, rounding=ROUND_HALF_UP)
        out.append(ln)
    return out


def default_receipt_pack_sizes(product_ids) -> dict:
    """{product_id: factor} для единиц с is_default_receipt и factor > 1: рекомендуемый заказ кратен им."""
    from apps.main.models import ProductSaleUnit

    out = {}
    for u in ProductSaleUnit.objects.filter(product_id__in=list(product_ids), is_default_receipt=True,
                                            for_receipt=True, factor__gt=1):
        out.setdefault(u.product_id, float(u.factor))
    return out


def unit_in_receipts(unit) -> bool:
    """Приход не хранит ссылку на единицу (в строке только базовое количество), поэтому всегда False."""
    return False
