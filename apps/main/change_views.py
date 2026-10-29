"""ТЗ Н-31: GET api/main/changes/ — лента изменений для касс."""
import base64
import hashlib
import time

from rest_framework.negotiation import BaseContentNegotiation
from django.core.cache import cache
from django.db.models import Q
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.change_feed import RETENTION_DAYS, feed_type, serialize_event
from apps.main.models import ChangeEvent
from apps.main.price_check import _etag_matches
from core.http_cache import ETagMixin
from apps.main.views import CompanyBranchRestrictedMixin

MAX_LIMIT = 1000


def encode_cursor(last_id: int, epoch: float | None = None) -> str:
    raw = f"{int(last_id)}.{int(epoch if epoch is not None else time.time())}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(value: str):
    """→ (id, epoch) или None, если курсор испорчен."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        a, b = raw.split(".", 1)
        return int(a), int(b)
    except Exception:
        return None


class ChangesFeedView(ETagMixin, CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/changes/?since=<cursor>&types=a,b&limit=500
    → {cursor, has_more, events:[{id,type,at,branch,actor,entity,entity_id,data}]}
    Без since — вернёт текущий курсор без событий (старт с «сейчас»).
    Курсор старше 7 дней → 410 cursor_expired. ETag/If-None-Match → 304.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = self._company()
        if company is None:
            return Response({"detail": "Нет компании."}, status=403)
        qp = request.query_params
        try:
            limit = max(1, min(int(qp.get("limit") or 500), MAX_LIMIT))
        except ValueError:
            limit = 500
        types = [feed_type(t.strip()) for t in (qp.get("types") or "").split(",") if t.strip()]

        qs = ChangeEvent.objects.filter(company_id=company.id)
        branch_id = self._fixed_branch_id(company)
        if branch_id is None and qp.get("branch"):
            qs = qs.filter(Q(branch_id=qp.get("branch")) | Q(branch__isnull=True))
        elif branch_id is not None:
            qs = qs.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))
        if types:
            qs = qs.filter(type__in=types)

        since_raw = qp.get("since")
        if not since_raw:
            last = qs.order_by("-id").values_list("id", flat=True).first() or 0
            return self._json({"cursor": encode_cursor(last), "has_more": False, "events": []},
                              self._etag(company.id, last, types, limit), request)

        parsed = decode_cursor(since_raw)
        if parsed is None:
            return Response({"detail": "Неверный курсор.", "code": "invalid_cursor"}, status=400)
        since_id, epoch = parsed
        if time.time() - epoch > RETENTION_DAYS * 86400:
            return Response({"detail": "Курсор устарел, перечитайте данные целиком.", "code": "cursor_expired"},
                            status=410)

        rows = list(qs.filter(id__gt=since_id).order_by("id")[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        if rows:
            cursor = encode_cursor(rows[-1].id, rows[-1].created_at.timestamp())
            last = rows[-1].id
        else:
            cursor = encode_cursor(since_id)  # событий нет: курсор «свежий», но тот же id
            last = since_id
        etag = self._etag(company.id, last, types, limit, count=len(rows), since=since_id)
        return self._json(
            {"cursor": cursor, "has_more": has_more, "events": [serialize_event(e) for e in rows]},
            etag, request,
        )

    def _fixed_branch_id(self, company):
        """Жёсткий филиал сотрудника; владельцу не ищем, остальным — кэш на минуту (быстрый пустой ответ)."""
        user = self.request.user
        if getattr(user, "role", None) in ("owner", "admin"):
            return None
        key = f"chg_fixed_branch:{user.id}:{company.id}"
        cached = cache.get(key)
        if cached is None:
            br = self._fixed_branch_from_user(company)
            cached = str(br.id) if br is not None else ""
            cache.set(key, cached, 60)
        return cached or None

    @staticmethod
    def _etag(company_id, last, types, limit, count=0, since=0):
        raw = f"{company_id}|{since}|{last}|{count}|{','.join(sorted(types))}|{limit}"
        return '"' + hashlib.sha1(raw.encode()).hexdigest()[:20] + '"'

    @staticmethod
    def _json(payload, etag, request):
        if _etag_matches(request.headers.get("If-None-Match"), etag):
            resp = Response(status=304)
        else:
            resp = Response(payload)
        resp["ETag"] = etag
        resp["Cache-Control"] = "private, no-cache"
        return resp


class _AnyAccept(BaseContentNegotiation):
    """Accept: text/event-stream не должен давать 406 — ответ формируем сами."""

    def select_parser(self, request, parsers):
        return parsers[0] if parsers else None

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class EventsStreamView(APIView):
    """ТЗ 29.10 Б-102: GET main/events/stream/ — не держит соединение, отсылает на WebSocket."""

    content_negotiation_class = _AnyAccept

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        from apps.main.events_stream import events_stream_http

        return events_stream_http(request)
