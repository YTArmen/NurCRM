"""Этап 6: аналитика, 5 вкладок (ТЗ §3.8). Раздел `analytics`."""
from datetime import date

from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .permissions import ArendaSection
from .services import analytics as svc
from .views import CtxMixin


class AnalyticsView(CtxMixin, APIView):
    """GET analytics/?tab=overview|revenue|expenses|tenants|units&period=month|quarter|year&date=&branch=
    То же по вкладкам: GET analytics/<tab>/."""

    permission_classes = [IsAuthenticated, ArendaSection("analytics")]

    def get(self, request, tab=None):
        tab = tab or request.query_params.get("tab") or "overview"
        if tab not in svc.TABS:
            raise ValidationError({"tab": [f"Допустимо: {', '.join(svc.TABS)}."]})
        period = request.query_params.get("period") or "month"
        if period not in svc.PERIODS:
            raise ValidationError({"period": [f"Допустимо: {', '.join(svc.PERIODS)}."]})
        raw = request.query_params.get("date")
        try:
            on_date = date.fromisoformat(raw) if raw else self.today()
        except ValueError:
            raise ValidationError({"date": ["Формат даты YYYY-MM-DD."]})
        sc = svc.Scope(self.ctx.company, self.branch_ids(), period, on_date, self.today())
        return Response({"tab": tab, "period": period, "date_from": sc.start, "date_to": sc.end,
                         "branch": request.query_params.get("branch") or "all", **svc.build(tab, sc)})
