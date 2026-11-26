"""П1-6 К-6 (15.11.2026, k6_1511): серийные номера в приходе.

POST suppliers/<id>/receipt/: строка принимает serial_numbers: ["SN1", ...] (общий формат с продажей, sn1311).
Проверки (флаг компании electronics_2026_11 + новая касса, товар с track_serials):
- 400 serial_count_mismatch {index, count, quantity} — номеров не столько, сколько штук в строке;
- 409 serial_in_stock {index, serial, product_id} — номер уже числится на складе (пришёл и не продан);
- serial_duplicate_in_line, serial_too_long — как в продаже.
Без флага номера просто сохраняются. Ответ прихода: serials_saved — сколько номеров записано.
GET stock/serials/?product=&serial=&status=in_stock|sold&page=&page_size= — номера с прихода и их статус.
"""
from decimal import Decimal

from django.db.models import Exists, OuterRef
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ApiError


def _sold_subquery():
    from apps.main.models import SoldSerial

    return SoldSerial.objects.filter(company_id=OuterRef("company_id"), serial_norm=OuterRef("serial_norm"),
                                     returned_at__isnull=True)


def record(request, company, receipt, receipt_items, *, received_at=None):
    from apps.main.models import StockSerial
    from apps.main.serials import SerialError, is_electronics_strict, norm, parse_serials

    raw = request.data.get("items") if hasattr(request.data, "get") else None
    if not isinstance(raw, list):
        return 0
    strict = is_electronics_strict(request, company)
    rows, seen = [], set()
    for idx, (row, ri) in enumerate(zip(raw, receipt_items)):
        if not isinstance(row, dict):
            continue
        try:
            serials = parse_serials(row)
        except SerialError as e:
            raise ApiError(e.code, e.detail, data={"index": idx, **e.extra})
        if not serials:
            continue
        if strict and getattr(ri.product, "track_serials", False):
            qty = Decimal(str(ri.qty))
            if Decimal(len(serials)) != qty:
                raise ApiError("serial_count_mismatch", f"Строка {idx + 1}: серийных номеров {len(serials)}, а количество {qty}.",
                               data={"index": idx, "count": len(serials), "quantity": str(qty)})
            busy = (StockSerial.objects.filter(company=company, serial_norm__in=[norm(s) for s in serials])
                    .exclude(Exists(_sold_subquery())).values_list("serial", "product_id").first())
            if busy:
                raise ApiError("serial_in_stock", f"Серийный номер {busy[0]} уже числится на складе.", status_code=409,
                               data={"index": idx, "serial": busy[0], "product_id": str(busy[1])})
        for s in serials:
            n = norm(s)
            if (ri.product_id, n) in seen:
                if strict:
                    raise ApiError("serial_duplicate_in_line", f"Серийный номер {s} повторяется в приходе.",
                                   data={"index": idx, "serial": s})
                continue
            seen.add((ri.product_id, n))
            rows.append(StockSerial(company=company, product_id=ri.product_id, serial=s, serial_norm=n, receipt=receipt,
                                    receipt_item=ri, received_at=received_at or receipt.created_at or timezone.now()))
    if rows:
        StockSerial.objects.bulk_create(rows)
    return len(rows)


class StockSerialsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import StockSerial
        from apps.main.serials import norm

        company = getattr(request.user, "owned_company", None) or getattr(request.user, "company", None)
        qp = request.query_params
        qs = (StockSerial.objects.filter(company=company).annotate(sold=Exists(_sold_subquery()))
              .select_related("product").order_by("-received_at"))
        if qp.get("product"):
            qs = qs.filter(product_id=qp["product"])
        if qp.get("serial"):
            s = norm(qp["serial"])
            qs = qs.filter(serial_norm__endswith=s) if len(s) >= 4 else qs.filter(serial_norm=s)
        st = (qp.get("status") or "").strip()
        if st == "in_stock":
            qs = qs.filter(sold=False)
        elif st == "sold":
            qs = qs.filter(sold=True)
        try:
            page = max(1, int(qp.get("page") or 1))
            size = min(500, max(1, int(qp.get("page_size") or 100)))
        except ValueError:
            return Response({"code": "validation_error", "detail": "page и page_size — целые числа."}, status=400)
        total = qs.count()
        return Response({"count": total, "page": page, "page_size": size, "results": [{
            "serial": r.serial, "product_id": str(r.product_id), "name": r.product.name,
            "status": "sold" if r.sold else "in_stock", "receipt_id": str(r.receipt_id) if r.receipt_id else None,
            "received_at": r.received_at.isoformat(),
        } for r in qs[(page - 1) * size: page * size]]})
