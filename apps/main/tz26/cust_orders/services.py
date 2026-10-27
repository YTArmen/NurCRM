"""Б-60: бизнес-логика заказа покупателя. Деньги и продажу берём из существующего кода кассы."""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from core.exceptions import ApiError

from .models import (
    CustomerOrder,
    CustomerOrderLine,
    CustomerOrderPayment,
    CustomerOrderSupplyLink,
)

Q2 = Decimal("0.01")
Q3 = Decimal("0.001")
ZERO = Decimal("0")
INACTIVE = ("delivered", "closed", "canceled")


def money(v):
    return Decimal(str(v or 0)).quantize(Q2, rounding=ROUND_HALF_UP)


def q3(v):
    return Decimal(str(v or 0)).quantize(Q3, rounding=ROUND_HALF_UP)


def factor_of(line):
    """Сколько базовых единиц товара в одной единице строки."""
    su = line.sale_unit
    f = Decimal(str(su.factor)) if su is not None and su.factor else Decimal("1")
    return f if f > 0 else Decimal("1")


def lines_total(order):
    return money(sum((money(ln.qty * ln.price) for ln in order.lines.all() if ln.status != "canceled"), ZERO))


def order_total(order):
    return money(lines_total(order) + (order.delivery_price or ZERO))


# ---------------------------------------------------------------- номер, создание

def next_number(company):
    return (CustomerOrder.objects.filter(company=company).aggregate(m=Max("number"))["m"] or 0) + 1


def create_order(*, company, branch, user, data, items, source=None):
    """Создаёт заказ + строки одной транзакцией. items — [{product, sale_unit, qty, price, name}]."""
    for _ in range(5):
        try:
            with transaction.atomic():
                order = CustomerOrder(
                    company=company, branch=branch, number=next_number(company), created_by=user,
                    source=source or data.get("source") or CustomerOrder.Source.KASSA,
                    status=data.get("status") or CustomerOrder.Status.DRAFT,
                    client=data.get("client"), client_object=data.get("object"),
                    customer_name=data.get("customer_name", ""), customer_phone=data.get("customer_phone", ""),
                    due_date=data.get("due_date"), note=data.get("note", ""),
                    needs_quote=bool(data.get("needs_quote")), showcase_order_id=data.get("showcase_order_id"),
                )
                apply_delivery(order, data.get("delivery") or {})
                order.save()
                replace_lines(order, items)
            return order
        except IntegrityError:
            continue
    raise ApiError("number_conflict", "Не удалось выдать номер заказа, повторите.", status_code=409)


def apply_delivery(order, d):
    if "type" in d:
        order.delivery_type = d["type"]
    if "address" in d:
        order.delivery_address = d["address"] or ""
    if "floor" in d:
        order.delivery_floor = d["floor"] or ""
    if "lift" in d:
        order.delivery_lift = d["lift"]
    if "date" in d:
        order.delivery_date = d["date"]
    if "price" in d:
        order.delivery_price = money(d["price"])


def replace_lines(order, items):
    order.lines.all().delete()
    CustomerOrderLine.objects.bulk_create([
        CustomerOrderLine(
            order=order, product=it["product"], sale_unit=it.get("sale_unit"),
            name=it.get("name") or it["product"].name, qty=q3(it["qty"]), price=money(it["price"]), sort_order=i,
        )
        for i, it in enumerate(items)
    ])


# ---------------------------------------------------------------- резерв

def _held_base(company_id, product_id, exclude_order_id=None):
    """Сколько базовых единиц товара уже удерживают заказы покупателей."""
    qs = (CustomerOrderLine.objects
          .filter(order__company_id=company_id, product_id=product_id,
                  order__status__in=CustomerOrder.ACTIVE_STATUSES, reserved_qty__gt=0)
          .select_related("sale_unit"))
    if exclude_order_id:
        qs = qs.exclude(order_id=exclude_order_id)
    return sum((ln.reserved_qty * factor_of(ln) for ln in qs), ZERO)


