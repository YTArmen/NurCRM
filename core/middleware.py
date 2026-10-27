"""
Служебные заголовки и журнал медленных запросов (ТЗ-28 §11.2–11.4).

- `X-Request-Id`: клиент присылает свой uuid — сервер возвращает его в ответе и пишет
  в журнал; без заголовка сервер генерирует сам. По нему сводятся журнал кассы и сервера.
- `X-Server-Time`: время сервера (UTC, ISO 8601) в каждом ответе — клиент правит
  `sold_at` для чеков без связи и предупреждает, если часы разошлись > 10 минут.
- Журнал `nurcrm.slow_requests`: всё дольше SLOW_REQUEST_MS (по умолчанию 1000 мс),
  без тела запроса и без токенов.
- `X-NurCRM-API` (единое ТЗ 26.10.2026, Б-01): версия API во всех ответах `/api/`.
- `X-NurMarket-Client` (Б-01): версия кассы/мобилки пишется в журнал запросов и, раз в час на пару
  компания+клиент, в `users.ClientVersionSeen` (счётчик запросов копится в кэше между записями).
Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
import logging
import re
import time
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db.models import F
from django.utils import timezone

from core.api_compat import API_VERSION, client_label

logger = logging.getLogger("nurcrm.slow_requests")
clients_log = logging.getLogger("nurcrm.api_clients")

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class RequestMetaMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.slow_ms = int(getattr(settings, "SLOW_REQUEST_MS", 1000))

    def __call__(self, request):
        started = time.monotonic()
        rid = (request.headers.get("X-Request-Id") or "").strip()
        if not rid or not _SAFE_ID.match(rid):
            rid = uuid.uuid4().hex
        request.request_id = rid

        response = self.get_response(request)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        response["X-Request-Id"] = rid
        response["X-Server-Time"] = timezone.now().isoformat(timespec="milliseconds")
        response["X-Response-Time-Ms"] = str(elapsed_ms)
        if request.path.startswith("/api/"):
            response["X-NurCRM-API"] = API_VERSION
            _note_client(request)

        if elapsed_ms >= self.slow_ms:
            user = getattr(request, "user", None)
            logger.warning(
                "slow %s %s %sms status=%s user=%s rid=%s",
                request.method,
                request.path,
                elapsed_ms,
                getattr(response, "status_code", "?"),
                getattr(user, "pk", None) if user is not None and getattr(user, "is_authenticated", False) else None,
                rid,
            )
        return response


def _note_client(request):
    """Б-01: какая версия клиента ходит в API компании. Ошибки журнала не ломают ответ."""
    try:
        user = getattr(request, "user", None)  # DRF кладёт сюда пользователя из JWT
        if user is None or not getattr(user, "is_authenticated", False):
            return
        company_id = getattr(user, "company_id", None)
        if not company_id:
            return
        client = client_label(request) or "none"
        base = f"nm-client:{company_id}:{client}"
        try:
            cache.incr(base + ":n")
        except ValueError:
            cache.set(base + ":n", 1, 7200)
        if not cache.add(base + ":flush", 1, 3600):
            return
        hits = int(cache.get(base + ":n") or 1)
        cache.delete(base + ":n")
        from apps.users.models import ClientVersionSeen

        now = timezone.now()
        updated = ClientVersionSeen.objects.filter(company_id=company_id, client=client).update(
            last_seen_at=now, hits=F("hits") + hits
        )
        if not updated:
            ClientVersionSeen.objects.get_or_create(
                company_id=company_id, client=client, defaults={"last_seen_at": now, "hits": hits}
            )
        clients_log.info("company=%s client=%s hits=%s", company_id, client, hits)
    except Exception:  # noqa: BLE001 — журнал версий не должен ронять запрос
        logging.getLogger(__name__).debug("client version note failed", exc_info=True)
