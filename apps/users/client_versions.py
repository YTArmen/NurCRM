"""
cver2011: минимальная и последняя версия клиентов (ТЗ Бекназара, задача 7).

Где задаются (по умолчанию всё null — проверки нет):
  1) файл /home/nur/client_versions.json (меняется БЕЗ перезапуска, читается по mtime):
       {"kassa": {"min": "1.17.80", "latest": "1.17.90"}, "owner": {...}, "android_pos": {...}, "mobile": {...}}
  2) или переменные окружения CLIENT_KASSA_MIN / CLIENT_KASSA_LATEST / CLIENT_OWNER_MIN / ... (нужен перезапуск).

Отдаём:
  GET api/public/client-versions/   — без входа;
  capabilities → "client_versions" (одной строкой в core/capabilities.py, вписывает главное окно).
426 client_outdated — только запросам с токеном, только если min задан и версия из X-NurMarket-Client меньше.
capabilities и client-versions не блокируются (клиент должен увидеть причину и ссылку на обновление).
"""
import json
import os

from django.conf import settings
from rest_framework import permissions
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import APIView

KEYS = ("kassa", "android_pos", "owner", "mobile")
TITLES = {"kassa": "касса на ПК", "android_pos": "Android-касса", "owner": "программа владельца", "mobile": "приложение"}
_cache = {"mtime": None, "data": None}


def _file():
    return getattr(settings, "CLIENT_VERSIONS_FILE", None) or os.environ.get("CLIENT_VERSIONS_FILE") \
        or os.path.join(str(getattr(settings, "BASE_DIR", "/home/nur")), "client_versions.json")


def _ver(v):
    if v in (None, ""):
        return None
    s = str(v).strip()
    try:
        tuple(int(x) for x in s.split("."))
    except ValueError:
        return None
    return s


def _tuple(s):
    t = tuple(int(x) for x in s.split("."))
    return t + (0,) * (4 - len(t))


def load() -> dict:
    raw = {}
    path = _file()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if mtime is not None:
        if _cache["mtime"] != mtime:
            try:
                with open(path, encoding="utf-8") as fh:
                    _cache["data"] = json.load(fh) or {}
            except (OSError, ValueError):
                _cache["data"] = {}
            _cache["mtime"] = mtime
        raw = _cache["data"] if isinstance(_cache["data"], dict) else {}
    out = {}
    for k in KEYS:
        row = raw.get(k) if isinstance(raw.get(k), dict) else {}
        env = k.upper()
        out[k] = {
            "min": _ver(row.get("min", os.environ.get(f"CLIENT_{env}_MIN"))),
            "latest": _ver(row.get("latest", os.environ.get(f"CLIENT_{env}_LATEST"))),
        }
    return out


def block() -> dict:
    """Для capabilities: {"kassa": {"min", "latest"}, "android_pos": …, "owner": …, "mobile": …}."""
    return load()


def request_kind_version(request):
    from core.api_compat import _raw_kind, parse_client

    kind, ver = parse_client(request)
    if not kind or not ver:
        return None, None
    if _raw_kind(request) == "android-pos":
        kind = "android_pos"
    return (kind if kind in KEYS else None), ".".join(map(str, ver))


class ClientOutdated(APIException):
    status_code = 426

    def __init__(self, body):
        super().__init__(detail=body)
        self.detail = body


def check(request):
    """426, если min для этого клиента задан и версия меньше. Иначе ничего."""
    kind, ver = request_kind_version(request)
    if not kind:
        return
    row = load().get(kind) or {}
    need = row.get("min")
    if not need or _tuple(ver) >= _tuple(need):
        return
    raise ClientOutdated({
        "code": "client_outdated", "client": kind, "version": ver, "min": need, "latest": row.get("latest"),
        "detail": f"Версия ({TITLES.get(kind, kind)}) {ver} устарела. Обновите программу до {row.get('latest') or need} или новее.",
    })


EXEMPT_SUFFIXES = ("/capabilities", "/client-versions")


def exempt(request) -> bool:
    return (getattr(request, "path", "") or "").rstrip("/").endswith(EXEMPT_SUFFIXES)


class PublicClientVersionsAPIView(APIView):
    """GET api/public/client-versions/ — без входа."""
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        resp = Response(block())
        resp["Cache-Control"] = "no-cache"
        return resp
