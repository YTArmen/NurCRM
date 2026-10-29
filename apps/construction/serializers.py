from decimal import Decimal

from core.exceptions import ApiError  # Б-02
from django.db.models import Q, Sum
from rest_framework import serializers
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.contrib.auth import get_user_model

from apps.construction.models import Cashbox, CashFlow, CashFlowCategory, CashShift
from apps.users.models import Branch

from apps.construction.utils import (
    get_company_from_user as _get_company_from_user,
    is_owner_like as _is_owner_like,
    fixed_branch_from_user as _fixed_branch_from_user,
    get_active_branch,
)

User = get_user_model()


def _resolve_branch_for_request(request):
    """Обертка для get_active_branch для совместимости."""
    return get_active_branch(request)


# ─────────────────────────────────────────────────────────────
# base serializer mixin
# ─────────────────────────────────────────────────────────────
class CompanyBranchReadOnlyMixin(serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    def _auto_branch(self):
        request = self.context.get("request")
        if not request:
            return None
        user = getattr(request, "user", None)
        company = _get_company_from_user(user)
        if not company:
            return None
        return _resolve_branch_for_request(request)

    def create(self, validated_data):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None

        if user:
            company = _get_company_from_user(user)
            if company is not None:
                validated_data["company"] = company

            br = self._auto_branch()
            if br is not None:
                validated_data["branch"] = br

        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None

        if user:
            company = _get_company_from_user(user)
            if company is not None:
                validated_data["company"] = company

            br = self._auto_branch()
            if br is not None:
                validated_data["branch"] = br

        return super().update(instance, validated_data)


# ─────────────────────────────────────────────────────────────
# CashShift
# ─────────────────────────────────────────────────────────────
class CashShiftListSerializer(serializers.ModelSerializer):
    cashbox_name = serializers.SerializerMethodField()
    cashier_display = serializers.SerializerMethodField()

    expected_cash = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    drawer_expected_cash = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    ledger_expected_cash = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    non_drawer_expenses_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    cash_diff = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    payment_breakdown = serializers.SerializerMethodField()
    cashier_breakdown = serializers.SerializerMethodField()
    resolved_cashbox_id = serializers.SerializerMethodField()
    resolved_cashbox_name = serializers.SerializerMethodField()
    closed_by = serializers.SerializerMethodField()
    close_reason = serializers.SerializerMethodField()
    auto_close_at = serializers.SerializerMethodField()

    class Meta:
        model = CashShift
        fields = [
            "id",
            "company",
            "branch",
            "cashbox",
            "cashbox_name",
            "resolved_cashbox_id",
            "resolved_cashbox_name",
            "cashier",
            "cashier_display",
            "status",
            "opened_at",
            "closed_at",
            "opening_cash",
            "closing_cash",
            "income_total",
            "expense_total",
            "sales_count",
            "sales_total",
            "cash_sales_total",
            "noncash_sales_total",
            "expected_cash",
            "drawer_expected_cash",
            "ledger_expected_cash",
            "non_drawer_expenses_total",
            "cash_diff",
            "payment_breakdown",
            "cashier_breakdown",
            "closed_by",
            "close_reason",
            "auto_close_at",
        ]
        read_only_fields = fields

    # ТЗ Н-09: без миграции — «кто закрыл» выводим из close_reason модели.
    _SYSTEM_REASONS = ("auto_timeout_24h", "employee_deleted")

    def get_closed_by(self, obj):
        if obj.status != CashShift.Status.CLOSED:
            return None
        return "system" if (obj.close_reason or "") in self._SYSTEM_REASONS else "user"

    def get_close_reason(self, obj):
        if obj.status != CashShift.Status.CLOSED:
            return None
        r = obj.close_reason or ""
        if r == "auto_timeout_24h":
            return "auto_24h"
        return r or "manual"

    def get_auto_close_at(self, obj):
        from apps.construction.services_shifts import shift_auto_close_at

        dt = shift_auto_close_at(obj)
        return dt.isoformat() if dt else None

    def get_cashbox_name(self, obj):
        if obj.cashbox and obj.cashbox.branch:
            return f"Касса филиала {obj.cashbox.branch.name}"
        return getattr(obj.cashbox, "name", None) or "Касса"

    def get_resolved_cashbox_id(self, obj):
        return str(obj.cashbox_id) if obj.cashbox_id else None

    def get_resolved_cashbox_name(self, obj):
        return self.get_cashbox_name(obj)

    def get_cashier_display(self, obj):
        u = obj.cashier
        if not u:
            return None
        return (
            getattr(u, "get_full_name", lambda: "")()
            or getattr(u, "email", None)
            or getattr(u, "username", None)
        )

    def get_payment_breakdown(self, obj):
        return obj.calc_payment_breakdown() or []

    def get_cashier_breakdown(self, obj):
        return obj.calc_cashier_breakdown() or []

    def to_representation(self, obj):
        data = super().to_representation(obj)

        if obj.status == CashShift.Status.OPEN:
            t = obj.calc_live_totals()

            data["income_total"] = str(t["income_total"])
            data["expense_total"] = str(t["expense_total"])
            data["sales_count"] = int(t["sales_count"])
            data["sales_total"] = str(t["sales_total"])
            data["cash_sales_total"] = str(t["cash_sales_total"])
            data["noncash_sales_total"] = str(t["noncash_sales_total"])

            data["expected_cash"] = str(t["expected_cash"])
            data["drawer_expected_cash"] = str(t["drawer_expected_cash"])
            data["ledger_expected_cash"] = str(t["ledger_expected_cash"])
            data["non_drawer_expenses_total"] = str(t["non_drawer_expenses_total"])
            data["cash_diff"] = "0.00"
        else:
            data["drawer_expected_cash"] = str(obj.drawer_expected_cash)
            data["ledger_expected_cash"] = str(obj.ledger_expected_cash)
            data["non_drawer_expenses_total"] = str(obj.non_drawer_expenses_total)
            data["expected_cash"] = str(obj.expected_cash)
            data["cash_diff"] = str(obj.cash_diff)

        data["resolved_cashbox_id"] = str(obj.cashbox_id) if obj.cashbox_id else None
        data["resolved_cashbox_name"] = self.get_cashbox_name(obj)
        data["payment_breakdown"] = obj.calc_payment_breakdown() or []
        data.update(obj.returns_summary())
        # cashier_breakdown уже посчитан SerializerMethodField в super() —
        # второй вызов здесь дал бы лишний запрос на каждую смену.

        return data


class CashShiftOpenSerializer(serializers.ModelSerializer):
    """
    ✅ Разрешаем несколько OPEN смен на одну кассу.
    ✅ Запрещаем только повторную OPEN смену этому же кассиру на этой кассе.
    ✅ Авто-резолв кассы по branch_id / cashbox_role, если cashbox не передан.
    """
    MAX_OPEN_COMPANY_SHIFTS = 20

    cashier = serializers.PrimaryKeyRelatedField(required=False, allow_null=True, queryset=User.objects.none())
    cashbox = serializers.PrimaryKeyRelatedField(required=False, allow_null=True, queryset=Cashbox.objects.none())
    branch_id = serializers.UUIDField(required=False, allow_null=True, write_only=True)
    cashbox_role = serializers.CharField(required=False, allow_blank=True, allow_null=True, write_only=True)

    class Meta:
        model = CashShift
        fields = ["id", "cashbox", "cashier", "opening_cash", "branch_id", "cashbox_role"]
        read_only_fields = ["id"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        company = _get_company_from_user(user)

        # cashiers
        if company:
            self.fields["cashier"].queryset = User.objects.filter(Q(company=company) | Q(owned_company=company))
        else:
            self.fields["cashier"].queryset = User.objects.none()

        # cashboxes: кассы компании
        if company:
            target_branch = _resolve_branch_for_request(request) if request else None
            qs = Cashbox.objects.filter(company=company)

            if target_branch is not None and not _is_owner_like(user):
                qs = qs.filter(Q(branch__isnull=True) | Q(branch=target_branch))

            self.fields["cashbox"].queryset = qs
        else:
            self.fields["cashbox"].queryset = Cashbox.objects.none()

    def validate(self, attrs):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        company = _get_company_from_user(user)

        if not company:
            raise serializers.ValidationError("Нет компании у пользователя.")

        # cashier
        chosen_cashier = attrs.get("cashier") or None
        if chosen_cashier is None:
            if not user:
                raise serializers.ValidationError({"cashier": "Нужен кассир."})
            attrs["cashier"] = user
        else:
            if not _is_owner_like(user) and chosen_cashier.id != getattr(user, "id", None):
                raise serializers.ValidationError({"cashier": "Нельзя открыть смену на другого кассира."})

        cashier = attrs["cashier"]

        # resolve cashbox if not provided
        cashbox = attrs.get("cashbox")
        if not cashbox:
            from apps.construction.auto_cashflow import resolve_cashbox
            ctx = {
                "branch_id": attrs.get("branch_id") or (request.data.get("branch_id") if request else None),
                "cashbox_role": attrs.get("cashbox_role") or (request.data.get("cashbox_role") if request else None),
                "user": cashier,
            }
            cashbox = resolve_cashbox(company=company, context=ctx, source_kind=None, require_cashbox=False)
            if not cashbox:
                raise serializers.ValidationError({"cashbox": "Не удалось определить кассу для смены."})
            attrs["cashbox"] = cashbox

        if cashbox.company_id != company.id:
            raise serializers.ValidationError({"cashbox": "Касса другой компании."})

        # ✅ теперь проверяем только "есть ли уже OPEN смена этого кассира на этой кассе"
        existing = (
            CashShift.objects
            .select_for_update()
            .filter(company=company, cashier=cashier, status=CashShift.Status.OPEN)
            .order_by("-opened_at")
            .first()
        )
        if existing:
            attrs["_existing_shift"] = existing
            return attrs

        open_shifts_count = (
            CashShift.objects
            .filter(company=company, status=CashShift.Status.OPEN)
            .count()
        )
        if open_shifts_count >= self.MAX_OPEN_COMPANY_SHIFTS:
            raise serializers.ValidationError(
                {"shift": f"В компании уже открыто {self.MAX_OPEN_COMPANY_SHIFTS} смены. Закройте одну смену перед открытием новой."}
            )

        return attrs

    def create(self, validated_data):
        validated_data.pop("branch_id", None)
        validated_data.pop("cashbox_role", None)
        existing = validated_data.pop("_existing_shift", None)
        if existing:
            return existing

        cashbox = validated_data["cashbox"]
        cashier = validated_data["cashier"]

        return CashShift.objects.create(
            company=cashbox.company,
            branch=cashbox.branch,
            cashbox=cashbox,
            cashier=cashier,
            opening_cash=validated_data.get("opening_cash") or Decimal("0.00"),
            status=CashShift.Status.OPEN,
        )


class CashShiftCloseSerializer(serializers.Serializer):
    closing_cash = serializers.DecimalField(max_digits=12, decimal_places=2)

    def save(self, shift: CashShift):
        shift.close(self.validated_data["closing_cash"])
        return shift


# ─────────────────────────────────────────────────────────────
# Cashbox
# ─────────────────────────────────────────────────────────────
class CashFlowInsideCashboxSerializer(serializers.ModelSerializer):
    cashier_display = serializers.SerializerMethodField()
    category_title = serializers.CharField(source="category.title", read_only=True, default="")

    def get_cashier_display(self, obj):
        u = getattr(obj, "cashier", None)
        if not u:
            return None
        return (
            getattr(u, "get_full_name", lambda: "")()
            or getattr(u, "email", None)
            or getattr(u, "username", None)
        )

    class Meta:
        model = CashFlow
        fields = [
            "id",
            "type",
            "name",
            "amount",
            "status",
            "created_at",
            "source_cashbox_flow_id",
            "source_business_operation_id",
            "shift",
            "cashier",
            "cashier_display",
            "category",
            "category_title",
        ]
        read_only_fields = fields


def _add_drawer_cash(data, cashbox):
    """ТЗ Н-06: наличные в ящике (только pos_branch/pos_main), у остальных касс — null."""
    info = cashbox.drawer_cash_info()
    data["drawer_cash"] = str(info["drawer_cash"]) if info and info["drawer_cash"] is not None else None
    data["drawer_cash_at"] = info["drawer_cash_at"].isoformat() if info else None
    data["last_closed_shift"] = info["last_closed_shift"] if info else None
    return data


class CashboxWithFlowsSerializer(CompanyBranchReadOnlyMixin):
    cashflows = CashFlowInsideCashboxSerializer(source="flows", many=True, read_only=True)
    is_consumption = serializers.BooleanField(read_only=True)
    role = serializers.CharField(read_only=True)
    balance = serializers.SerializerMethodField()
    current_balance = serializers.SerializerMethodField()
    is_active = serializers.BooleanField(read_only=True)
    archived_at = serializers.DateTimeField(read_only=True, allow_null=True)
    archived_by = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)
    merged_into = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)

    class Meta:
        model = Cashbox
        fields = [
            "id", "company", "branch", "name", "role", "is_consumption",
            "balance", "current_balance", "is_active", "archived_at", "archived_by", "merged_into", "cashflows",
        ]
        read_only_fields = [
            "id", "company", "branch", "role", "cashflows", "is_consumption",
            "balance", "current_balance", "is_active", "archived_at", "archived_by", "merged_into",
        ]

    def get_balance(self, obj):
        inc = obj.flows.filter(status="approved", type="income", request_kind__isnull=True).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        exp = obj.flows.filter(status="approved", type="expense", request_kind__isnull=True).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        return f"{(inc - exp):.2f}"

    def get_current_balance(self, obj):
        return self.get_balance(obj)

    def to_representation(self, instance):
        return _add_drawer_cash(super().to_representation(instance), instance)



