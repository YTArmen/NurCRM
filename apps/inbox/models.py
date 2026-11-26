"""Модели ящика «Чаты» (ТЗ 26.10, раздел 11: Б-73…Б-77, Б-87)."""
import uuid

from django.conf import settings as _dj_settings
from django.db import models
from django.db.models import Q

AUTH_USER = _dj_settings.AUTH_USER_MODEL
OPEN_STATUSES = ("open", "pending", "snoozed")


class Channel(models.Model):
    """Канал компании (Б-73). Секреты — только в secrets_enc (AES-GCM), наружу не отдаются."""

    class Kind(models.TextChoices):
        TELEGRAM_BOT = "telegram_bot", "Telegram-бот"
        TELEGRAM_BUSINESS = "telegram_business", "Telegram Business"
        WHATSAPP_CLOUD = "whatsapp_cloud", "WhatsApp Cloud API"
        WHATSAPP_BSP = "whatsapp_bsp", "WhatsApp через провайдера"
        INSTAGRAM = "instagram", "Instagram"
        WEB_WIDGET = "web_widget", "Чат на витрине"

    class Provider(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        META = "meta", "Meta"
        WAZZUP = "wazzup", "Wazzup"
        D360 = "360dialog", "360dialog"
        OWN = "own", "Свой"
        UNOFFICIAL = "unofficial", "Неофициальный шлюз"

    class Status(models.TextChoices):
        CONNECTING = "connecting", "Подключается"
        ACTIVE = "active", "Работает"
        ERROR = "error", "Ошибка"
        DISABLED = "disabled", "Выключен"

    # id = channel_uuid в адресе вебхука
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="inbox_channels")
    branch = models.ForeignKey("users.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    provider = models.CharField(max_length=16, choices=Provider.choices, default=Provider.TELEGRAM)
    name = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.CONNECTING)
    external = models.JSONField(default=dict, blank=True)
    health = models.JSONField(default=dict, blank=True)
    settings = models.JSONField(default=dict, blank=True)
    secrets_enc = models.TextField(blank=True, default="")
    # секрет заголовка вебхука (Telegram) / verify_token (Meta)
    webhook_secret = models.CharField(max_length=128, blank=True, default="")
    # серверный Telegram-бот компании: канал = та же запись настроек бота
    bot_settings = models.OneToOneField(
        "main.TelegramBotSettings", on_delete=models.SET_NULL, null=True, blank=True, related_name="inbox_channel"
    )
    risk_accepted_at = models.DateTimeField(null=True, blank=True)
    risk_accepted_by = models.ForeignKey(
        AUTH_USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "inbox"
        indexes = [models.Index(fields=["company", "kind"])]

    def __str__(self):
        return f"Channel({self.kind}, {self.name})"


class InboxAccess(models.Model):
    """Права ящика сотрудника (в User нет флагов can_*_inbox). Нет записи — по умолчанию, см. access.py."""

    user = models.OneToOneField(AUTH_USER, on_delete=models.CASCADE, related_name="inbox_access")
    can_view_inbox = models.BooleanField(default=False)
    can_reply_inbox = models.BooleanField(default=False)
    can_view_all_conversations = models.BooleanField(default=False)
    can_manage_channels = models.BooleanField(default=False)
    can_broadcast = models.BooleanField(default=False)
    can_use_ai_inbox = models.BooleanField(default=False)

    class Meta:
        app_label = "inbox"


class Label(models.Model):
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="inbox_labels")
    name = models.CharField(max_length=64)
    color = models.CharField(max_length=16, blank=True, default="#888888")

    class Meta:
        app_label = "inbox"
        constraints = [models.UniqueConstraint(fields=["company", "name"], name="uq_inbox_label_company_name")]
        ordering = ["name", "id"]


