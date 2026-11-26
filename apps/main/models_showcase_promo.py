"""П2-5 (14.11.2026, p2514): промокоды витрины и этапы доставки заказа с сайта."""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class ShowcasePromoCode(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="showcase_promo_codes")
    code = models.CharField(max_length=32)
    kind = models.CharField(max_length=8, default="percent")  # percent | fixed
    value = models.DecimalField(max_digits=12, decimal_places=2)
    min_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    max_discount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_to = models.DateTimeField(null=True, blank=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    per_phone = models.PositiveIntegerField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        verbose_name = "Промокод витрины"
        verbose_name_plural = "Промокоды витрины"
        constraints = [models.UniqueConstraint(fields=["company", "code"], name="uniq_showcase_promo_company_code")]


class ShowcaseOrderPromo(models.Model):
    order = models.OneToOneField("main.ShowcaseOrder", on_delete=models.CASCADE, related_name="promo")
    promo = models.ForeignKey(ShowcasePromoCode, on_delete=models.SET_NULL, null=True, related_name="orders")
    code = models.CharField(max_length=32)
    discount = models.DecimalField(max_digits=12, decimal_places=2)
    phone = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        verbose_name = "Промокод в заказе"
        verbose_name_plural = "Промокоды в заказах"


class ShowcaseOrderDelivery(models.Model):
    order = models.OneToOneField("main.ShowcaseOrder", on_delete=models.CASCADE, related_name="delivery")
    stage = models.CharField(max_length=16)  # assembled | with_courier | delivered | failed
    courier = models.CharField(max_length=128, blank=True, default="")
    note = models.CharField(max_length=255, blank=True, default="")
    history = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "main"
        verbose_name = "Доставка заказа с сайта"
        verbose_name_plural = "Доставка заказов с сайта"
