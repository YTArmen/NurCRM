"""Б-65: единый журнал действий ИИ + настройки ИИ компании."""
import uuid

from django.db import models

__all__ = ["AiAction", "AiCompanySettings"]


class AiCompanySettings(models.Model):
    """Настройки ИИ компании (отдельная модель, Company не трогаем)."""

    company = models.OneToOneField("users.Company", on_delete=models.CASCADE, related_name="ai_settings")
    # Хранить ли текст фразы пользователя (персональные данные). По умолчанию — нет, только хэш.
    store_ai_utterances = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "main"


class AiAction(models.Model):
    """Одно действие ИИ (мобилка, касса, владелец, Telegram). Ключ повтора — call_id."""

    class ClientKind(models.TextChoices):
        MOBILE = "mobile", "Телефон"
        POS = "pos", "Касса"
        OWNER = "owner", "Программа владельца"
        TELEGRAM = "telegram", "Telegram"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="ai_actions")
    branch = models.ForeignKey("users.Branch", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # callId вызова = Idempotency-Key
    call_id = models.CharField(max_length=255)
    user = models.ForeignKey("users.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="ai_actions")
    device = models.CharField(max_length=128, blank=True, default="")
    client = models.CharField(max_length=16, choices=ClientKind.choices, default=ClientKind.MOBILE)
    at = models.DateTimeField(db_index=True)
    utterance_hash = models.CharField(max_length=64, blank=True, default="")
    utterance_text = models.TextField(null=True, blank=True)
    tool = models.CharField(max_length=128, blank=True, default="")
    args = models.JSONField(default=dict, blank=True)
    result_status = models.CharField(max_length=32, blank=True, default="")
    say = models.TextField(blank=True, default="")
    object_type = models.CharField(max_length=32, blank=True, default="", db_index=True)
    object_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    reversible = models.BooleanField(default=False)
    # call_id (или id) записи, которую эта запись компенсирует (возврат <-> оплата)
    compensation_of = models.CharField(max_length=255, null=True, blank=True)
    model = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        ordering = ("-at", "-created_at")
        constraints = [
            models.UniqueConstraint(fields=["company", "call_id"], name="uniq_aiaction_company_call"),
        ]
        indexes = [
            models.Index(fields=["company", "at"]),
            models.Index(fields=["company", "user", "at"]),
        ]
