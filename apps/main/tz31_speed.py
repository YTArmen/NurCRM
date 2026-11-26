"""
ТЗ кассы 05.11 п.34 / Б-51: точки замера скорости интернета для кнопки «Проверить интернет» в кассе.

  GET  /api/speed/ping             → 204
  GET  /api/speed/down?bytes=N     → N случайных несжимаемых байт (по умолчанию 10 МБ, не больше 100 МБ)
  POST /api/speed/up               → тело читается и выбрасывается (не больше 50 МБ; больше — 413)
  GET  /api/speed/ip               → {"ip": "..."}

Только для вошедшего сотрудника. down/up — не чаще 1 замера в минуту и 30 в сутки с одного IP
(иначе 429 и Retry-After). ping/ip — мягкий лимит 120 в минуту.
"""
import os

from django.core.cache import cache
from django.http import HttpResponse, StreamingHttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

DOWN_DEFAULT_BYTES = 10 * 1024 * 1024
DOWN_MAX_BYTES = 100 * 1024 * 1024
UP_MAX_BYTES = 50 * 1024 * 1024
CHUNK = 256 * 1024
PER_MINUTE = 1
PER_DAY = 30
LIGHT_PER_MINUTE = 120


def client_ip(request) -> str:
    for header in ("HTTP_CF_CONNECTING_IP", "HTTP_X_REAL_IP"):
        value = (request.META.get(header) or "").strip()
        if value:
            return value
    fwd = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return fwd or request.META.get("REMOTE_ADDR") or "unknown"


def _hit(key: str, ttl: int) -> int:
    """Счётчик в кэше; при сбое кэша не блокируем."""
    try:
        cache.add(key, 0, ttl)
        return cache.incr(key)
    except Exception:
        return 0


def _too_many(retry_after: int, code="speed_rate_limited"):
    resp = Response(
        {"detail": "Слишком частые замеры. Повторите позже.", "code": code},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )
    resp["Retry-After"] = str(max(1, int(retry_after)))
    return resp


def _check_measure_limit(request):
    """None — можно мерить; иначе готовый ответ 429."""
    ip = client_ip(request)
    if _hit(f"speed:min:{ip}", 60) > PER_MINUTE:
        try:
            ttl = cache.ttl(f"speed:min:{ip}")  # есть у некоторых бэкендов
        except Exception:
            ttl = 60
        return _too_many(ttl if isinstance(ttl, int) and ttl > 0 else 60)
    if _hit(f"speed:day:{ip}", 86400) > PER_DAY:
        return _too_many(3600, code="speed_daily_limit")
    return None


def _check_light_limit(request):
    if _hit(f"speed:light:{client_ip(request)}", 60) > LIGHT_PER_MINUTE:
        return _too_many(60)
    return None


class SpeedBase(APIView):
    permission_classes = [IsAuthenticated]


class SpeedPingAPIView(SpeedBase):
    def get(self, request):
        limited = _check_light_limit(request)
        if limited:
            return limited
        resp = HttpResponse(status=204)
        resp["Cache-Control"] = "no-store"
        return resp


class SpeedIpAPIView(SpeedBase):
    def get(self, request):
        limited = _check_light_limit(request)
        if limited:
            return limited
        return Response({"ip": client_ip(request)})


def _random_stream(total: int):
    left = total
    while left > 0:
        n = min(CHUNK, left)
        yield os.urandom(n)
        left -= n


class SpeedDownAPIView(SpeedBase):
    def get(self, request):
        raw = request.query_params.get("bytes")
        if raw in (None, ""):
            size = DOWN_DEFAULT_BYTES
        else:
            try:
                size = int(raw)
            except ValueError:
                return Response({"detail": "bytes — целое число.", "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
            if size < 1 or size > DOWN_MAX_BYTES:
                return Response(
                    {"detail": f"bytes — от 1 до {DOWN_MAX_BYTES}.", "code": "invalid"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        limited = _check_measure_limit(request)
        if limited:
            return limited
        resp = StreamingHttpResponse(_random_stream(size), content_type="application/octet-stream")
        resp["Content-Length"] = str(size)
        resp["Cache-Control"] = "no-store"
        resp["Content-Encoding"] = "identity"
        return resp


class SpeedUpAPIView(SpeedBase):
    def post(self, request):
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > UP_MAX_BYTES:
            return Response({"detail": "Тело больше 50 МБ.", "code": "too_large"}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        limited = _check_measure_limit(request)
        if limited:
            return limited
        # читаем поток из WSGI/ASGI напрямую, не через request.data (не разбираем тело)
        stream = request._request
        received = 0
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            received += len(chunk)
            if received > UP_MAX_BYTES:
                return Response({"detail": "Тело больше 50 МБ.", "code": "too_large"}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        return Response({"received": received})
