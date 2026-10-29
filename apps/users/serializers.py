from datetime import timedelta

import secrets
import string

from apps.users.permissions import READ_ONLY_FIELDS
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

import re

from django.db.models.functions import Lower

from rest_framework import serializers
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

# --- slug (формат: ^[a-z0-9]+(?:-[a-z0-9]+)*$, длина 3..50) ---
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_MIN, SLUG_MAX = 3, 50


def normalize_slug(value) -> str:
    return (value or "").strip().lower()


def slug_format_error(value):
    """Возвращает текст ошибки если формат невалиден, иначе None."""
    if len(value) < SLUG_MIN or len(value) > SLUG_MAX:
        return f"Slug должен быть от {SLUG_MIN} до {SLUG_MAX} символов."
    if not SLUG_RE.match(value):
        return "Разрешены строчные латинские буквы, цифры и дефис (не подряд, не по краям)."
    return None


def slug_taken_by_other(value, exclude_pk=None) -> bool:
    qs = Company.objects.annotate(_s=Lower("slug")).filter(_s=value)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


class SlugConflict(APIException):
    status_code = 409
    default_detail = {"slug": ["Slug already exists"]}
    default_code = "conflict"

from apps.users.models import (
    User, Company, Roles, Industry, SubscriptionPlan,
    Feature, Sector, CustomRole, Branch, BranchMembership, KyrgyzstanRegion,
    SCALE_BARCODE_LAYOUT_PLU, UserUiPreferences,
)


# ======================
# Helpers
# ======================

def _ensure_owner_or_admin(user):
    if not user or (getattr(user, "role", None) not in ("owner", "admin") and not getattr(user, "is_superuser", False)):
        raise serializers.ValidationError("Требуются права владельца или администратора.")


def _get_user_company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)

def _is_market_company(company: Company) -> bool:
    try:
        return bool(company and getattr(company, "is_market", None) and company.is_market())
    except Exception:
        return False

def _company_can_use_cashier(company: Company) -> bool:
    try:
        return bool(company and getattr(company, "can_use_cashier", None) and company.can_use_cashier())
    except Exception:
        return False

def _apply_cashier_sector_gate(rep: dict, user: User):
    """
    Гейт "интерфейс кассира доступен только сферам Маркет и Услуги":
    - если сфера компании не разрешена -> can_view_cashier всегда False (даже если флаг True в БД)
    """
    company = _get_user_company(user)
    if not _company_can_use_cashier(company):
        rep["can_view_cashier"] = False
    return rep


def _validate_branch_ids_for_company(branch_ids, company):
    if not branch_ids:
        return []
    # уникальные id без лишних запросов
    unique_ids = list(dict.fromkeys(branch_ids))
    branches = list(Branch.objects.filter(id__in=unique_ids, company=company))
    if len(branches) != len(unique_ids):
        raise serializers.ValidationError({"branch_ids": "Некоторые филиалы не найдены в вашей компании."})
    # сохраняем порядок как пришло
    by_id = {str(b.id): b for b in branches}
    ordered = [by_id[str(bid)] for bid in unique_ids]
    return ordered


@transaction.atomic
def _sync_user_branches(user: User, branches: list[Branch]):
    """
    Пересобирает членства:
    - удаляет лишние
    - добавляет недостающие
    - первый филиал делает primary
    Пустой список -> очищаем membership (пользователь остаётся «глобальный по компании»)
    """
    current_ids = set(user.branch_memberships.values_list("branch_id", flat=True))
    new_ids = set(b.id for b in branches)

    to_delete = current_ids - new_ids
    if to_delete:
        BranchMembership.objects.filter(user=user, branch_id__in=to_delete).delete()

    to_add = new_ids - current_ids
    if to_add:
        BranchMembership.objects.bulk_create(
            [BranchMembership(user=user, branch=b, is_primary=False) for b in branches if b.id in to_add],
            ignore_conflicts=True,
        )

    # primary: гарантируем ровно один
    BranchMembership.objects.filter(user=user, is_primary=True).update(is_primary=False)
    if branches:
        BranchMembership.objects.filter(user=user, branch_id=branches[0].id).update(is_primary=True)


@transaction.atomic
def _set_primary_branch(user: User, branch_id):
    """Ставит primary филиал пользователю, учитывая constraint '1 primary'."""
    BranchMembership.objects.filter(user=user, is_primary=True).update(is_primary=False)
    if branch_id:
        BranchMembership.objects.filter(user=user, branch_id=branch_id).update(is_primary=True)


def _generate_password(length=6):
    """Простой пароль из цифр (легче ввести и запомнить)."""
    return "".join(secrets.choice(string.digits) for _ in range(length))


# ======================
# JWT
# ======================

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        email_val = attrs.get("email") or attrs.get("username")
        if isinstance(email_val, str):
            clean_email = email_val.strip().lower()
            if "email" in attrs:
                attrs["email"] = clean_email
            if "username" in attrs:
                attrs["username"] = clean_email
        data = super().validate(attrs)

        user = (
            User.objects.filter(pk=self.user.pk)
            .select_related("company", "custom_role")
            .prefetch_related(
                Prefetch(
                    "branch_memberships",
                    queryset=BranchMembership.objects.select_related("branch"),
                ),
            )
            .first()
        )
        if user is None:
            raise serializers.ValidationError("Пользователь не найден.")
        self.user = user

        if not getattr(user, "is_active", True):
            raise ValidationError("Аккаунт деактивирован.")

        if (
            user.company
            and not getattr(user.company, "is_active", True)
            and not getattr(user, "is_platform_admin", False)
        ):
            raise PermissionDenied("Компания заблокирована. Обратитесь в поддержку NUR.")

        branch_ids = []
        primary_branch_id = None
        for mb in user.branch_memberships.all():
            branch_ids.append(mb.branch_id)
            if mb.is_primary:
                primary_branch_id = mb.branch_id

        data.update({
            "user_id": user.id,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "avatar": user.avatar,
            "phone_number": user.phone_number,
            "track_number": user.track_number,
            "company": user.company.name if user.company else None,
            "role": user.role_display,
            "is_platform_admin": bool(getattr(user, "is_platform_admin", False)),
            "can_manage_lead_ad_spend": bool(getattr(user, "can_manage_lead_ad_spend", False)),
            "branch_ids": branch_ids,
            "primary_branch_id": primary_branch_id,
        })
        return data


# ======================
# Branch
# ======================

