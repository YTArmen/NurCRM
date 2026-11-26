"""
Приложение клиентов (покупателей) магазинов NurCRM: /api/v1/.

AppCustomer — человек с подтверждённым через Telegram телефоном. С бонусами компаний
он связан не FK, а по телефону: main.Client.phone_normalized == AppCustomer.phone.
Клиентов (main.Client) создаёт касса; баланс бонусов — Client.bonus_balance.
"""
import hashlib
import secrets
import uuid

from django.db import models
from django.utils import timezone

from apps.users.models import Branch, Company

REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def hash_secret(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def generate_referral_code(length: int = 7) -> str:
    return "".join(secrets.choice(REFERRAL_ALPHABET) for _ in range(length))


class AppCustomer(models.Model):
    class Lang(models.TextChoices):
        RU = "ru", "Русский"
        KY = "ky", "Кыргызча"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # null — после удаления аккаунта (DELETE /me); уникальность только среди живых.
    phone = models.CharField("Телефон (E.164)", max_length=20, unique=True, null=True, blank=True)
    # sha256 телефона: остаётся после удаления, чтобы бонус за приглашение нельзя было получить дважды.
    phone_hash = models.CharField(max_length=64, db_index=True, blank=True, default="")
    full_name = models.CharField("ФИО", max_length=255, blank=True, default="")
    birth_date = models.DateField("Дата рождения", null=True, blank=True)
    telegram_user_id = models.BigIntegerField("Telegram user id", null=True, blank=True, db_index=True)
    lang = models.CharField(max_length=2, choices=Lang.choices, default=Lang.RU)
    referral_code = models.CharField(max_length=16, unique=True, null=True, blank=True)
    referred_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="invited_customers"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = "Клиент приложения"
        verbose_name_plural = "Клиенты приложения"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.full_name or '—'} ({self.phone or 'удалён'})"

    # DRF: request.user для токена приложения
    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    @property
    def is_active(self):
        return self.deleted_at is None

    def ensure_referral_code(self):
        if self.referral_code:
            return self.referral_code
        for _ in range(20):
            code = generate_referral_code()
            if not AppCustomer.objects.filter(referral_code=code).exists():
                AppCustomer.objects.filter(pk=self.pk).update(referral_code=code)
                self.referral_code = code
                return code
        raise RuntimeError("Не удалось подобрать код приглашения")