def available_base(product, exclude_order_id=None):
    stock = Decimal(str(product.quantity or 0))
    return max(ZERO, stock - _held_base(product.company_id, product.id, exclude_order_id))


def recompute_status(order):
    """Статус заказа по строкам. Возвращает True, если заказ перешёл в ready."""
    if order.status in INACTIVE or order.status in ("draft", "confirmed"):
        return False
    lines = [ln for ln in order.lines.all() if ln.status != "canceled"]
    prev = order.status
    if not lines:
        return False
    if any(ln.reserved_qty < ln.qty for ln in lines):
        order.status = CustomerOrder.Status.WAITING_SUPPLY
    elif any(ln.status == "ready" for ln in lines):
        order.status = CustomerOrder.Status.READY
    else:
        order.status = CustomerOrder.Status.RESERVED
    order.save(update_fields=["status", "updated_at"])
    return order.status == CustomerOrder.Status.READY and prev != CustomerOrder.Status.READY


def reserve_order(order, user, *, create_supplier_order=False, supplier=None):
    """Резерв того, что есть. Нехватка → waiting_supply; по запросу — черновик заказа поставщику."""
    from apps.main.models import Product

    if order.status in INACTIVE:
        raise ApiError("order_closed", "Заказ уже закрыт.", status_code=409)
    if order.status == CustomerOrder.Status.DRAFT:
        raise ApiError("order_not_confirmed", "Сначала подтвердите заказ (status=confirmed).", status_code=409)
    lines = list(order.lines.select_related("product", "sale_unit").order_by("sort_order", "id"))
    products = {p.id: p for p in Product.objects.select_for_update().filter(id__in={ln.product_id for ln in lines})}
    left = {}
    for ln in lines:
        if ln.status == "canceled":
            continue
        p = products[ln.product_id]
        service = getattr(p, "kind", None) == "service"
        if p.id not in left:
            left[p.id] = None if service else available_base(p)
        need = ln.qty - ln.reserved_qty
        if need <= 0:
            continue
        if left[p.id] is None:
            take = need
        else:
            f = factor_of(ln)
            take = min(need, (left[p.id] / f).quantize(Q3, rounding=ROUND_DOWN))
            take = max(take, ZERO)
            left[p.id] -= take * f
        ln.reserved_qty += take
        if ln.reserved_qty >= ln.qty:
            ln.status = "ready" if ln.status == "ready" else "reserved"
        else:
            ln.status = "waiting_supply"
        ln.save(update_fields=["reserved_qty", "status"])
    order.status = CustomerOrder.Status.RESERVED
    order.save(update_fields=["status", "updated_at"])
    order = CustomerOrder.objects.get(pk=order.pk)
    recompute_status(order)
    po = None
    if create_supplier_order:
        po = create_supplier_order_for(order, user, supplier)
    return order, po


def waiting_to_order(line):
    """Сколько (в ед. строки) ещё не заказано поставщику."""
    f = factor_of(line)
    ordered = sum((lk.qty - lk.allocated_qty for lk in line.supply_links.all()), ZERO) / f
    return max(ZERO, line.qty - line.reserved_qty - ordered)


