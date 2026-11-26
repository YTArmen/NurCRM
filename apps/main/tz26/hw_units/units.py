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


def sale_unit_scan_payload(unit) -> dict:
    """Добавка к ответу поиска по штрихкоду единицы: sale_unit, matched_by, quantity (= factor)."""
    return {
        "sale_unit": serialize_sale_unit(unit),
        "matched_by": "sale_unit_barcode",
        "quantity": _fmt(unit.factor, 3),
    }


# ---------------------------------------------------------------- Б-53 п.4: строка корзины с единицей продажи

def unit_factor(item) -> Decimal:
    """Коэффициент единицы строки корзины/продажи (1, если единицы нет)."""
    su = getattr(item, "sale_unit", None) if getattr(item, "sale_unit_id", None) else None
    return Decimal(str(su.factor)) if su is not None else Decimal(1)


def base_quantity_of(item) -> Decimal:
    """Сколько базовых единиц товара в строке: quantity x factor (строка с sale_unit)."""
    return q3(Decimal(str(getattr(item, "quantity", None) or 0)) * unit_factor(item))


def sale_unit_line_fields(item):
    """Для сериализаторов строки: (sale_unit {id,name,factor} | None, base_quantity | None)."""
    if not getattr(item, "sale_unit_id", None):
        return None, None
    return sale_unit_brief(item.sale_unit), _fmt(base_quantity_of(item), 3)


def get_sale_unit_for_line(obj):
    return sale_unit_line_fields(obj)[0]


def get_base_quantity_for_line(obj):
    return sale_unit_line_fields(obj)[1]


def _get_sale_unit(product, unit_id):
    from apps.main.models import ProductSaleUnit

    try:
        unit = ProductSaleUnit.objects.filter(pk=int(unit_id), product_id=product.id).first()
    except (TypeError, ValueError):
        unit = None
    if unit is None or not unit.for_sale:
        raise ApiError("sale_unit_not_found", "Единица продажи не найдена или не разрешена к продаже.", status_code=404)
    return unit