class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = [
            "id", "name", "code", "address", "phone", "email",
            "timezone", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class BranchCreateUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = [
            "id", "name", "code", "address", "phone", "email",
            "timezone", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        request = self.context.get("request")
        company = _get_user_company(getattr(request, "user", None)) if request else None
        if not company:
            raise serializers.ValidationError("Компания не определена.")

        code = attrs.get("code")
        if code:
            qs = Branch.objects.filter(company=company, code=code)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"code": "Код филиала должен быть уникален в пределах компании."})

        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        company = _get_user_company(getattr(request, "user", None)) if request else None
        if not company:
            raise serializers.ValidationError("Компания не определена для создания филиала.")
        return Branch.objects.create(company=company, **validated_data)


# ======================
# User (current user)
# ======================

def _get_user_consulting_regions(user):
    codes = user.get_consulting_region_codes() if hasattr(user, "get_consulting_region_codes") else []
    if not codes:
        return []
    try:
        from apps.consalting.models import RegionalFunnelRuleConsalting, REGION_LABELS
        company = getattr(user, "company", None) or getattr(user, "owned_company", None)
        if not company:
            return [{"code": c, "label": REGION_LABELS.get(c, c), "funnel_id": None} for c in codes]
        rules = RegionalFunnelRuleConsalting.objects.filter(
            routing__company=company,
            region_code__in=codes,
            is_active=True,
        ).select_related("funnel")
        rule_map = {r.region_code: r for r in rules}
        result = []
        for code in codes:
            r = rule_map.get(code)
            label = (r.label if r and r.label else REGION_LABELS.get(code, code)) if r else REGION_LABELS.get(code, code)
            funnel_id = str(r.funnel_id) if (r and r.funnel_id) else None
            result.append({
                "code": code,
                "label": label,
                "funnel_id": funnel_id,
            })
        return result
    except Exception:
        return []


# ТЗ 29.10 Б-101: новые поля карточки сотрудника
EMPLOYEE_CARD_FIELDS = [
    "is_active", "hired_at", "fired_at", "birth_date", "address", "emergency_contact",
    "comment", "language", "default_cashbox",
]


def _check_phone_digits(value, current=None):
    """Телефон: 7–15 цифр (допустимы +, пробелы, скобки, дефисы). Пустая строка очищает поле.
    Проверяется только новое значение: у существующих сотрудников плохие телефоны не ломаем."""
    if value is None:
        return None
    value = str(value).strip()
    if value == "":
        return None
    if current is not None and value == str(current).strip():
        return value
    digits = re.sub(r"[\s()+\-.]", "", value)
    if not digits.isdigit() or not 7 <= len(digits) <= 15:
        raise serializers.ValidationError("Телефон: от 7 до 15 цифр.", code="phone_invalid")
    return value


