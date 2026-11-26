"""П0-4 К-20 (14.11.2026, k20_1411): проверки ввода товара (создание и правка карточки). Проверяются только изменённые штрихкод и цены.

Строгие ошибки — только новым клиентам (X-NurMarket-Client kassa/owner ≥ 1.17.61, android-pos ≥ 1.0, mobile ≥ 1.18.5,
site ≥ 1.0): у старых касс и сайта без заголовка всё как раньше.
- 409 barcode_duplicate {product_id, name, code, source: product|alternate} — штрихкод занят другим товаром
  (в том же складе/филиале) или как дополнительный; для всех клиентов, раньше тут было 400/500;
- 400 barcode_checksum — EAN-8/EAN-13/UPC-A с неверной контрольной цифрой (кроме весовых/внутренних 20–29);
  провести: allow_invalid_barcode=true;
- 400 price_zero — цена продажи 0 у товара (не услуги); провести: allow_zero_price=true;
- 400 price_below_cost — цена продажи ниже закупочной; провести: allow_below_cost=true (владелец/админ или
  право can_sell_below_cost).
"""
from decimal import Decimal, InvalidOperation

from core.exceptions import ApiError


def _truthy(v):
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


def ean_ok(code: str) -> bool:
    """Контрольная цифра EAN-8 / UPC-A (12) / EAN-13. Иные длины и не цифры — не проверяем."""
    if not code.isdigit() or len(code) not in (8, 12, 13):
        return True
    if len(code) == 13 and code[:1] == "2":  # весовые и внутренние коды магазина (20–29)
        return True
    body, check = code[:-1], int(code[-1])
    total = 0
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == check


def _dec(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _barcode_owner(company_id, branch_id, barcode, exclude_pk):
    from apps.main.models import Product, ProductAlternateBarcode

    qs = Product.objects.filter(company_id=company_id, barcode=barcode)
    qs = qs.filter(branch_id=branch_id) if branch_id else qs.filter(branch__isnull=True)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    p = qs.only("id", "name", "code").first()
    if p is not None:
        return p, "product"
    aqs = ProductAlternateBarcode.objects.filter(company_id=company_id, barcode=barcode).select_related("product")
    if exclude_pk:
        aqs = aqs.exclude(product_id=exclude_pk)
    a = aqs.first()
    return (a.product, "alternate") if a is not None else (None, None)


def check_product(serializer, attrs):
    from core.api_compat import is_new_client

    request = serializer.context.get("request")
    data = getattr(request, "data", None) if request is not None else None
    data = data if hasattr(data, "get") else {}
    inst = serializer.instance
    new_client = bool(request is not None and is_new_client(request))

    barcode = attrs.get("barcode") if "barcode" in attrs else None
    if barcode and inst is not None and str(barcode).strip() == (inst.barcode or ""):
        barcode = None  # штрихкод не меняли — старые данные не блокируют правку карточки
    if barcode:
        b = str(barcode).strip()
        company = getattr(inst, "company", None) or serializer._user_company()
        if company is not None:
            if inst is not None:
                branch_id = inst.branch_id
            else:
                try:
                    br = serializer._auto_branch()
                except Exception:  # noqa: BLE001
                    br = None
                branch_id = getattr(br, "id", None)
            owner, src = _barcode_owner(company.id, branch_id, b, getattr(inst, "pk", None))
            if owner is not None:
                raise ApiError("barcode_duplicate", f"Штрихкод «{b}» уже есть у товара «{owner.name}».", status_code=409,
                               data={"product_id": str(owner.id), "name": owner.name, "code": owner.code or None,
                                     "source": src, "barcode": b},
                               fields={"barcode": [f"Уже есть у товара «{owner.name}»."]})
        if new_client and not ean_ok(b) and not _truthy(data.get("allow_invalid_barcode")):
            raise ApiError("barcode_checksum", f"У штрихкода «{b}» неверная контрольная цифра.",
                           data={"barcode": b, "confirm": "allow_invalid_barcode=true"},
                           fields={"barcode": ["Неверная контрольная цифра."]})

    if not new_client:
        return
    kind = attrs.get("kind", getattr(inst, "kind", None))
    if kind == "service":
        return
    if "price" not in attrs and "purchase_price" not in attrs:
        return
    price = _dec(attrs.get("price", getattr(inst, "price", None)))
    cost = _dec(attrs.get("purchase_price", getattr(inst, "purchase_price", None)))
    if inst is not None and price == _dec(inst.price) and cost == _dec(inst.purchase_price):
        return  # цены не меняли — не мешаем править остальное
    if "price" in attrs and price is not None and price == 0 and not _truthy(data.get("allow_zero_price")):
        raise ApiError("price_zero", "Цена продажи 0. Укажите цену или передайте allow_zero_price=true.",
                       data={"confirm": "allow_zero_price=true"}, fields={"price": ["Цена 0."]})
    if price is not None and cost is not None and price > 0 and price < cost:
        if _truthy(data.get("allow_below_cost")):
            from apps.main.pos_serializers import may_sell_below_cost

            if may_sell_below_cost(getattr(request, "user", None)):
                return
            raise ApiError("permission_denied", "Цену ниже закупки ставит владелец или сотрудник с правом.",
                           status_code=403, data={"permission": "can_sell_below_cost"})
        raise ApiError("price_below_cost", f"Цена продажи {price} ниже закупочной {cost}.",
                       data={"price": str(price), "purchase_price": str(cost), "confirm": "allow_below_cost=true"},
                       fields={"price": ["Ниже закупочной."]})
