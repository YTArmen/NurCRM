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


class SaleItemsPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 500


def _user_name(u) -> str | None:
    if u is None:
        return None
    return (getattr(u, "get_full_name", lambda: "")() or getattr(u, "email", None) or getattr(u, "username", None)) or None


class SaleItemsListAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/pos/sale-items/?date_from&date_to&branch&cashier&product&page&page_size

    Строки чеков постранично (page_size до 500) — для отчётов без запроса на каждый чек.
    Область видимости та же, что у списка чеков pos/sales/ (компания + активный филиал).
    Дополнительные фильтры: shift, cashbox, status (через запятую), payment_method.
    Открытые корзины (status=new) не попадают. date_from/date_to — по created_at чека,
    сутки Asia/Bishkek, включительно.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qp = request.query_params
        sales = self._filter_qs_company_branch(Sale.objects.all()).exclude(status=Sale.Status.NEW)
        start, end = day_bounds((qp.get("date_from") or "").strip(), (qp.get("date_to") or "").strip())
        if start:
            sales = sales.filter(created_at__gte=start)
        if end:
            sales = sales.filter(created_at__lte=end)
        for param, lookup in (("cashier", "user_id"), ("shift", "shift_id"), ("cashbox", "cashbox_id"),
                              ("payment_method", "payment_method")):
            val = (qp.get(param) or "").strip()
            if val:
                sales = sales.filter(**{lookup: val})
        statuses = [s.strip() for s in (qp.get("status") or "").split(",") if s.strip()]
        if statuses:
            sales = sales.filter(status__in=statuses)

        qs = SaleItem.objects.filter(sale__in=sales.values("pk"))
        product = (qp.get("product") or "").strip()
        if product:
            qs = qs.filter(product_id=product)
        qs = qs.select_related("sale", "sale__user", "product").order_by("-sale__created_at", "-sale_id", "id")

        paginator = SaleItemsPagination()
        page = paginator.paginate_queryset(qs, request, view=self)

        from .models import SalePackLine

        sale_ids = {it.sale_id for it in page}
        returned = self._returned_qty(sale_ids)
        packs = {}
        for ln in SalePackLine.objects.filter(sale_id__in=sale_ids):
            packs.setdefault((ln.sale_id, ln.product_id, ln.variant_id), []).append(ln)

        rows = []
        for it in page:
            sale = it.sale
            price = it.unit_price or Decimal("0")
            disc = it.line_discount or Decimal("0")
            row = {
                "id": str(it.id),
                "sale_id": str(sale.id),
                "number": sale.doc_number,
                "date": (sale.paid_at or sale.created_at).isoformat(),
                "created_at": sale.created_at.isoformat(),
                "cashier_id": str(sale.user_id) if sale.user_id else None,
                "cashier": _user_name(sale.user),
                "product_id": str(it.product_id) if it.product_id else None,
                "variant_id": str(it.variant_id) if it.variant_id else None,
                "name": it.name_snapshot,
                "barcode": it.barcode_snapshot,
                "qty": str(it.quantity),
                "price": str(price.quantize(Decimal("0.01"))),
                "discount": str(disc.quantize(Decimal("0.01"))),
                "sum": str(((price * it.quantity) - disc).quantize(Decimal("0.01"))),
                "payment_method": sale.payment_method,
                "sale_status": sale.status,
                "sale_package_id": str(it.sale_package_id) if it.sale_package_id else None,
                "returned_qty": str(returned.get(it.id, Decimal("0")).quantize(Decimal("0.001"))),
            }
            lns = packs.get((it.sale_id, it.product_id, it.variant_id))
            if lns:
                first = lns[0]
                row["sale_package_id"] = str(first.package_id) if first.package_id else None
                row["package_name"] = first.package_name
                row["packs_qty"] = str(sum((l.packs for l in lns), Decimal("0")))
                row["units_per_pack"] = str(first.units_per_pack)
            rows.append(row)
        return paginator.get_paginated_response(rows)

    @staticmethod
    def _returned_qty(sale_ids) -> dict:
        from apps.main.models import SaleReturn

        out: dict = {}
        if not sale_ids:
            return out
        for lines in SaleReturn.objects.filter(sale_id__in=sale_ids).values_list("returned_items", flat=True):
            for ln in lines or []:
                sid, q = ln.get("sale_item"), ln.get("qty")
                if not sid or q is None:
                    continue
                try:
                    import uuid as _uuid

                    key = _uuid.UUID(str(sid))
                    out[key] = out.get(key, Decimal("0")) + Decimal(str(q))
                except Exception:  # noqa: BLE001
                    continue
        return out
