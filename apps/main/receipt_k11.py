"""П0-4 К-11 (14.11.2026, k11_1411): приход от поставщика — дубль накладной, режим НДС, файлы при создании, фильтр по времени.

POST suppliers/<id>/receipt/ дополнительно принимает:
- vat_mode: "" | "none" (без НДС) | "included" (НДС в цене) | "on_top" (НДС сверху);
- attachment_ids: [uuid MainMedia своей компании] — файлы накладной сразу при создании;
- allow_duplicate: true — провести, даже если накладная с тем же номером уже есть.
409 receipt_duplicate {receipt_id, number, document_number, document_date} — тот же поставщик и тот же номер накладной
(и дата, если обе указаны); только новым клиентам (kassa/owner ≥ 1.17.61), остальным — как раньше.
- esf_number, esf_received — ЭСФ поставщика (П2-4 Б6).
GET suppliers/receipts/?date_from=&date_to= — дата YYYY-MM-DD или время ISO-8601 с поясом; ?esf=missing|received.
POST suppliers/receipts/<id>/esf/ {esf_number?, esf_received?} — отметить ЭСФ позже (владелец/бухгалтер/закупки).
"""
from datetime import datetime, time

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.exceptions import ApiError

VAT_MODES = ("", "none", "included", "on_top")


def _truthy(v):
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


def vat_mode_of(data):
    v = str((data.get("vat_mode") if hasattr(data, "get") else "") or "").strip()
    if v not in VAT_MODES:
        raise ApiError("validation_error", "vat_mode: none, included или on_top.", fields={"vat_mode": ["none, included, on_top."]})
    return v


def check_duplicate(request, company, supplier, doc_fields):
    from core.api_compat import is_new_client

    num = (doc_fields or {}).get("document_number")
    data = request.data if hasattr(request.data, "get") else {}
    if not num or not is_new_client(request) or _truthy(data.get("allow_duplicate")):
        return
    from apps.main.models import SupplierReceipt

    qs = SupplierReceipt.objects.filter(company=company, supplier=supplier, document_number=num)
    dd = doc_fields.get("document_date")
    if dd is not None:
        from django.db.models import Q

        qs = qs.filter(Q(document_date=dd) | Q(document_date__isnull=True))
    prior = qs.order_by("-created_at").first()
    if prior is not None:
        raise ApiError("receipt_duplicate", f"Накладная № {num} этого поставщика уже оприходована.", status_code=409,
                       data={"receipt_id": str(prior.id), "number": prior.number, "document_number": prior.document_number,
                             "document_date": prior.document_date.isoformat() if prior.document_date else None,
                             "confirm": "allow_duplicate=true"})


def attach(receipt, company, data):
    raw = data.get("attachment_ids") if hasattr(data, "get") else None
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ApiError("validation_error", "attachment_ids — список.", fields={"attachment_ids": ["Список id файлов."]})
    from apps.main.models import MainMedia

    try:
        media = list(MainMedia.objects.filter(company=company, id__in=[str(x) for x in raw]))
    except Exception:  # noqa: BLE001 — неверный uuid
        media = []
    found = {str(m.id) for m in media}
    missing = [str(x) for x in raw if str(x) not in found]
    if missing:
        raise ApiError("validation_error", "Файлы не найдены.", fields={"attachment_ids": missing})
    receipt.attachments.add(*media)
    return sorted(found)


def time_bound(raw, *, end):
    """'YYYY-MM-DD' -> начало/конец дня (часовой пояс сервера); ISO-время -> как есть. None — не фильтровать."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if len(raw) == 10:
        d = parse_date(raw)
        if d is None:
            return None
        return timezone.make_aware(datetime.combine(d, time.max if end else time.min))
    dt = parse_datetime(raw.replace(" ", "+"))
    if dt is None:
        return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def esf_fields(data):
    out = {}
    if not hasattr(data, "get"):
        return out
    if data.get("esf_number") not in (None, ""):
        out["esf_number"] = str(data["esf_number"]).strip()[:64]
    if "esf_received" in data:
        out["esf_received"] = _truthy(data.get("esf_received"))
    elif out.get("esf_number"):
        out["esf_received"] = True
    return out


def esf_filter(qs, qp):
    v = (qp.get("esf") or "").strip().lower()
    if v == "missing":
        return qs.filter(esf_received=False)
    if v == "received":
        return qs.filter(esf_received=True)
    return qs


from rest_framework import permissions  # noqa: E402
from rest_framework.response import Response  # noqa: E402
from rest_framework.views import APIView  # noqa: E402


class ReceiptEsfAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from apps.main.models import SupplierReceipt
        from apps.main.pos_serializers import _is_owner_like

        u = request.user
        if not (_is_owner_like(u) or getattr(u, "can_edit_procurement", None) or getattr(u, "can_view_products", False)):
            return Response({"code": "permission_denied", "detail": "Нет права менять приходы.",
                             "permission": "can_edit_procurement"}, status=403)
        company = getattr(u, "owned_company", None) or getattr(u, "company", None)
        r = SupplierReceipt.objects.filter(pk=pk, company=company).first()
        if r is None:
            return Response({"code": "not_found", "detail": "Приход не найден."}, status=404)
        f = esf_fields(request.data)
        if not f:
            return Response({"code": "validation_error", "detail": "Передайте esf_number и/или esf_received."}, status=400)
        for k, v in f.items():
            setattr(r, k, v)
        r.save(update_fields=list(f))
        return Response({"id": str(r.id), "esf_number": r.esf_number or None, "esf_received": r.esf_received})
