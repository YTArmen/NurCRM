"""Б-63: GET api/main/market-salary/statement/?employee=&period_start=&period_end= — выписка по сотруднику."""
import uuid

from django.contrib.auth import get_user_model
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.analytics_market import _get_period
from core.exceptions import ApiError

from . import perms
from .salary import build_statement

User = get_user_model()


class MarketSalaryStatementView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        company = perms.company_of(user)
        if company is None:
            raise ApiError("permission_denied", "Пользователь не привязан к компании.", status_code=403)
        raw = (request.query_params.get("employee") or "").strip()
        can_view = perms.can_view_salary(user)
        if not raw or raw == str(user.id):
            employee = user
        else:
            if not can_view:
                raise ApiError("permission_denied", "Нет доступа: зарплата.", status_code=403,
                               top={"required": "can_view_salary"})
            try:
                employee = User.objects.filter(id=uuid.UUID(raw), company=company).first()
            except ValueError:
                employee = None
            if employee is None:
                raise ApiError("employee_not_found", "Сотрудник не найден.", status_code=404)
        period = _get_period(request)
        return Response(build_statement(request, company, employee, period))
