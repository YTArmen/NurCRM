from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError

from .models import (
    BarberProfile,
    Service,
    Client,
    Appointment,
    AppointmentService,
    Document,
    ClientDocument,
    Folder,
    ServiceCategory,
    Payout,
    PayoutSale,
    ProductSalePayout,
    OnlineBooking,
    ServiceSalaryRate,
    MasterSalaryPayout,
    MasterSalaryAccrual
)
from apps.users.models import Branch  # для проверки филиала по ?branch=
from apps.users.models import User
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from calendar import monthrange
from datetime import timedelta
import re


# ===========================
# Общий миксин: company/branch (branch авто из контекста/запроса)
# ===========================
class CompanyBranchReadOnlyMixin:
    """
    Делает company/branch read-only наружу и гарантированно проставляет их из контекста на create/update.
    Порядок получения branch:
      0) ?branch=<uuid> в запросе (если филиал принадлежит компании пользователя)
      1) user.primary_branch (свойство или метод, если есть)
      2) request.branch (если положил middleware)
      3) None (глобальная запись компании)
    """

    # ---- helpers ----
    def _user(self):
        request = self.context.get("request")
        return getattr(request, "user", None) if request else None

    def _user_company(self):
        user = self._user()
        if not user:
            return None
        return getattr(user, "company", None) or getattr(user, "owned_company", None)

    # ---- какой филиал реально использовать ----
    def _auto_branch(self):
        request = self.context.get("request")
        user = self._user()
        company = self._user_company()
        comp_id = getattr(company, "id", None)

        if not request or not user or not comp_id:
            return None

        # 0) ?branch=<uuid> в query-параметрах
        branch_id = None
        if hasattr(request, "query_params"):
            branch_id = request.query_params.get("branch")
        elif hasattr(request, "GET"):
            branch_id = request.GET.get("branch")

        if branch_id:
            try:
                br = Branch.objects.get(id=branch_id, company_id=comp_id)
                setattr(request, "branch", br)
                return br
            except (Branch.DoesNotExist, ValueError):
                # если id кривой/чужой — игнорируем
                pass

        # 1) primary_branch может быть полем или методом
        primary = getattr(user, "primary_branch", None)
        if callable(primary):
            try:
                val = primary()
                if val and getattr(val, "company_id", None) == comp_id:
                    return val
            except Exception:
                pass
        # 2) из instance (если редактируем существующий объект в филиале)
        if self.instance and getattr(self.instance, "branch", None):
            return self.instance.branch

        # 3) из middleware (на будущее)
        if hasattr(request, "branch"):
            b = getattr(request, "branch")
            if b and getattr(b, "company_id", None) == comp_id:
                return b

        # 4) глобально
        return None

    def create(self, validated_data):
        company = self._user_company()
        if company is not None:
            validated_data["company"] = company
        # branch строго из контекста (payload игнорируется)
        validated_data["branch"] = self._auto_branch()
        return super().create(validated_data)

    def update(self, instance, validated_data):
        company = self._user_company()
        if company is not None:
            validated_data["company"] = company
        validated_data["branch"] = self._auto_branch()
        return super().update(instance, validated_data)


# ===========================
# BarberProfile
# ===========================
class BarberProfileSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company_id")
    branch = serializers.ReadOnlyField(source="branch_id")  # показываем id филиала (или None)

    class Meta:
        model = BarberProfile
        fields = [
            "id", "company", "branch",
            "full_name", "phone", "extra_phone",
            "work_schedule", "is_active", "created_at",
        ]
        read_only_fields = ["id", "created_at", "company", "branch"]


class ServiceCategorySerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company_id")
    branch = serializers.ReadOnlyField(source="branch_id")

    class Meta:
        model = ServiceCategory
        fields = [
            "id",
            "company",
            "branch",
            "name",
            "is_active",
        ]
        read_only_fields = [
            "id",
            "company",
            "branch",
        ]

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Название не может быть пустым.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)

        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        if not company:
            return attrs

        target_branch = self._auto_branch()
        name = (attrs.get("name") or getattr(self.instance, "name", "")).strip()

        qs = ServiceCategory.objects.filter(company=company, name__iexact=name)
        if target_branch is None:
            qs = qs.filter(branch__isnull=True)   # глобальные категории
        else:
            qs = qs.filter(branch=target_branch)  # категории конкретного филиала

        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)

        if qs.exists():
            raise serializers.ValidationError(
                {"name": "Категория с таким названием уже существует (для этой компании/филиала)."}
            )

        return attrs
    
class ServiceBarberBriefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    full_name = serializers.SerializerMethodField()

    def get_full_name(self, obj):
        first = getattr(obj, "first_name", None) or ""
        last = getattr(obj, "last_name", None) or ""
        full = f"{first} {last}".strip()
        return full or getattr(obj, "email", "")


def normalize_service_name(value: str) -> str:
    """
    Нормализация названия услуги согласно спецификации (service-name-per-category.md §3.2):
    - обрезка пробелов по краям;
    - сжатие повторяющихся пробелов;
    - сравнение без учёта регистра (casefold).
    """
    return " ".join(str(value or "").split()).casefold()


class ServiceSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company_id")
    branch = serializers.ReadOnlyField(source="branch_id")

    # категория теперь динамическая (FK)
    category = serializers.PrimaryKeyRelatedField(
        queryset=ServiceCategory.objects.all(),
        allow_null=True,
        required=False,
    )
    category_name = serializers.CharField(
        source="category.name",
        read_only=True,
    )
    barbers = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        many=True,
        required=False,
    )
    barbers_detail = ServiceBarberBriefSerializer(source="barbers", many=True, read_only=True)

    class Meta:
        model = Service
        fields = [
            "id", "company", "branch",
            "name", "time",
            "category", "category_name",
            "price", "is_active",
            "barbers", "barbers_detail",
        ]
        read_only_fields = ["id", "company", "branch", "category_name", "barbers_detail"]

    def get_fields(self):
        fields = super().get_fields()
        company = self._user_company()
        if company:
            fields["barbers"].queryset = User.objects.filter(company=company, is_active=True)
        else:
            fields["barbers"].queryset = User.objects.none()
        return fields

    def validate_name(self, value):
        cleaned = " ".join(str(value or "").split())
        if not cleaned:
            raise serializers.ValidationError("Название не может быть пустым.")
        return cleaned

    def _validate_barbers(self, barbers, company_id, target_branch):
        if not barbers:
            return
        for user in barbers:
            if user.company_id != company_id:
                raise serializers.ValidationError(
                    {"barbers": "Один из сотрудников принадлежит другой компании."}
                )
            if target_branch is not None:
                user_branch_ids = set(user.branches.values_list("id", flat=True))
                if user_branch_ids and target_branch.id not in user_branch_ids:
                    raise serializers.ValidationError(
                        {"barbers": f"Сотрудник «{user}» не привязан к этому филиалу."}
                    )

    def validate(self, attrs):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        company = getattr(user, "company", None) or getattr(user, "owned_company", None)
        company_id = getattr(company, "id", None)
        if not company_id:
            return attrs

        target_branch = self._auto_branch()  # тот же источник, что в create()

        # Получаем очищенное и нормализованное имя
        raw_name = attrs.get("name") if "name" in attrs else getattr(self.instance, "name", "")
        clean_name = " ".join(str(raw_name or "").split())
        if not clean_name:
            raise serializers.ValidationError({"name": "Название не может быть пустым."})
        norm_name = normalize_service_name(clean_name)

        # Категория
        if "category" in attrs:
            category = attrs.get("category")
        elif self.instance:
            category = getattr(self.instance, "category", None)
        else:
            category = None
        category_id = getattr(category, "id", None) if category else None

        # ---- проверка категории, если указана ----
        if category:
            if category.company_id != company_id:
                raise serializers.ValidationError({"category": "Категория принадлежит другой компании."})
            if target_branch is not None and category.branch_id not in (None, target_branch.id):
                raise serializers.ValidationError({"category": "Категория принадлежит другому филиалу."})

        # ---- проверка уникальности имени внутри (company, branch, category) ----
        qs = Service.objects.filter(company_id=company_id)
        if target_branch is None:
            qs = qs.filter(branch__isnull=True)   # среди глобальных
        else:
            qs = qs.filter(branch=target_branch)  # внутри филиала

        if category_id is None:
            qs = qs.filter(category__isnull=True)  # «Общее» (без категории)
        else:
            qs = qs.filter(category_id=category_id)

        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)

        for row in qs.only("name"):
            if normalize_service_name(row.name) == norm_name:
                raise serializers.ValidationError({
                    "name": "Услуга с таким названием уже есть в этой категории."
                })

        barbers = attrs.get("barbers")
        if barbers is None and self.instance is not None:
            barbers = list(self.instance.barbers.all())
        self._validate_barbers(barbers or [], company_id, target_branch)

        return attrs

    def create(self, validated_data):
        barbers = validated_data.pop("barbers", None)
        instance = super().create(validated_data)
        if barbers is not None:
            instance.barbers.set(barbers)
        return instance

    def update(self, instance, validated_data):
        barbers = validated_data.pop("barbers", None)
        instance = super().update(instance, validated_data)
        if barbers is not None:
            instance.barbers.set(barbers)
        return instance


# ===========================
# Client
# ===========================
def normalize_barber_phone(phone):
    if not phone:
        return None
    phone = phone.strip()
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return phone
    if len(digits) == 10 and digits.startswith("0"):
        return "996" + digits[1:]
    elif len(digits) == 9 and not digits.startswith("996"):
        return "996" + digits
    return digits


class ClientSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company_id")
    branch = serializers.ReadOnlyField(source="branch_id")

    class Meta:
        model = Client
        fields = [
            "id", "company", "branch",
            "full_name", "phone", "email",
            "birth_date", "status", "notes", "created_at",
        ]
        read_only_fields = ["id", "created_at", "company", "branch"]
        ref_name = "BarberClient"

    def validate(self, attrs):
        raw_phone = attrs.get("phone")
        if raw_phone:
            norm_phone = normalize_barber_phone(raw_phone)
            attrs["phone"] = norm_phone or raw_phone
            phone = attrs["phone"]
        else:
            phone = None

        if not phone:
            return attrs

        company = self._user_company()
        branch = self.context.get("active_branch")
        if branch is None:
            branch = self._auto_branch()
        if not company:
            return attrs

        from django.db.models import Q
        last9 = phone[-9:] if len(phone) >= 9 else phone
        qs = Client.objects.filter(company=company).filter(
            Q(phone=phone) | Q(phone__endswith=last9)
        )
        if branch is not None:
            qs = qs.filter(branch=branch)
        else:
            qs = qs.filter(branch__isnull=True)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                {"phone": "Клиент с таким номером телефона уже существует в этой компании (или в выбранном филиале)."}
            )
        return attrs


