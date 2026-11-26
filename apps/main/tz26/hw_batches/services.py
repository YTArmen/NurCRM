"""Б-61: логика партий и отрезных товаров.

Крючки в чужом коде (каждый — 1–3 строки):
  * приход поставщика  -> record_receipt_batches(...)
  * pos checkout       -> plan_checkout(cart, request.data) до checkout_cart,
                          commit_checkout(plan, sale, user) после
Если ни у одной строки нет партии и нет отрезных товаров — функции ничего не делают.
"""
import uuid
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import F

from core.exceptions import ApiError

from .models import (
    CartItemBatch, ProductBatch, ProductBatchReceiptLine, ProductCutSettings, ProductRemnant,
    ProductRoll, SaleItemBatch,
)

ZERO = Decimal("0")
LABEL_MAX = 32


def q3(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- партии: приход

def normalize_label(raw, index=None):
    """Метка партии: None если пусто, иначе обрезанная строка; длиннее 32 — ошибка."""
    if raw is None:
        return None
    label = str(raw).strip()
    if not label:
        return None
    if len(label) > LABEL_MAX:
        raise ApiError(
            "batch_label_too_long", f"batch_label — не длиннее {LABEL_MAX} символов.",
            fields={f"items[{index}].batch_label" if index is not None else "batch_label": ["Слишком длинная метка."]},
            top={"errors": [{"index": index, "code": "batch_label_too_long"}]},
        )
    return label


def record_receipt_batches(company, raw_items, receipt_items):
    """Крючок прихода поставщика. raw_items — items из тела запроса, receipt_items — созданные
    SupplierReceiptItem в том же порядке. Метка есть -> остаток партии растёт на qty строки."""
    if not isinstance(raw_items, (list, tuple)):
        return
    for idx, (raw, ri) in enumerate(zip(raw_items, receipt_items)):
        if not isinstance(raw, dict) or "batch_label" not in raw:
            continue
        label = normalize_label(raw.get("batch_label"), idx)
        if label is None:
            continue
        qty = q3(ri.qty)
        batch, _ = ProductBatch.objects.select_for_update().get_or_create(
            product_id=ri.product_id, label=label, defaults={"company": company})
        ProductBatch.objects.filter(pk=batch.pk).update(
            remaining=F("remaining") + qty, received_total=F("received_total") + qty)
        ProductBatchReceiptLine.objects.create(batch=batch, receipt_item=ri, quantity=qty)


# ---------------------------------------------------------------- партии: выбор и предупреждения

def resolve_batch(company_id, product_id, ref, index=None, lock=False):
    """ref — uuid партии или её метка. Возвращает ProductBatch или бросает batch_not_found."""
    qs = ProductBatch.objects.filter(company_id=company_id, product_id=product_id)
    if lock:
        qs = qs.select_for_update()
    batch = None
    s = str(ref).strip()
    try:
        batch = qs.filter(pk=uuid.UUID(s)).first()
    except ValueError:
        pass
    if batch is None:
        batch = qs.filter(label=s).first()
    if batch is None:
        raise ApiError(
            "batch_not_found", "Партия не найдена у этого товара.",
            top={"errors": [{"index": index, "code": "batch_not_found", "batch": s}]},
        )
    return batch


def mixed_batches_warnings(lines):
    """lines: [{'product': id, 'batch_id': id|None, 'batch_label': str|None}] ->
    warnings: если у одного товара в чеке разные партии."""
    by_product = defaultdict(dict)
    for ln in lines:
        if ln.get("batch_id"):
            by_product[str(ln["product"])][str(ln["batch_id"])] = ln.get("batch_label") or ""
    out = []
    for pid, batches in by_product.items():
        if len(batches) > 1:
            out.append({
                "code": "mixed_batches", "product": pid,
                "batches": [{"id": b, "label": l} for b, l in batches.items()],
                "detail": "В чеке один товар из разных партий (оттенок/калибр могут отличаться).",
            })
    return out


def cart_lines(cart):
    """Строки корзины с выбранной партией (для quote)."""
    from apps.main.models import CartItem
    items = list(CartItem.objects.filter(cart=cart).exclude(product__isnull=True))
    links = {
        l.cart_item_id: l.batch
        for l in CartItemBatch.objects.filter(cart_item__in=[i.pk for i in items]).select_related("batch")
    }
    out = []
    for it in items:
        b = links.get(it.pk)
        out.append({
            "cart_item": str(it.pk), "product": it.product_id, "variant": it.variant_id,
            "quantity": it.quantity, "batch_id": b.pk if b else None, "batch_label": b.label if b else None,
        })
    return out


def set_cart_item_batch(cart_item, ref):
    """Привязать/снять (ref=None) партию строки корзины."""
    if ref in (None, ""):
        CartItemBatch.objects.filter(cart_item=cart_item).delete()
        return None
    if not cart_item.product_id:
        raise ApiError("batch_product_mismatch", "У своей позиции нет партии.")
    batch = resolve_batch(cart_item.company_id, cart_item.product_id, ref)
    CartItemBatch.objects.update_or_create(cart_item=cart_item, defaults={"batch": batch})
    return batch


# ---------------------------------------------------------------- checkout

class CheckoutPlan:
    def __init__(self):
        self.lines = []         # dict(product, variant, quantity, unit_price, batch)
        self.cut_products = {}  # product_id -> ProductCutSettings

    def __bool__(self):
        return bool(self.lines)


def plan_checkout(cart, raw_data=None):
    """Крючок ДО checkout_cart: проверяет партии и отрезы. None — ничего не нужно делать."""
    from apps.main.models import CartItem

    items = list(CartItem.objects.filter(cart=cart))
    if not items:
        return None
    pids = {i.product_id for i in items if i.product_id}
    cuts = {c.product_id: c for c in ProductCutSettings.objects.filter(product_id__in=pids)} if pids else {}
    links = {
        l.cart_item_id: l.batch_id
        for l in CartItemBatch.objects.filter(cart_item__in=[i.pk for i in items])
    }
    # batch в строках тела pos/checkout/: сопоставляем с позициями корзины по (товар, вариант) по порядку
    raw_refs = []
    raw_items = raw_data.get("items") if hasattr(raw_data, "get") else None
    if isinstance(raw_items, list):
        for idx, r in enumerate(raw_items):
            if isinstance(r, dict) and r.get("batch") not in (None, "") and r.get("product"):
                raw_refs.append((idx, str(r["product"]), str(r["variant"]) if r.get("variant") else None, r["batch"]))
    if not links and not raw_refs and not cuts:
        return None

    plan = CheckoutPlan()
    plan.cut_products = cuts
    used = set()
    explicit = {}
    for idx, pid, vid, ref in raw_refs:
        for it in items:
            if it.pk in used or str(it.product_id) != pid or (str(it.variant_id) if it.variant_id else None) != vid:
                continue
            used.add(it.pk)
            explicit[it.pk] = (idx, ref)
            break

    need = defaultdict(lambda: ZERO)
    batches = {}
    for it in items:
        if not it.product_id:
            continue
        batch = None
        if it.pk in explicit:
            idx, ref = explicit[it.pk]
            batch = resolve_batch(cart.company_id, it.product_id, ref, index=idx, lock=True)
        elif it.pk in links:
            batch = ProductBatch.objects.select_for_update().filter(pk=links[it.pk], product_id=it.product_id).first()
        if batch is not None:
            batches[batch.pk] = batch
            need[batch.pk] += Decimal(str(it.quantity))
        cut = cuts.get(it.product_id)
        if cut is not None and cut.min_cut and Decimal(str(it.quantity)) < cut.min_cut:
            raise ApiError(
                "below_min_cut", f"Минимальный отрез — {cut.min_cut}.",
                data={"product": str(it.product_id), "min_cut": str(cut.min_cut)},
                top={"errors": [{"index": None, "code": "below_min_cut", "product_id": str(it.product_id),
                                 "min_cut": str(cut.min_cut)}]},
            )
        plan.lines.append({
            "product": it.product_id, "variant": it.variant_id, "quantity": it.quantity,
            "unit_price": it.unit_price, "batch": batch,
        })
    for bid, n in need.items():
        if n > batches[bid].remaining:
            raise ApiError(
                "batch_insufficient", f"В партии «{batches[bid].label}» осталось {batches[bid].remaining}.",
                data={"batch": str(bid), "available": str(batches[bid].remaining), "requested": str(n)},
                top={"errors": [{"index": None, "code": "batch_insufficient", "batch": str(bid),
                                 "available": str(batches[bid].remaining), "requested": str(n)}]},
            )
    if not batches and not cuts:
        return None
    return plan


def commit_checkout(plan, sale, user):
    """Крючок ПОСЛЕ checkout_cart (внутри той же транзакции): списывает партии и бухты."""
    if not plan:
        return
    from apps.main.models import SaleItem

    sale_items = list(SaleItem.objects.filter(sale=sale))
    taken = set()

    def match(line):
        for strict in (True, False):
            for si in sale_items:
                if si.pk in taken or si.product_id != line["product"]:
                    continue
                if strict and (si.variant_id != line["variant"] or si.quantity != line["quantity"]
                               or si.unit_price != line["unit_price"]):
                    continue
                taken.add(si.pk)
                return si
        return None

    cut_qty = defaultdict(lambda: ZERO)
    for line in plan.lines:
        si = match(line)
        qty = q3(line["quantity"])
        batch = line["batch"]
        if batch is not None and si is not None:
            ProductBatch.objects.filter(pk=batch.pk).update(remaining=F("remaining") - qty)
            SaleItemBatch.objects.create(sale_item=si, batch=batch, quantity=qty)
        if line["product"] in plan.cut_products:
            cut_qty[line["product"]] += qty
    for pid, qty in cut_qty.items():
        consume_cut(plan.cut_products[pid], qty, user, sale)


# ---------------------------------------------------------------- отрезной товар

def _new_rolls(cut, product, amount):
    """Бухты на объём amount: полные по roll_length и неполная."""
    full = int(amount // cut.roll_length)
    rest = amount - cut.roll_length * full
    rolls = [ProductRoll(company_id=cut.company_id, product=product, length=cut.roll_length,
                         remaining=cut.roll_length) for _ in range(full)]
    if rest > 0:
        rolls.append(ProductRoll(company_id=cut.company_id, product=product, length=cut.roll_length,
                                 remaining=rest))
    ProductRoll.objects.bulk_create(rolls)


def ensure_rolls(cut, product, stock_total):
    """Сверяет бухты с остатком товара: недостающее до stock_total достраивает новыми бухтами."""
    active = ProductRoll.objects.select_for_update().filter(product=product, status=ProductRoll.Status.ACTIVE)
    have = sum((r.remaining for r in active), ZERO)
    diff = q3(stock_total) - have
    if diff > 0:
        _new_rolls(cut, product, diff)


def consume_cut(cut, qty, user, sale=None):
    """Продажа отреза qty: уменьшает текущую бухту; остаток меньше порога -> товар-обрезок."""
    from django.utils import timezone
    from apps.main.models import Product

    product = Product.objects.select_for_update().get(pk=cut.product_id)
    # остаток товара checkout уже уменьшил; до продажи было на qty больше
    ensure_rolls(cut, product, Decimal(str(product.quantity or 0)) + qty)
    rolls = list(ProductRoll.objects.select_for_update().filter(product=product, status=ProductRoll.Status.ACTIVE))
    rolls.sort(key=lambda r: (r.remaining, r.opened_at))
    need = qty
    touched = []
    for r in rolls:
        if need <= 0:
            break
        take = min(r.remaining, need)
        r.remaining = q3(r.remaining - take)
        need -= take
        touched.append(r)
    for r in touched:
        if r.remaining <= 0:
            r.status = ProductRoll.Status.CONSUMED
            r.closed_at = timezone.now()
            r.save(update_fields=["remaining", "status", "closed_at"])
        else:
            r.save(update_fields=["remaining"])
            if cut.remnant_threshold and r.remaining < cut.remnant_threshold:
                make_remnant(cut, product, r, user, sale)


def make_remnant(cut, product, roll, user, sale=None):
    """Остаток бухты -> товар-обрезок со скидкой; движение склада remnant, без денег."""
    from django.utils import timezone
    from apps.main.models import Product, record_stock_movement

    rem = roll.remaining
    price = (Decimal(str(product.price or 0)) * (Decimal("100") - cut.remnant_discount_percent) / Decimal("100")
             ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    unit = getattr(product, "unit", "") or ""
    name = f"{product.name} (обрезок {rem.normalize():f} {unit})".strip()[:255]
    child = Product.objects.create(
        company_id=product.company_id, branch=product.branch, name=name, category=product.category,
        brand=product.brand, kind=product.kind, unit=unit, unit_code=product.unit_code,
        is_weight=product.is_weight, purchase_price=product.purchase_price, price=price, quantity=rem,
        status=product.status, created_by=user,
    )
    ProductRemnant.objects.create(
        company_id=product.company_id, product=child, remnant_of=product, roll=roll, length=rem,
        discount_percent=cut.remnant_discount_percent)
    roll.status = ProductRoll.Status.REMNANT
    roll.remaining = ZERO
    roll.closed_at = timezone.now()
    roll.save(update_fields=["remaining", "status", "closed_at"])

    before = Decimal(str(product.quantity or 0))
    Product.objects.filter(pk=product.pk).update(quantity=F("quantity") - rem)
    product.quantity = before - rem
    comment = "Остаток бухты переведён в обрезок"
    ref = dict(ref_type="sale" if sale is not None else "remnant", ref_id=getattr(sale, "id", None) or child.id)
    record_stock_movement(
        company=product.company, type="remnant", object_id=product.pk, created_by=user,
        qty_before=before, change=-rem, qty_after=before - rem, branch=product.branch,
        product_name=product.name, warehouse="finished_goods", comment=comment, **ref)
    record_stock_movement(
        company=product.company, type="remnant", object_id=child.pk, created_by=user,
        qty_before=ZERO, change=rem, qty_after=rem, branch=product.branch,
        product_name=child.name, warehouse="finished_goods", comment=comment, **ref)
    return child
