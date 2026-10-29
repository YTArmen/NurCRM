from django.db import models, transaction
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.text import slugify
from datetime import timedelta
import uuid

from apps.users.managers import UserManager


class Feature(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name="ID")
    name = models.CharField(max_length=128, db_index=True)
    description = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Функция"
        verbose_name_plural = "Функции"
        indexes = [
            models.Index(fields=["name"]),
        ]


class SubscriptionPlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name="ID")
    name = models.CharField(max_length=128, db_index=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.TextField(blank=True, null=True)
    features = models.ManyToManyField(Feature, blank=True)

    def __str__(self):
        return f"{self.name} - {self.price}₽"

    class Meta:
        verbose_name = "Тариф"
        verbose_name_plural = "Тарифы"
        indexes = [
            models.Index(fields=["name"]),
        ]

    def has_feature(self, feature_name: str) -> bool:
        return self.features.filter(name=feature_name).exists()


class Roles(models.TextChoices):
    ADMIN = "admin", "Администратор"
    OWNER = "owner", "Владелец"
    ROP = "rop", "РОП"
    SUPERVISOR = "supervisor", "Руководитель региона"
    SALESPERSON = "salesperson", "Сотрудник"


class KyrgyzstanRegion(models.TextChoices):
    BISHKEK = "bishkek", "г. Бишкек"
    OSH_CITY = "osh_city", "г. Ош"
    CHUY = "chuy", "Чуйская область"
    OSH = "osh", "Ошская область"
    JALAL_ABAD = "jalal_abad", "Джалал-Абадская область"
    ISSYK_KUL = "issyk_kul", "Иссык-Кульская область"
    NARYN = "naryn", "Нарынская область"
    TALAS = "talas", "Таласская область"
    BATKEN = "batken", "Баткенская область"


class Sector(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True, db_index=True, verbose_name="Название отрасли")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Отрасль"
        verbose_name_plural = "Отрасли"


class Industry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True, db_index=True, verbose_name="Название вида деятельности")
    sectors = models.ManyToManyField(Sector, blank=True, related_name="industries", verbose_name="Отрасли")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Вид деятельности"
        verbose_name_plural = "Виды деятельности"


class CompanyQuerySet(models.QuerySet):
    def delete(self):
        from apps.users.company_deletion import mark_companies_for_deletion, unmark_companies_for_deletion

        company_ids = list(self.values_list("pk", flat=True))
        mark_companies_for_deletion(company_ids)
        try:
            return super().delete()
        finally:
            unmark_companies_for_deletion(company_ids)


class CompanyManager(models.Manager.from_queryset(CompanyQuerySet)):
    pass


# Режим чтения штрихкода весов (ШТРИХ-ПРИНТ и т.п.).
# В EAN-13 весов поле EEEEE может означать ВЕС (граммы) или СУММУ (сомы) —
# зависит от модели/настройки весов. Этот переключатель задаёт трактовку.
SCALE_BARCODE_MODE_AUTO = "auto"      # по префиксу: 20 = вес, 25 = сумма
SCALE_BARCODE_MODE_WEIGHT = "weight"  # всегда вес (граммы / 1000)
SCALE_BARCODE_MODE_AMOUNT = "amount"  # всегда сумма (сомы)
SCALE_BARCODE_MODE_CHOICES = [
    (SCALE_BARCODE_MODE_AUTO, "Авто (по префиксу ШК: 20=вес, 25=сумма)"),
    (SCALE_BARCODE_MODE_WEIGHT, "По весу"),
    (SCALE_BARCODE_MODE_AMOUNT, "По сумме"),
]

# Раскладка полей EAN-13 весов — что стоит между префиксом и контрольной цифрой:
#   "plu"  : префикс(2) + PLU(5) + значение(5) + чек   — товар ищется по Product.plu
#   "code" : префикс(2) + Код(6) + вес(4) + чек         — весы кладут «Код» (1000+(plu-1)*10),
#            PLU восстанавливается из Кода; вес в граммах / 1000
SCALE_BARCODE_LAYOUT_PLU = "plu"
SCALE_BARCODE_LAYOUT_CODE = "code"
SCALE_BARCODE_LAYOUT_CHOICES = [
    (SCALE_BARCODE_LAYOUT_PLU, "По PLU (префикс+PLU(5)+вес(5))"),
    (SCALE_BARCODE_LAYOUT_CODE, "По коду (префикс+PLU(6)+вес(4))"),
]

# Единица суммы в поле ШК — применяется, когда поле трактуется как СУММА
# (mode=amount либо auto с «суммовым» префиксом). Разные весы кодируют по-разному:
#   "tiyin" : сумма в тыйынах, 2 знака после запятой — 03800 → 38.00 сом
#   "som"   : сумма целыми сомами                    — 00036 → 36 сом
SCALE_BARCODE_AMOUNT_UNIT_TIYIN = "tiyin"
SCALE_BARCODE_AMOUNT_UNIT_SOM = "som"
SCALE_BARCODE_AMOUNT_UNIT_CHOICES = [
    (SCALE_BARCODE_AMOUNT_UNIT_TIYIN, "Тыйын (03800 = 38.00 сом)"),
    (SCALE_BARCODE_AMOUNT_UNIT_SOM, "Сом (00036 = 36 сом)"),
]


