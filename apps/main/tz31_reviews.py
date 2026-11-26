"""ТЗ кассы 05.11 п.39 / Б-95: отзывы о товарах витрины с модерацией.

Публично: POST/GET .../public/companies/<slug>/showcase/<product_id>/reviews/ (в показ попадают только approved).
Сотрудники (владелец/админ): /api/main/showcase/reviews/?status=pending, PATCH status/reply, DELETE.
Телефон автора наружу не отдаётся никогда (только сотрудникам).
"""
import hashlib
import os
import re

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.db.models import Avg, Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Product, ShowcaseProductReview
from apps.main.showcase import design_schema as ds
from apps.main.showcase import services as svc
from apps.main.showcase.design_schema import _err
from apps.main.showcase.services import ShowcaseErrorMixin, resolve_public_company
from apps.main.showcase.views_design import EditorAPIView
from apps.main.tz31_speed import client_ip

PER_HOUR = 5
HOUR = 3600
DAY = 24 * 3600
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-]{4,30}$")
St = ShowcaseProductReview.Status


class _Page(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def summary(product) -> dict:
    agg = ShowcaseProductReview.objects.filter(product=product, status=St.APPROVED).aggregate(a=Avg("rating"), n=Count("id"))
    return {"avg": round(float(agg["a"]), 2) if agg["a"] is not None else None, "count": agg["n"]}


def _ip_hash(ip: str) -> str:
    return hashlib.sha256(f"{ip}|{dj_settings.SECRET_KEY}".encode()).hexdigest()[:32]


def _limited(ip_hash: str, product_id) -> int:
    """Секунд до разрешения, если лимит исчерпан; 0 — можно. Сбой кэша не блокирует."""
    try:
        if not cache.add(f"showcase:rev:day:{ip_hash}:{product_id}", 1, DAY):
            return DAY
        key = f"showcase:rev:hour:{ip_hash}"
        cache.add(key, 0, HOUR)
        if cache.incr(key) > PER_HOUR:
            cache.delete(f"showcase:rev:day:{ip_hash}:{product_id}")  # попытка не считается за «отзыв сегодня»
            return HOUR
    except Exception:
        return 0
    return 0


def _turnstile_ok(request, ip) -> bool:
    secret = (os.getenv("TURNSTILE_SECRET_KEY") or getattr(dj_settings, "TURNSTILE_SECRET_KEY", "") or "").strip()
    if not secret:
        return True  # капча не настроена — пропускаем (как в виджете чата)
    from apps.inbox.assistant import turnstile_verify

    token = str(request.data.get("turnstile_token") or request.headers.get("X-Turnstile-Token") or "")
    return turnstile_verify(token, ip)


def _clean_new(data) -> dict:
    if not isinstance(data, dict):
        _err(None, "Ожидается объект.")
    rating = data.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) and not (isinstance(rating, str) and rating.isdigit()):
        _err("rating", "Оценка — целое число от 1 до 5.")
    rating = int(rating)
    if not 1 <= rating <= 5:
        _err("rating", "Оценка — целое число от 1 до 5.")
    text = ds.clean_text(data.get("text") or "", "text", 2000) if data.get("text") else ""
    name = ds.clean_text(data.get("author_name") or "", "author_name", 80) if data.get("author_name") else ""
    if not name:
        _err("author_name", "Укажите имя.", "required")
    phone = str(data.get("author_phone") or "").strip()
    if phone and not PHONE_RE.match(phone):
        _err("author_phone", "Неверный номер телефона.")
    return {"rating": rating, "text": text, "author_name": name, "author_phone": phone}


def public_out(r) -> dict:
    return {
        "id": str(r.id), "rating": r.rating, "text": r.text, "author_name": r.author_name,
        "reply": r.reply or None, "replied_at": r.replied_at.isoformat() if r.replied_at else None,
        "created_at": r.created_at.isoformat(),
    }


class PublicProductReviewsAPIView(ShowcaseErrorMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def _product(self, slug, product_id):
        company = resolve_public_company(slug, self.request)
        catalog = svc.get_public_catalog(company, None)
        qs = svc.apply_catalog_visibility(Product.objects.filter(company=company, id=product_id), catalog)
        product = qs.first()
        if product is None:
            raise NotFound("Товар не найден")
        return company, product

    def get(self, request, slug, product_id):
        _, product = self._product(slug, product_id)
        qs = ShowcaseProductReview.objects.filter(product=product, status=St.APPROVED).order_by("-created_at", "id")
        pg = _Page()
        page = pg.paginate_queryset(qs, request)
        resp = pg.get_paginated_response([public_out(r) for r in page])
        resp.data["summary"] = summary(product)
        return resp

    def post(self, request, slug, product_id):
        company, product = self._product(slug, product_id)
        data = _clean_new(request.data)
        ip = client_ip(request)
        if not _turnstile_ok(request, ip):
            return Response({"detail": "Проверка «я не робот» не пройдена.", "code": "turnstile_failed"}, status=status.HTTP_403_FORBIDDEN)
        h = _ip_hash(ip)
        wait = _limited(h, product.id)
        if wait:
            resp = Response(
                {"detail": "Слишком много отзывов. Попробуйте позже.", "code": "rate_limited", "retry_after": wait},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            resp["Retry-After"] = str(wait)
            return resp
        r = ShowcaseProductReview.objects.create(company=company, product=product, ip_hash=h, **data)
        return Response(
            {"id": str(r.id), "status": r.status, "detail": "Спасибо! Отзыв появится после проверки."},
            status=status.HTTP_201_CREATED,
        )


def staff_out(r) -> dict:
    d = public_out(r)
    d.update({
        "status": r.status, "product": str(r.product_id), "product_name": r.product.name,
        "author_phone": r.author_phone or None,
    })
    return d


class StaffReviewListAPIView(EditorAPIView):
    def get(self, request):
        qs = ShowcaseProductReview.objects.filter(company=self.company()).select_related("product")
        st = (request.query_params.get("status") or "").strip()
        if st:
            if st not in St.values:
                _err("status", f"Допустимые значения: {', '.join(St.values)}.")
            qs = qs.filter(status=st)
        pid = (request.query_params.get("product") or "").strip()
        if pid:
            try:
                qs = qs.filter(product_id=pid)
            except Exception:
                _err("product", "Ожидается UUID товара.")
        pg = _Page()
        page = pg.paginate_queryset(qs.order_by("-created_at", "id"), request)
        resp = pg.get_paginated_response([staff_out(r) for r in page])
        resp.data["pending_count"] = ShowcaseProductReview.objects.filter(company=self.company(), status=St.PENDING).count()
        return resp


class StaffReviewDetailAPIView(EditorAPIView):
    def _get(self, pk):
        return get_object_or_404(ShowcaseProductReview.objects.select_related("product"), id=pk, company=self.company())

    def get(self, request, pk):
        return Response(staff_out(self._get(pk)))

    def patch(self, request, pk):
        r = self._get(pk)
        data = request.data
        if not isinstance(data, dict):
            _err(None, "Ожидается объект.")
        for key in data:
            if key not in ("status", "reply"):
                _err(key, "Можно менять только status и reply.", "unknown_field")
        if "status" in data:
            if data["status"] not in St.values:
                _err("status", f"Допустимые значения: {', '.join(St.values)}.")
            r.status = data["status"]
        if "reply" in data:
            reply = ds.clean_text(data["reply"] or "", "reply", 1000) if data["reply"] else ""
            if reply != r.reply:
                r.reply, r.replied_at = reply, (timezone.now() if reply else None)
        r.save()
        return Response(staff_out(r))

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
