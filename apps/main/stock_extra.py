"""П1-6 (15.11.2026, p16_1511): склад, вторая очередь — единицы, сроки годности, события остатков.

GET units/ — справочник единиц (код, ОКЕИ, названия на 5 языках, можно ли дробное).
GET stock/expiring/?days=30&product=&page=&page_size= — партии со сроком годности, истекающие за days дней
    (и уже просроченные с остатком), FEFO-порядок: раньше истекает — выше.
События ленты main/changes/ и WebSocket:
- stock_changed {product, quantity, delta, source} — теперь по ВСЕМ изменениям остатка (продажи, приходы, ревизия),
  из журнала остатков (команда stock_ledger_relay, cron раз в минуту: задержка до 1 минуты);
- price_changed {product, price, wholesale_price} — при смене цены товара.
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

UNITS = [
    # code, okei, ru, ky, en, tr, uz, fractional
    ("pcs", "796", "шт", "даана", "pcs", "adet", "dona", False),
    ("kg", "166", "кг", "кг", "kg", "kg", "kg", True),
    ("g", "163", "г", "г", "g", "g", "g", True),
    ("l", "112", "л", "л", "l", "l", "l", True),
    ("m", "006", "м", "м", "m", "m", "m", True),
    ("m2", "055", "м²", "м²", "m²", "m²", "m²", True),
    ("m3", "113", "м³", "м³", "m³", "m³", "m³", True),
    ("t", "168", "т", "т", "t", "ton", "t", True),
    ("pack", "778", "уп", "таңг.", "pack", "paket", "qadoq", False),
    ("box", "778", "кор", "куту", "box", "kutu", "quti", False),
    ("bag", "778", "мешок", "кап", "bag", "çuval", "qop", False),
    ("roll", "736", "рулон", "орам", "roll", "rulo", "rulon", False),
    ("sheet", "796", "лист", "барак", "sheet", "levha", "varaq", False),
    ("set", "839", "компл", "топтом", "set", "takım", "to'plam", False),
    ("pair", "715", "пара", "жуп", "pair", "çift", "juft", False),
]


class UnitsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response([{"code": c, "okei": o, "name_ru": ru, "name_ky": ky, "name_en": en, "name_tr": tr, "name_uz": uz,
                          "fractional": f} for c, o, ru, ky, en, tr, uz, f in UNITS])


class StockExpiringAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import ProductExpiryBatch

        company = getattr(request.user, "owned_company", None) or getattr(request.user, "company", None)
        qp = request.query_params
        try:
            days = min(365, max(0, int(qp.get("days") or 30)))
            page = max(1, int(qp.get("page") or 1))
            size = min(500, max(1, int(qp.get("page_size") or 100)))
        except ValueError:
            return Response({"code": "validation_error", "detail": "days, page, page_size — целые числа."}, status=400)
        from apps.main.stock_batches import batches_on as _k458_on, expiring_from_batches as _k458_exp

        if _k458_on(company):  # К-5 (k458): сроки по партиям
            return Response(_k458_exp(request, company, days, page, size, qp.get("product")))
        today = timezone.localdate()
        qs = (ProductExpiryBatch.objects.filter(company=company, status=ProductExpiryBatch.Status.ACTIVE,
                                                remaining_quantity__gt=0, expires_at__isnull=False,
                                                expires_at__lte=today + timedelta(days=days))
              .select_related("product").order_by("expires_at", "received_at"))
        if qp.get("product"):
            qs = qs.filter(product_id=qp["product"])
        total = qs.count()
        rows = qs[(page - 1) * size: page * size]
        return Response({"days": days, "today": today.isoformat(), "count": total, "page": page, "page_size": size,
                         "results": [{
                             "batch_id": str(b.id), "product_id": str(b.product_id), "name": b.product.name,
                             "remaining": format(b.remaining_quantity, "f"), "expires_at": b.expires_at.isoformat(),
                             "days_left": (b.expires_at - today).days, "expired": b.expires_at < today,
                             "received_at": b.received_at.isoformat(), "source_kind": b.source_kind,
                         } for b in rows]})
