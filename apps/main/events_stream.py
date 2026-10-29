"""
ТЗ 29.10 Б-102: поток событий для касс и программы владельца.

Источник один — лента ChangeEvent (apps.main.change_feed.record_change, запись после коммита
и group_send в группу компании). Здесь:
  * to_stream_event(ev) — перевод записи ленты в формат потока ТЗ;
  * allowed_for(msg, ctx) — права и филиал;
  * вьюха GET main/events/stream/ — заглушка «используйте WebSocket» (см. ниже).
"""
from datetime import timezone as dt_tz

from django.http import HttpResponse
from django.utils.dateparse import parse_datetime

MAX_IDS = 500
OWNER_ROLES = ("owner", "admin")
# Приложения настроек, которые видит только владелец/админ
OWNER_ONLY_APPS = ("owner",)

_SALE_STATE = {"shift_opened": "opened", "shift_closed": "closed"}


def _at_utc(value: str) -> str:
    dt = parse_datetime(value) if value else None
    if dt is None:
        return value
    return dt.astimezone(dt_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ids(ev) -> list:
    d = ev.get("data") or {}
    if isinstance(d.get("products"), list):
        ids = [str(i) for i in d["products"]]
    elif d.get("product") is not None:
        ids = [str(d["product"])]
    elif ev.get("entity_id"):
        ids = [str(ev["entity_id"])]
    else:
        ids = []
    return ids


def to_stream_event(ev: dict):
    """Запись ленты (serialize_event) → сообщение потока ТЗ или None, если тип потоку не нужен."""
    t = ev.get("type") or ""
    d = ev.get("data") or {}
    out_type, extra = None, {}
    ids = _ids(ev)

    if t == "product_changed":
        if d.get("what") == "price":
            out_type = "price_changed"
        elif d.get("what") == "quantity":
            return None  # остаток отдельно приходит как stock_changed
        else:
            out_type = "product_changed"
    elif t == "stock_changed":
        out_type = "stock_changed"
    elif t == "app_settings_changed":
        out_type = "settings_changed"
        extra = {"app": d.get("app") or ev.get("entity_id"), "keys": list(d.get("keys") or [])}
        ids = []
    elif t == "sale_paid":
        out_type = "sale_created"
        sale = d.get("sale") if d.get("sale") is not None else ev.get("entity_id")
        ids = [str(sale)] if sale not in (None, "") else []
        extra = {"sale_id": str(sale) if sale not in (None, "") else None,
                 "cashbox": d.get("cashbox"), "shift": d.get("shift")}
    elif t == "sale_returned":
        out_type = "sale_changed"
        sale = d.get("sale")
        ids = [str(sale)] if sale else ids
        extra = {"sale_id": str(sale) if sale else None, "shift": d.get("shift")}
    elif t in _SALE_STATE:
        out_type = "shift_changed"
        extra = {"state": _SALE_STATE[t], "cashbox": d.get("cashbox")}
        ids = [str(d["shift"])] if d.get("shift") else ids
    elif t == "cash_moved":
        out_type = "shift_changed"
        extra = {"state": d.get("state"), "cashbox": d.get("cashbox")}
        ids = []
    elif t == "staff_changed":
        out_type = "staff_changed"
    elif t == "client_changed":
        out_type = "client_changed"
    elif t == "bonus_changed":
        out_type = "bonus_changed"
    elif t in ("debt_created", "debt_paid"):
        out_type = "debt_changed"
        if d.get("client") is not None:
            ids = [str(d["client"])]
    elif t == "features_changed":
        out_type = "license_changed"
        ids = []
    if out_type is None:
        return None

    msg = {
        "id": ev.get("id"),
        "type": out_type,
        "at": _at_utc(ev.get("at")),
        "branch": ev.get("branch"),
        "ids": ids if len(ids) <= MAX_IDS else None,
        "actor": ev.get("actor"),
    }
    msg.update(extra)
    return msg


def allowed_for(msg: dict, *, role, user_id, branch_id) -> bool:
    """Права: только то, что пользователь вправе читать. branch_id — фильтр филиала (None = все)."""
    if msg is None:
        return False
    if branch_id and msg.get("branch") and msg["branch"] != str(branch_id):
        return False
    is_owner = role in OWNER_ROLES
    t = msg["type"]
    if is_owner:
        return True
    if t == "settings_changed" and (msg.get("app") or "") in OWNER_ONLY_APPS:
        return False
    if t == "license_changed":
        return False
    if t == "staff_changed":
        # сотрудник узнаёт только об изменении собственной учётной записи (права, роль)
        return str(user_id) in (msg.get("ids") or [])
    return True


STREAM_WS_PATH = "/ws/main/events/"


def _stream_url(request):
    from django.conf import settings as dj

    host = request.get_host()
    if host.startswith(("localhost", "127.0.0.1", "testserver")):
        return ("ws://" if not host.startswith("testserver") else "wss://") + host + STREAM_WS_PATH
    return (getattr(dj, "PUBLIC_WS_BASE", None) or f"wss://{host}") + STREAM_WS_PATH


def events_stream_http(request):
    """
    GET /api/main/events/stream/ — НЕ держит соединение.

    HTTP обслуживает gunicorn (3 воркера): долгие SSE-соединения заняли бы воркеры и положили
    остальные запросы. Поэтому поток живёт в WebSocket (daphne, Channels+Redis), а этот адрес
    для text/event-stream отвечает сразу одним событием use_websocket и закрывается.
    """
    import json

    if "text/event-stream" not in (request.headers.get("Accept") or ""):
        from django.http import JsonResponse

        return JsonResponse({"code": "use_websocket", "url": _stream_url(request)})
    body = "retry: 3000\nevent: use_websocket\ndata: " + json.dumps({"url": _stream_url(request)}) + "\n\n"
    resp = HttpResponse(body, content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp
