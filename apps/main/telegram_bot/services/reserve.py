"""bot2711b: бот-продавец Ф3 — бронь заказа из Telegram на N часов без предоплаты (решение владельца: 24 ч)
и еженедельная сводка бота-продавца владельцу.

Остаток уже резервируется при создании заказа (stock_reserved). Здесь — срок брони в ShowcaseOrder.hold_until:
за 2 часа до конца — напоминание покупателю и владельцу (кнопка «Продлить на 24 ч»), по истечении —
автоотмена и возврат остатка (если reserve_auto_cancel), уведомления обеим сторонам.
"""
import html
import logging
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.main.telegram_bot.services import telegram_api

logger = logging.getLogger("telegram_bot.reserve")

BOT_SOURCES = ("telegram", "telegram_bot")
REMIND_BEFORE = timedelta(hours=2)
LIVE = ("new", "accepted")


def _cfg(bs):
    from apps.main.telegram_bot.services import seller
    return seller.get_config(bs)


def hold_text(order, lang="ru"):
    if not order.hold_until:
        return ""
    t = timezone.localtime(order.hold_until).strftime("%d.%m %H:%M")
    if lang == "ky":
        return f"🕒 Товар {t} чейин сизге сакталат."
    return f"🕒 Товар забронирован за вами до {t}."


def set_hold(bs, order):
    """После создания заказа ботом: срок брони по настройке. Возвращает текст для покупателя или ''."""
    try:
        cfg = _cfg(bs)
        hours = int(cfg.get("reserve_hours") or 0)
        if not cfg.get("seller_enabled") or hours <= 0 or order.hold_until:
            return ""
        order.hold_until = timezone.now() + timedelta(hours=hours)
        order.save(update_fields=["hold_until", "updated_at"])
        return hold_text(order)
    except Exception:
        logger.exception("set_hold failed for order %s", getattr(order, "pk", None))
        return ""


def _bs_for(company_id):
    from apps.main.telegram_bot.models import TelegramBotSettings
    return TelegramBotSettings.objects.select_related("company").filter(company_id=company_id).first()


def _owner_send(bs, text, markup=None):
    if bs and bs.token and bs.owner_chat_id:
        telegram_api.send_message(bs.token, bs.owner_chat_id, text, parse_mode="HTML", reply_markup=markup)


def remind_and_expire(now=None) -> dict:
    from apps.main.models import ShowcaseOrder
    from apps.main.variant_utils import order_stock_lines, release_stock
    now = now or timezone.now()
    base = ShowcaseOrder.objects.filter(source__in=BOT_SOURCES, status__in=LIVE, hold_until__isnull=False) \
        .exclude(delivery_type="try_on")
    out = {"reminded": 0, "canceled": 0, "expired_kept": 0}
    bs_cache = {}

    def bs_of(cid):
        if cid not in bs_cache:
            bs_cache[cid] = _bs_for(cid)
        return bs_cache[cid]

    # 1. напоминание за 2 часа
    for o in base.filter(hold_until__gt=now, hold_until__lte=now + REMIND_BEFORE)[:300]:
        key = f"tgrs:rem:{o.pk}:{int(o.hold_until.timestamp())}"
        if not cache.add(key, 1, 3 * 24 * 3600):
            continue
        bs = bs_of(o.company_id)
        if not bs or not bs.token:
            continue
        t = timezone.localtime(o.hold_until).strftime("%H:%M")
        if o.telegram_chat_id:
            telegram_api.send_message(bs.token, o.telegram_chat_id,
                                      f"Напоминаем: заказ №{o.number} ждёт вас до {t}. Успеете забрать? "
                                      f"Если нужно больше времени — напишите здесь.", parse_mode=None)
        _owner_send(bs, f"⏳ Бронь заказа №{o.number} ({html.escape(o.customer_name or '')}) заканчивается в {t}.",
                    {"inline_keyboard": [[{"text": "➕ Продлить на 24 ч", "callback_data": f"rs:x:{o.pk}"}]]})
        out["reminded"] += 1

    # 2. истекли
    for pk, cid in list(base.filter(hold_until__lte=now).values_list("pk", "company_id")[:300]):
        bs = bs_of(cid)
        auto = bool(_cfg(bs).get("reserve_auto_cancel", True)) if bs else True
        try:
            with transaction.atomic():
                o = ShowcaseOrder.objects.select_for_update().filter(pk=pk).first()
                if o is None or o.status not in LIVE or not o.hold_until or o.hold_until > now:
                    continue
                if not auto:
                    key = f"tgrs:exp:{o.pk}:{int(o.hold_until.timestamp())}"
                    if cache.add(key, 1, 7 * 24 * 3600):
                        transaction.on_commit(lambda o=o, bs=bs: _owner_send(
                            bs, f"⌛ Срок брони заказа №{o.number} истёк. Автоотмена выключена — решите вручную."))
                        out["expired_kept"] += 1
                    continue
                if o.stock_reserved:
                    release_stock(order_stock_lines(o))
                    o.stock_reserved = False
                o.status = ShowcaseOrder.Status.CANCELED
                o.comment = ((o.comment or "") + "\nБронь истекла — заказ отменён автоматически.").strip()
                o.save(update_fields=["status", "stock_reserved", "comment", "updated_at"])
                out["canceled"] += 1

                def notify(o=o, bs=bs):
                    if not bs or not bs.token:
                        return
                    if o.telegram_chat_id:
                        telegram_api.send_message(bs.token, o.telegram_chat_id,
                                                  f"Срок брони заказа №{o.number} истёк, заказ отменён. "
                                                  f"Если товар ещё нужен — напишите, оформим заново.", parse_mode=None)
                    _owner_send(bs, f"❌ Бронь заказа №{o.number} истекла — заказ отменён, товар вернулся в остаток.")
                transaction.on_commit(notify)
        except Exception:
            logger.exception("reserve expire failed for order %s", pk)
    return out


