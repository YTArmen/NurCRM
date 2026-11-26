"""ТЗ кассы 05.11 п.31 / Б-30: сообщения покупателям серверным ботом.

Правила проверяются при отправке, а не только при постановке в очередь:
  - служебные (чек, долг, срок проката, заказ готов) — только если у клиента consents.service=true;
  - реклама — только если consents.marketing=true;
  - в тихие часы компании сообщение откладывается до конца тихих часов (status=deferred, send_after);
  - язык — Client.language (ru|ky|en|tr|uz); нет текста на языке — русский;
  - одно сообщение на событие: повтор с тем же dedupe_key ничего не отправляет.
Серверные рассылки покупателям включаются только через этот модуль.
"""
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.main.telegram_bot.models import TelegramBotSettings, TelegramCustomerOutbox
from apps.main.telegram_bot.services import telegram_api
from apps.main.tz31_consents import LANGUAGES, quiet_until

logger = logging.getLogger(__name__)

SERVICE_KINDS = ("receipt", "debt_changed", "debt_paid", "rental_due", "order_ready", "other")
MARKETING_KIND = "marketing"

TEXTS = {
    "receipt": {
        "ru": "Спасибо за покупку в «{shop}». Сумма чека: {amount}.",
        "ky": "«{shop}» дүкөнүнөн сатып алганыңыз үчүн рахмат. Чектин суммасы: {amount}.",
        "en": "Thank you for shopping at {shop}. Receipt total: {amount}.",
        "tr": "{shop} mağazasından alışveriş yaptığınız için teşekkürler. Fiş tutarı: {amount}.",
        "uz": "{shop} do'konidan xarid qilganingiz uchun rahmat. Chek summasi: {amount}.",
    },
    "debt_changed": {
        "ru": "«{shop}»: ваш долг изменился. Сейчас долг: {amount}.",
        "ky": "«{shop}»: карызыңыз өзгөрдү. Азыркы карыз: {amount}.",
        "en": "{shop}: your balance due has changed. Current debt: {amount}.",
        "tr": "{shop}: borcunuz değişti. Güncel borç: {amount}.",
        "uz": "{shop}: qarzingiz o'zgardi. Joriy qarz: {amount}.",
    },
    "debt_paid": {
        "ru": "«{shop}»: долг погашен. Спасибо!",
        "ky": "«{shop}»: карыз төлөндү. Рахмат!",
        "en": "{shop}: your debt is paid off. Thank you!",
        "tr": "{shop}: borcunuz kapandı. Teşekkürler!",
        "uz": "{shop}: qarz to'landi. Rahmat!",
    },
    "rental_due": {
        "ru": "«{shop}»: срок проката истекает {date}. Пожалуйста, верните или продлите.",
        "ky": "«{shop}»: ижара мөөнөтү {date} бүтөт. Кайтарып бериңиз же узартыңыз.",
        "en": "{shop}: your rental ends on {date}. Please return or extend it.",
        "tr": "{shop}: kiralama süreniz {date} tarihinde bitiyor. Lütfen iade edin veya uzatın.",
        "uz": "{shop}: ijara muddati {date} da tugaydi. Qaytaring yoki uzaytiring.",
    },
    "order_ready": {
        "ru": "«{shop}»: ваш заказ {number} готов.",
        "ky": "«{shop}»: {number} буйрутмаңыз даяр.",
        "en": "{shop}: your order {number} is ready.",
        "tr": "{shop}: {number} numaralı siparişiniz hazır.",
        "uz": "{shop}: {number} buyurtmangiz tayyor.",
    },
}

STOP_REPLY = {
    "ru": "Вы отписались от сообщений. Чтобы вернуться, напишите /start.",
    "ky": "Билдирүүлөрдөн баш тарттыңыз. Кайтуу үчүн /start жазыңыз.",
    "en": "You have unsubscribed from messages. Send /start to subscribe again.",
    "tr": "Mesajlardan çıktınız. Geri dönmek için /start yazın.",
    "uz": "Xabarlardan voz kechdingiz. Qaytish uchun /start yozing.",
}