def create_supplier_order_for(order, user, supplier):
    """Черновик заказа поставщику (Б-44) на недостающее; связь — CustomerOrderSupplyLink."""
    from apps.main.models import Client, PurchaseOrder, PurchaseOrderLine

    lines = [ln for ln in order.lines.select_related("product", "sale_unit").prefetch_related("supply_links")
             if ln.status == "waiting_supply" and waiting_to_order(ln) > 0]
    if not lines:
        return None
    if supplier is None:
        raise ApiError("supplier_required", "Укажите поставщика (supplier).", fields={"supplier": ["Обязательное поле."]})
    if supplier.company_id != order.company_id or supplier.type != Client.StatusClient.SUPPLIERS:
        raise ApiError("supplier_not_found", "Поставщик не найден.", fields={"supplier": ["Поставщик не найден."]})
    for _ in range(5):
        try:
            with transaction.atomic():
                nxt = (PurchaseOrder.objects.filter(company=order.company).aggregate(m=Max("number"))["m"] or 0) + 1
                po = PurchaseOrder.objects.create(
                    company=order.company, branch=order.branch, number=nxt, supplier=supplier,
                    note=f"Под заказ покупателя №{order.number}", created_by=user,
                )
            break
        except IntegrityError:
            continue
    else:
        raise ApiError("number_conflict", "Не удалось выдать номер заказа поставщику.", status_code=409)
    for ln in lines:
        base = (waiting_to_order(ln) * factor_of(ln)).quantize(Q3)
        pol = PurchaseOrderLine.objects.create(
            order=po, product=ln.product, qty=base, purchase_price=ln.product.purchase_price)
        CustomerOrderSupplyLink.objects.create(line=ln, purchase_order=po, purchase_order_line=pol, qty=base)
    return po


def on_supplier_order_received(po):
    """Крючок из apply_receipt_to_order: пришёл товар по заказу поставщику → строки заказов покупателей ready."""
    links = list(CustomerOrderSupplyLink.objects.select_for_update(of=("self",))
                 .filter(purchase_order=po).select_related("purchase_order_line", "line__order", "line__sale_unit"))
    touched = {}
    for lk in links:
        line = lk.line
        order = line.order
        if order.status in INACTIVE or line.status == "canceled":
            continue
        arrived = min(lk.qty, lk.purchase_order_line.received_qty) - lk.allocated_qty
        if arrived <= 0:
            continue
        f = factor_of(line)
        units = min((arrived / f).quantize(Q3, rounding=ROUND_DOWN), line.qty - line.reserved_qty)
        lk.allocated_qty += arrived
        lk.save(update_fields=["allocated_qty"])
        if units > 0:
            line.reserved_qty += units
            if line.reserved_qty >= line.qty:
                line.status = "ready"
            line.save(update_fields=["reserved_qty", "status"])
        touched[order.pk] = order
    for order in touched.values():
        order = CustomerOrder.objects.get(pk=order.pk)
        if recompute_status(order):
            _emit_ready(order)


def _emit_ready(order):
    try:
        from apps.main.change_feed import record_change

        record_change(
            order.company_id, "customer_order_ready", branch_id=order.branch_id, actor_id=None,
            entity="customer_order", entity_id=order.id,
            data={"id": str(order.id), "number": order.number,
                  "client": str(order.client_id) if order.client_id else None,
                  "customer_phone": order.customer_phone or (order.client.phone if order.client_id else "")},
        )
    except Exception:  # лента не должна ронять приход
        pass


# ---------------------------------------------------------------- деньги

def _resolve_shift(company, user, shift_id=None):
    from apps.construction.models import CashShift
    from apps.main.pos_views import _find_open_shift_for_cashier

    if shift_id:
        shift = CashShift.objects.filter(pk=shift_id, company=company).first()
        if shift is None:
            raise ApiError("shift_not_found", "Смена не найдена.", status_code=404, fields={"shift": ["Смена не найдена."]})
        if shift.status != CashShift.Status.OPEN:
            raise ApiError("shift_closed", "Смена закрыта.", status_code=409)
        return shift
    shift = _find_open_shift_for_cashier(company=company, cashier=user)
    if shift is None:
        raise ApiError("shift_not_open", "Смена не открыта. Сначала откройте смену на кассе.", status_code=409)
    return shift


