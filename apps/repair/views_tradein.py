"""П2-1 (15.11.2026, p2115): trade-in / скупка. Адреса — api/main/trade-in/… (см. urls.py).

Решения владельца 15.11: зачёт в покупку — способ оплаты trade_in_credit (выручка чека полная, в ящик не попадает);
паспорт хранится зашифрованно 1 год и виден только с правом trade_in.view_documents; фото — файлами, до 5.
Инвариант: paid_out + credit_confirmed + credit_pending ≤ amount.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.repair.models import TradeIn, TradeInPayout, TradeInPhoto
from apps.repair.perms import company_of, err, guard, perms_of
from apps.repair.services import Z, cash_move, hist, lock_company, money, next_number, norm_serial, open_shift

MAX_PHOTOS = 5
DOC_DAYS = 365
FIELDS = ("customer_name", "customer_phone", "device", "serial", "condition")


def _enc(doc):
    from apps.ekassa.crypto import encrypt_secret

    return encrypt_secret(json.dumps(doc, ensure_ascii=False)) if doc else ""


def _dec(s):
    from apps.ekassa.crypto import decrypt_secret

    try:
        return json.loads(decrypt_secret(s)) if s else None
    except Exception:  # noqa: BLE001
        return None


def payload(t, request=None, with_doc=False):
    out = {
        "id": str(t.id), "number": t.number, "number_display": f"ТИ-{t.number}", "local_number": t.local_number or None,
        "status": t.status, "client": str(t.client_id) if t.client_id else None, "customer_name": t.customer_name,
        "customer_phone": t.customer_phone, "device": t.device, "serial": t.serial, "condition": t.condition,
        "amount": format(t.amount, "f"), "paid_out": format(t.paid_out, "f"), "credit_confirmed": format(t.credit_confirmed, "f"),
        "credit_pending": format(t.credit_pending, "f"), "available": format(t.available, "f"),
        "product": str(t.product_id) if t.product_id else None, "has_document": bool(t.document_enc),
        "document_expires_at": t.document_expires_at.isoformat() if t.document_expires_at else None,
        "photos": [{"id": p.pk, "url": request.build_absolute_uri(p.file.url) if request else p.file.url} for p in t.photos.all()],
        "credits": [{"sale": str(c.sale_id), "amount": format(c.amount, "f")} for c in t.credits.all()],
        "created_at": t.created_at.isoformat() if t.created_at else None, "history": t.history,
    }
    if with_doc:
        out["document"] = _dec(t.document_enc)
    return out


def _refresh_status(t):
    if t.status in ("cancelled", "stocked"):
        return
    t.status = "settled" if t.available <= 0 and t.credit_pending <= 0 else "open"


def _fill(t, d, create=False):
    for f in FIELDS:
        if f in d:
            setattr(t, f, str(d.get(f) or "").strip()[:255 if f != "condition" else 4000])
    if create and not (t.device or "").strip():
        return err("validation_error", "Укажите устройство (device).", fields={"device": ["Обязательно."]})
    t.serial_norm = norm_serial(t.serial)
    if create or "amount" in d:
        v = money(d.get("amount"))
        if v is None:
            return err("validation_error", "amount: больше 0.", fields={"amount": ["Больше 0."]})
        if not create and v < (t.paid_out + t.credit_confirmed + t.credit_pending):
            return err("amount_below_used", "Сумма меньше уже выплаченного и зачтённого.", 409)
        t.amount = v
    if "client" in d:
        from apps.main.models import Client

        t.client = Client.objects.filter(company_id=t.company_id, pk=d.get("client")).first() if d.get("client") else None
    if isinstance(d.get("document"), dict) and d.get("document"):
        doc = {k: str(v)[:128] for k, v in d["document"].items() if k in ("type", "number", "issued_by", "issued_at", "full_name", "birth_date", "inn")}
        t.document_enc = _enc(doc)
        t.document_expires_at = timezone.now() + timedelta(days=DOC_DAYS)
    return None


def _get(request, pk):
    return get_object_or_404(TradeIn.objects.prefetch_related("photos", "credits"), pk=pk, company=company_of(request.user))


def _feed(t, user):
    try:
        from apps.main.change_feed import record_change

        record_change(t.company_id, "trade_in_changed", actor_id=getattr(user, "pk", None), entity="trade_in", entity_id=t.pk,
                      data={"trade_in": str(t.pk), "number": t.number, "status": t.status, "available": format(t.available, "f")})
    except Exception:  # noqa: BLE001
        pass


class TradeInsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        bad = guard(request, "trade_in", "trade_in.manage")
        if bad:
            return bad
        qp = request.query_params
        qs = TradeIn.objects.filter(company=company_of(request.user)).prefetch_related("photos", "credits")
        if qp.get("status"):
            qs = qs.filter(status__in=[s for s in qp["status"].split(",") if s])
        if qp.get("available") in ("1", "true"):
            qs = qs.filter(status="open")
        q = (qp.get("q") or "").strip()
        if q:
            ns = norm_serial(q)
            cond = Q(customer_name__icontains=q) | Q(customer_phone__icontains=q) | Q(device__icontains=q)
            if q.isdigit():
                cond |= Q(number=int(q))
            if len(ns) >= 4:
                cond |= Q(serial_norm__endswith=ns)
            qs = qs.filter(cond)
        d1, d2 = parse_date(qp.get("from") or ""), parse_date(qp.get("to") or "")
        if d1:
            qs = qs.filter(created_at__date__gte=d1)
        if d2:
            qs = qs.filter(created_at__date__lte=d2)
        return Response([payload(t, request) for t in qs[:200]])

    def post(self, request):
        bad = guard(request, "trade_in", "trade_in.manage")
        if bad:
            return bad
        company = company_of(request.user)
        key = (request.headers.get("Idempotency-Key") or "").strip()[:128]
        if not key:
            return err("idempotency_key_required", "Нужен заголовок Idempotency-Key.")
        old = TradeIn.objects.filter(company=company, idempotency_key=key).first()
        if old:
            return Response({**payload(old, request), "replayed": True})
        d = request.data if hasattr(request.data, "get") else {}
        with transaction.atomic():
            lock_company(company)
            t = TradeIn(company=company, created_by=request.user, idempotency_key=key)
            t.branch = getattr(open_shift(request.user, company), "branch", None)
            bad = _fill(t, d, create=True)
            if bad:
                return bad
            t.number = next_number(TradeIn, company)
            hist(t, request.user, "created", amount=format(t.amount, "f"))
            t.save()
        _feed(t, request.user)
        return Response({**payload(t, request), "replayed": False}, status=201)


class TradeInDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        bad = guard(request, "trade_in", "trade_in.manage")
        if bad:
            return bad
        t = _get(request, pk)
        return Response(payload(t, request, with_doc="trade_in.view_documents" in perms_of(request.user)))

    def patch(self, request, pk):
        bad = guard(request, "trade_in", "trade_in.manage")
        if bad:
            return bad
        with transaction.atomic():
            t = TradeIn.objects.select_for_update().get(pk=_get(request, pk).pk)
            if t.status in ("cancelled", "stocked"):
                return err("bad_transition", "Закрытую скупку менять нельзя.", 409, status=t.status)
            bad = _fill(t, request.data if hasattr(request.data, "get") else {})
            if bad:
                return bad
            _refresh_status(t)
            hist(t, request.user, "edited")
            t.save()
        _feed(t, request.user)
        return Response(payload(_get(request, pk), request))


class TradeInPayoutAPIView(APIView):
    """POST trade-in/<id>/payout/ {amount?, shift?} + Idempotency-Key — выплата наличными = изъятие из смены, одной транзакцией."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        bad = guard(request, "trade_in", "trade_in.manage")
        if bad:
            return bad
        key = (request.headers.get("Idempotency-Key") or "").strip()[:128]
        if not key:
            return err("idempotency_key_required", "Нужен заголовок Idempotency-Key.")
        d = request.data if hasattr(request.data, "get") else {}
        company = company_of(request.user)
        with transaction.atomic():
            t = TradeIn.objects.select_for_update().get(pk=_get(request, pk).pk)
            if TradeInPayout.objects.filter(trade_in=t, idempotency_key=key).exists():
                return Response({**payload(t, request), "replayed": True})
            if t.status in ("cancelled",):
                return err("bad_transition", "Скупка отменена.", 409)
            amount = money(d.get("amount")) if d.get("amount") not in (None, "") else t.available
            if amount is None or amount <= 0:
                return err("validation_error", "amount: больше 0.", fields={"amount": ["Больше 0."]})
            if amount > t.available:
                return err("amount_exceeds_available", "Больше доступного к выплате.", 409, available=format(t.available, "f"))
            sh = open_shift(request.user, company, d.get("shift"))
            if sh is None:
                return err("shift_closed", "Откройте смену: выплата идёт из кассы смены.", 400)
            cf = cash_move(company=company, user=request.user, shift=sh, direction="out", amount=amount, method="cash",
                           source_id=t.pk, name=f"Выплата за скупку ТИ-{t.number}", key=f"tradein:{t.pk}:{key}")
            TradeInPayout.objects.create(trade_in=t, amount=amount, shift=sh, cashflow_id=getattr(cf, "pk", None),
                                         user=request.user, idempotency_key=key)
            t.paid_out += amount
            _refresh_status(t)
            hist(t, request.user, "payout", amount=format(amount, "f"))
            t.save()
        _feed(t, request.user)
        return Response({**payload(_get(request, pk), request), "replayed": False}, status=201)


class TradeInCancelAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        bad = guard(request, "trade_in", "trade_in.manage")
        if bad:
            return bad
        with transaction.atomic():
            t = TradeIn.objects.select_for_update().get(pk=_get(request, pk).pk)
            if t.status == "cancelled":
                return Response({**payload(t, request), "replayed": True})
            if t.paid_out > 0 or t.credit_confirmed > 0 or t.product_id:
                return err("trade_in_used", "По скупке уже были выплаты, зачёт или оприходование — отменить нельзя.", 409)
            t.status, t.credit_pending = "cancelled", Z
            hist(t, request.user, "cancelled", note=str((request.data or {}).get("reason") or "")[:255])
            t.save()
        _feed(t, request.user)
        return Response(payload(t, request))


class TradeInStockAPIView(APIView):
    """POST trade-in/<id>/stock/ {price, name?, category?} — оприходовать устройство: товар «Б/у …», 1 шт,
    закупка = сумма скупки, без поставщика, учёт серийников включён."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        bad = guard(request, "trade_in", "trade_in.manage")
        if bad:
            return bad
        from apps.main.models import Product

        d = request.data if hasattr(request.data, "get") else {}
        price = money(d.get("price"))
        if price is None:
            return err("validation_error", "price: цена продажи больше 0.", fields={"price": ["Больше 0."]})
        with transaction.atomic():
            t = TradeIn.objects.select_for_update().get(pk=_get(request, pk).pk)
            if t.product_id:
                return Response({**payload(t, request), "replayed": True})
            if t.status == "cancelled":
                return err("bad_transition", "Скупка отменена.", 409)
            name = str(d.get("name") or f"Б/у {t.device}").strip()[:255]
            p = Product(company=t.company, branch=t.branch, name=name, price=price, purchase_price=t.amount, quantity=Decimal("1"))
            if hasattr(p, "track_serials"):
                p.track_serials = True
            if d.get("category"):
                p.category_id = d.get("category")
            p.save()
            t.product, t.status = p, "stocked"
            hist(t, request.user, "stocked", product=str(p.pk))
            t.save()
        _feed(t, request.user)
        return Response(payload(_get(request, pk), request), status=201)


class TradeInPhotosAPIView(APIView):
    """POST trade-in/<id>/photos/ (multipart file, до 5 фото, до 8 МБ, jpg/png/webp/heic); DELETE trade-in/<id>/photos/<photo_id>/"""
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, pk):
        bad = guard(request, "trade_in", "trade_in.manage")
        if bad:
            return bad
        f = request.FILES.get("file")
        if f is None:
            return err("file_required", "Пришлите файл в поле file.")
        if f.size > 8 * 1024 * 1024:
            return err("file_too_large", "Фото не больше 8 МБ.", 413)
        ext = (f.name.rsplit(".", 1)[-1] if "." in f.name else "").lower()
        if ext not in ("jpg", "jpeg", "png", "webp", "heic"):
            return err("file_type_invalid", "Только jpg, png, webp, heic.")
        with transaction.atomic():
            t = TradeIn.objects.select_for_update().get(pk=_get(request, pk).pk)
            if t.photos.count() >= MAX_PHOTOS:
                return err("photos_limit", f"Не больше {MAX_PHOTOS} фото на скупку.", 409, max=MAX_PHOTOS)
            ph = TradeInPhoto.objects.create(trade_in=t, file=f)
        return Response({"id": ph.pk, "url": request.build_absolute_uri(ph.file.url)}, status=201)

    def delete(self, request, pk, photo_id):
        bad = guard(request, "trade_in", "trade_in.manage")
        if bad:
            return bad
        t = _get(request, pk)
        ph = TradeInPhoto.objects.filter(trade_in=t, pk=photo_id).first()
        if ph is None:
            return err("not_found", "Нет такого фото.", 404)
        ph.file.delete(save=False)
        ph.delete()
        return Response(status=204)


class TradeInImportAPIView(APIView):
    """POST trade-in/import/ {items:[{device_id, local_id, local_number, amount, paid_out?, …поля}]} — без движений денег."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        bad = guard(request, "trade_in", "trade_in.manage")
        if bad:
            return bad
        company = company_of(request.user)
        items = (request.data or {}).get("items") or []
        if not isinstance(items, list) or len(items) > 500:
            return err("validation_error", "items: список до 500.", fields={"items": ["До 500."]})
        out = []
        with transaction.atomic():
            lock_company(company)
            for i, it in enumerate(items):
                it = it if isinstance(it, dict) else {}
                dev, lid = str(it.get("device_id") or "")[:64], str(it.get("local_id") or "")[:64]
                if not dev or not lid:
                    out.append({"index": i, "error": "device_id_local_id_required"})
                    continue
                t = TradeIn.objects.filter(company=company, device_id=dev, local_id=lid).first()
                if t:
                    out.append({"index": i, "id": str(t.id), "number": t.number, "replayed": True})
                    continue
                t = TradeIn(company=company, created_by=request.user, device_id=dev, local_id=lid, local_number=str(it.get("local_number") or "")[:32])
                bad = _fill(t, it, create=True)
                if bad:
                    out.append({"index": i, "error": bad.data.get("code"), "detail": bad.data.get("detail")})
                    continue
                po = money(it.get("paid_out") or 0, allow_zero=True) or Z
                t.paid_out = min(po, t.amount)
                _refresh_status(t)
                t.number = next_number(TradeIn, company)
                hist(t, request.user, "imported", local_number=t.local_number)
                t.save()
                out.append({"index": i, "id": str(t.id), "number": t.number, "replayed": False})
        return Response({"results": out})
