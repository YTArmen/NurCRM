"""
ТЗ 26.10, Б-55 и Б-56: уровни цен, цены товара по уровням и объёму, расчёт цены строки, лимит долга, объекты клиента.

ПОРЯДОК ЦЕН (ответ на вопрос 4 к разделу 8 ТЗ). Цена строки считается только под флагом `is_hardware_strict`
(компания с `hardware_2026_11` + новая касса) и только для строки, которую кассир не правил руками:

1. Ручная цена кассира всегда побеждает. Ручной опт тоже: строка с `is_wholesale`, корзина в режиме опта, цена,
   равная `wholesale_price`, или любая другая цена, не равная розничной, — это «ручная» строка, её не трогаем
   (`price_source` = `wholesale` или `manual`). Уровень клиента и объём такую цену не меняют.
2. Иначе берётся цена УРОВНЯ клиента: `retail` — Product.price; `wholesale` — Product.wholesale_price
   (если 0 — розница); свой уровень — ProductLevelPrice (если у товара для него цены нет — розница).
   У клиента без уровня — розница.
3. Затем объёмные цены: подходят ступени с `min_qty` <= общее количество товара в чеке (все строки товара суммируются,
   в базовых единицах), у которых `level` пустой (для всех) или равен уровню клиента. Берём «лучшую цену»:
   МИНИМУМ из цены уровня и подходящих объёмных. Объёмная цена никогда не поднимает цену выше уровня.
   `price_source` = `volume`, если победила объёмная; иначе `retail` | `wholesale` | `level:<code>`.
4. Скидки идут ПОСЛЕ цены и считаются от неё: акция `promotion_rules` (discount_source=promotion), ручная скидка
   строки и скидка чека — это поля скидок строки/чека, а не цена, они ничего не заменяют и не отменяются нашим
   расчётом. `max_discount_percent` проверяется кассой/сервером как раньше — от цены строки, которая здесь получилась.
`base_price` строки = розничная цена товара до пересчёта (то, что показала бы старая касса).
Строки без товара, с вариантом, упаковкой (sale_package) или единицей продажи (sale_unit) не пересчитываются.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.exceptions import ApiError

from .models import ClientObject, DealObject, PriceLevel, ProductLevelPrice, ProductVolumePrice

Q2 = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_CUSTOM_LEVELS = 5
CLIENT_KINDS = ("person", "foreman", "team", "company")
SYSTEM_LEVELS = (("retail", "Розница", 0), ("wholesale", "Опт", 1))
_CODE_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def q2(x) -> Decimal:
    return Decimal(str(x if x is not None else 0)).quantize(Q2, rounding=ROUND_HALF_UP)


def parse_money(value, field, *, allow_null=False) -> Optional[Decimal]:
    if value in (None, ""):
        if allow_null:
            return None
        raise ValidationError({field: "Обязательное поле."})
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError({field: "Число."})
    if d < 0:
        raise ValidationError({field: "Не может быть отрицательным."})
    return q2(d)


# ---------------------------------------------------------------- права / компания

def company_of(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def is_owner_like(user) -> bool:
    from apps.main.pos_serializers import _is_owner_like

    return _is_owner_like(user)


def can_edit_prices(user) -> bool:
    """Справочник и цены — владелец и can_view_products."""
    return is_owner_like(user) or bool(getattr(user, "can_view_products", False))


def can_edit_clients(user) -> bool:
    return is_owner_like(user) or bool(getattr(user, "can_view_clients", False))


def can_override_credit_limit(user) -> bool:
    return is_owner_like(user) or bool(getattr(user, "can_override_credit_limit", False))


def deny(required: str, title: str):
    return ApiError("permission_denied", f"Нет доступа: {title}", status_code=403, top={"required": required})


# ---------------------------------------------------------------- Б-55: справочник уровней

def ensure_system_levels(company) -> dict:
    """Создаёт retail/wholesale лениво; возвращает {code: PriceLevel}."""
    have = {lv.code: lv for lv in PriceLevel.objects.filter(company=company, code__in=[c for c, _, _ in SYSTEM_LEVELS])}
    for code, name, order in SYSTEM_LEVELS:
        if code not in have:
            have[code], _ = PriceLevel.objects.get_or_create(
                company=company, code=code, defaults={"name": name, "sort_order": order, "is_system": True}
            )
    return have


def level_payload(lv: PriceLevel) -> dict:
    return {"id": lv.id, "code": lv.code, "name": lv.name, "sort_order": lv.sort_order, "is_system": lv.is_system}


_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    "a b v g d e e zh z i y k l m n o p r s t u f h c ch sh sch _ y _ e yu ya".split(),
))


def _slug_code(name: str) -> str:
    out = "".join(_TRANSLIT.get(ch, ch) for ch in name.lower())
    out = re.sub(r"[^a-z0-9]+", "_", out).strip("_")
    return out[:32] or "level"


def create_level(company, data) -> PriceLevel:
    ensure_system_levels(company)
    name = (data.get("name") or "").strip()
    if not name:
        raise ValidationError({"name": "Укажите название."})
    if len(name) > 64:
        raise ValidationError({"name": "Не длиннее 64 символов."})
    if PriceLevel.objects.filter(company=company, is_system=False).count() >= MAX_CUSTOM_LEVELS:
        raise ApiError("price_levels_limit", f"Своих уровней цен не больше {MAX_CUSTOM_LEVELS}.", status_code=400)
    code = (data.get("code") or "").strip().lower() or _slug_code(name)
    if not _CODE_RE.match(code):
        raise ValidationError({"code": "Латиница, цифры и «_», до 32 символов."})
    if PriceLevel.objects.filter(company=company, code=code).exists():
        raise ApiError("price_level_exists", "Уровень с таким кодом уже есть.", status_code=400, fields={"code": ["Занято."]})
    order = data.get("sort_order")
    if order in (None, ""):
        order = (PriceLevel.objects.filter(company=company).order_by("-sort_order").values_list("sort_order", flat=True).first() or 0) + 1
    return PriceLevel.objects.create(company=company, code=code, name=name, sort_order=int(order), is_system=False)


def update_level(level: PriceLevel, data) -> PriceLevel:
    if "code" in data and (data["code"] or "").strip().lower() != level.code:
        raise ValidationError({"code": "Код уровня менять нельзя."})
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name or len(name) > 64:
            raise ValidationError({"name": "От 1 до 64 символов."})
        level.name = name
    if "sort_order" in data and data["sort_order"] not in (None, ""):
        try:
            level.sort_order = max(0, int(data["sort_order"]))
        except (TypeError, ValueError):
            raise ValidationError({"sort_order": "Целое число."})
    level.save()
    return level


# ---------------------------------------------------------------- Б-55: цены товара

def _level_ref(raw, levels_by_code, levels_by_id):
    """level из тела: код (строка) или id (число)."""
    if isinstance(raw, bool) or raw in (None, ""):
        return None
    if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
        return levels_by_id.get(int(raw))
    return levels_by_code.get(str(raw).strip().lower())


def _qty_str(d: Decimal) -> str:
    d = Decimal(d)
    return str(int(d)) if d == d.to_integral_value() else format(d.normalize(), "f")


def product_prices_payload(product) -> dict:
    """{"prices": [...], "volume_prices": [...]}. retail/wholesale — зеркало Product.price / wholesale_price."""
    company = product.company
    ensure_system_levels(company)
    custom_levels = list(PriceLevel.objects.filter(company=company, is_system=False))
    prices = [{"level": "retail", "price": str(q2(product.price))}]
    wp = product.wholesale_price
    prices.append({"level": "wholesale", "price": str(q2(wp)) if wp not in (None, 0, Decimal("0")) else None})
    custom = {lp.level_id: lp for lp in product.level_prices.all()}
    code_by_id = {lv.id: lv.code for lv in PriceLevel.objects.filter(company=company)}
    for lv in custom_levels:
        if lv.id in custom:
            prices.append({"level": lv.code, "price": str(q2(custom[lv.id].price))})
    volume = [
        {
            "min_qty": _qty_str(vp.min_qty),
            "price": str(q2(vp.price)),
            "level": code_by_id.get(vp.level_id) if vp.level_id else None,
        }
        for vp in product.volume_prices.all()
    ]
    return {"prices": prices, "volume_prices": volume}


@transaction.atomic
def save_product_prices(product, prices=None, volume_prices=None):
    """
    prices: список {level, price}. retail/wholesale пишутся в Product.price / wholesale_price, остальные — в
    ProductLevelPrice; свой уровень, которого нет в списке, удаляется. price=null у своего уровня тоже удаляет цену.
    volume_prices: полная замена списка ступеней. Ключ не передан (None) — не трогаем.
    """
    from apps.main.models import Product

    company = product.company
    ensure_system_levels(company)
    levels = list(PriceLevel.objects.filter(company=company))
    by_code = {lv.code: lv for lv in levels}
    by_id = {lv.id: lv for lv in levels}
    changed = False

    if prices is not None:
        if not isinstance(prices, list):
            raise ValidationError({"prices": "Список."})
        wanted = {}
        update = {}
        for i, row in enumerate(prices):
            if not isinstance(row, dict):
                raise ValidationError({"prices": {i: "Объект {level, price}."}})
            lv = _level_ref(row.get("level"), by_code, by_id)
            if lv is None:
                raise ValidationError({"prices": {i: "Неизвестный уровень цен."}})
            price = parse_money(row.get("price"), f"prices[{i}].price", allow_null=True)
            if lv.code == "retail":
                if price is None:
                    raise ValidationError({"prices": {i: "Розничная цена обязательна."}})
                update["price"] = price
            elif lv.code == "wholesale":
                update["wholesale_price"] = price if price is not None else Decimal("0.00")
            elif price is not None:
                wanted[lv.id] = price
        if update:
            Product.objects.filter(pk=product.pk).update(**update)
            for k, v in update.items():
                setattr(product, k, v)
        ProductLevelPrice.objects.filter(product=product).exclude(level_id__in=list(wanted)).delete()
        for lid, price in wanted.items():
            ProductLevelPrice.objects.update_or_create(product=product, level_id=lid, defaults={"price": price})
        changed = True

    if volume_prices is not None:
        if not isinstance(volume_prices, list):
            raise ValidationError({"volume_prices": "Список."})
        rows, seen = [], set()
        for i, row in enumerate(volume_prices):
            if not isinstance(row, dict):
                raise ValidationError({"volume_prices": {i: "Объект {min_qty, price}."}})
            try:
                min_qty = Decimal(str(row.get("min_qty")))
            except (InvalidOperation, ValueError):
                raise ValidationError({"volume_prices": {i: "min_qty — число."}})
            if min_qty <= 0:
                raise ValidationError({"volume_prices": {i: "min_qty должен быть больше нуля."}})
            price = parse_money(row.get("price"), f"volume_prices[{i}].price")
            lv = None
            if row.get("level") not in (None, ""):
                lv = _level_ref(row.get("level"), by_code, by_id)
                if lv is None:
                    raise ValidationError({"volume_prices": {i: "Неизвестный уровень цен."}})
            key = (lv.id if lv else None, min_qty.quantize(Decimal("0.001")))
            if key in seen:
                raise ValidationError({"volume_prices": {i: "Такая ступень уже есть."}})
            seen.add(key)
            rows.append(ProductVolumePrice(product=product, level=lv, min_qty=min_qty, price=price))
        ProductVolumePrice.objects.filter(product=product).delete()
        ProductVolumePrice.objects.bulk_create(rows)
        changed = True

    if changed:
        # касса сверяет каталог по updated_at — подталкиваем
        Product.objects.filter(pk=product.pk).update(updated_at=timezone.now())
        cache = getattr(product, "_prefetched_objects_cache", None)
        if cache:
            cache.pop("level_prices", None)
            cache.pop("volume_prices", None)


def product_serializer_save(serializer, super_save, **kwargs):
    """Крючок для ProductSerializer.save(): prices / volume_prices не попадают в Product(**data)."""
    vd = getattr(serializer, "_validated_data", None)
    prices = vp = None
    has_prices = has_vp = False
    if isinstance(vd, dict):
        if "prices" in vd:
            has_prices, prices = True, vd.pop("prices")
        if "volume_prices" in vd:
            has_vp, vp = True, vd.pop("volume_prices")
    with transaction.atomic():
        if has_prices or has_vp:
            req = serializer.context.get("request")
            user = getattr(req, "user", None)
            if user is not None and not can_edit_prices(user):
                raise deny("can_view_products", "Товары")
        instance = super_save(**kwargs)
        if has_prices or has_vp:
            save_product_prices(instance, prices if has_prices else None, vp if has_vp else None)
    return instance


# ---------------------------------------------------------------- Б-55: расчёт цены строки

class PriceContext:
    """Цены товаров чека для одного клиента (или без клиента). Данные читаются пакетом."""

    def __init__(self, company, client=None):
        self.company = company
        self.client = client
        self.level = getattr(client, "price_level", None) if client is not None else None
        if self.level is not None and self.level.company_id != company.id:
            self.level = None
        self._level_prices = {}
        self._volume = {}

    def prefetch(self, product_ids):
        ids = list(product_ids)
        if not ids:
            return
        if self.level is not None and not self.level.is_system:
            for lp in ProductLevelPrice.objects.filter(product_id__in=ids, level=self.level):
                self._level_prices[lp.product_id] = lp.price
        lvl_id = self.level.id if self.level is not None else None
        for vp in ProductVolumePrice.objects.filter(product_id__in=ids):
            if vp.level_id is None or vp.level_id == lvl_id:
                self._volume.setdefault(vp.product_id, []).append((vp.min_qty, q2(vp.price)))

    def resolve(self, product, qty_total) -> tuple:
        """-> (цена, price_source, base_price) для строки, которую кассир не правил."""
        retail = q2(product.price)
        code = self.level.code if self.level is not None else "retail"
        if code == "retail":
            price, source = retail, "retail"
        elif code == "wholesale":
            wp = q2(product.wholesale_price) if product.wholesale_price else ZERO
            price, source = (wp, "wholesale") if wp > 0 else (retail, "retail")
        else:
            lp = self._level_prices.get(product.id)
            price, source = (q2(lp), f"level:{code}") if lp is not None else (retail, "retail")
        qty = Decimal(str(qty_total or 0))
        level_price = price
        for min_qty, vprice in self._volume.get(product.id, ()):
            if qty >= min_qty and vprice < price and vprice < level_price:
                price, source = vprice, "volume"
        return price, source, retail


def _line_qty(it) -> Decimal:
    bq = getattr(it, "base_quantity", None)
    return Decimal(str(bq if bq is not None else it.quantity or 0))


def reprice_cart(cart, client, request=None) -> bool:
    """
    Пересчитывает цены строк корзины по уровню клиента и объёму (порядок — в docstring модуля).
    Работает ТОЛЬКО под флагом hardware_2026_11 + новая касса (is_hardware_strict). True — если считали.
    """
    from apps.main.models import CartItem
    from core.api_compat import is_hardware_strict

    company = cart.company
    if not is_hardware_strict(request, company):
        return False
    items = list(cart.items.select_related("product"))
    ctx = PriceContext(company, client)
    ctx.prefetch({it.product_id for it in items if it.product_id})
    totals = {}
    for it in items:
        if it.product_id:
            totals[it.product_id] = totals.get(it.product_id, Decimal("0")) + _line_qty(it)
    changed = []
    info = {}
    cart_opt = bool(getattr(cart, "is_wholesale", False))
    for it in items:
        p = it.product
        if p is None or it.variant_id or it.sale_package_id or getattr(it, "sale_unit_id", None):
            continue
        unit = q2(it.unit_price)
        retail = q2(p.price)
        wholesale = q2(p.wholesale_price) if p.wholesale_price else ZERO
        res_price, res_src, base = ctx.resolve(p, totals.get(p.id))
        computed_before = it.price_source == "retail" or it.price_source == "volume" or it.price_source.startswith("level:")
        if it.is_wholesale or cart_opt:
            price, src = unit, "wholesale"  # ручной опт важнее уровня клиента
        elif unit == res_price:
            price, src = res_price, res_src  # касса сама посчитала так же, как сервер
        elif (unit == retail and not it.price_manually_edited) or (computed_before and not it.price_manually_edited):
            price, src = res_price, res_src
        else:
            price, src = unit, ("wholesale" if (wholesale > 0 and unit == wholesale) else "manual")
        info[(it.product_id, it.variant_id, it.sale_package_id)] = (src, base)
        if (unit, it.price_source, it.base_price) != (price, src, base):
            it.unit_price, it.price_source, it.base_price = price, src, base
            changed.append(it)
    # корзина после продажи удаляется — источник цены строк запоминаем на запросе (см. copy_price_info_to_sale)
    try:
        request._hw_price_info = info
    except AttributeError:
        pass
    if changed:
        CartItem.objects.bulk_update(changed, ["unit_price", "price_source", "base_price"])
        cart.recalc()
    return True


def reprice_for_request(request, cart, client_id) -> bool:
    """Крючок кассы: клиент по id (своей компании) -> reprice_cart. Без флага/новой кассы ничего не делает."""
    from core.api_compat import is_hardware_strict

    if not is_hardware_strict(request, cart.company):
        return False
    client = None
    if client_id:
        from apps.main.models import Client

        client = Client.objects.select_related("price_level").filter(pk=client_id, company_id=cart.company_id).first()
    return reprice_cart(cart, client, request)


def copy_price_info_to_sale(request, sale) -> None:
    """price_source/base_price строк (запомнены при пересчёте на запросе) -> строки продажи."""
    from apps.main.models import SaleItem

    for (pid, vid, pkg), (src, base) in (getattr(request, "_hw_price_info", None) or {}).items():
        if pid:
            SaleItem.objects.filter(sale=sale, product_id=pid, variant_id=vid, sale_package_id=pkg).update(
                price_source=src, base_price=base
            )


def quote_lines(company, client, lines) -> list:
    """Расчёт цен без чека: lines=[{product, qty, price?}] -> [{product, qty, price, price_source, base_price}]."""
    from apps.main.models import Product

    ctx = PriceContext(company, client)
    try:
        ids = [str(ln.get("product")) for ln in lines]
        products = {str(p.id): p for p in Product.objects.filter(company=company, id__in=ids)}
    except Exception:
        raise ValidationError({"items": "Некорректный id товара."})
    ctx.prefetch([p.id for p in products.values()])
    totals = {}
    for ln in lines:
        try:
            q = Decimal(str(ln.get("qty") or ln.get("quantity") or 0))
        except InvalidOperation:
            raise ValidationError({"items": "qty — число."})
        totals[str(ln.get("product"))] = totals.get(str(ln.get("product")), Decimal("0")) + q
    out = []
    for i, ln in enumerate(lines):
        p = products.get(str(ln.get("product")))
        if p is None:
            raise ApiError("product_not_found", "Товар не найден.", status_code=404, data={"index": i})
        qty = Decimal(str(ln.get("qty") or ln.get("quantity") or 0))
        retail = q2(p.price)
        given = parse_money(ln.get("price"), "price", allow_null=True)
        if given is not None and given != retail:
            wp = q2(p.wholesale_price) if p.wholesale_price else ZERO
            res = (given, "wholesale" if (wp > 0 and given == wp) else "manual", retail)
        else:
            res = ctx.resolve(p, totals[str(p.id)])
        out.append({"product": str(p.id), "qty": str(qty), "price": str(res[0]), "price_source": res[1], "base_price": str(res[2])})
    return out


# ---------------------------------------------------------------- Б-56: объекты клиента

def object_payload(o: ClientObject) -> dict:
    return {"id": o.id, "name": o.name, "address": o.address, "is_active": o.is_active}


def save_object(obj: ClientObject, data) -> ClientObject:
    if "name" in data or obj.pk is None:
        name = (data.get("name") or "").strip()
        if not name or len(name) > 128:
            raise ValidationError({"name": "От 1 до 128 символов."})
        obj.name = name
    if "address" in data:
        obj.address = (data.get("address") or "").strip()[:255]
    if "is_active" in data:
        obj.is_active = bool(data["is_active"])
    obj.save()
    return obj


def resolve_object(client, raw):
    """object из запроса (id) -> ClientObject этого клиента или None; чужой/неизвестный — 404 object_not_found."""
    if raw in (None, "", 0):
        return None
    try:
        oid = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({"object": "Нужен id объекта."})
    obj = ClientObject.objects.filter(pk=oid, client=client).first()
    if obj is None:
        raise ApiError("object_not_found", "Объект не найден у этого клиента.", status_code=404, fields={"object": ["Не найден."]})
    return obj


def client_fields_check(serializer, attrs) -> None:
    """Крючок для ClientSerializer.validate(): права и компания для новых полей клиента."""
    req = serializer.context.get("request")
    user = getattr(req, "user", None)
    inst = getattr(serializer, "instance", None)
    if user is None:
        return
    if attrs.get("client_kind") not in (None, "") and attrs["client_kind"] not in CLIENT_KINDS:
        raise ValidationError({"client_kind": f"Допустимо: {', '.join(CLIENT_KINDS)}."})
    if "credit_limit" in attrs:
        cur = getattr(inst, "credit_limit", None)
        if attrs["credit_limit"] != cur and not is_owner_like(user):
            raise deny("owner", "Лимит долга клиента (только владелец)")
        if attrs["credit_limit"] is not None and attrs["credit_limit"] < 0:
            raise ValidationError({"credit_limit": "Не может быть отрицательным."})
    if "price_level" in attrs:
        lv = attrs["price_level"]
        cur = getattr(inst, "price_level_id", None)
        if (lv.id if lv else None) != cur:
            if not can_edit_clients(user):
                raise deny("can_view_clients", "Клиенты")
            company = company_of(user)
            if lv is not None and company is not None and lv.company_id != company.id:
                raise ValidationError({"price_level": "Уровень цен другой компании."})


# ---------------------------------------------------------------- Б-56: лимит долга

def check_credit_limit(request, client, requested, *, exclude_sale_id=None) -> None:
    """
    409 credit_limit_exceeded (data: limit, debt, requested) — только под флагом хозмага (is_hardware_strict) и
    только если у клиента задан credit_limit. Владелец (или право can_override_credit_limit) проводит
    долг с `override_credit_limit: true`. Клиент блокируется строкой (select_for_update): два чека подряд
    ждут друг друга. Исключение откатывает всю продажу.
    """
    from apps.main.models import Client
    from core.api_compat import is_hardware_strict

    if client is None or requested is None or Decimal(str(requested)) <= 0:
        return
    company = company_of(request.user)
    if not is_hardware_strict(request, company):
        return
    locked = Client.objects.select_for_update().get(pk=client.pk)
    limit = locked.credit_limit
    if limit is None:
        return
    from apps.possync.debt_limits import client_current_debt

    debt = client_current_debt(locked, exclude_sale_id=exclude_sale_id)
    requested = q2(requested)
    if debt + requested <= q2(limit):
        return
    override = request.data.get("override_credit_limit") if hasattr(request, "data") else None
    if override in (True, "true", "True", "1", 1):
        if not can_override_credit_limit(request.user):
            raise deny("can_override_credit_limit", "Превышение лимита долга")
        return
    raise ApiError(
        "credit_limit_exceeded",
        f"Лимит долга клиента {q2(limit)} сом, текущий долг {q2(debt)} сом, запрошено {requested} сом.",
        status_code=409,
        data={"limit": str(q2(limit)), "debt": str(q2(debt)), "requested": str(requested)},
    )


def after_checkout(request, cart, sale, client) -> None:
    """
    Крючок SaleCheckoutAPIView.checkout() после создания продажи (срабатывает и для pos/checkout/):
    price_source/base_price строк, объект продажи, лимит долга, срок долга по умолчанию.
    """
    from core.api_compat import is_hardware_strict

    copy_price_info_to_sale(request, sale)
    if client is None or sale.client_id != client.id:
        return
    data = getattr(request, "data", None)
    raw_obj = data.get("object") if hasattr(data, "get") else None
    obj = resolve_object(client, raw_obj)
    if obj is not None:
        type(sale).objects.filter(pk=sale.pk).update(client_object=obj)
        sale.client_object = obj
    debt = sale.debt_initial or ZERO
    if debt > 0:
        check_credit_limit(request, client, debt, exclude_sale_id=sale.pk)
        if client.default_due_days and is_hardware_strict(request, cart.company):
            _apply_default_due_days(sale, client.default_due_days)


def _apply_default_due_days(sale, days) -> None:
    from apps.main.models import ClientDeal, DealInstallment

    due = timezone.localdate() + timedelta(days=int(days))
    for deal in ClientDeal.objects.filter(sale=sale, kind=ClientDeal.Kind.DEBT):
        ClientDeal.objects.filter(pk=deal.pk).update(first_due_date=due)
        DealInstallment.objects.filter(deal=deal).update(due_date=due)


def default_due_date(request, client):
    """Срок прямого долга по умолчанию: default_due_days клиента (под флагом) либо 30 дней, как раньше."""
    from core.api_compat import is_hardware_strict

    days = 30
    if client.default_due_days and is_hardware_strict(request, company_of(request.user)):
        days = int(client.default_due_days)
    return timezone.localdate() + timedelta(days=days)


def link_direct_debt(request, client, deal) -> Optional[ClientObject]:
    data = getattr(request, "data", None)
    obj = resolve_object(client, data.get("object") if hasattr(data, "get") else None)
    if obj is not None:
        DealObject.objects.update_or_create(deal=deal, defaults={"client_object": obj})
    return obj


# ---------------------------------------------------------------- Б-56: должники по объектам

def debts_by_objects(client_ids, company_id) -> dict:
    """{client_id: [{object, name, address, debt_total}]} — открытый долг клиента по объектам (None — без объекта)."""
    from django.db.models import DecimalField, OuterRef, Subquery, Sum, Value
    from django.db.models.functions import Coalesce

    from apps.main.models import ClientDeal, DealInstallment, Sale

    money_field = DecimalField(max_digits=14, decimal_places=2)
    paid_sq = DealInstallment.objects.filter(deal=OuterRef("pk")).values("deal").annotate(s=Sum("paid_amount")).values("s")
    deals = (
        ClientDeal.objects.filter(company_id=company_id, kind=ClientDeal.Kind.DEBT, client_id__in=client_ids)
        .exclude(sale__status=Sale.Status.PAID)
        .annotate(paid=Coalesce(Subquery(paid_sq, output_field=money_field), Value(ZERO), output_field=money_field))
        .select_related("sale", "hw_object_link")
    )
    acc = {}

    def add(cid, oid, amount):
        per = acc.setdefault(cid, {})
        per[oid] = per.get(oid, ZERO) + amount

    for d in deals:
        remaining = q2((d.amount or ZERO) - (d.prepayment or ZERO) - (d.paid or ZERO))
        if remaining <= 0:
            continue
        oid = d.sale.client_object_id if d.sale_id and d.sale else None
        if oid is None:
            link = getattr(d, "hw_object_link", None)
            oid = link.client_object_id if link else None
        add(d.client_id, oid, remaining)
    for s in Sale.objects.filter(company_id=company_id, status=Sale.Status.DEBT, client_id__in=client_ids, deals__isnull=True):
        remaining = q2((s.total or ZERO) - (s.cash_received or ZERO))
        if remaining > 0:
            add(s.client_id, s.client_object_id, remaining)
    names = {o.id: o for o in ClientObject.objects.filter(client_id__in=client_ids)}
    out = {}
    for cid, per in acc.items():
        rows = []
        for oid, amount in sorted(per.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
            o = names.get(oid)
            rows.append({
                "object": oid,
                "name": o.name if o else None,
                "address": o.address if o else None,
                "debt_total": str(q2(amount)),
            })
        out[cid] = rows
    return out


def attach_debtor_objects(rows, company_id) -> None:
    """Крючок ClientDebtorsAPIView: ?include=objects -> row['objects']."""
    import uuid as _uuid

    by_client = debts_by_objects([_uuid.UUID(r["client"]) for r in rows], company_id)
    for r in rows:
        r["objects"] = by_client.get(_uuid.UUID(r["client"]), [])


def enrich_sale_response(sale, data) -> None:
    """Крючок _enrich ответа pos/checkout/: object и price_source/base_price строк."""
    data["object"] = sale.client_object_id
    items = data.get("items")
    if isinstance(items, list) and items:
        from apps.main.models import SaleItem

        info = {
            str(i): (ps, bp)
            for i, ps, bp in SaleItem.objects.filter(sale=sale).values_list("id", "price_source", "base_price")
        }
        for it in items:
            ps, bp = info.get(str(it.get("id")), ("", None))
            it["price_source"] = ps or None
            it["base_price"] = str(bp) if bp is not None else None