class ChannelContact(models.Model):
    """Собеседник в канале (Б-74). external_id — wa_id, IGSID, Telegram chat_id или visitor_id."""

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="contacts")
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    external_id = models.CharField(max_length=128)
    phone = models.CharField(max_length=32, blank=True, default="")
    username = models.CharField(max_length=128, blank=True, default="")
    display_name = models.CharField(max_length=255, blank=True, default="")
    avatar_url = models.CharField(max_length=500, blank=True, default="")
    client = models.ForeignKey("main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="channel_contacts")
    client_candidates = models.JSONField(default=list, blank=True)
    language = models.CharField(max_length=8, blank=True, default="ru")
    consent_service = models.BooleanField(default=True)
    consent_marketing = models.BooleanField(default=False)
    consents_updated_at = models.DateTimeField(null=True, blank=True)
    consents_source = models.CharField(max_length=24, blank=True, default="")
    blocked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "inbox"
        constraints = [models.UniqueConstraint(fields=["channel", "external_id"], name="uq_inbox_contact_channel_ext")]
        indexes = [models.Index(fields=["company", "client"])]


class Conversation(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Открыт"
        PENDING = "pending", "Ждёт"
        SNOOZED = "snoozed", "Отложен"
        CLOSED = "closed", "Закрыт"

    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="inbox_conversations")
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="conversations")
    contact = models.ForeignKey(ChannelContact, on_delete=models.CASCADE, related_name="conversations")
    branch = models.ForeignKey("users.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    closed_reason = models.CharField(max_length=16, blank=True, default="")
    closed_at = models.DateTimeField(null=True, blank=True)
    assignee = models.ForeignKey(AUTH_USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    labels = models.ManyToManyField(Label, blank=True, related_name="conversations")
    needs_human = models.BooleanField(default=False)
    waiting_since = models.DateTimeField(null=True, blank=True)
    window_expires_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_inbound_message_id = models.BigIntegerField(default=0)
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_message_text = models.CharField(max_length=200, blank=True, default="")
    last_message_direction = models.CharField(max_length=3, blank=True, default="")
    last_message_sender = models.CharField(max_length=8, blank=True, default="")
    ai_paused_until = models.DateTimeField(null=True, blank=True)
    first_taken_at = models.DateTimeField(null=True, blank=True)
    order = models.ForeignKey("main.ShowcaseOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    deal_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        app_label = "inbox"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "contact"],
                condition=Q(status__in=OPEN_STATUSES),
                name="uq_inbox_one_open_conv",
            )
        ]
        indexes = [
            models.Index(fields=["company", "status", "-last_message_at"]),
            models.Index(fields=["company", "updated_at"]),
        ]


class Message(models.Model):
    class Direction(models.TextChoices):
        IN = "in", "Входящее"
        OUT = "out", "Исходящее"

    class Sender(models.TextChoices):
        CONTACT = "contact", "Покупатель"
        USER = "user", "Сотрудник"
        AI = "ai", "ИИ/бот"
        SYSTEM = "system", "Система"

    class Status(models.TextChoices):
        QUEUED = "queued", "В очереди"
        SENT = "sent", "Отправлено"
        DELIVERED = "delivered", "Доставлено"
        READ = "read", "Прочитано"
        FAILED = "failed", "Ошибка"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="messages")
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    direction = models.CharField(max_length=3, choices=Direction.choices)
    sender = models.CharField(max_length=8, choices=Sender.choices)
    sender_user = models.ForeignKey(AUTH_USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=10, default="text")
    text = models.TextField(blank=True, default="")
    attachments = models.JSONField(default=list, blank=True)
    template = models.JSONField(null=True, blank=True)
    reply_to = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    error = models.JSONField(null=True, blank=True)
    external_message_id = models.CharField(max_length=160, blank=True, default="")
    idempotency_key = models.CharField(max_length=64, blank=True, default="")
    idempotency_hash = models.CharField(max_length=64, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    human_agent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "inbox"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "external_message_id"],
                condition=~Q(external_message_id=""),
                name="uq_inbox_msg_channel_ext",
            ),
            models.UniqueConstraint(
                fields=["company", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="uq_inbox_msg_idem",
            ),
        ]
        indexes = [models.Index(fields=["conversation", "id"])]


class PendingStatus(models.Model):
    """Статус пришёл раньше сообщения — храним и применяем, когда сообщение появится (Б-76 п. 4)."""

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="+")
    external_message_id = models.CharField(max_length=160)
    status = models.CharField(max_length=10)
    error = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "inbox"
        indexes = [models.Index(fields=["channel", "external_message_id"])]


class ConversationRead(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(AUTH_USER, on_delete=models.CASCADE, related_name="+")
    last_read_message_id = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "inbox"
        constraints = [models.UniqueConstraint(fields=["conversation", "user"], name="uq_inbox_read")]


class WebhookEvent(models.Model):
    """Журнал вебхуков (Б-76): тело хранится ≤ 30 дней, обрабатывает воркер."""

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="webhook_events")
    provider_event_id = models.CharField(max_length=160, blank=True, default="")
    signature_ok = models.BooleanField(default=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        app_label = "inbox"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "provider_event_id"],
                condition=~Q(provider_event_id=""),
                name="uq_inbox_webhook_event",
            )
        ]


class DataErasureLog(models.Model):
    """Журнал удаления данных контакта (Б-75 п. 7). Содержимого переписки не хранит."""

    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    channel_id = models.UUIDField()
    contact_external_hash = models.CharField(max_length=64)
    contact_id = models.BigIntegerField()
    requested_by = models.ForeignKey(AUTH_USER, on_delete=models.SET_NULL, null=True, related_name="+")
    messages_deleted = models.PositiveIntegerField(default=0)
    conversations_deleted = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "inbox"


class PushLog(models.Model):
    """Журнал push (провайдер «log» только пишет сюда) и защита «не чаще 1 раза в 30 с на диалог»."""

    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, null=True, related_name="+")
    user = models.ForeignKey(AUTH_USER, on_delete=models.CASCADE, null=True, related_name="+")
    provider = models.CharField(max_length=16, default="log")
    title = models.CharField(max_length=80, blank=True, default="")
    body = models.CharField(max_length=80, blank=True, default="")
    tokens = models.PositiveIntegerField(default=0)
    ok = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        app_label = "inbox"


class OwnerAlert(models.Model):
    """Уведомление владельцу «N чатов без ответа» (Б-77 п. 4): журнал и ограничитель «раз в час»."""

    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="+")
    count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        app_label = "inbox"


# Модели этапа 2 (Б-78…Б-86) лежат в models_ext.py
from .models_ext import *  # noqa: E402,F401,F403
