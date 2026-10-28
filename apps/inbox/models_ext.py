"""Модели ящика, этап 2 (ТЗ 26.10, Б-78…Б-86). Подключаются в конце models.py."""
import uuid

from django.conf import settings as _dj_settings
from django.db import models
from django.db.models import Q

AUTH_USER = _dj_settings.AUTH_USER_MODEL

__all__ = ["AiRun", "WidgetSettings", "WidgetSession", "QuickReply", "WaTemplate", "Campaign", "CampaignRecipient", "DealExt"]


class AiRun(models.Model):
    """Журнал вызовов ИИ-ответчика (Б-79 п. 6, 7). Персональные данные в вопросе хранятся кодами [Д1]."""

    conversation = models.ForeignKey("inbox.Conversation", on_delete=models.CASCADE, related_name="ai_runs")
    channel = models.ForeignKey("inbox.Channel", on_delete=models.CASCADE, related_name="+")
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    question = models.TextField(blank=True, default="")
    tools = models.JSONField(default=list, blank=True)
    answer = models.TextField(blank=True, default="")
    mode = models.CharField(max_length=8, default="draft")  # draft | auto
    # unsent | auto | user — кто отправил ответ
    sent_by = models.CharField(max_length=8, blank=True, default="")
    sent_by_user = models.ForeignKey(AUTH_USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    sent_message = models.ForeignKey("inbox.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # answered | handoff | refused | unclear | error
    outcome = models.CharField(max_length=10, default="answered")
    handoff_reason = models.CharField(max_length=24, blank=True, default="")
    model = models.CharField(max_length=64, blank=True, default="")
    tokens = models.PositiveIntegerField(default=0)
    cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        app_label = "inbox"
        ordering = ["-id"]
        indexes = [models.Index(fields=["company", "created_at"])]


class WidgetSettings(models.Model):
    """Настройки чата на витрине (Б-83 п. 1). Отдельная модель: документ дизайна витрины не трогаем."""

    company = models.OneToOneField("users.Company", on_delete=models.CASCADE, related_name="inbox_widget")
    enabled = models.BooleanField(default=False)
    greeting = models.JSONField(default=dict, blank=True)  # {ru, ky, en, tr, uz}
    placement = models.JSONField(default=list, blank=True)  # all | product | cart
    offer_telegram = models.BooleanField(default=False)
    offer_whatsapp = models.BooleanField(default=False)
    collect_phone = models.CharField(max_length=10, default="optional")  # off | optional | required
    consent_text = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "inbox"


class WidgetSession(models.Model):
    """Сеанс посетителя витрины. Токен хранится только хешем (sha256)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    channel = models.ForeignKey("inbox.Channel", on_delete=models.CASCADE, related_name="+")
    contact = models.ForeignKey("inbox.ChannelContact", on_delete=models.CASCADE, related_name="widget_sessions")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    ip = models.CharField(max_length=64, blank=True, default="", db_index=True)
    spam = models.BooleanField(default=False)
    link_token = models.CharField(max_length=40, null=True, blank=True, unique=True)  # для t.me/<бот>?start=w_<токен>
    page = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        app_label = "inbox"


class QuickReply(models.Model):
    """Быстрый ответ (Б-78 п. 1). owner=NULL — общий (создаёт админ), иначе личный."""

    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    owner = models.ForeignKey(AUTH_USER, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    shortcut = models.CharField(max_length=32)
    title = models.CharField(max_length=128, blank=True, default="")
    text = models.JSONField(default=dict, blank=True)  # {ru, ky, en, tr, uz}
    variables = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "inbox"
        ordering = ["shortcut", "id"]
        indexes = [models.Index(fields=["company", "shortcut"])]


class WaTemplate(models.Model):
    """Шаблон WhatsApp канала (Б-78 п. 2). Статусы приходят от Meta/BSP при синхронизации."""

    channel = models.ForeignKey("inbox.Channel", on_delete=models.CASCADE, related_name="wa_templates")
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=128)
    language = models.CharField(max_length=10)
    category = models.CharField(max_length=16, default="utility")  # marketing|utility|authentication
    components = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, default="pending")  # approved|pending|rejected|paused
    rejected_reason = models.CharField(max_length=300, blank=True, default="")
    external_id = models.CharField(max_length=64, blank=True, default="")
    synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "inbox"
        ordering = ["name", "language"]
        constraints = [models.UniqueConstraint(fields=["channel", "name", "language"], name="uq_inbox_watemplate")]


class Campaign(models.Model):
    """Рассылка (Б-81). Неофициальные каналы запрещены (channel_not_allowed)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        SCHEDULED = "scheduled", "Запланирована"
        SENDING = "sending", "Идёт отправка"
        DONE = "done", "Завершена"
        CANCELED = "canceled", "Отменена"

    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    channel = models.ForeignKey("inbox.Channel", on_delete=models.CASCADE, related_name="campaigns")
    name = models.CharField(max_length=128, blank=True, default="")
    kind = models.CharField(max_length=16, default="promo")  # promo | service | debt_reminder
    template = models.ForeignKey("inbox.WaTemplate", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    template_params = models.JSONField(default=list, blank=True)  # строки с {имя}, {долг}, {срок}, {магазин}
    text = models.TextField(blank=True, default="")  # Telegram
    segment = models.JSONField(default=dict, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    quiet_hours = models.JSONField(default=dict, blank=True)  # {"from": "21:00", "to": "09:00"}
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    idempotency_key = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(AUTH_USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "inbox"
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["company", "idempotency_key"], condition=~Q(idempotency_key=""),
                                               name="uq_inbox_campaign_idem")]


class CampaignRecipient(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    contact = models.ForeignKey("inbox.ChannelContact", on_delete=models.CASCADE, related_name="+")
    client = models.ForeignKey("main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # queued | sent | delivered | read | failed | skipped
    status = models.CharField(max_length=10, default="queued")
    skip_reason = models.CharField(max_length=24, blank=True, default="")
    message = models.ForeignKey("inbox.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    dedupe_key = models.CharField(max_length=80)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "inbox"
        constraints = [models.UniqueConstraint(fields=["dedupe_key"], name="uq_inbox_campaign_recipient")]
        indexes = [models.Index(fields=["campaign", "status"])]


class DealExt(models.Model):
    """Б-82: добавочные поля сделки main.Deal (воронка магазина): клиент, источник, диалог, заметка."""

    deal = models.OneToOneField("main.Deal", on_delete=models.CASCADE, related_name="inbox_ext")
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    client = models.ForeignKey("main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # telegram | whatsapp | instagram | call | visit | referral | site | other
    source = models.CharField(max_length=12, default="other")
    conversation = models.ForeignKey("inbox.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    note = models.TextField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    instagram_username = models.CharField(max_length=64, blank=True, default="")
    telegram_username = models.CharField(max_length=64, blank=True, default="")
    chat_id = models.CharField(max_length=64, blank=True, default="")
    external_id = models.CharField(max_length=64, blank=True, default="")  # id карточки из funnel.json

    class Meta:
        app_label = "inbox"
        constraints = [models.UniqueConstraint(fields=["company", "external_id"], condition=~Q(external_id=""),
                                               name="uq_inbox_dealext_external")]
