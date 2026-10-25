"""Этап 4: расходы, налоги, активы, итог месяца, сотрудники, зарплата."""
from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, status as http, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import Branch
from core.exceptions import ApiError

from . import audit
from .branch_filter import apply_branch
from .models import Asset, Employee, Expense, ExpenseCategory, TaxPayment
from .permissions import ArendaSection
from .serializers_team import (AssetSerializer, EmployeeSerializer, ExpenseCategorySerializer, ExpenseSerializer,
                               TaxPaymentSerializer, check_branch)
from .services import expenses as ex, payroll as pr
from .views import CtxMixin

METHODS = {"cash", "bank", "qr", "wallet"}


def _snap(ser_cls, obj, ctx, **extra):
    return dict(ser_cls(obj, context={"ctx": ctx, **extra}).data)


def _json(d):
    import json
    from rest_framework.utils.encoders import JSONEncoder
    return json.loads(json.dumps(d, cls=JSONEncoder))


class TeamMixin(CtxMixin):
    def ser_ctx(self):
        return {"ctx": self.ctx, "request": self.request}

    def month(self):
        return ex.parse_month(self.request.query_params.get("month"), self.today())

    def method_from_body(self):
        m = self.request.data.get("method", "cash")
        if m not in METHODS:
            raise ValidationError({"method": ["Недопустимый способ оплаты."]})
        return m

    def default_branch(self, tax_branch=None):
        b = tax_branch
        raw = self.request.data.get("branch")
        if raw and not b:
            b = Branch.objects.filter(id=raw, company=self.ctx.company).first()
            if not b:
                raise ValidationError({"branch": ["Филиал не найден."]})
        if not b:
            qs = Branch.objects.filter(company=self.ctx.company)
            allowed = self.ctx.allowed_branch_ids()
            if allowed is not None:
                qs = qs.filter(id__in=allowed)
            if qs.count() != 1:
                raise ValidationError({"branch": ["Укажите филиал."]})
            b = qs.first()
        try:
            return check_branch(self.ctx, b)
        except ValidationError as e:
            raise ValidationError({"branch": e.detail})


# ---------------------------------------------------------------- категории и расходы


class ExpenseCategoryViewSet(TeamMixin, mixins.ListModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin,
                             viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, ArendaSection("expenses")]
    serializer_class = ExpenseCategorySerializer
    pagination_class = None

    def get_queryset(self):
        return ExpenseCategory.objects.filter(company=self.ctx.company, archived_at__isnull=True)

    def perform_create(self, ser):
        obj = ser.save(company=self.ctx.company, created_by=self.request.user)
        audit.log(self.request, self.ctx.company, "create", "expense_category", obj,
                  after=_json(ser.data), object_name=obj.name)

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.is_system:
            raise ApiError("system_category", "Системную категорию нельзя убрать.", status_code=409)
        obj.archived_at = timezone.now()
        obj.save(update_fields=["archived_at"])
        audit.log(request, self.ctx.company, "archive", "expense_category", obj, object_name=obj.name)
        return Response(status=http.HTTP_204_NO_CONTENT)


