"""ТЗ 26.10, Б-69/Б-71/Б-72: задания печати, отложенные чеки, сеансы телефона-сканера."""
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

__all__ = ["PrintJob", "HeldCart", "ScannerSession", "ScannerEvent"]

LEASE_SECONDS = 60          # аренда задания печати
MAX_ATTEMPTS = 3            # после стольких истёкших аренд задание -> failed
PRINT_JOB_RETENTION_DAYS = 7
HELD_CART_TTL = timedelta(hours=24)
SCANNER_SESSION_TTL = timedelta(hours=8)


class PrintJob(models.Model):
    class Kind(models.TextChoices):
        SALE_RECEIPT = "sale_receipt", "Чек продажи"
        RETURN_RECEIPT = "return_receipt", "Чек возврата"
        TEST = "test", "Тест"

    class Status(models.TextChoices):
        PENDING = "pending", "Ждёт"
        CLAIMED = "claimed", "Взято"
        PRINTED = "printed", "Напечатано"
        FAILED = "failed", "Ошибка"
        CANCELED = "canceled", "Отменено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="print_jobs")
    branch = models.ForeignKey("users.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.SALE_RECEIPT)
    sale = models.ForeignKey("main.Sale", on_delete=models.CASCADE, null=True, blank=True, related_name="print_jobs")
    sale_return = models.ForeignKey("main.SaleReturn", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    target_device_key = models.CharField(max_length=64, blank=True, default="", db_index=True)
    target_cashbox = models.ForeignKey(
        "construction.Cashbox", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    copies = models.PositiveSmallIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    source_device_key = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_by_device = models.CharField(max_length=64, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True)
    printed_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        app_label = "main"
        verbose_name = "Задание печати"
        verbose_name_plural = "Задания печати"
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=["company", "status", "created_at"], name="printjob_co_st_cr_idx"),
        ]

    def __str__(self):
        return f"PrintJob({self.id}, {self.status})"


class HeldCart(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Открыт"
        TAKEN = "taken", "Забран"
        CLOSED = "closed", "Закрыт"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="held_carts")
    branch = models.ForeignKey("users.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cashbox = models.ForeignKey("construction.Cashbox", on_delete=models.CASCADE, related_name="held_carts")
    number = models.PositiveIntegerField(default=0)
    label = models.CharField(max_length=120, blank=True, default="")
    items = models.JSONField(default=list)
    order_discount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    client = models.ForeignKey("main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    from_device_key = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    taken_at = models.DateTimeField(null=True, blank=True)
    taken_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    taken_by_device = models.CharField(max_length=64, blank=True, default="")
    sale = models.ForeignKey("main.Sale", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "main"
        verbose_name = "Отложенный чек"
        verbose_name_plural = "Отложенные чеки"
        ordering = ("created_at", "id")
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="heldcart_company_number_uq")]
        indexes = [models.Index(fields=["company", "cashbox", "status"], name="heldcart_co_cb_st_idx")]

    def __str__(self):
        return f"HeldCart({self.number}, {self.status})"


class ScannerSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="scanner_sessions")
    branch = models.ForeignKey("users.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cashbox = models.ForeignKey("construction.Cashbox", on_delete=models.CASCADE, related_name="scanner_sessions")
    code = models.CharField(max_length=6, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "main"
        verbose_name = "Сеанс сканера"
        verbose_name_plural = "Сеансы сканера"

    def is_active(self):
        return self.closed_at is None and self.expires_at > timezone.now()


class ScannerEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    session = models.ForeignKey(ScannerSession, on_delete=models.CASCADE, related_name="events")
    seq = models.PositiveIntegerField()
    barcode = models.CharField(max_length=128)
    scanned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        verbose_name = "Скан сеанса"
        verbose_name_plural = "Сканы сеанса"
        ordering = ("seq", "id")
        constraints = [models.UniqueConstraint(fields=["session", "seq"], name="scanevent_session_seq_uq")]
