import logging
from decimal import Decimal
from django.core.cache import cache

from apps.main.telegram_bot.services import telegram_api
from apps.main.telegram_bot.services.photo_service import format_amount, format_qty

logger = logging.getLogger("telegram_bot.events")


def _fmt_money(val) -> str:
    # ТЗ ч.9, 1.1: без лишних нулей — 660 066, 45,50
    return format_amount(val)


def send_shift_closed_notification(company_id, shift_id) -> bool:
    """Формирует и отправляет владельцу сводку закрытия смены."""
    from apps.main.telegram_bot.models import TelegramBotSettings
    from apps.construction.models import CashShift

    settings = TelegramBotSettings.objects.filter(company_id=company_id).first()
    if not settings or not settings.token or not settings.owner_chat_id or not settings.shift_summary_enabled:
        return False

    shift = (
        CashShift.objects.filter(company_id=company_id, id=shift_id)
        .select_related("cashbox", "cashier", "branch")
        .first()
    )
    if not shift:
        logger.warning("Shift %s not found for shift.closed notification", shift_id)
        return False

    cashbox_name = getattr(shift.cashbox, "name", "Основная касса")
    cashier_name = ""
    if shift.cashier:
        cashier_name = f"{(shift.cashier.first_name or '').strip()} {(shift.cashier.last_name or '').strip()}".strip() or shift.cashier.email

    opened_at_str = shift.opened_at.strftime("%H:%M %d.%m") if shift.opened_at else "-"
    closed_at_str = shift.closed_at.strftime("%H:%M %d.%m") if shift.closed_at else "-"

    sales_total = shift.sales_total or Decimal("0.00")
    sales_count = shift.sales_count or 0
    cash_sales = shift.cash_sales_total or Decimal("0.00")
    noncash_sales = shift.noncash_sales_total or Decimal("0.00")

    try:
        live_totals = shift.calc_live_totals()
        expected = Decimal(str(live_totals.get("drawer_expected_cash") or 0))
    except Exception:
        expected = (shift.opening_cash or Decimal("0.00")) + cash_sales

    closing = shift.closing_cash or Decimal("0.00")
    diff = closing - expected

    diff_line = ""
    if diff != Decimal("0.00"):
        diff_emoji = "⚠️" if diff < 0 else "ℹ️"
        diff_line = f"\n{diff_emoji} <b>Расхождение:</b> {_fmt_money(diff)} сом"

    text = (
        f"📊 <b>Смена закрыта ({cashbox_name})</b>\n"
        f"👤 Кассир: {cashier_name}\n"
        f"⏰ Время: {opened_at_str} — {closed_at_str}\n"
        "──────────────\n"
        f"💰 <b>Выручка:</b> {_fmt_money(sales_total)} сом ({sales_count} чеков)\n"
        f"💵 Наличными: {_fmt_money(cash_sales)} сом\n"
        f"💳 Безналичными: {_fmt_money(noncash_sales)} сом\n"
        f"📥 Факт в кассе: {_fmt_money(closing)} сом\n"
        f"🎯 Ожидалось: {_fmt_money(expected)} сом"
        f"{diff_line}"
    )

    telegram_api.send_message(settings.token, settings.owner_chat_id, text, parse_mode="HTML")
    return True


def send_low_stock_notification(company_id, product_data: dict) -> bool:
    """
    Отправляет уведомление о низком остатке товара владельцу.
    Защита от спама: не чаще раза в 24 часа на один товар.
    """
    from apps.main.telegram_bot.models import TelegramBotSettings

    settings = TelegramBotSettings.objects.filter(company_id=company_id).first()
    if not settings or not settings.token or not settings.owner_chat_id:
        return False

    product_id = product_data.get("product")
    if not product_id:
        return False

    # Debounce на 24 часа (86400 сек)
    cache_key = f"tg_low_stock_debounce:{company_id}:{product_id}"
    if cache.get(cache_key):
        return False

    cache.set(cache_key, 1, timeout=86400)

    name = product_data.get("name") or "Товар"
    qty = product_data.get("quantity") or 0
    min_qty = product_data.get("minimum_quantity") or 0

    text = (
        f"⚠️ <b>Заканчивается товар:</b>\n"
        f"«<b>{name}</b>»\n"
        f"Текущий остаток: {format_qty(qty)} шт (минимум: {format_qty(min_qty)})\n"
        f"<i>Рекомендуется заказать товар у поставщика (/zakaz).</i>"
    )

    telegram_api.send_message(settings.token, settings.owner_chat_id, text, parse_mode="HTML")
    return True


def send_showcase_order_notification(company_id, order_data: dict) -> bool:
    """Уведомление владельца о новом заказе с витрины."""
    from apps.main.telegram_bot.models import TelegramBotSettings

    settings = TelegramBotSettings.objects.filter(company_id=company_id).first()
    if not settings or not settings.token or not settings.owner_chat_id:
        return False

    number = order_data.get("number") or "?"
    total = order_data.get("total") or "0.00"
    cust = order_data.get("customer") or {}
    name = cust.get("name") or "Покупатель"
    phone = cust.get("phone") or ""

    delivery = order_data.get("delivery") or {}
    dtype = "Доставка" if delivery.get("type") == "delivery" else "Самовывоз"

    text = (
        f"🛒 <b>Новый заказ витрины №{number}!</b>\n"
        f"👤 Клиент: {name} ({phone})\n"
        f"💰 Сумма: <b>{_fmt_money(total)} сом</b>\n"
        f"📍 Получение: {dtype}"
    )

    telegram_api.send_message(settings.token, settings.owner_chat_id, text, parse_mode="HTML")
    return True


def handle_internal_event(company_id, event: str, data: dict) -> None:
    """
    Точка входа для внутренних событий компании из apps.integrations.events.emit_event.
    Работает напрямую без лишних HTTP-запросов.
    """
    if not company_id:
        return

    try:
        if event == "shift.closed":
            shift_id = data.get("shift")
            if shift_id:
                from apps.main.telegram_bot.tasks import send_telegram_shift_summary
                send_telegram_shift_summary.delay(str(company_id), str(shift_id))

        elif event == "stock.low":
            send_low_stock_notification(company_id, data)

        # order.created: уведомление о заказе витрины шлёт order_notify (ТЗ Б-22) — с кнопками,
        # флагами суммы/телефона и не больше одного на заказ и чат. Старое сообщение здесь давало дубль.

    except Exception as exc:
        logger.error("handle_internal_event failed for %s (%s): %s", event, company_id, exc)
