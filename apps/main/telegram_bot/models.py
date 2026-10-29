import uuid
from decimal import Decimal
from django.conf import settings as django_settings
from django.db import models
from apps.main.telegram_bot.crypto import encrypt_secret, decrypt_secret


class TelegramBotSettings(models.Model):
    class Mode(models.TextChoices):
        SERVER = "server", "Сервер"
        LOCAL = "local", "Касса"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.OneToOneField(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="telegram_bot_settings",
        verbose_name="Компания",
    )
    bot_uuid = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True, editable=False)
    mode = models.CharField(
        "Режим работы",
        max_length=16,
        choices=Mode.choices,
        default=Mode.SERVER,
    )
    bot_username = models.CharField("Имя бота (@username)", max_length=128, blank=True, null=True)
    encrypted_token = models.TextField("Зашифрованный токен", blank=True, default="")
    webhook_ok = models.BooleanField("Webhook установлен", default=False)
    webhook_error = models.TextField("Ошибка webhook", blank=True, null=True)
    secret_token = models.CharField("Secret token webhook", max_length=128, blank=True, default="")

    owner_chat_id = models.CharField("Chat ID владельца", max_length=64, blank=True, null=True, db_index=True)
    owner_chat_title = models.CharField("Название чата владельца", max_length=255, blank=True, null=True)
    owner_phone = models.CharField("Телефон владельца", max_length=32, blank=True, null=True)
    # ТЗ Н-23 (2): владелец определяется по from.id Telegram, общий для всех касс компании
    owner_user_id = models.CharField("Telegram from.id владельца", max_length=64, blank=True, default="", db_default="")

    # ТЗ Б-31/Б-22: «группа заказов» — куда слать уведомления о новых заказах
    class OrdersRecipients(models.TextChoices):
        OWNER = "owner", "Владельцу"
        GROUP = "group", "В группу"
        BOTH = "both", "Владельцу и в группу"

    orders_chat_id = models.CharField("Chat ID группы заказов", max_length=64, blank=True, null=True, db_index=True)
    orders_chat_title = models.CharField("Название группы заказов", max_length=255, blank=True, default="")
    orders_chat_bound_at = models.DateTimeField("Группа заказов привязана", blank=True, null=True)
    orders_recipients = models.CharField(
        "Кому слать заказы", max_length=8, choices=OrdersRecipients.choices, default=OrdersRecipients.OWNER,
    )
    orders_show_amount = models.BooleanField("Показывать сумму заказа в Telegram", default=False)
    orders_show_phone = models.BooleanField("Показывать телефон покупателя в Telegram", default=True)
    orders_last_error = models.CharField("Последняя ошибка отправки заказа", max_length=255, blank=True, default="")

    shift_summary_enabled = models.BooleanField("Сводка при закрытии смены", default=True)
    commands_enabled = models.BooleanField("Команды владельца включены", default=True)
    ai_enabled = models.BooleanField("ИИ включён", default=True)
    encrypted_ai_key = models.TextField("Зашифрованный ключ ИИ", blank=True, default="")
    ai_functions_enabled = models.BooleanField("Вызов функций ИИ (аналитика)", default=True)
    daily_report_time = models.CharField("Время ежедневного отчёта (HH:MM)", max_length=16, blank=True, null=True, default="21:00")
    weekly_report = models.BooleanField("Еженедельный отчёт (пн 9:00)", default=True)
    monthly_report = models.BooleanField("Ежемесячный отчёт (1-е число)", default=True)
    ai_daily_limit = models.PositiveIntegerField("Дневной лимит запросов к ИИ", default=200)
    consultant_enabled = models.BooleanField("ИИ-консультант покупателей включён", default=True)
    customer_limit_per_hour = models.PositiveIntegerField("Лимит запросов покупателя в час", default=20)
    voice_replies_enabled = models.BooleanField("Голосовые ответы включены", default=True)
    # ТЗ ч.15: голос и действия ИИ в чате владельца
    class VoiceLanguage(models.TextChoices):
        AUTO = "auto", "Как у владельца"
        RU = "ru", "Русский"
        KY = "ky", "Кыргызский"

    voice_language = models.CharField(
        "Язык голосового ответа", max_length=8, choices=VoiceLanguage.choices, default=VoiceLanguage.AUTO,
    )
    ai_min_markup_percent = models.DecimalField(
        "Минимальная наценка при приходе по накладной, %", max_digits=6, decimal_places=2, default=Decimal("20.00"),
    )
    ai_owner_actions_enabled = models.BooleanField("ИИ может предлагать изменения товаров", default=True)
    send_product_photos = models.BooleanField("Присылать фото товаров", default=True)
    last_update_at = models.DateTimeField("Последнее входящее обновление", blank=True, null=True)
    last_reply_at = models.DateTimeField("Последний ответ бота", blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройки Telegram-бота"
        verbose_name_plural = "Настройки Telegram-ботов"

    def __str__(self):
        return f"TelegramBotSettings({self.company_id}, mode={self.mode}, bot=@{self.bot_username})"

    @property
    def token(self) -> str:
        return decrypt_secret(self.encrypted_token)

    @token.setter
    def token(self, val: str):
        self.encrypted_token = encrypt_secret(val.strip()) if val else ""

    @property
    def ai_key(self) -> str:
        return decrypt_secret(self.encrypted_ai_key)

    @ai_key.setter
    def ai_key(self, val: str):
        self.encrypted_ai_key = encrypt_secret(val.strip()) if val else ""

    @property
    def token_set(self) -> bool:
        return bool(self.encrypted_token)

    @property
    def ai_key_set(self) -> bool:
        return bool(self.encrypted_ai_key)

    @property
    def ai_source(self) -> str:
        return "own" if self.ai_key_set else "shared"


class TelegramInquiry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="telegram_inquiries",
        db_index=True,
        verbose_name="Компания",
    )
    chat_id = models.CharField("Chat ID", max_length=64, db_index=True)
    name = models.CharField("Имя", max_length=255, blank=True, default="")
    username = models.CharField("Username", max_length=255, blank=True, default="")
    text = models.TextField("Текст вопроса", blank=True, default="")
    reply = models.TextField("Текст ответа", blank=True, default="")
    is_voice = models.BooleanField("Голосовое сообщение", default=False)
    order = models.ForeignKey(
        "main.ShowcaseOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="telegram_inquiries",
        verbose_name="Заказ",
    )
    scenario = models.ForeignKey(
        "TelegramBotScenario",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inquiries",
        verbose_name="Сработавший сценарий",
    )
    scenario_title = models.CharField("Название сценария", max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Обращение в Telegram"
        verbose_name_plural = "Обращения в Telegram"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["company", "chat_id"]),
        ]

    def __str__(self):
        return f"TelegramInquiry({self.chat_id}, {self.created_at})"


class TelegramCustomerProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="telegram_customer_profiles",
        db_index=True,
        verbose_name="Компания",
    )
    chat_id = models.CharField("Chat ID", max_length=64, db_index=True)
    name = models.CharField("Имя", max_length=255, blank=True, default="")
    username = models.CharField("Username", max_length=255, blank=True, default="")
    messages_count = models.PositiveIntegerField("Количество сообщений", default=0)
    orders_count = models.PositiveIntegerField("Количество заказов", default=0)
    last_at = models.DateTimeField("Последняя активность", auto_now=True)
    client = models.ForeignKey(
        "main.Client",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="telegram_profiles",
        verbose_name="Привязанный клиент CRM",
    )

    class Meta:
        verbose_name = "Профиль покупателя Telegram"
        verbose_name_plural = "Профили покупателей Telegram"
        unique_together = ("company", "chat_id")
        indexes = [
            models.Index(fields=["company", "last_at"]),
        ]

    def __str__(self):
        return f"TelegramCustomerProfile({self.chat_id}, {self.name})"


class TelegramProcessedUpdate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bot_settings = models.ForeignKey(
        TelegramBotSettings,
        on_delete=models.CASCADE,
        related_name="processed_updates",
    )
    update_id = models.BigIntegerField("Update ID", db_index=True)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Обработанный update Telegram"
        verbose_name_plural = "Обработанные update Telegram"
        unique_together = ("bot_settings", "update_id")

    def __str__(self):
        return f"TelegramProcessedUpdate({self.bot_settings_id}, {self.update_id})"


class TelegramMessageLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bot_settings = models.ForeignKey(
        TelegramBotSettings,
        on_delete=models.CASCADE,
        related_name="message_logs",
    )
    chat_id = models.CharField("Chat ID", max_length=64, db_index=True)
    chat_title = models.CharField("Название чата", max_length=255, blank=True, default="")
    sender_name = models.CharField("Имя отправителя", max_length=255, blank=True, default="")
    text = models.TextField("Текст сообщения", blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Лог входящего сообщения Telegram"
        verbose_name_plural = "Логи входящих сообщений Telegram"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["bot_settings", "created_at"]),
        ]

    def __str__(self):
        return f"TelegramMessageLog({self.chat_id}, {self.created_at})"


