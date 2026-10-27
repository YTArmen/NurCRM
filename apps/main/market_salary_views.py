"""
ТЗ Н-18: выплата зарплаты магазина.

  POST/GET /api/main/market-salary/payouts/      — выплатить / история
  DELETE   /api/main/market-salary/payouts/{id}/ — отмена обратным движением

Одна выплата = один расход кассы (source_kind=salary_payout, source_id=id выплаты).
deduction (удержание) денег не двигает. source:"owner" пока не поддержан (см. код ошибки).
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from apps.main.models import MarketSalaryPayout, _user_display_name
from apps.main.tz26.staff import perms as staff_perms
from apps.users.permissions import read_only_guard
from apps.utils import _is_owner_like

User = get_user_model()
_Q2 = Decimal("0.01")


def _err(code, detail, http=status.HTTP_400_BAD_REQUEST):
    return Response({"code": code, "detail": detail}, status=http)


def _company_of(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def serialize_payout(p):
    return {
        "id": str(p.id),
        "employee": str(p.employee_id),
        "employee_name": _user_display_name(p.employee),
        "kind": p.kind,
        "amount": f"{p.amount:.2f}",
        "period_from": p.period_from.isoformat() if p.period_from else None,
        "period_to": p.period_to.isoformat() if p.period_to else None,
        "date": p.date.isoformat() if p.date else None,
        "method": p.method,
        "cashbox": str(p.cashbox_id) if p.cashbox_id else None,
        "shift": str(p.shift_id) if p.shift_id else None,
        "comment": p.comment,
        "reason": p.comment,
        "cashflow_id": str(p.cashflow_id) if p.cashflow_id else None,
        "reversed": p.reversed_at is not None,
        "reversed_at": p.reversed_at.isoformat() if p.reversed_at else None,
        "created_by": str(p.created_by_id) if p.created_by_id else None,
        "created_at": p.created_at.isoformat(),
    }


def payout_queryset(company, start, end, user_ids=None):
    """
    Неотменённые записи за период аналитики [start, end).
    Штраф/премия (есть `date`) - по своей дате; остальные - по period_to (если задан), иначе по дате создания.
    """
    from django.db.models import Q

    qs = MarketSalaryPayout.objects.filter(company=company, reversed_at__isnull=True)
    if user_ids is not None:
        qs = qs.filter(employee_id__in=list(user_ids))
    sd = start.date() if hasattr(start, "date") else start
    ed = end.date() if hasattr(end, "date") else end
    return qs.filter(
        Q(date__isnull=False, date__gte=sd, date__lt=ed)
        | Q(date__isnull=True, period_to__isnull=False, period_to__gte=sd, period_to__lt=ed)
        | Q(date__isnull=True, period_to__isnull=True, created_at__gte=start, created_at__lt=end)
    )


def salary_payout_totals(company, start, end, user_ids=None):
    """
    ТЗ Н-18 / Б-63: суммы записей по сотрудникам за период аналитики [start, end). Отменённые не считаются.
    -> {str(user_id): {"salary","advance","bonus","deduction","fine"}} (Decimal)
    """
    out = {}
    for p in payout_queryset(company, start, end, user_ids):
        row = out.setdefault(
            str(p.employee_id), {k: Decimal("0.00") for k in ("salary", "advance", "bonus", "deduction", "fine")}
        )
        row[p.kind] += p.amount
    return out


def _fine_warning(request, company, employee, payout):
    """fine_exceeds_accrued - предупреждение (не отказ): штрафы за период больше начисленного + премий."""
    try:
        import calendar
        from datetime import datetime, time, timedelta

        from apps.main.analytics_market import Period
        from apps.main.tz26.staff.salary import employee_row

        d = payout.date or timezone.localdate()
        a = payout.period_from or d.replace(day=1)
        b = payout.period_to or d.replace(day=calendar.monthrange(d.year, d.month)[1])
        tz = timezone.get_current_timezone()
        period = Period(
            start=timezone.make_aware(datetime.combine(a, time.min), tz),
            end=timezone.make_aware(datetime.combine(b + timedelta(days=1), time.min), tz),
        )
        row = employee_row(request, company, employee, period)
        if Decimal(row["fines"]) > Decimal(row["accrued"]) + Decimal(row["bonuses"]):
            return {
                "code": "fine_exceeds_accrued",
                "detail": "Штрафы за период больше начисленного.",
                "data": {"accrued": row["accrued"], "fines": row["fines"]},
            }
    except Exception:
        return None
    return None


class _Pagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200


@read_only_guard("can_edit_salary")
class MarketSalaryPayoutListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company_of(request.user)
        if company is None:
            return _err("permission_denied", "Пользователь не привязан к компании.", 403)
        qs = MarketSalaryPayout.objects.filter(company=company).select_related("employee")
        if not staff_perms.can_view_salary(request.user):  # сотрудник видит только свои
            qs = qs.filter(employee=request.user)
        q = request.query_params
        if q.get("employee"):
            try:
                qs = qs.filter(employee_id=uuid.UUID(str(q["employee"])))
            except (ValueError, TypeError):
                return _err("employee_not_found", "Сотрудник не найден.", 404)
        if q.get("kind"):
            qs = qs.filter(kind=q["kind"])
        df = parse_date(q.get("date_from") or "")
        dt = parse_date(q.get("date_to") or "")
        from django.db.models import Q

        if df:
            qs = qs.filter(Q(date__gte=df) | Q(date__isnull=True, created_at__date__gte=df))
        if dt:
            qs = qs.filter(Q(date__lte=dt) | Q(date__isnull=True, created_at__date__lte=dt))
        pager = _Pagination()
        page = pager.paginate_queryset(qs.order_by("-created_at"), request, view=self)
        return pager.get_paginated_response([serialize_payout(p) for p in page])

    @idempotent(body_key=True, replay_flag=True)
    def post(self, request):
        user = request.user
        company = _company_of(user)
        if company is None or not staff_perms.can_edit_salary(user):
            return Response(
                {"detail": "Нет доступа: зарплата.", "code": "permission_denied", "required": "can_edit_salary"}, status=403
            )
        d = request.data

        try:
            employee = User.objects.filter(id=d.get("employee"), company=company).first()
        except Exception:  # не uuid
            employee = None
        if employee is None:
            return _err("employee_not_found", "Сотрудник не найден.", 404)

        kind = str(d.get("kind") or "salary").strip().lower()
        if kind not in MarketSalaryPayout.Kind.values:
            return _err("kind_invalid", "kind: salary | advance | bonus | fine | deduction.")
        try:
            amount = Decimal(str(d.get("amount"))).quantize(_Q2, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            amount = Decimal("0")
        if not amount.is_finite() or amount <= 0:
            return _err("amount_invalid", "Сумма должна быть больше нуля.")

        if str(d.get("source") or "").strip().lower() == "owner":
            return _err(
                "owner_source_not_supported",
                "Выплата «из кармана владельца» пока не поддержана: внесите вложение владельца и выплатите из кассы.",
            )

        pf = parse_date(str(d.get("period_from") or "")) if d.get("period_from") else None
        pt = parse_date(str(d.get("period_to") or "")) if d.get("period_to") else None
        if (d.get("period_from") and pf is None) or (d.get("period_to") and pt is None) or (pf and pt and pf > pt):
            return _err("period_invalid", "Период указан неверно.")

        # Б-63: штраф и премия - записи начисления с датой, денег не двигают (как и удержание)
        rec_date = None
        if d.get("date"):
            rec_date = parse_date(str(d.get("date")))
            if rec_date is None:
                return _err("period_invalid", "Дата указана неверно.")
        elif kind in (MarketSalaryPayout.Kind.FINE, MarketSalaryPayout.Kind.BONUS):
            rec_date = timezone.localdate()

        method = str(d.get("method") or "").strip().lower()[:32]
        comment = str(d.get("comment") or d.get("reason") or "").strip()[:255]
        pays_money = kind in (MarketSalaryPayout.Kind.SALARY, MarketSalaryPayout.Kind.ADVANCE)

        from apps.construction.models import Cashbox

        cashbox = None
        raw_cb = d.get("cashbox") or d.get("cashbox_id")
        if raw_cb:
            try:
                cashbox = Cashbox.objects.filter(id=raw_cb, company=company).first()
            except Exception:
                cashbox = None
            if cashbox is None:
                return _err("cashbox_not_found", "Касса не найдена.", 404)

        shift = None
        if d.get("shift") or d.get("shift_id"):
            from apps.main.views import _receipt_drawer_shift
            from rest_framework.exceptions import PermissionDenied, ValidationError

            try:
                shift = _receipt_drawer_shift(request, company)
            except ValidationError as e:
                det = e.detail if isinstance(e.detail, dict) else {}
                code = str(det.get("code") or "shift_closed")
                return _err("shift_closed" if code == "shift_closed" else code, str(det.get("detail") or "Смена недоступна."))
            except PermissionDenied:
                return _err("permission_denied", "Это смена другого кассира.", 403)
            if cashbox is not None and cashbox.id != shift.cashbox_id:
                return _err("cashbox_not_found", "Касса не относится к этой смене.")
            cashbox = shift.cashbox

        with transaction.atomic():
            payout = MarketSalaryPayout.objects.create(
                company=company, employee=employee, kind=kind, amount=amount,
                period_from=pf, period_to=pt, date=rec_date, method=method, cashbox=cashbox if pays_money else None,
                shift=shift if pays_money else None, comment=comment, created_by=user,
            )
            if pays_money:
                from apps.construction.auto_cashflow import create_auto_cashflow
                from apps.construction.categories import get_default_category
                from apps.construction.models import CashFlow

                label = dict(MarketSalaryPayout.Kind.choices)[kind]
                tag = f" [#{employee.id} {pf or ''}–{pt or ''}]" if (pf or pt) else ""
                cf = create_auto_cashflow(
                    company=company,
                    branch=getattr(shift, "branch", None) if shift else None,
                    cashbox=cashbox,
                    cashbox_id=str(cashbox.id) if cashbox else None,
                    cashbox_role="expense_variable",
                    user=user,
                    shift=shift,
                    type=CashFlow.Type.EXPENSE,
                    amount=amount,
                    source_kind=CashFlow.SourceKind.SALARY_PAYOUT,
                    source_id=str(payout.id),
                    name=f"Зарплата ({label.lower()}): {_user_display_name(employee)}{tag}"[:255],
                    source_business_operation_id="Зарплата",
                    category=get_default_category(company, "salary"),
                    affects_shift_drawer=bool(shift is not None and method in ("", "cash")),
                    payment_method=method or None,
                )
                payout.cashflow = cf
                if cf is not None and payout.cashbox_id is None:
                    payout.cashbox = cf.cashbox
                payout.save(update_fields=["cashflow", "cashbox"])

        from apps.main.cache_utils import invalidate_cache_pattern

        invalidate_cache_pattern(f"analytics:market:{company.id}:")
        body = serialize_payout(payout)
        if kind == MarketSalaryPayout.Kind.FINE:
            warn = _fine_warning(request, company, employee, payout)
            if warn:
                body["warnings"] = [warn]
        return Response(body, status=status.HTTP_201_CREATED)


@read_only_guard("can_edit_salary")
class MarketSalaryPayoutDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk):
        user = request.user
        company = _company_of(user)
        if company is None or not staff_perms.can_edit_salary(user):
            return Response(
                {"detail": "Нет доступа: зарплата.", "code": "permission_denied", "required": "can_edit_salary"}, status=403
            )
        with transaction.atomic():
            payout = (
                MarketSalaryPayout.objects.select_for_update()
                .select_related("employee", "cashflow", "shift")
                .filter(company=company, id=pk).first()
            )
            if payout is None:
                return _err("payout_not_found", "Выплата не найдена.", 404)
            if payout.reversed_at is not None:
                return _err("already_reversed", "Выплата уже отменена.", 409)
            if payout.cashflow_id:
                from apps.construction.auto_cashflow import create_auto_cashflow
                from apps.construction.models import CashFlow, CashShift

                # смена ещё открыта — возврат в её ящик, иначе просто приход на кассу
                shift = payout.shift if (payout.shift and payout.shift.status == CashShift.Status.OPEN) else None
                rev = create_auto_cashflow(
                    company=company,
                    branch=payout.cashflow.branch,
                    cashbox=payout.cashflow.cashbox,
                    user=user,
                    shift=shift,
                    type=CashFlow.Type.INCOME,
                    amount=payout.amount,
                    source_kind=CashFlow.SourceKind.SALARY_PAYOUT,
                    source_id=f"{payout.id}:reversal",
                    name=f"Отмена выплаты: {payout.cashflow.name or ''}"[:255],
                    source_business_operation_id="Зарплата",
                    category=payout.cashflow.category,
                    affects_shift_drawer=bool(shift is not None and payout.cashflow.affects_shift_drawer),
                    payment_method=payout.cashflow.payment_method,
                )
                payout.reversal_cashflow = rev
            payout.reversed_at = timezone.now()
            payout.save(update_fields=["reversed_at", "reversal_cashflow"])

        from apps.main.cache_utils import invalidate_cache_pattern

        invalidate_cache_pattern(f"analytics:market:{company.id}:")
        return Response(serialize_payout(payout))
