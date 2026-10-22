"""
Чеки для веб-кассы и отчётов (ТЗ веб-кассы, пункты 4 и 5).

- SaleListWithDatesAPIView — GET main/pos/sales/: date_from/date_to (YYYY-MM-DD, включительно,
  Asia/Bishkek) вместе с count. Самодостаточно: не правит pos_views, а превращает голые даты
  в границы суток до вызова штатного списка. Исправление «date_to теряет последний день»
  в pos_views (другая ветка) этому не мешает.
- SaleItemsListAPIView — GET main/pos/sale-items/: строки чеков постранично.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Sale, SaleItem
from apps.main.pos_views import MarketCashierOnlyMixin, SaleListAPIView
from apps.main.views import CompanyBranchRestrictedMixin
from core.exceptions import ApiError

BISHKEK = dt_timezone(timedelta(hours=6))  # Asia/Bishkek без перехода на летнее время


def parse_day(raw: str, name: str) -> date:
    d = parse_date(raw) if raw and "T" not in raw and " " not in raw else None
    if d is None:
        raise ApiError("validation_error", f"{name}: ожидается дата YYYY-MM-DD.", fields={name: ["Ожидается YYYY-MM-DD."]})
    return d


def day_bounds(date_from: str | None, date_to: str | None):
    """(начало, конец включительно) в Asia/Bishkek; None, если граница не задана."""
    start = end = None
    if date_from:
        start = datetime.combine(parse_day(date_from, "date_from"), time.min, tzinfo=BISHKEK)
    if date_to:
        end = datetime.combine(parse_day(date_to, "date_to"), time.max, tzinfo=BISHKEK)
    return start, end


class SaleListWithDatesAPIView(SaleListAPIView):
    """GET /api/main/pos/sales/ — как раньше; date_from/date_to — по суткам Asia/Bishkek, включительно."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        get = request._request.GET.copy()
        changed = False
        for name, end in (("date_from", False), ("date_to", True), ("start", False), ("end", True)):
            raw = (get.get(name) or "").strip()
            if raw and "T" not in raw and " " not in raw:
                day = parse_day(raw, name)
                t = time.max if end else time.min
                get[name] = datetime.combine(day, t, tzinfo=BISHKEK).isoformat()
                changed = True
        if changed:
            request._request.GET = get