class TelegramBotScenario(models.Model):
    class Kind(models.TextChoices):
        COMMAND = "command", "Команда"
        KEYWORDS = "keywords", "Ключевые слова"

    class Audience(models.TextChoices):
        CUSTOMERS = "customers", "Покупатели"
        OWNER = "owner", "Владелец"
        ALL = "all", "Все"

    class Source(models.TextChoices):
        OWNER = "owner", "Владелец"
        AI_ADVISOR = "ai_advisor", "ИИ-советник"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="telegram_bot_scenarios",
        verbose_name="Компания",
        db_index=True,
    )
    kind = models.CharField("Вид сценария", max_length=16, choices=Kind.choices, default=Kind.KEYWORDS)
    command = models.CharField("Команда", max_length=32, blank=True, default="", db_index=True)
    keywords = models.JSONField("Ключевые слова", default=list, blank=True)
    title = models.CharField("Название", max_length=80)
    reply_text = models.TextField("Текст ответа", max_length=3500)
    buttons = models.JSONField("Кнопки", default=list, blank=True)
    photo_product = models.ForeignKey(
        "main.Product",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scenario_photos",
        verbose_name="Товар для фото",
    )
    audience = models.CharField("Аудитория", max_length=16, choices=Audience.choices, default=Audience.CUSTOMERS)
    languages = models.JSONField("Языки", default=list, blank=True)
    priority = models.PositiveSmallIntegerField("Приоритет", default=50)
    is_active = models.BooleanField("Активен", default=True)
    show_in_menu = models.BooleanField("Показывать в меню", default=False)
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Создал",
    )
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Изменил",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    source = models.CharField("Источник", max_length=16, choices=Source.choices, default=Source.OWNER)
    hits = models.PositiveIntegerField("Количество срабатываний", default=0)

    class Meta:
        db_table = "telegram_bot_scenarios"
        verbose_name = "Сценарий Telegram-бота"
        verbose_name_plural = "Сценарии Telegram-бота"
        ordering = ["-priority", "title"]
        indexes = [
            models.Index(fields=["company", "-priority"]),
            models.Index(fields=["company", "is_active"]),
        ]

    def __str__(self):
        return f"TelegramBotScenario({self.company_id}, {self.kind}, {self.title})"


class TelegramBotAudit(models.Model):
    class Action(models.TextChoices):
        SETTINGS_UPDATE = "settings_update", "Обновление настроек"
        SCENARIO_CREATE = "scenario_create", "Создание сценария"
        SCENARIO_UPDATE = "scenario_update", "Обновление сценария"
        SCENARIO_DELETE = "scenario_delete", "Удаление сценария"
        AI_ADVICE_APPLY = "ai_advice_apply", "Применение совета ИИ"
        ORDER_NOTIFIED = "order_notified", "Уведомление о заказе"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="telegram_bot_audits",
        verbose_name="Компания",
        db_index=True,
    )
    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Пользователь",
    )
    user_name = models.CharField("Имя пользователя", max_length=255, blank=True, default="")
    action = models.CharField("Действие", max_length=64, choices=Action.choices, db_index=True)
    object_title = models.CharField("Объект", max_length=255, blank=True, default="")
    source = models.CharField("Источник", max_length=32, default="owner")
    changes = models.JSONField("Изменения", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "telegram_bot_audit"
        verbose_name = "Аудит Telegram-бота"
        verbose_name_plural = "Аудит Telegram-бота"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "-created_at"]),
        ]

    def __str__(self):
        return f"TelegramBotAudit({self.company_id}, {self.action}, {self.created_at})"


# ТЗ Н-23 (1): журнал уведомлений владельцу с ключом идемпотентности
class TelegramOwnerNotification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company", on_delete=models.CASCADE, related_name="telegram_owner_notifications",
    )
    dedupe_key = models.CharField("Ключ идемпотентности", max_length=128, blank=True, default="")
    text = models.TextField("Текст", blank=True, default="")
    ok = models.BooleanField("Отправлено", default=False)
    error = models.CharField("Ошибка", max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "telegram_owner_notifications"
        indexes = [models.Index(fields=["company", "dedupe_key", "-created_at"], name="main_tgown_notif_idx")]


# ТЗ Н-23 (2): одноразовый код подтверждения владельца (хранится хешем)
class TelegramOwnerCode(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company", on_delete=models.CASCADE, related_name="telegram_owner_codes",
    )
    code_hash = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "telegram_owner_codes"
        indexes = [models.Index(fields=["company", "-created_at"], name="main_tgown_code_idx")]


# ТЗ Н-24 (5): журнал звонков голосового агента
class VoiceCall(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="voice_calls")
    client = models.ForeignKey(
        "main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    phone = models.CharField("Номер звонящего", max_length=32, blank=True, default="")
    duration_seconds = models.PositiveIntegerField(default=0)
    language = models.CharField(max_length=8, blank=True, default="")
    outcome = models.CharField("Итог", max_length=32, blank=True, default="", db_index=True)
    transcript = models.TextField(blank=True, default="")
    recording_url = models.URLField(max_length=500, blank=True, default="")
    cost = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    callback_needed = models.BooleanField("Нужно перезвонить", default=False, db_index=True)
    callback_done = models.BooleanField(default=False)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "voice_calls"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "-created_at"], name="main_voicecall_co_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "idempotency_key"], name="main_voicecall_idem_uniq",
                condition=models.Q(idempotency_key__isnull=False),
            ),
        ]
# ТЗ Б-22: журнал уведомлений о заказах витрины (не больше одного на заказ и чат)
class TelegramOrderNotice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="telegram_order_notices")
    order = models.ForeignKey("main.ShowcaseOrder", on_delete=models.CASCADE, related_name="telegram_notices")
    chat_id = models.CharField(max_length=64)
    ok = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    gave_up = models.BooleanField("Не доставлено (повторы закончились)", default=False)
    error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "telegram_order_notices"
        constraints = [models.UniqueConstraint(fields=["order", "chat_id"], name="uniq_tg_order_notice")]
