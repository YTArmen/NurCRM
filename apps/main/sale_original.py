"""ТЗ 31.10 п.3: исходный чек после возвратов — для ответа API, без изменения денег.

Возврат по-прежнему уменьшает SaleItem.quantity (строку при нуле удаляет), Sale.total
и строки оплаты — на это опираются выручка, смена, остатки, долги и повторный возврат.
Ответ чека (pos/sales/{id}/, список, публичный чек) показывает ИСХОДНЫЕ строки, итог
и оплаты, а возврат — отдельно (refunded_total, refunds[], returned_qty у строки).

Откуда берём исходный чек:
1) снимок `SaleReturn.sale_before` первого возврата (пишется с 31.10, перед изменением чека);
2) у старых возвратов снимка нет — восстанавливаем: количество строки = текущее +
   возвращённое, удалённые строки — из `returned_items`, итог = текущий + суммы частичных
   (не последних) возвратов, оплаты — масштабом к исходному итогу.

Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
import copy
import json
from decimal import Decimal

from django.core.serializers.json import DjangoJSONEncoder

from apps.main.models import Sale, SaleItem
from apps.main.pos_utils import money, qty3

D0 = Decimal("0")

# Поля строки, которые храним в снимке и восстанавливаем у удалённой строки.
_ITEM_FIELDS = (
    "id", "company_id", "branch_id", "sale_id", "product_id", "variant_id", "performer_id",
    "sale_package_id", "sale_unit_id",
    "name_snapshot", "barcode_snapshot", "variant_name", "unit_price", "quantity", "line_discount",
    "manual_discount", "discount_source", "promotion_id", "price_manually_edited",
    "is_custom", "is_wholesale", "performer_commission_amount", "purchase_price_snapshot",
    "base_quantity", "price_source", "base_price",
)


def _dec(v, default=D0):
    try:
        return Decimal(str(v)) if v is not None and v != "" else default
    except Exception:
        return default


def _jsonable(data):
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


def sale_snapshot(sale) -> dict:
    """Снимок чека перед возвратом: шапка, оплаты, строки (для SaleReturn.sale_before)."""
    items = []
    for it in SaleItem.objects.filter(sale=sale).order_by("id"):
        items.append({f: getattr(it, f, None) for f in _ITEM_FIELDS})
    payments = [
        {"method": p.method, "amount": p.amount}
        for p in sale.payments.order_by("created_at", "id")
    ]
    return _jsonable({
        "v": 1,
        "subtotal": sale.subtotal,
        "discount_total": sale.discount_total,
        "tax_total": sale.tax_total,
        "total": sale.total,
        "cash_received": sale.cash_received,
        "payments": payments,
        "items": items,
    })


def _returns_sorted(sale):
    return sorted(sale.returns.all(), key=lambda r: (r.created_at is None, r.created_at, str(r.id)))


def _returned_rows(returns):
    """{sale_item_id: {"qty", "discount", "row"}} по всем возвратам чека."""
    out = {}
    for r in returns:
        for row in (r.returned_items or []):
            if not isinstance(row, dict) or not row.get("sale_item"):
                continue
            sid = str(row["sale_item"])
            q = _dec(row.get("qty"))
            amount = _dec(row.get("total") if row.get("total") is not None else row.get("amount"))
            price = _dec(row.get("price"), None)
            if price is None:
                price = money(amount / q) if q > 0 else D0
            disc = _dec(row.get("discount"), None)
            if disc is None:
                disc = money(price * q - amount)
            acc = out.setdefault(sid, {"qty": D0, "discount": D0, "row": row, "price": price,
                                       "mut_qty": D0, "mut_discount": D0})
            acc["qty"] += q
            acc["discount"] += disc
            # Строку чека меняет возврат по позициям (items_payload) или частичный; полный
            # возврат «весь чек» строки не трогает.
            if r.items_payload or not r.is_full:
                acc["mut_qty"] += q
                acc["mut_discount"] += disc
    return out


def _restore_item(sale, data: dict) -> SaleItem:
    """Удалённая строка из словаря снимка (поля приводим к типам модели)."""
    it = SaleItem()
    for name in _ITEM_FIELDS:
        if name not in data:
            continue
        field = SaleItem._meta.get_field(name[:-3] if name.endswith("_id") and name != "promotion_id" else name)
        val = data[name]
        try:
            val = field.to_python(val) if val is not None else None
        except Exception:
            pass
        setattr(it, field.attname, val)
    it.sale = sale
    return it


def _attach_relations(items):
    """У восстановленных строк подставляем товар/вариант одним запросом; удалённые из базы — None."""
    from apps.main.models import Product, ProductVariant

    restored = [it for it in items if getattr(it, "_restored", False)]
    if not restored:
        return
    p_ids = {it.product_id for it in restored if it.product_id}
    v_ids = {it.variant_id for it in restored if it.variant_id}
    products = {p.id: p for p in Product.objects.filter(id__in=p_ids)} if p_ids else {}
    variants = {v.id: v for v in ProductVariant.objects.filter(id__in=v_ids)} if v_ids else {}
    for it in restored:
        it.product = products.get(it.product_id)
        it.variant = variants.get(it.variant_id)
        it.sale_package_id = None
        it.sale_unit_id = None


def _scale_payments(rows, k: Decimal, target: Decimal):
    """Оплаты, приведённые к исходному итогу (обратный масштаб частичного возврата)."""
    out = [{"method": m, "amount": money(a * k)} for m, a in rows]
    if out:
        residual = money(target - sum((p["amount"] for p in out), D0))
        if residual:
            biggest = max(out, key=lambda p: p["amount"])
            biggest["amount"] = money(biggest["amount"] + residual)
    return out


def original_receipt(sale) -> dict:
    """Исходный чек: {items, subtotal, discount_total, tax_total, total, cash_received,
    payments: [{method, amount}] | None, refunded_total, has_returns}.

    items — объекты SaleItem (копии; живые строки базы не меняем) с атрибутами
    `_returned_qty` (сколько вернули) и `_current_qty` (сколько осталось в чеке).
    payments=None — оплаты не менялись, брать sale.payment_lines().
    """
    cached = getattr(sale, "_original_receipt_cache", None)
    if cached is not None:
        return cached

    returns = _returns_sorted(sale)
    live = list(sale.items.all())
    refunded = money(sum((_dec(r.returned_amount) for r in returns), D0))
    rows = _returned_rows(returns)

    if not returns:
        for it in live:
            it._returned_qty = D0
            it._current_qty = qty3(_dec(it.quantity))
        res = {
            "items": live, "subtotal": sale.subtotal, "discount_total": sale.discount_total,
            "tax_total": sale.tax_total, "total": sale.total, "cash_received": sale.cash_received,
            "payments": None, "refunded_total": refunded, "has_returns": False,
        }
        sale._original_receipt_cache = res
        return res

    live_by_id = {str(it.id): it for it in live}
    snap = returns[0].sale_before if isinstance(getattr(returns[0], "sale_before", None), dict) else None
    items = []

    def _from_live(it, qty, disc):
        c = copy.copy(it)
        c._current_qty = qty3(_dec(it.quantity))
        c.quantity = qty3(qty)
        c.line_discount = money(disc)
        return c

    if snap:
        for data in snap.get("items") or []:
            sid = str(data.get("id"))
            if sid in live_by_id:
                items.append(_from_live(live_by_id[sid], _dec(data.get("quantity")), _dec(data.get("line_discount"))))
            else:
                it = _restore_item(sale, data)
                it._restored = True
                it._current_qty = Decimal("0.000")
                items.append(it)
        total = _dec(snap.get("total"))
        res = {
            "subtotal": _dec(snap.get("subtotal")), "discount_total": _dec(snap.get("discount_total")),
            "tax_total": _dec(snap.get("tax_total")), "total": total,
            "cash_received": _dec(snap.get("cash_received")),
            "payments": [{"method": p.get("method"), "amount": _dec(p.get("amount"))} for p in snap.get("payments") or []],
        }
    else:
        # Старые возвраты без снимка — восстановление по текущим данным и returned_items.
        for it in live:
            acc = rows.get(str(it.id)) or {}
            items.append(_from_live(
                it, _dec(it.quantity) + acc.get("mut_qty", D0),
                _dec(it.line_discount) + acc.get("mut_discount", D0),
            ))
        for sid, acc in rows.items():
            # Удалённая строка вернулась целиком: исходное количество = всё возвращённое по ней.
            if sid in live_by_id or acc["qty"] <= 0:
                continue
            row = acc["row"]
            it = _restore_item(sale, {
                "id": sid, "company_id": sale.company_id, "branch_id": sale.branch_id,
                "product_id": row.get("product"), "variant_id": row.get("variant"),
                "name_snapshot": row.get("name") or "", "unit_price": str(acc["price"]),
                "quantity": str(acc["qty"]), "line_discount": str(money(acc["discount"])),
                "is_custom": not row.get("product"),
            })
            it._restored = True
            it._current_qty = Decimal("0.000")
            items.append(it)
        cur_total = _dec(sale.total)
        partial_sum = sum((_dec(r.returned_amount) for r in returns if not r.is_full), D0)
        total = money(cur_total + partial_sum)
        k = (total / cur_total) if (partial_sum > 0 and cur_total > 0) else Decimal("1")
        if partial_sum > 0:
            subtotal = money(sum((_dec(i.unit_price) * _dec(i.quantity) for i in items), D0))
            tax = money(_dec(sale.tax_total) * k)
            res = {
                "subtotal": subtotal, "tax_total": tax, "total": total,
                "discount_total": money(subtotal + tax - total),
                "cash_received": money(_dec(sale.cash_received) * k),
            }
        else:
            res = {
                "subtotal": sale.subtotal, "discount_total": sale.discount_total,
                "tax_total": sale.tax_total, "total": total, "cash_received": sale.cash_received,
            }
        pay_rows = [(p.method, _dec(p.amount)) for p in sale.payments.all()]
        res["payments"] = _scale_payments(pay_rows, k, total) if (pay_rows and partial_sum > 0) else (
            [{"method": m, "amount": a} for m, a in pay_rows] if pay_rows else None
        )

    # Чек без строк оплаты (старый): одна строка способом оплаты чека на исходный итог.
    if not res.get("payments") and sale.payment_method and sale.payment_method != Sale.PaymentMethod.DEBT:
        res["payments"] = [{"method": sale.payment_method, "amount": money(res["total"])}]
    elif not res.get("payments"):
        res["payments"] = None

    for it in items:
        acc = rows.get(str(it.id)) or {}
        it._returned_qty = qty3(min(acc.get("qty", D0), _dec(it.quantity))) if acc else Decimal("0.000")
    _attach_relations(items)
    res.update({"items": items, "refunded_total": refunded, "has_returns": True})
    sale._original_receipt_cache = res
    return res


def original_change(sale, orig: dict) -> Decimal:
    """Сдача исходного чека: получено наличными − наличная часть оплаты."""
    pays = orig.get("payments")
    if pays is None:
        return sale.change
    cash = sum((money(p["amount"]) for p in pays if p.get("method") == Sale.PaymentMethod.CASH), D0)
    if cash <= 0:
        return Decimal("0.00")
    diff = _dec(orig.get("cash_received")) - cash
    return money(diff) if diff > 0 else Decimal("0.00")


def sale_refunds(sale) -> list:
    """refunds[] для ответа чека: каждый возврат отдельно, по времени."""
    out = []
    for r in _returns_sorted(sale):
        lines = []
        for row in (r.returned_items or []):
            if not isinstance(row, dict):
                continue
            amount = row.get("total") if row.get("total") is not None else row.get("amount")
            lines.append({
                "sale_item": row.get("sale_item"),
                "name": row.get("name"),
                "qty": str(qty3(_dec(row.get("qty")))),
                "amount": str(money(_dec(amount))),
            })
        out.append({
            "id": str(r.id),
            "amount": str(money(_dec(r.returned_amount))),
            "cash": str(money(_dec(r.refund_cash))),
            "noncash": str(money(_dec(r.refund_noncash))),
            "method": r.refund_method or None,
            "is_full": bool(r.is_full),
            "is_defect": bool(r.is_defect),
            "reason": r.reason or "",
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "lines": lines,
        })
    return out


def return_status(sale, refunded: Decimal) -> str:
    """none | partial | full — состояние возврата чека (отдельно от status)."""
    if sale.returns.all() and sale.status == Sale.Status.CANCELED:
        return "full"
    if refunded > 0 or sale.status == getattr(Sale.Status, "PARTIALLY_RETURNED", "partially_returned"):
        return "partial"
    return "none"