class CashboxSerializer(CompanyBranchReadOnlyMixin):
    # ТЗ 11.4: филиал кассы отдаём id (null — общая касса); у ReadOnlyField(source="branch.id") ключ пропадал при null
    branch = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)
    analytics = serializers.SerializerMethodField()
    is_consumption = serializers.BooleanField(required=False, default=False)
    role = serializers.ChoiceField(choices=Cashbox.CashboxRole.choices, required=False, allow_null=True)
    balance = serializers.SerializerMethodField()
    current_balance = serializers.SerializerMethodField()
    currency = serializers.CharField(default="KGS", read_only=True)
    is_active = serializers.BooleanField(default=True, read_only=True)
    archived_at = serializers.DateTimeField(read_only=True, allow_null=True)
    archived_by = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)
    merged_into = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)

    class Meta:
        model = Cashbox
        fields = [
            "id", "company", "branch", "name", "role",
            "is_consumption", "balance", "current_balance", "currency", "is_active",
            "archived_at", "archived_by", "merged_into",
            "analytics",
        ]
        read_only_fields = [
            "id", "company", "branch", "analytics",
            "balance", "current_balance", "currency", "is_active",
            "archived_at", "archived_by", "merged_into",
        ]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        role = attrs.get("role")
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        if role == Cashbox.CashboxRole.POS_BRANCH and not branch:
            raise serializers.ValidationError({"role": "Для роли pos_branch требуется branch_id."})
        if attrs.get("is_consumption") and not attrs.get("role"):
            attrs["role"] = Cashbox.CashboxRole.EXPENSE_VARIABLE
        return attrs

    def get_balance(self, obj):
        amap = self.context.get("analytics_map")
        if amap and str(obj.id) in amap:
            a = amap[str(obj.id)]
            inc = Decimal(str(a.get("income_total") or "0.00"))
            exp = Decimal(str(a.get("expense_total") or "0.00"))
            return f"{(inc - exp):.2f}"
        inc = obj.flows.filter(status="approved", type="income", request_kind__isnull=True).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        exp = obj.flows.filter(status="approved", type="expense", request_kind__isnull=True).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        return f"{(inc - exp):.2f}"

    def get_current_balance(self, obj):
        return self.get_balance(obj)

    def to_representation(self, instance):
        data = super().to_representation(instance)

        # если name пустой — проверяем, первая ли это касса компании
        if not data.get("name"):
            first_cashbox_id = (
                Cashbox.objects
                .filter(company=instance.company)
                .order_by("created_at")
                .values_list("id", flat=True)
                .first()
            )

            if instance.id == first_cashbox_id:
                data["name"] = "Основная касса компании"

        if not data.get("role"):
            data["role"] = instance.get_inferred_role()

        _add_drawer_cash(data, instance)

        return data
    def get_analytics(self, obj):
        amap = self.context.get("analytics_map")
        if amap is not None:
            a = amap.get(str(obj.id))
            if a is None:
                return {
                    "income_total": "0.00",
                    "expense_total": "0.00",
                    "sales_count": 0,
                    "sales_total": "0.00",
                    "cash_sales_total": "0.00",
                    "noncash_sales_total": "0.00",
                    "open_shift_expected_cash": None,
                }

            def _d(v):
                return str(v) if isinstance(v, Decimal) else v

            return {
                "income_total": _d(a.get("income_total")),
                "expense_total": _d(a.get("expense_total")),
                "sales_count": int(a.get("sales_count") or 0),
                "sales_total": _d(a.get("sales_total")),
                "cash_sales_total": _d(a.get("cash_sales_total")),
                "noncash_sales_total": _d(a.get("noncash_sales_total")),
                "open_shift_expected_cash": _d(a.get("open_shift_expected_cash")),
            }

        return obj.get_summary()


