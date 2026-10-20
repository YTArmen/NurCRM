"""Эндпоинты ТЗ «Бухгалтер»: остатки касс на дату, отметка «проверено»."""
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
