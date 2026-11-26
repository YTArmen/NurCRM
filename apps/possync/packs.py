"""
Продажа упаковкой (ТЗ веб-кассы, п. 3): строка pos/checkout/ с `sale_package_id`.

В отличие от старого поля `sale_package` (поштучная продажа из пачки: qty в штуках, остаток в пачках),
`sale_package_id` — продажа упаковками: qty = число упаковок, цена = цена упаковки,
со склада уходит qty × quantity_in_package базовых единиц.

Строка сохраняется в базовых единицах: quantity = qty × ipp, цена за единицу = цена упаковки / ipp
(округляется вверх до копеек, разницу забирает скидка строки, чтобы итог строки был ровно
qty × цена упаковки). Поэтому списание остатка и возврат работают без изменений — возврат
отдаёт базовые единицы. «2 пачки по 20» хранится в possync.SalePackLine.
"""
from __future__ import annotations

from collections import OrderedDict
from decimal import ROUND_CEILING, Decimal

from rest_framework.exceptions import ValidationError

from apps.main.models import Product, ProductPackage, ProductVariant
from apps.main.pos_utils import money, qty3

from .models import SalePackLine

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def _ceil_cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_CEILING)


def convert_pack_items(company_id, items):
    """
    items — validated_data["items"]. Возвращает (items_для_базового_кода, adjustments, pack_info).
    adjustments: {(product_id, variant_id): добавка к скидке строки (копейки округления)}.
    Строки одного товара, если среди них есть упаковочная, сливаются в одну.
    """
    if not any(it.get("sale_package_id") for it in items):
        return items, {}, []

    groups = OrderedDict()  # (product, variant) -> {"lines": [...], "has_pack": bool}
    out_order = []  # порядок: ('plain', item) | ('group', key)
    for idx, it in enumerate(items):
        if it.get("custom") or it.get("sale_package"):
            if it.get("sale_package_id"):
                raise ValidationError({"items": {idx: "Нельзя указывать sale_package и sale_package_id вместе."}})
            out_order.append(("plain", it))
            continue
        key = (it["product"], it.get("variant"))
        if key not in groups:
            groups[key] = {"lines": [], "has_pack": False}
            out_order.append(("group", key))
        groups[key]["lines"].append((idx, it))
        if it.get("sale_package_id"):
            groups[key]["has_pack"] = True

    products = {p.id: p for p in Product.objects.filter(company_id=company_id, id__in=[k[0] for k in groups])}
    pkg_ids = [it["sale_package_id"] for g in groups.values() for _i, it in g["lines"] if it.get("sale_package_id")]
    pkgs = {p.id: p for p in ProductPackage.objects.filter(company_id=company_id, id__in=pkg_ids)}
    variants = {
        v.id: v
        for v in ProductVariant.objects.filter(
            company_id=company_id, id__in=[k[1] for k in groups if k[1]]
        )
    }

    result, adjustments, info = [], {}, []
    for kind, val in out_order:
        if kind == "plain":
            result.append(val)
            continue
        key = val
        g = groups[key]
        if not g["has_pack"]:
            result.extend(it for _i, it in g["lines"])
            continue
        product = products.get(key[0])
        if product is None:
            raise ValidationError({"items": {g["lines"][0][0]: "Товар не найден."}})
        variant = variants.get(key[1]) if key[1] else None
        gross = ZERO       # что должен заплатить покупатель до скидок
        user_discount = ZERO
        base_total = Decimal("0")
        wholesale = False
        for idx, it in g["lines"]:
            qty = qty3(it["qty"])
            user_discount += money(it.get("discount") or ZERO)
            wholesale = wholesale or bool(it.get("is_wholesale"))
            pid = it.get("sale_package_id")
            if pid:
                pkg = pkgs.get(pid)
                if pkg is None or pkg.product_id != product.id:
                    raise ValidationError({"items": {idx: "Упаковка не найдена."}})
                ipp = Decimal(str(pkg.quantity_in_package or 0))
                if ipp <= 0:
                    raise ValidationError({"items": {idx: "У упаковки не задано количество."}})
                unit_base_price = Decimal(str(variant.effective_price if variant else product.price or 0))
                pack_price = money(it["price"]) if it.get("price") is not None else money(unit_base_price * ipp)
                base_qty = qty3(qty * ipp)
                gross += money(pack_price * qty)
                base_total += base_qty
                info.append({
                    "product_id": product.id, "variant_id": variant.id if variant else None,
                    "package": pkg, "package_name": pkg.name, "packs": qty, "units_per_pack": ipp,
                    "pack_price": pack_price, "base_qty": base_qty,
                })
            else:
                price = money(it["price"]) if it.get("price") is not None else money(
                    Decimal(str(variant.effective_price if variant else product.price or 0))
                )
                gross += money(price * qty)
                base_total += qty
        unit = _ceil_cents(gross / base_total) if base_total else ZERO
        adj = money(unit * base_total - gross)
        merged = dict(g["lines"][0][1])
        merged.pop("sale_package_id", None)
        merged.update({"qty": base_total, "price": unit, "discount": user_discount, "is_wholesale": wholesale})
        result.append(merged)
        if adj > 0:
            adjustments[key] = adj
    return result, adjustments, info


def save_pack_lines(sale, pack_info):
    for p in pack_info:
        SalePackLine.objects.create(
            sale=sale, product_id=p["product_id"], variant_id=p["variant_id"], package=p["package"],
            package_name=p["package_name"], packs=p["packs"], units_per_pack=p["units_per_pack"],
            pack_price=p["pack_price"], base_qty=p["base_qty"],
        )


def annotate_items(sale_id, items):
    """Дописывает в items ответа sale_package_id, package_name, packs_qty, units_per_pack, pack_price."""
    lines = list(SalePackLine.objects.filter(sale_id=sale_id))
    if not lines:
        return
    by_key = {}
    for ln in lines:
        by_key.setdefault((str(ln.product_id), str(ln.variant_id) if ln.variant_id else None), []).append(ln)
    for it in items:
        lns = by_key.get((it.get("product"), it.get("variant")))
        if not lns:
            continue
        first = lns[0]
        it["sale_package_id"] = str(first.package_id) if first.package_id else None
        it["package_name"] = first.package_name
        it["packs_qty"] = str(sum((l.packs for l in lns), Decimal("0")))
        it["units_per_pack"] = str(first.units_per_pack)
        it["pack_price"] = str(first.pack_price)
