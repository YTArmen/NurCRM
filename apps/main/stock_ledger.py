"""П0-4 К-1 (14.11.2026, led1411): журнал остатков товара.

Каждое изменение main_product.quantity записывает триггер PostgreSQL (nur_stock_ledger) в main_stockledger:
было / стало / разница / кто / каким запросом. Ни одно место в коде не может изменить остаток мимо журнала.

Кто и откуда: middleware кладёт запрос в contextvar, а обёртка запросов к базе перед каждым
UPDATE/INSERT в "main_product" ставит в той же команде set_config('nur.ledger', <json>, true):
{u: пользователь, s: имя адреса, m: метод, d: id из адреса, c: клиент, r: X-Request-Id}.
Без запроса (Celery, команды) — source "system". Сбой журнала никогда не ломает продажу:
триггер ловит свои ошибки, обёртка при любой ошибке выполняет исходный запрос как есть.
Выключатель: переменная окружения NUR_LEDGER_CTX=0 (только автор/адрес, сам журнал пишет триггер).
"""
import contextvars
import json
import logging
import os

from django.utils.functional import empty

log = logging.getLogger("nurcrm.stock_ledger")
_REQ = contextvars.ContextVar("nur_ledger_request", default=None)
_PREFIXES = ('UPDATE "main_product" ', 'INSERT INTO "main_product" ',
             # hist2111: история товара — удаление и штрихкоды/фото
             'DELETE FROM "main_product" ', 'INSERT INTO "main_productalternatebarcode" ',
             'UPDATE "main_productalternatebarcode" ', 'DELETE FROM "main_productalternatebarcode" ',
             'INSERT INTO "main_productimage" ', 'UPDATE "main_productimage" ', 'DELETE FROM "main_productimage" ')
_SET = "SELECT set_config('nur.ledger', %s, true); "


def _ctx_json():
    req = _REQ.get()
    if req is None:
        return None
    uid = None
    u = req.__dict__.get("user")
    try:
        if u is not None and getattr(u, "_wrapped", None) is not empty:
            if getattr(u, "is_authenticated", False):
                uid = str(u.pk)
    except Exception:
        uid = None
    rm = getattr(req, "resolver_match", None)
    doc = None
    if rm is not None:
        doc = rm.kwargs.get("pk") or rm.kwargs.get("id") or rm.kwargs.get("supplier_id")
    try:
        client = (req.headers.get("X-NurMarket-Client") or "")[:32]
    except Exception:
        client = ""
    doc = getattr(req, "_ledger_doc", None) or doc  # К-2: документ движения задаёт себя сам
    return json.dumps({
        "u": uid, "s": (rm.url_name if rm is not None and rm.url_name else req.path[:64]),
        "m": req.method, "d": str(doc) if doc else None, "c": client or None,
        "r": getattr(req, "request_id", None),
        "o": getattr(req, "_ledger_at", None),  # К-2: настоящее время движения (офлайн)
        **(getattr(req, "_ledger_extra", None) or {}),
        **_who(req),  # hist2111: IP и браузер/программа для истории товара  # k458: цена/срок/партия для партий
    })


def _who(req):
    try:
        m = req.META
        ip = m.get("HTTP_CF_CONNECTING_IP") or (m.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip() or m.get("REMOTE_ADDR")
        return {"i": (ip or "")[:64] or None, "a": (m.get("HTTP_USER_AGENT") or "")[:160] or None}
    except Exception:
        return {}


def execute_wrapper(execute, sql, params, many, context):
    if many or params is None or isinstance(params, dict) or not isinstance(sql, str) or not sql.startswith(_PREFIXES):
        return execute(sql, params, many, context)
    try:
        ctx = _ctx_json()
    except Exception:
        ctx = None
    if not ctx:
        return execute(sql, params, many, context)
    return execute(_SET + sql, [ctx, *params], many, context)


def install(sender=None, connection=None, **kwargs):
    """connection_created: обёртка на каждое новое соединение с PostgreSQL."""
    if connection is None or connection.vendor != "postgresql" or os.environ.get("NUR_LEDGER_CTX", "1") == "0":
        return
    if execute_wrapper not in connection.execute_wrappers:
        connection.execute_wrappers.append(execute_wrapper)


class StockLedgerContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _REQ.set(request)
        try:
            return self.get_response(request)
        finally:
            _REQ.reset(token)


def _connect():
    from django.db.backends.signals import connection_created

    connection_created.connect(install, dispatch_uid="nur_stock_ledger_ctx")


_connect()
