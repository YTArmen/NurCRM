"""
События для внешних интеграций (вебхуки).

    emit_event(company_id, "sale.paid", {...})

Отправка идёт после коммита транзакции через Celery; ошибки здесь никогда не
ломают основную операцию (продажу, закрытие смены и т.д.).
"""
import ipaddress
import logging
import socket
import uuid
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

EVENTS = (
    "sale.paid",
    "sale.returned",
    "shift.opened",
    "shift.closed",
    "stock.low",
    "company.updated",
    "appointment.created",
    "order.created",
    # ТЗ Н-31: новые типы ленты изменений (apps.main.change_feed)
    "debt_paid",
    "client_changed",  # ТЗ 29.10 Б-102
    "bonus_changed",
    "staff_changed",
    "cash_moved",
    "debt_created",
    "app_settings_changed",
    "product_changed",
    "stock_changed",
    "rental_created",
    "rental_updated",
    "rental_extended",
    "supplier_debt_changed",
    "features_changed",
    "pos_request_changed",
    # ТЗ 26.10 (Б-62, Б-69, Б-72, Б-60)
    "task.assigned",
    "task.reminder",
    "print_job_created",
    "scanner_scan",
    "customer_order_ready",
)


def validate_public_url(url: str) -> None:
    """Запрещаем вебхуки на внутренние адреса сервера (SSRF). ValueError — если нельзя."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Адрес должен начинаться с http:// или https://")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError("Не удаётся найти адрес вебхука.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("Адрес вебхука указывает во внутреннюю сеть.")


def _json_safe(value):
    from decimal import Decimal
    from datetime import date, datetime

    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01"))) if value == value.quantize(Decimal("0.01")) else str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _dispatch(company_id, event: str, data: dict) -> None:
    from .models import WebhookEndpoint
    from .tasks import deliver_webhook

    # ТЗ Н-31: частые типы ленты — не ходим в БД за подписками каждый раз (кэш на 30 с)
    from apps.main.change_feed import NEW_EVENT_TYPES
    if event in NEW_EVENT_TYPES:
        from django.core.cache import cache
        key = f"wh_has_eps:{company_id}"
        has = cache.get(key)
        if has is None:
            has = WebhookEndpoint.objects.filter(company_id=company_id, is_active=True).exists()
            cache.set(key, has, 30)
        if not has:
            return

    endpoints = [
        ep for ep in WebhookEndpoint.objects.filter(company_id=company_id, is_active=True).only("id", "events")
        if event in (ep.events or [])
    ]
    if not endpoints:
        return
    envelope = {
        "id": f"evt_{uuid.uuid4().hex}",
        "event": event,
        "company": str(company_id),
        "at": timezone.localtime().isoformat(timespec="seconds"),
        "data": _json_safe(data),
    }
    for ep in endpoints:
        deliver_webhook.delay(str(ep.id), envelope)


def emit_event(company_id, event: str, data: dict) -> None:
    if not company_id or event not in EVENTS:
        return

    # ТЗ Н-31: каждое событие попадает в ленту изменений (одна вставка после коммита)
    try:
        from apps.main.change_feed import record_from_emit
        record_from_emit(company_id, event, data)
    except Exception:
        logger.exception("change feed emit failed: %s", event)

    def _send():
        try:
            _dispatch(company_id, event, data)
        except Exception:
            logger.exception("webhook emit failed: %s", event)

        try:
            from apps.main.telegram_bot.services.events_handler import handle_internal_event
            handle_internal_event(company_id, event, data)
        except Exception:
            logger.exception("telegram bot handle_internal_event failed: %s", event)

    try:
        transaction.on_commit(_send)
    except Exception:
        logger.exception("webhook on_commit failed: %s", event)