class AppToken(models.Model):
    """Непрозрачный bearer-токен приложения. В БД — только sha256."""

    PREFIX = "nca_"

    id = models.BigAutoField(primary_key=True)
    customer = models.ForeignKey(AppCustomer, on_delete=models.CASCADE, related_name="tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "Токен приложения"
        verbose_name_plural = "Токены приложения"

    @classmethod
    def issue(cls, customer, user_agent: str = ""):
        raw = cls.PREFIX + secrets.token_urlsafe(32)
        obj = cls.objects.create(customer=customer, token_hash=hash_secret(raw), user_agent=(user_agent or "")[:255])
        return obj, raw


class TelegramAuthNonce(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        OK = "ok", "Подтверждён"
        CONSUMED = "consumed", "Токен выдан"
        EXPIRED = "expired", "Истёк"

    id = models.BigAutoField(primary_key=True)
    nonce = models.CharField(max_length=64, unique=True)
    full_name = models.CharField(max_length=255, blank=True, default="")
    birth_date = models.DateField(null=True, blank=True)
    referral_code = models.CharField(max_length=16, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    customer = models.ForeignKey(AppCustomer, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    telegram_user_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    expires_at = models.DateTimeField()
    created_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Вход через Telegram"
        verbose_name_plural = "Входы через Telegram"
        indexes = [models.Index(fields=["telegram_user_id", "status"])]

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at


class AppPushToken(models.Model):
    class Platform(models.TextChoices):
        ANDROID = "android", "Android"
        IOS = "ios", "iOS"
        WEB = "web", "Web"

    id = models.BigAutoField(primary_key=True)
    customer = models.ForeignKey(AppCustomer, on_delete=models.CASCADE, related_name="push_tokens")
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=16, choices=Platform.choices, default=Platform.ANDROID)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Push-токен"
        verbose_name_plural = "Push-токены"


class AppShopSettings(models.Model):
    """
    Настройки магазина в приложении.
    branch=None — строка компании: главный выключатель show_in_app, бонусы, адрес (если филиалов нет).
    branch=<id> — филиал как отдельный магазин: свой адрес/телефон/часы/координаты,
    show_in_app=False скрывает только этот филиал; points_* = None — как у компании.
    """

    class GeocodeStatus(models.TextChoices):
        NONE = "", "—"
        OK = "ok", "Найдено"
        NOT_FOUND = "not_found", "Не найдено"
        ERROR = "error", "Ошибка"
        MANUAL = "manual", "Задано вручную"

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="app_shop_settings")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, null=True, blank=True, related_name="app_shop_settings"
    )
    show_in_app = models.BooleanField(default=False)
    display_name = models.CharField(max_length=255, blank=True, default="")
    address = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=64, blank=True, default="")
    hours = models.CharField(max_length=255, blank=True, default="")
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    points_enabled = models.BooleanField(null=True, blank=True)
    points_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    # ТЗ 29.10 Б-99: срок жизни бонусов, дней (только строка компании); null = не сгорают
    points_expire_days = models.PositiveIntegerField(null=True, blank=True)
    geocoded_at = models.DateTimeField(null=True, blank=True)
    geocoded_address = models.CharField(max_length=255, blank=True, default="")
    geocode_status = models.CharField(max_length=16, choices=GeocodeStatus.choices, blank=True, default="")
    geocode_attempts = models.PositiveSmallIntegerField(default=0)
    # Защита карты: администратор NurCRM может скрыть любой магазин (строка компании — весь магазин).
    hidden_by_admin = models.BooleanField("Скрыт администратором", default=False)
    hidden_reason = models.CharField("Почему скрыт", max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Магазин в приложении"
        verbose_name_plural = "Магазины в приложении"
        constraints = [
            models.UniqueConstraint(
                fields=["company"], condition=models.Q(branch__isnull=True), name="uniq_app_shop_company_row"
            ),
            models.UniqueConstraint(
                fields=["company", "branch"], condition=models.Q(branch__isnull=False), name="uniq_app_shop_branch_row"
            ),
        ]


class ReferralRule(models.Model):
    id = models.BigAutoField(primary_key=True)
    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="app_referral_rule")
    enabled = models.BooleanField(default=False)
    inviter_points = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    invitee_points = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Правило приглашений"
        verbose_name_plural = "Правила приглашений"


class Referral(models.Model):
    """Приглашение. company/rewarded_* заполняются при первой оплаченной покупке приглашённого."""

    id = models.BigAutoField(primary_key=True)
    inviter = models.ForeignKey(AppCustomer, on_delete=models.CASCADE, related_name="referrals_made")
    invitee = models.OneToOneField(AppCustomer, on_delete=models.CASCADE, related_name="referral_received")
    invitee_phone_hash = models.CharField(max_length=64, db_index=True, blank=True, default="")
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    sale_id = models.UUIDField(null=True, blank=True)
    inviter_points = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    invitee_points = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    rewarded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Приглашение"
        verbose_name_plural = "Приглашения"


class AppQrToken(models.Model):
    """Короткоживущий токен для QR «NURCRMT<token>». В БД — sha256."""

    id = models.BigAutoField(primary_key=True)
    customer = models.ForeignKey(AppCustomer, on_delete=models.CASCADE, related_name="qr_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_resolved_at = models.DateTimeField(null=True, blank=True)
    resolve_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "QR-токен"
        verbose_name_plural = "QR-токены"


class ClientAppConfig(models.Model):
    """
    Общие настройки приложения клиентов (одна запись, правится в админке).
    Пока free_until не наступил (или пусто) — любой магазин подключается бесплатно, без тарифа.
    После — только компании с функцией paid_feature_code (тариф или платная функция).
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    free_until = models.DateField(
        "Бесплатно до (включительно)", null=True, blank=True,
        help_text="Пусто — бесплатно без срока. После даты магазин нужно оплатить, выпуск приложения не нужен.",
    )
    paid_feature_code = models.SlugField(
        "Код платной функции", max_length=64, default="client_app",
        help_text="Код функции компании (тариф или «Платные функции компании»), открывающей приложение после бесплатного периода.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройки приложения клиентов"
        verbose_name_plural = "Настройки приложения клиентов"

    def __str__(self):
        return f"Бесплатно до {self.free_until or 'без срока'}"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
