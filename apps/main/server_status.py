"""П1-2 (14.11.2026, p1214): состояние сервера и каталог кодов ошибок для кассы и сайта.

GET api/public/status/      — без входа: {status: ok|degraded|maintenance, message{ru,ky,en,tr,uz}, until, updated_at,
                              planned: {starts_at, ends_at, message{…}} | null}; кэш 30 с.
GET api/public/error-codes/ — без входа: каталог {version, count, codes:[{code, http[], kind, area, detail_ru}]}.
Статус задаёт поддержка командой:  manage.py server_status set|plan|clear|show  (см. команду).
Плановые работы попадают и в ленту main/changes/ событием maintenance_planned.
"""
import json
import os
from pathlib import Path

from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

STATUS_FILE = Path(os.environ.get("NUR_STATUS_FILE", "/home/nur/var/server_status.json"))
CODES_FILE = Path(__file__).with_name("error_codes.json")
CACHE_KEY = "nur:public_status:v1"
LANGS = ("ru", "ky", "en", "tr", "uz")
STATUSES = ("ok", "degraded", "maintenance")


def read_state() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATUS_FILE)
    cache.delete(CACHE_KEY)


def messages(raw) -> dict:
    raw = raw or {}
    ru = raw.get("ru") or ""
    return {lang: raw.get(lang) or ru for lang in LANGS}


def _past(iso) -> bool:
    d = parse_datetime(iso) if iso else None
    return d is not None and d <= timezone.now()


def build_status() -> dict:
    st = read_state()
    status = st.get("status") if st.get("status") in STATUSES else "ok"
    until = st.get("until")
    if status != "ok" and until and _past(until):  # время вышло — снова ok
        status, until = "ok", None
    planned = st.get("planned") or None
    if planned and _past(planned.get("ends_at")):
        planned = None
    if status == "ok":
        try:  # база недоступна — degraded
            with connection.cursor() as c:
                c.execute("SELECT 1")
        except Exception:  # noqa: BLE001
            status = "degraded"
    return {
        "status": status,
        "message": messages(st.get("message")) if status != "ok" else messages({}),
        "until": until if status != "ok" else None,
        "updated_at": st.get("updated_at") or None,
        "planned": {"starts_at": planned.get("starts_at"), "ends_at": planned.get("ends_at"),
                    "message": messages(planned.get("message"))} if planned else None,
    }


class PublicStatusAPIView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = []

    def get(self, request):
        data = cache.get(CACHE_KEY)
        if data is None:
            data = build_status()
            cache.set(CACHE_KEY, data, 30)
        resp = Response(data)
        resp["Cache-Control"] = "public, max-age=30"
        return resp


class PublicErrorCodesAPIView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        try:
            data = json.loads(CODES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {"version": None, "count": 0, "codes": []}
        resp = Response(data)
        resp["Cache-Control"] = "public, max-age=3600"
        return resp