class EmployeeFieldsMixin:
    """Общая валидация полей сотрудника (создание и PATCH)."""

    def validate_phone_number(self, value):
        inst = getattr(self, "instance", None)
        return _check_phone_digits(value, getattr(inst, "phone_number", None))

    def validate_track_number(self, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def validate_default_cashbox(self, value):
        if value is None:
            return None
        inst = getattr(self, "instance", None)
        company = getattr(inst, "company", None)
        if company is None:
            u = self.context["request"].user
            company = getattr(u, "owned_company", None) or getattr(u, "company", None)
        if company is None or value.company_id != company.id:
            raise serializers.ValidationError("Касса не найдена в вашей компании.", code="cashbox_not_found")
        return value


class UserSerializer(serializers.ModelSerializer):
    can_view_showcase = serializers.SerializerMethodField()

    def get_can_view_showcase(self, obj):
        if getattr(obj, "company", None):
            return bool(getattr(obj.company, "can_view_showcase", False))
        return False

    password = serializers.CharField(write_only=True, required=False, min_length=8, style={"input_type": "password"})
    role_display = serializers.CharField(read_only=True)

    branch_ids = serializers.SerializerMethodField()
    primary_branch_id = serializers.SerializerMethodField()
    funnel_grants = serializers.SerializerMethodField()
    consulting_region_codes = serializers.SerializerMethodField()
    consulting_regions = serializers.SerializerMethodField()

    def get_consulting_region_codes(self, obj):
        return obj.get_consulting_region_codes()

    def get_consulting_regions(self, obj):
        return _get_user_consulting_regions(obj)

    def get_funnel_grants(self, obj):
        try:
            return [
                {
                    "funnel_id": str(g.funnel_id),
                    "can_manage_leads": g.can_manage_leads,
                    "can_manage_stages": g.can_manage_stages,
                }
                for g in obj.funnel_grants.all()
            ]
        except Exception:
            return []

    def _memberships_once(self, obj):
        cache = self.context.setdefault("_branch_memberships_cache", {})
        oid = obj.pk
        if oid not in cache:
            cache[oid] = list(obj.branch_memberships.all())
        return cache[oid]

    def get_branch_ids(self, obj):
        return [m.branch_id for m in self._memberships_once(obj)]

    def get_primary_branch_id(self, obj):
        for m in self._memberships_once(obj):
            if m.is_primary:
                return m.branch_id
        return None

    class Meta:
        model = User
        fields = [
            "id", "email", "password",
            "first_name", "last_name", "track_number", "phone_number", "avatar",
            "company", "role", "custom_role", "role_display", "is_platform_admin",
            "consulting_region_codes", "consulting_regions",

            "can_view_dashboard", "can_view_cashbox", "can_view_departments",
            "can_view_orders", "can_view_analytics", "can_view_department_analytics",
            "can_view_products", "can_view_booking",
            "can_view_employees", "can_view_clients",
            "can_view_brand_category", "can_view_settings", "can_view_sale",

            "can_view_building_analytics", "can_view_building_cash_register",
            "can_view_building_clients", "can_view_building_department",
            "can_view_building_employess", "can_view_building_notification",
            "can_view_building_procurement", "can_view_building_projects",
            "can_view_building_salary", "can_view_building_sell",
            "can_view_building_stock", "can_view_building_treaty",
            "can_view_building_work_process", "can_view_building_objects",
            "can_view_additional_services", "can_view_debts",

            "can_view_barber_clients", "can_view_barber_services",
            "can_view_barber_history", "can_view_barber_records",

            "can_view_hostel_rooms", "can_view_hostel_booking",
            "can_view_hostel_clients", "can_view_hostel_analytics",

            "can_view_cafe_menu", "can_view_cafe_orders",
            "can_view_cafe_purchasing", "can_view_cafe_booking",
            "can_view_cafe_clients", "can_view_cafe_tables",
            "can_view_cafe_cook", "can_view_cafe_inventory", "can_view_cafe_calculation",
            "can_view_cafe_order_pay", "can_view_cafe_order_return",

            "can_view_school_students", "can_view_school_groups",
            "can_view_school_lessons", "can_view_school_teachers",
            "can_view_school_leads", "can_view_school_invoices",

            "can_view_showcase", "can_view_client_requests", "can_view_salary",
            "can_view_sales", "can_view_services",
            "can_view_agent", "can_view_catalog",
            "can_view_branch", "can_view_logistics", "can_view_request", "can_view_shifts",
            "can_view_cashier", "can_use_pos", "can_view_document", "can_view_market_scales", "can_view_market_label",
            "can_view_market_discount", "can_view_market_edit_price", "can_view_market_delete_cart_item",
            "can_view_market_procurement", "can_view_market_supplier", "can_view_market_employee_return",

            # Склад: опасные операции (null — по умолчанию для роли)
            "can_post_negative_stock", "can_unpost_documents", "can_change_document_date",
            "can_post_closed_period", "can_cash_negative",
            *READ_ONLY_FIELDS,

            # Consulting: воронка продаж
            "can_view_funnel", "can_manage_funnel_leads", "can_manage_funnel_stages", "can_create_funnel", "can_manage_lead_ad_spend", "can_view_leads_inbox", "funnel_grants",

            "branch_ids", "primary_branch_id",
            "created_at", "updated_at",
            *EMPLOYEE_CARD_FIELDS,  # ТЗ 29.10 Б-101
        ]
        read_only_fields = [
            "id", "created_at", "updated_at", "company", "is_platform_admin",
            # в профиле сам сотрудник меняет только язык; остальное — карточка от владельца
            "is_active", "hired_at", "fired_at", "birth_date", "address", "emergency_contact",
            "comment", "default_cashbox",
        ]

    def validate_email(self, value):
        value = (value or "").strip().lower()
        if self.instance and (self.instance.email or "").strip().lower() == value:
            return value
        if User.objects.filter(email=value, is_active=True).exists():
            raise serializers.ValidationError("Email уже занят другим пользователем.")
        return value

    def validate_avatar(self, value):
        if value and not value.startswith(("http://", "https://")):
            raise serializers.ValidationError("Некорректная ссылка на аватар.")
        return value

    def validate(self, data):
        request = self.context.get("request")
        current_user = getattr(request, "user", None) if request else None

        if current_user and getattr(current_user, "role", None) == "manager":
            # менеджеру запрещаем менять любые permission-флаги
            restricted_admin_permissions = {"can_view_settings", "can_manage_funnel_stages", "can_create_funnel"}
            for p in restricted_admin_permissions:
                data[p] = False
                raise serializers.ValidationError("Менеджеру запрещено изменять права доступа.")

        # проверка типов только для тех флагов, которые реально прислали
        for k, v in data.items():
            if k.startswith("can_view_") and not isinstance(v, bool):
                raise serializers.ValidationError({k: "Значение должно быть True или False."})

        # Права на опасные складские операции выдаёт только владелец.
        from apps.warehouse.op_permissions import OP_PERMISSION_FIELDS, is_company_owner

        # Фронт может присылать карточку целиком — запрещаем только реальное изменение.
        sent = [
            k for k in OP_PERMISSION_FIELDS
            if k in data and data[k] != (getattr(self.instance, k, None) if self.instance is not None else None)
        ]
        if sent and current_user is not None and not is_company_owner(current_user):
            raise serializers.ValidationError({sent[0]: "Это право может выдать только владелец компании."})

        return data

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        return _apply_cashier_sector_gate(rep, instance)


# ======================
# Owner register
# ======================

class OwnerRegisterSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(write_only=True, min_length=8, style={"input_type": "password"})
    password2 = serializers.CharField(write_only=True, style={"input_type": "password"})
    company_name = serializers.CharField(write_only=True, required=True)
    company_sector_id = serializers.UUIDField(write_only=True, required=True)
    company_region = serializers.ChoiceField(
        choices=KyrgyzstanRegion.choices,
        write_only=True,
        required=False,
        allow_null=True,
        allow_blank=True,
    )
    subscription_plan_id = serializers.UUIDField(write_only=True, required=True)

    class Meta:
        model = User
        fields = [
            "email", "password", "password2",
            "first_name", "last_name",
            "avatar",
            "company_name", "company_sector_id", "company_region", "subscription_plan_id",
        ]

    def validate_email(self, value):
        value = (value or "").strip().lower()
        if User.objects.filter(email=value, is_active=True).exists():
            raise serializers.ValidationError("Этот email уже используется.")
        return value

    def validate(self, data):
        if data["password"] != data["password2"]:
            raise serializers.ValidationError({"password2": "Пароли не совпадают."})
        return data

    @transaction.atomic
    def create(self, validated_data):
        company_name = validated_data.pop("company_name")
        sector_id = validated_data.pop("company_sector_id")
        company_region = validated_data.pop("company_region", None) or None
        plan_id = validated_data.pop("subscription_plan_id")
        validated_data.pop("password2")

        try:
            sector = Sector.objects.get(id=sector_id)
        except Sector.DoesNotExist:
            raise serializers.ValidationError({"company_sector_id": "Выбранный сектор не найден."})

        industries = sector.industries.all()
        if not industries.exists():
            raise serializers.ValidationError({"company_sector_id": "Для выбранного сектора не найдена индустрия."})
        if industries.count() > 1:
            raise serializers.ValidationError({"company_sector_id": "Для выбранного сектора найдено несколько индустрий."})
        industry = industries.first()

        try:
            subscription_plan = SubscriptionPlan.objects.get(id=plan_id)
        except SubscriptionPlan.DoesNotExist:
            raise serializers.ValidationError({"subscription_plan_id": "Выбранный тариф не найден."})

        user = User.objects.create(
            email=(validated_data["email"] or "").strip().lower(),
            first_name=validated_data.get("first_name"),
            last_name=validated_data.get("last_name"),
            avatar=validated_data.get("avatar"),
            role=Roles.OWNER,
            is_active=True,
        )

        # владельцу — все доступы
        for f in [x.name for x in User._meta.fields if x.name.startswith("can_view_")]:
            setattr(user, f, True)

        user.set_password(validated_data["password"])
        user.save()

        company = Company.objects.create(
            name=company_name,
            industry=industry,
            sector=sector,
            region=company_region,
            subscription_plan=subscription_plan,
            owner=user,
            scale_barcode_layout=SCALE_BARCODE_LAYOUT_PLU,
        )

        user.company = company
        user.save(update_fields=["company"])

        # ВНИМАНИЕ: «Основная касса компании» создаётся сигналом
        # apps/construction/signals.py::create_cashbox_for_company.
        # Здесь дублирующее создание убрано, чтобы не плодить пустые кассы.

        return user


# ======================
# Employee create
# ======================

class EmployeeCreateSerializer(EmployeeFieldsMixin, serializers.ModelSerializer):
    email = serializers.EmailField(required=True)
    role_display = serializers.CharField(read_only=True)

    primary_branch = serializers.UUIDField(required=False, allow_null=True, write_only=True)
    branches = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
        write_only=True,
        help_text="Список UUID филиалов, к которым нужно прикрепить сотрудника",
    )
    region_code = serializers.CharField(required=False, allow_blank=True, write_only=True)
    consulting_region_codes = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        write_only=True,
    )

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "last_name", "track_number", "phone_number", "avatar",
            "role", "custom_role", "role_display",
            "region_code", "consulting_region_codes", "can_use_pos",  # ТЗ Н-16: id всегда в ответе

            # все can_view_* которые ты раньше использовал
            *[f.name for f in User._meta.fields if f.name.startswith("can_view_")],
            "can_manage_funnel_leads", "can_manage_funnel_stages", "can_create_funnel", "can_manage_lead_ad_spend",

            "primary_branch", "branches",
            *EMPLOYEE_CARD_FIELDS,  # ТЗ 29.10 Б-101
        ]
        read_only_fields = ["is_active", "fired_at"]

    def validate_email(self, value):
        value = (value or "").strip().lower()
        # ТЗ 29.10 Б-101: email уникален; уволенные (is_active=False) сохраняют email, поэтому проверяем всех
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Этот email уже используется.", code="email_taken")
        return value

    def validate(self, data):
        request = self.context["request"]
        current_user = request.user

        if getattr(current_user, "role", None) in ("manager", "salesperson"):
            raise serializers.ValidationError("У вас нет прав для создания сотрудников.")

        company = getattr(current_user, "owned_company", None) or getattr(current_user, "company", None)
        if not company:
            raise serializers.ValidationError("Компания не определена.")

        primary_branch_id = data.get("primary_branch")
        branch_ids = list(data.get("branches") or [])

        # primary обязательно должен быть в списке
        if primary_branch_id and primary_branch_id not in branch_ids:
            branch_ids = [primary_branch_id] + branch_ids

        branches = _validate_branch_ids_for_company(branch_ids, company)
        data["_branches_objects"] = branches

        # Supervisor clamping (§6)
        if getattr(current_user, "role", None) == "supervisor":
            my_regions = current_user.get_consulting_region_codes()
            if not my_regions:
                raise serializers.ValidationError("У руководителя не настроены регионы.")
            req_region = (data.get("region_code") or "").strip().lower()
            if not req_region and data.get("consulting_region_codes"):
                req_region = str(data.get("consulting_region_codes")[0]).strip().lower()

            if len(my_regions) == 1:
                target_region = my_regions[0]
                if req_region and req_region != target_region:
                    raise serializers.ValidationError({"region_code": f"Руководитель может создавать сотрудников только своего региона ({target_region})."})
            else:
                if not req_region or req_region not in my_regions:
                    raise serializers.ValidationError({"region_code": "Необходимо указать допустимый region_code из ваших регионов."})
                target_region = req_region

            data["role"] = "salesperson"
            data["consulting_region_codes"] = [target_region]
            data["_target_region"] = target_region

            restricted_admin_permissions = {"can_view_settings", "can_manage_funnel_stages", "can_create_funnel"}
            for p in restricted_admin_permissions:
                data[p] = False
        else:
            req_region = (data.get("region_code") or "").strip().lower()
            if req_region and not data.get("consulting_region_codes"):
                data["consulting_region_codes"] = [req_region]
            elif data.get("consulting_region_codes"):
                data["consulting_region_codes"] = [str(x).strip().lower() for x in data.get("consulting_region_codes")]
            if data.get("consulting_region_codes"):
                data["_target_region"] = data["consulting_region_codes"][0]

        return data

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        creator = request.user
        company = getattr(creator, "owned_company", None) or getattr(creator, "company", None)

        if company is not None:
            from apps.users.services_subscription import employee_limit_error

            # Лимит тарифа; блокировка строки компании — чтобы два запроса разом не прошли оба.
            Company.objects.select_for_update().filter(pk=company.pk).first()
            limit_error = employee_limit_error(company)
            if limit_error:
                # ТЗ 29.10 Б-101: новое поведение — 403 employee_limit_reached {limit, current}
                from core.api_compat import is_strict
                if is_strict(request, company):
                    from apps.users.services_subscription import employee_limit_reached_body
                    raise ConflictError(employee_limit_reached_body(company, limit_error), 403)
                raise serializers.ValidationError({"detail": limit_error, "code": "employee_limit"})

        primary_branch_id = validated_data.pop("primary_branch", None)
        validated_data.pop("branches", None)
        branches_objects = validated_data.pop("_branches_objects", [])

        region_code = validated_data.pop("region_code", None)
        target_region = validated_data.pop("_target_region", None)
        consulting_region_codes = validated_data.pop("consulting_region_codes", [])
        if target_region and not consulting_region_codes:
            consulting_region_codes = [target_region]

        generated_password = _generate_password()

        access_flags = {}
        for f in [x.name for x in User._meta.fields if x.name.startswith("can_view_")]:
            access_flags[f] = validated_data.pop(f, None)

        user = User.objects.create(
            email=(validated_data.get("email") or "").strip().lower(),
            first_name=validated_data.get("first_name"),
            last_name=validated_data.get("last_name"),
            track_number=validated_data.get("track_number"),
            phone_number=validated_data.get("phone_number"),
            avatar=validated_data.get("avatar"),
            role=validated_data.get("role"),
            custom_role=validated_data.get("custom_role"),
            consulting_region_codes=consulting_region_codes,
            company=company,
            is_active=True,
            **{k: validated_data.get(k) for k in (
                "hired_at", "birth_date", "address", "emergency_contact", "comment", "language", "default_cashbox",
            )},
        )
        user.set_password(generated_password)

        if getattr(creator, "role", None) == "supervisor":
            restricted_admin_permissions = {"can_view_settings", "can_manage_funnel_stages", "can_create_funnel"}
            for k, v in access_flags.items():
                if k in restricted_admin_permissions:
                    setattr(user, k, False)
                elif v is not None:
                    setattr(user, k, v)
            if not any(bool(getattr(user, k, False)) for k in access_flags.keys()):
                user.can_view_funnel = True
                user.can_manage_funnel_leads = True
                user.can_view_sale = True
        elif all(v is None for v in access_flags.values()):
            if user.role in ["owner", "admin"]:
                for k in access_flags.keys():
                    setattr(user, k, True)
            elif user.role == "manager":
                user.can_view_cashbox = True
                user.can_view_orders = True
                user.can_view_products = True
            else:
                user.can_view_dashboard = True
        else:
            for k, v in access_flags.items():
                if v is not None:
                    setattr(user, k, v)

        if "can_use_pos" in validated_data:  # ТЗ Н-16
            user.can_use_pos = bool(validated_data["can_use_pos"])
        user.save()

        # Funnel grant for region
        if target_region:
            try:
                from apps.consalting.models import RegionalFunnelRuleConsalting, EmployeeFunnelGrant
                rule = RegionalFunnelRuleConsalting.objects.filter(
                    routing__company=company,
                    region_code=target_region,
                ).first()
                if rule and rule.funnel:
                    EmployeeFunnelGrant.objects.update_or_create(
                        employee=user,
                        funnel=rule.funnel,
                        defaults={"can_manage_leads": True, "can_manage_stages": False},
                    )
            except Exception:
                pass

        # memberships
        if branches_objects:
            BranchMembership.objects.bulk_create(
                [BranchMembership(user=user, branch=b, is_primary=False) for b in branches_objects],
                ignore_conflicts=True,
            )
            if primary_branch_id is None:
                primary_branch_id = branches_objects[0].id
            _set_primary_branch(user, primary_branch_id)

        try:
            send_mail(
                subject="Добро пожаловать в CRM",
                message=(
                    f"Здравствуйте, {user.first_name}!\n\n"
                    f"Ваш аккаунт создан в системе.\n"
                    f"Логин: {user.email}\n"
                    f"Пароль: {generated_password}\n\n"
                    "Рекомендуем сменить пароль после входа."
                ),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[user.email],
                fail_silently=False,
            )
        except Exception:
            pass

        self._generated_password = generated_password
        return user

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        rep["generated_password"] = getattr(self, "_generated_password", None)
        rep["consulting_region_codes"] = instance.get_consulting_region_codes()
        rep["consulting_regions"] = _get_user_consulting_regions(instance)
        rep["branches_attached"] = [
            {"id": str(m.branch_id), "name": m.branch.name, "is_primary": m.is_primary}
            for m in instance.branch_memberships.select_related("branch").all()
        ]
        return rep


