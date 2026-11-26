"""П2-3 К-12 (15.11.2026, p23_1511): переоценка — документ массовой смены цен продажи. Импорт в конце models.py."""
import uuid

from django.conf import settings
from django.db import models


class PriceRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="price_revisions")
    number = models.PositiveIntegerField(default=0)
    comment = models.TextField(blank=True, default="")
    rule = models.JSONField("Правило", null=True, blank=True)
    effective_at = models.DateTimeField()
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=["company", "idempotency_key"], condition=~models.Q(idempotency_key=""),
                                               name="uniq_pricerev_company_idem")]


class PriceRevisionLine(models.Model):
    revision = models.ForeignKey(PriceRevision, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="+")
    old_price = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    new_price = models.DecimalField(max_digits=12, decimal_places=2)
    old_wholesale = models.DecimalField(max_digits=12, decimal_places=3, null=True)
    new_wholesale = models.DecimalField(max_digits=12, decimal_places=3, null=True)

    class Meta:
        app_label = "main"
        ordering = ("id",)