# ─────────────────────────────────────────────────────────────
# CashFlowCategory
# ─────────────────────────────────────────────────────────────
class CashFlowCategorySerializer(CompanyBranchReadOnlyMixin):
    """Категория опционально привязана к филиалу: branch=null — на всю компанию."""

    branch = serializers.PrimaryKeyRelatedField(
        queryset=Branch.objects.all(), required=False, allow_null=True
    )

    is_default = serializers.SerializerMethodField()

    def get_is_default(self, obj):
        return bool(obj.code)

    def to_internal_value(self, data):
        # ТЗ «Бухгалтер»: фронт шлёт name, исторически поле называется title.
        if hasattr(data, "get") and "name" in data and "title" not in data:
            data = data.copy()
            data["title"] = data["name"]
        return super().to_internal_value(data)

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        rep["name"] = rep.get("title")
        return rep

    class Meta:
        model = CashFlowCategory
        fields = ["id", "company", "branch", "title", "type", "archived", "is_default", "slug", "is_system", "created_at"]
        read_only_fields = ["id", "company", "created_at", "slug", "is_system"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        company = _get_company_from_user(user) if user else None
        if company and "branch" in self.fields:
            self.fields["branch"].queryset = Branch.objects.filter(company=company)

    def validate_title(self, value):
        v = (value or "").strip()
        if not v:
            raise serializers.ValidationError("Укажите название категории.")
        return v

    def validate(self, attrs):
        br = attrs.get("branch")
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        company = _get_company_from_user(user) if user else None
        if br is not None and company and br.company_id != company.id:
            raise serializers.ValidationError({"branch": "Филиал другой компании."})
        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        if user:
            company = _get_company_from_user(user)
            if company is not None:
                validated_data["company"] = company
        if "branch" not in validated_data:
            validated_data["branch"] = self._auto_branch()
        # ТЗ Н-19: «Зарплата/Аренда/Налоги/Коммунальные» (без регистра) получают системный slug.
        from apps.construction.categories import system_slug_for

        slug = system_slug_for(validated_data.get("title"))
        if slug:
            validated_data["slug"] = slug
            validated_data["is_system"] = True
        return serializers.ModelSerializer.create(self, validated_data)

    def update(self, instance, validated_data):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        if user:
            company = _get_company_from_user(user)
            if company is not None:
                validated_data["company"] = company
        return serializers.ModelSerializer.update(self, instance, validated_data)


class TargetFlowBriefSerializer(serializers.ModelSerializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, coerce_to_string=True)

    class Meta:
        model = CashFlow
        fields = ["id", "name", "amount", "type", "created_at"]


DRAWER_SOURCE_KINDS = (
    CashFlow.SourceKind.SHIFT_DRAWER_OUTFLOW,
    CashFlow.SourceKind.SHIFT_DRAWER_INFLOW,
    CashFlow.SourceKind.SHIFT_DRAWER_COLLECTION,  # ТЗ Н-19
)
# ТЗ Н-19: расходы «по назначению» — всегда расход, ящик не трогают.
EXPENSE_ONLY_KINDS = (
    CashFlow.SourceKind.SHIFT_DRAWER_OUTFLOW,
    CashFlow.SourceKind.SHIFT_DRAWER_COLLECTION,
)
PURPOSE_SOURCE_KINDS = (
    CashFlow.SourceKind.SALARY_PAYOUT,
    CashFlow.SourceKind.RENT_PAYMENT,
    CashFlow.SourceKind.SUPPLIER_PAYMENT,
)


class CashFlowSerializer(CompanyBranchReadOnlyMixin):
    cashbox = serializers.PrimaryKeyRelatedField(queryset=Cashbox.objects.all())
    cashbox_name = serializers.SerializerMethodField()

    shift = serializers.PrimaryKeyRelatedField(
        queryset=CashShift.objects.all(),
        required=False,
        allow_null=True,
    )
    category = serializers.PrimaryKeyRelatedField(
        queryset=CashFlowCategory.objects.all(),
        required=False,
        allow_null=True,
    )
    category_title = serializers.CharField(source="category.title", read_only=True, default="")
    # ТЗ «Бухгалтер»: category_id — алиас category (на запись и чтение)
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=CashFlowCategory.objects.all(), source="category", required=False, allow_null=True
    )

    cashier = serializers.ReadOnlyField(source="cashier.id")
    cashier_display = serializers.SerializerMethodField()
    # Алиас для аналитики Производства (фронт читает user_name|created_by_name|...).
    user_name = serializers.SerializerMethodField()

    request_kind = serializers.CharField(read_only=True, allow_null=True)
    target_flow = TargetFlowBriefSerializer(read_only=True, allow_null=True)
    proposed = serializers.JSONField(read_only=True)
    reason = serializers.CharField(read_only=True)
    requested_by = serializers.SerializerMethodField()
    resolved_by = serializers.SerializerMethodField()
    resolved_at = serializers.DateTimeField(read_only=True, allow_null=True)
    idempotency_key = serializers.CharField(read_only=True, allow_null=True)

    class Meta:
        model = CashFlow
        fields = [
            "id",
            "company",
            "branch",
            "cashbox",
            "cashbox_name",
            "type",
            "name",
            "amount",
            "created_at",
            "status",
            "source_cashbox_flow_id",
            "source_business_operation_id",
            "source_kind",
            "source_id",
            "shift",
            "category", "category_id",
            "checked_at", "checked_by", "accountant_note",
            "category_title",
            "cashier",
            "cashier_display",
            "user_name",
            "request_kind",
            "target_flow",
            "proposed",
            "reason",
            "requested_by",
            "resolved_by",
            "resolved_at",
            "idempotency_key",
            "payment_method",
            "kind",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "cashbox_name",
            "company",
            "branch",
            "cashier",
            "cashier_display",
            "user_name",
            "category_title",
            "request_kind",
            "target_flow",
            "proposed",
            "reason",
            "requested_by",
            "resolved_by",
            "resolved_at",
            "idempotency_key",
            "checked_at", "checked_by", "accountant_note",
        ]

    def validate_kind(self, value):
        # ТЗ 29.10 Б-100: допустимо "" или "startup"; "startup" ставит только владелец/администратор
        value = (value or "").strip()
        if value not in ("", "startup"):
            raise serializers.ValidationError('Допустимо: "" или "startup".')
        if value == "startup":
            from apps.utils import _is_owner_like
            req = self.context.get("request")
            if req is None or not _is_owner_like(req.user):
                raise serializers.ValidationError("Стартовые расходы — только владелец или администратор.")
        return value

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        request = self.context.get("request")
        if not request:
            return

        user = getattr(request, "user", None)
        company = _get_company_from_user(user)
        if not company:
            self.fields["cashbox"].queryset = Cashbox.objects.none()
            self.fields["shift"].queryset = CashShift.objects.none()
            return

        target_branch = self._auto_branch()

        cb_qs = Cashbox.objects.filter(company=company)
        if target_branch is not None:
            cb_qs = cb_qs.filter(Q(branch__isnull=True) | Q(branch=target_branch))
        self.fields["cashbox"].queryset = cb_qs

        # Б-02: чужую смену не прячем в queryset (иначе общий 400 без кода): validate() даёт 400 shift_not_owned.
        sh_qs = CashShift.objects.filter(company=company)
        self.fields["shift"].queryset = sh_qs

        cat_qs = CashFlowCategory.objects.filter(company=company)
        if target_branch is not None:
            cat_qs = cat_qs.filter(Q(branch__isnull=True) | Q(branch=target_branch))
        self.fields["category"].queryset = cat_qs
        self.fields["category_id"].queryset = cat_qs

    def get_cashbox_name(self, obj):
        if obj.cashbox and obj.cashbox.branch:
            return f"Касса филиала {obj.cashbox.branch.name}"
        return getattr(obj.cashbox, "name", None) or f"Касса компании {obj.company.name}"

    def get_cashier_display(self, obj):
        u = getattr(obj, "cashier", None)
        if not u:
            return None
        return (
            getattr(u, "get_full_name", lambda: "")()
            or getattr(u, "email", None)
            or getattr(u, "username", None)
        )

    def get_user_name(self, obj):
        # Тот же автор, что и cashier_display — отдельный ключ для аналитики Производства.
        return self.get_cashier_display(obj)

    def get_requested_by(self, obj):
        if not obj.requested_by_id:
            return None
        u = getattr(obj, "requested_by", None)
        if not u:
            return None
        name = (getattr(u, "get_full_name", lambda: "")() or getattr(u, "email", None) or getattr(u, "username", "") or str(u.id))
        return {"id": str(u.id), "name": name.strip()}

    def get_resolved_by(self, obj):
        if not obj.resolved_by_id:
            return None
        u = getattr(obj, "resolved_by", None)
        if not u:
            return None
        name = (getattr(u, "get_full_name", lambda: "")() or getattr(u, "email", None) or getattr(u, "username", "") or str(u.id))
        return {"id": str(u.id), "name": name.strip()}

    def validate(self, attrs):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None

        cashbox = attrs.get("cashbox") or getattr(self.instance, "cashbox", None)
        if cashbox and not getattr(cashbox, "is_active", True):
            raise DRFValidationError({"detail": "Касса находится в архиве.", "code": "cashbox_inactive"})

        category = attrs.get("category") if "category" in attrs else getattr(self.instance, "category", None)
        if self.instance is None and category is None and "category" not in attrs:
            # По умолчанию — статья «Прочие» (ТЗ «Бухгалтер», п. 3)
            from apps.construction.categories import get_default_category

            category = get_default_category(_get_company_from_user(user) if user else None, "other")
            if category is not None:
                attrs["category"] = category
        if category is not None and "category" in attrs and getattr(category, "archived", False) and (
            self.instance is None or self.instance.category_id != category.id
        ):
            raise serializers.ValidationError({"category": "Статья в архиве."})
        if category is not None:
            company = _get_company_from_user(user) if user else None
            if company and category.company_id != company.id:
                raise serializers.ValidationError({"category": "Категория другой компании."})
            if cashbox:
                cb_br = cashbox.branch_id
                cat_br = category.branch_id
                if cat_br is not None and cat_br != cb_br:
                    raise serializers.ValidationError(
                        {"category": "Категория привязана к другому филиалу. Выберите общую категорию или категорию этого филиала."}
                    )

        # amount: в БД стоит CheckConstraint amount__gt=0.
        # На фронте часто отправляют отрицательное значение для "расхода" —
        # нормализуем и сохраняем всегда положительное значение.
        if "amount" in attrs:
            amount = attrs.get("amount")
            if amount is None:
                raise serializers.ValidationError({"amount": "Обязательное поле."})
            try:
                amount = abs(amount)
            except Exception:
                raise serializers.ValidationError({"amount": "Некорректное значение суммы."})
            if amount <= 0:
                raise serializers.ValidationError({"amount": "Сумма должна быть больше 0."})
            attrs["amount"] = amount

        # важно: различаем "shift не передали" и "shift=None"
        shift_provided = "shift" in attrs
        shift = attrs.get("shift") if shift_provided else getattr(self.instance, "shift", None)

        if request and cashbox:
            company = _get_company_from_user(user)
            if cashbox.company_id != getattr(company, "id", None):
                raise serializers.ValidationError("Касса должна принадлежать вашей компании.")

        target_branch = self._auto_branch()
        if cashbox and target_branch is not None and cashbox.branch_id not in (None, getattr(target_branch, "id", None)):
            raise serializers.ValidationError("Касса принадлежит другому филиалу.")

        # ✅ строгие проверки ТОЛЬКО если shift передали и он не None
        if shift_provided and shift is not None:
            if cashbox and shift.cashbox_id != cashbox.id:
                raise serializers.ValidationError({"shift": "Смена относится к другой кассе."})

            if shift.status != CashShift.Status.OPEN:
                # Б-02: старая касса повторяет очередь на любой отказ кроме 400 — даём 400 с понятным кодом.
                _m = "Нельзя делать движение по закрытой смене."
                raise ApiError("shift_closed", _m, top={"shift": _m})

            if user and (not _is_owner_like(user)) and shift.cashier_id != user.id:
                _m = "Это не ваша смена."
                raise ApiError("shift_not_owned", _m, top={"shift": _m})

        # «Оплачено из кассы (ящика) смены» — shift-drawer-funding-purchases-backend.md.
        # Обычная ручная запись ящик не трогает: calc_live_totals учитывает только
        # движения, привязанные к смене И помеченные affects_shift_drawer. Оба
        # признака проставляем по явному source_kind, чтобы не менять поведение
        # остальных ручных операций кассы. Сам флаг выставляет CashFlow.save().
        source_kind = attrs.get("source_kind") or getattr(self.instance, "source_kind", None)
        if source_kind in DRAWER_SOURCE_KINDS:
            flow_type = attrs.get("type") or getattr(self.instance, "type", None)
            if source_kind in EXPENSE_ONLY_KINDS and flow_type != CashFlow.Type.EXPENSE:
                raise serializers.ValidationError(
                    {"type": "Списание из кассы смены возможно только расходом."}
                )
            if source_kind == CashFlow.SourceKind.SHIFT_DRAWER_INFLOW and flow_type != CashFlow.Type.INCOME:
                raise serializers.ValidationError(
                    {"type": "Внесение в кассу смены возможно только приходом."}
                )
            if not cashbox:
                raise serializers.ValidationError({"cashbox": "Укажите кассу смены."})

            if shift is None:
                open_qs = CashShift.objects.filter(cashbox=cashbox, status=CashShift.Status.OPEN)
                if user and not _is_owner_like(user):
                    open_qs = open_qs.filter(cashier=user)
                candidates = list(open_qs.order_by("-opened_at")[:2])
                if not candidates:
                    raise serializers.ValidationError(
                        {"shift": "На этой кассе нет открытой смены — операция по ящику невозможна."}
                    )
                if len(candidates) > 1:
                    # Общая касса на несколько смен: угадывать, из чьего ящика
                    # ушли деньги, нельзя — пусть вызывающий укажет смену явно.
                    raise serializers.ValidationError(
                        {"shift": "На кассе открыто несколько смен — укажите смену явно."}
                    )
                attrs["shift"] = candidates[0]

        if user and not _is_owner_like(user):
            self._validate_employee_rights(attrs, user)

        # ✅ если shift не передали или shift=None — это “общий режим”, разрешаем
        return attrs

    EMPLOYEE_SOURCE_KINDS = (None, "", CashFlow.SourceKind.MANUAL) + DRAWER_SOURCE_KINDS + PURPOSE_SOURCE_KINDS

    def _validate_employee_rights(self, attrs, user):
        """Ограничения для не-владельца (кассира): одобрение, авто-движения, закрытые смены."""
        if "source_kind" in attrs and attrs.get("source_kind") not in self.EMPLOYEE_SOURCE_KINDS:
            raise serializers.ValidationError({"source_kind": "Этот тип источника ставит только система."})

        inst = self.instance
        if inst is None:
            return  # статус при создании выставляет create()

        new_status = attrs.get("status", inst.status)
        if new_status != inst.status:
            if inst.status == CashFlow.Status.REJECTED:
                raise serializers.ValidationError({"status": "Отклонённое движение нельзя вернуть."})
            company = _get_company_from_user(user)
            if new_status == CashFlow.Status.APPROVED and getattr(company, "cashflow_requests_enabled", False):
                raise serializers.ValidationError({"status": "Одобрять движения может только владелец или администратор."})

        money_changed = any(
            k in attrs and attrs[k] != getattr(inst, k) for k in ("amount", "type", "cashbox")
        )
        if money_changed:
            if inst.shift_id and inst.shift.status != CashShift.Status.OPEN:
                raise serializers.ValidationError({"detail": "Движение закрытой смены менять нельзя — оформите заявку на правку."})
            if inst.source_kind not in self.EMPLOYEE_SOURCE_KINDS:
                raise serializers.ValidationError({"detail": "Автоматическое движение менять нельзя — оформите заявку на правку."})

    def create(self, validated_data):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None

        # Повторная отправка внесения/изъятия кассой (тот же source_id) — не дублируем.
        source_kind = validated_data.get("source_kind")
        source_id = (validated_data.get("source_id") or "").strip()
        if source_kind in DRAWER_SOURCE_KINDS and source_id:
            cashbox = validated_data.get("cashbox")
            existing = (
                CashFlow.objects.filter(
                    company_id=getattr(cashbox, "company_id", None),
                    source_kind=source_kind,
                    source_id=source_id,
                    request_kind__isnull=True,
                )
                .order_by("created_at")
                .first()
            )
            if existing is not None:
                return existing

        # ✅ НИКАКОГО авто-поиска смены. Если shift нет — создаём общий cashflow.
        if user and "cashier" not in validated_data:
            validated_data["cashier"] = user

        company = validated_data.get("company") or (getattr(user, "company", None) if user else None)
        req_enabled = bool(getattr(company, "cashflow_requests_enabled", False))

        if not req_enabled:
            validated_data["status"] = CashFlow.Status.APPROVED
        elif _is_owner_like(user):
            validated_data.setdefault("status", CashFlow.Status.APPROVED)
        else:
            # Включено одобрение: сотрудник не может сам провести движение (status=approved).
            validated_data["status"] = CashFlow.Status.PENDING

        return super().create(validated_data)

class CashFlowBulkStatusItemSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=CashFlow.Status.choices)


class CashFlowBulkStatusSerializer(serializers.Serializer):
    items = CashFlowBulkStatusItemSerializer(many=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("Пустой список.")
        if len(items) > 50000:
            raise serializers.ValidationError("Слишком много. Максимум 50 000 за раз.")
        return items


class CashFlowEditRequestSerializer(serializers.Serializer):
    proposed = serializers.DictField(required=True)
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128, default="")

    def validate_proposed(self, val):
        if not isinstance(val, dict) or not val:
            raise serializers.ValidationError("Параметр 'proposed' должен быть непустым объектом.")
        if "amount" in val:
            try:
                amt = Decimal(str(val["amount"]))
                if amt <= 0:
                    raise serializers.ValidationError("Сумма в 'proposed.amount' должна быть больше нуля.")
            except (ValueError, TypeError):
                raise serializers.ValidationError("Некорректный формат суммы в 'proposed.amount'.")
        else:
            raise serializers.ValidationError("Поле 'proposed.amount' обязательно.")

        if "type" in val:
            t = str(val["type"]).strip().lower()
            if t not in (CashFlow.Type.INCOME, CashFlow.Type.EXPENSE):
                raise serializers.ValidationError(f"Недопустимый тип '{t}'. Ожидается 'income' или 'expense'.")
        return val


class CashFlowCancelRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128, default="")
