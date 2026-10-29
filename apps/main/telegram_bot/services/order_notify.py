"""ТЗ Б-22: уведомление о новом заказе витрины в Telegram (серверный бот), кнопки «Принять»/«Отменить»."""
import html
import logging
import re

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from apps.main.telegram_bot.models import TelegramBotAudit, TelegramBotSettings, TelegramOrderNotice
from apps.main.telegram_bot.services import telegram_api

logger = logging.getLogger("telegram_bot.orders")

# повторы после ошибки отправки: через 1, 5, 30 минут, затем «не доставлено»
RETRY_DELAYS = (60, 300, 1800)
DELIVERY_RU = {"pickup": "Самовывоз", "delivery": "Доставка"}


def _fmt_qty(q):
    q = q.normalize() if hasattr(q, "normalize") else q
    return f"{q:f}"


def build_order_text(order, bot_settings) -> str:
    e = html.escape
    local = timezone.localtime(order.created_at).strftime("%d.%m %H:%M")
    rows = [f"🛒 <b>Новый заказ №{order.number}</b> · {local}"]
    if order.customer_name:
        rows.append(f"👤 {e(order.customer_name)}")
    if bot_settings.orders_show_phone and order.customer_phone:
        rows.append(f"📞 {e(order.customer_phone)}")
    rows.append(f"📍 {DELIVERY_RU.get(order.delivery_type, order.delivery_type)}"
                + (f": {e(order.delivery_address)}" if order.delivery_type == "delivery" and order.delivery_address else ""))
    rows.append("📦 Состав:")
    for it in order.items.all():
        rows.append(f"  • {e(it.product_name)} × {_fmt_qty(it.qty)}")
    if bot_settings.orders_show_amount:
        rows.append(f"💰 Сумма: <b>{order.total} сом</b>")
    if order.comment and order.source not in ("telegram",):
        rows.append(f"💬 {e(order.comment[:300])}")
    return "\n".join(rows)


def build_order_buttons(order):
    site = getattr(django_settings, "SITE_PUBLIC_URL", "https://app.nurcrm.kg").rstrip("/")
    return {"inline_keyboard": [
        [
            {"text": "✅ Принять", "callback_data": f"ordn:{order.id}:a"},
            {"text": "❌ Отменить", "callback_data": f"ordn:{order.id}:c"},
        ],
        [{"text": "🔗 Открыть на сайте", "url": f"{site}/"}],
    ]}


def recipients(bot_settings):
    chats = []
    mode = bot_settings.orders_recipients
    if mode in ("owner", "both") or not bot_settings.orders_chat_id:
        if bot_settings.owner_chat_id:
            chats.append(str(bot_settings.owner_chat_id))
    if mode in ("group", "both") and bot_settings.orders_chat_id:
        chats.append(str(bot_settings.orders_chat_id))
    return list(dict.fromkeys(chats))


def _audit(company, order, chat_id, ok, error):
    TelegramBotAudit.objects.create(
        company=company, action=TelegramBotAudit.Action.ORDER_NOTIFIED,
        object_title=f"Заказ №{order.number}", source="system",
        changes={"order_id": str(order.id), "chat_id": chat_id, "ok": ok, "error": error},
    )


