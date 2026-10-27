"""Разбор входящих обновлений провайдеров (после проверки подписи). Этап 1: Telegram."""
import logging

from django.utils import timezone

from . import services

logger = logging.getLogger("inbox")


def telegram_event_id(payload) -> str:
    uid = payload.get("update_id") if isinstance(payload, dict) else None
    return str(uid) if uid is not None else ""


def _media(message):
    """(kind, attachments) по сообщению Telegram; сами файлы пока не скачиваем — храним file_id."""
    if message.get("photo"):
        best = sorted(message["photo"], key=lambda p: p.get("file_size") or 0)[-1]
        return "image", [{"media_id": None, "file_id": best.get("file_id"), "mime": "image/jpeg", "name": ""}]
    for key, kind in (("voice", "audio"), ("audio", "audio"), ("video", "video"), ("video_note", "video"),
                      ("document", "file"), ("sticker", "sticker")):
        if message.get(key):
            part = message[key]
            return kind, [{"media_id": None, "file_id": part.get("file_id"), "mime": part.get("mime_type") or "",
                           "size": part.get("file_size"), "name": part.get("file_name") or ""}]
    if message.get("location"):
        loc = message["location"]
        return "location", [{"lat": loc.get("latitude"), "lon": loc.get("longitude")}]
    return "text", []


def parse_telegram_message(message: dict) -> dict:
    """Нормализованное входящее из message Telegram (общий код для вебхука канала и крючка бота)."""
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    text = message.get("text") or message.get("caption") or ""
    kind, attachments = _media(message)
    name = f"{(sender.get('first_name') or '').strip()} {(sender.get('last_name') or '').strip()}".strip()
    phone = (message.get("contact") or {}).get("phone_number") or ""
    start_payload = ""
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        start_payload = parts[1].strip() if len(parts) > 1 else ""
    ts = message.get("date")
    return {
        "external_id": str(chat.get("id")),
        "message_external_id": f"{chat.get('id')}:{message.get('message_id')}" if message.get("message_id") else "",
        "text": text, "kind": kind, "attachments": attachments,
        "display_name": name or chat.get("title") or sender.get("username") or "",
        "username": sender.get("username") or "", "phone": phone,
        "language": (sender.get("language_code") or "ru")[:2], "start_payload": start_payload, "date": ts,
    }


def process_telegram(channel, payload) -> str:
    """Обновление Telegram для обычного (не серверного) канала telegram_bot."""
    message = payload.get("message")
    if not message:
        return "ignored"
    if (message.get("chat") or {}).get("type", "private") != "private":
        return "ignored_group"  # группы и чат владельца в ящик не попадают
    parsed = parse_telegram_message(message)
    parsed.pop("date", None)
    _msg, created = services.ingest_inbound(channel, **parsed)
    return "created" if created else "duplicate"


def process_event(event) -> str:
    """Обработка WebhookEvent воркером."""
    channel = event.channel
    if channel.kind.startswith("telegram"):
        return process_telegram(channel, event.payload or {})
    if channel.kind in ("whatsapp_cloud", "whatsapp_bsp") and channel.provider == "meta":
        from . import meta_channels
        return meta_channels.process_whatsapp(channel, event.payload or {})
    if channel.kind == "instagram":
        from . import meta_channels
        return meta_channels.process_instagram(channel, event.payload or {})
    return "provider_not_supported"
