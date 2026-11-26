"""Б-67: источник и устройство продажи/возврата. Поля пишет только сервер (из тела или заголовка)."""
import re

from rest_framework.exceptions import ValidationError

from core.api_compat import parse_client

SOURCES = ("pos", "mobile", "web", "owner", "telegram_bot", "voice_ai")
DEVICE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
DEVICE_NAME_MAX = 64
# source / device_key / device_name не входят в сравнение тела при повторе Idempotency-Key
IDEMPOTENCY_IGNORED_KEYS = ("source", "device_key", "device_name")

_HEADER_SOURCE = {"kassa": "pos", "mobile": "mobile", "owner": "owner"}


def _base_request(request):
    """Для запроса из checkout/batch (обёртка) — исходный запрос с заголовками."""
    return getattr(request, "_request_obj", request)


def source_from_header(request) -> str:
    kind, _ver = parse_client(_base_request(request))
    return _HEADER_SOURCE.get(kind, "web")


def resolve_origin(request, data=None) -> dict:
    """{"source","device_key","device_name"}; 400 при неверных значениях. Без source — по заголовку клиента."""
    data = data if data is not None else getattr(request, "data", None)
    if not hasattr(data, "get"):
        data = {}
    errors = {}

    source = data.get("source")
    if source in (None, ""):
        source = source_from_header(request)
    else:
        source = str(source).strip()
        if source not in SOURCES:
            errors["source"] = [f"Допустимо: {', '.join(SOURCES)}."]

    device_key = data.get("device_key")
    device_key = "" if device_key is None else str(device_key).strip()
    if device_key and not DEVICE_KEY_RE.match(device_key):
        errors["device_key"] = ["8–64 символа: латиница, цифры, _ и -."]

    device_name = data.get("device_name")
    device_name = "" if device_name is None else str(device_name).strip()
    if len(device_name) > DEVICE_NAME_MAX:
        errors["device_name"] = [f"Не длиннее {DEVICE_NAME_MAX} символов."]

    if errors:
        from core.api_compat import is_strict

        if is_strict(request):
            raise ValidationError(errors)
        # горячая правка 29.10: старые кассы и сайт — неверные значения не отклоняют продажу
        if "source" in errors:
            source = source_from_header(request)
            if source not in SOURCES:
                source = SOURCES[0]
        if "device_key" in errors:
            device_key = ""
        if "device_name" in errors:
            device_name = device_name[:DEVICE_NAME_MAX]
    return {"source": source, "device_key": device_key, "device_name": device_name}


def user_brief(user):
    """created_by {id, name}."""
    if user is None:
        return None
    name = ""
    getter = getattr(user, "get_full_name", None)
    if getter:
        name = getter() or ""
    return {"id": str(user.pk), "name": name or getattr(user, "email", "") or ""}


def record_return_origin(request, sale, data=None):
    """Запоминает устройство возврата (последний возврат продажи). Не должен ломать возврат."""
    from apps.main.models import SaleReturn
    from apps.main.tz26.pos_sync.models import ReturnOrigin

    try:
        origin = resolve_origin(request, data)
    except ValidationError:
        origin = {"source": source_from_header(request), "device_key": "", "device_name": ""}
    ret = SaleReturn.objects.filter(sale=sale).order_by("-created_at", "-id").first()
    if ret is not None:
        ReturnOrigin.objects.get_or_create(sale_return=ret, defaults=origin)


def return_origin_fields(sale_return) -> dict:
    """source, device_key, device_name, created_by для pos/returns/ (только чтение)."""
    origin = getattr(sale_return, "origin", None) if sale_return is not None else None
    return {
        "source": origin.source if origin else "",
        "device_key": origin.device_key if origin else "",
        "device_name": origin.device_name if origin else "",
        "created_by": user_brief(getattr(sale_return, "user", None)),
    }
