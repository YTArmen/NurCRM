from rest_framework import serializers

from apps.users.models import Branch

from .models import Asset, Employee, EmploymentContract, Expense, ExpenseCategory, TaxPayment
from .services import expenses as ex


def check_branch(ctx, branch):
    """Филиал принадлежит компании и доступен пользователю."""
    if branch is None:
        return branch
    if branch.company_id != ctx.company.id:
        raise serializers.ValidationError("Филиал не найден.")
    allowed = ctx.allowed_branch_ids()
    if allowed is not None and branch.id not in allowed:
        raise serializers.ValidationError("Нет доступа к филиалу.")
    return branch


class _Scoped:
    def _ctx(self):
        return self.context["ctx"]

    def validate_branch(self, v):
        return check_branch(self._ctx(), v)


class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ["id", "name", "is_system"]
        read_only_fields = ["id", "is_system"]
        validators = []

    def validate_name(self, v):
        v = v.strip()
        if not v:
            raise serializers.ValidationError("Укажите название.")
        qs = ExpenseCategory.objects.filter(company=self.context["ctx"].company)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if any(n.casefold() == v.casefold() for n in qs.values_list("name", flat=True)):
            raise serializers.ValidationError("Такая категория уже есть.")
        return v


class ExpenseSerializer(_Scoped, serializers.ModelSerializer):
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all())
    category = serializers.PrimaryKeyRelatedField(queryset=ExpenseCategory.objects.all())
    category_name = serializers.CharField(source="category.name", read_only=True)
    amount = serializers.IntegerField(min_value=1)
    voided = serializers.SerializerMethodField()

    class Meta:
        model = Expense
        fields = ["id", "branch", "category", "category_name", "date", "amount", "description", "method",
                  "voided", "void_reason", "voided_at", "created_at"]
        read_only_fields = ["id", "void_reason", "voided_at", "created_at"]

    def get_voided(self, obj):
        return obj.voided_at is not None

    def validate_category(self, v):
        if v.company_id != self._ctx().company.id or v.archived_at:
            raise serializers.ValidationError("Категория не найдена.")
        return v


class TaxPaymentSerializer(_Scoped, serializers.ModelSerializer):
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)
    amount = serializers.IntegerField(min_value=0)

    class Meta:
        model = TaxPayment
        fields = ["id", "branch", "tax", "due_date", "amount", "paid", "paid_at", "expense", "source"]
        read_only_fields = ["id", "paid", "paid_at", "expense", "source"]


class AssetSerializer(_Scoped, serializers.ModelSerializer):
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all())
    cost = serializers.IntegerField(min_value=1)
    life_years = serializers.IntegerField(min_value=1, max_value=100)
    monthly = serializers.SerializerMethodField()
    accumulated = serializers.SerializerMethodField()
    residual = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = ["id", "branch", "name", "cost", "purchase_date", "life_years", "monthly", "accumulated",
                  "residual"]
        read_only_fields = ["id"]

    def _st(self, obj):
        return ex.asset_state(obj, self.context["month"])

    def get_monthly(self, obj):
        return self._st(obj)["monthly"]

    def get_accumulated(self, obj):
        return self._st(obj)["accumulated"]

    def get_residual(self, obj):
        return self._st(obj)["residual"]


class EmployeeSerializer(_Scoped, serializers.ModelSerializer):
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all())
    salary = serializers.IntegerField(min_value=0)
    bonus = serializers.IntegerField(min_value=0, required=False, default=0)
    contract = serializers.SerializerMethodField()

    class Meta:
        model = Employee
        fields = ["id", "branch", "full_name", "position", "phone", "hired_at", "fired_at", "schedule", "salary",
                  "bonus", "contract"]
        read_only_fields = ["id"]

    def get_contract(self, obj):
        c = (EmploymentContract.objects.filter(employee=obj, archived_at__isnull=True).order_by("-date").first())
        if not c:
            return None
        return {"id": str(c.id), "number": c.number, "label": c.label, "date": c.date}