# ======================
# Employee update
# ======================

class ConflictError(APIException):
    """Ошибка с произвольным статусом и телом {detail, code, ...} (409 last_admin / open_shift)."""
    status_code = 409

    def __init__(self, body, status_code=409):
        self.status_code = status_code
        super().__init__(detail=body)
        self.detail = body  # числа (limit/current) остаются числами, не строками


def _check_dismissal_allowed(request, target_user):
    """ТЗ 29.10 Б-101: 409 last_admin (последний владелец/админ) и open_shift (открытая смена).
    Только для нового клиента / strict-компании; старые кассы ведут себя как раньше
    (открытые смены закрываются автоматически)."""
    from core.api_compat import is_strict

    if not is_strict(request, target_user.company):
        return
    if target_user.role in ("owner", "admin"):
        others = User.objects.filter(
            company=target_user.company, is_active=True, deleted_at__isnull=True, role__in=("owner", "admin"),
        ).exclude(pk=target_user.pk).exists()
        if not others:
            raise ConflictError({"detail": "Нельзя убрать последнего администратора компании.", "code": "last_admin"})
    from apps.construction.models import CashShift
    shift = CashShift.objects.filter(cashier_id=target_user.pk, status=CashShift.Status.OPEN).first()
    if shift:
        raise ConflictError({
            "detail": "У сотрудника открыта смена. Сначала закройте её.", "code": "open_shift",
            "shift": str(shift.pk),
        })


