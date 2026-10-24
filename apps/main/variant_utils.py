"""
Общие помощники для вариантов товара (размер/цвет): порядок размеров,
цена/акция варианта, подпись «Товар — 32, синий», резерв/возврат остатка.

Используются ботом на сервере (каталог ИИ, заказы) и онлайн-витриной.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Iterable, List, Optional, Tuple

_LETTER_SIZES = ["XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "4XL", "5XL", "6XL"]
_LETTER_INDEX = {s: i for i, s in enumerate(_LETTER_SIZES)}
_LETTER_ALIASES = {"2XS": "XXS", "3XS": "XXXS", "2XL": "XXL", "3XL": "XXXL"}
_NUM_RE = re.compile(r"^\d+([.,]\d+)?")
_W_RE = re.compile(r"^W(\d+)(?:[/L\d]*)$", re.I)  # джинсовые W26…W36


def size_sort_key(size) -> tuple:
    """
    ТЗ Н-26. Порядок размеров по умолчанию: буквенные XXS…5XL по порядку, затем числовые
    по возрастанию, затем W26…W36 (по числу), затем прочие по алфавиту, пустой размер — в конце.
    """
    s = str(size or "").strip()
    if not s:
        return (4, 0, "")
    up = s.upper().replace(" ", "")
    up = _LETTER_ALIASES.get(up, up)
    if up in _LETTER_INDEX:
        return (0, _LETTER_INDEX[up], "")
    m = _NUM_RE.match(s)
    if m:
        try:
            return (1, float(m.group(0).replace(",", ".")), s.lower())
        except ValueError:
            pass
    mw = _W_RE.match(s.replace(" ", ""))
    if mw:
        return (2, int(mw.group(1)), s.lower())
    return (3, 0, s.lower())


def variant_sort_key(variant) -> tuple:
    """ТЗ Н-26: ручной sort_order (если задан, не 0) важнее; при равных — порядок размеров, затем цвет."""
    so = getattr(variant, "sort_order", 0) or 0
    return (so,) + size_sort_key(getattr(variant, "size", "")) + (str(getattr(variant, "color", "") or "").lower(),)


def sort_variants(variants: Iterable) -> list:
    return sorted(variants, key=variant_sort_key)


def active_variants(product) -> list:
    """Активные варианты товара в порядке размеров (использует prefetch, если он есть)."""
    return sort_variants(v for v in product.variants.all() if getattr(v, "is_active", True))


def variant_prices(variant, product) -> Tuple[Decimal, Optional[Decimal]]:
    """
    (цена_к_оплате, обычная_цена_если_акция).
    Акционная цена варианта — его собственная цена, если она ниже цены товара (как в кассе).
    """
    base = Decimal(str(product.price or 0))
    vp = getattr(variant, "price", None)
    if vp is None:
        return base, None
    vp = Decimal(str(vp))
    if vp < base:
        return vp, base
    return vp, None


def variant_label(variant) -> str:
    if not variant:
        return ""
    return ", ".join(p for p in [str(variant.size or "").strip(), str(variant.color or "").strip()] if p)


def name_with_variant(product_name: str, variant) -> str:
    label = variant_label(variant)
    return f"{product_name} — {label}" if label else product_name


# ----------------------------------------------------------------------
# Резерв/возврат остатка (как в кассе: вариант и товар списываются вместе)
# ----------------------------------------------------------------------

class InsufficientStock(Exception):
    pass


def reserve_stock(lines: List[dict], *, check_variants: bool = True, check_products: bool = False) -> None:
    """
    Списывает остаток по строкам [{"product": Product, "variant": ProductVariant|None, "qty": Decimal}].
    Вызывать внутри transaction.atomic(). Блокирует строки select_for_update.

    check_variants — нельзя заказать вариант сверх остатка (на витрине вариант без остатка не выбрать).
    check_products — то же для товаров без вариантов. По умолчанию выключено: заказы витрины
    на товары без вариантов принимались и при нулевом остатке (многие магазины не ведут остаток).
    """
    from apps.main.models import Product, ProductVariant

    # Резервируются только строки с вариантом (размер/цвет, ТЗ-07 4.2.4). Заказы на обычные
    # товары остаток не трогают, как и раньше: касса пробивает такой заказ обычной продажей
    # и списывает сама — иначе вышло бы двойное списание.
    lines = [ln for ln in lines if ln.get("variant") is not None]
    pids = {ln["product"].id for ln in lines if ln.get("product") is not None}
    vids = {ln["variant"].id for ln in lines}
    products = {p.id: p for p in Product.objects.select_for_update().filter(id__in=pids)}
    variants = {v.id: v for v in ProductVariant.objects.select_for_update().filter(id__in=vids)}

    need_p: dict = {}
    need_v: dict = {}
    for ln in lines:
        prod = ln.get("product")
        if prod is None:
            continue
        qty = Decimal(str(ln["qty"]))
        var = ln.get("variant")
        if var is not None:
            need_v[var.id] = need_v.get(var.id, Decimal("0")) + qty
        need_p[prod.id] = need_p.get(prod.id, Decimal("0")) + qty

    if check_variants:
        for vid, need in need_v.items():
            v = variants.get(vid)
            if v is None or not v.is_active:
                raise InsufficientStock("Вариант товара недоступен.")
            if need > Decimal(str(v.quantity or 0)):
                p = products.get(v.product_id)
                raise InsufficientStock(
                    f"Недостаточно остатка «{name_with_variant(p.name if p else '', v)}»: "
                    f"доступно {Decimal(str(v.quantity or 0)):g}."
                )
    if check_products:
        for pid, need in need_p.items():
            if pid in {ln["product"].id for ln in lines if ln.get("variant") is not None}:
                continue
            p = products[pid]
            if p.kind == Product.Kind.SERVICE:
                continue
            if need > Decimal(str(p.quantity or 0)):
                raise InsufficientStock(
                    f"Недостаточно остатка «{p.name}»: доступно {Decimal(str(p.quantity or 0)):g}."
                )

    _apply(products, variants, need_p, need_v, sign=-1)


def release_stock(lines: List[dict]) -> None:
    """Возвращает ранее зарезервированный остаток (отмена заказа)."""
    from apps.main.models import Product, ProductVariant

    pids = {ln["product"].id for ln in lines if ln.get("product") is not None}
    vids = {ln["variant"].id for ln in lines if ln.get("variant") is not None}
    products = {p.id: p for p in Product.objects.select_for_update().filter(id__in=pids)}
    variants = {v.id: v for v in ProductVariant.objects.select_for_update().filter(id__in=vids)}
    need_p: dict = {}
    need_v: dict = {}
    for ln in lines:
        prod = ln.get("product")
        if prod is None:
            continue
        qty = Decimal(str(ln["qty"]))
        var = ln.get("variant")
        if var is not None and var.id in variants:
            need_v[var.id] = need_v.get(var.id, Decimal("0")) + qty
        need_p[prod.id] = need_p.get(prod.id, Decimal("0")) + qty
    _apply(products, variants, need_p, need_v, sign=1)


def recalc_product_quantity(product_id) -> Optional[Decimal]:
    """
    ТЗ Н-27: у товара с вариантами остаток = сумма остатков АКТИВНЫХ вариантов (сервер пересчитывает сам).
    Если вариантов нет вообще — остаток не трогаем (возвращает None).
    """
    from django.db.models import Sum

    from apps.main.models import Product, ProductVariant

    qs = ProductVariant.objects.filter(product_id=product_id)
    if not qs.exists():
        return None
    total = qs.filter(is_active=True).aggregate(s=Sum("quantity"))["s"] or Decimal("0")
    Product.objects.filter(pk=product_id).update(quantity=total)
    return total


def notify_variants_in_stock(variant_ids) -> int:
    """
    ТЗ Н-27: у варианта появился остаток > 0 → заявки «сообщить о поступлении» помечаются ready.
    TODO: отправка покупателю (SMS/Telegram по phone / telegram_chat_id) — готового помощника для
    покупательских уведомлений в проекте нет; пока только notified_at/статус, владелец видит заявки
    в GET main/showcase/stock-requests/?status=ready.
    """
    from django.utils import timezone

    from apps.main.models import ShowcaseStockRequest, ProductVariant

    ids = [v for v in variant_ids if v]
    if not ids:
        return 0
    open_ids = list(
        ShowcaseStockRequest.objects.filter(variant_id__in=ids, status=ShowcaseStockRequest.Status.OPEN)
        .values_list("variant_id", flat=True).distinct()
    )
    if not open_ids:
        return 0
    in_stock = list(ProductVariant.objects.filter(id__in=open_ids, is_active=True, quantity__gt=0).values_list("id", flat=True))
    if not in_stock:
        return 0
    return ShowcaseStockRequest.objects.filter(variant_id__in=in_stock, status=ShowcaseStockRequest.Status.OPEN).update(
        status=ShowcaseStockRequest.Status.READY, notified_at=timezone.now()
    )


def _apply(products, variants, need_p, need_v, *, sign: int) -> None:
    from apps.main.models import Product, ProductVariant

    changed_v = []
    for vid, qty in need_v.items():
        v = variants[vid]
        v.quantity = Decimal(str(v.quantity or 0)) + sign * qty
        changed_v.append(v)
    if changed_v:
        ProductVariant.objects.bulk_update(changed_v, ["quantity"])
        if sign > 0:
            notify_variants_in_stock([v.id for v in changed_v])

    # ТЗ Н-27: товары с вариантами — остаток пересчитывается суммой активных вариантов
    recalculated = {v.product_id for v in changed_v}
    for pid in recalculated:
        p = products.get(pid)
        if p is None or p.kind == Product.Kind.SERVICE:
            continue
        total = recalc_product_quantity(pid)
        if total is not None:
            p.quantity = total

    changed_p = []
    for pid, qty in need_p.items():
        p = products.get(pid)
        if p is None or p.kind == Product.Kind.SERVICE:
            continue
        if pid in recalculated:
            changed_p.append(p)  # остаток уже пересчитан; ниже — только сброс кеша
            continue
        p.quantity = Decimal(str(p.quantity or 0)) + sign * qty
        changed_p.append(p)
    if changed_p:
        Product.objects.bulk_update([p for p in changed_p if p.id not in recalculated], ["quantity"])
        for p in changed_p:
            if getattr(p, "company_id", None):
                try:
                    from django.core.cache import cache
                    cache.delete(f"tg_catalog_data:{p.company_id}")
                except Exception:
                    pass


def order_stock_lines(order) -> List[dict]:
    """Строки резерва для ShowcaseOrder (по его позициям)."""
    lines = []
    for it in order.items.select_related("product", "variant"):
        if it.product_id is None or it.variant_id is None:
            continue
        lines.append({"product": it.product, "variant": it.variant, "qty": it.qty})
    return lines