class ExpenseViewSet(TeamMixin, mixins.ListModelMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                     mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """Расходы без физического удаления: сторно `POST expenses/<id>/void/ {reason}`."""
    permission_classes = [IsAuthenticated, ArendaSection("expenses")]
    serializer_class = ExpenseSerializer

    def get_queryset(self):
        qs = ex.expense_qs(self.ctx.company, self.branch_ids()).select_related("category")
        q = self.request.query_params
        if q.get("month"):
            m = self.month()
            qs = qs.filter(date__gte=m, date__lte=ex.month_end(m))
        if q.get("category"):
            qs = qs.filter(category_id=q["category"])
        if q.get("method"):
            qs = qs.filter(method=q["method"])
        if q.get("voided") in ("true", "1"):
            qs = qs.filter(voided_at__isnull=False)
        elif q.get("voided") in ("false", "0"):
            qs = qs.filter(voided_at__isnull=True)
        return qs

    def get_serializer_context(self):
        return {**super().get_serializer_context(), **self.ser_ctx()}

    def perform_create(self, ser):
        obj = ser.save(company=self.ctx.company, created_by=self.request.user)
        audit.log(self.request, self.ctx.company, "create", "expense", obj, after=_json(ser.data),
                  branch=obj.branch, object_name=f"{obj.category.name}: {obj.amount}")

    def update(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.voided_at:
            raise ApiError("voided", "Сторнированный расход нельзя менять.", status_code=409)
        before = _snap(ExpenseSerializer, obj, self.ctx)
        kwargs["partial"] = True
        resp = super().update(request, *args, **kwargs)
        obj.refresh_from_db()
        audit.log(request, self.ctx.company, "update", "expense", obj, before=_json(before),
                  after=_json(_snap(ExpenseSerializer, obj, self.ctx)), branch=obj.branch,
                  object_name=f"{obj.category.name}: {obj.amount}")
        return resp

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        obj = self.get_object()
        reason = str(request.data.get("reason", "")).strip()
        if not reason:
            raise ValidationError({"reason": ["Укажите причину."]})
        if obj.voided_at:
            return Response(self.get_serializer(obj).data)
        before = _snap(ExpenseSerializer, obj, self.ctx)
        ex.void_expense(obj, reason[:255])
        obj.refresh_from_db()
        audit.log(request, self.ctx.company, "void", "expense", obj, before=_json(before),
                  after=_json(_snap(ExpenseSerializer, obj, self.ctx)), branch=obj.branch,
                  object_name=f"{obj.category.name}: {obj.amount}")
        return Response(self.get_serializer(obj).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        return Response(ex.expenses_by_category(self.ctx.company, self.month(), self.branch_ids()))


# ---------------------------------------------------------------- налоговый календарь


class TaxPaymentViewSet(TeamMixin, mixins.ListModelMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                        mixins.UpdateModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, ArendaSection("expenses")]
    serializer_class = TaxPaymentSerializer

    def get_queryset(self):
        qs = TaxPayment.objects.filter(company=self.ctx.company)
        ids = self.branch_ids()
        if ids is not None:
            qs = qs.filter(branch_id__in=ids)
        q = self.request.query_params
        if q.get("month"):
            m = self.month()
            qs = qs.filter(due_date__gte=m, due_date__lte=ex.month_end(m))
        if q.get("paid") in ("true", "1"):
            qs = qs.filter(paid=True)
        elif q.get("paid") in ("false", "0"):
            qs = qs.filter(paid=False)
        return qs

    def get_serializer_context(self):
        return {**super().get_serializer_context(), **self.ser_ctx()}

    def list(self, request, *args, **kwargs):
        # удержания и взнос работодателя из ведомости -> календарь (идемпотентно)
        base = ex.parse_month(request.query_params.get("month"), self.today())
        for m in {ex.add_months(base, -1), base, ex.month_start(self.today()),
                  ex.add_months(ex.month_start(self.today()), -1)}:
            ex.sync_payroll_taxes(self.ctx.company, m, self.branch_ids(), request.user)
        return super().list(request, *args, **kwargs)

    def perform_create(self, ser):
        branch = ser.validated_data.get("branch") or self.default_branch()
        obj = ser.save(company=self.ctx.company, created_by=self.request.user, source="manual", branch=branch)
        audit.log(self.request, self.ctx.company, "create", "tax_payment", obj, after=_json(ser.data),
                  branch=obj.branch, object_name=obj.tax)

    def update(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.paid:
            raise ApiError("paid", "Оплаченный платёж менять нельзя.", status_code=409)
        before = _snap(TaxPaymentSerializer, obj, self.ctx)
        kwargs["partial"] = True
        resp = super().update(request, *args, **kwargs)
        obj.refresh_from_db()
        audit.log(request, self.ctx.company, "update", "tax_payment", obj, before=_json(before),
                  after=_json(_snap(TaxPaymentSerializer, obj, self.ctx)), branch=obj.branch, object_name=obj.tax)
        return resp

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        tax = self.get_object()
        if tax.amount <= 0:
            raise ValidationError({"amount": ["Сумма платежа должна быть больше нуля."]})
        was_paid = tax.paid and tax.expense_id and not tax.expense.voided_at
        before = _snap(TaxPaymentSerializer, tax, self.ctx)
        branch = self.default_branch(tax.branch)
        with transaction.atomic():
            exp = ex.pay_tax(tax, branch=branch, method=self.method_from_body(), user=request.user)
            if not was_paid:
                audit.log(request, self.ctx.company, "pay", "tax_payment", tax, before=_json(before),
                          after={"expense": str(exp.id), "amount": exp.amount}, branch=branch, object_name=tax.tax)
        tax.refresh_from_db()
        return Response(self.get_serializer(tax).data)


# ---------------------------------------------------------------- активы


class AssetViewSet(TeamMixin, mixins.ListModelMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                   mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, ArendaSection("expenses")]
    serializer_class = AssetSerializer

    def get_queryset(self):
        return apply_branch(Asset.objects.filter(company=self.ctx.company, archived_at__isnull=True),
                            self.branch_ids()).order_by("purchase_date", "created_at")

    def get_serializer_context(self):
        return {**super().get_serializer_context(), **self.ser_ctx(), "month": self.month()}

    def perform_create(self, ser):
        obj = ser.save(company=self.ctx.company, created_by=self.request.user)
        audit.log(self.request, self.ctx.company, "create", "asset", obj, after=_json(ser.data),
                  branch=obj.branch, object_name=obj.name)

    def update(self, request, *args, **kwargs):
        obj = self.get_object()
        before = _snap(AssetSerializer, obj, self.ctx, month=self.month())
        kwargs["partial"] = True
        resp = super().update(request, *args, **kwargs)
        obj.refresh_from_db()
        audit.log(request, self.ctx.company, "update", "asset", obj, before=_json(before),
                  after=_json(_snap(AssetSerializer, obj, self.ctx, month=self.month())), branch=obj.branch,
                  object_name=obj.name)
        return resp

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        before = _snap(AssetSerializer, obj, self.ctx, month=self.month())
        obj.archived_at = timezone.now()  # списание: амортизация прекращается
        obj.save(update_fields=["archived_at"])
        audit.log(request, self.ctx.company, "archive", "asset", obj, before=_json(before), branch=obj.branch,
                  object_name=obj.name)
        return Response(status=http.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        m = self.month()
        assets = list(self.get_queryset())
        sts = [ex.asset_state(a, m) for a in assets]
        return Response({"month": m.strftime("%Y-%m"), "count": len(assets),
                         "cost": sum(a.cost for a in assets), "monthly": sum(s["monthly"] for s in sts),
                         "accumulated": sum(s["accumulated"] for s in sts),
                         "residual": sum(s["residual"] for s in sts)})


# ---------------------------------------------------------------- итог месяца


class FinanceSummaryView(TeamMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("expenses")]

    def get(self, request):
        return Response(ex.month_summary(self.ctx.company, self.month(), self.branch_ids()))


# ---------------------------------------------------------------- сотрудники и зарплата


class EmployeeViewSet(TeamMixin, mixins.ListModelMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                      mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, ArendaSection("staff")]
    serializer_class = EmployeeSerializer

    def get_queryset(self):
        qs = apply_branch(Employee.objects.filter(company=self.ctx.company, archived_at__isnull=True),
                          self.branch_ids()).order_by("full_name")
        s = self.request.query_params.get("search")
        return qs.filter(full_name__icontains=s) if s else qs

    def get_serializer_context(self):
        return {**super().get_serializer_context(), **self.ser_ctx()}

    def perform_create(self, ser):
        obj = ser.save(company=self.ctx.company, created_by=self.request.user)
        audit.log(self.request, self.ctx.company, "create", "employee", obj, after=_json(ser.data),
                  branch=obj.branch, object_name=obj.full_name)

    def update(self, request, *args, **kwargs):
        obj = self.get_object()
        before = _snap(EmployeeSerializer, obj, self.ctx)
        kwargs["partial"] = True
        resp = super().update(request, *args, **kwargs)
        obj.refresh_from_db()
        audit.log(request, self.ctx.company, "update", "employee", obj, before=_json(before),
                  after=_json(_snap(EmployeeSerializer, obj, self.ctx)), branch=obj.branch,
                  object_name=obj.full_name)
        return resp

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        before = _snap(EmployeeSerializer, obj, self.ctx)
        obj.archived_at = timezone.now()
        if not obj.fired_at:
            obj.fired_at = self.today()
        obj.save(update_fields=["archived_at", "fired_at"])
        audit.log(request, self.ctx.company, "archive", "employee", obj, before=_json(before), branch=obj.branch,
                  object_name=obj.full_name)
        return Response(status=http.HTTP_204_NO_CONTENT)


class PayrollView(TeamMixin, APIView):
    """GET payroll/?month=YYYY-MM&branch= — ведомость."""
    permission_classes = [IsAuthenticated, ArendaSection("staff")]

    def get(self, request):
        m = self.month()
        ids = self.branch_ids()
        ex.sync_payroll_taxes(self.ctx.company, m, ids, request.user)
        return Response(pr.public_sheet(pr.compute_sheet(self.ctx.company, m, ids)))


class PayrollPayView(TeamMixin, APIView):
    """POST payroll/<YYYY-MM>/advance/ | salary/  {branch?, employees?: [id], method?}."""
    permission_classes = [IsAuthenticated, ArendaSection("staff")]
    kind = None

    def post(self, request, month):
        m = ex.parse_month(month)
        ids = self.branch_ids()
        emp = request.data.get("employees")
        if emp is not None:
            if not isinstance(emp, list):
                raise ValidationError({"employees": ["Ожидается список."]})
            emp = {str(x) for x in emp}
        res = pr.pay(self.ctx.company, m, self.kind, ids, emp, self.method_from_body(), request.user)
        if res["created"]:
            audit.log(request, self.ctx.company, "pay_advance" if self.kind == "advance" else "pay_salary",
                      "payroll", object_id=m.strftime("%Y-%m"), object_name=f"Выплата {m:%Y-%m}",
                      after=_json(res))
        ex.sync_payroll_taxes(self.ctx.company, m, ids, request.user)
        return Response({**res, "sheet": pr.public_sheet(pr.compute_sheet(self.ctx.company, m, ids))})


class PayrollAdvanceView(PayrollPayView):
    kind = "advance"


class PayrollSalaryView(PayrollPayView):
    kind = "salary"
