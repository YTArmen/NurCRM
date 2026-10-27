# apps/main/production_salary_views.py
"""
Зарплата в производстве: ставки, табель, начисления, сводка, выплаты.
Базовый префикс: /api/main/production/salary/

Доступ: владелец/админ видит всех и правит ставки; обычный сотрудник видит
только свои табель/начисления/выплаты (403 на чужое и на изменение ставок).
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Q, Sum, Value as V, DecimalField
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date

from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.utils import _is_owner_like
from apps.main import production_salary_services as svc
from apps.main.models import (
    Product,
    ProductionEmployeeRate,
    ProductionPieceRate,
    ProductionSalaryAccrual,
    ProductionSalaryPayout,
    ProductionWorkSession,
    _user_display_name,
)
from apps.main.production_salary_serializers import (
    ProductionEmployeeRateSerializer,
    ProductionEmployeeRateUpdateSerializer,
    ProductionPieceRateSerializer,
    ProductionPieceRateUpdateSerializer,
    ProductionSalaryAccrualSerializer,
    ProductionSalaryPayoutCreateSerializer,
    ProductionSalaryPayoutSerializer,
    ProductionWorkSessionCreateSerializer,
    ProductionWorkSessionSerializer,
)

User = get_user_model()

MONEY = DecimalField(max_digits=12, decimal_places=2)
ZERO_MONEY = V(Decimal("0.00"), output_field=MONEY)
HOURS = DecimalField(max_digits=10, decimal_places=2)
ZERO_HOURS = V(Decimal("0.00"), output_field=HOURS)
QTY = DecimalField(max_digits=14, decimal_places=3)
ZERO_QTY = V(Decimal("0.000"), output_field=QTY)


from apps.users.permissions import read_only_guard  # noqa: E402
class SalaryPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200


class _SalaryBase(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _company(self):
        user = self.request.user
        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        if company is None:
            raise ValidationError({"detail": "У вас не задана компания."})
        return company

    def _is_manager(self) -> bool:
        return _is_owner_like(self.request.user)

    def _require_manager(self):
        if not self._is_manager():
            raise PermissionDenied("Доступно только владельцу или администратору.")

    def _scope_to_user(self, qs, field: str = "employee"):
        """Сотрудник видит только свои записи; владелец/админ — все."""
        if self._is_manager():
            return qs
        return qs.filter(**{f"{field}_id": self.request.user.id})

    def _requested_employee_id(self):
        """employee из query-параметров; сотрудник не может запросить чужого."""
        raw = (self.request.query_params.get("employee") or "").strip()
        if not self._is_manager():
            if raw and str(raw) != str(self.request.user.id):
                raise PermissionDenied("Доступны только собственные данные.")
            return str(self.request.user.id)
        return raw or None

    def _date_range(self):
        qp = self.request.query_params
        df_raw = (qp.get("date_from") or "").strip()
        dt_raw = (qp.get("date_to") or "").strip()
        df = dt = None
        if df_raw:
            df = parse_date(df_raw)
            if df is None:
                raise ValidationError({"date_from": ["Некорректная дата."]})
        if dt_raw:
            dt = parse_date(dt_raw)
            if dt is None:
                raise ValidationError({"date_to": ["Некорректная дата."]})
        return df, dt


# ─────────────────────────────────────────────────────────────
# Ставки
# ─────────────────────────────────────────────────────────────
class ProductionRateListAPIView(_SalaryBase):
    """GET /rates/ — почасовые ставки сотрудников."""

    def get(self, request, *args, **kwargs):
        self._require_manager()
        company = self._company()
        rows = (
            ProductionEmployeeRate.objects
            .filter(company=company)
            .select_related("employee")
            .order_by("employee__first_name", "employee__last_name")
        )
        return Response(ProductionEmployeeRateSerializer(rows, many=True).data)


class ProductionRateDetailAPIView(_SalaryBase):
    """PUT /rates/{employee_id}/ — установить почасовую ставку."""

    def put(self, request, employee_id, *args, **kwargs):
        self._require_manager()
        company = self._company()

        employee = get_object_or_404(User, id=employee_id, company=company)
        ser = ProductionEmployeeRateUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        row, _ = ProductionEmployeeRate.objects.update_or_create(
            company=company,
            employee=employee,
            defaults={
                "hourly_rate": ser.validated_data["hourly_rate"],
                "updated_by": request.user,
            },
        )
        return Response(ProductionEmployeeRateSerializer(row).data)


class ProductionPieceRateListAPIView(_SalaryBase):
    """GET /piece-rates/ — сдельные ставки по товарам."""

    def get(self, request, *args, **kwargs):
        self._require_manager()
        company = self._company()
        rows = (
            ProductionPieceRate.objects
            .filter(company=company)
            .select_related("product")
            .order_by("product__name")
        )
        return Response(ProductionPieceRateSerializer(rows, many=True).data)


class ProductionPieceRateDetailAPIView(_SalaryBase):
    """PUT /piece-rates/{product_id}/ — установить сдельную ставку за единицу."""

    def put(self, request, product_id, *args, **kwargs):
        self._require_manager()
        company = self._company()

        product = get_object_or_404(Product, id=product_id, company=company)
        ser = ProductionPieceRateUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        row, _ = ProductionPieceRate.objects.update_or_create(
            company=company,
            product=product,
            defaults={
                "amount_per_unit": ser.validated_data["amount_per_unit"],
                "updated_by": request.user,
            },
        )
        return Response(ProductionPieceRateSerializer(row).data)


# ─────────────────────────────────────────────────────────────
# Табель
# ─────────────────────────────────────────────────────────────
class ProductionWorkSessionListCreateAPIView(_SalaryBase):
    """GET /work-sessions/ — табель; POST — upsert часов за день."""

    def get(self, request, *args, **kwargs):
        company = self._company()
        qs = (
            ProductionWorkSession.objects
            .filter(company=company)
            .select_related("employee")
        )
        qs = self._scope_to_user(qs)

        employee_id = self._requested_employee_id()
        if employee_id:
            qs = qs.filter(employee_id=employee_id)

        df, dt = self._date_range()
        if df:
            qs = qs.filter(date__gte=df)
        if dt:
            qs = qs.filter(date__lte=dt)

        paginator = SalaryPagination()
        page = paginator.paginate_queryset(qs.order_by("-date", "-created_at"), request, view=self)
        return paginator.get_paginated_response(
            ProductionWorkSessionSerializer(page, many=True).data
        )

    def post(self, request, *args, **kwargs):
        self._require_manager()
        company = self._company()

        ser = ProductionWorkSessionCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        employee = get_object_or_404(User, id=data["employee"], company=company)

        session, _ = ProductionWorkSession.objects.update_or_create(
            company=company,
            employee=employee,
            date=data["date"],
            defaults={
                "hours": data["hours"],
                "comment": (data.get("comment") or "").strip(),
                "created_by": request.user,
            },
        )

        try:
            svc.upsert_hourly_accrual(session)
        except svc.AccrualPaidError:
            return Response(
                {"detail": "Начисление за этот день уже выплачено — изменить табель нельзя."},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(ProductionWorkSessionSerializer(session).data, status=status.HTTP_201_CREATED)


class ProductionWorkSessionDetailAPIView(_SalaryBase):
    """DELETE /work-sessions/{id}/ — удалить табель (если начисление не оплачено)."""

    def delete(self, request, pk, *args, **kwargs):
        self._require_manager()
        company = self._company()

        session = get_object_or_404(ProductionWorkSession, id=pk, company=company)
        try:
            svc.cancel_session_accruals(session)
        except svc.AccrualPaidError:
            return Response(
                {"detail": "Начисление по этому табелю уже выплачено — удалить нельзя."},
                status=status.HTTP_409_CONFLICT,
            )

        session.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────────────────────
# Начисления
# ─────────────────────────────────────────────────────────────
class ProductionAccrualListAPIView(_SalaryBase):
    """GET /accruals/ — начисления + summary по фильтру."""

    def get(self, request, *args, **kwargs):
        company = self._company()
        qs = (
            ProductionSalaryAccrual.objects
            .filter(company=company)
            .select_related("employee", "product")
        )
        qs = self._scope_to_user(qs)

        employee_id = self._requested_employee_id()
        if employee_id:
            qs = qs.filter(employee_id=employee_id)

        kind = (request.query_params.get("kind") or "").strip()
        if kind:
            if kind not in ProductionSalaryAccrual.Kind.values:
                raise ValidationError({"kind": ["Допустимые значения: hourly, piece."]})
            qs = qs.filter(kind=kind)

        status_q = (request.query_params.get("status") or "").strip()
        if status_q:
            if status_q not in ProductionSalaryAccrual.Status.values:
                raise ValidationError({"status": ["Допустимые значения: accrued, paid, canceled."]})
            qs = qs.filter(status=status_q)

        df, dt = self._date_range()
        if df:
            qs = qs.filter(created_at__date__gte=df)
        if dt:
            qs = qs.filter(created_at__date__lte=dt)

        agg = qs.aggregate(
            accrued_total=Coalesce(
                Sum("amount", filter=Q(status=ProductionSalaryAccrual.Status.ACCRUED), output_field=MONEY),
                ZERO_MONEY,
            ),
            paid_total=Coalesce(
                Sum("amount", filter=Q(status=ProductionSalaryAccrual.Status.PAID), output_field=MONEY),
                ZERO_MONEY,
            ),
        )

        paginator = SalaryPagination()
        page = paginator.paginate_queryset(qs.order_by("-created_at"), request, view=self)
        response = paginator.get_paginated_response(
            ProductionSalaryAccrualSerializer(page, many=True).data
        )
        response.data["summary"] = {
            "accrued_total": str(agg["accrued_total"]),
            "paid_total": str(agg["paid_total"]),
        }
        return response


# ─────────────────────────────────────────────────────────────
# Сводка по сотрудникам
# ─────────────────────────────────────────────────────────────
class ProductionSalarySummaryAPIView(_SalaryBase):
    """GET /summary/ — по сотрудникам: часы, почасовое, сдельное, начислено, выплачено."""

    def get(self, request, *args, **kwargs):
        company = self._company()
        df, dt = self._date_range()

        sessions = ProductionWorkSession.objects.filter(company=company)
        accruals = ProductionSalaryAccrual.objects.filter(company=company).exclude(
            status=ProductionSalaryAccrual.Status.CANCELED
        )
        sessions = self._scope_to_user(sessions)
        accruals = self._scope_to_user(accruals)

        employee_id = self._requested_employee_id()
        if employee_id:
            sessions = sessions.filter(employee_id=employee_id)
            accruals = accruals.filter(employee_id=employee_id)

        if df:
            sessions = sessions.filter(date__gte=df)
            accruals = accruals.filter(created_at__date__gte=df)
        if dt:
            sessions = sessions.filter(date__lte=dt)
            accruals = accruals.filter(created_at__date__lte=dt)

        hours_by_employee = {
            r["employee_id"]: r["hours_total"]
            for r in sessions.values("employee_id").annotate(
                hours_total=Coalesce(Sum("hours", output_field=HOURS), ZERO_HOURS)
            )
        }

        rows = accruals.values("employee_id").annotate(
            hourly_amount=Coalesce(
                Sum("amount", filter=Q(kind=ProductionSalaryAccrual.Kind.HOURLY), output_field=MONEY),
                ZERO_MONEY,
            ),
            piece_amount=Coalesce(
                Sum("amount", filter=Q(kind=ProductionSalaryAccrual.Kind.PIECE), output_field=MONEY),
                ZERO_MONEY,
            ),
            piece_quantity=Coalesce(
                Sum("quantity", filter=Q(kind=ProductionSalaryAccrual.Kind.PIECE), output_field=QTY),
                ZERO_QTY,
            ),
            accrued=Coalesce(
                Sum("amount", filter=Q(status=ProductionSalaryAccrual.Status.ACCRUED), output_field=MONEY),
                ZERO_MONEY,
            ),
            paid=Coalesce(
                Sum("amount", filter=Q(status=ProductionSalaryAccrual.Status.PAID), output_field=MONEY),
                ZERO_MONEY,
            ),
        )

        # Сотрудники, у которых есть только часы без начислений (ставка не задана),
        # тоже должны попасть в сводку — иначе их часы «исчезнут».
        by_employee = {r["employee_id"]: r for r in rows}
        for emp_id, hours_total in hours_by_employee.items():
            by_employee.setdefault(emp_id, {
                "employee_id": emp_id,
                "hourly_amount": Decimal("0.00"),
                "piece_amount": Decimal("0.00"),
                "piece_quantity": Decimal("0.000"),
                "accrued": Decimal("0.00"),
                "paid": Decimal("0.00"),
            })

        users = {u.id: u for u in User.objects.filter(id__in=by_employee.keys())}

        result = []
        for emp_id, r in by_employee.items():
            hourly = r["hourly_amount"]
            piece = r["piece_amount"]
            result.append({
                "employee": str(emp_id),
                "employee_name": _user_display_name(users.get(emp_id)),
                "hours_total": str(hours_by_employee.get(emp_id, Decimal("0.00"))),
                "hourly_amount": str(hourly),
                "piece_quantity": str(r["piece_quantity"]),
                "piece_amount": str(piece),
                "total": str(hourly + piece),
                "accrued": str(r["accrued"]),
                "paid": str(r["paid"]),
            })

        result.sort(key=lambda x: x["employee_name"])
        return Response(result)


# ─────────────────────────────────────────────────────────────
# Выплаты
# ─────────────────────────────────────────────────────────────
@read_only_guard("can_edit_salary")
class ProductionPayoutListCreateAPIView(_SalaryBase):
    """GET /payouts/ — история выплат; POST — выплатить (FIFO + расход кассы)."""

    def get(self, request, *args, **kwargs):
        company = self._company()
        qs = (
            ProductionSalaryPayout.objects
            .filter(company=company)
            .select_related("employee")
        )
        qs = self._scope_to_user(qs)

        employee_id = self._requested_employee_id()
        if employee_id:
            qs = qs.filter(employee_id=employee_id)

        df, dt = self._date_range()
        if df:
            qs = qs.filter(created_at__date__gte=df)
        if dt:
            qs = qs.filter(created_at__date__lte=dt)

        paginator = SalaryPagination()
        page = paginator.paginate_queryset(qs.order_by("-created_at"), request, view=self)
        return paginator.get_paginated_response(
            ProductionSalaryPayoutSerializer(page, many=True).data
        )

    def post(self, request, *args, **kwargs):
        self._require_manager()
        company = self._company()

        from apps.construction.models import Cashbox

        ser = ProductionSalaryPayoutCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        employee = get_object_or_404(User, id=data["employee"], company=company)
        cashbox = get_object_or_404(Cashbox, id=data["cashbox"], company=company)

        try:
            payout = svc.create_payout(
                company=company,
                employee=employee,
                amount=data["amount"],
                cashbox=cashbox,
                comment=data.get("comment") or "",
                created_by=request.user,
            )
        except svc.SalaryBalanceError as exc:
            return Response(
                {
                    "detail": "Сумма выплаты превышает начисленный остаток.",
                    "balance": str(exc.balance),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValueError:
            return Response(
                {"detail": "Сумма выплаты должна быть больше нуля."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(ProductionSalaryPayoutSerializer(payout).data, status=status.HTTP_201_CREATED)
