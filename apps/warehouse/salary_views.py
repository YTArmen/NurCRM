# apps/warehouse/salary_views.py
"""
Зарплата агентов (сектор «Склад»): ставки складов, начисления, сводка, выплаты.
Базовый префикс: /api/warehouse/salary/
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Sum, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date

from rest_framework import permissions, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.utils import _is_owner_like
from apps.warehouse import models as m
from apps.warehouse import salary_services
from apps.warehouse.salary_serializers import (
    WarehouseRateRowSerializer,
    WarehouseRateUpdateSerializer,
    AgentSalaryAccrualSerializer,
    AgentSalaryPayoutSerializer,
    AgentSalaryPayoutCreateSerializer,
)

User = get_user_model()

# Статусы, входящие в «начислено за период».
ACCRUED_OR_PAID = (
    m.AgentSalaryAccrual.Status.ACCRUED,
    m.AgentSalaryAccrual.Status.PAID,
)


def _money2(v) -> str:
    return str(Decimal(v or 0).quantize(Decimal("0.01")))


def _user_name(user) -> str:
    if not user:
        return ""
    full = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
    return full or getattr(user, "email", None) or str(getattr(user, "id", "") or "")


from apps.users.permissions import read_only_guard  # noqa: E402
class SalaryPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200


# ─────────────────────────────────────────────────────────────
# Доступ
# ─────────────────────────────────────────────────────────────
# scope: 'manager' (owner/admin) | 'employee' (can_view_salary) | 'agent' | None
def _resolve_access(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None, None, []
    if _is_owner_like(user):
        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        return "manager", company, ([company.id] if company else [])
    company = getattr(user, "company", None)
    if company and getattr(user, "can_view_salary", False):
        return "employee", company, [company.id]
    agent_company_ids = list(
        m.CompanyWarehouseAgent.objects
        .filter(user=user, status=m.CompanyWarehouseAgent.Status.ACTIVE)
        .values_list("company_id", flat=True)
    )
    if agent_company_ids:
        return "agent", None, agent_company_ids
    return None, None, []


def _parse_day_bound(raw, *, end: bool):
    """YYYY-MM-DD → aware datetime в TZ компании. end=True → начало следующего дня (эксклюзивно)."""
    d = parse_date((raw or "").strip())
    if not d:
        return None
    tz = timezone.get_current_timezone()
    base = datetime.combine(d, time.min)
    dt = timezone.make_aware(base, tz)
    return dt + timedelta(days=1) if end else dt


def _apply_common_accrual_filters(qs, request):
    warehouse = (request.query_params.get("warehouse") or "").strip()
    if warehouse:
        qs = qs.filter(warehouse_id=warehouse)
    sale_type = (request.query_params.get("sale_type") or "").strip().lower()
    if sale_type in (m.AgentSalaryAccrual.SaleType.RETAIL, m.AgentSalaryAccrual.SaleType.WHOLESALE):
        qs = qs.filter(sale_type=sale_type)
    return qs


# ─────────────────────────────────────────────────────────────
# 4.1 / 4.2 Ставки складов
# ─────────────────────────────────────────────────────────────
class SalaryRateListAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = SalaryPagination

    def get(self, request, *args, **kwargs):
        scope, company, _ = _resolve_access(request.user)
        if scope != "manager" or company is None:
            return Response({"detail": "Только владелец/админ."}, status=status.HTTP_403_FORBIDDEN)

        qs = m.Warehouse.objects.filter(company=company).select_related("salary_rate")
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        qs = qs.order_by("name", "id")

        paginator = SalaryPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = WarehouseRateRowSerializer(page, many=True).data
        return paginator.get_paginated_response(data)


@read_only_guard("can_edit_salary")
class SalaryRateUpdateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def put(self, request, warehouse_id=None, *args, **kwargs):
        scope, company, _ = _resolve_access(request.user)
        if scope != "manager" or company is None:
            return Response({"detail": "Только владелец/админ."}, status=status.HTTP_403_FORBIDDEN)

        warehouse = get_object_or_404(m.Warehouse.objects.filter(company=company), pk=warehouse_id)

        ser = WarehouseRateUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        rate, _created = m.WarehouseSalaryRate.objects.get_or_create(
            company=company, warehouse=warehouse,
        )
        if "retail_percent" in ser.validated_data:
            rate.retail_percent = ser.validated_data["retail_percent"]
        if "wholesale_percent" in ser.validated_data:
            rate.wholesale_percent = ser.validated_data["wholesale_percent"]
        rate.updated_by = request.user
        try:
            rate.full_clean()
        except DjangoValidationError as exc:
            return Response(
                getattr(exc, "message_dict", {"detail": exc.messages}),
                status=status.HTTP_400_BAD_REQUEST,
            )
        rate.save()

        warehouse = m.Warehouse.objects.select_related("salary_rate").get(pk=warehouse.pk)
        return Response(WarehouseRateRowSerializer(warehouse).data)


# ─────────────────────────────────────────────────────────────
# 4.3 История начислений
# ─────────────────────────────────────────────────────────────
class SalaryAccrualListAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        scope, company, cids = _resolve_access(request.user)
        if scope is None:
            return Response({"detail": "Нет доступа к зарплате."}, status=status.HTTP_403_FORBIDDEN)

        qs = m.AgentSalaryAccrual.objects.select_related("agent", "sale", "warehouse")
        if scope in ("manager", "employee"):
            qs = qs.filter(company=company)
            agent = (request.query_params.get("agent") or "").strip()
            if agent:
                qs = qs.filter(agent_id=agent)
        else:  # agent — только свои
            qs = qs.filter(company_id__in=cids, agent=request.user)

        qs = _apply_common_accrual_filters(qs, request)

        status_f = (request.query_params.get("status") or "").strip().lower()
        valid_statuses = {c for c, _ in m.AgentSalaryAccrual.Status.choices}
        if status_f in valid_statuses:
            qs = qs.filter(status=status_f)

        df = _parse_day_bound(request.query_params.get("date_from"), end=False)
        dt = _parse_day_bound(request.query_params.get("date_to"), end=True)
        if df:
            qs = qs.filter(created_at__gte=df)
        if dt:
            qs = qs.filter(created_at__lt=dt)

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(sale__number__icontains=search)

        qs = qs.order_by("-created_at", "id")

        paginator = SalaryPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = AgentSalaryAccrualSerializer(page, many=True).data
        return paginator.get_paginated_response(data)


# ─────────────────────────────────────────────────────────────
# 4.4 Сводка
# ─────────────────────────────────────────────────────────────
class SalarySummaryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        scope, company, cids = _resolve_access(request.user)
        if scope is None:
            return Response({"detail": "Нет доступа к зарплате."}, status=status.HTTP_403_FORBIDDEN)

        # Базовые queryset'ы по области видимости
        accr = m.AgentSalaryAccrual.objects.all()
        payouts = m.AgentSalaryPayout.objects.all()
        if scope in ("manager", "employee"):
            accr = accr.filter(company=company)
            payouts = payouts.filter(company=company)
            agent = (request.query_params.get("agent") or "").strip()
            if agent:
                accr = accr.filter(agent_id=agent)
                payouts = payouts.filter(agent_id=agent)
        else:
            accr = accr.filter(company_id__in=cids, agent=request.user)
            payouts = payouts.filter(company_id__in=cids, agent=request.user)

        accr = _apply_common_accrual_filters(accr, request)

        # balance — без фильтра периода (текущий долг)
        balance_base = accr.filter(status=m.AgentSalaryAccrual.Status.ACCRUED)

        # период по created_at
        df = _parse_day_bound(request.query_params.get("date_from"), end=False)
        dt = _parse_day_bound(request.query_params.get("date_to"), end=True)
        period = accr
        payouts_period = payouts
        if df:
            period = period.filter(created_at__gte=df)
            payouts_period = payouts_period.filter(created_at__gte=df)
        if dt:
            period = period.filter(created_at__lt=dt)
            payouts_period = payouts_period.filter(created_at__lt=dt)

        accrued_total = period.filter(status__in=ACCRUED_OR_PAID).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        paid_total = payouts_period.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        balance_total = balance_base.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        accruals_count = period.count()

        totals = {
            "accrued_total": _money2(accrued_total),
            "paid_total": _money2(paid_total),
            "balance": _money2(balance_total),
            "accruals_count": accruals_count,
        }

        # ── by_agent ──
        by_agent_map: dict = {}

        def _row(agent_id):
            return by_agent_map.setdefault(agent_id, {
                "agent": str(agent_id),
                "agent_name": "",
                "retail_amount": Decimal("0.00"),
                "wholesale_amount": Decimal("0.00"),
                "accrued_total": Decimal("0.00"),
                "paid_total": Decimal("0.00"),
                "balance": Decimal("0.00"),
            })

        # начислено за период (accrued+paid), с разбивкой по типу
        for r in (
            period.filter(status__in=ACCRUED_OR_PAID)
            .values("agent_id", "sale_type")
            .annotate(s=Sum("amount"))
        ):
            row = _row(r["agent_id"])
            amt = Decimal(r["s"] or 0)
            row["accrued_total"] += amt
            if r["sale_type"] == m.AgentSalaryAccrual.SaleType.WHOLESALE:
                row["wholesale_amount"] += amt
            else:
                row["retail_amount"] += amt

        # выплачено за период
        for r in payouts_period.values("agent_id").annotate(s=Sum("amount")):
            _row(r["agent_id"])["paid_total"] += Decimal(r["s"] or 0)

        # текущий баланс (без периода)
        for r in balance_base.values("agent_id").annotate(s=Sum("amount")):
            _row(r["agent_id"])["balance"] += Decimal(r["s"] or 0)

        # имена агентов
        agent_ids = list(by_agent_map.keys())
        names = {u.id: _user_name(u) for u in User.objects.filter(id__in=agent_ids)}

        by_agent = []
        for aid, row in by_agent_map.items():
            by_agent.append({
                "agent": str(aid),
                "agent_name": names.get(aid, ""),
                "retail_amount": _money2(row["retail_amount"]),
                "wholesale_amount": _money2(row["wholesale_amount"]),
                "accrued_total": _money2(row["accrued_total"]),
                "paid_total": _money2(row["paid_total"]),
                "balance": _money2(row["balance"]),
            })
        by_agent.sort(key=lambda x: Decimal(x["balance"]), reverse=True)

        return Response({"totals": totals, "by_agent": by_agent})


# ─────────────────────────────────────────────────────────────
# 4.5 / 4.6 Выплаты
# ─────────────────────────────────────────────────────────────
@read_only_guard("can_edit_salary")
class SalaryPayoutListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        scope, company, cids = _resolve_access(request.user)
        if scope is None:
            return Response({"detail": "Нет доступа к зарплате."}, status=status.HTTP_403_FORBIDDEN)

        qs = m.AgentSalaryPayout.objects.select_related("agent", "created_by", "money_document__cash_register")
        if scope in ("manager", "employee"):
            qs = qs.filter(company=company)
            agent = (request.query_params.get("agent") or "").strip()
            if agent:
                qs = qs.filter(agent_id=agent)
        else:
            qs = qs.filter(company_id__in=cids, agent=request.user)

        df = _parse_day_bound(request.query_params.get("date_from"), end=False)
        dt = _parse_day_bound(request.query_params.get("date_to"), end=True)
        if df:
            qs = qs.filter(created_at__gte=df)
        if dt:
            qs = qs.filter(created_at__lt=dt)

        qs = qs.order_by("-created_at", "id")

        paginator = SalaryPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = AgentSalaryPayoutSerializer(page, many=True).data
        return paginator.get_paginated_response(data)

    def post(self, request, *args, **kwargs):
        scope, company, _ = _resolve_access(request.user)
        if scope != "manager" or company is None:
            return Response({"detail": "Только владелец/админ."}, status=status.HTTP_403_FORBIDDEN)

        ser = AgentSalaryPayoutCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        agent_id = ser.validated_data["agent"]

        agent = User.objects.filter(id=agent_id).first()
        if agent is None or not _agent_valid_for_company(agent, company):
            return Response(
                {"agent": ["Агент не найден или не активен в компании"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cash_register = None
        cash_register_id = ser.validated_data.get("cash_register")
        if cash_register_id:
            cash_register = m.CashRegister.objects.filter(id=cash_register_id, company=company).first()
            if cash_register is None:
                return Response(
                    {"cash_register": ["Касса не найдена в компании."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            payout = salary_services.create_payout(
                company=company,
                agent=agent,
                amount=ser.validated_data["amount"],
                comment=ser.validated_data.get("comment") or "",
                created_by=request.user,
                cash_register=cash_register,
            )
        except salary_services.SalaryCashError as exc:
            return Response({"cash_register": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        except salary_services.SalaryBalanceError as exc:
            return Response(
                {"amount": [f"Сумма превышает баланс агента ({_money2(exc.balance)})"]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValueError:
            return Response(
                {"amount": ["Сумма должна быть больше 0."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        _salary_cashflow(payout, company=company, agent=agent, user=request.user)
        return Response(
            AgentSalaryPayoutSerializer(payout).data,
            status=status.HTTP_201_CREATED,
        )


def _salary_cashflow(payout, *, company, agent, user):
    """Серверная выплата ЗП → операция кассы со статьёй «Зарплата» (ТЗ «Бухгалтер», п. 3). Не ломает выплату."""
    try:
        from django.db import transaction
        from apps.construction.auto_cashflow import create_auto_cashflow
        from apps.construction.categories import get_default_category
        from apps.construction.models import CashFlow

        name = f"{getattr(agent, 'first_name', '') or ''} {getattr(agent, 'last_name', '') or ''}".strip() or str(agent.pk)
        with transaction.atomic():
            create_auto_cashflow(
                company=company, user=user, type=CashFlow.Type.EXPENSE, amount=payout.amount,
                source_kind=CashFlow.SourceKind.MANUAL, source_id=f"salary_payout:{payout.pk}",
                name=f"Выплата ЗП: {name}", category=get_default_category(company, "salary"),
            )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("salary cashflow failed for payout %s", payout.pk, exc_info=True)


def _agent_valid_for_company(agent, company) -> bool:
    """Агент валиден для выплаты, если у него активное членство в компании либо есть начисления."""
    if m.CompanyWarehouseAgent.objects.filter(
        company=company, user=agent, status=m.CompanyWarehouseAgent.Status.ACTIVE
    ).exists():
        return True
    return m.AgentSalaryAccrual.objects.filter(company=company, agent=agent).exists()
