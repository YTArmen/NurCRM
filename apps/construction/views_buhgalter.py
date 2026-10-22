"""Эндпоинты ТЗ «Бухгалтер»: остатки касс на дату, отметка «проверено».

Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.construction.models import Cashbox, CashFlow
from apps.construction.views import CompanyBranchScopedMixin

ZERO = Decimal("0.00")


def _day_start(d):
    return timezone.make_aware(datetime.combine(d, time.min))


class CashboxBalancesView(CompanyBranchScopedMixin, APIView):
    """
    GET /api/construction/cashboxes/balances/?date_from=&date_to=&branch=
    → [{cashbox_id, name, opening, income, expense, closing}]
    Считаются одобренные движения без строк заявок (как баланс кассы).
    Период включает обе даты; opening — остаток на начало date_from.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qp = request.query_params
        d_from, d_to = parse_date(qp.get("date_from") or ""), parse_date(qp.get("date_to") or "")
        if not d_from or not d_to:
            raise ValidationError({"detail": "Укажите date_from и date_to (YYYY-MM-DD)."})
        if d_to < d_from:
            raise ValidationError({"detail": "date_to раньше date_from."})
        start, end = _day_start(d_from), _day_start(d_to + timedelta(days=1))

        company = self._company()
        boxes = Cashbox.objects.filter(company=company)
        branch = qp.get("branch")
        if branch:
            boxes = boxes.filter(branch_id=branch)
        else:
            boxes = self._scoped_queryset(boxes)

        base = CashFlow.objects.filter(
            company=company, cashbox__in=boxes, status=CashFlow.Status.APPROVED, request_kind__isnull=True,
            created_at__lt=end,
        )
        rows = {}
        for r in base.values("cashbox_id").annotate(
            opening_inc=Sum("amount", filter=Q(type="income", created_at__lt=start)),
            opening_exp=Sum("amount", filter=Q(type="expense", created_at__lt=start)),
            inc=Sum("amount", filter=Q(type="income", created_at__gte=start)),
            exp=Sum("amount", filter=Q(type="expense", created_at__gte=start)),
        ):
            rows[r["cashbox_id"]] = r

        out = []
        for box in boxes.order_by("name"):
            r = rows.get(box.id) or {}
            opening = (r.get("opening_inc") or ZERO) - (r.get("opening_exp") or ZERO)
            inc, exp = r.get("inc") or ZERO, r.get("exp") or ZERO
            if not box.is_active and not (opening or inc or exp):
                continue
            out.append({
                "cashbox_id": str(box.id),
                "name": box.name or (f"Касса филиала {box.branch.name}" if box.branch_id else "Касса"),
                "opening": f"{opening:.2f}",
                "income": f"{inc:.2f}",
                "expense": f"{exp:.2f}",
                "closing": f"{(opening + inc - exp):.2f}",
            })
        return Response(out)


class CashFlowCheckView(CompanyBranchScopedMixin, APIView):
    """
    POST /api/construction/cashflows/<id>/check/  {checked: true|false, note: "..."}
    Ставит/снимает отметку «проверено». Сумму не меняет, can_edit_cashbox не нужен,
    нужен просмотр касс; в закрытом периоде разрешено.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        user = request.user
        if not (
            getattr(user, "is_superuser", False)
            or getattr(user, "role", None) in ("owner", "admin")
            or getattr(user, "owned_company_id", None)
            or getattr(user, "can_view_cashbox", False)
        ):
            raise PermissionDenied({"detail": "Нет доступа к кассам.", "code": "permission_denied"})
        flow = self._scoped_queryset(CashFlow.objects.all()).filter(id=pk).first()
        if flow is None:
            raise NotFound({"detail": "Движение кассы не найдено."})
        raw = request.data.get("checked", True)
        checked = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("true", "1", "yes")
        fields = {"checked_at": timezone.now() if checked else None, "checked_by": user if checked else None}
        if "note" in request.data:
            fields["accountant_note"] = str(request.data.get("note") or "")[:2000]
        CashFlow.objects.filter(pk=flow.pk).update(**fields)  # update(): деньги и валидации не трогаем
        flow.refresh_from_db()
        return Response({
            "id": str(flow.id),
            "checked": flow.checked_at is not None,
            "checked_at": flow.checked_at.isoformat() if flow.checked_at else None,
            "checked_by": str(flow.checked_by_id) if flow.checked_by_id else None,
            "accountant_note": flow.accountant_note,
        })


class AuditLogView(CompanyBranchScopedMixin, APIView):
    """
    GET /api/construction/audit-log/?object_type=cashflow&object_id=&action=&user=&date_from=&date_to=&page=
    Журнал денежных операций (только чтение). Видят владелец/админ и те, кому открыт просмотр касс.
    """

    permission_classes = [permissions.IsAuthenticated]
    PAGE = 50

    def get(self, request, *args, **kwargs):
        from apps.construction.models import AuditLog

        user = request.user
        if not (
            getattr(user, "is_superuser", False)
            or getattr(user, "role", None) in ("owner", "admin")
            or getattr(user, "owned_company_id", None)
            or getattr(user, "can_view_cashbox", False)
        ):
            raise PermissionDenied({"detail": "Нет доступа к журналу.", "code": "permission_denied"})
        company = self._company()
        if company is None:
            raise PermissionDenied("У пользователя не настроена компания.")
        qs = AuditLog.objects.filter(company=company).select_related("user")
        q = request.query_params
        # те, кому открыты только кассы, видят лишь денежные записи; остальной журнал — владелец/админ
        full = (
            getattr(user, "is_superuser", False)
            or getattr(user, "role", None) in ("owner", "admin")
            or getattr(user, "owned_company_id", None)
        )
        if not full:
            qs = qs.filter(object_type__in=("cashflow", "period"))
        for param, field in (("object_type", "object_type"), ("object_id", "object_id"), ("action", "action"),
                             ("user", "user_id"), ("branch", "branch_id")):
            if q.get(param):
                if field.endswith("_id") and field != "object_id":
                    try:
                        uuid.UUID(str(q[param]))
                    except ValueError:
                        raise ValidationError({param: "Неверный идентификатор."})
                qs = qs.filter(**{field: q[param]})
        df, dt = parse_date(q.get("date_from") or ""), parse_date(q.get("date_to") or "")
        if df:
            qs = qs.filter(created_at__gte=_day_start(df))
        if dt:
            qs = qs.filter(created_at__lt=_day_start(dt + timedelta(days=1)))
        try:
            page = max(int(q.get("page") or 1), 1)
        except ValueError:
            page = 1
        total = qs.count()
        rows = list(qs[(page - 1) * self.PAGE: page * self.PAGE])
        results = [
            {
                "id": str(r.id), "created_at": r.created_at, "action": r.action,
                "user": str(r.user_id) if r.user_id else None,
                "user_name": (f"{r.user.first_name} {r.user.last_name}".strip() or r.user.email) if r.user else None,
                "object_type": r.object_type, "object_id": r.object_id, "object_name": r.object_name,
                "before": r.before, "after": r.after, "ip": r.ip,
                "branch": str(r.branch_id) if r.branch_id else None,
            }
            for r in rows
        ]
        return Response({"count": total, "page": page, "page_size": self.PAGE, "results": results})