def upsert_sale_unit_item(request, cart, product, unit, qty, *, unit_price=None, discount_total=None,
                          discount_percent=None, can_minus=False):
    """Добавляет qty единиц продажи в корзину (слияние по товару и единице). Возвращает CartItem.

    quantity строки — в единицах продажи, unit_price — цена единицы; списание остатка — quantity x factor.
    Правило слияния: одна строка на (товар, единица продажи); та же пара суммируется, другая единица того
    же товара в той же корзине — 409 sale_unit_conflict (в БД одна строка товара без упаковки на корзину).
    """
    from django.db import transaction  # noqa: F401

    from apps.main.models import Cart, CartItem, Product
    from apps.main.pos_utils import total_cart_consume_packs_for_product
    from apps.main.pos_views import (
        _discount_limit_error,
        _line_discount_from_request,
        _line_discount_over_limit,
        _q2,
    )

    from .services import check_line_quantity

    qty = q3(qty)
    if qty <= 0:
        raise _bad("quantity", "Количество должно быть > 0.")
    factor = Decimal(str(unit.factor))
    cart = Cart.objects.select_for_update().get(id=cart.id)

    others = CartItem.objects.select_for_update().filter(
        cart=cart, product=product, sale_package__isnull=True, variant__isnull=True)
    target = None
    for ci in others:
        if ci.sale_unit_id == unit.id:
            target = ci
        else:
            raise ApiError("sale_unit_conflict",
                           "В корзине уже есть этот товар в другой единице. Уберите ту строку или продавайте в одной единице.",
                           status_code=409, data={"item_id": str(ci.id)})

    wholesale = bool(getattr(cart, "is_wholesale", False))
    if unit_price is not None:
        base_price = _q2(unit_price)
    else:
        base_price = effective_unit_price(product, unit, wholesale=wholesale)
    disc = _line_discount_from_request(base_price, qty, discount_total, discount_percent)

    user = request.user
    is_admin = getattr(user, "role", None) in ["owner", "admin"]
    max_dp = user.company.max_discount_percent
    if not is_admin and _line_discount_over_limit(base_price, qty, disc, max_dp):
        body = _discount_limit_error(max_dp)
        raise ApiError("discount_limit_exceeded", body.get("detail") or "Скидка больше разрешённой.")

    if disc <= 0:
        min_price = _q2(Decimal(str(product.purchase_price or 0)) * factor)
        if base_price < min_price:
            raise ApiError("price_below_cost", f"Цена продажи не может быть ниже закупочной ({min_price}).",
                           fields={"unit_price": [f"Цена продажи не может быть ниже закупочной ({min_price})."]},
                           top={"unit_price": f"Цена продажи не может быть ниже закупочной ({min_price})."})

    new_qty = q3((target.quantity if target else Decimal(0)) + qty)
    check_line_quantity(request, cart.company, product, new_qty, sale_unit=unit)

    other = total_cart_consume_packs_for_product(cart.id, product.id)
    if target:
        other = q3(other - base_quantity_of(target))
    need = q3(new_qty * factor)
    have = Decimal(str(product.quantity or 0))
    if product.kind != Product.Kind.SERVICE and not can_minus and q3(other + need) > have:
        raise ApiError("insufficient_stock",
                       f"Недостаточно остатка. Доступно не более {q3(max(Decimal(0), have - other))} (в базовых единицах).",
                       data={"available": str(q3(max(Decimal(0), have - other))), "requested": str(need)})

    if target:
        item = target
        item.quantity = new_qty
        update_f = ["quantity", "base_quantity"]
        if unit_price is not None:
            item.unit_price = base_price
            item.price_manually_edited = True
            update_f += ["unit_price", "price_manually_edited"]
        if discount_total is not None or discount_percent is not None:
            item.manual_discount = item.manual_discount_base() + disc
            item.line_discount = item.manual_discount
            update_f += ["line_discount", "manual_discount"]
        item.base_quantity = need
        item.save(update_fields=update_f, skip_full_clean=True)
    else:
        item = CartItem(
            cart=cart, company=cart.company, branch=getattr(cart, "branch", None), product=product,
            sale_unit=unit, quantity=new_qty, base_quantity=need, unit_price=base_price,
            line_discount=disc, manual_discount=disc,
            price_manually_edited=unit_price is not None,
        )
        item.save(skip_full_clean=True)
    cart.recalc()
    return item


def add_sale_unit_item_response(request, cart, product, data, *, can_minus=False):
    """Крючок add-item: строка с `sale_unit_id`. Ответ — корзина + added_item_id, как у обычной строки."""
    from rest_framework import status

    from apps.main.pos_views import _cart_response

    unit = _get_sale_unit(product, data.get("sale_unit_id"))
    item = upsert_sale_unit_item(
        request, cart, product, unit, data.get("quantity") or Decimal(1),
        unit_price=data.get("unit_price"), discount_total=data.get("discount_total"),
        discount_percent=data.get("discount_percent"), can_minus=can_minus,
    )
    resp = _cart_response(request, cart.id, status_code=status.HTTP_201_CREATED)
    if isinstance(resp.data, dict):
        resp.data["added_item_id"] = str(item.id)
        resp.data["added_item"] = {
            "id": str(item.id), "product": str(product.id), "sale_package": None,
            "sale_unit": sale_unit_brief(unit),
        }
    return resp


def scan_sale_unit_response(request, url_cart, unit, qty):
    """Крючок scan: штрихкод единицы продажи -> в корзину qty таких единиц (по умолчанию 1)."""
    from django.db import transaction
    from django.shortcuts import get_object_or_404
    from rest_framework import status

    from apps.main.models import Cart
    from apps.main.pos_views import (
        _cart_queryset_for_response,
        _lock_pos_target_cart,
        _pos_multi_cart_response,
    )

    product = unit.product
    sale_id = request.data.get("sale_id") if hasattr(request.data, "get") else None
    with transaction.atomic():
        cart = _lock_pos_target_cart(
            company=request.user.company, user=request.user, shift=url_cart.shift, sale_id=sale_id,
        )
        upsert_sale_unit_item(request, cart, product, unit, qty)
        cart_id = cart.id
    cart = get_object_or_404(_cart_queryset_for_response(), id=cart_id, company=request.user.company)
    resp = _pos_multi_cart_response(request, cart, status_code=status.HTTP_201_CREATED)
    if isinstance(resp.data, dict):
        resp.data.update(sale_unit_scan_payload(unit))
    return resp