class Company(models.Model):
    objects = CompanyManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, verbose_name="Название компании")
    slug = models.SlugField("Slug", max_length=80, unique=True, db_index=True, blank=True)

    subscription_plan = models.ForeignKey("SubscriptionPlan", on_delete=models.SET_NULL, null=True, blank=True)
    industry = models.ForeignKey("Industry", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Вид деятельности")
    sector = models.ForeignKey("Sector", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Отрасль")
    region = models.CharField(
        max_length=32,
        choices=KyrgyzstanRegion.choices,
        blank=True,
        null=True,
        verbose_name="Регион",
    )
    phone = models.CharField(max_length=60, verbose_name="Номер телефона", null=True, blank=True)
    phones_howcase = models.CharField(max_length=60, verbose_name="Номер для обращения по витрине", null=True, blank=True)
    whatsapp_phone = models.CharField(max_length=60, verbose_name="Номер для WhatsApp", null=True, blank=True)

    owner = models.OneToOneField("User", on_delete=models.CASCADE, related_name="owned_company", verbose_name="Владелец компании")

    llc = models.CharField("Название компании", max_length=255, blank=True, null=True)
    inn = models.CharField("ИНН", max_length=32, blank=True, null=True)
    okpo = models.CharField("ОКПО", max_length=32, blank=True, null=True)
    score = models.CharField("Расчетный счет", max_length=64, blank=True, null=True)
    bik = models.CharField("БИК", max_length=32, blank=True, null=True)
    address = models.CharField("Адрес", max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    start_date = models.DateTimeField(verbose_name="Дата начала", blank=True, null=True)
    end_date = models.DateTimeField(verbose_name="Дата окончания", blank=True, null=True)
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Активна")
    support_note = models.TextField(blank=True, null=True, verbose_name="Заметка поддержки")

    can_view_documents = models.BooleanField(default=False, verbose_name="Доступ к документам")
    can_view_whatsapp = models.BooleanField(default=False, verbose_name="Доступ к whatsapp")
    can_view_instagram = models.BooleanField(default=False, verbose_name="Доступ к instagram")
    can_view_telegram = models.BooleanField(default=False, verbose_name="Доступ к telegram")
    can_view_showcase = models.BooleanField(default=False, verbose_name="Доступ к витрине")

    cashier_password = models.CharField(
        "Пароль для кассы",
        max_length=128,
        null=True,
        blank=True,
        default=None,
    )
    max_discount_percent = models.DecimalField(
        "Максимальная скидка (%)",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True
    )

    class MarketSphere(models.TextChoices):
        GROCERY = "grocery", "Продукты"
        CLOTHING = "clothing", "Одежда"
        SERVICES = "services", "Услуги"

    market_sphere = models.CharField(
        "Вид магазина",
        max_length=16,
        choices=MarketSphere.choices,
        null=True,
        blank=True,
        help_text="Под него перестраиваются кассы: каталог, карточка товара, запись, прокат.",
    )
    # BE2-02: магазин может совмещать виды (одежда + услуги); market_sphere — основной, первый в списке.
    market_spheres = models.JSONField("Виды магазина", default=list, blank=True)


    scale_api_token = models.CharField(
        max_length=64,
        unique=True,
        blank=True,
        null=True,
        verbose_name="Токен для весов/агентов",
        help_text="Постоянный токен, которым подключаются весы к API",
    )

    scale_barcode_mode = models.CharField(
        max_length=10,
        choices=SCALE_BARCODE_MODE_CHOICES,
        default=SCALE_BARCODE_MODE_AUTO,
        verbose_name="Режим штрихкода весов",
        help_text=(
            "Как трактовать поле в штрихкоде весов: «По весу» (граммы), "
            "«По сумме» (сомы) или «Авто» по префиксу (20=вес, 25=сумма)."
        ),
    )

    scale_barcode_layout = models.CharField(
        max_length=10,
        choices=SCALE_BARCODE_LAYOUT_CHOICES,
        default=SCALE_BARCODE_LAYOUT_PLU,
        verbose_name="Раскладка штрихкода весов",
        help_text=(
            "Как разбирать штрихкод: «По PLU» — префикс+PLU(5)+вес(5); "
            "«По коду» — префикс+PLU(6)+вес(4). В обоих товар ищется по Product.plu "
            "(в выгрузке «Код»=PLU)."
        ),
    )

    scale_barcode_amount_unit = models.CharField(
        max_length=10,
        choices=SCALE_BARCODE_AMOUNT_UNIT_CHOICES,
        default=SCALE_BARCODE_AMOUNT_UNIT_TIYIN,
        verbose_name="Единица суммы в штрихкоде весов",
        help_text=(
            "Работает только когда поле ШК трактуется как СУММА. "
            "«Тыйын» — 03800 = 38.00 сом; «Сом» — 00036 = 36 сом."
        ),
    )

    pos_quick_slots = models.JSONField(
        "Быстрые слоты кассы F3",
        default=dict,
        blank=True,
        help_text="Слоты быстрых товаров F3+0..9: {\"0\": \"product_uuid\", ...}",
    )
    debt_schedule_version = models.CharField(
        "Версия графика отсрочки",
        max_length=8,
        default="v2",
        choices=[("v1", "v1"), ("v2", "v2")],
        blank=True,
        null=True,
        help_text="Версия графика рассрочки на кассе ('v1' или 'v2')",
    )
    cashflow_requests_enabled = models.BooleanField(
        default=False,
        verbose_name="Запросы на подтверждение движений кассы",
        help_text="True — ручные записи не-владельца, компенсация возврата и правка/отмена одобренного движения создают pending-заявку. False — проводятся немедленно.",
    )

    appointment_work_start = models.CharField(
        "Начало рабочего дня для записей",
        max_length=5,
        default="09:00",
        blank=True,
        null=True,
        help_text="Время начала рабочего дня для календаря записей (формат HH:MM)",
    )
    appointment_work_end = models.CharField(
        "Конец рабочего дня для записей",
        max_length=5,
        default="21:00",
        blank=True,
        null=True,
        help_text="Время окончания рабочего дня для календаря записей (формат HH:MM)",
    )


    def ensure_scale_api_token(self):
        if not self.scale_api_token:
            self.scale_api_token = str(uuid.uuid4())
            self.save(update_fields=["scale_api_token"])
        return self.scale_api_token

    # Ключевые слова, которые на практике встречаются в названии сферы "Маркет".
    # (сфера хранится текстом в справочниках, поэтому проверяем "по смыслу")
    MARKET_SECTOR_KEYWORDS = (
        "market", "маркет", "marketing", "маркетинг",
        "shop", "store", "retail", "ритейл",
        "магазин", "продукт", "супермаркет", "гипермаркет", "торгов",
    )

    # Сфера услуг: барбершоп / салон красоты / стоматология / общий "услуги".
    SERVICES_SECTOR_KEYWORDS = (
        "услуг", "service",
        "барбер", "barber", "парикмахер",
        "салон", "beauty", "красот",
        "стоматолог", "dental", "dentistry", "клиник", "clinic",
    )

    def _sector_names(self) -> list:
        """
        Названия сферы компании: Company.industry (вид деятельности) и Company.sector (отрасль).
        """
        names = []
        for attr in ("industry", "sector"):
            try:
                names.append(getattr(getattr(self, attr, None), "name", None))
            except Exception:
                pass
        return names

    def _sector_matches(self, keywords) -> bool:
        for n in self._sector_names():
            s = (n or "").strip().casefold()
            if not s:
                continue
            if any(k in s for k in keywords):
                return True
        return False

    def is_market(self) -> bool:
        """
        True если компания относится к сфере "Маркет".

        В проекте "сфера" задаётся через справочники:
          - Company.industry (вид деятельности)
          - Company.sector (отрасль)

        Чтобы не зависеть от точного написания в БД, используем поиск по подстроке
        ("market"/"маркет") в названии industry/sector.
        """
        return self._sector_matches(self.MARKET_SECTOR_KEYWORDS)

    def is_services(self) -> bool:
        """
        True если компания относится к сфере услуг (барбершоп/салон/стоматология и т.п.).
        """
        return self._sector_matches(self.SERVICES_SECTOR_KEYWORDS)

    def can_use_cashier(self) -> bool:
        """
        Доступен ли компании интерфейс кассы (User.can_view_cashier).

        Сейчас: сфера "Маркет" и сфера услуг.
        """
        return self.is_market() or self.is_services()

    def is_consulting(self) -> bool:
        """True если компания относится к сфере «Консалтинг» (по названию industry/sector)."""
        names = []
        try:
            names.append(getattr(self.industry, "name", None))
        except Exception:
            pass
        try:
            names.append(getattr(self.sector, "name", None))
        except Exception:
            pass

        keywords = ("consult", "консалт", "консалтинг", "consulting")
        for n in names:
            s = (n or "").strip().casefold()
            if s and any(k in s for k in keywords):
                return True
        return False

    def _generate_unique_slug(self) -> str:
        base = slugify(self.name)[:60] or "company"
        candidate = base
        i = 2
        while type(self).objects.filter(slug=candidate).exclude(pk=self.pk).exists():
            candidate = f"{base}-{i}"
            i += 1
            if len(candidate) > 80:
                candidate = f"{base[:50]}-{uuid.uuid4().hex[:8]}"
        return candidate

    def delete(self, using=None, keep_parents=False):
        from apps.users.company_deletion import mark_companies_for_deletion, unmark_companies_for_deletion

        mark_companies_for_deletion([self.pk])
        try:
            return super().delete(using=using, keep_parents=keep_parents)
        finally:
            unmark_companies_for_deletion([self.pk])

    def save(self, *args, **kwargs):
        # даты
        if self._state.adding:
            if not self.start_date:
                self.start_date = timezone.now()
            if not self.end_date and self.start_date:
                self.end_date = self.start_date + timedelta(days=10)

        # весы
        if not self.scale_barcode_layout:
            self.scale_barcode_layout = SCALE_BARCODE_LAYOUT_PLU
        if not self.scale_barcode_amount_unit:
            self.scale_barcode_amount_unit = SCALE_BARCODE_AMOUNT_UNIT_TIYIN

        # slug
        if not self.slug:
            self.slug = self._generate_unique_slug()

        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Компания"
        verbose_name_plural = "Компании"
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["end_date"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["is_active", "end_date"]),
        ]
        constraints = [
            # Уникальность slug без учёта регистра (последняя линия защиты от гонок).
            models.UniqueConstraint(Lower("slug"), name="uniq_company_slug_lower"),
        ]

class CustomRole(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="custom_roles",
        verbose_name="Компания",
    )
    name = models.CharField(max_length=64, verbose_name="Название роли")

    class Meta:
        verbose_name = "Доп. роль"
        verbose_name_plural = "Доп. роли"
        unique_together = ("company", "name")
        indexes = [
            models.Index(fields=["company", "name"]),
        ]

    def __str__(self):
        return self.name


class Branch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name="ID")

    company = models.ForeignKey("Company", on_delete=models.CASCADE, related_name="branches", verbose_name="Компания")

    name = models.CharField(max_length=128, verbose_name="Название филиала")
    code = models.SlugField(
        max_length=64,
        blank=True,
        null=True,
        verbose_name="Код филиала (slug)",
        help_text="Например: spb, moscow, online — уникален в пределах компании.",
    )
    address = models.CharField(max_length=255, blank=True, null=True, verbose_name="Адрес")
    phone = models.CharField(max_length=32, blank=True, null=True, verbose_name="Телефон")
    email = models.EmailField(blank=True, null=True, verbose_name="Email филиала")

    timezone = models.CharField(
        max_length=64,
        default="Asia/Bishkek",
        verbose_name="Часовой пояс",
        help_text="Используется для расписаний, графиков и аналитики",
    )

    subscription_plan = models.ForeignKey(
        "SubscriptionPlan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="branches",
        verbose_name="Тариф филиала",
    )

    features = models.ManyToManyField("Feature", blank=True, related_name="branches", verbose_name="Доп. функции филиала")

    is_active = models.BooleanField(default=True, verbose_name="Активен")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        verbose_name = "Филиал"
        verbose_name_plural = "Филиалы"
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="uq_branch_company_name"),
            models.UniqueConstraint(fields=["company", "code"], name="uq_branch_company_code"),
        ]
        indexes = [
            models.Index(fields=["company", "is_active"]),
            models.Index(fields=["company", "name"]),
            models.Index(fields=["company", "code"]),
        ]

    def __str__(self):
        return f"{self.company.name} / {self.name}"

    def _generate_unique_code(self) -> str:
        # Очень редко, но коллизии возможны — делаем гарантированно уникально ВНУТРИ компании
        for _ in range(20):
            candidate = uuid.uuid4().hex[:10]
            if not Branch.objects.filter(company_id=self.company_id, code=candidate).exists():
                return candidate
        # если что-то совсем странное — fallback длиннее
        return uuid.uuid4().hex

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = self._generate_unique_code()
        super().save(*args, **kwargs)

    def effective_plan(self):
        return self.subscription_plan or getattr(self.company, "subscription_plan", None)

    def has_feature(self, feature_name: str) -> bool:
        if self.features.filter(name=feature_name).exists():
            return True
        plan = self.effective_plan()
        return bool(plan and plan.has_feature(feature_name))


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name="ID")
    email = models.EmailField(unique=True, verbose_name="Email")
    password = models.CharField(max_length=128, verbose_name="Пароль")

    first_name = models.CharField(max_length=64, verbose_name="Имя")
    last_name = models.CharField(max_length=64, verbose_name="Фамилия")
    avatar = models.URLField(blank=True, null=True, verbose_name="Аватар (URL)")
    phone_number = models.CharField(max_length=64, verbose_name="Номер телефона", blank=True, null=True)
    track_number = models.CharField(max_length=64, verbose_name="Номер машины", blank=True, null=True)
    # ТЗ 29.10 Б-101: карточка сотрудника (все поля необязательные)
    hired_at = models.DateField(null=True, blank=True, verbose_name="Принят")
    fired_at = models.DateField(null=True, blank=True, verbose_name="Уволен")
    birth_date = models.DateField(null=True, blank=True, verbose_name="Дата рождения")
    address = models.CharField(max_length=255, null=True, blank=True, verbose_name="Адрес")
    emergency_contact = models.CharField(max_length=255, null=True, blank=True, verbose_name="Контакт на случай ЧС")
    comment = models.CharField(max_length=500, null=True, blank=True, verbose_name="Комментарий")
    language = models.CharField(
        max_length=2, null=True, blank=True,
        choices=[("ru", "ru"), ("ky", "ky"), ("en", "en"), ("tr", "tr"), ("uz", "uz")],
        verbose_name="Язык интерфейса",
    )
    default_cashbox = models.ForeignKey(
        "construction.Cashbox", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", verbose_name="Касса по умолчанию",
    )

    company = models.ForeignKey(
        "Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="employees",
        verbose_name="Компания",
    )

    role = models.CharField(max_length=32, choices=Roles.choices, blank=True, null=True, verbose_name="Системная роль")
    custom_role = models.ForeignKey(
        CustomRole,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
        verbose_name="Кастомная роль",
    )

    consulting_region_codes = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Коды регионов (консалтинг)",
    )

    def get_consulting_region_codes(self) -> list[str]:
        val = self.consulting_region_codes
        if isinstance(val, list):
            return [str(x).strip().lower() for x in val if str(x).strip()]
        return []

    @property
    def is_consulting_supervisor(self) -> bool:
        return str(getattr(self, "role", "") or "").lower() == "supervisor"

    @property
    def is_consulting_salesperson(self) -> bool:
        return str(getattr(self, "role", "") or "").lower() == "salesperson"

    @property
    def is_consulting_manager(self) -> bool:
        r = str(getattr(self, "role", "") or "").lower()
        return r in ("owner", "admin", "rop") or bool(getattr(self, "is_superuser", False)) or bool(getattr(self, "owned_company_id", None))

    # ===== Права доступа =====
    can_view_dashboard = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к обзору")
    can_view_cashbox = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к кассе")
    can_view_departments = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к отделам")
    can_view_orders = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к заказам")
    can_view_analytics = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к аналитике")
    can_view_products = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к товарам")
    can_view_booking = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к бронированию")
    can_view_department_analytics = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к аналитике отделов")
    can_view_employees = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к сотрудникам")
    can_view_clients = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к клиентам")
    can_view_brand_category = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к брендам и категориям")
    can_view_settings = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к настройкам")
    can_view_sale = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к продажам")

    # Consulting: воронка продаж
    can_view_funnel = models.BooleanField(default=False, blank=True, null=True, verbose_name="Консалтинг: страница воронки")
    can_manage_funnel_leads = models.BooleanField(default=False, blank=True, null=True, verbose_name="Консалтинг: управление лидами своей роли")
    can_manage_funnel_stages = models.BooleanField(default=False, blank=True, null=True, verbose_name="Консалтинг: управление стадиями своей роли")
    can_create_funnel = models.BooleanField(default=False, blank=True, null=True, verbose_name="Консалтинг: создание собственных воронок")
    can_manage_lead_ad_spend = models.BooleanField(default=False, blank=True, null=True, verbose_name="Консалтинг: управление рекламным бюджетом лидов")
    can_view_leads_inbox = models.BooleanField(default=False, blank=True, null=True, verbose_name="Консалтинг: доступ к входящим лидам")

    # Building permissions (CRM) — ключи для profile, меню Building и роутинга
    can_view_building_analytics = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Аналитика")
    can_view_building_cash_register = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Касса")
    can_view_building_clients = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Клиенты")
    can_view_building_department = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Отделы")
    can_view_building_employess = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Сотрудники")
    can_view_building_notification = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Уведомления")
    can_view_building_procurement = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Закупки")
    can_view_building_projects = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Проекты")
    can_view_building_salary = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Зарплата")
    can_view_building_sell = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Продажи")
    can_view_building_stock = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Склад")
    can_view_building_treaty = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Договора")
    can_view_building_work_process = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Процесс работы")
    can_view_building_objects = models.BooleanField(default=False, blank=True, null=True, verbose_name="Building: Квартиры/Объекты")

    can_view_additional_services = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к доп. услугам")
    can_view_debts = models.BooleanField(default=False, blank=True, null=True, verbose_name="Доступ к долгам")

    can_view_barber_clients = models.BooleanField(default=False, blank=True, null=True, verbose_name="Барбершоп: клиенты")
    can_view_barber_services = models.BooleanField(default=False, blank=True, null=True, verbose_name="Барбершоп: услуги")
    can_view_barber_history = models.BooleanField(default=False, blank=True, null=True, verbose_name="Барбершоп: история")
    can_view_barber_records = models.BooleanField(default=False, blank=True, null=True, verbose_name="Барбершоп: записи")

    can_view_hostel_rooms = models.BooleanField(default=False, blank=True, null=True, verbose_name="Хостел: комнаты")
    can_view_hostel_booking = models.BooleanField(default=False, blank=True, null=True, verbose_name="Хостел: бронирование")
    can_view_hostel_clients = models.BooleanField(default=False, blank=True, null=True, verbose_name="Хостел: клиенты")
    can_view_hostel_analytics = models.BooleanField(default=False, blank=True, null=True, verbose_name="Хостел: аналитика")

    can_view_cafe_menu = models.BooleanField(default=False, blank=True, null=True, verbose_name="Кафе: меню")
    can_view_cafe_orders = models.BooleanField(default=False, blank=True, null=True, verbose_name="Кафе: заказы")
    can_view_cafe_purchasing = models.BooleanField(default=False, blank=True, null=True, verbose_name="Кафе: закупки")
    can_view_cafe_booking = models.BooleanField(default=False, blank=True, null=True, verbose_name="Кафе: бронирование")
    can_view_cafe_clients = models.BooleanField(default=False, blank=True, null=True, verbose_name="Кафе: клиенты")
    can_view_cafe_tables = models.BooleanField(default=False, blank=True, null=True, verbose_name="Кафе: столы")
    can_view_cafe_cook = models.BooleanField(default=False, blank=True, null=True, verbose_name="Кафе: Кухня")
    can_view_cafe_inventory = models.BooleanField(default=False, blank=True, null=True, verbose_name="Кафе: Инвентаризация")
    can_view_cafe_calculation = models.BooleanField(default=False, blank=True, null=True, verbose_name="Кафе: калькуляция")
    can_view_cafe_order_pay = models.BooleanField(
        default=False, blank=True, null=True, verbose_name="Кафе: проведение оплаты заказов"
    )
    can_view_cafe_order_return = models.BooleanField(
        default=False, blank=True, null=True, verbose_name="Кафе: возврат по заказам"
    )

    can_view_school_students = models.BooleanField(default=False, blank=True, null=True, verbose_name="Школа: ученики")
    can_view_school_groups = models.BooleanField(default=False, blank=True, null=True, verbose_name="Школа: группы")
    can_view_school_lessons = models.BooleanField(default=False, blank=True, null=True, verbose_name="Школа: занятия")
    can_view_school_teachers = models.BooleanField(default=False, blank=True, null=True, verbose_name="Школа: преподаватели")
    can_view_school_leads = models.BooleanField(default=False, blank=True, null=True, verbose_name="Школа: лиды")
    can_view_school_invoices = models.BooleanField(default=False, blank=True, null=True, verbose_name="Школа: счета")

    can_view_client_requests = models.BooleanField(default=False, verbose_name="Доступ к заявкам клиентов")
    can_view_salary = models.BooleanField(default=False, verbose_name="Доступ к зарплатам")
    can_view_sales = models.BooleanField(default=False, verbose_name="Доступ к продажам")
    can_view_services = models.BooleanField(default=False, verbose_name="Доступ к услугам")
    can_view_agent = models.BooleanField(default=False, verbose_name="Доступ к агентам")
    can_view_catalog = models.BooleanField(default=False, verbose_name="Доступ к каталогу")
    can_view_branch = models.BooleanField(default=False, verbose_name="Доступ к филиалу")
    can_view_logistics = models.BooleanField(default=False, verbose_name="Доступ к логистике")
    can_view_request = models.BooleanField(default=False, verbose_name="Доступ к запросам")
    can_view_shifts = models.BooleanField(default=False, verbose_name="Доступ к смене")
    can_view_cashier = models.BooleanField(default=False, verbose_name="Доступ к интерфейсу кассы")
    # ТЗ Н-16: «Работа на кассе» отдельно от «Финансы: кассы компании» (can_view_cashbox).
    # По умолчанию True (db_default) — после миграции никто не теряет доступ к кассе.
    can_use_pos = models.BooleanField(default=True, db_default=True, verbose_name="Работа на кассе (POS)")
    can_view_document = models.BooleanField(default=False, verbose_name="Доступ к документам")
    can_view_market_scales = models.BooleanField(default=False, verbose_name="Доступ к весам")
    can_view_market_label = models.BooleanField(default=False, verbose_name="Доступ к печати этикеток")
    can_view_market_discount = models.BooleanField(default=False, verbose_name="Маркет: доступ к использованию скидки")
    can_view_market_edit_price = models.BooleanField(default=False, verbose_name="Маркет: доступ к редактированию цены")
    can_view_market_delete_cart_item = models.BooleanField(default=False, verbose_name="Маркет: доступ к удалению позиции из корзины")
    can_view_market_procurement = models.BooleanField(default=False, verbose_name="Маркет: доступ к закупкам")
    can_view_market_supplier = models.BooleanField(default=False, verbose_name="Маркет: доступ к поставщикам")
    can_view_market_employee_return = models.BooleanField(default=False, verbose_name="Маркет: доступ к возврату сотрудника")

    # Склад: права на опасные операции (QA 06.10, §2). NULL — по умолчанию для роли
    # (см. apps.warehouse.op_permissions.ROLE_DEFAULTS), True/False — явно выданное право.
    can_post_negative_stock = models.BooleanField(null=True, blank=True, default=None, verbose_name="Склад: проводить в минус")
    can_unpost_documents = models.BooleanField(null=True, blank=True, default=None, verbose_name="Склад: отменять проведение")
    can_change_document_date = models.BooleanField(null=True, blank=True, default=None, verbose_name="Склад: менять дату документа")
    can_post_closed_period = models.BooleanField(null=True, blank=True, default=None, verbose_name="Склад: изменения в закрытом периоде")
    can_cash_negative = models.BooleanField(null=True, blank=True, default=None, verbose_name="Склад: расход кассы в минус")

    # Режим «только смотреть» (ТЗ «Бухгалтер», п. 1). NULL — как раньше (видит → может менять), False — только просмотр.
    can_edit_cashbox = models.BooleanField(null=True, blank=True, default=None, verbose_name="Менять: кассы")
    can_edit_products = models.BooleanField(null=True, blank=True, default=None, verbose_name="Менять: товары")
    can_edit_procurement = models.BooleanField(null=True, blank=True, default=None, verbose_name="Менять: закупки")
    can_edit_documents = models.BooleanField(null=True, blank=True, default=None, verbose_name="Менять: документы склада")
    can_edit_clients = models.BooleanField(null=True, blank=True, default=None, verbose_name="Менять: клиенты и долги")
    can_edit_salary = models.BooleanField(null=True, blank=True, default=None, verbose_name="Менять: зарплата")
    can_edit_employees = models.BooleanField(null=True, blank=True, default=None, verbose_name="Менять: сотрудники")
    can_edit_settings = models.BooleanField(null=True, blank=True, default=None, verbose_name="Менять: настройки")

    branches = models.ManyToManyField(
        "Branch",
        through="BranchMembership",
        blank=True,
        related_name="users",
        verbose_name="Филиалы",
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_platform_admin = models.BooleanField(
        default=False,
        verbose_name="Платформенный администратор NUR",
        help_text="Доступ к /platform-admin/*. Клиентские owner/admin роли этот флаг не получают автоматически.",
    )

    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name="Удалён")
    deleted_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deleted_users",
        verbose_name="Кем удалён",
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"
        indexes = [
            models.Index(fields=["company", "role"]),
            models.Index(fields=["company", "is_active"]),
            models.Index(fields=["deleted_at"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return self.email

    @property
    def role_display(self) -> str:
        if self.role:
            return self.get_role_display()
        if self.custom_role:
            return self.custom_role.name
        return "Без роли"

    def is_in_branch(self, branch) -> bool:
        if branch is None:
            return True
        if not self.pk:
            return False
        return self.branch_memberships.filter(branch=branch).exists()

    @property
    def allowed_branch_ids(self):
        return list(self.branch_memberships.values_list("branch_id", flat=True))

    @property
    def primary_branch(self):
        membership = self.branch_memberships.select_related("branch").filter(is_primary=True).first()
        return membership.branch if membership else None

    def _close_open_cash_shifts(self, by_user=None):
        """
        Автозакрытие открытых кассовых смен сотрудника при его удалении.
        Закрытие идентично штатному (итоги/время/статус), выполняется в той же
        транзакции, что и soft_delete: ошибка закрытия откатывает удаление
        (ошибки НЕ подавляем — в отличие от пакетного close_open_shifts).
        """
        # Локальный импорт во избежание циклической зависимости (construction → users).
        from apps.construction.models import CashShift
        from apps.construction.services_shifts import (
            close_shift_as_system, REASON_EMPLOYEE_DELETED,
        )

        open_shifts = (
            CashShift.objects
            .select_for_update()
            .filter(cashier_id=self.pk, status=CashShift.Status.OPEN)
        )
        for shift in open_shifts:
            close_shift_as_system(shift, REASON_EMPLOYEE_DELETED, by_user=by_user)

    @transaction.atomic
    def soft_delete(self, by_user=None):
        if self.deleted_at:
            return

        # До деактивации закрываем открытые смены (в той же транзакции).
        self._close_open_cash_shifts(by_user=by_user)

        old_email = self.email or ""
        stamp = uuid.uuid4().hex
        new_email = f"deleted__{stamp}__{old_email}"[:254]

        self.is_active = False
        self.deleted_at = timezone.now()
        if by_user and getattr(by_user, "pk", None):
            self.deleted_by = by_user

        self.email = new_email
        self.set_unusable_password()
        self.save(update_fields=["is_active", "deleted_at", "deleted_by", "email", "password"])

    def get_full_name(self):
        full_name = f"{(self.first_name or '').strip()} {(self.last_name or '').strip()}".strip()
        return full_name or self.email or ""

    def get_short_name(self):
        return (self.first_name or "").strip() or self.email or ""

    def __str__(self):
        full_name = self.get_full_name()
        return f"{full_name} ({self.email})" if full_name and full_name != self.email else self.email

    def save(self, *args, **kwargs):
        # Доступ к кассе разрешён только для сфер "Маркет" и "Услуги".
        company = getattr(self, "company", None)
        if not (company and getattr(company, "can_use_cashier", None) and company.can_use_cashier()):
            self.can_view_cashier = False
        super().save(*args, **kwargs)


class PlatformAdminAuditLog(models.Model):
    class Action(models.TextChoices):
        COMPANY_PATCH = "company.patch", "Правка компании"
        COMPANY_SUBSCRIPTION = "company.subscription", "Подписка компании"
        USER_CREATE = "user.create", "Создание пользователя"
        USER_PATCH = "user.patch", "Правка пользователя"
        USER_DELETE = "user.delete", "Удаление пользователя"
        USER_RESET_PASSWORD = "user.reset_password", "Сброс пароля"
        USER_IMPERSONATE = "user.impersonate", "Вход под пользователем"

    actor = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="platform_admin_audit_logs",
        verbose_name="Исполнитель",
    )
    action = models.CharField(max_length=64, choices=Action.choices, db_index=True, verbose_name="Действие")
    object_type = models.CharField(max_length=64, db_index=True, verbose_name="Тип объекта")
    object_id = models.CharField(max_length=64, db_index=True, verbose_name="ID объекта")
    company_id = models.CharField(max_length=64, null=True, blank=True, db_index=True, verbose_name="ID компании")
    payload = models.JSONField(default=dict, blank=True, verbose_name="Данные")
    ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="IP")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Создано")

    class Meta:
        verbose_name = "Аудит платформенной админки"
        verbose_name_plural = "Аудит платформенной админки"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action", "created_at"]),
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["company_id", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action} {self.object_type}:{self.object_id}"


class BranchMembership(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name="ID")

    user = models.ForeignKey("User", on_delete=models.CASCADE, related_name="branch_memberships", verbose_name="Сотрудник")
    branch = models.ForeignKey("Branch", on_delete=models.CASCADE, related_name="branch_memberships", verbose_name="Филиал")

    role = models.CharField(max_length=64, blank=True, null=True, verbose_name="Роль в филиале")
    is_primary = models.BooleanField(default=False, verbose_name="Основной филиал")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")

    class Meta:
        verbose_name = "Членство в филиале"
        verbose_name_plural = "Членства в филиалах"
        constraints = [
            models.UniqueConstraint(fields=["user", "branch"], name="uq_membership_user_branch"),
            # ✅ гарантируем: у одного юзера может быть только 1 primary membership
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(is_primary=True),
                name="uq_membership_user_primary_one",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "branch"]),
            models.Index(fields=["branch", "is_primary"]),
            models.Index(fields=["user", "is_primary"]),
        ]

    def __str__(self):
        return f"{self.user.email} → {self.branch}"

    def clean(self):
        if self.user and self.branch:
            if not self.user.company_id:
                raise ValidationError({"user": "У сотрудника не указана компания."})
            if self.user.company_id != self.branch.company_id:
                raise ValidationError("Сотрудник и филиал принадлежат разным компаниям.")


