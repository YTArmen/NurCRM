"""П0-4 К-2 (14.11.2026, k2_1411): документы движения склада — приход без поставщика, списание, корректировка.
Импортируется в конце apps/main/models.py (app_label = main). Остаток меняется через Product.quantity,
каждую строку пишет журнал остатков (К-1)."""
import uuid

from django.conf import settings
from django.db import models


class StockDocument(models.Model):
    class Kind(models.TextChoices):
        RECEIVE = "receive", "Приход без поставщика"
        WRITE_OFF = "write_off", "Списание"
        COUNT_FIX = "count_fix", "Корректировка остатка"

    class Status(models.TextChoices):
        POSTED = "posted", "Проведён"
        VOIDED = "voided", "Аннулирован"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="stock_documents")
    branch = models.ForeignKey("users.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    number = models.PositiveIntegerField("Номер", default=0)
    kind = models.CharField("Вид", max_length=16, choices=Kind.choices)
    status = models.CharField("Статус", max_length=8, choices=Status.choices, default=Status.POSTED)
    reason_code = models.CharField("Причина", max_length=32, blank=True, default="")
    comment = models.TextField("Комментарий", blank=True, default="")
    occurred_at = models.DateTimeField("Когда произошло")
    total_cost = models.DecimalField("Сумма по закупке", max_digits=14, decimal_places=2, default=0)
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    body_hash = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    void_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        app_label = "main"
        verbose_name = "Документ движения склада"
        verbose_name_plural = "Документы движения склада"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["company", "created_at"], name="idx_stockdoc_company_created")]
        constraints = [models.UniqueConstraint(fields=["company", "idempotency_key"], condition=~models.Q(idempotency_key=""),
                                               name="uniq_stockdoc_company_idem")]


class StockDocumentLine(models.Model):
    document = models.ForeignKey(StockDocument, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("main.Product", on_delete=models.PROTECT, related_name="+")
    name_snapshot = models.CharField(max_length=255, blank=True, default="")
    qty_delta = models.DecimalField("Изменение", max_digits=14, decimal_places=3)
    qty_before = models.DecimalField(max_digits=14, decimal_places=3)
    qty_after = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        app_label = "main"
        ordering = ("id",)
