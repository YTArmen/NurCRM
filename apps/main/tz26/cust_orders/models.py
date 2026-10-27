"""Б-60: заказ покупателя — резерв, предоплата, доставка, связь с заказом поставщику."""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

__all__ = ["CustomerOrder", "CustomerOrderLine", "CustomerOrderPayment", "CustomerOrderSupplyLink"]

ZERO2 = Decimal("0.00")
ZERO3 = Decimal("0.000")


class CustomerOrder(models.Model):
    """Заказ покупателя. Деньги (предоплата) — отдельными движениями, выручка только при выдаче."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        CONFIRMED = "confirmed", "Подтверждён"
        RESERVED = "reserved", "Зарезервирован"
        WAITING_SUPPLY = "waiting_supply", "Ждём поставку"
        READY = "ready", "Готов к выдаче"
        DELIVERED = "delivered", "Выдан"
        CLOSED = "closed", "Закрыт"
        CANCELED = "canceled", "Отменён"

    class Source(models.TextChoices):
        KASSA = "kassa", "Касса"
        SHOWCASE = "showcase", "Витрина"
        BOT = "bot", "Бот"

    class DeliveryType(models.TextChoices):
        PICKUP = "pickup", "Самовывоз"
        DELIVERY = "delivery", "Доставка"

    ACTIVE_STATUSES = ("confirmed", "reserved", "waiting_supply", "ready")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="customer_orders")
    branch = models.ForeignKey("users.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    number = models.PositiveIntegerField("Номер (по компании)")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.KASSA, db_index=True)

    client = models.ForeignKey("main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="customer_orders")
    client_object = models.ForeignKey("main.ClientObject", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # снимок для заказов витрины без карточки клиента
    customer_name = models.CharField(max_length=255, blank=True, default="")
    customer_phone = models.CharField(max_length=64, blank=True, default="")

    due_date = models.DateField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    needs_quote = models.BooleanField("Нужна смета", default=False)
    showcase_order_id = models.UUIDField(null=True, blank=True, db_index=True)

    delivery_type = models.CharField(max_length=16, choices=DeliveryType.choices, default=DeliveryType.PICKUP)
    delivery_address = models.CharField(max_length=255, blank=True, default="")
    delivery_floor = models.CharField(max_length=16, blank=True, default="")
    delivery_lift = models.BooleanField(null=True, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    delivery_price = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO2)

    # сколько предоплаты сейчас держит заказ (внесено − возвращено)
    prepaid_total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO2)
    sale = models.ForeignKey("main.Sale", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    version = models.PositiveIntegerField(default=1)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "main"
        ordering = ["-created_at", "-number"]
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="uniq_customer_order_number")]
        indexes = [models.Index(fields=["company", "status"], name="custord_company_status_idx")]


class CustomerOrderLine(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новая"
        RESERVED = "reserved", "В резерве"
        WAITING_SUPPLY = "waiting_supply", "Ждём поставку"
        READY = "ready", "Готова"
        DELIVERED = "delivered", "Выдана"
        CANCELED = "canceled", "Отменена"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(CustomerOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("main.Product", on_delete=models.PROTECT, related_name="customer_order_lines")
    sale_unit = models.ForeignKey("main.ProductSaleUnit", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    name = models.CharField(max_length=255, blank=True, default="")  # снимок названия
    qty = models.DecimalField(max_digits=14, decimal_places=3)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    # в единицах строки (sale_unit, иначе базовых): сколько удерживается со склада
    reserved_qty = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO3)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NEW)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        app_label = "main"
        ordering = ["sort_order", "id"]


class CustomerOrderPayment(models.Model):
    """Движение денег по заказу: предоплата (income) или её возврат (refund). Не выручка."""

    class Kind(models.TextChoices):
        PREPAYMENT = "prepayment", "Предоплата"
        REFUND = "refund", "Возврат предоплаты"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(CustomerOrder, on_delete=models.CASCADE, related_name="payments")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=32, default="cash")
    shift = models.ForeignKey("construction.CashShift", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    cashflow = models.ForeignKey("construction.CashFlow", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        ordering = ["created_at", "id"]


class CustomerOrderSupplyLink(models.Model):
    """Связь строки заказа покупателя со строкой заказа поставщику (Б-44)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    line = models.ForeignKey(CustomerOrderLine, on_delete=models.CASCADE, related_name="supply_links")
    purchase_order = models.ForeignKey("main.PurchaseOrder", on_delete=models.CASCADE, related_name="customer_order_links")
    purchase_order_line = models.ForeignKey("main.PurchaseOrderLine", on_delete=models.CASCADE, related_name="customer_order_links")
    qty = models.DecimalField(max_digits=14, decimal_places=3)          # сколько заказано под этот заказ (ед. строки)
    allocated_qty = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO3)  # сколько уже пришло и ушло в резерв
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        ordering = ["created_at", "id"]