class _Safe(dict):
    def __missing__(self, key):
        return ""


def render(kind, lang, params=None, text=None):
    """Текст по виду события и языку (нет перевода — русский). `text` — готовый текст (реклама, прочее)."""
    lang = lang if lang in LANGUAGES else "ru"
    if text:
        return str(text if not isinstance(text, dict) else (text.get(lang) or text.get("ru") or ""))
    tpl = TEXTS.get(kind)
    if not tpl:
        return ""
    return (tpl.get(lang) or tpl["ru"]).format_map(_Safe(params or {}))


def _skip(row, reason):
    row.status, row.reason = TelegramCustomerOutbox.Status.SKIPPED, reason
    row.save(update_fields=["status", "reason"])
    return row


def _check_and_send(row, client, company, now):
    """Проверки в момент отправки: согласие, тихие часы, бот. Меняет row.status."""
    St = TelegramCustomerOutbox.Status
    ok_consent = client.consent_marketing if row.category == "marketing" else client.consent_service
    if not ok_consent:
        return _skip(row, "no_consent")
    chat_id = row.chat_id or client.telegram_chat_id
    if not chat_id:
        return _skip(row, "no_chat")
    until = quiet_until(company, now)
    if until:
        row.status, row.reason, row.send_after = St.DEFERRED, "quiet_hours", until
        row.save(update_fields=["status", "reason", "send_after"])
        return row
    bot = TelegramBotSettings.objects.filter(company=company).first()
    if not bot or not bot.token:
        return _skip(row, "no_bot")
    if not row.text:
        return _skip(row, "empty_text")
    resp = telegram_api.send_message(bot.token, chat_id, row.text, parse_mode=None)
    if resp.get("ok"):
        row.status, row.reason, row.sent_at, row.error = St.SENT, "", now, ""
        row.save(update_fields=["status", "reason", "sent_at", "error"])
    else:
        row.status, row.reason = St.FAILED, "telegram_error"
        row.error = str(resp.get("description") or "")[:255]
        row.save(update_fields=["status", "reason", "error"])
    return row


def send_to_customer(company, client, kind, *, params=None, text=None, dedupe_key=""):
    """Сообщение покупателю. kind: receipt|debt_changed|debt_paid|rental_due|order_ready|other|marketing.
    Возвращает строку журнала; повтор с тем же dedupe_key возвращает прежнюю без отправки."""
    category = "marketing" if kind == MARKETING_KIND else "service"
    dedupe_key = (dedupe_key or "").strip()[:128]
    if dedupe_key:
        prev = TelegramCustomerOutbox.objects.filter(company=company, dedupe_key=dedupe_key).first()
        if prev:
            return prev
    lang = client.language if client.language in LANGUAGES else "ru"
    params = dict(params or {})
    params.setdefault("shop", getattr(company, "name", "") or "")
    try:
        with transaction.atomic():
            row = TelegramCustomerOutbox.objects.create(
                company=company, client=client, chat_id=client.telegram_chat_id or "", kind=kind, category=category,
                language=lang, text=render(kind, lang, params, text), dedupe_key=dedupe_key,
            )
    except IntegrityError:
        return TelegramCustomerOutbox.objects.get(company=company, dedupe_key=dedupe_key)
    return _check_and_send(row, client, company, timezone.now())


def flush_deferred(limit=200):
    """Отложенные, у которых наступило send_after: согласие и тихие часы проверяются заново."""
    now = timezone.now()
    rows = list(TelegramCustomerOutbox.objects.filter(
        status=TelegramCustomerOutbox.Status.DEFERRED, send_after__lte=now,
    ).select_related("client", "company").order_by("send_after")[:limit])
    done = 0
    for row in rows:
        if not row.client_id:
            _skip(row, "no_client")
            continue
        try:
            _check_and_send(row, row.client, row.company, now)
            done += 1
        except Exception:
            logger.exception("customer outbox flush failed: %s", row.pk)
    return done