def _money_flow(order, user, shift, amount, method, *, refund, payment_id):
    from apps.construction.auto_cashflow import create_auto_cashflow
    from apps.construction.models import CashFlow

    return create_auto_cashflow(
        company=order.company, branch=shift.branch, cashbox=shift.cashbox, user=user, shift=shift,
        type=CashFlow.Type.EXPENSE if refund else CashFlow.Type.INCOME,
        amount=amount,
        source_kind=CashFlow.SourceKind.WORK_ORDER_PREPAYMENT,  # предоплата вперёд: в ящике есть, выручки нет
        source_id=str(order.id), payment_method=method,
        affects_shift_drawer=(method == "cash"),
        name=f"{'Возврат предоплаты' if refund else 'Предоплата'} по заказу покупателя №{order.number}",
        source_business_operation_id="customer_order",
        idempotency_key=f"custord-{'refund' if refund else 'pay'}-{payment_id}",
    )


def add_prepayment(order, user, amount, method, shift_id=None):
    from apps.main.services_debt import normalize_debt_prepayment_method

    amount = money(amount)
    if amount <= 0:
        raise ApiError("invalid", "Сумма предоплаты должна быть больше нуля.", fields={"amount": ["Больше нуля."]})
    if order.status in INACTIVE:
        raise ApiError("order_closed", "Заказ уже закрыт.", status_code=409)
    total = order_total(order)
    if total > 0 and order.prepaid_total + amount > total:
        raise ApiError("prepayment_exceeds_total", "Предоплата больше суммы заказа.", status_code=409,
                       data={"total": str(total), "prepaid": str(order.prepaid_total),
                             "max": str(max(ZERO, total - order.prepaid_total))})
    shift = _resolve_shift(order.company, user, shift_id)
    method = normalize_debt_prepayment_method(method)
    pay = CustomerOrderPayment.objects.create(
        order=order, kind="prepayment", amount=amount, method=method, shift=shift, created_by=user)
    pay.cashflow = _money_flow(order, user, shift, amount, method, refund=False, payment_id=pay.id)
    pay.save(update_fields=["cashflow"])
    order.prepaid_total = money(order.prepaid_total + amount)
    order.save(update_fields=["prepaid_total", "updated_at"])
    return pay


def refund_prepayment(order, user):
    """Возврат всей удерживаемой предоплаты тем же способом, каким вносили. Из кассы текущей смены."""
    if order.prepaid_total <= 0:
        return []
    shift = _resolve_shift(order.company, user)
    held = {}
    for p in order.payments.all():
        sign = 1 if p.kind == "prepayment" else -1
        held[p.method] = held.get(p.method, ZERO) + sign * p.amount
    out = []
    for method, amt in held.items():
        if amt <= 0:
            continue
        pay = CustomerOrderPayment.objects.create(
            order=order, kind="refund", amount=amt, method=method, shift=shift, created_by=user)
        pay.cashflow = _money_flow(order, user, shift, amt, method, refund=True, payment_id=pay.id)
        pay.save(update_fields=["cashflow"])
        out.append(pay)
    order.prepaid_total = ZERO
    order.save(update_fields=["prepaid_total", "updated_at"])
    return out


# ---------------------------------------------------------------- выдача и отмена

def cancel_order(order, user, *, may_refund):
    if order.status == CustomerOrder.Status.CANCELED:
        return order, True
    if order.status in ("delivered", "closed"):
        raise ApiError("order_already_delivered", "Заказ уже выдан, отменить нельзя.", status_code=409)
    if order.prepaid_total > 0 and not may_refund:
        from apps.users.permissions_flags import denied
        raise denied("can_view_cashier", "Отмена с возвратом предоплаты: только владелец или кассир с правом.")
    refund_prepayment(order, user)
    order.lines.update(reserved_qty=ZERO, status="canceled")
    order.status = CustomerOrder.Status.CANCELED
    order.canceled_at = timezone.now()
    order.save(update_fields=["status", "canceled_at", "updated_at"])
    return order, False


