"""ТЗ кассы 05.11 п.31 / Б-30: согласия, язык и тихие часы покупателей.

Согласия клиента — `consents {service, marketing, updated_at, source}`; язык — ru|ky|en|tr|uz;
тихие часы компании — `Company.quiet_hours {from, to, tz}`. Всё это проверяется в момент отправки
(telegram_bot/services/customer_notify.py), а не только при постановке в очередь.
"""
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ApiError

LANGUAGES = ("ru", "ky", "en", "tr", "uz")
SOURCES = ("bot_start", "cashier", "site", "customer")
DEFAULT_QUIET = {"from": "21:00", "to": "09:00", "tz": "Asia/Bishkek"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def clean_quiet_hours(value):
    """Проверка {from, to, tz}; пустое значение — «по умолчанию»."""
    if value in (None, {}, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("Ожидается объект {from, to, tz}.")
    out = {"from": str(value.get("from") or DEFAULT_QUIET["from"]).strip(),
           "to": str(value.get("to") or DEFAULT_QUIET["to"]).strip(),
           "tz": str(value.get("tz") or DEFAULT_QUIET["tz"]).strip()}
    if not _HHMM.match(out["from"]) or not _HHMM.match(out["to"]):
        raise ValueError("Время в формате ЧЧ:ММ, например 21:00.")
    try:
        ZoneInfo(out["tz"])
    except Exception:
        raise ValueError("Неизвестный часовой пояс.")
    return out


def effective_quiet_hours(company) -> dict:
    q = getattr(company, "quiet_hours", None) or {}
    try:
        return clean_quiet_hours(q) or dict(DEFAULT_QUIET)
    except ValueError:
        return dict(DEFAULT_QUIET)


def quiet_until(company, now=None):
    """Если сейчас тихие часы — вернуть момент их конца (aware), иначе None. from == to → тихих часов нет."""
    q = effective_quiet_hours(company)
    tz = ZoneInfo(q["tz"])
    now = now or timezone.now()
    loc = now.astimezone(tz)
    fh, fm = map(int, q["from"].split(":"))
    th, tm = map(int, q["to"].split(":"))
    start_t, end_t = time(fh, fm), time(th, tm)
    if start_t == end_t:
        return None
    cur = loc.time()
    if start_t < end_t:
        inside = start_t <= cur < end_t
        end_day = loc.date()
    else:  # через полночь
        inside = cur >= start_t or cur < end_t
        end_day = loc.date() + timedelta(days=1) if cur >= start_t else loc.date()
    if not inside:
        return None
    return datetime.combine(end_day, end_t, tzinfo=tz)


def serialize_consents(client) -> dict:
    return {
        "service": bool(client.consent_service), "marketing": bool(client.consent_marketing),
        "updated_at": client.consents_updated_at.isoformat() if client.consents_updated_at else None,
        "source": client.consents_source or None,
    }


def set_client_consents(client, *, service=None, marketing=None, source, language=None):
    """Меняет согласия клиента. /stop — service=False и marketing=False."""
    upd = []
    if service is not None:
        client.consent_service = bool(service)
        upd.append("consent_service")
    if marketing is not None:
        client.consent_marketing = bool(marketing)
        upd.append("consent_marketing")
    if language is not None and language in LANGUAGES:
        client.language = language
        upd.append("language")
    if service is not None or marketing is not None:
        client.consents_updated_at = timezone.now()
        client.consents_source = source
        upd += ["consents_updated_at", "consents_source"]
    if upd:
        client.save(update_fields=upd)
    return client


def stop_client_by_chat(company, chat_id) -> int:
    """/stop в чате: все клиенты компании с этим telegram_chat_id → оба согласия выключены."""
    from apps.main.models import Client
    n = 0
    for c in Client.objects.filter(company=company, telegram_chat_id=str(chat_id)):
        set_client_consents(c, service=False, marketing=False, source="bot_start")
        n += 1
    return n


class ClientConsentsView(APIView):
    """GET/PATCH /api/main/clients/{id}/consents/ — согласия и язык клиента.

    PATCH {service?, marketing?, language?}: сотрудник компании, source=cashier. Сам покупатель меняет согласия в боте."""
    permission_classes = [permissions.IsAuthenticated]

    def _client(self, request, client_id):
        from apps.main.models import Client
        user = request.user
        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        if not company:
            raise ApiError("permission_denied", "У пользователя не найдена компания.", status_code=403)
        c = Client.objects.filter(company=company, pk=client_id).first()
        if not c:
            raise ApiError("not_found", "Клиент не найден.", status_code=404)
        return c

    def _out(self, c):
        return {"client": str(c.id), "consents": serialize_consents(c), "language": c.language}

    def get(self, request, client_id):
        return Response(self._out(self._client(request, client_id)))

    def patch(self, request, client_id):
        c = self._client(request, client_id)
        d = request.data
        for k in ("service", "marketing"):
            if k in d and not isinstance(d[k], bool):
                raise ApiError("invalid", f"{k}: ожидается true/false.", fields={k: ["Недопустимое значение"]})
        if "language" in d and d["language"] not in LANGUAGES:
            raise ApiError("invalid", "language: ru, ky, en, tr или uz.", fields={"language": ["Недопустимое значение"]})
        set_client_consents(c, service=d.get("service"), marketing=d.get("marketing"), source="cashier",
                            language=d.get("language"))
        return Response(self._out(c))