def notify_new_order(order_id, attempt=0):
    """
    Шлёт уведомление получателям. Не больше одного успешного на (заказ, чат).
    Возвращает число успешно отправленных. Ошибки не бросает.
    """
    from apps.main.models import ShowcaseOrder

    try:
        order = ShowcaseOrder.objects.select_related("company").prefetch_related("items").filter(id=order_id).first()
        if order is None or order.status != ShowcaseOrder.Status.NEW:
            return 0
        bs = TelegramBotSettings.objects.filter(company=order.company).first()
        if bs is None or bs.mode != TelegramBotSettings.Mode.SERVER or not bs.token:
            return 0
        chats = recipients(bs)
        if not chats:
            return 0
        text = build_order_text(order, bs)
        markup = build_order_buttons(order)
        sent, failed = 0, False
        for chat in chats:
            notice, _ = TelegramOrderNotice.objects.get_or_create(order=order, chat_id=chat, defaults={"company": order.company})
            if notice.ok or notice.gave_up:
                continue
            res = telegram_api.send_message(bs.token, chat, text, parse_mode="HTML", reply_markup=markup)
            ok = bool(res.get("ok"))
            err = "" if ok else str(res.get("description") or "Не удалось отправить")[:255]
            notice.ok, notice.error, notice.attempts = ok, err, notice.attempts + 1
            notice.gave_up = (not ok) and attempt >= len(RETRY_DELAYS)
            notice.save()
            _audit(order.company, order, chat, ok, err if ok or not notice.gave_up else f"не доставлено: {err}")
            if ok:
                sent += 1
            else:
                failed = True
                TelegramBotSettings.objects.filter(id=bs.id).update(orders_last_error=err)
        if failed and attempt < len(RETRY_DELAYS):
            _schedule_retry(order.id, attempt + 1)
        elif sent and not failed:
            TelegramBotSettings.objects.filter(id=bs.id).exclude(orders_last_error="").update(orders_last_error="")
        return sent
    except Exception:
        logger.exception("notify_new_order failed for %s", order_id)
        return 0


def _schedule_retry(order_id, attempt):
    try:
        from apps.main.telegram_bot.tasks import retry_order_notification

        retry_order_notification.apply_async(args=[str(order_id), attempt], countdown=RETRY_DELAYS[attempt - 1])
    except Exception:
        logger.warning("order notification retry not scheduled", exc_info=True)


def notify_after_commit(order_id):
    """Вызывать из запроса: отправка после коммита транзакции, в фоне, а без очереди — сразу."""
    def _go():
        try:
            from apps.main.telegram_bot.tasks import send_order_notification

            send_order_notification.delay(str(order_id))
        except Exception:
            notify_new_order(order_id)

    transaction.on_commit(_go)


def apply_order_action(bs, order, action, from_id, chat_id):
    """
    Кнопки «Принять»/«Отменить». Только владелец (по from.id или личному чату владельца).
    Возвращает (ok, сообщение).
    """
    from apps.main.models import ShowcaseOrder
    from apps.main.variant_utils import order_stock_lines, release_stock

    is_owner = bool(from_id) and (
        str(from_id) == str(getattr(bs, "owner_user_id", "") or "") or str(from_id) == str(bs.owner_chat_id or "")
    )
    if not is_owner:
        return False, "Доступно только владельцу."
    with transaction.atomic():
        o = ShowcaseOrder.objects.select_for_update().get(pk=order.pk)
        if o.status in (ShowcaseOrder.Status.DONE, ShowcaseOrder.Status.CANCELED):
            return False, f"Заказ №{o.number} уже закрыт."
        if action == "a":
            o.status = ShowcaseOrder.Status.ACCEPTED
            fields = ["status", "updated_at"]
            msg = f"✅ Заказ №{o.number} принят."
        else:
            o.status = ShowcaseOrder.Status.CANCELED
            fields = ["status", "updated_at"]
            if o.stock_reserved:
                release_stock(order_stock_lines(o))
                o.stock_reserved = False
                fields.append("stock_reserved")
            msg = f"❌ Заказ №{o.number} отменён."
        o.save(update_fields=fields)
    m = re.search(r"chat_id=(\d+)", o.comment or "")
    cust = m.group(1) if m else (o.telegram_chat_id or "")
    if cust and bs.token:
        text = (f"✅ Ваш заказ №{o.number} принят магазином." if action == "a"
                else f"К сожалению, ваш заказ №{o.number} отменён магазином.")
        telegram_api.send_message(bs.token, cust, text, parse_mode=None)
    return True, msg
