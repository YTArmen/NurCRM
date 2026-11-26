"""ТЗ 30.10 №10: сколько вернуть по строке чека — одно правило для возврата и для ответа продажи.

Правило (ТЗ Н-02): строка возвращается по цене после скидки на позицию × доля скидки на чек
(итог чека / сумма строк), округление до копеек на каждом шаге; последний возврат — ровно остаток.

Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
from decimal import Decimal

from apps.main.models import Sale
from apps.main.pos_utils import money, qty3


def check_ratio(sale, items=None):
    """Доля скидки на чек: текущий итог / сумма строк после скидок на позиции."""
    items = list(sale.items.all()) if items is None else items
    total = money(sale.total or Decimal("0.00"))
    lines_net = money(sum(
        (Decimal(str(it.unit_price or 0)) * Decimal(str(it.quantity or 0)) - Decimal(str(it.line_discount or 0)))
        for it in items
    ))
    return (total / lines_net) if lines_net > 0 else Decimal("1")


def line_refund(item, rq, ratio):
    """(деньги к возврату, скидка на позицию для rq, сумма rq после скидки на позицию)."""
    old_q = qty3(Decimal(str(item.quantity or 0)))
    old_disc = Decimal(str(item.line_discount or 0))
    item_disc = money(old_disc * (rq / old_q)) if old_q > 0 else Decimal("0.00")
    item_net = money((Decimal(str(item.unit_price or 0)) * rq) - item_disc)
    return money(item_net * ratio), item_disc, item_net


def sale_refundable_lines(sale):
    """{sale_item_id: {quantity, amount, unit_price}} — остаток к возврату по строкам сейчас.

    Сумма amount по строкам = итог чека (копейки округления — в последнюю строку,
    как при последнем возврате). Отменённый / полностью возвращённый чек — нули.
    """
    cached = getattr(sale, "_refundable_lines_cache", None)
    if cached is not None:
        return cached
    items = list(sale.items.all())
    out = {}
    if sale.status == Sale.Status.CANCELED:
        for it in items:
            out[str(it.id)] = {"quantity": Decimal("0.000"), "amount": Decimal("0.00"), "unit_price": Decimal("0.00")}
    else:
        ratio = check_ratio(sale, items)
        running = Decimal("0.00")
        for it in items:
            q = qty3(Decimal(str(it.quantity or 0)))
            amount = line_refund(it, q, ratio)[0] if q > 0 else Decimal("0.00")
            unit = line_refund(it, Decimal("1"), ratio)[0] if q >= 1 else (money(amount / q) if q > 0 else Decimal("0.00"))
            out[str(it.id)] = {"quantity": q, "amount": amount, "unit_price": unit}
            running += amount
        diff = money(sale.total or Decimal("0.00")) - running
        if items and diff:
            last = out[str(items[-1].id)]
            last["amount"] = money(last["amount"] + diff)
    sale._refundable_lines_cache = out
    return out
