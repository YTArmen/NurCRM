"""Б-60: вход и выход api/main/customer-orders/."""
from decimal import Decimal

from rest_framework import serializers

from apps.main.models import Client, ClientObject, Product, ProductSaleUnit

from .models import CustomerOrder
from .services import factor_of, lines_total, money, order_total


class OrderItemInSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    sale_unit = serializers.PrimaryKeyRelatedField(queryset=ProductSaleUnit.objects.all(), required=False, allow_null=True)
    qty = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"), required=False)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"), required=False)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True)

    def validate(self, attrs):
        qty = attrs.get("qty") if attrs.get("qty") is not None else attrs.get("quantity")
        if qty is None:
            raise serializers.ValidationError({"qty": "Укажите количество."})
        attrs["qty"] = qty
        attrs.pop("quantity", None)
        su = attrs.get("sale_unit")
        if su is not None and su.product_id != attrs["product"].id:
            raise serializers.ValidationError({"sale_unit": "Единица продажи другого товара."})
        if attrs.get("price") is None:
            p = attrs["product"]
            base = su.price if su is not None and su.price is not None else (
                Decimal(str(p.price or 0)) * (Decimal(str(su.factor)) if su is not None else 1))
            attrs["price"] = money(base)
        return attrs


class PrepaymentInSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    method = serializers.CharField(required=False, allow_blank=True, default="cash")
    shift = serializers.UUIDField(required=False, allow_null=True)


class DeliveryInSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=CustomerOrder.DeliveryType.values, required=False)
    address = serializers.CharField(required=False, allow_blank=True, max_length=255)
    floor = serializers.CharField(required=False, allow_blank=True, max_length=16)
    lift = serializers.BooleanField(required=False, allow_null=True)
    date = serializers.DateField(required=False, allow_null=True)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False)


class OrderInSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=CustomerOrder.Status.values, required=False)
    source = serializers.ChoiceField(choices=CustomerOrder.Source.values, required=False)
    client = serializers.PrimaryKeyRelatedField(queryset=Client.objects.all(), required=False, allow_null=True)
    object = serializers.PrimaryKeyRelatedField(queryset=ClientObject.objects.all(), required=False, allow_null=True)
    customer_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    customer_phone = serializers.CharField(required=False, allow_blank=True, max_length=64)
    items = OrderItemInSerializer(many=True, required=False)
    prepayment = PrepaymentInSerializer(required=False)
    delivery = DeliveryInSerializer(required=False)
    due_date = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True)
    needs_quote = serializers.BooleanField(required=False)


def _user_ref(u):
    if u is None:
        return None
    return {"id": str(u.pk), "name": (u.get_full_name() if hasattr(u, "get_full_name") else "") or getattr(u, "email", "")}


def line_payload(ln):
    f = factor_of(ln)
    waiting = max(Decimal("0"), ln.qty - ln.reserved_qty)
    return {
        "id": str(ln.id), "product": str(ln.product_id), "product_name": ln.name or ln.product.name,
        "sale_unit": str(ln.sale_unit_id) if ln.sale_unit_id else None,
        "qty": str(ln.qty), "quantity": str(ln.qty), "price": str(ln.price),
        "total": str(money(ln.qty * ln.price)),
        "reserved_qty": str(ln.reserved_qty), "waiting_qty": str(waiting), "status": ln.status,
        "base_factor": str(f.normalize()) if f != 1 else "1",
    }


def order_payload(o):
    lines = list(o.lines.all())
    payments = list(o.payments.all())
    last_pay = next((p for p in reversed(payments) if p.kind == "prepayment"), None)
    total = order_total(o)
    po_ids = []
    for ln in lines:
        for lk in ln.supply_links.all():
            if str(lk.purchase_order_id) not in po_ids:
                po_ids.append(str(lk.purchase_order_id))
    return {
        "id": str(o.id), "number": o.number, "status": o.status, "source": o.source,
        "client": str(o.client_id) if o.client_id else None,
        "client_name": (o.client.full_name if o.client_id else None) or o.customer_name or None,
        "customer_name": o.customer_name, "customer_phone": o.customer_phone,
        "object": str(o.client_object_id) if o.client_object_id else None,
        "items": [line_payload(ln) for ln in lines],
        "prepayment": {
            "amount": str(o.prepaid_total),
            "method": last_pay.method if last_pay else None,
            "shift": str(last_pay.shift_id) if last_pay and last_pay.shift_id else None,
            "payments": [{"id": str(p.id), "kind": p.kind, "amount": str(p.amount), "method": p.method,
                          "shift": str(p.shift_id) if p.shift_id else None,
                          "cashflow": str(p.cashflow_id) if p.cashflow_id else None,
                          "created_at": p.created_at.isoformat()} for p in payments],
        },
        "delivery": {
            "type": o.delivery_type, "address": o.delivery_address, "floor": o.delivery_floor,
            "lift": o.delivery_lift, "date": o.delivery_date.isoformat() if o.delivery_date else None,
            "price": str(o.delivery_price),
        },
        "items_total": str(lines_total(o)), "total": str(total), "left_to_pay": str(money(total - o.prepaid_total)),
        "due_date": o.due_date.isoformat() if o.due_date else None,
        "note": o.note, "needs_quote": o.needs_quote,
        "sale": str(o.sale_id) if o.sale_id else None,
        "supplier_orders": po_ids,
        "version": o.version,
        "created_by": _user_ref(o.created_by),
        "created_at": o.created_at.isoformat(), "updated_at": o.updated_at.isoformat(),
        "delivered_at": o.delivered_at.isoformat() if o.delivered_at else None,
        "canceled_at": o.canceled_at.isoformat() if o.canceled_at else None,
    }
