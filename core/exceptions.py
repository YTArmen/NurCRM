"""
Единый формат ошибок API (ТЗ-BE-2026-02 п. 2.1/2.4; ТЗ-28 §4 — машинные коды).

Тело любой ошибки:

    {"code": "insufficient_stock",            # машинный код — клиенты сравнивают ТОЛЬКО его
     "detail": "Товара «Mars» осталось 2 шт.", # текст для человека, может меняться
     "fields": {"items[0].qty": ["Больше остатка"]},   # только при ошибках полей
     "data": {"product_id": "...", "available": "2"}}  # только если есть что передать

Совместимость: ошибки полей по-прежнему лежат и на верхнем уровне
({"amount": ["..."]}) — сайт показывает их ключи как поля.

Бросать из кода:

    raise ApiError("shift_not_open", "Смена не открыта.", status_code=409)
    raise ApiError("insufficient_stock", "…", data={"available": "2"})
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

STATUS_CODES = {
    400: "invalid",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    415: "unsupported_media_type",
    429: "rate_limited",
}

# Коды из таблицы ТЗ-28 §4 — не переименовывать, только дополнять.
KNOWN_CODES = {
    "shift_not_open", "shift_closed", "shift_of_other_cashier",
    "insufficient_stock", "price_changed", "total_mismatch",
    "no_debt", "debt_overpay",
    "already_returned", "return_qty_exceeds",
    "idempotency_conflict", "idempotency_in_progress",
    "device_revoked", "token_expired",
    "tariff_limit", "feature_disabled",
    "rate_limited", "validation_error",
    "client_exists", "not_found", "permission_denied", "not_authenticated", "conflict",
}

_META_KEYS = ("code", "detail", "fields", "data", "non_field_errors")


class ApiError(exceptions.APIException):
    """Ошибка с машинным кодом и необязательными данными."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "invalid"
    default_detail = "Ошибка запроса."

    def __init__(self, code, detail=None, *, status_code=None, data=None, fields=None, top=None):
        if status_code is not None:
            self.status_code = int(status_code)
        self.code = code
        self.extra_data = data
        self.extra_fields = fields
        self.extra_top = top  # поля верхнего уровня ответа (например limit/current)
        super().__init__(detail=detail or self.default_detail, code=code)


def _django_validation_detail(exc: DjangoValidationError):
    if hasattr(exc, "error_dict"):
        return {field: [str(m) for m in msgs] for field, msgs in exc.message_dict.items()}
    return {"detail": " ".join(str(m) for m in exc.messages)}


def _flatten_fields(value, prefix=""):
    """{"items": {0: {"qty": ["…"]}}} → {"items[0].qty": ["…"]}"""
    out = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("code", "detail") and not prefix:
                continue
            key = f"{prefix}[{k}]" if (prefix and str(k).isdigit()) else (f"{prefix}.{k}" if prefix else str(k))
            out.update(_flatten_fields(v, key))
    elif isinstance(value, (list, tuple)):
        if value and all(isinstance(x, (dict, list, tuple)) for x in value):
            for i, v in enumerate(value):
                out.update(_flatten_fields(v, f"{prefix}[{i}]"))
        else:
            out[prefix or "non_field_errors"] = [str(x) for x in value]
    else:
        out[prefix or "non_field_errors"] = [str(value)]
    return out


def _first_message(fields: dict) -> str:
    for _k, msgs in fields.items():
        if msgs:
            return str(msgs[0])
    return "Проверьте данные запроса."


def _api_exception_handler(exc, context):
    # Ошибка правил модели (full_clean) — это ошибка данных, а не сбой сервера: 400, не 500.
    if isinstance(exc, DjangoValidationError):
        exc = exceptions.ValidationError(_django_validation_detail(exc))

    response = drf_exception_handler(exc, context)
    if response is None:
        return response

    data = response.data
    if isinstance(data, list):
        # ValidationError(["…"]) — список сообщений без полей
        data = {"detail": " ".join(str(x) for x in data) or "Ошибка запроса.", "non_field_errors": [str(x) for x in data]}
        response.data = data
    if not isinstance(data, dict):
        return response

    if isinstance(exc, ApiError):
        data["code"] = exc.code
        if exc.extra_data is not None:
            data["data"] = exc.extra_data
        if exc.extra_fields:
            data["fields"] = exc.extra_fields
        if getattr(exc, "extra_top", None):
            data.update(exc.extra_top)
        if "detail" in data:
            data["detail"] = str(data["detail"])
        return response

    if "detail" in data:
        detail = data["detail"]
        if "code" not in data:
            code = getattr(detail, "code", None)
            if not code or (code == "invalid" and response.status_code != status.HTTP_400_BAD_REQUEST):
                code = STATUS_CODES.get(response.status_code, "error")
            data["code"] = code
        data["detail"] = str(detail)
    else:
        # Ошибки по полям: оставляем ключи на верхнем уровне (совместимость) и
        # добавляем code/detail/fields по ТЗ-28 §4.
        field_errors = {k: v for k, v in data.items() if k not in ("code", "fields")}
        fields = _flatten_fields(field_errors)
        data.setdefault("code", "validation_error" if response.status_code == 400 else STATUS_CODES.get(response.status_code, "error"))
        data.setdefault("fields", fields)
        data.setdefault("detail", _first_message(fields))

    if response.status_code == 429:
        data["code"] = "rate_limited"
        wait = getattr(exc, "wait", None)
        if wait is not None and "Retry-After" not in response:
            response["Retry-After"] = str(int(wait) + 1)
    return response


_LEAD_KEYS = ("detail", "code", "data", "errors", "fields")


def api_exception_handler(exc, context):
    """
    Единое ТЗ 26.10.2026, Б-11 / О1: `detail` — первым ключом, затем code, data, errors, fields.
    Старые разборщики кассы показывают первое строковое поле ответа.
    """
    response = _api_exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict) and "detail" in response.data:
        data = response.data
        ordered = {k: data[k] for k in _LEAD_KEYS if k in data}
        ordered.update((k, v) for k, v in data.items() if k not in ordered)
        response.data = ordered
    return response
