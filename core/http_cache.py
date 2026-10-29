"""
ТЗ 29.10 Б-46 (п.1): единый ETag и 304 для GET-списков и справочников.

`ETagMixin` ставят первым в базовых классах DRF-вьюхи (до APIView/generics):
  - ответ 200 на GET получает ETag (если вьюха не поставила свой) — хэш содержимого ответа;
  - If-None-Match совпал (слабые W/, списки через запятую, `*`) → 304 без тела;
  - 304 не считается в лимит 429: учтённый запрос возвращается в окно троттлинга.
Работает только для GET; остальные методы не затрагиваются.
"""
import hashlib

from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response


def etag_matches(header_value, etag) -> bool:
    """If-None-Match против текущего ETag: `*`, список через запятую, слабые W/ и кавычки не мешают."""
    if not header_value or not etag:
        return False
    if header_value.strip() == "*":
        return True
    bare = str(etag).strip()
    if bare.startswith("W/"):
        bare = bare[2:]
    bare = bare.strip('"')
    for token in header_value.split(","):
        t = token.strip()
        if t.startswith("W/"):
            t = t[2:]
        if t.strip('"') == bare:
            return True
    return False


def make_etag(data) -> str:
    raw = JSONRenderer().render(data)
    return '"' + hashlib.sha1(raw).hexdigest()[:24] + '"'


def _refund_throttles(throttles):
    """Возвращает последний учтённый запрос в окно троттлинга (DRF кладёт его в history[0])."""
    for t in throttles:
        key = getattr(t, "key", None)
        cache = getattr(t, "cache", None)
        duration = getattr(t, "duration", None)
        if not key or cache is None or not duration:
            continue
        history = cache.get(key, [])
        if history:
            history.pop(0)
            cache.set(key, history, duration)


class ETagMixin:
    _etag_throttles = None

    def get_throttles(self):
        throttles = super().get_throttles()
        self._etag_throttles = throttles
        return throttles

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if request.method != "GET":
            return response
        etag = response.get("ETag")
        if response.status_code == 200 and isinstance(response, Response):
            if not etag:
                etag = make_etag(response.data)
                response["ETag"] = etag
            if "Cache-Control" not in response:
                response["Cache-Control"] = "private, no-cache"
            if etag_matches(request.headers.get("If-None-Match"), etag):
                response.status_code = 304
                response.data = None
                response.content = b""
        if response.status_code == 304:
            _refund_throttles(self._etag_throttles or [])
        return response
