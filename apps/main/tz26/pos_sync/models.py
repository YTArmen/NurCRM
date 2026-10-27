"""ТЗ 26.10 кусок pos_sync: устройства компании (Б-70), токены push (Б-77 п.3), источник возврата (Б-67)."""
import uuid

from django.db import models

__all__ = ["Device", "DevicePushToken", "ReturnOrigin"]


class Device(models.Model):
    """Б-70: устройство компании (касса, программа владельца, телефон), шлёт heartbeat раз в 60 с."""

    class Kind(models.TextChoices):
        KASSA = "kassa", "Касса"
        OWNER = "owner", "Программа владельца"
        MOBILE = "mobile", "Телефон"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="pos_devices")
    device_key = models.CharField(max_length=64)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.KASSA)
    name = models.CharField(max_length=64, blank=True, default="")
    app_version = models.CharField(max_length=32, blank=True, default="")
    cashbox = models.ForeignKey(
        "construction.Cashbox", on_delete=models.SET_NULL, null=True, blank=True, related_name="pos_devices"
    )
    can_print = models.BooleanField(default=False)
    user = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="pos_devices"
    )
    last_seen_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        ordering = ["-last_seen_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "device_key"], name="uniq_pos_device_company_key"),
        ]


class DevicePushToken(models.Model):
    """Б-77 п.3: токен push устройства сотрудника. Только хранение, отправки нет."""

    class Platform(models.TextChoices):
        ANDROID = "android", "Android"
        IOS = "ios", "iOS"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="device_push_tokens")
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="device_push_tokens")
    device_key = models.CharField(max_length=64)
    platform = models.CharField(max_length=16, choices=Platform.choices)
    token = models.CharField(max_length=1024, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "main"
        ordering = ["-updated_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "device_key"], name="uniq_push_token_user_device"),
        ]


class ReturnOrigin(models.Model):
    """Б-67: с какого устройства проведён возврат (в самой SaleReturn полей нет)."""
    sale_return = models.OneToOneField("main.SaleReturn", on_delete=models.CASCADE, related_name="origin")
    source = models.CharField(max_length=20, blank=True, default="")
    device_key = models.CharField(max_length=64, blank=True, default="")
    device_name = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        app_label = "main"
