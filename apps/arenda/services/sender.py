"""Единая отправка SMS и WhatsApp через настраиваемый HTTP-шлюз.

Настройки (env -> django settings): ARENDA_SMS_URL, ARENDA_SMS_TOKEN, ARENDA_WHATSAPP_URL, ARENDA_WHATSAPP_TOKEN.
Запрос: POST JSON {"phone", "text", "id"} с заголовком Authorization: Bearer <token>, таймаут 10 с.
Шлюз потом может сообщить о доставке: POST reminders/delivery/ (см. views_notify.DeliveryReportView)."""
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings as dj_settings

logger = logging.getLogger(__name__)

TIMEOUT = 10
NOT_CONFIGURED = {"sms": "Сервис SMS не подключён", "whatsapp": "Сервис WhatsApp не подключён"}
FAILED = {"sms": "Сервис SMS не принял сообщение", "whatsapp": "Сервис WhatsApp не принял сообщение"}


def _conf(channel):
    p = "ARENDA_SMS" if channel == "sms" else "ARENDA_WHATSAPP"
    return getattr(dj_settings, p + "_URL", "") or "", getattr(dj_settings, p + "_TOKEN", "") or ""


def is_configured(channel) -> bool:
    return bool(_conf(channel)[0])


def _post(url, token, payload):
    """Возвращает HTTP-код ответа. Вынесено отдельно, чтобы подменять в тестах."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310
        return r.status


def send_message(channel, phone, text, ref_id="") -> tuple:
    """-> (status, error). status: "sent" | "not_sent" (шлюз не настроен / нет номера) | "failed" (шлюз не ответил 2xx)."""
    if channel not in ("sms", "whatsapp"):
        return "not_sent", "Неизвестный канал"
    url, token = _conf(channel)
    if not url:
        return "not_sent", NOT_CONFIGURED[channel]
    if not phone:
        return "not_sent", "Не указан телефон"
    try:
        code = _post(url, token, {"phone": phone, "text": text, "id": str(ref_id)})
    except urllib.error.HTTPError as exc:
        logger.warning("arenda %s gateway http %s", channel, exc.code)
        return "failed", FAILED[channel]
    except Exception as exc:  # сеть, таймаут
        logger.warning("arenda %s gateway failed: %s", channel, type(exc).__name__)
        return "failed", FAILED[channel]
    if 200 <= int(code) < 300:
        return "sent", ""
    return "failed", FAILED[channel]