def deliver_order(order, request, payment):
    """
    Выдача: обычная продажа через существующий checkout; предоплата идёт строкой «зачёт».
    Возвращает response checkout; None — если заказ уже выдан (повтор).
    """
    from apps.main.kassa_stage3 import _checkout_payload, _new_cart
    from apps.main.kassa_views import PosQuickCheckoutAPIView, QuickCheckoutItemSerializer
    from apps.main.pos_views import SaleCheckoutAPIView

    user = request.user
    if order.status == CustomerOrder.Status.DELIVERED and order.sale_id:
        return None
    if order.status in ("canceled", "closed"):
        raise ApiError("order_closed", "Заказ закрыт или отменён.", status_code=409)
    if order.status == CustomerOrder.Status.DRAFT:
        raise ApiError("order_not_confirmed", "Заказ ещё не подтверждён.", status_code=409)
    lines = [ln for ln in order.lines.select_related("product", "sale_unit") if ln.status != "canceled"]
    if not lines:
        raise ApiError("order_empty", "В заказе нет строк.", status_code=400)
    if order.status == CustomerOrder.Status.WAITING_SUPPLY or any(ln.status == "waiting_supply" for ln in lines):
        raise ApiError("order_waiting_supply", "Часть товара ещё не пришла.", status_code=409,
                       data={"waiting": [str(ln.id) for ln in lines if ln.status == "waiting_supply"]})

    items = []
    for ln in lines:
        f = factor_of(ln)
        items.append({"product": str(ln.product_id), "qty": str(q3(ln.qty * f)),
                      "price": str(money(ln.price / f) if f != 1 else money(ln.price))})
    if order.delivery_price and order.delivery_price > 0:
        items.append({"custom": True, "name": "Доставка", "price": str(money(order.delivery_price)), "qty": "1"})
    ser = QuickCheckoutItemSerializer(data=items, many=True)
    ser.is_valid(raise_exception=True)

    shift = _resolve_shift(order.company, user)
    cart = _new_cart(order.company, user, shift)
    PosQuickCheckoutAPIView()._add_items(cart, ser.validated_data, False, None)
    cart.recalc()
    total = money(cart.total)
    prepaid = money(order.prepaid_total)
    if prepaid > total:
        raise ApiError("prepayment_exceeds_total", "Предоплата больше суммы заказа.", status_code=409,
                       data={"total": str(total), "prepaid": str(prepaid)})
    data = _checkout_payload(payment, total, prepaid)
    if order.client_id:
        data["client_id"] = str(order.client_id)
    resp = SaleCheckoutAPIView().checkout(request, cart.id, data, allow_offset=True)
    if resp.status_code >= 400:
        transaction.set_rollback(True)
        return resp
    order.sale_id = resp.data["sale_id"]
    order.status = CustomerOrder.Status.DELIVERED
    order.delivered_at = timezone.now()
    order.save(update_fields=["sale", "status", "delivered_at", "updated_at"])
    order.lines.exclude(status="canceled").update(status="delivered")
    return resp


# ---------------------------------------------------------------- витрина (needs_quote)

def create_draft_from_showcase(*, company, branch=None, customer_name="", customer_phone="", items=(),
                               note="", showcase_order_id=None, needs_quote=True, delivery=None):
    """
    Заказ витрины с «Нужна смета» (needs_quote:true) → черновик заказа покупателя source=showcase.
    Вызывается из публичного заказа витрины (Б-34). items: [{product: Product, qty, price?}].
    """
    from apps.main.models import Client

    client = None
    if customer_phone:
        client = Client.objects.filter(company=company, phone=customer_phone).first()
    rows = [{"product": it["product"], "qty": it["qty"],
             "price": it.get("price") if it.get("price") is not None else it["product"].price}
            for it in items]
    data = {"source": "showcase", "status": "draft", "client": client, "customer_name": customer_name,
            "customer_phone": customer_phone, "note": note, "needs_quote": needs_quote,
            "showcase_order_id": showcase_order_id, "delivery": delivery or {}}
    return create_order(company=company, branch=branch, user=None, data=data, items=rows, source="showcase")
