"""
BE2-11: Idempotency-Key для операций, которые касса повторяет после сбоя связи.

Повтор с тем же ключом в течение 7 суток → первый ответ + заголовок Idempotent-Replayed: true.
Тот же ключ с другим запросом → 409 idempotency_conflict; пока первый запрос выполняется → 409 in_progress.
Неуспешный ответ (4xx/5xx) не запоминается: ошибку можно исправить и повторить с тем же ключом.
Без заголовка адрес работает как раньше.
"""
import functools
import hashlib
import json
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder

from apps.integrations.models import IdempotencyRecord

TTL = timedelta(days=7)
MAX_KEY_LENGTH = 255
IN_PROGRESS_TIMEOUT = timedelta(minutes=5)


def _body_hash(request) -> str:
    data = getattr(request, "data", None)
    if isinstance(data, dict):
        ignored = {"idempotency_key"}
        if "/pos/checkout" in (getattr(request, "path", "") or ""):
            ignored |= {"source", "device_key", "device_name"}  # Б-67: в сравнение тела не входят
        data = {k: v for k, v in data.items() if k not in ignored}
    try:
        raw = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        raw = repr(data)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _error(detail, code, http_status, **headers):
    resp = Response({"detail": detail, "code": code}, status=http_status)
    for k, v in headers.items():
        resp[k] = v
    return resp


def _request_key(request, body_key: bool) -> str:
    headers = getattr(request, "headers", {})
    meta = getattr(request, "META", {})
    key = (headers.get("Idempotency-Key") or meta.get("HTTP_IDEMPOTENCY_KEY") or "").strip()
    if not key and body_key and isinstance(getattr(request, "data", None), dict):
        key = str(request.data.get("idempotency_key") or "").strip()
    return key


def _normalize_scope(request) -> str:
    path = getattr(request, "path", "") or ""
    if path.startswith("/api/"):
        path = path[4:]
    if not path.endswith("/"):
        path = path + "/"
    method = (getattr(request, "method", "") or "POST").upper()
    return f"{method} {path}"[:255]


def idempotent(handler=None, *, body_key=False, replay_flag=False):
    """
    Декоратор метода APIView (post/patch/put/delete).
    body_key=True — ключ можно передать и полем idempotency_key в теле (если нет заголовка).
    replay_flag=True — при повторе в теле ответа (dict) ставится replayed=true.
    """
    if handler is None:
        return functools.partial(idempotent, body_key=body_key, replay_flag=replay_flag)

    @functools.wraps(handler)
    def wrapper(view, request, *args, **kwargs):
        key = _request_key(request, body_key)
        company_id = getattr(getattr(request, "user", None), "company_id", None)
        if not key or company_id is None:
            return handler(view, request, *args, **kwargs)
        if len(key) > MAX_KEY_LENGTH:
            return _error("Idempotency-Key не длиннее 255 символов.", "invalid", status.HTTP_400_BAD_REQUEST)

        scope = _normalize_scope(request)
        body_hash = _body_hash(request)
        IdempotencyRecord.objects.filter(
            company_id=company_id, key=key, created_at__lt=timezone.now() - TTL
        ).delete()
        try:
            with transaction.atomic():
                record, created = IdempotencyRecord.objects.get_or_create(
                    company_id=company_id, key=key, defaults={"scope": scope, "body_hash": body_hash}
                )
        except IntegrityError:
            record, created = IdempotencyRecord.objects.get(company_id=company_id, key=key), False

        if not created:
            if record.scope != scope or record.body_hash != body_hash:
                return _error(
                    "Этот Idempotency-Key уже использован для другого запроса.",
                    "idempotency_conflict", status.HTTP_409_CONFLICT,
                )
            if record.state != IdempotencyRecord.State.DONE:
                if record.created_at and record.created_at < timezone.now() - IN_PROGRESS_TIMEOUT:
                    # Устаревший повисший запрос — очищаем и запускаем заново
                    record.delete()
                    try:
                        with transaction.atomic():
                            record, created = IdempotencyRecord.objects.get_or_create(
                                company_id=company_id, key=key, defaults={"scope": scope, "body_hash": body_hash}
                            )
                    except IntegrityError:
                        record, created = IdempotencyRecord.objects.get(company_id=company_id, key=key), False
                    if not created and record.state != IdempotencyRecord.State.DONE:
                        return _error(
                            "Запрос с этим Idempotency-Key ещё выполняется.",
                            "in_progress", status.HTTP_409_CONFLICT, **{"Retry-After": "1"},
                        )
                else:
                    return _error(
                        "Запрос с этим Idempotency-Key ещё выполняется.",
                        "in_progress", status.HTTP_409_CONFLICT, **{"Retry-After": "1"},
                    )
            if record.state == IdempotencyRecord.State.DONE:
                body = record.response_body
                # ТЗ Н-08/Н-10: признак повтора в теле — по флагу или если ответ его уже содержит.
                if isinstance(body, dict) and (replay_flag or "replayed" in body):
                    body = {**body, "replayed": True}
                resp = Response(body, status=record.status_code)
                resp["Idempotent-Replayed"] = "true"
                return resp

        try:
            response = handler(view, request, *args, **kwargs)
        except Exception:
            record.delete()
            raise
        if 200 <= response.status_code < 300 and hasattr(response, "data"):
            record.state = IdempotencyRecord.State.DONE
            record.status_code = response.status_code
            # Тем же кодировщиком, что и рендерер DRF, — повтор отдаёт байт-в-байт тот же JSON.
            record.response_body = json.loads(json.dumps(response.data, cls=JSONEncoder))
            record.save(update_fields=["state", "status_code", "response_body"])
        else:
            record.delete()
        return response

    return wrapper


def purge_expired() -> int:
    deleted, _ = IdempotencyRecord.objects.filter(created_at__lt=timezone.now() - TTL).delete()
    return deleted