class ScaleDevice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name="ID")

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="scales", verbose_name="Компания")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="scales", verbose_name="Филиал")

    name = models.CharField(max_length=128, verbose_name="Название весов")
    ip_address = models.GenericIPAddressField(blank=True, null=True, verbose_name="IP агента/весов")

    is_active = models.BooleanField(default=True, verbose_name="Активны")
    last_seen_at = models.DateTimeField(blank=True, null=True, verbose_name="Последнее подключение")
    products_last_sync_at = models.DateTimeField(blank=True, null=True, verbose_name="Последняя загрузка товаров")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        verbose_name = "Весы"
        verbose_name_plural = "Весы"
        indexes = [
            models.Index(fields=["company", "is_active"]),
            models.Index(fields=["branch", "is_active"]),
            models.Index(fields=["last_seen_at"]),
        ]

    def __str__(self):
        if self.branch:
            return f"{self.company.name} / {self.branch.name} / {self.name}"
        return f"{self.company.name} / {self.name}"


class CompanyAddon(models.Model):
    """Платная функция компании (голос, лояльность и т.п.). Включается администратором платформы."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="addons")
    code = models.SlugField(max_length=64)
    active = models.BooleanField(default=True)
    until = models.DateField(null=True, blank=True, help_text="Пусто — бессрочно")
    note = models.CharField(max_length=255, blank=True, default="")
    # ТЗ Н-32: откуда функция — key (ключ), trial (пробный доступ), support (включила поддержка)
    source = models.CharField(max_length=16, default="support", db_default="support")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Платная функция компании"
        verbose_name_plural = "Платные функции компаний"
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uniq_company_addon_code"),
        ]

    def is_effective(self, today=None) -> bool:
        from django.utils import timezone

        today = today or timezone.localdate()
        return self.active and (self.until is None or self.until >= today)

    def __str__(self):
        return f"{self.company_id}: {self.code}"


class UserUiPreferences(models.Model):
    """
    Персональные настройки интерфейса пользователя (сайдбар, тема, кнопка кассы).
    Одна запись на пользователя (singleton per-user).
    """
    class ThemeMode(models.TextChoices):
        LIGHT = "light", "light"
        DARK = "dark", "dark"

    class ThemeDarkPalette(models.TextChoices):
        SOFT = "soft", "soft"
        CLASSIC = "classic", "classic"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        "users.User",
        on_delete=models.CASCADE,
        related_name="ui_preferences",
        verbose_name="Пользователь",
        db_index=True,
    )
    sidebar_auto_close = models.BooleanField(
        default=False,
        verbose_name="Автозакрытие сайдбара",
    )
    cashier_desktop_download = models.BooleanField(
        default=True,
        verbose_name="Кнопка кассы скачивает desktop",
    )
    theme_mode = models.CharField(
        max_length=16,
        choices=ThemeMode.choices,
        default=ThemeMode.LIGHT,
        verbose_name="Тема интерфейса",
    )
    theme_dark_palette = models.CharField(
        max_length=16,
        choices=ThemeDarkPalette.choices,
        default=ThemeDarkPalette.SOFT,
        verbose_name="Стиль тёмной темы",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        verbose_name = "Пользовательские настройки интерфейса"
        verbose_name_plural = "Пользовательские настройки интерфейса"

    def __str__(self):
        return f"UiPreferences ({self.user_id})"


# ---------------------------------------------------------------------------
# ТЗ ч.13, 1: настройки программ (касса, программа владельца) на сервере
# ---------------------------------------------------------------------------

class UserAppSettings(models.Model):
    """Личные настройки пользователя для программы app (nurmarket-kassa, nurmarket-owner, …)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="app_settings")
    app = models.CharField("Программа", max_length=64)
    settings = models.JSONField("Настройки", default=dict, blank=True)
    secrets = models.JSONField("Секреты (зашифрованы)", default=dict, blank=True)
    version = models.PositiveIntegerField("Версия", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройки программы пользователя"
        verbose_name_plural = "Настройки программ пользователей"
        constraints = [
            models.UniqueConstraint(fields=["user", "app"], name="uniq_user_app_settings"),
        ]

    def __str__(self):
        return f"{self.app} ({self.user_id})"


class CompanyAppSettings(models.Model):
    """Общие настройки программы app для компании (branch пусто) или филиала."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="app_settings")
    branch = models.ForeignKey(
        "users.Branch", on_delete=models.CASCADE, null=True, blank=True, related_name="app_settings"
    )
    app = models.CharField("Программа", max_length=64)
    settings = models.JSONField("Настройки", default=dict, blank=True)
    secrets = models.JSONField("Секреты (зашифрованы)", default=dict, blank=True)
    version = models.PositiveIntegerField("Версия", default=0)
    updated_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройки программы компании"
        verbose_name_plural = "Настройки программ компаний"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "app"], condition=Q(branch__isnull=True), name="uniq_company_app_settings"
            ),
            models.UniqueConstraint(
                fields=["company", "branch", "app"], condition=Q(branch__isnull=False),
                name="uniq_company_branch_app_settings",
            ),
        ]

    def __str__(self):
        return f"{self.app} ({self.company_id}{'/' + str(self.branch_id) if self.branch_id else ''})"


# ---------------------------------------------------------------------------
# ТЗ ч.13, 2: купленные функции — ключи активации
# ---------------------------------------------------------------------------

def _new_activation_key():
    import secrets as _s
    raw = _s.token_hex(8).upper()
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


class FeatureActivationKey(models.Model):
    """
    Ключ, который владелец вводит на кассе: включает функцию (code) компании на days дней
    (пусто — бессрочно). Ключ одноразовый — запоминается, какая компания его использовала.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField("Ключ", max_length=64, unique=True, default=_new_activation_key)
    code = models.SlugField("Код функции", max_length=64,
                            help_text="ai, clients, sales_analytics, restock, salary, debts, service, …")
    days = models.PositiveIntegerField("Дней", null=True, blank=True, help_text="Пусто — бессрочно")
    note = models.CharField(max_length=255, blank=True, default="")
    used_by_company = models.ForeignKey(
        Company, on_delete=models.SET_NULL, null=True, blank=True, related_name="used_activation_keys"
    )
    used_by_user = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ключ активации функции"
        verbose_name_plural = "Ключи активации функций"

    def __str__(self):
        return f"{self.key} → {self.code}"


class FeatureTrial(models.Model):
    """ТЗ Н-32: пробный доступ к функции — один раз на функцию для компании."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="feature_trials")
    code = models.SlugField(max_length=64)
    expires_at = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Пробный доступ к функции"
        verbose_name_plural = "Пробные доступы к функциям"
        constraints = [models.UniqueConstraint(fields=["company", "code"], name="uniq_feature_trial_company_code")]


class FeatureKeyAttempt(models.Model):
    """ТЗ Н-32: неверная попытка ввода ключа функции (защита от подбора, 10 в час)."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Неверная попытка ключа"
        verbose_name_plural = "Неверные попытки ключей"


class AdminPin(models.Model):
    """PIN подтверждения опасных действий (ТЗ п.10): хэш, счётчик неверных попыток, блокировка."""

    user = models.OneToOneField("users.User", on_delete=models.CASCADE, related_name="admin_pin")
    pin_hash = models.CharField(max_length=256)
    failed_attempts = models.PositiveSmallIntegerField(default=0, db_default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "PIN администратора"
        verbose_name_plural = "PIN администраторов"


class CompanyPinSetting(models.Model):
    """Настройка компании «Подтверждать PIN» для опасных действий."""

    company = models.OneToOneField("users.Company", on_delete=models.CASCADE, related_name="pin_setting")
    require_pin = models.BooleanField(default=False, db_default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройка PIN компании"
        verbose_name_plural = "Настройки PIN компаний"


class EmployeeAccessCode(models.Model):
    """ТЗ Н-16: код доступа сотрудника к действию (например, скидка/удаление чека) — хранится хэшем."""

    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="employee_access_codes")
    employee = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="access_codes")
    action = models.CharField(max_length=64)
    code_hash = models.CharField(max_length=256)
    updated_by = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Код доступа сотрудника"
        verbose_name_plural = "Коды доступа сотрудников"
        constraints = [
            models.UniqueConstraint(fields=["employee", "action"], name="uniq_employee_access_code_action"),
        ]
        indexes = [models.Index(fields=["company", "action"], name="ix_empcode_company_action")]


class ClientVersionSeen(models.Model):
    """
    Какие версии касс и мобилок ходят в API компании (единое ТЗ 26.10.2026, Б-01).
    Пишет core.middleware по заголовку X-NurMarket-Client не чаще раза в час на пару компания+клиент.
    По журналу решают, когда снимать переходный режим (флаг api_strict_2026_10, Б-02).
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="client_versions")
    client = models.CharField(max_length=40)  # «kassa/1.17.61», «mobile/1.18.5»; без заголовка — «none»
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()
    hits = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Версия клиента API"
        verbose_name_plural = "Версии клиентов API"
        constraints = [models.UniqueConstraint(fields=["company", "client"], name="uniq_company_client_version")]
        indexes = [models.Index(fields=["last_seen_at"])]

    def __str__(self):
        return f"{self.company_id}: {self.client}"