class FunnelGrantSerializer(serializers.Serializer):
    """Доступ сотрудника к воронке consalting: {funnel_id, can_manage_leads, can_manage_stages}."""
    funnel_id = serializers.UUIDField()
    can_manage_leads = serializers.BooleanField(required=False, default=False)
    can_manage_stages = serializers.BooleanField(required=False, default=False)


class EmployeeUpdateSerializer(EmployeeFieldsMixin, serializers.ModelSerializer):
    role_display = serializers.CharField(read_only=True)
    branch_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="Полный новый список филиалов сотрудника. Если не указано — без изменений.",
    )
    funnel_grants = FunnelGrantSerializer(
        many=True, required=False,
        help_text="Полный новый список доступов к воронкам consalting. Если не указано — без изменений.",
    )
    consulting_region_codes = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Коды регионов (только для руководителя/администратора)",
    )

    class Meta:
        model = User
        fields = [
            "id", "first_name", "last_name", "track_number", "phone_number", "avatar",
            "role", "custom_role", "role_display",
            "consulting_region_codes", "can_use_pos",
            *[f.name for f in User._meta.fields if f.name.startswith("can_view_")],
            "can_manage_funnel_leads",
            "can_manage_funnel_stages",
            "can_create_funnel",
            "can_manage_lead_ad_spend",
            *READ_ONLY_FIELDS,
            "funnel_grants",
            "branch_ids",
            *EMPLOYEE_CARD_FIELDS,  # ТЗ 29.10 Б-101
        ]
        read_only_fields = ["id"]

    def validate(self, data):
        request = self.context["request"]
        current_user = request.user
        target_user = self.instance

        if getattr(current_user, "role", None) == "manager":
            raise serializers.ValidationError("Менеджеру запрещено редактировать сотрудников.")

        if current_user.id == target_user.id:
            raise serializers.ValidationError("Вы не можете редактировать самого себя через этот интерфейс.")

        if getattr(target_user, "role", None) == "owner" and not getattr(current_user, "is_superuser", False):
            if "role" in data and data["role"] != "owner":
                raise serializers.ValidationError("Вы не можете изменить роль владельца компании.")

        # Supervisor restrictions (§5, §6)
        if getattr(current_user, "role", None) == "supervisor":
            my_regions = current_user.get_consulting_region_codes()
            target_regions = target_user.get_consulting_region_codes()
            if not any(r in my_regions for r in target_regions):
                raise serializers.ValidationError("Вы можете редактировать только сотрудников своего региона.")
            if "role" in data and data["role"] != target_user.role:
                raise serializers.ValidationError("Руководитель не может изменять роль сотрудника.")
            restricted_admin_permissions = {"can_view_settings", "can_manage_funnel_stages", "can_create_funnel"}
            for p in restricted_admin_permissions:
                data[p] = False
            if "consulting_region_codes" in data:
                new_codes = [str(x).strip().lower() for x in data["consulting_region_codes"]]
                if not set(new_codes).issubset(set(my_regions)):
                    raise serializers.ValidationError("Нельзя привязать сотрудника к чужому региону.")

        branch_ids = data.get("branch_ids", None)
        if branch_ids is not None:
            company = target_user.company
            _validate_branch_ids_for_company(branch_ids, company)

        # ТЗ 29.10 Б-101: мягкое увольнение / возврат сотрудника через is_active
        if "is_active" in data and data["is_active"] != target_user.is_active:
            if getattr(current_user, "role", None) == "supervisor":
                raise ConflictError(
                    {"detail": "Руководитель региона не может увольнять сотрудников.", "code": "permission_denied",
                     "required": "can_edit_employees"}, 403)
            if data["is_active"] is False:
                if getattr(target_user, "role", None) == "owner" and not getattr(current_user, "is_superuser", False):
                    raise serializers.ValidationError({"is_active": "Нельзя уволить владельца компании."})
                _check_dismissal_allowed(request, target_user)
            else:
                from apps.users.services_subscription import employee_limit_error, employee_limit_reached_body
                limit_error = employee_limit_error(target_user.company)
                if limit_error:
                    raise ConflictError(employee_limit_reached_body(target_user.company, limit_error), 403)
                if getattr(target_user, "deleted_at", None):
                    raise serializers.ValidationError({"is_active": "Удалённого сотрудника вернуть нельзя."})

        return data

    @transaction.atomic
    def update(self, instance, validated_data):
        branch_ids = validated_data.pop("branch_ids", None)
        funnel_grants = validated_data.pop("funnel_grants", None)
        consulting_region_codes = validated_data.pop("consulting_region_codes", None)

        # ТЗ 29.10 Б-101: уволили — ставим дату увольнения и закрываем открытые смены; вернули — снимаем дату
        if "is_active" in validated_data and validated_data["is_active"] != instance.is_active:
            if validated_data["is_active"] is False:
                instance._close_open_cash_shifts(by_user=self.context["request"].user)
                validated_data.setdefault("fired_at", timezone.localdate())
            else:
                validated_data.setdefault("fired_at", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if consulting_region_codes is not None:
            instance.consulting_region_codes = [str(x).strip().lower() for x in consulting_region_codes]

        instance.save()

        if branch_ids is not None:
            branches = _validate_branch_ids_for_company(branch_ids, instance.company)
            _sync_user_branches(instance, branches)

        if funnel_grants is not None:
            self._sync_funnel_grants(instance, funnel_grants)

        return instance

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        rep["consulting_region_codes"] = instance.get_consulting_region_codes()
        rep["consulting_regions"] = _get_user_consulting_regions(instance)
        return rep

    def _sync_funnel_grants(self, instance, grants):
        """Полная замена доступов сотрудника к воронкам (только воронки своей компании)."""
        from apps.consalting.models import EmployeeFunnelGrant, FunnelConsalting

        company = instance.company
        valid_funnel_ids = set(
            FunnelConsalting.objects.filter(
                company=company,
                id__in=[g["funnel_id"] for g in grants],
            ).values_list("id", flat=True)
        )
        EmployeeFunnelGrant.objects.filter(employee=instance).delete()
        EmployeeFunnelGrant.objects.bulk_create([
            EmployeeFunnelGrant(
                employee=instance,
                funnel_id=g["funnel_id"],
                can_manage_leads=bool(g.get("can_manage_leads", False)),
                can_manage_stages=bool(g.get("can_manage_stages", False)),
            )
            for g in grants
            if g["funnel_id"] in valid_funnel_ids
        ])


# ======================
# Lists / dictionaries
# ======================

class UserListSerializer(serializers.ModelSerializer):
    role_display = serializers.CharField(read_only=True)
    branch_ids = serializers.ListField(child=serializers.UUIDField(), read_only=True, source="allowed_branch_ids")
    primary_branch_id = serializers.UUIDField(read_only=True, source="primary_branch.id")
    consulting_region_codes = serializers.SerializerMethodField()
    consulting_regions = serializers.SerializerMethodField()

    def get_consulting_region_codes(self, obj):
        return obj.get_consulting_region_codes()

    def get_consulting_regions(self, obj):
        return _get_user_consulting_regions(obj)

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "last_name", "track_number", "phone_number",
            "role", "custom_role", "role_display", "avatar",
            "consulting_region_codes", "consulting_regions", "can_use_pos",
            *[f.name for f in User._meta.fields if f.name.startswith("can_view_")],
            "can_manage_funnel_leads", "can_manage_funnel_stages", "can_create_funnel", "can_manage_lead_ad_spend",
            *READ_ONLY_FIELDS,
            "branch_ids", "primary_branch_id",
            *EMPLOYEE_CARD_FIELDS,  # ТЗ 29.10 Б-101
        ]

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        return _apply_cashier_sector_gate(rep, instance)


