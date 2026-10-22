"""
Модели ТЗ для бэкенда, части 27 (раздел 4) и 28 (разделы 3, 8.4).

Идемпотентность (ТЗ-27 §4, ТЗ-28 §8.4) — общий декоратор apps.integrations.idempotency.idempotent
(BE2-11), своей модели здесь нет.

StockAdjustment / StockAdjustmentLine — изменение остатка «на величину» документом
(приёмка без поставщика, списание, корректировка, возврат поставщику), атомарно,
с ключом идемпотентности. Заменяет опасную пару «GET остаток → PATCH quantity».
Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.users.models import Branch, Company


class StockAdjustment(models.Model):
    """Документ изменения остатка на величину (ТЗ-28 §3)."""

    class Kind(models.TextChoices):
        RECEIVE = "receive", "Приход"
        WRITE_OFF = "write_off", "Списание"
        COUNT_FIX = "count_fix", "Корректировка (точное количество)"
        RETURN_TO_SUPPLIER = "return_to_supplier", "Возврат поставщику"

    class Source(models.TextChoices):
        KASSA = "kassa", "Касса"
        PHONE = "phone", "Телефон"
        WEB = "web", "Веб"

    PREFIX = {
        Kind.RECEIVE: "ПР",
        Kind.WRITE_OFF: "СП",
        Kind.COUNT_FIX: "КР",
        Kind.RETURN_TO_SUPPLIER: "ВП",
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="stock_adjustments")
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_adjustments"
    )
    seq = models.PositiveIntegerField("Порядковый номер по компании")
    kind = models.CharField(max_length=24, choices=Kind.choices, db_index=True)
    reason = models.CharField("Причина", max_length=255, blank=True, default="")
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.KASSA)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_adjustments"
    )
    device_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Изменение остатка"
        verbose_name_plural = "Изменения остатка"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["company", "seq"], name="uniq_stockadj_company_seq"),
            models.UniqueConstraint(
                fields=["company", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="uniq_stockadj_idempotency",
            ),
        ]

    @property
    def number(self) -> str:
        return f"{self.PREFIX.get(self.kind, 'ДК')}-{self.seq:06d}"

    def __str__(self):
        return self.number


class StockAdjustmentLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    adjustment = models.ForeignKey(StockAdjustment, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="stock_adjustment_lines")
    variant = models.ForeignKey(
        "main.ProductVariant", on_delete=models.CASCADE, null=True, blank=True, related_name="stock_adjustment_lines"
    )
    delta = models.DecimalField(max_digits=14, decimal_places=3)
    before = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    after = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "Строка изменения остатка"
        verbose_name_plural = "Строки изменения остатка"
        indexes = [models.Index(fields=["product"], name="possync_adjline_product")]


class ClientDebtLimit(models.Model):
    """
    Лимит долга клиента (ТЗ веб-кассы, п. 2). В apps.main схему менять нельзя (миграции main живут
    только на сервере), поэтому лимит лежит отдельной таблицей. Нет записи или limit = NULL — без лимита.
    """

    client = models.OneToOneField("main.Client", on_delete=models.CASCADE, related_name="debt_limit_row")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="client_debt_limits")
    limit = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Лимит долга клиента"
        verbose_name_plural = "Лимиты долга клиентов"
