"""ТЗ 31.10 п.11: GET /api/main/pos/sales/items/ — строки чеков пачкой (для отчётов, без запроса на каждый чек).

Права, компания, филиал и фильтры чеков — те же, что у pos/sales/ (date_from, date_to, status, cashbox,
shift, client, ... — берутся из SaleListAPIView). Чеки со статусом «new» (открытые корзины) не отдаются,
если статус не задан явно. Ответ: {count, limit, offset, results:[{sale_id, doc_number, created_at,
product, name, qty, price, discount, total}]}; limit по умолчанию 1000, максимум 5000.
"""
from decimal import Decimal

from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.main.models import Sale, SaleItem
from apps.main.pos_views import SaleListAPIView

DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000


def _int_param(raw, name, default, lo, hi=None):
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: "Ожидается целое число."})
    value = max(lo, value)
    return min(value, hi) if hi else value


def _m(v):
    return f"{Decimal(v or 0):.2f}"


class SaleItemsBatchAPIView(SaleListAPIView):
    def list(self, request, *args, **kwargs):
        qp = request.query_params
        limit = _int_param(qp.get("limit"), "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _int_param(qp.get("offset"), "offset", 0, 0)

        sales = self.filter_queryset(self.get_queryset())
        if not (qp.get("status") or qp.get("statuses")):
            sales = sales.exclude(status=Sale.Status.NEW)
        items = SaleItem.objects.filter(sale_id__in=sales.values("pk"))
        count = items.count()
        rows = (
            items.select_related("sale")
            .order_by("sale__created_at", "sale_id", "id")
            .values("sale_id", "sale__doc_number", "sale__created_at", "product_id", "name_snapshot",
                    "quantity", "unit_price", "line_discount")[offset:offset + limit]
        )
        results = []
        for r in rows:
            qty, price, disc = r["quantity"] or Decimal("0"), r["unit_price"] or Decimal("0"), r["line_discount"] or Decimal("0")
            created = r["sale__created_at"]
            results.append({
                "sale_id": str(r["sale_id"]),
                "doc_number": r["sale__doc_number"],
                "created_at": created.isoformat() if created else None,
                "product": str(r["product_id"]) if r["product_id"] else None,
                "name": r["name_snapshot"],
                "qty": f"{qty:.3f}",
                "price": _m(price),
                "discount": _m(disc),
                "total": _m(qty * price - disc),
            })
        return Response({"count": count, "limit": limit, "offset": offset, "results": results})
