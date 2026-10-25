import re

from rest_framework import serializers

from apps.users.models import Branch

from .models import ArendaSettings, Lease, Tenant, Unit, UnitType
from .constants import SECTIONS


class _CompanyScoped:
    """Хелпер: company берётся из context['ctx']."""

    def _company(self):
        return self.context["ctx"].company


class UnitTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitType
        fields = ["id", "name", "is_system"]
        read_only_fields = ["id", "is_system"]
        validators = []


class UnitSerializer(_CompanyScoped, serializers.ModelSerializer):
    unit_type_name = serializers.CharField(source="unit_type.name", read_only=True, default=None)
    rent_monthly_calc = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    overdue_days = serializers.SerializerMethodField()
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all())
    unit_type = serializers.PrimaryKeyRelatedField(queryset=UnitType.objects.all(), required=False, allow_null=True)
    area = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    rate = serializers.IntegerField(min_value=0)

    class Meta:
        model = Unit
        fields = ["id", "branch", "floor", "number", "unit_type", "unit_type_name", "area", "rate",
                  "rent_monthly_calc", "reserved_until", "reserved_for", "status", "overdue_days"]
        read_only_fields = ["id"]
        validators = []

    def _st(self, obj):
        return (self.context.get("state_map") or {}).get(obj.id, {})

    def get_status(self, obj):
        return self._st(obj).get("status", "free")

    def get_overdue_days(self, obj):
        return self._st(obj).get("overdue_days", 0)

    def get_rent_monthly_calc(self, obj):
        from .services.money import calc_rent
        return calc_rent(obj.area, obj.rate)

    def validate_branch(self, v):
        if v.company_id != self._company().id:
            raise serializers.ValidationError("Филиал не найден.")
        return v

    def validate_unit_type(self, v):
        if v is not None and v.company_id != self._company().id:
            raise serializers.ValidationError("Тип не найден.")
        return v

    def validate_number(self, v):
        v = (v or "").strip()
        if not v:
            raise serializers.ValidationError("Укажите номер.")
        return v

    def validate(self, attrs):
        if self.instance is not None and "branch" in attrs and attrs["branch"].id != self.instance.branch_id:
            raise serializers.ValidationError({"branch": "Филиал помещения менять нельзя."})
        return attrs


class TenantInSerializer(serializers.ModelSerializer):
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)

    class Meta:
        model = Tenant
        fields = ["name", "form", "inn", "contact_person", "passport", "phone", "email", "activity", "branch"]

    def validate_branch(self, v):
        if v is not None and v.company_id != self.context["ctx"].company.id:
            raise serializers.ValidationError("Филиал не найден.")
        return v


class LeaseInSerializer(serializers.Serializer):
    tenant = serializers.PrimaryKeyRelatedField(queryset=Tenant.objects.all())
    units = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all(), many=True, allow_empty=False)
    start_date = serializers.DateField()
    end_date = serializers.DateField(required=False, allow_null=True)
    rent_monthly = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    pay_day = serializers.IntegerField(required=False, min_value=1, max_value=31, default=10)
    deposit_months = serializers.IntegerField(required=False, min_value=0, default=0)
    deposit_amount = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    penalty_rate = serializers.DecimalField(max_digits=6, decimal_places=3, required=False, min_value=0)
    indexation_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, min_value=0, default=0)
    utilities = serializers.CharField(required=False, allow_blank=True, default="")
    status = serializers.ChoiceField(choices=["draft", "active"], required=False, default="active")

    def validate(self, attrs):
        company = self.context["ctx"].company
        t = attrs.get("tenant")
        if t is not None and t.company_id != company.id:
            raise serializers.ValidationError({"tenant": "Арендатор не найден."})
        units = attrs.get("units") or []
        if any(u.company_id != company.id for u in units):
            raise serializers.ValidationError({"units": "Помещение не найдено."})
        if any(u.archived_at for u in units):
            raise serializers.ValidationError({"units": "Помещение в архиве."})
        if len({u.branch_id for u in units}) > 1:
            raise serializers.ValidationError({"units": "Помещения должны быть из одного филиала."})
        s, e = attrs.get("start_date"), attrs.get("end_date")
        if s and e and e < s:
            raise serializers.ValidationError({"end_date": "Дата окончания раньше начала."})
        return attrs


def _digits(label, n):
    def v(value):
        if value and not re.fullmatch(rf"\d{{{n}}}", value):
            raise serializers.ValidationError(f"{label}: ровно {n} цифр.")
        return value
    return v


class SettingsSerializer(serializers.ModelSerializer):
    inn = serializers.CharField(required=False, allow_blank=True, validators=[_digits("ИНН", 14)])
    bik = serializers.CharField(required=False, allow_blank=True, validators=[_digits("БИК", 6)])
    account = serializers.CharField(required=False, allow_blank=True, validators=[_digits("Счёт", 16)])

    class Meta:
        model = ArendaSettings
        fields = ["org_name", "inn", "okpo", "legal_address", "director", "chief_accountant", "bank", "bik",
                  "account", "phone", "email", "penalty_rate", "payroll_social_employee", "payroll_income_tax",
                  "payroll_social_employer", "advance_percent", "advance_day", "settle_day", "soon_days",
                  "lease_expiring_days", "indexation_notice_days"]


class AccessInSerializer(serializers.Serializer):
    preset = serializers.CharField(required=False, allow_blank=True)
    sections = serializers.ListField(child=serializers.ChoiceField(choices=SECTIONS), required=False)
    all_branches = serializers.BooleanField(required=False)
    branches = serializers.ListField(child=serializers.UUIDField(), required=False)
    is_active = serializers.BooleanField(required=False)
