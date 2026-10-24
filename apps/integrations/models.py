import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models

from apps.users.models import Company

API_KEY_PREFIX = "nk_live_"


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class ApiKey(models.Model):
    """Ключ интеграции компании (бот, внешние сервисы). В базе хранится только хэш."""

    class Scope(models.TextChoices):
        READ = "read", "Чтение"
        WRITE = "write", "Запись"
        # ТЗ Н-24 (1): узкие области для голосового агента и ИИ-бота
        CATALOG_READ = "catalog:read", "Каталог и цены"
        STOCK_READ = "stock:read", "Остатки"
        CLIENT_READ_SELF = "client:read_self", "Данные клиента (по телефону звонящего)"
        ORDER_DRAFT = "order:draft", "Черновик заказа"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=128)
    prefix = models.CharField(max_length=24, help_text="Начало ключа — чтобы узнать его в списке")
    key_hash = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    # ТЗ Н-24 (1): лимит запросов в минуту на ключ
    rate_limit_per_minute = models.PositiveIntegerField(default=120, db_default=120)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "API-ключ"
        verbose_name_plural = "API-ключи"

    @staticmethod
    def generate() -> str:
        return API_KEY_PREFIX + secrets.token_urlsafe(32)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def has_scope(self, scope: str) -> bool:
        return scope in (self.scopes or [])

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"


class ApiKeyCallLog(models.Model):
    """ТЗ Н-24 (1): журнал вызовов по ключу."""

    id = models.BigAutoField(primary_key=True)
    key = models.ForeignKey(ApiKey, on_delete=models.CASCADE, related_name="call_logs")
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=255)
    scope = models.CharField(max_length=32, blank=True, default="")
    allowed = models.BooleanField(default=True)
    reason = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)


class WebhookEndpoint(models.Model):
    """Адрес, куда сервер шлёт события компании (POST JSON, подпись X-Signature)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="webhook_endpoints")
    url = models.URLField(max_length=500)
    events = models.JSONField(default=list)
    secret = models.CharField(max_length=128)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_delivery_at = models.DateTimeField(null=True, blank=True)
    last_status = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Вебхук"
        verbose_name_plural = "Вебхуки"

    def __str__(self):
        return self.url


class IdempotencyRecord(models.Model):
    """
    BE2-11: первый ответ операции по Idempotency-Key. Повтор с тем же ключом
    возвращает этот ответ, а не выполняет операцию ещё раз. Хранится 7 суток.
    """

    class State(models.TextChoices):
        IN_PROGRESS = "in_progress", "Выполняется"
        DONE = "done", "Готово"

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="idempotency_records")
    key = models.CharField(max_length=255)
    scope = models.CharField(max_length=255, help_text="Метод и адрес запроса")
    body_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=16, choices=State.choices, default=State.IN_PROGRESS)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "key"], name="uniq_idempotency_company_key"),
        ]
