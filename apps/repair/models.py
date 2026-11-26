"""П2-1 (15.11.2026, p2115): ремонт и trade-in на сервере. Решения владельца — см. apps/repair/views.py (docstring)."""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

Z = Decimal("0.00")


class RepairSettings(models.Model):
    company = models.OneToOneField("users.Company", on_delete=models.CASCADE, related_name="repair_settings")
    warranty_mode = models.CharField(max_length=8, default="hint")  # zero — цена 0 автоматически | hint — только подсказка
    updated_at = models.DateTimeField(auto_now=True)


class RepairStaffPerms(models.Model):
    """Права сотрудника на ремонт и trade-in (владелец/админ — всё). perms: repair.view, repair.manage, repair.refund,
    trade_in.manage, trade_in.view_documents. Нет записи — по умолчанию (см. perms.py)."""
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="repair_perms")
    perms = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)


class RepairOrder(models.Model):
    STATUSES = ("accepted", "in_work", "ready", "issued", "cancelled")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="repair_orders")
    branch = models.ForeignKey("users.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    number = models.PositiveIntegerField(default=0)
    local_number = models.CharField(max_length=32, blank=True, default="")
    device_id = models.CharField(max_length=64, blank=True, default="")
    local_id = models.CharField(max_length=64, blank=True, default="")
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    client = models.ForeignKey("main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    customer_name = models.CharField(max_length=128, blank=True, default="")
    customer_phone = models.CharField(max_length=64, blank=True, default="")
    device = models.CharField(max_length=255)
    serial = models.CharField(max_length=128, blank=True, default="")
    serial_norm = models.CharField(max_length=128, blank=True, default="", db_index=True)
    problem = models.TextField(blank=True, default="")
    condition_note = models.TextField(blank=True, default="")
    accessories = models.CharField(max_length=255, blank=True, default="")
    estimated_price = models.DecimalField(max_digits=12, decimal_places=2, default=Z)
    final_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_warranty = models.BooleanField(default=False)
    warranty_ref = models.CharField(max_length=64, blank=True, default="")
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, default="accepted", db_index=True)
    prepaid = models.DecimalField(max_digits=12, decimal_places=2, default=Z)
    refunded = models.DecimalField(max_digits=12, decimal_places=2, default=Z)
    sale = models.ForeignKey("main.Sale", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    master = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    notified_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    history = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["company", "status"], name="idx_repair_company_status")]
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="uniq_repair_company_number")]

    @property
    def net_prepaid(self):
        return (self.prepaid or Z) - (self.refunded or Z)


class RepairPayment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(RepairOrder, on_delete=models.CASCADE, related_name="payments")
    kind = models.CharField(max_length=8)  # prepay | refund
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=16, default="cash")
    shift = models.ForeignKey("construction.CashShift", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cashflow_id = models.UUIDField(null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    approved_by_pin = models.BooleanField(default=False)
    idempotency_key = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["order", "idempotency_key"], name="uniq_repairpay_order_key")]


class TradeIn(models.Model):
    STATUSES = ("open", "settled", "stocked", "cancelled")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="trade_ins")
    branch = models.ForeignKey("users.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    number = models.PositiveIntegerField(default=0)
    local_number = models.CharField(max_length=32, blank=True, default="")
    device_id = models.CharField(max_length=64, blank=True, default="")
    local_id = models.CharField(max_length=64, blank=True, default="")
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    client = models.ForeignKey("main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    customer_name = models.CharField(max_length=128, blank=True, default="")
    customer_phone = models.CharField(max_length=64, blank=True, default="")
    device = models.CharField(max_length=255)
    serial = models.CharField(max_length=128, blank=True, default="")
    serial_norm = models.CharField(max_length=128, blank=True, default="", db_index=True)
    condition = models.TextField(blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_out = models.DecimalField(max_digits=12, decimal_places=2, default=Z)
    credit_confirmed = models.DecimalField(max_digits=12, decimal_places=2, default=Z)
    credit_pending = models.DecimalField(max_digits=12, decimal_places=2, default=Z)
    status = models.CharField(max_length=16, default="open", db_index=True)
    document_enc = models.TextField(blank=True, default="")
    document_expires_at = models.DateTimeField(null=True, blank=True)
    product = models.ForeignKey("main.Product", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="uniq_tradein_company_number")]

    @property
    def available(self):
        return (self.amount or Z) - (self.paid_out or Z) - (self.credit_confirmed or Z) - (self.credit_pending or Z)


class TradeInPayout(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trade_in = models.ForeignKey(TradeIn, on_delete=models.CASCADE, related_name="payouts")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    shift = models.ForeignKey("construction.CashShift", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cashflow_id = models.UUIDField(null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    idempotency_key = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["trade_in", "idempotency_key"], name="uniq_tipayout_key")]


class TradeInCredit(models.Model):
    trade_in = models.ForeignKey(TradeIn, on_delete=models.CASCADE, related_name="credits")
    sale = models.ForeignKey("main.Sale", on_delete=models.CASCADE, related_name="+")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["trade_in", "sale"], name="uniq_ticredit_sale")]


def tradein_photo_path(instance, filename):
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "jpg").lower()[:5]
    return f"tradein/{instance.trade_in.company_id}/{instance.trade_in_id}/{uuid.uuid4().hex}.{ext}"


class TradeInPhoto(models.Model):
    trade_in = models.ForeignKey(TradeIn, on_delete=models.CASCADE, related_name="photos")
    file = models.FileField(upload_to=tradein_photo_path, max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