def extend_click(bs, cq) -> bool:
    """Кнопка владельца rs:x:<order_id> — продлить бронь на 24 ч."""
    from apps.main.models import ShowcaseOrder
    from apps.main.telegram_bot.services import seller
    token = bs.token
    parts = (cq.get("data") or "").split(":")
    msg = cq.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    if not seller._is_owner(bs, (cq.get("from") or {}).get("id"), chat_id):
        telegram_api.answer_callback_query(token, cq.get("id"), text="Доступно только владельцу.", show_alert=True)
        return True
    try:
        o = ShowcaseOrder.objects.filter(company=bs.company, pk=parts[2]).first() if len(parts) == 3 else None
    except Exception:
        o = None
    if o is None or o.status not in LIVE:
        telegram_api.answer_callback_query(token, cq.get("id"), text="Заказ уже закрыт.", show_alert=True)
        return True
    now = timezone.now()
    o.hold_until = max(o.hold_until or now, now) + timedelta(hours=24)
    o.save(update_fields=["hold_until", "updated_at"])
    t = timezone.localtime(o.hold_until).strftime("%d.%m %H:%M")
    telegram_api.edit_message_reply_markup(token, chat_id, msg.get("message_id"), reply_markup={"inline_keyboard": []})
    telegram_api.answer_callback_query(token, cq.get("id"), text=f"Бронь продлена до {t}.")
    if o.telegram_chat_id:
        telegram_api.send_message(token, o.telegram_chat_id, f"Магазин продлил бронь заказа №{o.number} до {t}.", parse_mode=None)
    return True


# ---------------------------------------------------------------- еженедельная сводка

def _m(x):
    from apps.main.telegram_bot.services.photo_service import format_price_display
    return format_price_display(x)


def weekly_text(company, df, dt) -> str:
    from apps.main.telegram_bot.views import sales_stats_data
    d = sales_stats_data(company, df, dt)
    o, h, b = d["orders"], d["handoffs"], d.get("bargain") or {}
    rows = [f"📊 <b>Бот-продавец за неделю {df.strftime('%d.%m')}–{dt.strftime('%d.%m')}</b>",
            f"💬 Диалогов: {d['dialogs']}, сообщений: {d['messages']}",
            f"🛒 Заказов: {o['count']} на {_m(o['total'])} (выдано {o['done']} на {_m(o['done_total'])}, отменено {o['canceled']})",
            f"📈 Конверсия в заказ: {d['conversion_percent']} %"]
    if o["count"]:
        rows.append(f"🧾 Средний чек: {_m(o['avg_check'])}")
    if h["total"]:
        line = f"🙋 Звали продавца: {h['total']} (ответили {h['answered']}, без ответа {h['unanswered']}, перезвонить {h['callbacks']})"
        if h.get("avg_answer_seconds") is not None:
            line += f", среднее время ответа {max(1, round(h['avg_answer_seconds'] / 60))} мин"
        rows.append(line)
    if b.get("proposals"):
        rows.append(f"🤝 Торг: {b['dialogs']} диалогов, {b['orders']} заказов по торгу, уступили {_m(b['concessions_total'])} "
                    f"(в среднем {b['avg_discount_percent']} %), «лучшая цена» — {b['decline_share_percent']} %")
    unk = d.get("unknown_questions") or []
    if unk:
        rows.append("\n❓ Бот не знал ответа (добавьте в «Правила магазина»):")
        for q in unk[:5]:
            rows.append(f"  • {html.escape((q.get('text') or '')[:120])}")
    return "\n".join(rows)


def send_weekly(now=None) -> int:
    from apps.main.telegram_bot.models import TelegramBotSettings
    now = timezone.localtime(now or timezone.now())
    dt = (now - timedelta(days=1)).date()
    df = dt - timedelta(days=6)
    sent = 0
    for bs in TelegramBotSettings.objects.select_related("company").filter(mode="server").exclude(owner_chat_id__isnull=True).exclude(owner_chat_id="").iterator():
        cfg = _cfg(bs)
        if not cfg.get("seller_enabled") or not cfg.get("weekly_summary", True) or not bs.token:
            continue
        if not cache.add(f"tgrs:week:{bs.company_id}:{dt.isoformat()}", 1, 8 * 24 * 3600):
            continue
        try:
            res = telegram_api.send_message(bs.token, bs.owner_chat_id, weekly_text(bs.company, df, dt), parse_mode="HTML")
            sent += 1 if res.get("ok") else 0
        except Exception:
            logger.exception("weekly summary failed for company %s", bs.company_id)
    return sent

