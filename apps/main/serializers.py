from rest_framework import serializers
from django.db import transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.utils import timezone
from django.db.models import Q, Sum, F, ExpressionWrapper, DecimalField, Value as V, Prefetch, ProtectedError
from django.db.models.functions import Coalesce
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Dict, List
from datetime import date as _date, datetime as _datetime
from django.utils import timezone as dj_tz
from django.utils.dateparse import parse_datetime, parse_date

from apps.main.models import (
    SizeChart,
    Contact, Pipeline, Deal, Task, Integration, Analytics, Order, Product, Review,
    ProductInventorySession, ProductInventoryItem,
    Notification, Event, Warehouse, WarehouseEvent, ProductCategory, ProductBrand,
    OrderItem, Client, GlobalProduct, CartItem, ClientDeal, Bid, SocialApplications,
    TransactionRecord, DealInstallment, ContractorWork, Debt, DebtPayment,
    Sale,
    ObjectItem, ObjectSale, ObjectSaleItem, ItemMake, ManufactureSubreal, Acceptance,
    ReturnFromAgent, ProductImage, PromoRule, AgentRequestCart, AgentRequestItem,
    ProductPackage, ProductCharacteristics, DealPayment, AgentSaleAllocation,
    ProductRecipeItem, ProductPromotionTier, ProductAlternateBarcode, MarketSaleEmployeePayProfile,
    SupplierReceipt, SupplierReceiptItem, ProductExpiryBatch,
    SupplierReturn, SupplierReturnItem,
    KnowledgeBaseCourse, KnowledgeBaseLesson,
    FinishedToRawTransfer,
    Inventory, InventoryItem,
    StockShortageEvent,
    StockMovement,
    ProductionRecord,
    SupplierPurchase,
    PosPrinterSetting,
    MarketCustomerDisplaySettings,
)

from apps.consalting.models import ServicesConsalting
from apps.main.tz26.hw_prices.serializers import ProductPricesField as _HwProductPricesField, ProductVolumePricesField as _HwVolumePricesField
from apps.users.models import User, Company, Branch
from apps.utils import _is_owner_like


# ===========================
# Общие утилиты (STRICT branch)
# ===========================
def _company_from_ctx(serializer: serializers.Serializer):
    """
    Компания пользователя:
      - для суперюзера -> None (без ограничения по компании);
      - иначе owned_company или company.
    """
    req = serializer.context.get("request")
    user = getattr(req, "user", None) if req else None
    if not user or not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "is_superuser", False):
        return None
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _active_branch(serializer: serializers.Serializer):
    """
    Активный филиал:

      1) "жёстко" назначенный филиал пользователя
         (user.primary_branch() / user.primary_branch / user.branch / request.branch),
         если он принадлежит компании

      2) ?branch=<uuid> в запросе (если принадлежит компании и нет жёсткого филиала)

      3) None — нет филиала, работаем по всей компании (без фильтра по branch)
    """
    req = serializer.context.get("request")
    if not req:
        return None

    user = getattr(req, "user", None)
    company = getattr(user, "owned_company", None) or getattr(user, "company", None)
    company_id = getattr(company, "id", None)

    if not user or not getattr(user, "is_authenticated", False) or not company_id:
        return None

    # ----- 1. Жёстко назначенный филиал -----
    # 1a) user.primary_branch() как метод
    primary = getattr(user, "primary_branch", None)
    if callable(primary):
        try:
            val = primary()
            if val and getattr(val, "company_id", None) == company_id:
                setattr(req, "branch", val)
                return val
        except Exception:
            pass

    # 1b) user.primary_branch как атрибут
    if primary and not callable(primary) and getattr(primary, "company_id", None) == company_id:
        setattr(req, "branch", primary)
        return primary

    # 1c) user.branch
    if hasattr(user, "branch"):
        b = getattr(user, "branch")
        if b and getattr(b, "company_id", None) == company_id:
            setattr(req, "branch", b)
            return b

    # 1d) request.branch (если уже проставила middleware)
    if hasattr(req, "branch"):
        b = getattr(req, "branch")
        if b and getattr(b, "company_id", None) == company_id:
            return b

    # ----- 2. Разрешаем ?branch=... ТОЛЬКО если нет жёсткого филиала -----
    branch_id = None
    if hasattr(req, "query_params"):
        branch_id = req.query_params.get("branch")
    elif hasattr(req, "GET"):
        branch_id = req.GET.get("branch")

    if branch_id and str(branch_id).strip():
        branch_val = str(branch_id).strip()
        if branch_val.lower() == "main":
            setattr(req, "branch", None)
            return None
        try:
            from django.core.exceptions import ValidationError as DjangoValidationError
            from apps.users.models import Branch  # на случай круговой импорта
            br = Branch.objects.get(id=branch_val, company_id=company_id)
            setattr(req, "branch", br)
            return br
        except (Branch.DoesNotExist, ValueError, DjangoValidationError):
            pass

    # ----- 3. Глобальный режим по компании -----
    return None


def _restrict_pk_queryset_strict(field, base_qs, company, branch):
    """
    Было: если branch None -> показываем только branch__isnull=True.

    Теперь:
      - фильтруем по company (если есть поле company),
      - по branch фильтруем ТОЛЬКО если branch не None;
      - если branch is None -> не фильтруем по branch вообще.
    """
    if not field or base_qs is None or company is None:
        return
    qs = base_qs
    if hasattr(base_qs.model, "company"):
        qs = qs.filter(company=company)
    if hasattr(base_qs.model, "branch") and branch is not None:
        qs = qs.filter(branch=branch)
    field.queryset = qs


class ProductImageReadSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = ["id", "image_url", "alt", "is_primary", "color", "created_at"]
        read_only_fields = fields  # всё только на чтение тут

    def get_image_url(self, obj):
        """
        Делаем абсолютный URL, чтобы фронту было удобно.
        """
        request = self.context.get("request")
        if not obj.image:
            return None
        if request:
            return request.build_absolute_uri(obj.image.url)
        return obj.image.url


# ===========================
# Общий миксин: company/branch
# ===========================
class CompanyBranchReadOnlyMixin:
    """
    Делает company/branch read-only наружу и проставляет их из контекста на create/update.
    Правило:
      - есть активный филиал → branch = этот филиал
      - нет филиала → branch = NULL (глобально)
    """
    def _user(self):
        req = self.context.get("request")
        return getattr(req, "user", None) if req else None

    def _user_company(self):
        u = self._user()
        return getattr(u, "company", None) or getattr(u, "owned_company", None)

    def _auto_branch(self):
        return _active_branch(self)

    def create(self, validated_data):
        company = self._user_company()
        if company:
            validated_data.setdefault("company", company)
        if "branch" in getattr(self.Meta, "fields", []):
            validated_data["branch"] = self._auto_branch()
        return super().create(validated_data)

    def update(self, instance, validated_data):
        company = self._user_company()
        if company:
            validated_data["company"] = company
        if "branch" in getattr(self.Meta, "fields", []):
            validated_data["branch"] = self._auto_branch()
        return super().update(instance, validated_data)


# ===========================
# Простые справочники без company/branch
# ===========================
class SocialApplicationsSerializers(serializers.ModelSerializer):
    class Meta:
        model = SocialApplications
        fields = ['id', 'company', 'text', 'status', 'created_at']


class BidSerializers(serializers.ModelSerializer):
    class Meta:
        model = Bid
        fields = ['id', 'full_name', 'phone', 'text', 'status', 'created_at']


class BidPublicCreateSerializer(serializers.ModelSerializer):
    """Публичная заявка с лендинга — без авторизации."""

    class Meta:
        model = Bid
        fields = ['id', 'full_name', 'phone', 'text', 'created_at']
        read_only_fields = ['id', 'created_at']


# Ограничения на файл превью урока (согласованы с фронтом).
LESSON_THUMBNAIL_MAX_BYTES = 5 * 1024 * 1024
LESSON_THUMBNAIL_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")


class LessonThumbnailField(serializers.Field):
    """
    Одно поле `thumbnail` на два модельных: файл (`thumbnail`) и ссылку (`thumbnail_url`).

    Приём:
      - файл          -> сохраняем в thumbnail, ссылку чистим;
      - строка-URL    -> сохраняем в thumbnail_url, файл чистим;
      - "" или null   -> явная очистка обоих;
      - поле не пришло -> значение не трогаем (важно для PATCH без файла).

    Отдача: абсолютный URL файла, иначе ссылка, иначе "".
    """

    default_error_messages = {
        "invalid": "Передайте файл изображения или ссылку на него.",
        "too_large": "Файл превью больше 5 МБ.",
        "bad_type": "Допустимые форматы превью: JPEG, PNG, WebP, GIF.",
    }

    def __init__(self, **kwargs):
        kwargs.setdefault("source", "*")
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)
        self._image_field = serializers.ImageField()

    def to_representation(self, lesson):
        file = getattr(lesson, "thumbnail", None)
        if file:
            request = self.context.get("request")
            return request.build_absolute_uri(file.url) if request else file.url
        return getattr(lesson, "thumbnail_url", "") or ""

    def to_internal_value(self, data):
        if isinstance(data, (list, tuple)):
            data = data[0] if data else ""

        # Явная очистка превью
        if data is None or (isinstance(data, str) and not data.strip()):
            return {"thumbnail": None, "thumbnail_url": ""}

        # Загруженный файл
        if hasattr(data, "read"):
            content_type = (getattr(data, "content_type", "") or "").lower()
            if content_type and content_type not in LESSON_THUMBNAIL_CONTENT_TYPES:
                self.fail("bad_type")
            if (getattr(data, "size", 0) or 0) > LESSON_THUMBNAIL_MAX_BYTES:
                self.fail("too_large")
            # Проверяем, что это действительно изображение (нужен Pillow).
            # DRF ImageField пробрасывает наружу Django-ошибку — приводим её к DRF,
            # чтобы поле возвращало корректный 400 независимо от контекста вызова.
            try:
                file = self._image_field.to_internal_value(data)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(list(exc.messages))
            return {"thumbnail": file, "thumbnail_url": ""}

        # Ссылка на изображение
        if isinstance(data, str):
            value = data.strip()
            try:
                URLValidator()(value)
            except DjangoValidationError:
                self.fail("invalid")
            if len(value) > 500:
                raise serializers.ValidationError("Ссылка на превью длиннее 500 символов.")
            return {"thumbnail": None, "thumbnail_url": value}

        self.fail("invalid")


class PublicKnowledgeBaseLessonSerializer(serializers.ModelSerializer):
    # id приходит с фронта при обновлении существующего урока, поэтому он
    # writable, но необязателен: без него урок считается новым.
    id = serializers.UUIDField(required=False)
    thumbnail = LessonThumbnailField()

    class Meta:
        model = KnowledgeBaseLesson
        fields = ["id", "title", "description", "url", "thumbnail", "order", "created_at"]
        read_only_fields = ["order", "created_at"]


class PublicKnowledgeBaseCourseSerializer(serializers.ModelSerializer):
    lessons = PublicKnowledgeBaseLessonSerializer(many=True)

    class Meta:
        model = KnowledgeBaseCourse
        fields = ["id", "title", "lessons", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_lessons(self, value):
        if not value:
            raise serializers.ValidationError("Добавьте хотя бы один урок.")
        return value

    def validate_title(self, value):
        qs = KnowledgeBaseCourse.objects.filter(title=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Курс с таким названием уже есть.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        lessons_data = validated_data.pop("lessons")
        course = KnowledgeBaseCourse.objects.create(**validated_data)
        for index, lesson_data in enumerate(lessons_data):
            lesson_data.pop("id", None)
            # .save() вместо bulk_create: нужен корректный сейв файла превью
            KnowledgeBaseLesson(course=course, order=index, **lesson_data).save()
        return course

    @transaction.atomic
    def update(self, instance, validated_data):
        lessons_data = validated_data.pop("lessons", None)

        update_fields = []
        if "title" in validated_data:
            instance.title = validated_data["title"]
            update_fields.extend(["title", "updated_at"])

        # lessons отсутствует в теле (PATCH только с названием) — уроки не трогаем
        if lessons_data is not None:
            self._replace_lessons(instance, lessons_data)
            if "updated_at" not in update_fields:
                update_fields.append("updated_at")

        if update_fields:
            instance.save(update_fields=update_fields)
        return instance

    @staticmethod
    def _replace_lessons(course, lessons_data):
        """
        Полная замена списка уроков с сохранением уже загруженных превью.

        Урок из тела сопоставляется с существующим по `id`, а если фронт его не
        прислал — по совпадению `url`. Найденный урок обновляется на месте, так
        что превью, которое не перезагружали, остаётся. Не сопоставленные уроки
        удаляются вместе со своими файлами.
        """
        existing = list(course.lessons.all())
        by_id = {str(lesson.id): lesson for lesson in existing}
        by_url = {}
        for lesson in existing:
            by_url.setdefault(lesson.url, lesson)

        matched_ids = set()

        for index, lesson_data in enumerate(lessons_data):
            data = dict(lesson_data)
            lesson_id = data.pop("id", None)

            lesson = None
            if lesson_id is not None:
                candidate = by_id.get(str(lesson_id))
                if candidate is not None and str(candidate.id) not in matched_ids:
                    lesson = candidate
            if lesson is None:
                candidate = by_url.get(data.get("url"))
                if candidate is not None and str(candidate.id) not in matched_ids:
                    lesson = candidate

            if lesson is None:
                KnowledgeBaseLesson(course=course, order=index, **data).save()
                continue

            matched_ids.add(str(lesson.id))

            # Файл превью заменяем только если поле реально пришло в запросе
            if "thumbnail" in data or "thumbnail_url" in data:
                new_file = data.get("thumbnail")
                if lesson.thumbnail and lesson.thumbnail != new_file:
                    lesson.thumbnail.delete(save=False)

            for field, value in data.items():
                setattr(lesson, field, value)
            lesson.order = index
            lesson.save()

        for lesson in existing:
            if str(lesson.id) in matched_ids:
                continue
            if lesson.thumbnail:
                lesson.thumbnail.delete(save=False)
            lesson.delete()


# ===========================
# ProductCategory / ProductBrand (STRICT)
# ===========================
class ProductCategorySerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    parent = serializers.PrimaryKeyRelatedField(
        queryset=ProductCategory.objects.all(),
        allow_null=True,
        required=False
    )

    class Meta:
        model = ProductCategory
        fields = ['id', 'company', 'branch', 'name', 'parent']
        read_only_fields = ['id', 'company', 'branch']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        _restrict_pk_queryset_strict(self.fields.get("parent"), ProductCategory.objects.all(), comp, br)

    def validate(self, attrs):
        """
        Превращаем DB IntegrityError (unique constraint) в нормальную 400-ошибку.
        Повторяем логику constraints из модели:
          - если branch != NULL -> уникальность (branch, name)
          - если branch == NULL -> уникальность (company, name) среди branch IS NULL
        """
        attrs = super().validate(attrs)

        name = attrs.get("name")
        if name is None:
            return attrs

        name = name.strip()
        attrs["name"] = name

        company = self._user_company()
        branch = self._auto_branch()

        qs = ProductCategory.objects.all()
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)

        if branch is not None:
            qs = qs.filter(branch=branch, name=name)
        else:
            # глобальная категория (branch is NULL) — уникальна в рамках компании
            if company is not None:
                qs = qs.filter(company=company, branch__isnull=True, name=name)
            else:
                qs = qs.filter(branch__isnull=True, name=name)

        if qs.exists():
            raise serializers.ValidationError({"name": "Категория с таким названием уже существует."})

        return attrs


class ProductBrandSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    parent = serializers.PrimaryKeyRelatedField(
        queryset=ProductBrand.objects.all(),
        allow_null=True,
        required=False
    )

    class Meta:
        model = ProductBrand
        fields = ['id', 'company', 'branch', 'name', 'parent']
        read_only_fields = ['id', 'company', 'branch']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        _restrict_pk_queryset_strict(self.fields.get("parent"), ProductBrand.objects.all(), comp, br)

    def validate(self, attrs):
        """
        Аналогично ProductCategorySerializer.validate(), но для брендов.
        """
        attrs = super().validate(attrs)

        name = attrs.get("name")
        if name is None:
            return attrs

        name = name.strip()
        attrs["name"] = name

        company = self._user_company()
        branch = self._auto_branch()

        qs = ProductBrand.objects.all()
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)

        if branch is not None:
            qs = qs.filter(branch=branch, name=name)
        else:
            if company is not None:
                qs = qs.filter(company=company, branch__isnull=True, name=name)
            else:
                qs = qs.filter(branch__isnull=True, name=name)

        if qs.exists():
            raise serializers.ValidationError({"name": "Бренд с таким названием уже существует."})

        return attrs


# ===========================
# Contact / Pipeline / Deal / Task
# ===========================
class ContactSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    owner = serializers.ReadOnlyField(source='owner.id')
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    class Meta:
        ref_name = "MainContactSerializer"
        model = Contact
        fields = [
            'id', 'company', 'branch',
            'name', 'email', 'phone', 'address', 'client_company',
            'notes', 'department', 'created_at', 'updated_at',
            'owner'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'owner', 'company', 'branch']

    def create(self, validated_data):
        validated_data['owner'] = self.context['request'].user
        return super().create(validated_data)


class PipelineSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    owner = serializers.ReadOnlyField(source='owner.id')
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    class Meta:
        model = Pipeline
        fields = ['id', 'company', 'branch', 'name', 'stages', 'created_at', 'updated_at', 'owner']
        read_only_fields = ['id', 'created_at', 'updated_at', 'owner', 'company', 'branch']

    def create(self, validated_data):
        validated_data['owner'] = self.context['request'].user
        return super().create(validated_data)


class DealSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    pipeline = serializers.PrimaryKeyRelatedField(queryset=Pipeline.objects.all())
    contact = serializers.PrimaryKeyRelatedField(queryset=Contact.objects.all())
    assigned_to = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True)

    class Meta:
        ref_name = "MainDealSerializer"
        model = Deal
        fields = [
            'id', 'company', 'branch',
            'title', 'value', 'status',
            'pipeline', 'stage', 'contact', 'assigned_to',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'company', 'branch']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        _restrict_pk_queryset_strict(self.fields.get("pipeline"), Pipeline.objects.all(), comp, br)
        _restrict_pk_queryset_strict(self.fields.get("contact"), Contact.objects.all(), comp, br)
        if comp and self.fields.get("assigned_to"):
            self.fields["assigned_to"].queryset = User.objects.filter(company=comp)

    def validate(self, attrs):
        user = self.context['request'].user
        company = user.company
        branch = self._auto_branch()

        pipeline = attrs.get('pipeline') or getattr(self.instance, "pipeline", None)
        contact = attrs.get('contact') or getattr(self.instance, "contact", None)
        assigned_to = attrs.get('assigned_to') or getattr(self.instance, "assigned_to", None)

        if pipeline and pipeline.company != company:
            raise serializers.ValidationError({"pipeline": "Воронка принадлежит другой компании."})
        if contact and contact.company != company:
            raise serializers.ValidationError({"contact": "Контакт принадлежит другой компании."})
        if assigned_to and assigned_to.company != company:
            raise serializers.ValidationError({"assigned_to": "Ответственный не из вашей компании."})

        # STRICT: филиал должен совпадать с активным, если он есть
        if branch is not None:
            if pipeline and pipeline.branch_id != branch.id:
                raise serializers.ValidationError({"pipeline": "Воронка другого филиала."})
            if contact and contact.branch_id != branch.id:
                raise serializers.ValidationError({"contact": "Контакт другого филиала."})
        # если branch is None — не проверяем филиал (видим всю компанию)
        return attrs


class TaskSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    assigned_to = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True)
    deal = serializers.PrimaryKeyRelatedField(queryset=Deal.objects.all(), allow_null=True, required=False)

    class Meta:
        model = Task
        fields = [
            'id', 'company', 'branch',
            'title', 'description', 'due_date', 'status',
            'assigned_to', 'deal',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'company', 'branch']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        if comp and self.fields.get("assigned_to"):
            self.fields["assigned_to"].queryset = User.objects.filter(company=comp)
        _restrict_pk_queryset_strict(self.fields.get("deal"), Deal.objects.all(), comp, br)


# ===========================
# Integration / Analytics
# ===========================
class IntegrationSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    class Meta:
        model = Integration
        fields = ['id', 'company', 'branch', 'type', 'config', 'status', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at', 'company', 'branch']


class AnalyticsSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    class Meta:
        model = Analytics
        fields = ['id', 'company', 'branch', 'type', 'data', 'created_at']
        read_only_fields = ['id', 'created_at', 'company', 'branch']


# ===========================
# Order / OrderItem
# ===========================
class OrderItemSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = OrderItem
        fields = ['id', 'product', 'quantity', 'price', 'total']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        req = self.context.get("request")
        user = getattr(req, "user", None) if req else None
        comp = (
            getattr(user, "company", None)
            or getattr(user, "owned_company", None)
            or getattr(getattr(user, "branch", None), "company", None)
        )
        br = _active_branch(self)
        if comp and self.fields.get("product"):
            qs = Product.objects.filter(company=comp)
            if br is not None:
                qs = qs.filter(branch=br)
            # если br is None — оставляем все товары компании, независимо от филиала
            self.fields["product"].queryset = qs

    def validate(self, data):
        product = data['product']
        quantity = data['quantity']
        if product.quantity < quantity:
            raise serializers.ValidationError(
                f"Недостаточно товара на складе для '{product.name}'. Доступно: {product.quantity}"
            )
        return data

    def create(self, validated_data):
        product = validated_data['product']
        quantity = validated_data['quantity']
        price = product.price
        total = price * quantity

        product.quantity -= quantity
        product.save()

        order = validated_data['order']
        company = order.company
        branch = getattr(order, "branch", None)

        return OrderItem.objects.create(
            **validated_data,
            company=company,
            branch=branch,
            price=price,
            total=total
        )


class OrderSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')
    items = OrderItemSerializer(many=True)

    total = serializers.SerializerMethodField()
    total_quantity = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'company', 'branch',
            'order_number', 'customer_name', 'date_ordered',
            'status', 'phone', 'department',
            'created_at', 'updated_at',
            'items', 'total', 'total_quantity'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'company', 'branch', 'total', 'total_quantity']

    def get_total(self, obj):
        return obj.total

    def get_total_quantity(self, obj):
        return sum((it.quantity for it in obj.items.all()), 0)

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        order = super().create(validated_data)
        for item_data in items_data:
            item_data['order'] = order
            OrderItemSerializer(context=self.context).create(item_data)
        return order


# ===========================
# ItemMake / Product
# ===========================
class ItemMakeNestedSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    source = serializers.UUIDField(source="source_id", read_only=True, allow_null=True)
    source_name = serializers.CharField(source="source.name", read_only=True, allow_null=True)

    class Meta:
        model = ItemMake
        fields = [
            "id", "name", "price", "unit", "quantity",
            "kind", "kind_display", "source", "source_name",
            "needs_processing",
        ]


class ProductImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = ["id", "image", "image_url", "alt", "is_primary", "color", "created_at"]
        read_only_fields = ["id", "image_url", "created_at"]

    def get_image_url(self, obj):
        req = self.context.get("request")
        if obj.image and hasattr(obj.image, "url"):
            return req.build_absolute_uri(obj.image.url) if req else obj.image.url
        return None
    
class ProductCharacteristicsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCharacteristics
        fields = [
            "id",
            "height_cm",
            "width_cm",
            "depth_cm",
            "factual_weight_kg",
            "description",
        ]
        read_only_fields = ["id"]


class ProductPackageSerializer(serializers.ModelSerializer):
    piece_unit_price = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=Decimal("0"),
    )

    class Meta:
        model = ProductPackage
        fields = [
            "id",
            "name",
            "quantity_in_package",
            "unit",
            "piece_unit_price",
        ]
        read_only_fields = ["id"]


MAX_PRODUCT_PROMOTION_TIERS = 30


def _package_row_piece_unit_price(pkg) -> object:
    """Достаёт piece_unit_price из строки packages_input; None / пустая строка → «не задано»."""
    if not hasattr(pkg, "get"):
        raise serializers.ValidationError(
            {"packages_input": "Каждый элемент packages_input должен быть объектом с полями упаковки."}
        )
    pup = pkg.get("piece_unit_price")
    if pup in (None, ""):
        return None
    if isinstance(pup, str) and not str(pup).strip():
        return None
    return pup


class ProductPromotionTierSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductPromotionTier
        fields = ["id", "position", "min_amount", "discount_percent", "promo_quantity"]
        read_only_fields = ["id"]