class UserWithPermissionsSerializer(serializers.ModelSerializer):
    role_display = serializers.CharField(read_only=True)
    branch_ids = serializers.ListField(child=serializers.UUIDField(), read_only=True, source="allowed_branch_ids")
    primary_branch_id = serializers.UUIDField(read_only=True, source="primary_branch.id")
    consulting_region_codes = serializers.SerializerMethodField()
    consulting_regions = serializers.SerializerMethodField()

    def get_consulting_region_codes(self, obj):
        return obj.get_consulting_region_codes()

    def get_consulting_regions(self, obj):
        return _get_user_consulting_regions(obj)

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "last_name", "track_number", "phone_number",
            "role", "custom_role", "role_display", "avatar",
            "consulting_region_codes", "consulting_regions", "can_use_pos",
            *[f.name for f in User._meta.fields if f.name.startswith("can_view_")],
            "can_manage_funnel_leads", "can_manage_funnel_stages", "can_create_funnel", "can_manage_lead_ad_spend",
            *READ_ONLY_FIELDS,
            "branch_ids", "primary_branch_id",
        ]

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        return _apply_cashier_sector_gate(rep, instance)


class SectorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sector
        fields = ["id", "name"]


class IndustrySerializer(serializers.ModelSerializer):
    sectors = SectorSerializer(many=True, read_only=True)

    class Meta:
        model = Industry
        fields = ["id", "name", "sectors"]


