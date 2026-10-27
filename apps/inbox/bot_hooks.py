"""Крючки серверного Telegram-бота (apps/main/telegram_bot) в ящик (Б-76 п. 7).

- on_customer_message: входящее покупателя → в диалог; возвращает True, если бот/ИИ должны молчать
  (в диалоге недавно ответил человек, ai_pause_minutes);
- on_bot_send: каждый ответ бота покупателю → в диалог (sender=ai).
Чат владельца и группа заказов в ящик не попадают (крючок зовётся только для покупателей, личные чаты).
Любая ошибка здесь проглатывается: бот не должен ломаться из-за ящика.
"""
import contextvars
import html
import logging
import re
import time

from . import channels as ch_svc
from . import inbound, services

logger = logging.getLogger("inbox.bot")

CTX_TTL = 180  # секунд: контекст «идёт обработка сообщения покупателя этого чата»
_ctx = contextvars.ContextVar("inbox_bot_ctx", default=None)
_TAG = re.compile(r"<[^>]+>")


def on_customer_message(settings, message: dict) -> bool:
    try:
        chat = message.get("chat") or {}
        if chat.get("type", "private") != "private":
            return False  # группа заказов и прочие группы
        channel = ch_svc.ensure_server_bot_channel(settings.company)
        if channel is None:
            return False
        parsed = inbound.parse_telegram_message(message)
        parsed.pop("date", None)
        services.ingest_inbound(channel, **parsed)
        _ctx.set((channel.id, parsed["external_id"], time.time() + CTX_TTL))
        return services.ai_paused(channel, parsed["external_id"])
    except Exception:
        logger.exception("inbox: не удалось записать входящее бота")
        return False


def on_bot_send(chat_id, text, api_result=None) -> None:
    try:
        ctx = _ctx.get()
        if not ctx or str(chat_id) != ctx[1] or time.time() > ctx[2]:
            return
        from .models import Channel
        channel = Channel.objects.filter(pk=ctx[0]).first()
        if channel is None:
            return
        clean = html.unescape(_TAG.sub("", text or "")).strip()
        if not clean:
            return
        mid = ((api_result or {}).get("result") or {}).get("message_id")
        services.record_bot_message(channel, chat_id, clean, sender="ai",
                                    message_external_id=f"{chat_id}:{mid}" if mid else "")
    except Exception:
        logger.exception("inbox: не удалось записать ответ бота")
