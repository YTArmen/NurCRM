"""ТЗ Н-32: медиа-хранилище компании (api/main/media/) и электронный чек (public-link, api/public/receipts/).

Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
import hashlib
import io
import os
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.main.models import MainMedia, Sale, SaleReceiptLink, SaleReceiptRating

MAX_FILE_BYTES = 5 * 1024 * 1024
Q2 = Decimal("0.01")


def _err(code, detail, http=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def _company_of(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _m2(v):
    return str(Decimal(str(v or "0")).quantize(Q2, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------
# (1) Медиа
# --------------------------------------------------------------------------

def sniff_content_type(head: bytes):
    """Настоящий тип по первым байтам (расширению и Content-Type не верим)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def media_payload(media, request=None):
    url = media.file.url if media.file else ""
    if request is not None and url and not url.startswith("http"):
        url = request.build_absolute_uri(url)
    return {
        "id": str(media.id),
        "url": url,
        "size": media.size,
        "content_type": media.content_type,
        "sha256": media.sha256,
        "created_at": media.created_at.isoformat() if media.created_at else None,
    }


class MainMediaListCreateAPIView(APIView):
    """POST api/main/media/ (multipart: file, purpose) → 201 {id,url,size,content_type,sha256,created_at}.
    GET — список файлов компании (?purpose=). Ошибки: 400 invalid_type/invalid_purpose,
    413 too_large (> 5 МБ) и quota_exceeded (квота settings.COMPANY_MEDIA_QUOTA_MB, по умолчанию 500)."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        company = _company_of(request.user)
        if company is None:
            return _err("no_company", "У пользователя нет компании.", 403)
        qs = MainMedia.objects.filter(company=company)
        purpose = request.query_params.get("purpose")
        if purpose:
            qs = qs.filter(purpose=purpose)
        used = MainMedia.objects.filter(company=company).aggregate(s=Sum("size"))["s"] or 0
        return Response({
            "quota_bytes": int(settings.COMPANY_MEDIA_QUOTA_MB) * 1024 * 1024,
            "used_bytes": used,
            "results": [media_payload(m, request) for m in qs[:500]],
        })

    def post(self, request):
        company = _company_of(request.user)
        if company is None:
            return _err("no_company", "У пользователя нет компании.", 403)
        upload = request.FILES.get("file")
        if upload is None:
            return _err("file_required", "Файл не передан.", fields={"file": "Обязательное поле."})
        purpose = str(request.data.get("purpose") or MainMedia.Purpose.OTHER).strip()
        if purpose not in MainMedia.Purpose.values:
            return _err("invalid_purpose", f"purpose: {', '.join(MainMedia.Purpose.values)}.")
        if upload.size > MAX_FILE_BYTES:
            return _err("too_large", "Файл больше 5 МБ.", 413)
        upload.seek(0)
        head = upload.read(16)
        upload.seek(0)
        ctype = sniff_content_type(head)
        allowed = {"image/png", "image/jpeg", "image/webp"}
        if purpose == MainMedia.Purpose.INVOICE:
            allowed.add("application/pdf")
        if ctype not in allowed:
            return _err("invalid_type", "Допустимы PNG, JPG, WebP (для накладной ещё PDF).")
        used = MainMedia.objects.filter(company=company).aggregate(s=Sum("size"))["s"] or 0
        if used + upload.size > int(settings.COMPANY_MEDIA_QUOTA_MB) * 1024 * 1024:
            return _err("quota_exceeded", "Исчерпана квота хранилища компании.", 413)
        sha = hashlib.sha256()
        for chunk in upload.chunks():
            sha.update(chunk)
        upload.seek(0)
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/pdf": "pdf"}[ctype]
        media = MainMedia(company=company, purpose=purpose, size=upload.size, content_type=ctype,
                          sha256=sha.hexdigest(), created_by=request.user)
        media.file.save(f"{media.id.hex}.{ext}", upload, save=False)
        media.save()
        return Response(media_payload(media, request), status=status.HTTP_201_CREATED)


class MainMediaDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, pk):
        company = _company_of(request.user)
        return MainMedia.objects.filter(id=pk, company=company).first() if company else None

    def get(self, request, pk):
        media = self._get(request, pk)
        if media is None:
            return _err("not_found", "Файл не найден.", 404)
        return Response(media_payload(media, request))

    def delete(self, request, pk):
        media = self._get(request, pk)
        if media is None:
            return _err("not_found", "Файл не найден.", 404)
        try:
            media.file.delete(save=False)
        except Exception:
            pass
        media.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# (2) Электронный чек
# --------------------------------------------------------------------------

def _public_url(token):
    return settings.RECEIPT_PUBLIC_URL_TEMPLATE.format(token=token)


class SalePublicLinkAPIView(APIView):
    """POST pos/sales/{id}/public-link/ → {token, url}. Идемпотентно: у продажи одна ссылка."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        company = _company_of(request.user)
        sale = Sale.objects.filter(id=pk, company=company).first() if company else None
        if sale is None:
            return _err("not_found", "Продажа не найдена.", 404)
        link = SaleReceiptLink.objects.filter(sale=sale).first()
        created = False
        if link is None:
            try:
                with transaction.atomic():
                    link = SaleReceiptLink.objects.create(sale=sale, company=sale.company)
                created = True
            except IntegrityError:
                link = SaleReceiptLink.objects.get(sale=sale)
        return Response({"token": link.token, "url": _public_url(link.token)},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class PublicReceiptThrottle(AnonRateThrottle):
    """Лимит по IP для публичного чека."""
    scope = "public_receipt"
    rate = os.getenv("PUBLIC_RECEIPT_RATE", "60/minute")


class PublicReceiptRatingThrottle(AnonRateThrottle):
    scope = "public_receipt_rating"
    rate = os.getenv("PUBLIC_RECEIPT_RATING_RATE", "10/hour")


def receipt_public_data(link):
    """Данные чека для покупателя. Имени и телефона клиента нет намеренно."""
    from apps.main.receipt_header import receipt_vendor_header
    from apps.main.utils_numbers import ensure_sale_doc_number

    sale = link.sale
    vh = receipt_vendor_header(sale)
    dt = timezone.localtime(sale.created_at) if sale.created_at else timezone.localtime()
    items = []
    for it in sale.items.all():
        qty = Decimal(str(it.quantity or 0))
        unit = Decimal(str(it.unit_price or 0))
        disc = Decimal(str(getattr(it, "line_discount", 0) or 0))
        items.append({
            "name": (getattr(it, "name_snapshot", None) or getattr(it, "custom_name", None) or "").strip(),
            "qty": format(qty.normalize(), "f"),
            "unit_price": _m2(unit),
            "line_discount": _m2(disc),
            "total": _m2(unit * qty - disc),
        })
    payments = [{"method": p.method, "amount": _m2(p.amount)} for p in sale.payments.all()]
    if not payments and sale.payment_method:
        payments = [{"method": sale.payment_method, "amount": _m2(sale.total)}]
    rating = SaleReceiptRating.objects.filter(link=link).first()
    return {
        "company": {"name": (vh.get("brand") or sale.company.name or "").strip()},
        "number": str(ensure_sale_doc_number(sale)),
        "date": dt.isoformat(),
        "items": items,
        "totals": {
            "subtotal": _m2(sale.subtotal),
            "discount_total": _m2(sale.discount_total),
            "tax_total": _m2(sale.tax_total),
            "total": _m2(sale.total),
        },
        "payments": payments,
        "rating": ({"score": rating.score, "comment": rating.comment} if rating else None),
    }


def _link_or_none(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token or ""):
        return None
    return SaleReceiptLink.objects.select_related("sale", "sale__company").filter(token=token).first()


class PublicReceiptAPIView(APIView):
    """GET api/public/receipts/{token}/ — без авторизации."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [PublicReceiptThrottle]

    def get(self, request, token):
        link = _link_or_none(token)
        if link is None:
            return _err("not_found", "Чек не найден.", 404)
        return Response(receipt_public_data(link))


def build_receipt_pdf(data) -> bytes:
    from reportlab.lib.pagesizes import A6
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas

    font, bold = "Helvetica", "Helvetica-Bold"
    if "DejaVu" in pdfmetrics.getRegisteredFontNames():
        font, bold = "DejaVu", "DejaVu-Bold"
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A6)
    w, h = A6
    y = h - 24
    c.setFont(bold, 11)
    c.drawCentredString(w / 2, y, data["company"]["name"][:40])
    y -= 14
    c.setFont(font, 8)
    c.drawCentredString(w / 2, y, f"Чек № {data['number']}  {data['date'][:16].replace('T', ' ')}")
    y -= 14
    for it in data["items"]:
        if y < 70:
            c.showPage()
            y = h - 24
        c.setFont(font, 8)
        c.drawString(14, y, it["name"][:34])
        y -= 10
        c.drawString(20, y, f"{it['qty']} x {it['unit_price']}")
        c.drawRightString(w - 14, y, it["total"])
        y -= 11
    y -= 4
    c.setFont(bold, 10)
    c.drawString(14, y, "Итого")
    c.drawRightString(w - 14, y, data["totals"]["total"])
    y -= 13
    c.setFont(font, 8)
    for p in data["payments"]:
        c.drawString(14, y, str(p["method"]))
        c.drawRightString(w - 14, y, p["amount"])
        y -= 10
    c.save()
    return buf.getvalue()


class PublicReceiptPdfAPIView(APIView):
    """GET api/public/receipts/{token}/pdf/"""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [PublicReceiptThrottle]

    def get(self, request, token):
        link = _link_or_none(token)
        if link is None:
            return _err("not_found", "Чек не найден.", 404)
        resp = HttpResponse(build_receipt_pdf(receipt_public_data(link)), content_type="application/pdf")
        resp["Content-Disposition"] = 'inline; filename="receipt.pdf"'
        return resp


class PublicReceiptRatingAPIView(APIView):
    """POST api/public/receipts/{token}/rating/ {score 1-5, comment} — один раз на чек (повтор → 409)."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [PublicReceiptRatingThrottle]

    def post(self, request, token):
        link = _link_or_none(token)
        if link is None:
            return _err("not_found", "Чек не найден.", 404)
        raw = request.data.get("score")
        try:
            score = int(raw)
        except (TypeError, ValueError):
            score = 0
        if isinstance(raw, bool) or not 1 <= score <= 5:
            return _err("invalid_score", "Оценка — число от 1 до 5.", fields={"score": "От 1 до 5."})
        comment = str(request.data.get("comment") or "")[:1000]
        ip = (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or request.META.get("REMOTE_ADDR") or None)
        try:
            with transaction.atomic():
                SaleReceiptRating.objects.create(link=link, score=score, comment=comment, ip=ip)
        except IntegrityError:
            return _err("already_rated", "Этот чек уже оценён.", 409)
        return Response({"score": score, "comment": comment}, status=status.HTTP_201_CREATED)