def parse_product_promotion_tiers_payload(raw):
    """Парсит JSON-массив ступеней акции; пустой список или None → []."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise serializers.ValidationError({"promotion_rules_input": "Ожидается массив правил акции."})
    if len(raw) > MAX_PRODUCT_PROMOTION_TIERS:
        raise serializers.ValidationError({
            "promotion_rules_input": f"Не более {MAX_PRODUCT_PROMOTION_TIERS} ступеней на один товар.",
        })
    out = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise serializers.ValidationError(
                {"promotion_rules_input": f"Элемент #{i + 1}: ожидается объект."}
            )
        min_raw = row.get("min_amount")
        disc_raw = row.get("discount_percent")
        pq_raw = row.get("promo_quantity")
        pos = row.get("position", i)
        try:
            min_amount = Decimal(str(min_raw)) if min_raw is not None and min_raw != "" else None
        except Exception:
            raise serializers.ValidationError(
                {"promotion_rules_input": f"Элемент #{i + 1}: min_amount — неверное число."}
            )
        try:
            discount_percent = Decimal(str(disc_raw)) if disc_raw is not None and disc_raw != "" else None
        except Exception:
            raise serializers.ValidationError(
                {"promotion_rules_input": f"Элемент #{i + 1}: discount_percent — неверное число."}
            )
        pq = None
        if pq_raw is not None and pq_raw != "":
            try:
                pq = int(pq_raw)
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"promotion_rules_input": f"Элемент #{i + 1}: promo_quantity — целое число ≥ 1."}
                )
            if pq < 1:
                raise serializers.ValidationError(
                    {"promotion_rules_input": f"Элемент #{i + 1}: promo_quantity — целое число ≥ 1."}
                )
        if min_amount is None or min_amount < 0:
            raise serializers.ValidationError(
                {"promotion_rules_input": f"Элемент #{i + 1}: укажите min_amount ≥ 0."}
            )
        if discount_percent is None or discount_percent <= 0 or discount_percent > 100:
            raise serializers.ValidationError(
                {"promotion_rules_input": f"Элемент #{i + 1}: discount_percent от 0.01 до 100."}
            )
        try:
            pos_i = int(pos)
        except Exception:
            pos_i = i
        out.append(
            {
                "position": max(0, min(pos_i, 32767)),
                "min_amount": min_amount,
                "discount_percent": discount_percent,
                "promo_quantity": pq,
            }
        )
    return out


def sync_product_promotion_tiers(product, raw, *, stock_enabled: bool, partial: bool = False):
    """
    partial=True и raw is None — не трогаем ступени (PATCH без promotion_rules_input).
    """
    if partial and raw is None:
        if not stock_enabled:
            ProductPromotionTier.objects.filter(product=product).delete()
        return
    ProductPromotionTier.objects.filter(product=product).delete()
    if not stock_enabled:
        return
    rows = parse_product_promotion_tiers_payload(raw if raw is not None else [])
    if not rows:
        raise serializers.ValidationError({
            "promotion_rules_input": (
                "Для акционного товара укажите хотя бы одну ступень: "
                "min_amount (сумма строки от), discount_percent (%), при необходимости promo_quantity (лимит шт.)."
            ),
        })
    ProductPromotionTier.objects.bulk_create(
        [
            ProductPromotionTier(
                product=product,
                position=r["position"],
                min_amount=r["min_amount"],
                discount_percent=r["discount_percent"],
                promo_quantity=r["promo_quantity"],
            )
            for r in rows
        ]
    )


def sync_product_alternate_barcodes(product: Product, raw):
    """
    Полная замена списка доп. штрихкодов для товара.
    raw — list[str | dict] | None (пустой список очищает).
    """
    from django.core.cache import cache

    company_id = product.company_id
    main = (product.barcode or "").strip()
    seen = set()
    norm = []
    for x in raw or []:
        if isinstance(x, dict):
            b = str(x.get("barcode") or "").strip()
            n = str(x.get("name") or "").strip()
            raw_qty = x.get("quantity")
        else:
            b = str(x or "").strip()
            n = ""
            raw_qty = None
        if not b:
            continue
        if b in seen:
            raise serializers.ValidationError({
                "alternate_barcodes": f"Дубликат в списке: {b}.",
            })
        seen.add(b)

        qty = None
        if raw_qty is not None and str(raw_qty).strip() != "":
            try:
                val = float(raw_qty)
                if val <= 0 or not val.is_integer():
                    raise ValueError()
                qty = int(val)
            except (ValueError, TypeError):
                raise serializers.ValidationError({
                    "alternate_barcodes": f"Количество в доп. штрихкоде '{b}' должно быть положительным числом."
                })

        norm.append((b, n, qty))

    for b, n, _ in norm:
        if main and b == main:
            raise serializers.ValidationError({
                "alternate_barcodes": f"Доп. штрихкод «{b}» совпадает с основным штрихкодом товара.",
            })
        other = Product.objects.filter(company_id=company_id, barcode=b).exclude(pk=product.pk).only("name", "code").first()
        if other is not None:
            code = f" (код {other.code})" if other.code else ""
            raise serializers.ValidationError({
                "alternate_barcodes": f"Штрихкод «{b}» уже есть у товара «{other.name}»{code} как основной.",
            })
        alt = ProductAlternateBarcode.objects.filter(company_id=company_id, barcode=b).exclude(
            product_id=product.pk
        ).select_related("product").first()
        if alt is not None:
            code = f" (код {alt.product.code})" if alt.product.code else ""
            raise serializers.ValidationError({
                "alternate_barcodes": f"Штрихкод «{b}» уже есть у товара «{alt.product.name}»{code} как дополнительный.",
            })

    old_codes = list(product.alternate_barcodes.values_list("barcode", flat=True))
    product.alternate_barcodes.all().delete()
    if norm:
        ProductAlternateBarcode.objects.bulk_create(
            [
                ProductAlternateBarcode(product=product, company_id=company_id, barcode=b, name=n, quantity=q)
                for b, n, q in norm
            ]
        )
    for old in old_codes:
        if old:
            cache.delete(f"product_barcode:{company_id}:{old}")
    for b, _, _ in norm:
        cache.delete(f"product_barcode:{company_id}:{b}")


class RecipeItemSerializer(serializers.Serializer):
    """Read/write сериализатор для одной позиции рецепта."""
    id = serializers.CharField(help_text="item_make.id")
    qty_per_unit = serializers.DecimalField(max_digits=12, decimal_places=3)
    name = serializers.CharField(read_only=True, required=False)


class RecipeItemReadSerializer(serializers.ModelSerializer):
    """Read-only представление позиции рецепта для ответа API."""
    id = serializers.CharField(source="item_make_id")
    name = serializers.CharField(source="item_make.name", read_only=True)
    qty_per_unit = serializers.DecimalField(max_digits=12, decimal_places=3)
    unit = serializers.CharField(source="item_make.unit", read_only=True)
    unit_price = serializers.DecimalField(source="item_make.price", max_digits=10, decimal_places=2, read_only=True)
    line_cost = serializers.SerializerMethodField()

    class Meta:
        model = ProductRecipeItem
        fields = ["id", "name", "qty_per_unit", "unit", "unit_price", "line_cost"]
        read_only_fields = fields

    def get_line_cost(self, obj):
        qty = Decimal(str(obj.qty_per_unit or 0))
        price = Decimal(str(getattr(obj.item_make, "price", 0) or 0))
        return (qty * price).quantize(Decimal("0.01"))


class ProductSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    # ====== базовые поля компании/филиала ======
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    # ====== бренд / категория ======
    brand = serializers.CharField(source="brand.name", read_only=True)
    category = serializers.CharField(source="category.name", read_only=True)

    brand_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    category_name = serializers.CharField(write_only=True, required=False, allow_blank=True)

    # ====== кто создал ======
    created_by = serializers.ReadOnlyField(source="created_by.id")
    created_by_name = serializers.SerializerMethodField(read_only=True)

    # ====== единицы товара ======
    item_make = ItemMakeNestedSerializer(many=True, read_only=True)
    item_make_ids = serializers.PrimaryKeyRelatedField(
        queryset=ItemMake.objects.all(),
        many=True,
        write_only=True,
        required=False,
    )

    # ====== рецепт (сырьё + расход на 1 ед.) ======
    recipe = RecipeItemReadSerializer(source="recipe_items", many=True, read_only=True)

    # ТЗ Н-33: таблица размеров товара (id из showcase/size-charts/)
    size_chart = serializers.PrimaryKeyRelatedField(
        queryset=SizeChart.objects.all(), required=False, allow_null=True
    )

    def validate_size_chart(self, value):
        if value is not None:
            req = self.context.get("request")
            cid = getattr(getattr(req, "user", None), "company_id", None) or getattr(
                getattr(getattr(req, "user", None), "owned_company", None), "id", None
            )
            if cid and value.company_id != cid:
                raise serializers.ValidationError("Таблица размеров другой компании.")
        return value

    # ТЗ 26.10 Б-55: цены по уровням и от количества (логика — apps/main/tz26/hw_prices)
    prices = _HwProductPricesField(required=False)
    volume_prices = _HwVolumePricesField(required=False)

    def save(self, **kwargs):
        from apps.main.tz26.hw_prices.services import product_serializer_save
        return product_serializer_save(self, super().save, **kwargs)

    # ====== клиент ======
    client = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(),
        required=False,
        allow_null=True,
    )
    client_name = serializers.CharField(source="client.full_name", read_only=True)
    supplier_ids = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(),
        many=True,
        required=False,
        write_only=True,
    )
    suppliers = serializers.SerializerMethodField(read_only=True)

    # ====== статус ======
    status = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    # ====== дата (как и раньше) ======
    date = serializers.SerializerMethodField(read_only=True)

    # ====== картинки ======
    images = ProductImageSerializer(many=True, read_only=True)

    stock = serializers.BooleanField(required=False)
    is_favorite = serializers.BooleanField(read_only=True)

    promotion_rules = ProductPromotionTierSerializer(
        many=True, read_only=True, source="promotion_tiers"
    )
    promotion_rules_input = serializers.ListField(
        child=serializers.DictField(allow_empty=True),
        write_only=True,
        required=False,
        allow_null=True,
    )
    alternate_barcodes = serializers.ListField(
        write_only=True,
        required=False,
        allow_null=True,
    )

    # ====== новые поля модели ======
    code = serializers.CharField(read_only=True)  # генерится в модели
    article = serializers.CharField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    unit = serializers.CharField(required=False, allow_blank=True)
    is_weight = serializers.BooleanField(required=False)
    is_adult = serializers.BooleanField(required=False, default=False)

    # allow_null=True чтобы PATCH мог "очищать" значения
    purchase_price = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        allow_null=True,
        help_text=(
            "За учётную единицу (как quantity на складе). Для товара «пачка» — за пачку; "
            "при поштучной продаже через packages + sale_package закуп за шт. = это значение / quantity_in_package."
        ),
    )
    # model.Product.markup_percent имеет decimal_places=4
    markup_percent = serializers.DecimalField(max_digits=12, decimal_places=4, required=False, allow_null=True)
    price = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        allow_null=True,
        help_text=(
            "Розница за учётную единицу (как quantity). Для пачки сигарет — цена пачки; "
            "на кассе за штуку по умолчанию = price / quantity_in_package выбранной упаковки."
        ),
    )
    discount_percent = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    # Б-06: пересчитать цену продажи из закупки и наценки (иначе PATCH purchase_price цену не трогает).
    recalc_price = serializers.BooleanField(required=False, default=False, write_only=True)

    country = serializers.CharField(required=False, allow_blank=True)
    expiration_date = serializers.DateField(required=False, allow_null=True)
    shelf_life_days = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    expiry_batches = serializers.SerializerMethodField(read_only=True)

    # ==== ПЛУ ====
    plu = serializers.IntegerField(required=False, allow_null=True)

    # ==== ДАННЫЕ С ВЕСОВ ====
    weight_kg = serializers.SerializerMethodField(read_only=True)
    total_price = serializers.SerializerMethodField(read_only=True)

    # ==== История закупок (партии) ====
    # Отдаётся только в детальном просмотре товара (флаг include_purchase_batches
    # в контексте). В списках поле убирается в __init__, чтобы не ловить N+1.
    purchase_batches = serializers.SerializerMethodField(read_only=True)

    # ==== связанные модели ====
    characteristics = ProductCharacteristicsSerializer(read_only=True)

    # READ-ONLY — то, что отдаём фронту
    packages = ProductPackageSerializer(many=True, read_only=True)
    # WRITE-ONLY — то, что принимаем с фронта
    packages_input = ProductPackageSerializer(many=True, write_only=True, required=False)

    kind = serializers.ChoiceField(
        choices=Product.Kind.choices,
        required=False,
        default=Product.Kind.PRODUCT,
    )

    hotkey_group = serializers.ChoiceField(
        choices=Product.HotkeyGroup.choices,
        required=False,
        allow_null=True,
        allow_blank=True,
    )
    # Без default: не переданное поле не сбрасывается при PUT; при создании — default модели.
    upsell_priority = serializers.IntegerField(required=False, min_value=0, max_value=100)
    upsell_excluded = serializers.BooleanField(required=False)
    cashflows = serializers.SerializerMethodField(read_only=True)
    # ТЗ 26.10 Б-53: единицы продажи/закупки (запись — полной заменой списка)
    sale_units = serializers.SerializerMethodField(read_only=True)
    sale_units_input = serializers.ListField(child=serializers.DictField(), write_only=True, required=False)

    def get_sale_units(self, obj):
        from apps.main.tz26.hw_units.units import serialize_sale_units

        return serialize_sale_units(obj)

    class Meta:
        model = Product
        fields = [
            "id", "company", "branch",
            "kind",
            "hotkey_group",
            "upsell_priority",
            "upsell_excluded",
            "code", "article",
            "name", "description", "barcode",
            "brand", "brand_name",
            "category", "category_name",
            "unit", "is_weight", "is_adult",
            "item_make", "item_make_ids",
            "recipe",
            "quantity",
            "minimum_quantity",
            "purchase_price",
            "markup_percent",
            "price",
            "recalc_price",
            "wholesale_price",
            "discount_percent",
            "plu",
            "duration_min",
            "performer_commission_percent",
            "country",
            "expiration_date",
            "shelf_life_days", "expiry_batches",
            "status", "status_display",
            "client", "client_name",
            "supplier_ids", "suppliers",
            "stock", "promotion_rules", "promotion_rules_input",
            "alternate_barcodes",
            "date",
            "created_by", "created_by_name",
            "created_at", "updated_at",
            "is_favorite",
            "images",
            "characteristics",
            "packages",
            "packages_input",
            "weight_kg",
            "total_price",
            "purchase_batches",
            "cashflows",
            "size_chart",  # ТЗ Н-33
            "unit_code", "quantity_step", "min_sale_quantity", "quantity_decimals",  # ТЗ 26.10 Б-52
            "sale_units", "sale_units_input",  # ТЗ 26.10 Б-53
            "prices", "volume_prices",  # ТЗ 26.10 Б-55
        ]

        read_only_fields = [
            "id", "created_at", "updated_at",
            "company", "branch",
            "brand", "category",
            "client_name", "status_display",
            "item_make", "recipe", "date",
            "created_by", "created_by_name",
            "images",
            "code",
            "characteristics",
            "packages",
            "promotion_rules",
            "weight_kg", "total_price",
            "purchase_batches",
            "cashflows",
        ]

    def get_cashflows(self, obj):
        # В списке товаров вью кладёт в контекст готовую карту по всей странице
        # (один запрос вместо запроса на каждый товар — было ~100 на страницу).
        batch = self.context.get("cashflows_by_product")
        if batch is not None:
            return batch.get(str(obj.id), [])

        from apps.construction.models import CashFlow
        from apps.construction.auto_cashflow import serialize_auto_cashflows
        cfs = CashFlow.objects.filter(company_id=obj.company_id, source_id=str(obj.id))
        return serialize_auto_cashflows(cfs)
        extra_kwargs = {
            "kind": {"required": False, "default": Product.Kind.PRODUCT},
            "purchase_price": {"required": False, "default": 0, "allow_null": True},
            "markup_percent": {"required": False, "default": 0, "allow_null": True},
            "discount_percent": {"required": False, "default": 0, "allow_null": True},
            "quantity": {"required": False, "default": 0},
            "unit": {"required": False, "default": "шт."},
            "is_weight": {"required": False, "default": False},
            "is_adult": {"required": False, "default": False},
            "price": {"required": False, "allow_null": True},
            "description": {"required": False, "allow_blank": True, "allow_null": True},
        }

    def get_expiry_batches(self, obj):
        # Если партии уже загружены prefetch-ем — фильтруем в памяти, иначе
        # .filter() на менеджере сделал бы отдельный запрос на каждый товар.
        cached = obj.__dict__.get("_prefetched_objects_cache", {}).get("expiry_batches")
        if cached is not None:
            batches = [
                b for b in cached
                if b.status == ProductExpiryBatch.Status.ACTIVE
                and (b.remaining_quantity or 0) > 0
            ]
            batches.sort(key=lambda b: (b.expires_at is None, b.expires_at, b.received_at))
            batches = batches[:10]
        else:
            batches = obj.expiry_batches.filter(
                status=ProductExpiryBatch.Status.ACTIVE, remaining_quantity__gt=0
            ).order_by(F("expires_at").asc(nulls_last=True), "received_at")[:10]
        return ProductExpiryBatchSerializer(batches, many=True).data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        _restrict_pk_queryset_strict(self.fields.get("item_make_ids"), ItemMake.objects.all(), comp, br)
        _restrict_pk_queryset_strict(self.fields.get("client"), Client.objects.all(), comp, br)
        _restrict_pk_queryset_strict(self.fields.get("supplier_ids"), Client.objects.all(), comp, br)

        # История закупок отдаётся только там, где явно попросили (детальный
        # просмотр товара). В списках/вебхуках поле убираем, чтобы не делать
        # лишний запрос на каждый товар.
        if not self.context.get("include_purchase_batches"):
            self.fields.pop("purchase_batches", None)

    def get_purchase_batches(self, obj):
        qs = (
            SupplierReceiptItem.objects
            .filter(product_id=obj.pk)
            .select_related("receipt", "receipt__supplier", "receipt__created_by")
            .order_by("-receipt__created_at")[:50]
        )
        return ProductPurchaseBatchSerializer(qs, many=True, context=self.context).data

    def get_suppliers(self, obj):
        try:
            qs = obj.suppliers.all()
            return [{"id": str(c.id), "name": c.full_name} for c in qs]
        except Exception:
            return []

    def validate_hotkey_group(self, value):
        if value in (None, ""):
            return None
        return value

    def validate_barcode(self, value):
        if value in (None, ""):
            return value
        b = str(value).strip()
        if not b:
            return None
        company_id = getattr(self.instance, "company_id", None) if self.instance else None
        if not company_id:
            comp = self._user_company()
            company_id = getattr(comp, "id", None)
        if not company_id:
            return b
        pk = self.instance.pk if self.instance else None
        qs = ProductAlternateBarcode.objects.filter(company_id=company_id, barcode=b)
        if pk:
            qs = qs.exclude(product_id=pk)
        alt = qs.select_related("product").first()
        if alt is not None:
            code = f" (код {alt.product.code})" if alt.product.code else ""
            raise serializers.ValidationError(
                f"Штрихкод «{b}» уже есть у товара «{alt.product.name}»{code} как дополнительный."
            )
        return b

    def validate_plu(self, value):
        """
        PLU должен быть уникален в рамках компании (если задан).
        """
        if value in (None, ""):
            return None

        try:
            value_int = int(value)
        except Exception:
            raise serializers.ValidationError("PLU должен быть числом.")

        if value_int < 1:
            raise serializers.ValidationError("PLU должен быть больше 0.")

        company_id = getattr(getattr(self.instance, "company", None), "id", None)
        if not company_id:
            company_id = getattr(self.instance, "company_id", None)

        # для create company может быть проставлена позже — тогда не валидируем тут
        if not company_id:
            return value_int

        qs = Product.objects.filter(company_id=company_id, plu=value_int)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Этот PLU уже используется в вашей компании.")

        return value_int

    def validate(self, attrs):
        kind = attrs.get("kind")
        if kind is None and self.instance is not None:
            kind = getattr(self.instance, "kind", None)
        if kind == Product.Kind.SERVICE:
            attrs["quantity"] = Decimal("0.000")
            attrs["stock"] = False

        data = self.initial_data if isinstance(getattr(self, "initial_data", None), dict) else {}
        promo_in = "promotion_rules_input" in data or "promotion_rules" in data
        raw = None
        if promo_in:
            raw = data.get("promotion_rules_input", data.get("promotion_rules"))

        stock = attrs.get("stock")
        if stock is None and self.instance is not None:
            stock = self.instance.stock
        if stock is None:
            stock = False

        if self.instance is None:
            if stock and (not promo_in or not isinstance(raw, list) or len(raw) == 0):
                raise serializers.ValidationError({
                    "promotion_rules_input": (
                        "Для акционного товара передайте непустой promotion_rules_input "
                        "(список объектов с min_amount, discount_percent; опционально promo_quantity)."
                    ),
                })
        else:
            if promo_in and isinstance(raw, list) and len(raw) == 0 and stock:
                raise serializers.ValidationError({
                    "promotion_rules_input": "Для акции укажите хотя бы одну ступень или снимите галочку акции.",
                })
            if (
                attrs.get("stock") is True
                and not promo_in
                and not self.instance.promotion_tiers.exists()
            ):
                raise serializers.ValidationError({
                    "promotion_rules_input": (
                        "При включении акции укажите promotion_rules_input или сначала сохраните ступени."
                    ),
                })
        return attrs

    # ==== ДАННЫЕ С ВЕСОВ ====

    def get_weight_kg(self, obj):
        scale_data = self.context.get("scale_data")
        if not scale_data:
            return None
        if not obj.is_weight:
            return None
        return scale_data.get("weight_kg")

    def get_total_price(self, obj):
        scale_data = self.context.get("scale_data")
        if not obj.is_weight or not scale_data:
            return obj.price

        weight_kg = scale_data.get("weight_kg")
        if not weight_kg:
            return obj.price

        total = Decimal(obj.price) * Decimal(str(weight_kg))
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # ---- helpers ----

    def get_created_by_name(self, obj):
        u = getattr(obj, "created_by", None)
        if not u:
            return None
        return (
            getattr(u, "get_full_name", lambda: "")()
            or getattr(u, "email", None)
            or getattr(u, "username", None)
        )

    def get_date(self, obj):
        val = getattr(obj, "date", None)
        if not val:
            return None

        from datetime import datetime as _datetime, date as _date

        if isinstance(val, _datetime):
            try:
                if dj_tz.is_naive(val):
                    val = dj_tz.make_aware(val)
                val = dj_tz.localtime(val)
            except Exception:
                pass
            return val.date().isoformat()

        if isinstance(val, _date):
            return val.isoformat()

        try:
            dt = parse_datetime(str(val))
            if dt:
                if dj_tz.is_naive(dt):
                    dt = dj_tz.make_aware(dt)
                return dj_tz.localtime(dt).date().isoformat()
            d = parse_date(str(val))
            if d:
                return d.isoformat()
        except Exception:
            pass

        return None

    @staticmethod
    def _normalize_status(raw):
        if raw in (None, "", "null"):
            return None
        v = str(raw).strip().lower()
        mapping = {
            "pending": "pending", "ожидание": "pending",
            "accepted": "accepted", "принят": "accepted",
            "rejected": "rejected", "отказ": "rejected",
        }
        return mapping.get(v, v)

    def _ensure_company_brand(self, company, brand):
        if brand is None:
            return None
        return ProductBrand.objects.get_or_create(company=company, name=brand.name)[0]

    def _ensure_company_category(self, company, category):
        if category is None:
            return None
        return ProductCategory.objects.get_or_create(company=company, name=category.name)[0]

    # ====== PRICE <-> MARKUP helper ======

    _Q2 = Decimal("0.01")
    _Q3 = Decimal("0.001")
    _Q4 = Decimal("0.0001")

    @staticmethod
    def _to_dec(v, default=Decimal("0")):
        if v in (None, "", "null"):
            return default
        return Decimal(str(v))

    @classmethod
    def _calc_price(cls, purchase_price: Decimal, markup_percent: Decimal) -> Decimal:
        price = purchase_price * (Decimal("1") + markup_percent / Decimal("100"))
        return price.quantize(cls._Q2, rounding=ROUND_HALF_UP)

    @classmethod
    def _calc_markup(cls, purchase_price: Decimal, price: Decimal) -> Decimal:
        if purchase_price <= 0:
            return Decimal("0.00")
        mp = (price / purchase_price - Decimal("1")) * Decimal("100")
        # Храним/считаем наценку точнее (4 знака), чтобы обратный пересчёт цены
        # не давал «копейки» из-за округления процента.
        return mp.quantize(cls._Q4, rounding=ROUND_HALF_UP)

    # ====== SAFE DECIMAL OUTPUT ======
    @staticmethod
    def _safe_decimal(val, quant: Decimal):
        """
        Возвращает декремент для сериализации или None, если значение некорректное
        (NaN/inf/строка/пусто). Делает quantize с указанной точностью.
        """
        if val in (None, "", "null"):
            return None
        try:
            dec = Decimal(val)
            if not dec.is_finite():
                return None
            return dec.quantize(quant, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            return None

    def _sanitize_decimal_fields(self, instance):
        """Чистим потенциально битые Decimal, чтобы DRF не падал на quantize()."""
        for field, quant in (
            ("quantity", self._Q3),
            ("purchase_price", self._Q2),
            ("markup_percent", self._Q4),
            ("price", self._Q2),
            ("wholesale_price", Decimal("0.001")),
            ("discount_percent", self._Q2),
        ):
            safe = self._safe_decimal(getattr(instance, field, None), quant)
            setattr(instance, field, safe)

        # характеристики товара (OneToOne)
        chars = getattr(instance, "characteristics", None)
        if chars:
            for field, quant in (
                ("height_cm", Decimal("0.01")),
                ("width_cm", Decimal("0.01")),
                ("depth_cm", Decimal("0.01")),
                ("factual_weight_kg", Decimal("0.001")),
            ):
                safe = self._safe_decimal(getattr(chars, field, None), quant)
                setattr(chars, field, safe)

        # пакеты (prefetch_related уже есть, будет без доп. запросов)
        try:
            packages = list(instance.packages.all())
        except Exception:
            packages = []
        for pkg in packages:
            safe = self._safe_decimal(getattr(pkg, "quantity_in_package", None), Decimal("0.001"))
            setattr(pkg, "quantity_in_package", safe)
            pup = self._safe_decimal(getattr(pkg, "piece_unit_price", None), self._Q2)
            setattr(pkg, "piece_unit_price", pup)

    def to_internal_value(self, data):
        # «Калькуляция», задача 4: is_promo — понятное имя для stock («Акционный товар»).
        # Принимаем его вместо stock; если пришли оба — главный stock (старое имя).
        if hasattr(data, "get") and "is_promo" in data and "stock" not in data:
            data = data.copy() if hasattr(data, "copy") else dict(data)
            data["stock"] = data.get("is_promo")
        return super().to_internal_value(data)

    def to_representation(self, instance):
        # чистим проблемные Decimal перед сериализацией, чтобы избежать InvalidOperation
        self._sanitize_decimal_fields(instance)
        try:
            data = super().to_representation(instance)
        except InvalidOperation:
            self._sanitize_decimal_fields(instance)
            data = super().to_representation(instance)
        if "stock" in data:
            data["is_promo"] = data["stock"]
        # Б-06: в ответе PATCH/PUT — менялась ли цена продажи этим запросом.
        if hasattr(self, "_price_changed"):
            data["price_changed"] = bool(self._price_changed)
        try:
            data["alternate_barcodes"] = [
                {"barcode": item.barcode, "name": item.name, "quantity": item.quantity}
                for item in instance.alternate_barcodes.order_by("barcode")
            ]
        except Exception:
            data["alternate_barcodes"] = []
        if self.context.get("include_variants"):  # ТЗ Н-33: ?include=variants
            from apps.main.kassa_stage3 import ProductVariantSerializer

            data["variants"] = ProductVariantSerializer(
                sorted(instance.variants.all(), key=lambda v: (v.sort_order, v.size, v.color)), many=True,
                context={"product": instance},
            ).data
        from apps.main.tz26.hw_catalog.services import extend_representation  # ТЗ 26.10 Б-57/58/59
        return extend_representation(self, instance, data)

    # ==== CREATE / UPDATE ====

    @transaction.atomic
    def create(self, validated_data):
        item_make_data = validated_data.pop("item_make_ids", [])
        packages_data = validated_data.pop("packages_input", [])
        units_raw = validated_data.pop("sale_units_input", None)  # ТЗ 26.10 Б-53
        promotion_in = "promotion_rules_input" in validated_data
        promotion_raw = validated_data.pop("promotion_rules_input", None) if promotion_in else None
        alt_in = "alternate_barcodes" in getattr(self, "initial_data", {})
        alternate_raw = validated_data.pop("alternate_barcodes", None) if alt_in else None
        supplier_ids = validated_data.pop("supplier_ids", None)

        company = self._user_company()
        branch = None

        client = validated_data.pop("client", None)
        brand_name = (validated_data.pop("brand_name", "") or "").strip()
        category_name = (validated_data.pop("category_name", "") or "").strip()
        status_value = self._normalize_status(validated_data.pop("status", None))

        article = (validated_data.pop("article", "") or "").strip()
        unit = (validated_data.pop("unit", None) or "шт.").strip()
        is_weight = validated_data.pop("is_weight", False)
        is_adult = validated_data.pop("is_adult", False)

        description = (validated_data.pop("description", "") or "").strip()

        # ===== цены: двусторонняя логика =====
        validated_data.pop("recalc_price", None)  # при создании смысла не имеет
        purchase_price = self._to_dec(validated_data.pop("purchase_price", Decimal("0")))
        markup_percent = self._to_dec(validated_data.pop("markup_percent", Decimal("0")))
        price_in = validated_data.pop("price", None)
        wholesale_in = validated_data.pop("wholesale_price", None)

        if price_in not in (None, ""):
            price = self._to_dec(price_in)
            markup_percent = self._calc_markup(purchase_price, price)
        else:
            price = self._calc_price(purchase_price, markup_percent)

        wholesale_price = self._to_dec(wholesale_in, default=Decimal("0")) if wholesale_in not in (None, "") else Decimal("0")
        discount_percent = self._to_dec(validated_data.pop("discount_percent", Decimal("0")))

        country = (validated_data.pop("country", "") or "").strip()
        expiration_date = validated_data.pop("expiration_date", None)

        date_value = dj_tz.now()

        barcode = validated_data.get("barcode")
        gp = (
            GlobalProduct.objects
            .select_related("brand", "category")
            .filter(barcode=barcode)
            .first()
        )
        if not gp:
            raise serializers.ValidationError({
                "barcode": "Товар с таким штрих-кодом не найден в глобальной базе. Заполните карточку вручную."
            })

        brand = (
            ProductBrand.objects.get_or_create(company=company, name=brand_name)[0]
            if brand_name else self._ensure_company_brand(company, gp.brand)
        )
        category = (
            ProductCategory.objects.get_or_create(company=company, name=category_name)[0]
            if category_name else self._ensure_company_category(company, gp.category)
        )

        product = Product.objects.create(
            company=company,
            branch=branch,
            kind=validated_data.get("kind", Product.Kind.PRODUCT),

            name=gp.name,
            barcode=gp.barcode,

            brand=brand,
            category=category,

            article=article,
            description=description,
            unit=unit,
            is_weight=is_weight,
            is_adult=is_adult,

            purchase_price=purchase_price,
            markup_percent=markup_percent,
            price=price,
            wholesale_price=wholesale_price,
            discount_percent=discount_percent,

            quantity=validated_data.get("quantity", 0),
            minimum_quantity=validated_data.get("minimum_quantity", 0),

            country=country,
            expiration_date=expiration_date,

            client=client,
            status=status_value,
            date=date_value,
            created_by=self._user(),
            stock=validated_data.get("stock", False),
        )

        if supplier_ids is not None:
            product.suppliers.set(supplier_ids)

        if item_make_data:
            product.item_make.set(item_make_data)

        if packages_data and not isinstance(packages_data, (list, tuple)):
            raise serializers.ValidationError({"packages_input": "Ожидается массив упаковок."})
        for pkg in packages_data:
            pup = _package_row_piece_unit_price(pkg)
            if pup is None:
                raise serializers.ValidationError({
                    "packages_input": "Для каждой упаковки укажите piece_unit_price (цена за штуку при поштучной продаже).",
                })
            ProductPackage.objects.create(
                product=product,
                name=(pkg.get("name") or "").strip(),
                quantity_in_package=pkg.get("quantity_in_package"),
                unit=(pkg.get("unit") or "").strip(),
                piece_unit_price=pup,
            )

        sync_product_promotion_tiers(
            product,
            promotion_raw if promotion_in else [],
            stock_enabled=bool(product.stock),
            partial=False,
        )
        if alt_in:
            sync_product_alternate_barcodes(
                product,
                alternate_raw if alternate_raw is not None else [],
            )

        from apps.main.tz26.hw_catalog.services import apply_input  # ТЗ 26.10 Б-57/58/59
        apply_input(self, product)
        from apps.main.tz26.hw_units.units import apply_product_extras  # ТЗ 26.10 Б-52, Б-53

        apply_product_extras(product, getattr(self, "initial_data", None), units_raw=units_raw)
        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        company = self._user_company()
        branch = None

        packages_data = validated_data.pop("packages_input", None)
        units_raw = validated_data.pop("sale_units_input", None)  # ТЗ 26.10 Б-53
        supplier_ids = validated_data.pop("supplier_ids", None)
        promotion_in = "promotion_rules_input" in validated_data
        promotion_raw = validated_data.pop("promotion_rules_input", None) if promotion_in else None
        alt_in = "alternate_barcodes" in getattr(self, "initial_data", {})
        alternate_raw = validated_data.pop("alternate_barcodes", None) if alt_in else None
        # ТЗ 11.6: characteristics (read-only в схеме) принимаем из тела PATCH, как в create-manual.
        chars_raw = getattr(self, "initial_data", {}).get("characteristics") if hasattr(getattr(self, "initial_data", None), "get") else None
        if isinstance(chars_raw, dict):
            chars_ser = ProductCharacteristicsSerializer(
                instance=getattr(instance, "characteristics", None), data=chars_raw, partial=True
            )
            chars_ser.is_valid(raise_exception=True)
            chars_obj, _ = ProductCharacteristics.objects.update_or_create(
                product=instance,
                defaults={
                    "company": instance.company,
                    "branch": instance.branch,
                    **chars_ser.validated_data,
                },
            )
            instance.characteristics = chars_obj  # сбросить кэш, чтобы ответ показал новые значения
        if promotion_in and promotion_raw:
            try:
                pr_rows = parse_product_promotion_tiers_payload(promotion_raw)
            except serializers.ValidationError:
                raise
            if pr_rows:
                if validated_data.get("stock") is False:
                    raise serializers.ValidationError({
                        "stock": "Нельзя передать ступени акции при stock=false.",
                    })
                validated_data.setdefault("stock", True)

        # бренд/категория через *_name
        brand_name = (validated_data.pop("brand_name", "") or "").strip()
        category_name = (validated_data.pop("category_name", "") or "").strip()
        if brand_name:
            instance.brand, _ = ProductBrand.objects.get_or_create(company=company, name=brand_name)
        if category_name:
            instance.category, _ = ProductCategory.objects.get_or_create(company=company, name=category_name)

        # item_make
        item_make_data = validated_data.pop("item_make_ids", None)
        if item_make_data is not None:
            instance.item_make.set(item_make_data)

        # Филиал товара при редактировании не меняем: раньше здесь был сброс
        # instance.branch = None, из-за которого товар филиала «переезжал» на главный
        # склад и падал с IntegrityError (uq_company_main_barcode_not_empty).

        # ===== цены: двусторонняя логика =====
        recalc_price = bool(validated_data.pop("recalc_price", False))
        has_purchase = "purchase_price" in validated_data
        has_markup = "markup_percent" in validated_data
        has_price = "price" in validated_data

        purchase_price = self._to_dec(validated_data.get("purchase_price", instance.purchase_price))
        markup_percent = self._to_dec(validated_data.get("markup_percent", instance.markup_percent))
        price = self._to_dec(validated_data.get("price", instance.price))

        # 1) пришёл price -> считаем markup
        if has_price and validated_data.get("price") not in (None, ""):
            price = self._to_dec(validated_data["price"])
            markup_percent = self._calc_markup(purchase_price, price)

        # 2) иначе пришёл markup -> считаем price
        elif has_markup and validated_data.get("markup_percent") not in (None, ""):
            markup_percent = self._to_dec(validated_data["markup_percent"])
            price = self._calc_price(purchase_price, markup_percent)

        # 3) иначе пришёл только purchase_price -> цена продажи не меняется,
        #    наценка пересчитывается к текущей цене. Пересчёт цены от наценки —
        #    только по явному "recalc_price": true (ТЗ Н-04: иначе 11 → 1 → 11
        #    давало цену 165 вместо 15).
        elif has_purchase:
            raw_recalc = getattr(self, "initial_data", {}).get("recalc_price")
            if str(raw_recalc).lower() in ("true", "1") and Decimal(instance.markup_percent or 0) != Decimal("0"):
                price = self._calc_price(purchase_price, markup_percent)
            else:
                markup_percent = self._calc_markup(purchase_price, price)

        # Б-06: recalc_price без purchase_price — пересчитать цену от текущей закупки и наценки.
        elif recalc_price and not has_price and not has_markup:
            price = self._calc_price(purchase_price, markup_percent)

        old_price = instance.price
        instance.purchase_price = purchase_price
        instance.markup_percent = markup_percent
        instance.price = price
        # save() пересчитывает цену от наценки; цена здесь уже согласована —
        # не даём округлению наценки сдвинуть её на копейки.
        instance._manual_price = True
        self._price_changed = Decimal(str(old_price or 0)) != Decimal(str(price or 0))

        # ===== PATCH-friendly обновление остальных полей =====
        updatable_fields = (
            "name",
            "barcode",
            "description",
            "quantity",
            "minimum_quantity",
            "discount_percent",
            "article",
            "unit",
            "is_weight",
            "is_adult",
            "plu",
            "country",
            "expiration_date",
            "client",
            "kind",
            "hotkey_group",
            "wholesale_price",
        )
        for field in updatable_fields:
            if field in validated_data:
                if field == "wholesale_price":
                    raw = validated_data[field]
                    instance.wholesale_price = (
                        self._to_dec(raw, default=Decimal("0"))
                        if raw not in (None, "")
                        else Decimal("0")
                    )
                else:
                    setattr(instance, field, validated_data[field])

        if "size_chart" in validated_data:  # ТЗ Н-33
            instance.size_chart = validated_data["size_chart"]

        if "stock" in validated_data:
            instance.stock = validated_data["stock"]

        if "status" in validated_data:
            instance.status = self._normalize_status(validated_data["status"])

        if instance.is_weight is False:
            instance.plu = None

        # дата (опционально)
        raw_date = None
        if isinstance(getattr(self, "initial_data", None), dict):
            raw_date = self.initial_data.get("date") or self.initial_data.get("date_raw")

        if raw_date not in (None, ""):
            dt = parse_datetime(str(raw_date))
            if dt:
                if dj_tz.is_naive(dt):
                    dt = dj_tz.make_aware(dt)
                instance.date = dt
            else:
                d = parse_date(str(raw_date))
                if d:
                    from datetime import datetime as _datetime
                    instance.date = dj_tz.make_aware(_datetime(d.year, d.month, d.day))
                else:
                    raise serializers.ValidationError({
                        "date": "Неверный формат даты. Используйте YYYY-MM-DD или ISO datetime."
                    })

        instance.save()

        if supplier_ids is not None:
            instance.suppliers.set(supplier_ids)
        elif instance.client_id:
            try:
                instance.suppliers.add(instance.client)
            except Exception:
                pass

        # PACKAGES перезаписываем ТОЛЬКО если реально пришли в PATCH
        if packages_data is not None:
            if not isinstance(packages_data, (list, tuple)):
                raise serializers.ValidationError({"packages_input": "Ожидается массив упаковок."})
            try:
                instance.packages.all().delete()
            except ProtectedError:
                raise serializers.ValidationError({
                    "packages_input": (
                        "Нельзя заменить упаковки: они привязаны к позициям в корзинах кассы "
                        "(поштучная продажа). Удалите или измените такие позиции в корзинах и повторите."
                    ),
                })
            for pkg in packages_data:
                pup = _package_row_piece_unit_price(pkg)
                if pup is None:
                    raise serializers.ValidationError({
                        "packages_input": "Для каждой упаковки укажите piece_unit_price (цена за штуку при поштучной продаже).",
                    })
                ProductPackage.objects.create(
                    product=instance,
                    name=(pkg.get("name") or "").strip(),
                    quantity_in_package=pkg.get("quantity_in_package"),
                    unit=(pkg.get("unit") or "").strip(),
                    piece_unit_price=pup,
                )

        if promotion_in:
            sync_product_promotion_tiers(
                instance,
                promotion_raw,
                stock_enabled=bool(instance.stock),
                partial=False,
            )
        elif "stock" in validated_data and validated_data.get("stock") is False:
            sync_product_promotion_tiers(instance, None, stock_enabled=False, partial=True)

        if alt_in:
            sync_product_alternate_barcodes(
                instance,
                alternate_raw if alternate_raw is not None else [],
            )

        from apps.main.tz26.hw_catalog.services import apply_input  # ТЗ 26.10 Б-57/58/59
        apply_input(self, instance)
        from apps.main.tz26.hw_units.units import apply_product_extras  # ТЗ 26.10 Б-52, Б-53

        apply_product_extras(instance, getattr(self, "initial_data", None), units_raw=units_raw)
        return instance
# ===========================
# Review / Notification
# ===========================
class ReviewSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    user = serializers.ReadOnlyField(source='user.id')
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    class Meta:
        model = Review
        fields = ['id', 'company', 'branch', 'user', 'rating', 'comment', 'created_at', 'updated_at']
        read_only_fields = ['id', 'company', 'branch', 'user', 'created_at', 'updated_at']

    def create(self, validated_data):
        validated_data['user'] = self._user()
        return super().create(validated_data)


class NotificationSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')
    actor_name = serializers.SerializerMethodField()

    body = serializers.CharField(source='message', read_only=True)
    cta_url = serializers.CharField(source='url', read_only=True)
    cta_label = serializers.SerializerMethodField()
    meta = serializers.JSONField(source='data', read_only=True)

    class Meta:
        model = Notification
        fields = [
            'id', 'company', 'branch', 'category', 'type', 'title', 'message', 'body',
            'url', 'cta_url', 'cta_label', 'level', 'is_read', 'actor_name',
            'data', 'meta', 'created_at',
        ]
        read_only_fields = ['id', 'company', 'branch', 'actor_name', 'created_at']

    def get_cta_label(self, obj):
        if isinstance(obj.data, dict) and obj.data.get("cta_label"):
            return obj.data.get("cta_label")
        if obj.category == Notification.Category.TARIFF:
            return "Продлить"
        return ""

    def get_actor_name(self, obj):
        actor = getattr(obj, "actor", None)
        if not actor:
            return ""
        full = f"{(actor.first_name or '').strip()} {(actor.last_name or '').strip()}".strip()
        return full or getattr(actor, "email", "") or ""

    def create(self, validated_data):
        user = self._user()
        if user:
            validated_data.setdefault("user", user)
        return super().create(validated_data)



# ===========================
# Users short
# ===========================
class UserShortSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'first_name', 'last_name', 'email']


# ===========================
# Event (STRICT branch только по company для участников)
# ===========================
class EventSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    participants = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=User.objects.all()
    )
    participants_detail = UserShortSerializer(source='participants', many=True, read_only=True)

    class Meta:
        model = Event
        fields = [
            'id', 'company', 'branch',
            'title', 'datetime', 'participants',
            'participants_detail', 'notes', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'company', 'branch', 'created_at', 'updated_at', 'participants_detail']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        if comp and self.fields.get("participants"):
            self.fields["participants"].queryset = User.objects.filter(company=comp)

    def validate_participants(self, participants):
        company = self._user_company()
        for participant in participants:
            if participant.company != company:
                raise serializers.ValidationError(
                    f"Пользователь {participant.email} не принадлежит вашей компании."
                )
        return participants

    def create(self, validated_data):
        participants = validated_data.pop('participants')
        event = super().create(validated_data)
        event.participants.set(participants)
        return event

    def update(self, instance, validated_data):
        participants = validated_data.pop('participants', None)
        instance = super().update(instance, validated_data)
        if participants is not None:
            instance.participants.set(participants)
        return instance


# ===========================
# Warehouse / WarehouseEvent
# ===========================
class WarehouseSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    class Meta:
        model = Warehouse
        fields = ['id', 'company', 'branch', 'name', 'location', 'created_at', 'updated_at']
        read_only_fields = ['id', 'company', 'branch', 'created_at', 'updated_at']


class WarehouseEventSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    participants = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=User.objects.all()
    )
    participants_detail = serializers.StringRelatedField(source='participants', many=True, read_only=True)
    responsible_person = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True)
    warehouse = serializers.PrimaryKeyRelatedField(queryset=Warehouse.objects.all())

    class Meta:
        model = WarehouseEvent
        fields = [
            'id', 'company', 'branch',
            'warehouse', 'responsible_person', 'status', 'client_name',
            'title', 'description', 'amount', 'event_date', 'participants',
            'participants_detail', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'company', 'branch', 'created_at', 'updated_at', 'participants_detail']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        if comp:
            self.fields["participants"].queryset = User.objects.filter(company=comp)
            _restrict_pk_queryset_strict(self.fields.get("warehouse"), Warehouse.objects.all(), comp, br)
            if self.fields.get("responsible_person"):
                self.fields["responsible_person"].queryset = User.objects.filter(company=comp)

    def validate_participants(self, participants):
        company = self._user_company()
        for participant in participants:
            if participant.company != company:
                raise serializers.ValidationError(
                    f"Пользователь {participant.email} не принадлежит вашей компании."
                )
        return participants

    def create(self, validated_data):
        participants = validated_data.pop('participants')
        we = super().create(validated_data)
        we.participants.set(participants)
        return we

    def update(self, instance, validated_data):
        participants = validated_data.pop('participants', None)
        instance = super().update(instance, validated_data)
        if participants is not None:
            instance.participants.set(participants)
        return instance


# ===========================
# Client / ClientDeal / DealInstallment
# ===========================
class ClientSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')

    salesperson = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), required=False, allow_null=True
    )
    service = serializers.PrimaryKeyRelatedField(
        queryset=ServicesConsalting.objects.all(), required=False, allow_null=True
    )
    salesperson_display = serializers.SerializerMethodField(read_only=True)
    service_display = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Client
        fields = [
            'id', 'company', 'branch', 'sector',
            'type', 'full_name', 'phone', 'email', 'date', 'status',
            'llc', 'inn', 'okpo', 'score', 'bik', 'address',
            'salesperson', 'salesperson_display',
            'service', 'service_display',
            'telegram_chat_id', 'bonus_balance',
            'created_at', 'updated_at',
            'price_level', 'credit_limit', 'default_due_days', 'client_kind',  # ТЗ 26.10 Б-55, Б-56
        ]
        read_only_fields = [
            'id', 'company', 'branch', 'created_at', 'updated_at',
            'salesperson_display', 'service_display', 'bonus_balance'
        ]
        # ТЗ 12.16: телефон обязателен всем, кроме поставщиков (см. validate)
        extra_kwargs = {'phone': {'required': False, 'allow_blank': True}}

    def validate(self, attrs):
        attrs = super().validate(attrs)
        inst = getattr(self, 'instance', None)
        client_type = attrs.get('type', getattr(inst, 'type', None))
        # проверяем при создании или когда телефон явно присылают (старых клиентов без телефона не трогаем)
        if inst is None or 'phone' in attrs:
            phone = (attrs.get('phone') or '').strip()
            if not phone and client_type != Client.StatusClient.SUPPLIERS:
                raise serializers.ValidationError({'phone': 'Укажите телефон.'})
        if inst is None:
            attrs.setdefault('phone', '')
        from apps.main.tz26.hw_prices.services import client_fields_check  # ТЗ 26.10 Б-55, Б-56
        client_fields_check(self, attrs)
        return attrs

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        if comp and self.fields.get("salesperson"):
            self.fields["salesperson"].queryset = User.objects.filter(company=comp)

    def get_salesperson_display(self, obj):
        if obj.salesperson:
            return f"{obj.salesperson.first_name} {obj.salesperson.last_name}"
        return None

    def get_service_display(self, obj):
        if obj.service:
            return obj.service.name
        return None
class DealInstallmentSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")
    deal = serializers.ReadOnlyField(source="deal.id")

    # ВАЖНО: source НЕ НУЖЕН, потому что имя поля совпадает с property модели
    remaining_for_period = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        read_only=True,
    )
    is_overdue = serializers.BooleanField(read_only=True)
    overdue_days = serializers.IntegerField(read_only=True)
    overdue_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        read_only=True,
    )

    class Meta:
        model = DealInstallment
        fields = (
            "id",
            "company",
            "branch",
            "deal",
            "number",
            "due_date",
            "promised_date",
            "amount",
            "balance_after",
            "paid_on",
            "paid_amount",
            "remaining_for_period",
            "is_overdue",
            "overdue_days",
            "overdue_amount",
        )
        read_only_fields = fields


# ===== Payments =====
class DealPaymentSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    deal = serializers.ReadOnlyField(source="deal.id")
    installment = serializers.ReadOnlyField(source="installment.id")
    installment_number = serializers.IntegerField(source="installment.number", read_only=True)

    created_by = serializers.ReadOnlyField(source="created_by.id")
    # ТЗ Н-11: кто принял/выплатил и из какой кассы (касса — по связанному движению денег)
    created_by_name = serializers.SerializerMethodField()
    cashbox = serializers.SerializerMethodField()
    cashbox_name = serializers.SerializerMethodField()

    def get_created_by_name(self, obj):
        u = obj.created_by
        if u is None:
            return None
        full = f"{getattr(u, 'first_name', '') or ''} {getattr(u, 'last_name', '') or ''}".strip()
        return full or getattr(u, "email", None)

    def _cashflow(self, obj):
        cache = self.context.setdefault("_deal_pay_cf", {})
        if obj.pk not in cache:
            from django.db.models import Q
            from apps.construction.models import CashFlow
            cache[obj.pk] = (
                CashFlow.objects.select_related("cashbox")
                .filter(Q(idempotency_key=f"deal-pay:{obj.pk}") | Q(source_id=str(obj.pk)))
                .order_by("created_at").first()
            )
        return cache[obj.pk]

    def get_cashbox(self, obj):
        cf = self._cashflow(obj)
        return str(cf.cashbox_id) if cf and cf.cashbox_id else None

    def get_cashbox_name(self, obj):
        cf = self._cashflow(obj)
        return cf.cashbox.name if cf and cf.cashbox_id else None

    class Meta:
        model = DealPayment
        fields = (
            "id",
            "company",
            "branch",
            "deal",
            "installment",
            "installment_number",
            "kind",
            "amount",
            "paid_date",
            "idempotency_key",
            "created_by",
            "note",
            "payment_method",
            "created_at",
            "created_by_name",
            "cashbox",
            "cashbox_name",
        )
        read_only_fields = fields


# ===== Deals =====
class ClientDealSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    # client может прийти из URL /clients/<client_id>/deals/ -> required=False
    client = serializers.PrimaryKeyRelatedField(queryset=Client.objects.all(), required=False)
    client_full_name = serializers.CharField(source="client.full_name", read_only=True)

    debt_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    daily_payment = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    remaining_debt = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    schedule_version = serializers.CharField(required=False, default="v2")
    debt_months = serializers.IntegerField(required=False, allow_null=True)
    interval_days = serializers.IntegerField(required=False, default=1, allow_null=True)
    interval_months = serializers.IntegerField(required=False, default=1, allow_null=True)
    sale = serializers.PrimaryKeyRelatedField(queryset=Sale.objects.all(), required=False, allow_null=True)
    sale_id = serializers.UUIDField(required=False, allow_null=True)

    installments = serializers.JSONField(required=False, write_only=True)
    payments = DealPaymentSerializer(many=True, read_only=True)

    auto_schedule = serializers.BooleanField(required=False)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    cashflows = serializers.SerializerMethodField()

    class Meta:
        model = ClientDeal
        fields = [
            "id", "company", "branch",
            "client", "client_full_name",
            "title", "kind", "kind_display",
            "amount", "prepayment",
            "debt_days", "debt_months", "interval_days", "interval_months",
            "first_due_date", "schedule_version", "sale", "sale_id",
            "debt_amount", "daily_payment", "remaining_debt",
            "installments",
            "payments",
            "cashflows",
            "auto_schedule",
            "note", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "company", "branch",
            "created_at", "updated_at",
            "client_full_name", "kind_display",
            "debt_amount", "daily_payment", "remaining_debt",
            "payments",
            "cashflows",
        ]

    def get_cashflows(self, obj):
        from apps.construction.models import CashFlow
        from apps.construction.auto_cashflow import serialize_auto_cashflows
        cfs = CashFlow.objects.filter(company=obj.company, source_id=str(obj.id))
        return serialize_auto_cashflows(cfs)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["installments"] = DealInstallmentSerializer(
            instance.installments.order_by("number"), many=True, context=self.context
        ).data
        return ret

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        comp = self._user_company()
        br = self._auto_branch()

        _restrict_pk_queryset_strict(
            self.fields.get("client"),
            Client.objects.all(),
            comp,
            br,
        )
        _restrict_pk_queryset_strict(
            self.fields.get("sale"),
            Sale.objects.all(),
            comp,
            br,
        )

    def validate(self, attrs):
        request = self.context["request"]
        company = request.user.company
        branch = self._auto_branch()
        instance = getattr(self, "instance", None)

        # client обязателен, если не передан через URL
        view = self.context.get("view")
        client_id_from_url = getattr(view, "kwargs", {}).get("client_id") if view else None

        client = attrs.get("client") or (instance.client if instance else None)

        if not client and not client_id_from_url:
            raise serializers.ValidationError({"client": "Укажите клиента."})

        # scope проверки клиента (компания/филиал)
        if client:
            if client.company_id != company.id:
                raise serializers.ValidationError({"client": "Клиент принадлежит другой компании."})

            # клиент может быть общий (branch=None)
            if branch is not None and client.branch_id not in (None, branch.id):
                raise serializers.ValidationError({"client": "Клиент другого филиала."})

        # проверка sale / sale_id
        sale = attrs.get("sale")
        sale_id = attrs.get("sale_id")
        if sale_id and not sale:
            try:
                s_obj = Sale.objects.get(id=sale_id)
                if company and s_obj.company_id != company.id:
                    raise serializers.ValidationError({"sale_id": "Продажа принадлежит другой компании."})
                attrs["sale"] = s_obj
            except Sale.DoesNotExist:
                raise serializers.ValidationError({"sale_id": "Указанная продажа не найдена."})
        elif sale:
            if company and sale.company_id != company.id:
                raise serializers.ValidationError({"sale": "Продажа принадлежит другой компании."})

        target_sale = attrs.get("sale")
        if target_sale and not instance:
            existing = ClientDeal.objects.filter(sale=target_sale).first()
            if existing:
                attrs["_existing_deal"] = existing
                instance = existing
                self.instance = existing

        # прод: если уже есть платежи — условия сделки нельзя менять
        if instance and instance.pk and instance.payments.exists():
            allowed = {"title", "note"}  # максимально безопасно
            illegal = [k for k in attrs.keys() if k not in allowed]
            if illegal:
                raise serializers.ValidationError({
                    "detail": (
                        "Нельзя менять тип/суммы/срок/дату/график: по сделке уже есть платежи. "
                        "Разрешено менять только title и note."
                    )
                })

        amount = attrs.get("amount", getattr(instance, "amount", None))
        prepayment = attrs.get("prepayment", getattr(instance, "prepayment", None))
        kind = attrs.get("kind", getattr(instance, "kind", None))

        # Срок — либо в днях, либо в месяцах. XOR смотрим по телу запроса, а не по сделке:
        # сделку, созданную при продаже в долг, касса дополняет графиком, и там уже может
        # стоять debt_days=30 по умолчанию. Прислали только один вид срока — второй сбрасываем.
        sent_days = attrs.get("debt_days") is not None
        sent_months = attrs.get("debt_months") is not None
        if sent_days and sent_months:
            raise serializers.ValidationError(
                {"debt_months": "Нельзя одновременно указывать debt_days и debt_months."}
            )
        if sent_months:
            attrs["debt_days"] = None
        elif sent_days:
            attrs["debt_months"] = None

        debt_days = attrs.get("debt_days", getattr(instance, "debt_days", None))
        debt_months = attrs.get("debt_months", getattr(instance, "debt_months", None))

        sch_ver = attrs.get("schedule_version") or (instance.schedule_version if instance else "v2")
        if str(sch_ver).strip().lower() in ("v1", "1"):
            attrs["schedule_version"] = "v1"
        else:
            attrs["schedule_version"] = "v2"

        errors = {}

        if amount is not None and amount < 0:
            errors["amount"] = "Сумма не может быть отрицательной."
        if prepayment is not None and prepayment < 0:
            errors["prepayment"] = "Предоплата не может быть отрицательной."
        if amount is not None and prepayment is not None and prepayment > amount:
            errors["prepayment"] = "Предоплата не может превышать сумму договора."

        if kind == ClientDeal.Kind.DEBT:
            debt_amt = (amount or Decimal("0")) - (prepayment or Decimal("0"))
            if debt_amt <= 0:
                errors["prepayment"] = 'Для типа "Долг" сумма договора должна быть больше предоплаты.'

            if debt_days and debt_months:
                errors["debt_months"] = "Нельзя одновременно указывать debt_days и debt_months."

            if attrs["schedule_version"] == "v2":
                if not debt_days and not debt_months:
                    errors["debt_days"] = "Укажите количество платежей (debt_days или debt_months) для v2."

                inst_input = attrs.get("installments")
                if inst_input and isinstance(inst_input, (list, tuple)):
                    sum_inst = sum(Decimal(str(item.get("amount", "0"))) for item in inst_input if isinstance(item, dict))
                    if abs(sum_inst - debt_amt) > Decimal("0.05"):
                        errors["installments"] = f"Сумма графика платежей ({sum_inst}) должна совпадать с остатком долга ({debt_amt})."
            else:
                if not debt_days or debt_days <= 0:
                    errors["debt_days"] = "Укажите срок (в днях) для рассрочки."
        else:
            # не долг -> чистим всё, как в модели
            attrs["debt_days"] = None
            attrs["debt_months"] = None
            attrs["first_due_date"] = None
            attrs["auto_schedule"] = False

        if errors:
            raise serializers.ValidationError(errors)

        return attrs

    def create(self, validated_data):
        custom_inst = validated_data.pop("installments", None)
        validated_data.pop("sale_id", None)

        existing = validated_data.pop("_existing_deal", None)
        if existing:
            return self.update(existing, validated_data)

        instance = ClientDeal(**validated_data)
        if custom_inst and isinstance(custom_inst, (list, tuple)):
            instance._custom_installments = custom_inst

        try:
            instance.save()
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else (e.messages if hasattr(e, "messages") else str(e))
            raise serializers.ValidationError(msg)

        return instance

    def update(self, instance, validated_data):
        custom_inst = validated_data.pop("installments", None)
        validated_data.pop("sale_id", None)

        if custom_inst and isinstance(custom_inst, (list, tuple)):
            instance._custom_installments = custom_inst

        for attr, val in validated_data.items():
            setattr(instance, attr, val)

        try:
            instance.save()
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else (e.messages if hasattr(e, "messages") else str(e))
            raise serializers.ValidationError(msg)

        return instance



# ===== Inputs for pay/refund endpoints =====
class DealInstallmentRescheduleSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    promised_date = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class DealPayInputSerializer(serializers.Serializer):
    installment_id = serializers.UUIDField(required=False)  # если нет — возьмём первый не полностью оплаченный
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    date = serializers.DateField(required=False)
    idempotency_key = serializers.UUIDField(required=True)
    note = serializers.CharField(required=False, allow_blank=True)
    payment_method = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="cash")
    cashbox_id = serializers.UUIDField(required=False, allow_null=True)
    branch_id = serializers.UUIDField(required=False, allow_null=True)
    cashbox_role = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    shift_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_date(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError("Дата оплаты не может быть в будущем.")
        return value


class DealPayAnyInputSerializer(serializers.Serializer):
    """Input for one atomic payment distributed across a client's debts."""
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    date = serializers.DateField(required=False)
    idempotency_key = serializers.UUIDField(required=True)
    note = serializers.CharField(required=False, allow_blank=True)
    payment_method = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="cash")
    cashbox_id = serializers.UUIDField(required=False, allow_null=True)
    branch_id = serializers.UUIDField(required=False, allow_null=True)
    cashbox_role = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    shift_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_date(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError("Дата оплаты не может быть в будущем.")
        return value


class DealRefundInputSerializer(serializers.Serializer):
    installment_id = serializers.UUIDField(required=False)  # если нет — возьмём последний оплаченный/частично
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)  # если нет — вернём всё
    date = serializers.DateField(required=False)
    idempotency_key = serializers.UUIDField(required=True)
    note = serializers.CharField(required=False, allow_blank=True)
    payment_method = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="cash")