def load_line_units(company_id, items) -> dict:
    """Крючок pos/checkout: {id: ProductSaleUnit} для строк с sale_unit / sale_unit_id."""
    from apps.main.models import ProductSaleUnit

    ids = {it.get("sale_unit") or it.get("sale_unit_id") for it in items if isinstance(it, dict)}
    ids.discard(None)
    if not ids:
        return {}
    return {u.id: u for u in ProductSaleUnit.objects.filter(product__company_id=company_id, id__in=ids)}


def pick_line_unit(it, product, pkg, unit_map, seen, idx):
    """Единица продажи строки pos/checkout (или None). Ошибки — по строке, с index.

    Один товар в одном чеке только в одной единице (иначе 409 sale_unit_conflict): строки сливать нельзя,
    а раздельные строки одного товара запрещены уникальным ограничением корзины.
    """
    from apps.main.kassa_views import _line_error

    uid = it.get("sale_unit") or it.get("sale_unit_id")
    prev = seen.get(product.id, "-")
    if uid is None:
        if prev not in ("-", None):
            raise _line_error("sale_unit_conflict", idx, "Товар уже есть в чеке в другой единице.", http=409)
        seen[product.id] = None
        return None
    unit = unit_map.get(uid)
    if unit is None or unit.product_id != product.id or not unit.for_sale:
        raise _line_error("sale_unit_not_found", idx, "Единица продажи не найдена.", http=404)
    if pkg is not None:
        raise _line_error("validation_error", idx, "Нельзя указывать sale_unit вместе с упаковкой.")
    if prev != "-" and prev != unit.id:
        raise _line_error("sale_unit_conflict", idx, "Товар уже есть в чеке в другой единице.", http=409)
    seen[product.id] = unit.id
    return unit


# ---------------------------------------------------------------- Б-53 п.7, п.8: витрина

def product_allows_fraction(product, unit=None) -> bool:
    """Можно ли заказывать дробное количество штучного товара на витрине (Б-52): задан шаг/знаки или единица."""
    if unit is not None:
        return True
    if getattr(product, "quantity_step", None) is not None:
        return True
    dec = getattr(product, "quantity_decimals", None)
    return dec is not None and int(dec) > 0


def order_line_unit(product, item, idx, variant_obj):
    """Единица продажи строки заказа витрины (или None). ShowcaseFieldError — как у соседних проверок."""
    from apps.main.showcase.design_schema import ShowcaseFieldError

    uid = item.get("sale_unit")
    if uid in (None, ""):
        return None
    from apps.main.models import ProductSaleUnit

    unit = ProductSaleUnit.objects.filter(pk=uid, product_id=product.id, for_sale=True).first()
    if unit is None:
        raise ShowcaseFieldError(f"items[{idx}].sale_unit", f"Единица продажи {uid} не найдена для товара «{product.name}».",
                                 "sale_unit_not_found", extra={"index": idx})
    if variant_obj is not None:
        raise ShowcaseFieldError(f"items[{idx}].sale_unit", "Единица продажи недоступна для товара с размерами/цветами.",
                                 "sale_unit_not_allowed", extra={"index": idx})
    return unit


def to_base_line(unit, qty, unit_price):
    """Строка заказа витрины хранится и резервируется в базовых единицах: (qty x factor, цена/factor, подпись)."""
    factor = Decimal(str(unit.factor))
    base_qty = q3(qty * factor)
    base_price = (Decimal(str(unit_price)) / factor).quantize(Q2, rounding=ROUND_HALF_UP)
    return base_qty, base_price, f" ({_fmt(qty, 3).rstrip('0').rstrip('.')} x {unit.name})"
