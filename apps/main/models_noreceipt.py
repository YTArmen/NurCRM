"""П1-1 (14.11.2026, p1114): справочник причин возврата и возврат без исходной продажи.
Импортируется в конце apps/main/models.py (app_label = main)."""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class ReturnReason(models.Model):
    """Причина возврата. company=None — общая (системная), иначе своя у компании («+ Своё»)."""
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, null=True, blank=True, related_name="return_reasons")
    code = models.CharField("Код", max_length=32)
    names = models.JSONField("Названия {ru,ky,en,tr,uz}", default=dict)
    is_defect = models.BooleanField("Брак (на склад не возвращается)", default=False)
    requires_note = models.BooleanField("Нужен комментарий", default=False)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        verbose_name = "Причина возврата"
        verbose_name_plural = "Причины возврата"
        ordering = ("sort_order", "id")
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uniq_return_reason_company_code"),
            models.UniqueConstraint(fields=["code"], condition=models.Q(company__isnull=True), name="uniq_return_reason_system_code"),
        ]

    def __str__(self):
        return self.code


class ReturnWithoutSale(models.Model):
    """Возврат товара без исходного чека: товар на склад (кроме брака), деньги из смены."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="returns_without_sale")
    branch = models.ForeignKey("users.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    shift = models.ForeignKey("construction.CashShift", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    approved_by_pin = models.BooleanField("Подтверждено PIN старшего", default=False)
    number = models.PositiveIntegerField("Номер", default=0)
    reason_code = models.CharField(max_length=32)
    note = models.CharField(max_length=255, blank=True, default="")
    client = models.ForeignKey("main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    items = models.JSONField(default=list)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    refund_method = models.CharField(max_length=16, default="cash")  # cash | noncash
    refund_cash = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    refund_noncash = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    restocked = models.BooleanField(default=True)
    cashflow_id = models.UUIDField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        app_label = "main"
        verbose_name = "Возврат без чека"
        verbose_name_plural = "Возвраты без чека"
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=["company", "idempotency_key"], name="uniq_rws_company_key")]
        indexes = [models.Index(fields=["company", "created_at"], name="idx_rws_company_created")]