# ===========================
# TransactionRecord
# ===========================
class TransactionRecordSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    class Meta:
        model = TransactionRecord
        fields = [
            "id", "company", "branch",
            "description",
            "name", "amount", "status", "date",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company", "branch", "created_at", "updated_at"]

# ===========================
# ContractorWork
# ===========================
class ContractorWorkSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    duration_days = serializers.IntegerField(read_only=True)

    class Meta:
        model = ContractorWork
        fields = [
            "id", "company", "branch",
            "title",
            "contractor_name", "contractor_phone",
            "contractor_entity_type", "contractor_entity_name",
            "amount",
            "start_date", "end_date",
            "planned_completion_date", "work_calendar_date",
            "description",
            "duration_days",
            "created_at", "updated_at",
            "status",
        ]
        read_only_fields = [
            "id", "company", "branch",
            "duration_days",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        planned = attrs.get(
            "planned_completion_date",
            getattr(self.instance, "planned_completion_date", None),
        )

        errors = {}
        if start and end and end < start:
            errors["end_date"] = "Дата окончания не может быть раньше даты начала."
        if planned and start and planned < start:
            errors["planned_completion_date"] = "Плановая дата завершения не может быть раньше начала."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

def normalize_phone(v: str) -> str:
    v = (v or "").strip()
    # минимум: убрать пробелы
    v = v.replace(" ", "")
    return v


# ===========================
# Debt / DebtPayment
# ===========================
class DebtSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    paid_total = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()
    cashflows = serializers.SerializerMethodField()

    class Meta:
        model = Debt
        fields = [
            "id", "company", "branch",
            "name", "phone", "amount", "due_date",
            "paid_total", "balance",
            "cashflows",
            "payment_method",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company", "branch", "paid_total", "balance", "cashflows", "created_at", "updated_at"]

    def get_cashflows(self, obj):
        from apps.construction.models import CashFlow
        from apps.construction.auto_cashflow import serialize_auto_cashflows
        cfs = CashFlow.objects.filter(company=obj.company, source_id=str(obj.id))
        return serialize_auto_cashflows(cfs)

    def get_paid_total(self, obj) -> Decimal:
        # берём из queryset-аннотации если она есть
        v = getattr(obj, "paid_total_db", None)
        if v is None:
            v = obj.paid_total  # fallback (но это будет отдельный запрос)
        return Decimal(v).quantize(Decimal("0.01"))

    def get_balance(self, obj) -> Decimal:
        v = getattr(obj, "balance_db", None)
        if v is None:
            v = obj.balance
        return Decimal(v).quantize(Decimal("0.01"))

    def validate_phone(self, value: str) -> str:
        return normalize_phone(value)


class DebtPaymentSerializer(serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")
    debt = serializers.ReadOnlyField(source="debt.id")

    class Meta:
        model = DebtPayment
        fields = ["id", "company", "branch", "debt", "amount", "paid_at", "note", "payment_method", "created_at"]
        read_only_fields = ["id", "company", "branch", "debt", "created_at"]

    def create(self, validated_data):
        """
        Правильнее создавать оплату через debt.add_payment(), чтобы:
        - сработала блокировка/транзакция (если ты её сделал в модели)
        - вся бизнес-логика была в одном месте
        """
        debt = self.context.get("debt")
        if debt is None:
            raise serializers.ValidationError({"debt": "Debt обязателен (передай его в serializer context)."})

        amount = validated_data["amount"]
        paid_at = validated_data.get("paid_at")
        note = validated_data.get("note", "")
        pm = validated_data.get("payment_method", "cash")

        return debt.add_payment(amount=amount, paid_at=paid_at, note=note, payment_method=pm)

# ===========================
# ObjectItem / ObjectSale / ObjectSaleItem
# ===========================
class ObjectItemSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    class Meta:
        model = ObjectItem
        fields = ["id", "company", "branch", "name", "description", "price", "date", "quantity", "created_at", "updated_at"]
        read_only_fields = ["id", "company", "branch", "created_at", "updated_at"]


class ObjectSaleItemSerializer(serializers.ModelSerializer):
    object_name = serializers.ReadOnlyField(source="name_snapshot")

    class Meta:
        model = ObjectSaleItem
        fields = ["id", "object_item", "object_name", "unit_price", "quantity"]


class ObjectSaleSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")
    client_name = serializers.ReadOnlyField(source="client.full_name")
    items = ObjectSaleItemSerializer(many=True, read_only=True)

    class Meta:
        model = ObjectSale
        fields = ["id", "company", "branch", "client", "client_name", "status", "sold_at", "note", "subtotal", "items", "created_at"]
        read_only_fields = ["id", "company", "branch", "client_name", "subtotal", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        if comp and self.fields.get("client"):
            _restrict_pk_queryset_strict(self.fields["client"], Client.objects.all(), comp, br)

    def validate_client(self, client):
        company = self._user_company()
        branch = self._auto_branch()
        if client.company_id != company.id:
            raise serializers.ValidationError("Клиент принадлежит другой компании.")
        # STRICT branch
        if branch is not None and client.branch_id != branch.id:
            raise serializers.ValidationError("Клиент другого филиала.")
        # branch None — клиент из любого филиала компании
        return client


# ===========================
# BulkIds — утилита
# ===========================
class BulkIdsSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.UUIDField(format='hex_verbose'),
        allow_empty=False
    )
    soft = serializers.BooleanField(required=False, default=False)
    require_all = serializers.BooleanField(required=False, default=False)


# ===========================
# ItemMake — плоский список + связанные продукты (read-only)
# ===========================
class ProductNestedSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "name", "barcode", "quantity", "price", "wholesale_price"]


# Лёгкий сериализатор для списка товаров (минимальный набор полей)
class ProductListSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    is_favorite = serializers.BooleanField(read_only=True)
    shelf_life_days = serializers.IntegerField(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id", "kind", "name", "code", "article",
            "price", "wholesale_price", "purchase_price",
            "quantity", "brand", "category",
            "hotkey_group",
            "upsell_priority", "upsell_excluded",
            "image_url", "is_favorite",
            "expiration_date",
            "shelf_life_days",
        ]

    def get_image_url(self, obj):
        imgs = getattr(obj, "images", None)
        if not imgs:
            return None
        try:
            first = imgs.all()[0] if hasattr(imgs, "all") else imgs[0]
        except Exception:
            first = None
        if not first or not getattr(first, "image", None):
            return None
        req = self.context.get("request")
        try:
            return req.build_absolute_uri(first.image.url) if req else first.image.url
        except Exception:
            return None


class ItemMakeSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")
    products = ProductNestedSerializer(many=True, read_only=True)
    supplier_name = serializers.CharField(source="supplier.full_name", read_only=True)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    source = serializers.PrimaryKeyRelatedField(read_only=True)
    source_name = serializers.CharField(source="source.name", read_only=True, allow_null=True)
    is_processed = serializers.SerializerMethodField()

    class Meta:
        model = ItemMake
        fields = [
            "id", "company", "branch",
            "kind", "kind_display", "is_processed",
            "source", "source_name",
            "needs_processing",
            "name", "supplier", "supplier_name", "price", "unit", "quantity",
            "products",
            "created_at", "updated_at",
        ]
        read_only_fields = ["kind", "source"]

    def _item_make_instance(self):
        instance = getattr(self, "instance", None)
        return instance if isinstance(instance, ItemMake) else None

    def get_is_processed(self, obj):
        if not isinstance(obj, ItemMake):
            return False
        return obj.kind == ItemMake.Kind.PROCESSED

    def validate_needs_processing(self, value):
        instance = self._item_make_instance()
        if instance and instance.kind == ItemMake.Kind.PROCESSED:
            return False
        return value

    def validate(self, attrs):
        company = self._user_company()
        branch = self._auto_branch()
        instance = self._item_make_instance()
        supplier = attrs.get("supplier", getattr(instance, "supplier", None))
        kind = getattr(instance, "kind", ItemMake.Kind.RAW) if instance else ItemMake.Kind.RAW
        needs_processing = attrs.get(
            "needs_processing",
            getattr(instance, "needs_processing", False) if instance else False,
        )

        if kind == ItemMake.Kind.PROCESSED:
            attrs["needs_processing"] = False
        elif needs_processing and instance and instance.source_id:
            raise serializers.ValidationError({
                "needs_processing": "Обработанная позиция не может требовать обработки.",
            })

        if supplier is not None:
            if supplier.type != Client.StatusClient.SUPPLIERS:
                raise serializers.ValidationError({"supplier": "Выберите клиента с типом 'Поставщики'."})
            if company and supplier.company_id != company.id:
                raise serializers.ValidationError({"supplier": "Поставщик другой компании."})
            if branch and supplier.branch_id not in (None, branch.id):
                raise serializers.ValidationError({"supplier": "Поставщик другого филиала."})

        return attrs

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = self._user_company()
        branch = self._auto_branch()

        supplier_qs = Client.objects.filter(type=Client.StatusClient.SUPPLIERS)
        if company:
            supplier_qs = supplier_qs.filter(company=company)
        if branch:
            supplier_qs = supplier_qs.filter(branch__in=[None, branch])
        self.fields["supplier"].queryset = supplier_qs

        instance = self._item_make_instance()
        if instance and instance.kind == ItemMake.Kind.PROCESSED:
            self.fields["needs_processing"].read_only = True


class FinishedToRawTransferSerializer(serializers.ModelSerializer):
    """Чтение истории перемещений готовой продукции в сырьё."""
    product_name = serializers.CharField(source="product.name", read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = FinishedToRawTransfer
        fields = [
            "id", "product", "product_name", "raw_item", "quantity", "reason",
            "status", "user_name", "created_at", "canceled_at",
        ]

    def get_user_name(self, obj):
        u = getattr(obj, "user", None)
        if not u:
            return ""
        full = f"{(u.first_name or '').strip()} {(u.last_name or '').strip()}".strip()
        return full or getattr(u, "email", "") or "Пользователь"


class FinishedToRawMoveInputSerializer(serializers.Serializer):
    """Вход для POST /main/products/{id}/move-to-raw/."""
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_quantity(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError("Количество должно быть больше нуля.")
        return value


# ─────────────────────────────────────────────────────────────
# Инвентаризация (сверка остатков)
# ─────────────────────────────────────────────────────────────
class InventoryItemReadSerializer(serializers.ModelSerializer):
    product = serializers.UUIDField(source="object_id", read_only=True)

    class Meta:
        model = InventoryItem
        fields = ["id", "product", "product_name", "qty_system", "qty_fact", "diff"]


class InventoryReadSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    doc_no = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    items = InventoryItemReadSerializer(many=True, read_only=True)

    class Meta:
        model = Inventory
        fields = [
            "id", "doc_no", "warehouse", "status", "comment", "user_name",
            "created_at", "confirmed_at", "surplus_qty", "shortage_qty",
            "items_count", "items",
        ]

    def get_doc_no(self, obj):
        return f"INV-{str(obj.id)[:8].upper()}"

    def get_items_count(self, obj):
        return obj.items.count()

    def get_user_name(self, obj):
        u = getattr(obj, "user", None)
        if not u:
            return ""
        full = f"{(u.first_name or '').strip()} {(u.last_name or '').strip()}".strip()
        return full or getattr(u, "email", "") or "Пользователь"


class InventoryItemInputSerializer(serializers.Serializer):
    product = serializers.UUIDField()
    qty_system = serializers.DecimalField(max_digits=14, decimal_places=3)
    qty_fact = serializers.DecimalField(max_digits=14, decimal_places=3)

    def validate_qty_fact(self, value):
        if value is None or value < 0:
            raise serializers.ValidationError("Количество не может быть отрицательным.")
        return value

    def validate_qty_system(self, value):
        if value is None or value < 0:
            raise serializers.ValidationError("Количество не может быть отрицательным.")
        return value


class InventoryCreateSerializer(serializers.Serializer):
    warehouse = serializers.ChoiceField(choices=Inventory.Warehouse.choices)
    comment = serializers.CharField(required=False, allow_blank=True, default="")
    status = serializers.ChoiceField(
        choices=[Inventory.Status.DRAFT, Inventory.Status.CONFIRMED],
        required=False, default=Inventory.Status.DRAFT,
    )
    items = InventoryItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Добавьте хотя бы одну позицию.")
        return value


# ─────────────────────────────────────────────────────────────
# Нехватка готовой продукции (событие + уведомления)
# ─────────────────────────────────────────────────────────────
class StockShortageEventReadSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True, default="")
    agent_name = serializers.SerializerMethodField()

    class Meta:
        model = StockShortageEvent
        fields = [
            "id", "product", "product_name", "requested_qty", "available_qty",
            "agent", "agent_name", "source", "created_at",
        ]

    def get_agent_name(self, obj):
        u = getattr(obj, "agent", None)
        if not u:
            return ""
        full = f"{(u.first_name or '').strip()} {(u.last_name or '').strip()}".strip()
        return full or getattr(u, "email", "") or ""


class StockShortageEventCreateSerializer(serializers.Serializer):
    product = serializers.UUIDField()
    product_name = serializers.CharField(required=False, allow_blank=True, default="")
    requested_qty = serializers.DecimalField(max_digits=14, decimal_places=3)
    available_qty = serializers.DecimalField(max_digits=14, decimal_places=3)
    agent = serializers.UUIDField(required=False, allow_null=True)
    agent_name = serializers.CharField(required=False, allow_blank=True, default="")
    source = serializers.ChoiceField(
        choices=StockShortageEvent.Source.choices, required=False,
        default=StockShortageEvent.Source.TRANSFER,
    )


class StockMovementReadSerializer(serializers.ModelSerializer):
    """Read-only журнал движения склада."""
    class Meta:
        model = StockMovement
        fields = [
            "id", "type", "created_at", "object_id", "product_name", "warehouse",
            "qty_before", "change", "qty_after",
            "source_type", "source_id", "source_name",
            "target_type", "target_id", "target_name",
            "sender_id", "sender_name", "receiver_id", "receiver_name",
            "created_by_name", "comment", "ref_type", "ref_id",
        ]


class SupplierPurchaseReadSerializer(serializers.ModelSerializer):
    """Read-only история закупок в карточке поставщика."""
    purchased_at = serializers.SerializerMethodField()

    class Meta:
        model = SupplierPurchase
        fields = [
            "id", "product_name", "quantity", "unit", "amount",
            "payment_type", "purchased_at",
        ]

    def get_purchased_at(self, obj) -> str | None:
        if not obj.purchased_at:
            return None
        return timezone.localtime(obj.purchased_at).strftime("%Y-%m-%dT%H:%M:%S")


class ProductionRecordReadSerializer(serializers.ModelSerializer):
    """Read-only журнал производства (вкладка «Производство»)."""
    produced_at = serializers.SerializerMethodField()

    class Meta:
        model = ProductionRecord
        fields = [
            "id", "product", "product_name", "quantity", "unit", "cost_total",
            "produced_by", "produced_by_name", "produced_at", "shift",
        ]

    def get_produced_at(self, obj) -> str | None:
        # Местное время компании без смещения — договорённость docs/market/analytics.md.
        if not obj.produced_at:
            return None
        return timezone.localtime(obj.produced_at).strftime("%Y-%m-%dT%H:%M:%S")


class ItemMakeProcessSerializer(serializers.Serializer):
    input_quantity = serializers.DecimalField(max_digits=18, decimal_places=3, min_value=Decimal("0.001"))
    output_quantity = serializers.DecimalField(max_digits=18, decimal_places=3, min_value=Decimal("0.001"))
    name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    processing_cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=Decimal("0.00"), min_value=Decimal("0"),
    )
    target_item_make_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        inp = attrs["input_quantity"]
        out = attrs["output_quantity"]
        if out > inp:
            raise serializers.ValidationError({
                "output_quantity": "Выход не может быть больше входа (потери должны быть ≥ 0).",
            })
        return attrs


# ===========================
# Subreal / Acceptance / ReturnFromAgent
# ===========================
class ManufactureSubrealSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    product_name = serializers.ReadOnlyField(source="product.name")

    # Поля агента: имя, фамилия и номер машины
    agent_first_name = serializers.ReadOnlyField(source="agent.first_name")
    agent_last_name = serializers.ReadOnlyField(source="agent.last_name")
    agent_track_number = serializers.ReadOnlyField(source="agent.track_number")

    # вычисляемые — делаем через SerializerMethodField, чтобы не падать на None
    qty_remaining = serializers.SerializerMethodField()
    qty_on_agent = serializers.SerializerMethodField()
    transferred_products = serializers.SerializerMethodField()

    # “пилорама” на уровне конкретной передачи (разрешим только при create)
    is_sawmill = serializers.BooleanField(required=False, default=False)

    class Meta:
        model = ManufactureSubreal
        fields = [
            "id", "company", "branch",
            "external_ref",
            "user",
            "agent", "agent_first_name", "agent_last_name", "agent_track_number",
            "product", "product_name",
            "is_sawmill",
            "qty_transferred", "qty_accepted", "qty_returned",
            "qty_remaining", "qty_on_agent", "transferred_products",
            "status", "created_at",
        ]
        read_only_fields = [
            "id", "company", "branch", "external_ref", "user",
            "agent_first_name", "agent_last_name", "agent_track_number",
            "product_name",
            "qty_remaining", "qty_on_agent", "transferred_products",
            "status", "created_at",
        ]

    # ---- computed getters ----
    def get_qty_remaining(self, obj) -> int:
        return int(getattr(obj, "qty_remaining", 0) or 0)

    def get_qty_on_agent(self, obj) -> int:
        return int(getattr(obj, "qty_on_agent", 0) or 0)

    def get_transferred_products(self, obj) -> List[Dict[str, Any]]:
        ref = getattr(obj, "external_ref", None)
        if ref:
            rows = (
                ManufactureSubreal.objects
                .filter(company_id=obj.company_id, external_ref=ref)
                .select_related("product")
                .order_by("created_at", "id")
            )
        else:
            rows = [obj]

        return [
            {
                "subreal_id": row.id,
                "product_id": row.product_id,
                "product_name": getattr(getattr(row, "product", None), "name", "") or "",
                "qty_transferred": int(getattr(row, "qty_transferred", 0) or 0),
            }
            for row in rows
        ]

    # ---- init: ограничим queryset-ы по компании/филиалу ----
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        _restrict_pk_queryset_strict(self.fields.get("product"), Product.objects.all(), comp, br)
        if comp and self.fields.get("agent"):
            self.fields["agent"].queryset = User.objects.filter(company=comp)

    # ---- validation ----
    def validate(self, attrs):
        company = self._user_company()
        branch = self._auto_branch()

        # Для create — product обязателен
        if self.instance is None and attrs.get("product") is None:
            raise serializers.ValidationError({"product": "Обязательное поле."})

        product = attrs.get("product")
        if product is not None and product.company_id != getattr(company, "id", None):
            raise serializers.ValidationError({"product": "Товар другой компании."})

        # строгая проверка филиала: при наличии активного филиала товар либо глобальный, либо этого филиала
        if branch is not None and product is not None and product.branch_id not in (None, branch.id):
            raise serializers.ValidationError({"product": "Товар другого филиала."})

        agent = attrs.get("agent")
        if agent is not None:
            agent_company_id = getattr(agent, "company_id", None)
            if agent_company_id and agent_company_id != getattr(company, "id", None):
                raise serializers.ValidationError({"agent": "Агент другой компании."})

        # qty_transferred обязателен и >= 1 при создании
        qty = attrs.get("qty_transferred")
        if self.instance is None and qty is None:
            raise serializers.ValidationError({"qty_transferred": "Обязательное поле."})
        if qty is not None and qty < 1:
            raise serializers.ValidationError({"qty_transferred": "Минимум 1."})

        return attrs

    def create(self, validated_data):
        # сервер выставляет связь с компанией/филиалом/пользователем
        validated_data["user"] = self._user()
        validated_data["company"] = self._user_company()
        validated_data["branch"] = self._auto_branch()
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # запрещаем менять флаг после создания
        validated_data.pop("is_sawmill", None)
        return super().update(instance, validated_data)


class AcceptanceCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Acceptance
        fields = ["subreal", "qty"]
        extra_kwargs = {"subreal": {"queryset": ManufactureSubreal.objects.all()}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        req = self.context.get("request")
        comp = getattr(getattr(req, "user", None), "company", None)
        br = _active_branch(self)
        if comp and self.fields.get("subreal"):
            qs = ManufactureSubreal.objects.filter(company=comp)
            if br is not None:
                qs = qs.filter(branch=br)
            # если br None — все передачи компании
            self.fields["subreal"].queryset = qs

    def validate(self, attrs):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        user_company_id = getattr(user, "company_id", None)

        sub = attrs.get("subreal")
        qty = attrs.get("qty")
        if not sub:
            raise serializers.ValidationError({"subreal": "Обязательное поле."})
        if qty is None or qty < 1:
            raise serializers.ValidationError({"qty": "Минимум 1."})
        if qty > sub.qty_remaining:
            raise serializers.ValidationError({"qty": f"Доступно к приёму {sub.qty_remaining}."})
        if user_company_id and sub.company_id != user_company_id:
            raise serializers.ValidationError({"subreal": "Передача из другой компании."})
        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        company_id = getattr(user, "company_id", None)
        if not company_id:
            raise serializers.ValidationError({"company": "У пользователя не задана компания."})
        validated_data["company_id"] = company_id
        validated_data["accepted_by"] = user
        return super().create(validated_data)


class AcceptanceReadSerializer(serializers.ModelSerializer):
    subreal_id = serializers.UUIDField(source="subreal.id", read_only=True)
    product = serializers.CharField(source="subreal.product.name", read_only=True)
    agent = serializers.SerializerMethodField()
    accepted_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Acceptance
        fields = [
            "id", "company", "subreal_id", "product", "agent",
            "accepted_by", "accepted_by_name", "qty", "accepted_at",
        ]

    @staticmethod
    def _full_or_username(user):
        if not user:
            return None
        fn = getattr(user, "get_full_name", lambda: "")() or None
        return fn or getattr(user, "username", str(user))

    def get_agent(self, obj):
        return self._full_or_username(getattr(obj.subreal, "agent", None))

    def get_accepted_by_name(self, obj):
        return self._full_or_username(getattr(obj, "accepted_by", None))


# ===========================
# Supplier: receipt (оприходование)
# ===========================
class SupplierReceiptItemSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    qty = serializers.IntegerField(min_value=1)
    purchase_price = serializers.DecimalField(
        max_digits=11,
        decimal_places=3,
        required=False,
        allow_null=True,
    )
    price = serializers.DecimalField(
        max_digits=11,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    selling_price = serializers.DecimalField(
        max_digits=11,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    # ТЗ Н-04: пересчитать цену продажи от новой закупки по текущей наценке.
    recalc_price = serializers.BooleanField(required=False, default=False)
    # ТЗ Н-27: приход по варианту (размер/цвет): всё количество строки — в этот вариант;
    # либо раскладка variants=[{variant, qty}], сумма которой должна равняться qty строки.
    variant = serializers.UUIDField(required=False, allow_null=True)
    variants = serializers.ListField(
        child=serializers.DictField(), required=False, allow_empty=True
    )
    # ТЗ 26.10 Б-53 п.5: единица закупки (qty — в ней, purchase_price — за единицу)
    sale_unit_id = serializers.IntegerField(required=False, allow_null=True)


class SupplierReceiptCreateSerializer(serializers.Serializer):
    items = SupplierReceiptItemSerializer(many=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("items не может быть пустым.")
        return items


class SupplierReceiptItemReadSerializer(serializers.ModelSerializer):
    product = serializers.UUIDField(source="product.id", read_only=True)
    product_id = serializers.UUIDField(source="product.id", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_code = serializers.CharField(source="product.code", read_only=True)
    unit = serializers.CharField(source="product.unit", read_only=True, default="шт")
    returned_qty = serializers.SerializerMethodField()
    returnable_qty = serializers.SerializerMethodField()

    class Meta:
        model = SupplierReceiptItem
        fields = [
            "id", "product", "product_id", "product_name", "product_code",
            "qty", "purchase_price", "returned_qty", "returnable_qty", "unit",
        ]

    def get_returned_qty(self, obj):
        from apps.main.models import SupplierReturnItem
        ret = SupplierReturnItem.objects.filter(receipt_item=obj, supplier_return__status="posted").aggregate(s=Sum("qty"))["s"]
        if ret is None:
            return "0"
        return str(Decimal(str(ret)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))

    def get_returnable_qty(self, obj):
        from apps.main.models import SupplierReturnItem
        ret = SupplierReturnItem.objects.filter(receipt_item=obj, supplier_return__status="posted").aggregate(s=Sum("qty"))["s"] or Decimal("0")
        purchased = Decimal(str(obj.qty))
        stock = Decimal(str(getattr(obj.product, "quantity", 0) or 0))
        rem = max(Decimal("0"), purchased - Decimal(str(ret)))
        returnable = max(Decimal("0"), min(stock, rem))
        return str(returnable.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


class SupplierReceiptReadSerializer(serializers.ModelSerializer):
    supplier_id = serializers.UUIDField(read_only=True)
    supplier_name = serializers.CharField(source="supplier.full_name", read_only=True)
    supplier_llc = serializers.CharField(source="supplier.llc", read_only=True, allow_null=True)
    supplier_phone = serializers.CharField(source="supplier.phone", read_only=True)
    created_by_id = serializers.UUIDField(read_only=True, allow_null=True)
    created_by_name = serializers.SerializerMethodField()
    items = SupplierReceiptItemReadSerializer(many=True, read_only=True)
    total_amount = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    returned_amount = serializers.SerializerMethodField()
    returned_qty = serializers.SerializerMethodField()
    has_returns = serializers.SerializerMethodField()
    # ТЗ Н-10: способ оплаты прихода (выводится из сделки/расхода/журнала закупок)
    payment_type = serializers.SerializerMethodField()
    paid_amount = serializers.SerializerMethodField()
    debt_amount = serializers.SerializerMethodField()
    deal_id = serializers.SerializerMethodField()
    cashbox = serializers.SerializerMethodField()
    cashbox_name = serializers.SerializerMethodField()
    shift = serializers.SerializerMethodField()
    order_id = serializers.UUIDField(read_only=True, allow_null=True)  # ТЗ Н-29

    class Meta:
        model = SupplierReceipt
        fields = [
            "id",
            "order_id",
            "company",
            "branch",
            "supplier_id",
            "supplier_name",
            "supplier_llc",
            "supplier_phone",
            "created_by_id",
            "created_by_name",
            "created_at",
            "items_count",
            "total_amount",
            "returned_amount",
            "returned_qty",
            "has_returns",
            "payment_type",
            "paid_amount",
            "debt_amount",
            "deal_id",
            "cashbox",
            "cashbox_name",
            "shift",
            "discount",  # ТЗ Н-33
            "discount_percent",
            "extra_costs",
            "items",
        ]

    def _pay_info(self, obj):
        from apps.main.views import _receipt_payment_info
        cache = self.context.get("receipt_info")
        if cache is None:
            cache = self.context["receipt_info"] = {}
        key = str(obj.id)
        if key not in cache:
            cache.update(_receipt_payment_info([obj]))
        return cache.get(key) or {}

    def get_payment_type(self, obj):
        return self._pay_info(obj).get("payment_type")

    def get_paid_amount(self, obj):
        return self._pay_info(obj).get("paid_amount")

    def get_debt_amount(self, obj):
        return self._pay_info(obj).get("debt_amount")

    def get_deal_id(self, obj):
        return self._pay_info(obj).get("deal_id")

    def get_cashbox(self, obj):
        return self._pay_info(obj).get("cashbox")

    def get_cashbox_name(self, obj):
        return self._pay_info(obj).get("cashbox_name")

    def get_shift(self, obj):
        return self._pay_info(obj).get("shift")

    def get_created_by_name(self, obj):
        u = obj.created_by
        if u is None:
            return None
        return getattr(u, "first_name", None) or getattr(u, "email", None)

    def get_total_amount(self, obj):
        annotated = getattr(obj, "total_amount", None)
        if annotated is not None:
            return str(
                Decimal(str(annotated)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )

        total = Decimal("0")
        for item in obj.items.all():
            price = item.purchase_price if item.purchase_price is not None else Decimal("0")
            total += Decimal(str(item.qty)) * Decimal(str(price))
        return str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def get_items_count(self, obj):
        return obj.items.count()

    def get_returned_amount(self, obj):
        from apps.main.models import SupplierReturnItem
        qs = SupplierReturnItem.objects.filter(receipt_item__receipt=obj, supplier_return__status="posted")
        expr = ExpressionWrapper(F("qty") * F("purchase_price"), output_field=DecimalField(max_digits=12, decimal_places=3))
        res = qs.aggregate(s=Sum(expr))["s"]
        if res is None:
            return "0.00"
        return str(Decimal(str(res)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def get_returned_qty(self, obj):
        from apps.main.models import SupplierReturnItem
        res = SupplierReturnItem.objects.filter(receipt_item__receipt=obj, supplier_return__status="posted").aggregate(s=Sum("qty"))["s"]
        if res is None:
            return "0"
        return str(Decimal(str(res)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))

    def get_has_returns(self, obj):
        from apps.main.models import SupplierReturnItem
        return SupplierReturnItem.objects.filter(receipt_item__receipt=obj, supplier_return__status="posted").exists()


# ===========================
#  Supplier Returns Serializers
# ===========================
class SupplierReturnItemReadSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(source="product.id", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_code = serializers.CharField(source="product.code", read_only=True)
    unit = serializers.CharField(source="product.unit", read_only=True, default="шт")
    receipt_item_id = serializers.UUIDField(source="receipt_item.id", read_only=True, allow_null=True)
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = SupplierReturnItem
        fields = [
            "id", "product_id", "product_name", "product_code", "unit",
            "qty", "purchase_price", "line_total", "receipt_item_id",
        ]

    def get_line_total(self, obj):
        p = obj.purchase_price if obj.purchase_price is not None else Decimal("0")
        tot = Decimal(str(obj.qty)) * Decimal(str(p))
        return str(tot.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class SupplierReturnReadSerializer(serializers.ModelSerializer):
    supplier_id = serializers.UUIDField(source="supplier.id", read_only=True)
    supplier_name = serializers.CharField(source="supplier.full_name", read_only=True)
    receipt_id = serializers.UUIDField(source="receipt.id", read_only=True, allow_null=True)
    created_by_name = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()
    items = SupplierReturnItemReadSerializer(many=True, read_only=True)

    class Meta:
        model = SupplierReturn
        fields = [
            "id", "supplier_id", "supplier_name", "receipt_id",
            "created_at", "created_by_name", "reason", "comment",
            "compensation", "status", "items_count", "total_amount",
            "items",
        ]

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["uuid"] = ret["id"]
        ret["supplierId"] = ret["supplier_id"]
        ret["compensation_type"] = ret["compensation"]
        ret["lines"] = ret["items"]
        return ret

    def get_created_by_name(self, obj):
        u = obj.created_by
        if u is None:
            return None
        return getattr(u, "first_name", None) or getattr(u, "email", None)

    def get_items_count(self, obj):
        return obj.items.count()

    def get_total_amount(self, obj):
        annotated = getattr(obj, "total_amount", None)
        if annotated is not None:
            return str(Decimal(str(annotated)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        total = Decimal("0")
        for item in obj.items.all():
            price = item.purchase_price if item.purchase_price is not None else Decimal("0")
            total += Decimal(str(item.qty)) * Decimal(str(price))
        return str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class SupplierReturnCreateItemSerializer(serializers.Serializer):
    product_id = serializers.UUIDField(required=True)
    qty = serializers.DecimalField(max_digits=12, decimal_places=3, required=True)
    purchase_price = serializers.DecimalField(max_digits=12, decimal_places=3, required=False, allow_null=True)
    receipt_item_id = serializers.UUIDField(required=False, allow_null=True)


class SupplierReturnCreateSerializer(serializers.Serializer):
    receipt_id = serializers.UUIDField(required=False, allow_null=True)
    reason = serializers.ChoiceField(choices=SupplierReturn.Reason.choices, required=True)
    comment = serializers.CharField(required=False, allow_blank=True, default="")
    compensation = serializers.ChoiceField(choices=SupplierReturn.Compensation.choices, required=True)
    cashbox_id = serializers.UUIDField(required=False, allow_null=True)
    items = SupplierReturnCreateItemSerializer(many=True, required=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("Укажите хотя бы одну позицию.")
        return items

    def validate(self, attrs):
        reason = attrs.get("reason")
        comment = (attrs.get("comment") or "").strip()
        if reason == SupplierReturn.Reason.OTHER and not comment:
            raise serializers.ValidationError({"comment": "Укажите комментарий при выборе причины 'Другое'."})
        return attrs


class ProductExpiryBatchSerializer(serializers.ModelSerializer):
    days_left = serializers.SerializerMethodField()
    supplier_name = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True)

    class Meta:
        model = ProductExpiryBatch
        fields = ["id", "quantity", "remaining_quantity", "received_at", "expires_at", "days_left", "status", "source_kind", "source_id", "supplier_name", "created_by_name"]

    def get_days_left(self, obj):
        return (obj.expires_at - timezone.localdate()).days if obj.expires_at else None

    def get_supplier_name(self, obj):
        if obj.source_kind != "purchase" or not obj.source_id:
            return None
        try:
            return SupplierReceipt.objects.select_related("supplier").get(id=obj.source_id).supplier.full_name
        except (SupplierReceipt.DoesNotExist, ValueError):
            return None


class ProductPurchaseBatchSerializer(serializers.ModelSerializer):
    """
    Партия закупки товара — строка оприходования (SupplierReceiptItem),
    обогащённая данными самого оприходования (дата, поставщик, кто провёл).

    Используется как «история закупок» товара: каждая партия (своя цена и
    количество) сохраняется отдельной записью и не перезаписывается следующей.
    """

    receipt_id = serializers.UUIDField(source="receipt.id", read_only=True)
    qty = serializers.IntegerField(read_only=True)
    purchase_price = serializers.DecimalField(
        max_digits=11, decimal_places=3, read_only=True
    )
    line_total = serializers.SerializerMethodField()
    supplier_id = serializers.UUIDField(source="receipt.supplier_id", read_only=True)
    supplier_name = serializers.CharField(
        source="receipt.supplier.full_name", read_only=True
    )
    created_at = serializers.DateTimeField(source="receipt.created_at", read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SupplierReceiptItem
        fields = [
            "id",
            "receipt_id",
            "qty",
            "purchase_price",
            "line_total",
            "supplier_id",
            "supplier_name",
            "created_at",
            "created_by_name",
        ]

    def get_line_total(self, obj):
        price = obj.purchase_price if obj.purchase_price is not None else Decimal("0")
        total = Decimal(str(obj.qty)) * Decimal(str(price))
        return str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def get_created_by_name(self, obj):
        u = getattr(obj.receipt, "created_by", None)
        return getattr(u, "email", None) if u else None


class ReturnCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReturnFromAgent
        fields = ["subreal", "qty", "is_defect"]
        extra_kwargs = {
            "subreal": {"queryset": ManufactureSubreal.objects.all()},
            "is_defect": {"required": False, "default": False},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        req = self.context.get("request")
        comp = getattr(getattr(req, "user", None), "company", None)
        usr = getattr(req, "user", None)
        br = _active_branch(self)
        if comp and self.fields.get("subreal"):
            # Возврат создаёт агент, поэтому ограничиваем subreal-ы его передачами.
            qs = ManufactureSubreal.objects.filter(company=comp)
            if usr and getattr(usr, "id", None):
                qs = qs.filter(agent_id=usr.id)
            if br is not None:
                qs = qs.filter(branch=br)
            # если br None — все передачи компании
            self.fields["subreal"].queryset = qs

    def validate(self, attrs):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        user_company_id = getattr(user, "company_id", None)
        br = _active_branch(self)

        sub = attrs.get("subreal")
        qty = attrs.get("qty")
        if not sub:
            raise serializers.ValidationError({"subreal": "Обязательное поле."})
        if qty is None or qty < 1:
            raise serializers.ValidationError({"qty": "Минимум 1."})

        # Безопасность: subreal должен быть из компании пользователя
        if user_company_id and sub.company_id != user_company_id:
            raise serializers.ValidationError({"subreal": "Передача из другой компании."})

        # ВАЖНО: "на руках" считаем по ВСЕМ партиям (subreal) этого товара + продажи.
        # Возврат может быть больше остатка по одной партии — тогда разбиваем по нескольким subreal.
        company_id = user_company_id or sub.company_id
        agent_id = getattr(user, "id", None)
        if not agent_id:
            raise serializers.ValidationError({"returned_by": "Пользователь не определён."})

        candidates = ManufactureSubreal.objects.filter(
            company_id=company_id,
            agent_id=agent_id,
            product_id=sub.product_id,
        )
        if br is not None:
            candidates = candidates.filter(branch=br)

        candidates = candidates.order_by("-created_at", "-id")

        need = int(qty)
        total_on_hand = 0
        split_plan = []  # list[(subreal, take_qty)]

        for s in candidates:
            on_hand = int(s.get_qty_on_hand_with_sales(company_id=company_id) or 0)
            if on_hand <= 0:
                continue
            total_on_hand += on_hand
            if need <= 0:
                continue
            take = min(need, on_hand)
            if take > 0:
                split_plan.append((s, take))
                need -= take

        if int(qty) > total_on_hand:
            raise serializers.ValidationError({"qty": f"На руках {total_on_hand}."})

        # сохраняем план разбиения для create()
        attrs["_split_plan"] = split_plan
        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        company_id = getattr(user, "company_id", None)
        if not company_id:
            raise serializers.ValidationError({"company": "У пользователя не задана компания."})
        split_plan = validated_data.pop("_split_plan", None) or []
        is_defect = bool(validated_data.get("is_defect", False))
        qty_total = int(validated_data.get("qty") or 0)
        if qty_total < 1:
            raise serializers.ValidationError({"qty": "Минимум 1."})
        if not split_plan:
            raise serializers.ValidationError({"qty": "На руках 0."})

        created = []
        for subreal, take in split_plan:
            obj = ReturnFromAgent.objects.create(
                company_id=company_id,
                branch_id=getattr(subreal, "branch_id", None),
                subreal=subreal,
                returned_by=user,
                qty=int(take),
                is_defect=is_defect,
                status=ReturnFromAgent.Status.PENDING,
            )
            created.append(obj)
        return created


class ReturnReadSerializer(serializers.ModelSerializer):
    subreal_id = serializers.UUIDField(source="subreal.id", read_only=True)
    product = serializers.CharField(source="subreal.product.name", read_only=True)
    agent = serializers.SerializerMethodField()
    returned_by_name = serializers.SerializerMethodField()
    accepted_by_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    client_name = serializers.CharField(source="client.full_name", read_only=True, default=None)

    product_name = serializers.CharField(source="subreal.product.name", read_only=True)

    class Meta:
        model = ReturnFromAgent
        fields = [
            "id", "company", "subreal_id", "product", "product_name", "agent",
            "qty", "is_defect", "amount",
            "client", "client_name",
            "status", "status_display",
            "returned_by", "returned_by_name",
            "accepted_by", "accepted_by_name",
            "returned_at", "accepted_at",
        ]

    @staticmethod
    def _full_or_username(user):
        if not user:
            return None
        fn = getattr(user, "get_full_name", lambda: "")() or None
        return fn or getattr(user, "username", str(user))

    def get_agent(self, obj):
        return self._full_or_username(getattr(obj.subreal, "agent", None))

    def get_returned_by_name(self, obj):
        return self._full_or_username(getattr(obj, "returned_by", None))

    def get_accepted_by_name(self, obj):
        return self._full_or_username(getattr(obj, "accepted_by", None))


class ReturnApproveSerializer(serializers.Serializer):
    def save(self, **kwargs):
        ret: ReturnFromAgent = self.context["return_obj"]
        user = self.context["request"].user
        ret.accept(by_user=user)
        return ret


class ReturnRejectSerializer(serializers.Serializer):
    def save(self, **kwargs):
        ret: ReturnFromAgent = self.context["return_obj"]
        user = self.context["request"].user
        ret.reject(by_user=user)
        return ret


# ===========================
# BULK выдача агенту
# ===========================
class BulkSubrealItemSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    qty_transferred = serializers.IntegerField(min_value=1)
    is_sawmill = serializers.BooleanField(required=False, default=False)


class BulkSubrealCreateSerializer(serializers.Serializer):
    agent = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    items = BulkSubrealItemSerializer(many=True, allow_empty=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        req = self.context.get("request")
        comp = getattr(getattr(req, "user", None), "company", None)
        br = _active_branch(self)
        if comp:
            self.fields["agent"].queryset = User.objects.filter(company=comp)
            prod_qs = Product.objects.filter(company=comp)
            if br is not None:
                prod_qs = prod_qs.filter(branch=br)
            # если br None — все товары компании
            self.fields["items"].child.fields["product"].queryset = prod_qs

    def validate(self, attrs):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        company_id = getattr(user, "company_id", None)
        if not company_id:
            raise serializers.ValidationError("У пользователя не задана компания.")

        agent = attrs["agent"]
        agent_company_id = getattr(agent, "company_id", None)
        if agent_company_id and agent_company_id != company_id:
            raise serializers.ValidationError({"agent": "Агент принадлежит другой компании."})

        for i, item in enumerate(attrs["items"]):
            prod = item["product"]
            if prod.company_id != company_id:
                raise serializers.ValidationError({"items": {i: {"product": "Товар другой компании."}}})

        # сжимаем дубликаты по product, суммируя qty и OR по is_sawmill
        merged: Dict[Any, Dict[str, Any]] = {}
        for item in attrs["items"]:
            key = item["product"].pk
            prev = merged.get(key)
            if prev is None:
                merged[key] = {
                    "product": item["product"],
                    "qty_transferred": int(item["qty_transferred"]),
                    "is_sawmill": bool(item.get("is_sawmill", False)),
                }
            else:
                prev["qty_transferred"] += int(item["qty_transferred"])
                # если хотя бы один из дублей is_sawmill=True — считаем TRUE
                prev["is_sawmill"] = prev["is_sawmill"] or bool(item.get("is_sawmill", False))

        # убираем позиции с нулём и убеждаемся, что что-то осталось
        attrs["items"] = [it for it in merged.values() if int(it["qty_transferred"]) > 0]
        if not attrs["items"]:
            raise serializers.ValidationError({"items": "Нет позиций с количеством > 0."})

        return attrs


# ===========================
# Аггрегированные ответы для агента
# ===========================
class AgentSubrealSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    created_at = serializers.DateTimeField()
    qty_transferred = serializers.IntegerField()
    qty_accepted = serializers.IntegerField()
    qty_returned = serializers.IntegerField()


class AgentProductOnHandSerializer(serializers.Serializer):
    product = serializers.UUIDField()
    product_name = serializers.CharField()
    qty_on_hand = serializers.IntegerField()
    last_movement_at = serializers.DateTimeField(allow_null=True)
    subreals = AgentSubrealSerializer(many=True)


class AgentInfoSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    first_name = serializers.CharField(allow_blank=True)
    last_name = serializers.CharField(allow_blank=True)
    track_number = serializers.CharField(allow_blank=True, allow_null=True)


class AgentWithProductsSerializer(serializers.Serializer):
    agent = AgentInfoSerializer()
    products = AgentProductOnHandSerializer(many=True)


class GlobalProductReadSerializer(serializers.ModelSerializer):
    brand = serializers.CharField(source="brand.name", read_only=True)
    category = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = GlobalProduct
        fields = ["id", "name", "barcode", "brand", "category"]


class PromoRuleSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    # Покажем удобные подписи
    product_name = serializers.ReadOnlyField(source="product.name")
    brand_name = serializers.ReadOnlyField(source="brand.name")
    category_name = serializers.ReadOnlyField(source="category.name")

    class Meta:
        model = PromoRule
        fields = [
            "id", "company", "branch",
            "title",
            "product", "product_name",
            "brand", "brand_name",
            "category", "category_name",
            "min_qty", "gift_qty", "inclusive",
            "priority",
            "active_from", "active_to", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "company", "branch",
            "product_name", "brand_name", "category_name",
            "created_at", "updated_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        # Ограничим product / brand / category в выпадашках так же, как ты делаешь в других местах:
        _restrict_pk_queryset_strict(self.fields.get("product"), Product.objects.all(), comp, br)
        _restrict_pk_queryset_strict(self.fields.get("brand"), ProductBrand.objects.all(), comp, br)
        _restrict_pk_queryset_strict(self.fields.get("category"), ProductCategory.objects.all(), comp, br)

    def validate(self, attrs):
        """
        Правила:
          - только один таргет: product ИЛИ brand ИЛИ category ИЛИ ни одного (глобальное правило)
          - min_qty >= 1, gift_qty >= 1
          - объект должен принадлежать той же компании и (если есть филиал) филиалу
        Остальное уже проверяет модель .clean(), но мы словим пораньше.
        """
        product = attrs.get("product") or getattr(self.instance, "product", None)
        brand = attrs.get("brand") or getattr(self.instance, "brand", None)
        category = attrs.get("category") or getattr(self.instance, "category", None)

        chosen = [product, brand, category]
        if sum(bool(x) for x in chosen) > 1:
            raise serializers.ValidationError("Укажите только product ИЛИ brand ИЛИ category (или ничего).")

        min_qty = attrs.get("min_qty", getattr(self.instance, "min_qty", None))
        gift_qty = attrs.get("gift_qty", getattr(self.instance, "gift_qty", None))

        if min_qty is not None and min_qty < 1:
            raise serializers.ValidationError({"min_qty": "Порог должен быть ≥ 1."})
        if gift_qty is not None and gift_qty < 1:
            raise serializers.ValidationError({"gift_qty": "Подарок должен быть ≥ 1."})

        return attrs


class AgentRequestCartSubmitSerializer(serializers.Serializer):
    """
    Агент нажимает 'отправить заявку владельцу'.
    cart.submit() делает:
      - фиксирует подарки (gift_quantity/total_quantity/price_snapshot),
      - ставит status='submitted', submitted_at=...
    """
    def save(self, **kwargs):
        cart: AgentRequestCart = self.context["cart_obj"]
        cart.submit()
        return cart


class AgentRequestCartApproveSerializer(serializers.Serializer):
    """
    Владелец/админ нажимает 'одобрить'.
    cart.approve(by_user=request.user) делает:
      - проверяет остатки,
      - списывает Product.quantity,
      - создаёт ManufactureSubreal под агента с is_sawmill=True,
      - связывает эти subreal с позициями,
      - ставит status='approved'
    """
    def save(self, **kwargs):
        cart: AgentRequestCart = self.context["cart_obj"]
        user = self.context["request"].user
        cart.approve(by_user=user)
        return cart


class AgentRequestCartRejectSerializer(serializers.Serializer):
    """
    Владелец/админ отклоняет.
    cart.reject(by_user=request.user) просто помечает статус='rejected'
    """
    def save(self, **kwargs):
        cart: AgentRequestCart = self.context["cart_obj"]
        user = self.context["request"].user
        cart.reject(by_user=user)
        return cart


class AgentRequestItemSerializer(serializers.ModelSerializer):
    product_name = serializers.ReadOnlyField(source="product.name")
    product_barcode = serializers.ReadOnlyField(source="product.barcode")
    subreal_id = serializers.ReadOnlyField(source="subreal.id")
    product_image_url = serializers.SerializerMethodField()

    class Meta:
        model = AgentRequestItem
        ref_name = "MainAgentRequestItem"
        fields = [
            "id",
            "cart",
            "product", "product_name", "product_barcode",
            "product_image_url",
            "quantity_requested",
            "gift_quantity",
            "total_quantity",
            "price_snapshot",
            "subreal", "subreal_id",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id",
            "gift_quantity",
            "total_quantity",
            "price_snapshot",
            "subreal", "subreal_id",
            "created_at", "updated_at",
            "product_image_url",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        req = self.context.get("request")
        user = getattr(req, "user", None) if req else None
        comp = getattr(user, "company", None)
        br = _active_branch(self)

        # Ограничим queryset для product и cart по company/branch и по правам агента
        if comp and self.fields.get("product"):
            prod_qs = Product.objects.filter(company=comp)
            if br is not None:
                prod_qs = prod_qs.filter(branch=br)
            # если br None — все товары компании
            self.fields["product"].queryset = prod_qs

        if self.fields.get("cart"):
            # Не ограничиваем по company/branch на этом уровне,
            # чтобы не получать ложные "не существует".
            cart_qs = AgentRequestCart.objects.all()

            # агент может создавать строки только в своих корзинах
            if user and not _is_owner_like(user):
                cart_qs = cart_qs.filter(agent=user)

            self.fields["cart"].queryset = cart_qs

    def get_product_image_url(self, obj):
        """
        Возвращаем URL главной фотки товара (is_primary=True),
        если нет главной — просто первую.
        """
        product = getattr(obj, "product", None)
        if not product:
            return None

        # у продукта есть related_name="images"
        images_qs = getattr(product, "images", None)
        if images_qs is None:
            return None

        # сначала пытаемся взять основную
        primary = images_qs.filter(is_primary=True).first()
        img_obj = primary or images_qs.first()
        if not img_obj or not img_obj.image:
            return None

        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(img_obj.image.url)
        return img_obj.image.url

    def validate(self, data):
        """
        Проверяем:
          - cart в статусе draft
          - product из той же компании/филиала
          - quantity_requested >=1
          - есть ли столько товара на складе
        """
        cart = data.get("cart") or getattr(self.instance, "cart", None)
        product = data.get("product") or getattr(self.instance, "product", None)
        qty = data.get("quantity_requested", getattr(self.instance, "quantity_requested", None))

        if not cart:
            raise serializers.ValidationError({"cart": "Обязательное поле."})

        if cart.status not in (AgentRequestCart.Status.DRAFT, AgentRequestCart.Status.SUBMITTED):
            raise serializers.ValidationError("Нельзя менять позиции: корзина не в черновике или отправлена.")

        if not product:
            raise serializers.ValidationError({"product": "Обязательное поле."})

        if product.company_id != cart.company_id:
            raise serializers.ValidationError({"product": "Товар другой компании."})
        if cart.branch_id and product.branch_id not in (None, cart.branch_id):
            raise serializers.ValidationError({"product": "Товар другого филиала."})

        # защита от чужой компании
        comp = (
            getattr(self.context.get("request", None), "user", None) and
            (getattr(self.context["request"].user, "company", None)
             or getattr(self.context["request"].user, "owned_company", None)
             or getattr(getattr(self.context["request"].user, "branch", None), "company", None))
        )
        if comp and cart.company_id != comp.id:
            raise serializers.ValidationError({"cart": "Корзина другой компании."})

        if qty is None or qty < 1:
            raise serializers.ValidationError({"quantity_requested": "Количество должно быть ≥ 1."})

        # мягкая проверка склада
        if product.quantity < qty:
            raise serializers.ValidationError(
                {"quantity_requested": f"Недостаточно '{product.name}' на складе. Доступно: {product.quantity}"}
            )

        return data

    @transaction.atomic
    def create(self, validated_data):
        cart = validated_data["cart"]
        if cart.status not in (AgentRequestCart.Status.DRAFT, AgentRequestCart.Status.SUBMITTED):
            raise serializers.ValidationError("Добавлять можно только в черновик или отправленную заявку.")
        # вручную подарки не ставим — они рассчитываются на submit()
        return super().create(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        cart = instance.cart
        if cart.status not in (AgentRequestCart.Status.DRAFT, AgentRequestCart.Status.SUBMITTED):
            raise serializers.ValidationError("Редактировать можно только черновик или отправленную заявку.")
        instance.product = validated_data.get("product", instance.product)
        instance.quantity_requested = validated_data.get("quantity_requested", instance.quantity_requested)
        instance.save(update_fields=["product", "quantity_requested", "updated_at"])
        return instance


class AgentRequestCartSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    agent = serializers.ReadOnlyField(source="agent.id")
    agent_name = serializers.SerializerMethodField(read_only=True)

    client_name = serializers.ReadOnlyField(source="client.full_name")

    approved_by_name = serializers.SerializerMethodField(read_only=True)

    items = AgentRequestItemSerializer(many=True, read_only=True)

    total_requested = serializers.SerializerMethodField()
    total_gift = serializers.SerializerMethodField()
    total_all = serializers.SerializerMethodField()

    class Meta:
        model = AgentRequestCart
        ref_name = "MainAgentRequestCart"
        fields = [
            "id", "company", "branch",
            "agent", "agent_name",
            "client", "client_name",
            "status", "note",
            "submitted_at", "approved_at", "approved_by", "approved_by_name",
            "items",
            "total_requested", "total_gift", "total_all",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "company", "branch",
            "agent", "agent_name",
            "client_name",
            "status",
            "submitted_at", "approved_at", "approved_by", "approved_by_name",
            "items",
            "total_requested", "total_gift", "total_all",
            "created_at", "updated_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Ограничим выбор клиента: как в других местах
        comp = self._user_company()
        br = self._auto_branch()
        _restrict_pk_queryset_strict(self.fields.get("client"), Client.objects.all(), comp, br)

    def get_agent_name(self, obj):
        """
        Возвращаем человеко-читаемое имя агента:
        - сначала Имя + Фамилия
        - потом track_number (если вдруг у него нет имени)
        - потом email как самый последний fallback
        """
        u = getattr(obj, "agent", None)
        if not u:
            return ""

        full = f"{(u.first_name or '').strip()} {(u.last_name or '').strip()}".strip()
        if full:
            return full

        if getattr(u, "track_number", None):
            return u.track_number

        # last fallback: email или id
        return u.email or str(u.id)

    def get_agent_track_number(self, obj):
        u = getattr(obj, "agent", None)
        return getattr(u, "track_number", None)

    def get_approved_by_name(self, obj):
        u = getattr(obj, "approved_by", None)
        if not u:
            return None
        full = getattr(u, "get_full_name", lambda: "")() or ""
        return full or getattr(u, "username", None) or str(u.id)

    def get_total_requested(self, obj):
        # сумма quantity_requested по всем позициям
        return sum((it.quantity_requested for it in obj.items.all()), 0)

    def get_total_gift(self, obj):
        # сумма gift_quantity — рассчитывается и фиксируется на submit()
        return sum((it.gift_quantity for it in obj.items.all()), 0)

    def get_total_all(self, obj):
        # сумма total_quantity (requested + gift)
        return sum((it.total_quantity for it in obj.items.all()), 0)

    def validate_client(self, client):
        """
        Повторяем branch-валидацию как в других сериализаторах (ClientDeal, ObjectSale и т.д.)
        """
        if client is None:
            return None
        company = self._user_company()
        branch = self._auto_branch()
        if client.company_id != company.id:
            raise serializers.ValidationError("Клиент принадлежит другой компании.")
        if branch is not None and client.branch_id != branch.id:
            raise serializers.ValidationError("Клиент другого филиала.")
        # branch None — клиент из любого филиала компании
        return client

    def create(self, validated_data):
        """
        создаём черновик. agent = текущий пользователь.
        company/branch нам уже зафигачит CompanyBranchReadOnlyMixin.create(),
        но agent нужно подставить явно.
        """
        user = self.context["request"].user
        validated_data["agent"] = user
        cart = super().create(validated_data)
        return cart

    def update(self, instance, validated_data):
        """
        В draft агент может менять только:
          - client
          - note
        Статусы и системные поля руками менять нельзя.
        """
        if instance.status != AgentRequestCart.Status.DRAFT:
            raise serializers.ValidationError("Редактировать можно только черновик.")

        # client уже прошёл validate_client
        if "client" in validated_data:
            instance.client = validated_data["client"]

        if "note" in validated_data:
            instance.note = validated_data["note"]

        # branch/company ставит миксин update() сам, но status трогать нельзя
        super().update(instance, {})  # чтобы миксин прописал company/branch
        instance.save(update_fields=["client", "note", "branch", "updated_at"])
        return instance


class MarketSaleEmployeePayProfileSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    # ТЗ 12.21: branch можно передать явно (null = схема для всех филиалов) и он всегда возвращается.
    # Не передан -> как раньше: активный филиал запроса или null.
    branch = serializers.PrimaryKeyRelatedField(
        queryset=Branch.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = MarketSaleEmployeePayProfile
        fields = [
            "id",
            "company",
            "branch",
            "user",
            "pay_scheme",
            "monthly_base_salary",
            "sales_percent",
            "per_item_amount",
            "per_shift_amount",  # ТЗ 26.10 Б-63
        ]
        read_only_fields = ["id", "company"]

    def get_fields(self):
        fields = super().get_fields()
        company = self._user_company()
        if company and hasattr(User, "company_id"):
            fields["user"].queryset = User.objects.filter(company_id=company.id)
        if company and "branch" in fields:
            fields["branch"].queryset = Branch.objects.filter(company_id=company.id)
        return fields

    def create(self, validated_data):
        company = self._user_company()
        if company:
            validated_data["company"] = company
        if "branch" not in validated_data:
            validated_data["branch"] = self._auto_branch()
        return serializers.ModelSerializer.create(self, validated_data)

    def update(self, instance, validated_data):
        company = self._user_company()
        if company:
            validated_data["company"] = company
        return serializers.ModelSerializer.update(self, instance, validated_data)

    def validate(self, attrs):
        data = super().validate(attrs)
        scheme = data.get("pay_scheme")
        if scheme is None and self.instance:
            scheme = self.instance.pay_scheme
        base = data.get("monthly_base_salary")
        if base is None and self.instance:
            base = self.instance.monthly_base_salary
        pct = data.get("sales_percent")
        if pct is None and self.instance:
            pct = self.instance.sales_percent
        base = base or Decimal("0")
        pct = pct or Decimal("0")
        if scheme == MarketSaleEmployeePayProfile.PayScheme.SALARY:
            if base <= 0:
                raise serializers.ValidationError(
                    {"monthly_base_salary": "Для схемы «Оклад» укажите оклад больше 0."}
                )
        elif scheme == MarketSaleEmployeePayProfile.PayScheme.PERCENT:
            if pct <= 0:
                raise serializers.ValidationError(
                    {"sales_percent": "Для схемы «Процент» укажите процент больше 0."}
                )
        elif scheme == MarketSaleEmployeePayProfile.PayScheme.SALARY_PLUS_PERCENT:
            if base <= 0 or pct <= 0:
                raise serializers.ValidationError(
                    "Для схемы «Оклад + процент» задайте и оклад, и процент больше 0."
                )
        # ТЗ ч.13, 3: схемы «за проданный товар»
        per_item = data.get("per_item_amount")
        if per_item is None and self.instance:
            per_item = self.instance.per_item_amount
        per_item = per_item or Decimal("0")
        PS = MarketSaleEmployeePayProfile.PayScheme
        if scheme in (PS.PER_ITEM, PS.SALARY_PLUS_PER_ITEM, PS.PERCENT_PLUS_PER_ITEM) and per_item <= 0:
            raise serializers.ValidationError(
                {"per_item_amount": "Для этой схемы укажите сумму за проданный товар больше 0."}
            )
        if scheme == PS.SALARY_PLUS_PER_ITEM and base <= 0:
            raise serializers.ValidationError({"monthly_base_salary": "Укажите оклад больше 0."})
        if scheme == PS.PERCENT_PLUS_PER_ITEM and pct <= 0:
            raise serializers.ValidationError({"sales_percent": "Укажите процент больше 0."})
        # ТЗ 26.10 Б-63: сменные схемы
        per_shift = data.get("per_shift_amount")
        if per_shift is None and self.instance:
            per_shift = self.instance.per_shift_amount
        if scheme in (PS.PER_SHIFT, PS.PER_SHIFT_PLUS_PERCENT) and (per_shift or Decimal("0")) <= 0:
            raise serializers.ValidationError({"per_shift_amount": "Укажите ставку за смену больше 0."})
        if scheme == PS.PER_SHIFT_PLUS_PERCENT and pct <= 0:
            raise serializers.ValidationError({"sales_percent": "Укажите процент больше 0."})
        return data


# ===========================
# Инвентаризация товаров (Product.quantity)
# ===========================
class ProductInventoryLineCreateSerializer(serializers.Serializer):
    product_id = serializers.UUIDField()
    quantity_fact = serializers.DecimalField(max_digits=12, decimal_places=2)


class ProductInventorySessionCreateSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")
    items = ProductInventoryLineCreateSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Укажите хотя бы одну позицию.")
        seen = set()
        for row in value:
            pid = row["product_id"]
            if pid in seen:
                raise serializers.ValidationError(f"Товар {pid} указан более одного раза.")
            seen.add(pid)
        return value


class ProductInventoryLineReadSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    quantity_delta = serializers.SerializerMethodField()

    class Meta:
        model = ProductInventoryItem
        fields = (
            "id",
            "product",
            "product_name",
            "quantity_before",
            "quantity_fact",
            "quantity_delta",
        )
        read_only_fields = fields

    def get_quantity_delta(self, obj):
        if obj.quantity_before is None:
            return None
        d = Decimal(str(obj.quantity_fact or 0)) - Decimal(str(obj.quantity_before or 0))
        return str(d.quantize(Decimal("0.01")))


class ProductInventorySessionReadSerializer(serializers.ModelSerializer):
    lines = ProductInventoryLineReadSerializer(many=True, read_only=True)

    class Meta:
        model = ProductInventorySession
        fields = (
            "id",
            "company",
            "branch",
            "status",
            "note",
            "created_by",
            "applied_at",
            "created_at",
            "updated_at",
            "lines",
        )
        read_only_fields = fields


class PosPrinterSettingSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    device_key = serializers.CharField(max_length=128, required=True)
    settings = serializers.JSONField(required=False, default=dict)
    cashbox_id = serializers.UUIDField(source="cashbox.id", read_only=True, allow_null=True)

    class Meta:
        model = PosPrinterSetting
        fields = (
            "id",
            "company",
            "branch",
            "cashbox",
            "cashbox_id",
            "device_key",
            "settings",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "company", "created_at", "updated_at")


class MarketCustomerDisplaySettingsSerializer(serializers.ModelSerializer):
    enabled = serializers.BooleanField(
        required=False,
        error_messages={"invalid": "Must be a valid boolean."}
    )
    theme = serializers.ChoiceField(
        choices=["dark", "light"],
        required=False,
        error_messages={"invalid_choice": "Допустимы dark или light"}
    )
    welcome_text = serializers.CharField(
        max_length=200,
        allow_blank=True,
        allow_null=True,
        required=False,
        error_messages={"max_length": "Слишком длинный текст"}
    )
    slides = serializers.JSONField(required=False)

    class Meta:
        model = MarketCustomerDisplaySettings
        fields = ("enabled", "welcome_text", "theme", "slides")

    def validate_welcome_text(self, value):
        if value is None:
            return ""
        return value

    def validate_slides(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Каждый слайд должен содержать imageUrl")
        clean_slides = []
        for item in value:
            if not isinstance(item, dict):
                raise serializers.ValidationError("Каждый слайд должен содержать imageUrl")
            img = item.get("imageUrl")
            if not isinstance(img, str) or not img.strip():
                raise serializers.ValidationError("Каждый слайд должен содержать imageUrl")
            clean_item = dict(item)
            clean_item["imageUrl"] = img.strip()
            if "caption" in clean_item and clean_item["caption"] is not None:
                clean_item["caption"] = str(clean_item["caption"])
            clean_slides.append(clean_item)
        return clean_slides

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if ret.get("slides") is None:
            ret["slides"] = []
        if ret.get("welcome_text") is None:
            ret["welcome_text"] = ""
        return ret
