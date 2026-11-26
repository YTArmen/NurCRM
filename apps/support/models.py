import uuid

from django.db import models


class SupportIssue(models.Model):
    """«Проблема» — все отчёты с одинаковым fingerprint (ТЗ ч.7, п. 3.3)."""

    class Status(models.TextChoices):
        NEW = "new", "Новая"
        ACKNOWLEDGED = "acknowledged", "В работе"
        FIXED = "fixed", "Исправлена"
        MUTED = "muted", "Заглушена"

    class Severity(models.TextChoices):
        CRITICAL = "critical", "Критическая"
        ERROR = "error", "Ошибка"
        WARNING = "warning", "Предупреждение"

    id = models.BigAutoField(primary_key=True)
    fingerprint = models.CharField("Хэш ошибки (sha1)", max_length=64, unique=True)
    title = models.CharField("Краткое название", max_length=512)
    category = models.CharField("Категория", max_length=32, db_index=True)
    severity = models.CharField(
        "Важность", max_length=16, choices=Severity.choices, default=Severity.ERROR, db_index=True
    )
    first_seen = models.DateTimeField("Впервые замечена", db_index=True)
    last_seen = models.DateTimeField("Последний случай", db_index=True)
    occurrences = models.PositiveBigIntegerField("Всего случаев", default=0)
    companies_count = models.PositiveIntegerField("Магазинов затронуто", default=0)
    versions = models.JSONField("Версии", default=list, blank=True)
    status = models.CharField(
        "Статус", max_length=16, choices=Status.choices, default=Status.NEW, db_index=True
    )
    fixed_version = models.CharField("Версия исправления", max_length=64, blank=True, default="")
    fixed_at = models.DateTimeField("Когда отмечена исправленной", blank=True, null=True, db_index=True)
    muted_until = models.DateTimeField("Заглушено до", blank=True, null=True)

    # Оповещения технического бота
    alert_sent = models.BooleanField("Оповещение «новая критичная» отправлено", default=False)
    critical_alert_at = models.DateTimeField("Когда отправлено «новая критичная»", blank=True, null=True)
    last_surge_alert_at = models.DateTimeField("Последнее оповещение о всплеске", blank=True, null=True)

    # Пример для сообщения в группу
    sample_company_name = models.CharField("Пример магазина", max_length=255, blank=True, default="")
    sample_login = models.CharField("Пример логина", max_length=128, blank=True, default="")
    sample_app = models.CharField("Пример приложения", max_length=32, blank=True, default="")
    sample_version = models.CharField("Пример версии", max_length=32, blank=True, default="")
    sample_stack = models.TextField("Пример стека", blank=True, default="")

    class Meta:
        verbose_name = "Проблема (Issue)"
        verbose_name_plural = "Проблемы (Issues)"
        ordering = ["-last_seen"]
        indexes = [
            models.Index(fields=["status", "severity"]),
            models.Index(fields=["category", "last_seen"]),
        ]

    def __str__(self):
        return f"Issue #{self.id} [{self.category}/{self.severity}] {self.title[:50]}"


