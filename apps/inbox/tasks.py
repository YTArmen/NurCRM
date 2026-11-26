"""Celery-задачи ящика: исходящая очередь, обработка вебхуков, уведомления, чистка.

Расписание (CELERY_BEAT_SCHEDULE в core/settings.py, править его нельзя — см. отчёт):
  apps.inbox.tasks.sweep_webhook_events  — каждую минуту
  apps.inbox.tasks.sweep_outbound        — каждую минуту
  apps.inbox.tasks.wake_snoozed          — каждую минуту
  apps.inbox.tasks.alert_unanswered      — каждые 15 минут (внутри «не чаще раза в час»)
  apps.inbox.tasks.cleanup_old_data      — раз в сутки
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from . import crypto, inbound, services, telegram
from .channels import touch_health
from .models import Channel, Conversation, Message, OwnerAlert, WebhookEvent

logger = logging.getLogger("inbox")


def _chat_rate_wait(token, chat_id) -> float:
    try:
        from apps.main.telegram_bot.services import telegram_api
        return float(telegram_api.check_rate_limit(token, chat_id) or 0)
    except Exception:
        return 0.0


@shared_task(name="apps.inbox.tasks.send_outbound")
def send_outbound(message_id):
    """Отправка исходящего с повторами 1 с, 5 с, 30 с, 2 мин → failed (Б-76 п. 6)."""
    msg = Message.objects.select_related("conversation", "channel", "channel__bot_settings", "conversation__contact").filter(pk=message_id).first()
    if msg is None or msg.status != "queued" or msg.direction != "out" or msg.kind == "note":
        return "skipped"
    channel, contact, conv = msg.channel, msg.conversation.contact, msg.conversation
    if channel.kind == "web_widget":  # посетитель сам забирает сообщения (long-poll): отправлять некуда
        msg.sent_at = timezone.now()
        msg.save(update_fields=["sent_at"])
        services._set_status(msg, "sent")
        return "sent"
    if channel.kind in ("whatsapp_cloud", "whatsapp_bsp", "instagram"):
        return _send_meta(msg, channel, contact, conv)
    if not channel.kind.startswith("telegram"):
        return _fail(msg, "channel_error", "Отправка в этот вид канала пока не поддержана.")
    token = crypto.channel_token(channel)
    if not token:
        return _fail(msg, "channel_error", "У канала нет токена.")
    wait = _chat_rate_wait(token, contact.external_id)  # Telegram: 1 сообщение в секунду на чат
    if wait:
        services.enqueue_send(msg.id, countdown=max(1, int(wait + 0.99)))
        return "rate_limited"
    reply_id = None
    if msg.reply_to_id and msg.reply_to and ":" in (msg.reply_to.external_message_id or ""):
        try:
            reply_id = int(msg.reply_to.external_message_id.split(":", 1)[1])
        except ValueError:
            reply_id = None
    try:
        if msg.kind == "file":
            res = _send_telegram_file(token, contact, msg)
        else:
            res = telegram.send_message(token, contact.external_id, msg.text, reply_to_message_id=reply_id)
    except telegram.TelegramError as exc:
        return _on_error(msg, contact, exc)
    now = timezone.now()
    ext = f"{contact.external_id}:{res.get('message_id')}" if res and res.get("message_id") else ""
    msg.external_message_id, msg.sent_at = ext, now
    msg.save(update_fields=["external_message_id", "sent_at"])
    services._set_status(msg, "sent")
    services._apply_pending(channel, msg)
    touch_health(channel.id, outbound=True)
    services.emit(conv, "inbox_message_status", message=msg.id, message_status="sent")
    return "sent"


def _send_meta(msg, channel, contact, conv):
    """WhatsApp (Cloud API / 360dialog) и Instagram: повторы как у Telegram; отозванный токен → канал в error."""
    from . import meta_api, senders
    try:
        if channel.kind == "instagram":
            ext = senders.send_instagram(msg, contact)
        else:
            ext = senders.send_whatsapp(msg, contact)
    except meta_api.MetaError as exc:
        if exc.code == 190 or exc.status == 401:  # токен отозван/истёк
            from .models import Channel as Ch
            Ch.objects.filter(pk=channel.pk).update(status="error")
            from . import ai
            ai.notify_owner(channel.company_id, "⚠️ Доступ канала в «Чатах» отозван или истёк. Подключите канал заново.")
            return _fail(msg, "channel_error", str(exc))
        if exc.retry_after:
            services.enqueue_send(msg.id, countdown=int(exc.retry_after))
            return "retry_after"
        if exc.permanent:
            return _fail(msg, "channel_error", str(exc))
        msg.attempts += 1
        msg.save(update_fields=["attempts"])
        if msg.attempts > len(services.RETRY_DELAYS):
            return _fail(msg, "channel_error", str(exc))
        services.enqueue_send(msg.id, countdown=services.RETRY_DELAYS[msg.attempts - 1])
        return "retry"
    msg.external_message_id, msg.sent_at = ext or "", timezone.now()
    msg.save(update_fields=["external_message_id", "sent_at"])
    services._set_status(msg, "sent")
    services._apply_pending(channel, msg)
    touch_health(channel.id, outbound=True)
    services.emit(conv, "inbox_message_status", message=msg.id, message_status="sent")
    return "sent"


def _send_telegram_file(token, contact, msg):
    from apps.main.models import MainMedia
    att = (msg.attachments or [{}])[0]
    media = MainMedia.objects.filter(pk=att.get("media_id"), company_id=msg.company_id).first()
    if media is None:
        raise telegram.TelegramError("Файл не найден", code=404, permanent=True)
    with media.file.open("rb") as fh:
        content = fh.read()
    return telegram.send_document(token, contact.external_id, att.get("name") or "file", content, msg.text)


def _on_error(msg, contact, exc):
    code = exc.code
    if exc.retry_after:  # 429: ждём сколько просит Telegram, попытку не тратим
        services.enqueue_send(msg.id, countdown=int(exc.retry_after) + 1)
        return "retry_after"
    if code == 403:  # бота заблокировали
        contact.blocked = True
        contact.save(update_fields=["blocked"])
        return _fail(msg, "contact_blocked", str(exc))
    if exc.permanent:
        return _fail(msg, "channel_error", str(exc))
    msg.attempts += 1
    msg.save(update_fields=["attempts"])
    if msg.attempts > len(services.RETRY_DELAYS):
        return _fail(msg, "channel_error", str(exc))
    services.enqueue_send(msg.id, countdown=services.RETRY_DELAYS[msg.attempts - 1])
    return "retry"


def _fail(msg, code, detail):
    msg.error = {"code": code, "detail": str(detail)[:300]}
    msg.status = "failed"
    msg.save(update_fields=["error", "status"])
    touch_health(msg.channel_id, error=detail)
    services.emit(msg.conversation, "inbox_message_status", message=msg.id, message_status="failed")
    return "failed"


@shared_task(name="apps.inbox.tasks.process_webhook_event")
def process_webhook_event(event_id):
    ev = WebhookEvent.objects.select_related("channel", "channel__bot_settings").filter(pk=event_id).first()
    if ev is None or ev.processed_at or not ev.signature_ok:
        return "skipped"
    try:
        if ev.channel.bot_settings_id:
            # серверный бот: полная обработка (сценарии, ИИ, владелец) в его конвейере, а он пишет в ящик
            import time

            from apps.main.telegram_bot.tasks import process_telegram_update
            process_telegram_update.delay(str(ev.channel.bot_settings_id), ev.payload or {}, time.time())
            result = "delegated_to_bot"
        else:
            result = inbound.process_event(ev)
        ev.error = ""
    except Exception as exc:
        logger.exception("webhook event %s failed", event_id)
        ev.error = f"{type(exc).__name__}: {exc}"[:500]
        touch_health(ev.channel_id, error=ev.error)
        result = "error"
    ev.processed_at = timezone.now()
    ev.save(update_fields=["error", "processed_at"])
    return result


@shared_task(name="apps.inbox.tasks.sweep_webhook_events")
def sweep_webhook_events():
    """Подбирает необработанные события (брокер был недоступен)."""
    old = timezone.now() - timedelta(minutes=1)
    ids = list(WebhookEvent.objects.filter(processed_at__isnull=True, signature_ok=True, received_at__lt=old)
               .values_list("id", flat=True)[:200])
    for i in ids:
        process_webhook_event.delay(i)
    return len(ids)


@shared_task(name="apps.inbox.tasks.sweep_outbound")
def sweep_outbound():
    """Подбирает «застрявшие» в очереди исходящие."""
    old = timezone.now() - timedelta(minutes=2)
    ids = list(Message.objects.filter(status="queued", direction="out", created_at__lt=old).exclude(kind="note")
               .values_list("id", flat=True)[:200])
    for i in ids:
        send_outbound.delay(i)
    return len(ids)


@shared_task(name="apps.inbox.tasks.wake_snoozed")
def wake_snoozed():
    now = timezone.now()
    n = 0
    for conv in Conversation.objects.filter(status="snoozed", snoozed_until__lte=now):
        conv.status, conv.snoozed_until = "open", None
        conv.save(update_fields=["status", "snoozed_until", "updated_at"])
        services.emit(conv, "inbox_conversation_updated", change="status")
        n += 1
    return n


@shared_task(name="apps.inbox.tasks.alert_unanswered")
def alert_unanswered():
    """Б-77 п. 4: владельцу в Telegram (серверный бот): «N чатов без ответа больше 15 минут». Не чаще раза в час."""
    from apps.main.telegram_bot.models import TelegramBotSettings

    sent = 0
    now = timezone.now()
    company_ids = (Conversation.objects.filter(status="open", waiting_since__isnull=False)
                   .values_list("company_id", flat=True).distinct())
    for cid in list(company_ids):
        chans = list(Channel.objects.filter(company_id=cid))
        if any((c.settings or {}).get("notify_owner_unanswered") is False for c in chans):
            continue
        minutes = min([int((c.settings or {}).get("unanswered_alert_min") or 15) for c in chans] or [15])
        count = Conversation.objects.filter(company_id=cid, status="open", waiting_since__lte=now - timedelta(minutes=minutes)).count()
        if not count:
            continue
        if OwnerAlert.objects.filter(company_id=cid, created_at__gte=now - timedelta(hours=1)).exists():
            continue
        bot = TelegramBotSettings.objects.filter(company_id=cid).first()
        if not bot or not bot.owner_chat_id or not bot.token:
            continue
        from apps.main.telegram_bot.services import telegram_api
        text = f"💬 {count} {_chats_word(count)} без ответа больше {minutes} минут. Откройте «Чаты»."
        try:
            res = telegram_api.send_message(bot.token, bot.owner_chat_id, text, parse_mode=None)
        except Exception:
            logger.exception("alert_unanswered: send failed")
            continue
        if isinstance(res, dict) and res.get("ok"):
            OwnerAlert.objects.create(company_id=cid, count=count)
            sent += 1
    return sent


def _chats_word(n):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "чат"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return "чата"
    return "чатов"


@shared_task(name="apps.inbox.tasks.cleanup_old_data")
def cleanup_old_data():
    """Хранение (Б-75 п. 8): сообщения 12 мес., вложения 90 дней, тела вебхуков 30 дней."""
    now = timezone.now()
    days = int(getattr(settings, "INBOX_MESSAGE_RETENTION_DAYS", 365))
    deleted, _ = Message.objects.filter(created_at__lt=now - timedelta(days=days)).delete()
    att = Message.objects.filter(created_at__lt=now - timedelta(days=90)).exclude(attachments=[]).update(attachments=[])
    ev = WebhookEvent.objects.filter(received_at__lt=now - timedelta(days=30)).exclude(payload={}).update(payload={})
    return {"messages": deleted, "attachments": att, "webhook_payloads": ev}


# задачи этапа 2 (Б-78…Б-86) — в tasks_ext.py; импорт нужен, чтобы воркер их зарегистрировал
from . import tasks_ext  # noqa: E402,F401