class ClientDocumentSerializer(serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company_id")
    branch = serializers.ReadOnlyField(source="branch_id")
    client = serializers.ReadOnlyField(source="client.id")
    file = serializers.FileField(required=False, allow_null=True)

    class Meta:
        ref_name = "BarberClientDocument"
        model = ClientDocument
        fields = [
            "id",
            "company",
            "branch",
            "client",
            "file",
            "file_comment",
            "file_create_date",
        ]
        read_only_fields = ["id", "company", "branch", "client", "file_create_date"]


class ClientDetailSerializer(ClientSerializer):
    documents = ClientDocumentSerializer(many=True, read_only=True)

    class Meta(ClientSerializer.Meta):
        fields = ClientSerializer.Meta.fields + ["documents"]


# ===========================
# Appointment
# ===========================
class AppointmentPublicServiceSerializer(serializers.ModelSerializer):
    """Упрощённый вывод услуг для списка/деталей записей."""

    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = Service
        fields = ["id", "name", "time", "price", "category", "category_name"]


class AppointmentPublicMasterSerializer(serializers.Serializer):
    """Упрощённый вывод мастера для списка/деталей записей."""

    id = serializers.UUIDField()
    first_name = serializers.CharField(allow_null=True, required=False, allow_blank=True)
    last_name = serializers.CharField(allow_null=True, required=False, allow_blank=True)
    full_name = serializers.SerializerMethodField()
    avatar = serializers.URLField(allow_null=True, required=False)
    phone_number = serializers.CharField(allow_null=True, required=False, allow_blank=True)

    def get_full_name(self, obj):
        first = getattr(obj, "first_name", None) or ""
        last = getattr(obj, "last_name", None) or ""
        full = f"{first} {last}".strip()
        return full or getattr(obj, "email", "")


class AppointmentServicesListField(serializers.Field):
    """
    Поле services: массив ID услуг (или объектов с qty).
    Повторяющиеся ID — отдельные позиции (количество).
    Чтение: список ID в порядке позиций (с дубликатами).
    Запись: список ID (например: [uuid1, uuid1, uuid2]) или [{service_id: uuid1, qty: 2}, uuid2].
    """

    def to_representation(self, value):
        # value — manager (appointment_services) или prefetched list
        if value is None:
            return []
        mgr_inst = getattr(value, "instance", None)
        if mgr_inst and hasattr(mgr_inst, "_prefetched_objects_cache") and "appointment_services" in mgr_inst._prefetched_objects_cache:
            return [item.service_id for item in mgr_inst._prefetched_objects_cache["appointment_services"]]
        if hasattr(value, "__iter__") and not hasattr(value, "values_list"):
            return [getattr(item, "service_id", None) for item in value]
        if hasattr(value, "all"):
            return [getattr(item, "service_id", None) for item in value.all()]
        return []

    def to_internal_value(self, data):
        """Возвращаем список ID (с повторами по количеству)."""
        if not isinstance(data, list):
            raise serializers.ValidationError("Ожидается массив ID услуг.")
        
        flat_ids = []
        for item in data:
            if isinstance(item, dict):
                sid = item.get("service_id") or item.get("id") or item.get("pk")
                qty = item.get("qty") or item.get("quantity") or 1
                try:
                    qty = int(qty)
                except (ValueError, TypeError):
                    qty = 1
                if qty < 1:
                    qty = 1
                if sid:
                    for _ in range(qty):
                        flat_ids.append(str(sid))
            elif item is not None:
                flat_ids.append(str(item))

        qs = Service.objects.filter(pk__in=flat_ids)
        by_pk = {str(s.pk): s.pk for s in qs}
        ids = []
        for pk in flat_ids:
            if pk not in by_pk:
                raise serializers.ValidationError(f"Услуга с id {pk} не найдена или недоступна.")
            ids.append(by_pk[pk])
        return ids


class AppointmentSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company_id")
    branch = serializers.ReadOnlyField(source="branch_id")

    client_name = serializers.CharField(source="client.full_name", read_only=True)
    barber_name = serializers.SerializerMethodField()
    barber_public = AppointmentPublicMasterSerializer(source="barber", read_only=True)
    services = AppointmentServicesListField(
        source="appointment_services",
        required=False,
        help_text="Массив ID услуг. Один и тот же ID можно указать несколько раз — каждая позиция будет отдельной строкой в записи (например: [uuid1, uuid1, uuid2]).",
    )
    services_names = serializers.SerializerMethodField()
    services_public = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = [
            "id", "company", "branch",
            "client", "client_name",
            "barber", "barber_name", "barber_public",
            "services", "services_names", "services_public",
            "start_at", "end_at",
            "price", "discount",
            "status", "comment",
            "created_at",
        ]
        read_only_fields = ["id", "created_at", "company", "branch"]

    def get_barber_name(self, obj):
        if not obj.barber:
            return None
        if obj.barber.first_name or obj.barber.last_name:
            return f"{obj.barber.first_name or ''} {obj.barber.last_name or ''}".strip()
        return obj.barber.email

    def _get_appointment_services_ordered(self, obj):
        """Возвращает список AppointmentService по порядку position."""
        if hasattr(obj, "_prefetched_objects_cache") and "appointment_services" in obj._prefetched_objects_cache:
            return list(obj._prefetched_objects_cache["appointment_services"])
        rel = getattr(obj, "appointment_services", None)
        if rel is None:
            return []
        if hasattr(rel, "all"):
            return list(rel.all())
        return list(rel) if hasattr(rel, "__iter__") else []

    def get_services_names(self, obj):
        items = self._get_appointment_services_ordered(obj)
        return [getattr(item.service, "name", "") for item in items if getattr(item, "service", None)]

    def get_services_public(self, obj):
        items = self._get_appointment_services_ordered(obj)
        return [AppointmentPublicServiceSerializer(item.service).data for item in items if getattr(item, "service", None)]

    def create(self, validated_data):
        # DRF при source="appointment_services" кладёт значение в ключ "appointment_services"
        service_ids = validated_data.pop("appointment_services", None)
        if service_ids is None:
            service_ids = validated_data.pop("services", [])
        
        # Если price не передан с фронта, считаем sum(service.price * qty) с учетом discount
        if ("price" not in validated_data or validated_data.get("price") is None) and service_ids:
            by_pk = {str(s.pk): s for s in Service.objects.filter(pk__in=service_ids)}
            base_sum = sum((by_pk[str(sid)].price or Decimal("0.00")) for sid in service_ids if str(sid) in by_pk)
            discount = validated_data.get("discount") or Decimal("0.00")
            if discount > 0:
                validated_data["price"] = (base_sum * (Decimal("1") - Decimal(str(discount)) / Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                validated_data["price"] = base_sum.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        instance = super().create(validated_data)
        if service_ids:
            by_pk = {str(s.pk): s for s in Service.objects.filter(pk__in=service_ids)}
            for position, sid in enumerate(service_ids):
                service = by_pk.get(str(sid))
                if service:
                    AppointmentService.objects.create(appointment=instance, service=service, position=position)
        return instance

    def update(self, instance, validated_data):
        # DRF при source="appointment_services" кладёт значение в ключ "appointment_services"
        service_ids = validated_data.pop("appointment_services", None)
        if service_ids is None:
            service_ids = validated_data.pop("services", None)
        
        # Если price не передан в PATCH, но переданы новые services, пересчитываем price
        if "price" not in validated_data and service_ids is not None and len(service_ids) > 0:
            if instance.price is None or instance.price == Decimal("0.00"):
                by_pk = {str(s.pk): s for s in Service.objects.filter(pk__in=service_ids)}
                base_sum = sum((by_pk[str(sid)].price or Decimal("0.00")) for sid in service_ids if str(sid) in by_pk)
                discount = validated_data.get("discount") or instance.discount or Decimal("0.00")
                if discount > 0:
                    validated_data["price"] = (base_sum * (Decimal("1") - Decimal(str(discount)) / Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                else:
                    validated_data["price"] = base_sum.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        instance = super().update(instance, validated_data)
        if service_ids is not None:
            AppointmentService.objects.filter(appointment=instance).delete()
            if service_ids:
                by_pk = {str(s.pk): s for s in Service.objects.filter(pk__in=service_ids)}
                for position, sid in enumerate(service_ids):
                    service = by_pk.get(str(sid))
                    if service:
                        AppointmentService.objects.create(appointment=instance, service=service, position=position)
        return instance

    def _parse_minutes(self, s: str) -> int:
        """Парсим Service.time: '30', '00:30', '1:15', '30m', '1h', '1h30m' -> минуты."""
        if not s:
            return 0
        s = s.strip()
        try:
            return int(s)  # "30"
        except ValueError:
            pass
        if ":" in s:  # "HH:MM" или "MM:SS" — используем как часы:минуты
            h, m = s.split(":", 1)
            return int(h) * 60 + int(m)
        m = re.match(r'(?i)^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?$', s)
        if m:
            return (int(m.group(1) or 0) * 60) + int(m.group(2) or 0)
        return 0

    def validate(self, attrs):
        """
        ОДИН общий validate:
          1) проверка принадлежности client/barber/services компании и филиалу
          2) автоподстановка end_at из суммарной длительности услуг
          3) автоподстановка price из суммарной цены услуг (если не передали)
          4) проверка discount (0–100) и end_at > start_at
        """
        attrs = super().validate(attrs)

        request = self.context.get("request")
        user_company = getattr(getattr(request, "user", None), "company", None) if request else None
        company_id = getattr(user_company, "id", None)
        target_branch = self._auto_branch()

        # текущее значение client/barber/services (DRF при source кладёт в attrs ключ "appointment_services")
        client = attrs.get("client") or getattr(self.instance, "client", None)
        barber = attrs.get("barber") or getattr(self.instance, "barber", None)
        service_ids = attrs.get("appointment_services", attrs.get("services"))
        if service_ids is not None:
            services = list(Service.objects.filter(pk__in=service_ids)) if service_ids else []
            by_pk = {str(s.pk): s for s in services}
            services = [by_pk[str(sid)] for sid in service_ids if str(sid) in by_pk]
        elif self.instance:
            order_qs = self.instance.appointment_services.order_by("position").select_related("service")
            services = [item.service for item in order_qs]
        else:
            services = []

        # --- company проверки ---
        for obj, name in [(client, "client"), (barber, "barber")]:
            if obj and getattr(obj, "company_id", None) != company_id:
                raise serializers.ValidationError({name: "Объект не принадлежит вашей компании."})
        for service in services:
            if getattr(service, "company_id", None) != company_id:
                raise serializers.ValidationError({"services": "Одна из услуг принадлежит другой компании."})

        # --- branch проверки ---
        if target_branch is not None:
            tb_id = target_branch.id
            if client and getattr(client, "branch_id", None) not in (None, tb_id):
                raise serializers.ValidationError({"client": "Клиент принадлежит другому филиалу."})
            for service in services:
                if getattr(service, "branch_id", None) not in (None, tb_id):
                    raise serializers.ValidationError({
                        "services": f"Услуга '{service.name}' принадлежит другому филиалу."
                    })

        # --- время ---
        start_at = attrs.get("start_at") or getattr(self.instance, "start_at", None)
        end_at = attrs.get("end_at") or getattr(self.instance, "end_at", None)

        # Если конец не передан — вычисляем из услуг
        if start_at and not end_at and services:
            total_minutes = 0
            for s in services:
                total_minutes += self._parse_minutes(getattr(s, "time", None))
            if total_minutes > 0:
                attrs["end_at"] = start_at + timedelta(minutes=total_minutes)
                end_at = attrs["end_at"]

        if start_at and end_at and not (end_at > start_at):
            raise serializers.ValidationError({"end_at": "Должно быть строго позже start_at."})

        # --- цена и скидка ---
        price = attrs.get("price", None)
        discount = attrs.get("discount", None)

        # Если цена не передана в запросе
        if price is None:
            if not self.instance:
                if services:
                    base_price = sum((s.price or Decimal("0.00")) for s in services)
                    disc = Decimal(str(discount or "0.00")) if discount is not None else Decimal("0.00")
                    if disc > 0:
                        attrs["price"] = (base_price * (Decimal("1") - disc / Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    else:
                        attrs["price"] = base_price
            else:
                if "appointment_services" in attrs or "services" in attrs or "discount" in attrs:
                    disc = Decimal(str(discount if discount is not None else getattr(self.instance, "discount", Decimal("0.00")) or "0.00"))
                    base_price = sum((s.price or Decimal("0.00")) for s in services)
                    if disc > 0:
                        attrs["price"] = (base_price * (Decimal("1") - disc / Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    else:
                        attrs["price"] = base_price

        # скидка: 0–100
        if discount is not None:
            if discount < 0 or discount > 100:
                raise serializers.ValidationError({"discount": "Скидка должна быть от 0 до 100."})

        return attrs


# ===========================
# Appointment history row (for table)
# ===========================
class AppointmentHistoryRowSerializer(serializers.ModelSerializer):
    """
    Упрощённый сериализатор строки истории визитов.
    Поля рассчитаны под таблицу:
      Дата | Сотрудник | Клиент | Итого | Статус
    """

    date = serializers.DateTimeField(source="start_at", read_only=True)
    employee = serializers.SerializerMethodField()
    client = serializers.CharField(source="client.full_name", read_only=True)
    total = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = [
            "id",
            "date",
            "employee",
            "client",
            "total",
            "status",
        ]

    def get_employee(self, obj):
        b = getattr(obj, "barber", None)
        if not b:
            return None
        first = getattr(b, "first_name", None) or ""
        last = getattr(b, "last_name", None) or ""
        full = f"{first} {last}".strip()
        return full or getattr(b, "email", None)

    def get_total(self, obj):
        """
        Итого = price * (1 - discount/100)
        Возвращаем Decimal с 2 знаками (как деньги).
        """
        price = getattr(obj, "price", None) or Decimal("0")
        discount = getattr(obj, "discount", None) or Decimal("0")
        try:
            total = price * (Decimal("1") - (Decimal(discount) / Decimal("100")))
        except Exception:
            total = price
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ===========================
# Folder
# ===========================
class FolderSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company_id")
    branch = serializers.ReadOnlyField(source="branch_id")

    parent = serializers.PrimaryKeyRelatedField(
        queryset=Folder.objects.all(), allow_null=True, required=False
    )
    parent_name = serializers.CharField(source="parent.name", read_only=True)

    class Meta:
        model = Folder
        fields = ["id", "company", "branch", "name", "parent", "parent_name"]
        read_only_fields = ["id", "company", "branch", "parent_name"]
        ref_name = "BarberFolder"

    def validate_parent(self, parent):
        """
        Родительская папка (если указана) должна быть вашей компании
        и глобальной/филиала пользователя.
        """
        if parent is None:
            return parent
        request = self.context.get("request")
        user_company_id = getattr(getattr(request, "user", None), "company_id", None) if request else None
        target_branch = self._auto_branch()

        if user_company_id and parent.company_id != user_company_id:
            raise serializers.ValidationError("Родительская папка принадлежит другой компании.")

        if target_branch is not None and parent.branch_id not in (None, target_branch.id):
            raise serializers.ValidationError("Родительская папка принадлежит другому филиалу.")
        return parent


# ===========================
# Document
# ===========================
class DocumentSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company_id")
    branch = serializers.ReadOnlyField(source="branch_id")
    folder_name = serializers.CharField(source="folder.name", read_only=True)

    class Meta:
        ref_name = "BarberDocument"
        model = Document
        fields = [
            "id", "company", "branch",
            "name", "file",
            "folder", "folder_name",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company", "branch", "created_at", "updated_at", "folder_name"]

    def validate_folder(self, folder):
        """
        Папка должна быть компании пользователя и глобальной/филиала пользователя.
        """
        if folder is None:
            return folder
        request = self.context.get("request")
        user_company_id = getattr(getattr(request, "user", None), "company_id", None) if request else None
        target_branch = self._auto_branch()

        folder_company_id = getattr(getattr(folder, "company", None), "id", None)
        if folder_company_id and user_company_id and folder_company_id != user_company_id:
            raise serializers.ValidationError("Папка принадлежит другой компании.")

        if target_branch is not None and folder.branch_id not in (None, target_branch.id):
            raise serializers.ValidationError("Папка принадлежит другому филиалу.")
        return folder



class PayoutSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    """
    Создаёт и обновляет выплату, автоматически рассчитывая:
      - appointments_count  — кол-во завершённых записей за период
      - total_revenue       — выручка за период (completed)
      - payout_amount       — сумма выплаты = round(выручка * percent / 100) + (завершённые * per_record) + fixed
    """

    company = serializers.ReadOnlyField(source="company_id")
    branch = serializers.ReadOnlyField(source="branch_id")
    barber_name = serializers.SerializerMethodField()

    class Meta:
        model = Payout
        fields = [
            "id",
            "company",
            "branch",
            "barber",
            "barber_name",
            "period",
            "mode",
            "rate",
            "percent",
            "per_record",
            "fixed",
            "appointments_count",
            "total_revenue",
            "payout_amount",
            "comment",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "company",
            "branch",
            "appointments_count",
            "total_revenue",
            "payout_amount",
            "created_at",
            "updated_at",
        ]

    # ----- helpers -----

    def get_barber_name(self, obj):
        b = obj.barber
        if not b:
            return None
        if b.first_name or b.last_name:
            return f"{b.first_name or ''} {b.last_name or ''}".strip()
        return b.email

    def validate_period(self, value: str) -> str:
        """
        Период в формате YYYY-MM (например, 2025-11).
        """
        import re

        if not re.match(r"^\d{4}-\d{2}$", value):
            raise serializers.ValidationError("Период должен быть в формате YYYY-MM, например: 2025-11.")
        year, month = map(int, value.split("-"))
        if not (1 <= month <= 12):
            raise serializers.ValidationError("Месяц должен быть от 01 до 12.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)

        company = self._user_company()
        barber = attrs.get("barber") or getattr(self.instance, "barber", None)
        mode = attrs.get("mode") or getattr(self.instance, "mode", None)
        rate = attrs.get("rate") if "rate" in attrs else getattr(self.instance, "rate", None)
        percent = attrs.get("percent") if "percent" in attrs else getattr(self.instance, "percent", None)
        per_record = attrs.get("per_record") if "per_record" in attrs else getattr(self.instance, "per_record", None)
        fixed = attrs.get("fixed") if "fixed" in attrs else getattr(self.instance, "fixed", None)

        # барбер должен быть из той же компании
        if company and barber and getattr(barber, "company_id", None) != getattr(company, "id", None):
            raise serializers.ValidationError({"barber": "Сотрудник принадлежит другой компании."})

        if percent is not None and (percent < 0 or percent > 100):
            raise serializers.ValidationError({"percent": "Процент от выручки должен быть от 0 до 100."})
        if per_record is not None and per_record < 0:
            raise serializers.ValidationError({"per_record": "Фикс за запись не может быть отрицательным."})
        if fixed is not None and fixed < 0:
            raise serializers.ValidationError({"fixed": "Оклад не может быть отрицательным."})

        # проверка ставки для процента (legacy)
        if mode == Payout.Mode.PERCENT and rate is not None:
            if rate < 0 or rate > 100:
                raise serializers.ValidationError({"rate": "Для режима 'percent' ставка должна быть от 0 до 100."})

        return attrs

    def _period_bounds(self, period: str):
        """
        YYYY-MM -> (date_start, date_end_exclusive)
        """
        year, month = map(int, period.split("-"))
        start = date(year, month, 1)
        if month == 12:
            end = date(year + 1, 1, 1)
        else:
            end = date(year, month + 1, 1)
        return start, end

    def _calculate_payout_fields(self, company, branch, barber, period, data):
        start_date, end_date = self._period_bounds(period)

        qs = Appointment.objects.filter(
            company=company,
            barber=barber,
            start_at__date__gte=start_date,
            start_at__date__lt=end_date,
            status=Appointment.Status.COMPLETED,  # считаем только завершённые
        )
        if branch is not None:
            qs = qs.filter(branch=branch)

        appointments_count = qs.count()

        from django.db.models import Sum

        total_revenue = qs.aggregate(total=Sum("price"))["total"] or Decimal("0.00")

        # Извлекаем значения ставок
        mode = data.get("mode") or Payout.Mode.PERCENT
        rate = Decimal(str(data.get("rate") or 0))

        percent_val = data.get("percent")
        per_record_val = data.get("per_record")
        fixed_val = data.get("fixed")

        has_new_fields = percent_val is not None or per_record_val is not None or fixed_val is not None

        if has_new_fields:
            percent_d = Decimal(str(percent_val or 0))
            per_record_d = Decimal(str(per_record_val or 0))
            fixed_d = Decimal(str(fixed_val or 0))

            percent_amount = (total_revenue * percent_d / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            record_amount = (Decimal(appointments_count) * per_record_d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            fixed_amount = fixed_d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            payout_amount = percent_amount + record_amount + fixed_amount

            if percent_d > 0 and per_record_d == 0 and fixed_d == 0:
                mode = Payout.Mode.PERCENT
                rate = percent_d
            elif per_record_d > 0 and percent_d == 0 and fixed_d == 0:
                mode = Payout.Mode.RECORD
                rate = per_record_d
            elif fixed_d > 0 and percent_d == 0 and per_record_d == 0:
                mode = Payout.Mode.FIXED
                rate = fixed_d
            else:
                mode = Payout.Mode.PERCENT
                rate = percent_d
        else:
            if mode == Payout.Mode.RECORD:
                per_record_d = rate
                percent_d = Decimal("0.00")
                fixed_d = Decimal("0.00")
                payout_amount = (rate * Decimal(appointments_count)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            elif mode == Payout.Mode.FIXED:
                fixed_d = rate
                percent_d = Decimal("0.00")
                per_record_d = Decimal("0.00")
                payout_amount = rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            elif mode == Payout.Mode.PERCENT:
                percent_d = rate
                per_record_d = Decimal("0.00")
                fixed_d = Decimal("0.00")
                payout_amount = ((total_revenue * rate) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                percent_d = Decimal("0.00")
                per_record_d = Decimal("0.00")
                fixed_d = Decimal("0.00")
                payout_amount = Decimal("0.00")

        return {
            "mode": mode,
            "rate": rate,
            "percent": percent_d,
            "per_record": per_record_d,
            "fixed": fixed_d,
            "appointments_count": appointments_count,
            "total_revenue": total_revenue,
            "payout_amount": payout_amount,
        }

    # ----- create с расчётом выплаты -----

    def create(self, validated_data):
        company = self._user_company()
        if not company:
            raise serializers.ValidationError("У пользователя не задана компания.")

        branch = self._auto_branch()
        barber = validated_data["barber"]
        period = validated_data["period"]

        calculated = self._calculate_payout_fields(company, branch, barber, period, validated_data)

        validated_data["company"] = company
        validated_data["branch"] = branch
        validated_data.update(calculated)

        return super(CompanyBranchReadOnlyMixin, self).create(validated_data)

    def update(self, instance, validated_data):
        company = instance.company
        branch = instance.branch
        barber = validated_data.get("barber", instance.barber)
        period = validated_data.get("period", instance.period)

        merged_data = {
            "mode": validated_data.get("mode", instance.mode),
            "rate": validated_data.get("rate", instance.rate),
            "percent": validated_data.get("percent", instance.percent),
            "per_record": validated_data.get("per_record", instance.per_record),
            "fixed": validated_data.get("fixed", instance.fixed),
        }

        calculated = self._calculate_payout_fields(company, branch, barber, period, merged_data)
        validated_data.update(calculated)

        return super(CompanyBranchReadOnlyMixin, self).update(instance, validated_data)


    
class ProductSalePayoutSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    """
    Для формы «Процент от продажи товара».
    Поля модалки:
      - product      → Товар
      - employee     → Сотрудник
      - percent      → Процент (%)
      - price        → Цена (сом)
    payout_amount считается в save() модели и только возвращается наружу.
    """

    company = serializers.ReadOnlyField(source="company_id")
    branch = serializers.ReadOnlyField(source="branch_id")

    product_name = serializers.CharField(
        source="product.name",
        read_only=True,
    )
    employee_name = serializers.SerializerMethodField()

    class Meta:
        model = ProductSalePayout
        fields = [
            "id",
            "company",
            "branch",
            "product",
            "product_name",
            "employee",
            "employee_name",
            "percent",
            "price",
            "payout_amount",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "company",
            "branch",
            "product_name",
            "employee_name",
            "payout_amount",
            "created_at",
        ]

    # ----- helpers -----

    def get_employee_name(self, obj):
        e = obj.employee
        if not e:
            return None
        if e.first_name or e.last_name:
            return f"{e.first_name or ''} {e.last_name or ''}".strip()
        return getattr(e, "email", None) or getattr(e, "username", None)

    # ----- валидация -----

    def validate_percent(self, value):
        if value is None:
            return value
        if value < 0 or value > 100:
            raise serializers.ValidationError("Процент должен быть от 0 до 100.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)

        company = self._user_company()
        company_id = getattr(company, "id", None)

        product = attrs.get("product") or getattr(self.instance, "product", None)
        employee = attrs.get("employee") or getattr(self.instance, "employee", None)

        target_branch = self._auto_branch()

        # товар → та же компания
        if company_id and product and getattr(product, "company_id", None) != company_id:
            raise serializers.ValidationError({"product": "Товар принадлежит другой компании."})

        # сотрудник → та же компания
        if company_id and employee and getattr(employee, "company_id", None) != company_id:
            raise serializers.ValidationError({"employee": "Сотрудник принадлежит другой компании."})

        # если у товара есть branch, проверяем, что он глобальный/этого филиала
        if target_branch is not None and product is not None:
            pb = getattr(product, "branch_id", None)
            if pb not in (None, target_branch.id):
                raise serializers.ValidationError({"product": "Товар принадлежит другому филиалу."})

        return attrs

class PayoutSaleSerializer(serializers.ModelSerializer):
    # фронт шлёт "2025-11", храним datetime, наружу тоже "YYYY-MM"
    period = serializers.DateTimeField(
        input_formats=["%Y-%m", "iso-8601"],
        format="%Y-%m",
    )

    class Meta:
        model = PayoutSale
        fields = [
            "id",
            "period",
            "old_total_fund",
            "new_total_fund",
            "total",
        ]
        # эти поля считаются на бэке, с фронта игнорим
        read_only_fields = ["id", "old_total_fund", "total"]

    # ===== helpers =====

    def _calc_total(self, old_fund, new_fund) -> Decimal:
        """
        total = new_total_fund - old_total_fund
        """
        return (Decimal(new_fund) - Decimal(old_fund)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    def _get_company(self):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        if not user:
            return None
        return getattr(user, "company", None) or getattr(user, "owned_company", None)

    def _get_branch(self, request, company):
        """
        Берём филиал только из ?branch=<uuid>, если он принадлежит компании.
        Если нет/кривой — считаем запись глобальной (branch=None).
        """
        if not request or not company:
            return None

        from apps.users.models import Branch

        branch_id = None
        if hasattr(request, "query_params"):
            branch_id = request.query_params.get("branch")
        elif hasattr(request, "GET"):
            branch_id = request.GET.get("branch")

        if not branch_id:
            return None

        try:
            return Branch.objects.get(id=branch_id, company=company)
        except (Branch.DoesNotExist, ValueError):
            return None

    # ===== create: реализуем всю логику Infinity =====

    def create(self, validated_data):
        """
        Вход:  { "period": "YYYY-MM", "new_total_fund": "1200.00" }

        1) company/branch берём из пользователя и ?branch=
        2) Ищем запись за этот же period.
        3) old_total_fund:
              если запись была → old = previous.new_total_fund
              если не было     → old = 0
        4) total = new_total_fund - old_total_fund
        5) Upsert:
              если запись за period уже есть → UPDATE
              если нет → CREATE
        """
        request = self.context.get("request")

        company = self._get_company()
        if not company:
            raise serializers.ValidationError("У пользователя не задана компания.")

        branch = self._get_branch(request, company)
        period = validated_data["period"]
        new_total_fund = Decimal(str(validated_data["new_total_fund"]))

        # 2) ищем существующую запись за этот же period
        try:
            instance = PayoutSale.objects.get(
                company=company,
                branch=branch,
                period=period,
            )
            # 3а) была запись → old = прошлый new_total_fund
            old_total_fund = instance.new_total_fund
        except PayoutSale.DoesNotExist:
            instance = None
            # 3б) не было → old = 0
            old_total_fund = Decimal("0.00")

        # 4) считаем total
        total = self._calc_total(old_total_fund, new_total_fund)

        if instance is None:
            # 5) CREATE
            instance = PayoutSale.objects.create(
                company=company,
                branch=branch,
                period=period,
                old_total_fund=old_total_fund,
                new_total_fund=new_total_fund,
                total=total,
            )
        else:
            # 5) UPDATE
            instance.old_total_fund = old_total_fund
            instance.new_total_fund = new_total_fund
            instance.total = total
            instance.save(update_fields=["old_total_fund", "new_total_fund", "total"])

        return instance

    # опционально: если кто-то вдруг будет делать PATCH/PUT —
    # можно оставить поведение по умолчанию или тоже пересчитывать тут.


# ===========================
# OnlineBooking
# ===========================
class OnlineBookingCreateSerializer(serializers.ModelSerializer):
    """Публичный сериализатор для создания заявки (без авторизации)"""
    
    class Meta:
        model = OnlineBooking
        fields = [
            'services',
            'master_id',
            'master_name',
            'date',
            'time_start',
            'time_end',
            'client_name',
            'client_phone',
            'client_comment',
            'payment_method',
            'status'
        ]
        read_only_fields = ['status']
    
    def validate_services(self, value):
        """Валидация массива услуг"""
        if not value or not isinstance(value, list):
            raise serializers.ValidationError("services должен быть массивом")
        
        for service in value:
            if not isinstance(service, dict):
                raise serializers.ValidationError("Каждая услуга должна быть объектом")
            required_fields = ['service_id', 'title', 'price', 'duration_min']
            for field in required_fields:
                if field not in service:
                    raise serializers.ValidationError(f"Услуга должна содержать поле '{field}'")
        
        return value
    
    def validate(self, attrs):
        """Валидация времени и даты"""
        time_start = attrs.get('time_start')
        time_end = attrs.get('time_end')
        
        if time_start and time_end and time_end <= time_start:
            raise serializers.ValidationError({
                'time_end': 'Время окончания должно быть позже времени начала'
            })
        
        return attrs


class BookingAssignmentServiceSerializer(serializers.Serializer):
    """Услуга внутри назначения мастеру (multi-master бронь)."""
    service_id = serializers.UUIDField()
    title = serializers.CharField(required=False, allow_blank=True, default="")
    price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=Decimal("0"))
    duration_min = serializers.IntegerField(required=False, default=0, min_value=0)


class BookingAssignmentSerializer(serializers.Serializer):
    """Назначение: один мастер + его услуги + слот времени."""
    master_id = serializers.UUIDField()
    services = BookingAssignmentServiceSerializer(many=True)
    time_start = serializers.TimeField()
    time_end = serializers.TimeField()

    def validate(self, attrs):
        if not attrs.get("services"):
            raise serializers.ValidationError({"services": "Назначение должно содержать хотя бы одну услугу."})
        if attrs["time_end"] <= attrs["time_start"]:
            raise serializers.ValidationError({"time_end": "Время окончания должно быть позже времени начала."})
        return attrs


class OnlineBookingMultiCreateSerializer(serializers.Serializer):
    """
    Публичный сериализатор multi-master брони: распределение услуг между несколькими мастерами.
    Записи создаются атомарно во вьюхе.
    """
    client_name = serializers.CharField(max_length=255)
    client_phone = serializers.CharField(max_length=32)
    client_comment = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    date = serializers.DateField()
    payment_method = serializers.ChoiceField(
        choices=OnlineBooking.PaymentMethod.choices,
        required=False,
        default=OnlineBooking.PaymentMethod.CASH,
    )
    assignments = BookingAssignmentSerializer(many=True)

    def validate_assignments(self, value):
        if not value:
            raise serializers.ValidationError("Укажите хотя бы одно назначение мастеру.")
        return value


class OnlineBookingSerializer(serializers.ModelSerializer):
    """Сериализатор для списка и деталей заявок (с авторизацией)"""
    
    company_name = serializers.CharField(source='company.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    services_titles = serializers.SerializerMethodField()
    
    class Meta:
        model = OnlineBooking
        fields = [
            'id',
            'status',
            'created_at',
            'client_name',
            'client_phone',
            'services',
            'services_titles',
            'total_price',
            'total_duration_min',
            'date',
            'time_start',
            'time_end',
            'master_id',
            'master_name',
            'payment_method',
            'client_comment',
            'company',
            'company_name',
            'branch',
            'branch_name',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'total_price', 'total_duration_min']
    
    def get_services_titles(self, obj):
        """Извлекаем только названия услуг для отображения"""
        if obj.services:
            return [s.get('title', '') for s in obj.services]
        return []


class OnlineBookingStatusUpdateSerializer(serializers.ModelSerializer):
    """Сериализатор только для изменения статуса"""
    
    class Meta:
        model = OnlineBooking
        fields = ['status']
    
    def validate_status(self, value):
        """Проверяем, что статус из разрешенных значений"""
        allowed_statuses = [
            OnlineBooking.Status.NEW,
            OnlineBooking.Status.CONFIRMED,
            OnlineBooking.Status.NO_SHOW,
            OnlineBooking.Status.SPAM
        ]
        if value not in allowed_statuses:
            raise serializers.ValidationError(
                f"Статус должен быть одним из: {', '.join(allowed_statuses)}"
            )
        return value


# ===========================
# Публичные сериализаторы для онлайн-записи
# ===========================
class PublicServiceSerializer(serializers.ModelSerializer):
    """Публичный сериализатор для услуг (без авторизации)"""
    category_name = serializers.CharField(source="category.name", read_only=True)
    barbers = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    
    class Meta:
        model = Service
        fields = [
            'id',
            'name',
            'time',
            'price',
            'category',
            'category_name',
            'barbers',
        ]


class PublicServiceCategorySerializer(serializers.ModelSerializer):
    """Публичный сериализатор для категорий услуг (без авторизации)"""
    services = PublicServiceSerializer(many=True, read_only=True)
    
    class Meta:
        model = ServiceCategory
        fields = [
            'id',
            'name',
            'services',
        ]


class PublicMasterSerializer(serializers.Serializer):
    """Публичный сериализатор для мастеров (без авторизации)"""
    id = serializers.UUIDField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    full_name = serializers.SerializerMethodField()
    avatar = serializers.URLField(allow_null=True)
    phone_number = serializers.CharField(allow_null=True, required=False)
    
    def get_full_name(self, obj):
        first = obj.first_name or ''
        last = obj.last_name or ''
        return f"{first} {last}".strip() or obj.email


class PublicMasterScheduleSerializer(serializers.Serializer):
    """Сериализатор для занятых слотов мастера"""
    id = serializers.UUIDField()
    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField()
    # Не показываем детали клиента и услуг в публичном API


class PublicMasterAvailabilitySerializer(serializers.Serializer):
    """Сериализатор для доступности мастера на конкретную дату"""
    master_id = serializers.UUIDField()
    master_name = serializers.CharField()
    date = serializers.DateField()
    busy_slots = PublicMasterScheduleSerializer(many=True)
    # Рабочие часы (можно расширить в будущем)
    work_start = serializers.TimeField(default="09:00")
    work_end = serializers.TimeField(default="21:00")


# ===========================
# Analytics (company + my)
# ===========================
class BarberAnalyticsTotalsSerializer(serializers.Serializer):
    appointments_total = serializers.IntegerField()
    appointments_completed = serializers.IntegerField()
    appointments_canceled = serializers.IntegerField()
    appointments_no_show = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=14, decimal_places=2)
    avg_ticket = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)


class BarberAnalyticsServiceRowSerializer(serializers.Serializer):
    service_id = serializers.UUIDField()
    name = serializers.CharField()
    count = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=14, decimal_places=2)


class BarberAnalyticsMasterRowSerializer(serializers.Serializer):
    master_id = serializers.UUIDField()
    master_name = serializers.CharField()
    count = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=14, decimal_places=2)


class BarberAnalyticsResponseSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    totals = BarberAnalyticsTotalsSerializer()
    services = BarberAnalyticsServiceRowSerializer(many=True)
    masters = BarberAnalyticsMasterRowSerializer(many=True, required=False)


# ===========================
# Salary Serializers (Master's salary)
# ===========================

class ServiceSalaryRateSerializer(serializers.ModelSerializer):
    service = serializers.UUIDField(source="id", read_only=True)
    service_name = serializers.CharField(source="name", read_only=True)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    percent = serializers.SerializerMethodField()
    updated_at = serializers.SerializerMethodField()

    class Meta:
        model = Service
        fields = ["service", "service_name", "price", "percent", "updated_at"]

    def get_percent(self, obj):
        rate = getattr(obj, "salary_rate", None)
        return rate.percent if rate else Decimal("0.00")

    def get_updated_at(self, obj):
        rate = getattr(obj, "salary_rate", None)
        return rate.updated_at if rate else None


class MasterSalaryPayoutSerializer(serializers.ModelSerializer):
    master_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = MasterSalaryPayout
        fields = [
            "id", "created_at", "master", "master_name",
            "amount", "comment", "created_by", "created_by_name"
        ]
        read_only_fields = ["id", "created_at", "created_by"]

    def get_master_name(self, obj):
        if not obj.master:
            return ""
        if obj.master.first_name or obj.master.last_name:
            return f"{obj.master.first_name or ''} {obj.master.last_name or ''}".strip()
        return obj.master.email

    def get_created_by_name(self, obj):
        if not obj.created_by:
            return ""
        if obj.created_by.first_name or obj.created_by.last_name:
            return f"{obj.created_by.first_name or ''} {obj.created_by.last_name or ''}".strip()
        return obj.created_by.email


class MasterSalaryAccrualSerializer(serializers.ModelSerializer):
    master_name = serializers.SerializerMethodField()
    appointment_number = serializers.SerializerMethodField()
    service_name = serializers.CharField(source="service.name", read_only=True)

    class Meta:
        model = MasterSalaryAccrual
        fields = [
            "id", "created_at", "master", "master_name",
            "appointment", "appointment_number", "service", "service_name",
            "service_amount", "percent", "amount", "status"
        ]

    def get_master_name(self, obj):
        if not obj.master:
            return ""
        if obj.master.first_name or obj.master.last_name:
            return f"{obj.master.first_name or ''} {obj.master.last_name or ''}".strip()
        return obj.master.email

    def get_appointment_number(self, obj):
        if not obj.appointment_id:
            return ""
        return f"A-{obj.appointment_id.hex[:6].upper()}"
