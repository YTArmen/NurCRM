"""П3-1 (15.11.2026, p31_1511): подарочные сертификаты. Импорт в конце models.py."""
import uuid

from django.conf import settings
from django.db import models


class GiftCard(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Действует"
        USED = "used", "Использован"
        CANCELLED = "cancelled", "Аннулирован"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="gift_cards")
    code = models.CharField("Код", max_length=32)
    nominal = models.DecimalField(max_digits=12, decimal_places=2)
    balance = models.DecimalField(max_digits=12, decimal_places=2)
    pending = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    expires_at = models.DateField(null=True, blank=True)
    client = models.ForeignKey("main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    sold_sale_id = models.UUIDField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "main"
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=["company", "code"], name="uniq_giftcard_company_code")]


class GiftCardMove(models.Model):
    card = models.ForeignKey(GiftCard, on_delete=models.CASCADE, related_name="moves")
    kind = models.CharField(max_length=12)  # issue | redeem | refund | cancel
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    sale_id = models.UUIDField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        ordering = ("id",)
        constraints = [models.UniqueConstraint(fields=["card", "idempotency_key"], condition=~models.Q(idempotency_key=""),
                                               name="uniq_giftmove_card_idem")]