class FeatureSerializer(serializers.ModelSerializer):
    class Meta:
        model = Feature
        fields = ["id", "name", "description"]


class SubscriptionPlanSerializer(serializers.ModelSerializer):
    features = FeatureSerializer(many=True, read_only=True)
    code = serializers.SerializerMethodField()

    class Meta:
        model = SubscriptionPlan
        fields = ["id", "code", "name", "price", "description", "features"]

    def get_code(self, obj):
        from apps.users.services_subscription import resolve_plan_code
        return resolve_plan_code(obj)


def company_addons_payload(company):
    from apps.users.models import CompanyAddon

    return [
        {"code": a.code, "active": a.is_effective(), "until": a.until.isoformat() if a.until else None}
        for a in CompanyAddon.objects.filter(company=company).order_by("code")
    ]


def company_feature_codes(company):
    """Возможности тарифа (Feature.name из тарифа компании) + действующие платные функции."""
    codes = []
    plan = getattr(company, "subscription_plan", None)
    if plan is not None:
        codes.extend(plan.features.order_by("name").values_list("name", flat=True))
    codes.extend(a["code"] for a in company_addons_payload(company) if a["active"])
    if getattr(company, "can_view_showcase", False):
        codes.append("showcase_editor")
    free = _client_app_free()
    if free is not None:
        codes.append(free[0])
    return list(dict.fromkeys(c for c in codes if c))


def _client_app_free():
    """
    Приложение клиентов: пока идёт бесплатный период (ClientAppConfig.free_until),
    функция есть у каждой компании. → (code, until_iso | None) или None, если период кончился.
    """
    try:
        from apps.clientapp.services import app_free_until
        from apps.clientapp.models import ClientAppConfig

        free, until = app_free_until()
        if not free:
            return None
        return ClientAppConfig.get().paid_feature_code, (until.isoformat() if until else None)
    except Exception:
        return None


def company_features_detail(company, *, plan_until=None):
    """
    BE2-02: [{code, until}] — функции тарифа действуют до конца подписки (plan_until),
    платные функции — до своей даты (null — бессрочно). Только действующие.
    """
    out = {}
    plan = getattr(company, "subscription_plan", None)
    if plan is not None:
        for code in plan.features.order_by("name").values_list("name", flat=True):
            if code:
                out[code] = plan_until
    for a in company_addons_payload(company):
        if a["active"]:
            out[a["code"]] = a["until"]
    if getattr(company, "can_view_showcase", False):
        if "showcase_editor" not in out:
            out["showcase_editor"] = plan_until
    free = _client_app_free()
    if free is not None and free[0] not in out:
        out[free[0]] = free[1]
    return [{"code": code, "until": until} for code, until in out.items()]


def company_features_with_source(company, *, plan_until=None):
    """ТЗ Н-32: [{code, until, source}], source: tariff | key | trial | support."""
    from apps.users.models import CompanyAddon

    out = {}
    plan = getattr(company, "subscription_plan", None)
    if plan is not None:
        for code in plan.features.order_by("name").values_list("name", flat=True):
            if code:
                out[code] = {"code": code, "until": plan_until, "source": "tariff"}
    for a in CompanyAddon.objects.filter(company=company).order_by("code"):
        if a.is_effective():
            out[a.code] = {"code": a.code, "until": a.until.isoformat() if a.until else None,
                           "source": a.source if a.source in ("key", "trial", "support") else "support"}
    if getattr(company, "can_view_showcase", False) and "showcase_editor" not in out:
        out["showcase_editor"] = {"code": "showcase_editor", "until": plan_until, "source": "tariff"}
    free = _client_app_free()
    if free is not None and free[0] not in out:
        out[free[0]] = {"code": free[0], "until": free[1], "source": "support"}
    return list(out.values())


class CompanySerializer(serializers.ModelSerializer):
    industry = IndustrySerializer(read_only=True)
    subscription_plan = SubscriptionPlanSerializer(read_only=True)
    owner = UserListSerializer(read_only=True)
    sector = SectorSerializer(read_only=True)
    region_display = serializers.CharField(source="get_region_display", read_only=True)

    class Meta:
        model = Company
        fields = [
            "id", "name","slug",
            "industry", "sector", "region", "region_display",
            "phone", "phones_howcase", "whatsapp_phone", "subscription_plan",
            "owner",
            "created_at", "start_date", "end_date",
            "can_view_documents", "can_view_whatsapp", "can_view_instagram", "can_view_telegram", "can_view_showcase",
            "cashier_password",
            "llc", "inn", "okpo", "score", "bik", "address",
            "scale_barcode_mode",
            "scale_barcode_layout",
            "scale_barcode_amount_unit",
            "max_discount_percent",
            "finance_closed_until",
            "appointment_work_start",
            "appointment_work_end",
            "market_sphere",
        ]
        extra_kwargs = {"cashier_password": {"write_only": True}, "finance_closed_until": {"read_only": True}}

    def validate_inn(self, value):
        # QA B24: ИНН компании — как у контрагента: пусто или 14 цифр.
        from apps.warehouse.validators import normalize_inn, validate_inn

        if self.instance is not None and normalize_inn(value) == normalize_inn(self.instance.inn):
            return value
        return validate_inn(value)

    def to_representation(self, instance):
        from apps.users.services_subscription import (
            build_subscription_payload,
            get_company_limits,
            get_bishkek_date,
            get_bishkek_datetime_str,
            is_user_owner,
        )

        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        is_owner = is_user_owner(user, instance)

        # QA B35: пароль кассира не отдаётся никому (поле только на запись);
        # фронту достаточно знать, задан ли он.
        data.pop("cashier_password", None)
        data["has_cashier_password"] = bool(getattr(instance, "cashier_password", None))
        # QA B47: заполнены ли реквизиты продавца для печатных форм и ЭСФ.
        data["requisites_complete"] = all(
            str(getattr(instance, f, "") or "").strip() for f in ("inn", "okpo", "score", "bik")
        )
        if not is_owner:
            if isinstance(data.get("subscription_plan"), dict):
                data["subscription_plan"].pop("price", None)

        if instance.created_at:
            data["created_at"] = get_bishkek_datetime_str(instance.created_at)

        start_date_local = get_bishkek_date(instance.start_date)
        data["start_date"] = start_date_local.isoformat() if start_date_local else None

        end_date_local = get_bishkek_date(instance.end_date)
        end_date_str = end_date_local.isoformat() if end_date_local else None
        data["end_date"] = end_date_str
        data["subscription_end_date"] = end_date_str

        sub = build_subscription_payload(instance, is_owner=is_owner)
        data["subscription"] = sub

        plan_code = sub["plan"]["code"] if sub.get("plan") else None
        data["limits"] = get_company_limits(instance, plan_code=plan_code)
        data["features"] = company_feature_codes(instance)
        data["features_detail"] = company_features_detail(instance, plan_until=end_date_str)
        data["market_spheres"] = instance.market_spheres or (
            [instance.market_sphere] if instance.market_sphere else []
        )
        # ТЗ ч.12, 2.8: откроется ли редактор витрины (то же условие, что у 403 в showcase/*),
        # чтобы программа заранее показывала «подключите услугу», а не ошибку.
        data["can_edit_showcase"] = bool(getattr(instance, "can_view_showcase", False))

        return data



