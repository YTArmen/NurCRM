"""
Б-46 (27.10.2026): лента изменений каталога для касс и мобильного приложения.

GET /api/main/products/changes/?since=<курсор>&limit=500
    since  — курсор из прошлого ответа (`cursor`, мс с эпохи) или дата-время ISO 8601.
             Без since — первые `limit` товаров по updated_at (начальная загрузка).
    limit  — 1…1000, по умолчанию 500.
    branch — та же область, что у /main/products/list/ (компания, ?branch=, без архивных).

→ {
    "changes": [ {"id", "name", "price", "purchase_price", "markup_percent", "discount_percent",
                  "wholesale_price", "quantity", "updated_at"} ],   # только изменённые с since
    "deleted": ["<uuid>", …],                                       # удалённые с since
    "cursor": 1761550000000,     # since для следующего запроса (мс)
    "server_time": "...",        # ISO, то же время
    "has_more": false,           # true — в ленте больше limit строк: повторить сразу с cursor
    "catalog_version": "…"       # как в products/catalog-version/
  }

ETag = версия каталога + since: при If-None-Match с тем же значением → 304 без тела.
Кассы опрашивают раз в 5–10 с; 304 — один агрегирующий запрос к БД.

Полный список и карточки по-прежнему отдаёт /main/products/list/ — здесь только
поля, из-за которых касса может продать не по той цене или не тому остатку.
"""
import datetime as _dt
import hashlib

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from core.http_cache import ETagMixin, etag_matches

from .models import Product, ProductDeletion
from .price_check import catalog_version
from .views import CompanyBranchRestrictedMixin, _filter_products_company_only

DEFAULT_LIMIT = 500
MAX_LIMIT = 1000
FIELDS = (
    "id",
    "name",
    "price",
    "purchase_price",
    "markup_percent",
    "discount_percent",
    "wholesale_price",
    "quantity",
    "updated_at",
)


def parse_since(raw, field="since"):
    """Курсор: целое — мс с эпохи (cursor/server_time_ms прошлого ответа), иначе ISO 8601."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        try:
            return _dt.datetime.fromtimestamp(int(raw) / 1000, tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ValidationError({field: "Некорректное время (мс с эпохи)."})
    dt = parse_datetime(raw.replace(" ", "+"))
    if dt is None:
        raise ValidationError({field: "Ожидается дата-время ISO 8601 или мс с эпохи."})
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _row(r):
    out = {}
    for k in FIELDS:
        v = r.get(k)
        if k == "id":
            out[k] = str(v)
        elif k == "updated_at":
            out[k] = v.isoformat() if v else None
        elif k == "name":
            out[k] = v
        else:
            out[k] = str(v) if v is not None else None
    return out


class ProductChangesView(ETagMixin, CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        since = parse_since(request.query_params.get("since") or request.query_params.get("updated_since"))
        try:
            limit = int(request.query_params.get("limit") or DEFAULT_LIMIT)
        except ValueError:
            raise ValidationError({"limit": "Целое число 1…%d." % MAX_LIMIT})
        limit = max(1, min(limit, MAX_LIMIT))

        scope = _filter_products_company_only(self, Product.objects.all())
        version = catalog_version(scope)["version"]
        etag = '"%s"' % hashlib.sha1(("%s|%s|%s" % (version, _ms(since) if since else "", limit)).encode()).hexdigest()[:24]
        # ТЗ 29.10 Б-46: W/"…", список через запятую и `*` тоже дают 304 (раньше сравнивали строку целиком).
        if etag_matches(request.headers.get("If-None-Match"), etag):
            resp = HttpResponse(status=304)
            resp["ETag"] = etag
            resp["Cache-Control"] = "private, no-cache"
            return resp

        # server_time берём до выборки: изменения во время запроса придут в следующий раз.
        server_time = timezone.now()
        qs = scope
        if since is not None:
            qs = qs.filter(updated_at__gte=since)
        rows = list(qs.order_by("updated_at", "id").values(*FIELDS)[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]

        deleted = []
        company_id = getattr(request.user, "company_id", None) or getattr(self._company(), "id", None)
        if since is not None and company_id:
            deleted = [
                str(pid)
                for pid in ProductDeletion.objects.filter(company_id=company_id, deleted_at__gte=since)
                .values_list("product_id", flat=True)
                .distinct()
            ]

        # Если лента обрезана, курсор — updated_at последней строки: следующий запрос продолжит
        # с неё (повтор строки с тем же updated_at безопасен, касса просто перезапишет значения).
        cursor_dt = rows[-1]["updated_at"] if has_more and rows else server_time
        body = {
            "since": since.isoformat() if since else None,
            "changes": [_row(r) for r in rows],
            "deleted": deleted,
            "cursor": _ms(cursor_dt),
            "server_time": server_time.isoformat(),
            "server_time_ms": _ms(server_time),
            "has_more": has_more,
            "catalog_version": version,
        }
        resp = Response(body)
        resp["ETag"] = etag
        resp["Cache-Control"] = "private, no-cache"
        return resp