class SupportErrorReport(models.Model):
    """Один (свёрнутый клиентом) отчёт об ошибке. Хранится 90 дней."""

    class Level(models.TextChoices):
        CRITICAL = "critical", "Critical"
        ERROR = "error", "Error"
        WARNING = "warning", "Warning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_report_id = models.UUIDField("ID отчёта клиента", unique=True)
    company = models.ForeignKey(
        "users.Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_error_reports",
        verbose_name="Компания",
    )
    issue = models.ForeignKey(
        SupportIssue,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports",
        verbose_name="Проблема",
    )
    app = models.CharField("Приложение", max_length=32)
    version = models.CharField("Версия", max_length=32)
    os = models.CharField("ОС", max_length=128, blank=True, default="")
    device_id = models.CharField("ID устройства", max_length=128, blank=True, default="")
    login = models.CharField("Логин сотрудника", max_length=128, blank=True, default="")
    level = models.CharField("Уровень", max_length=16, choices=Level.choices, default=Level.ERROR)
    category = models.CharField("Категория", max_length=32)
    fingerprint = models.CharField("Fingerprint", max_length=64)
    message = models.TextField("Сообщение об ошибке")
    stack = models.TextField("Стек ошибки", blank=True, default="")
    context = models.JSONField("Строки журнала", default=list, blank=True)
    count = models.PositiveIntegerField("Число случаев за интервал", default=1)
    first_at = models.DateTimeField("Первый случай")
    last_at = models.DateTimeField("Последний случай")
    processed_at = models.DateTimeField("Обработан воркером", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Отчёт об ошибке"
        verbose_name_plural = "Отчёты об ошибках"
        ordering = ["-last_at"]
        indexes = [
            models.Index(fields=["fingerprint"]),
            models.Index(fields=["company", "last_at"]),
            models.Index(fields=["version"]),
            models.Index(fields=["fingerprint", "created_at"]),
            models.Index(fields=["device_id", "last_at"]),
            models.Index(fields=["processed_at", "created_at"]),
        ]

    def __str__(self):
        return f"ErrorReport({self.client_report_id}, {self.category}, {self.app})"


class SupportReportAttachment(models.Model):
    """
    ZIP журналов по кнопке «Отправить в поддержку» (до 5 МБ, хранится 30 дней).
    Отдельная таблица: вложение может прийти раньше самого отчёта.
    """

    id = models.BigAutoField(primary_key=True)
    client_report_id = models.UUIDField("ID отчёта клиента", unique=True)
    file = models.FileField("ZIP журнал", upload_to="support_reports/%Y/%m/")
    size = models.PositiveIntegerField("Размер, байт", default=0)
    company = models.ForeignKey(
        "users.Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_report_attachments",
        verbose_name="Компания",
    )
    device_id = models.CharField("ID устройства", max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Вложение к отчёту"
        verbose_name_plural = "Вложения к отчётам"

    def __str__(self):
        return f"Attachment({self.client_report_id}, {self.size} B)"


class SupportAlertRule(models.Model):
    """
    Правила критичности (ТЗ ч.7, п. 3.4) — меняются на сервере без выпуска программы.

    Правило срабатывает, если совпали категория (пусто — любая), минимальный уровень,
    регулярное выражение по сообщению (пусто — любое) и порог `scope`:
      - any            — сразу, порог не используется;
      - cases          — сумма count по этой ошибке у одной компании (или устройства) за окно >= threshold;
      - shops          — разных магазинов с этой ошибкой за окно >= threshold;
      - duration       — ошибка у одной компании держится (последний - первый случай за окно) >= threshold минут;
      - company_logins — разных логинов компании с этой ошибкой за окно >= min(threshold, сотрудников компании).
    """

    class Scope(models.TextChoices):
        ANY = "any", "Сразу"
        CASES = "cases", "Случаев у одного магазина за окно"
        SHOPS = "shops", "Магазинов за окно"
        DURATION = "duration", "Длительность, минут"
        COMPANY_LOGINS = "company_logins", "Все сотрудники компании"

    class Level(models.TextChoices):
        WARNING = "warning", "warning и выше"
        ERROR = "error", "error и выше"
        CRITICAL = "critical", "только critical"

    id = models.BigAutoField(primary_key=True)
    code = models.SlugField("Код", max_length=64, unique=True)
    name = models.CharField("Описание", max_length=255)
    category = models.CharField("Категория (пусто — любая)", max_length=32, blank=True, default="")
    min_level = models.CharField("Минимальный уровень", max_length=16, choices=Level.choices, default=Level.ERROR)
    message_pattern = models.CharField(
        "Регулярное выражение по сообщению (пусто — любое)", max_length=512, blank=True, default=""
    )
    scope = models.CharField("Тип порога", max_length=16, choices=Scope.choices, default=Scope.ANY)
    threshold = models.PositiveIntegerField("Порог", default=1)
    window_minutes = models.PositiveIntegerField("Окно, минут", default=60)
    severity = models.CharField(
        "Важность при срабатывании",
        max_length=16,
        choices=SupportIssue.Severity.choices,
        default=SupportIssue.Severity.CRITICAL,
    )
    enabled = models.BooleanField("Включено", default=True)
    position = models.PositiveIntegerField("Порядок", default=100)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Правило критичности"
        verbose_name_plural = "Правила критичности"
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.code}: {self.name}"


class SupportBotConfig(models.Model):
    """
    Настройки технического Telegram-бота команды. Пустые поля берутся из
    settings / окружения: SUPPORT_TELEGRAM_BOT_TOKEN, SUPPORT_TELEGRAM_CHAT_ID,
    SUPPORT_TELEGRAM_WEBHOOK_SECRET.
    """

    token = models.CharField("Токен бота поддержки", max_length=128, blank=True, default="")
    chat_id = models.CharField("ID Telegram-группы команды", max_length=64, blank=True, default="")
    webhook_secret = models.CharField("Секрет вебхука (secret_token)", max_length=256, blank=True, default="")
    enabled = models.BooleanField("Оповещения включены", default=True)
    hourly_msg_limit = models.PositiveIntegerField("Макс сообщений в час в группу", default=20)

    class Meta:
        verbose_name = "Настройки технического бота"
        verbose_name_plural = "Настройки технического бота"

    def __str__(self):
        return f"SupportBotConfig(chat_id={self.chat_id}, enabled={self.enabled})"


# П2-6 (14.11.2026, p2614)
from apps.support.models_requests import SupportRequest  # noqa: E402,F401