class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(required=True, write_only=True)
    new_password2 = serializers.CharField(required=True, write_only=True)

    def validate(self, data):
        user = self.context["request"].user

        if not user.check_password(data["current_password"]):
            raise serializers.ValidationError({"current_password": "Неверный текущий пароль."})

        if data["new_password"] != data["new_password2"]:
            raise serializers.ValidationError({"new_password2": "Пароли не совпадают."})

        if data["new_password"] == data["current_password"]:
            raise serializers.ValidationError({"new_password": "Новый пароль должен отличаться от текущего."})

        validate_password(data["new_password"], user)
        return data

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password"])
        return user

_OPTIONAL_TEXT = (
    "llc",
    "inn",
    "okpo",
    "score",
    "bik",
    "address",
    "phone",
    "phones_howcase",
    "whatsapp_phone",
)


class CompanyUpdateSerializer(serializers.ModelSerializer):
    slug = serializers.CharField(required=False, max_length=SLUG_MAX)

    def validate_slug(self, value):
        value = normalize_slug(value)
        err = slug_format_error(value)
        if err:
            raise serializers.ValidationError(err)  # 400
        exclude_pk = self.instance.pk if self.instance else None
        if slug_taken_by_other(value, exclude_pk=exclude_pk):
            raise SlugConflict()  # 409
        return value

    def validate_inn(self, value):
        # QA B24: пусто или 14 цифр. Старое неверное значение, присланное без изменений
        # вместе с остальной формой, не блокирует сохранение других полей.
        from apps.warehouse.validators import normalize_inn, validate_inn

        if self.instance is not None and normalize_inn(value) == normalize_inn(self.instance.inn):
            return value
        return validate_inn(value)

    def validate_appointment_work_start(self, value):
        if not value:
            return "09:00"
        value = str(value).strip()
        import re
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
            raise serializers.ValidationError("Формат времени должен быть HH:MM (например, 08:00).")
        return value

    def validate_appointment_work_end(self, value):
        if not value:
            return "21:00"
        value = str(value).strip()
        import re
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
            raise serializers.ValidationError("Формат времени должен быть HH:MM (например, 20:00).")
        return value

    class Meta:
        model = Company
        fields = [
            "name",
            "slug",
            "llc",
            "inn",
            "okpo",
            "score",
            "bik",
            "address",
            "phone",
            "phones_howcase",
            "whatsapp_phone",
            "industry",
            "sector",
            "region",
            "scale_barcode_mode",
            "scale_barcode_layout",
            "scale_barcode_amount_unit",
            "appointment_work_start",
            "appointment_work_end",
            "finance_closed_until",  # ТЗ 29.10 Б-100
            "market_sphere",
            "market_spheres",
        ]

    def validate_market_spheres(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Ожидается список видов магазина.")
        allowed = set(Company.MarketSphere.values)
        bad = [v for v in value if v not in allowed]
        if bad:
            raise serializers.ValidationError(f"Неизвестный вид магазина: {', '.join(map(str, bad))}.")
        return list(dict.fromkeys(value))

    def validate(self, attrs):
        # market_sphere и market_spheres держим согласованными: основной вид — первый в списке.
        if "market_spheres" in attrs and "market_sphere" not in attrs:
            attrs["market_sphere"] = attrs["market_spheres"][0] if attrs["market_spheres"] else None
        elif "market_sphere" in attrs and "market_spheres" not in attrs:
            attrs["market_spheres"] = [attrs["market_sphere"]] if attrs["market_sphere"] else []
        elif "market_sphere" in attrs and attrs["market_sphere"]:
            spheres = attrs["market_spheres"]
            attrs["market_spheres"] = [attrs["market_sphere"]] + [v for v in spheres if v != attrs["market_sphere"]]

        for f in _OPTIONAL_TEXT:
            if f in attrs and (attrs[f] is None or str(attrs[f]).strip() == ""):
                attrs[f] = None

        if "name" in attrs and len(attrs["name"].strip()) < 2:
            raise serializers.ValidationError({"name": "Название компании слишком короткое."})
        return attrs


class CustomRoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomRole
        fields = ["id", "name", "company"]
        read_only_fields = ["id", "company"]


class CompanySubscriptionSerializer(serializers.ModelSerializer):
    extend_months = serializers.IntegerField(
        write_only=True,
        required=False,
        min_value=1,
        help_text="На сколько месяцев продлить подписку (для кнопки 'Продлить на месяц' = 1).",
    )

    class Meta:
        model = Company
        fields = ["start_date", "end_date", "extend_months"]

    def update(self, instance, validated_data):
        extend_months = validated_data.pop("extend_months", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if extend_months:
            now = timezone.now()
            base_date = instance.end_date or now
            if base_date < now:
                base_date = now
            instance.end_date = base_date + timedelta(days=30 * extend_months)

        instance.save()
        return instance


class UserUiPreferencesSerializer(serializers.ModelSerializer):
    sidebar_auto_close = serializers.BooleanField(
        required=False,
        error_messages={"invalid": "Must be a valid boolean."}
    )
    cashier_desktop_download = serializers.BooleanField(
        required=False,
        error_messages={"invalid": "Must be a valid boolean."}
    )
    theme_mode = serializers.ChoiceField(
        choices=["light", "dark"],
        required=False,
        error_messages={"invalid_choice": "Допустимы light или dark"}
    )
    theme_dark_palette = serializers.ChoiceField(
        choices=["soft", "classic"],
        required=False,
        error_messages={"invalid_choice": "Допустимы soft или classic"}
    )

    class Meta:
        model = UserUiPreferences
        fields = (
            "sidebar_auto_close",
            "cashier_desktop_download",
            "theme_mode",
            "theme_dark_palette",
        )

