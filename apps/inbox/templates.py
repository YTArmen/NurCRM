"""Б-78: шаблоны WhatsApp (синхронизация, создание, стандартный набор, цены, проверка параметров)."""
import re
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from core.exceptions import ApiError

from . import crypto, meta_api
from .models import Channel, WaTemplate

WA_KINDS = ("whatsapp_cloud", "whatsapp_bsp")
CATEGORIES = ("marketing", "utility", "authentication")
STATUS_MAP = {"APPROVED": "approved", "PENDING": "pending", "REJECTED": "rejected", "PAUSED": "paused",
              "DISABLED": "paused", "IN_APPEAL": "pending", "PENDING_DELETION": "paused", "FLAGGED": "paused",
              "LIMIT_EXCEEDED": "paused", "DELETED": "paused"}
NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
PARAM_RE = re.compile(r"\{\{(\d+)\}\}")

# Ориентир цены за сообщение, USD (rate card Meta, обновлять раз в квартал). Переопределяется INBOX_WA_RATE_CARD.
RATE_CARD_UPDATED = "2026-10-01"
RATE_CARD = {
    "default": {"marketing": "0.0604", "utility": "0.0200", "authentication": "0.0200", "service": "0"},
    "KG": {"marketing": "0.0604", "utility": "0.0200", "authentication": "0.0200", "service": "0"},
    "KZ": {"marketing": "0.0600", "utility": "0.0200", "authentication": "0.0200", "service": "0"},
    "RU": {"marketing": "0.0600", "utility": "0.0200", "authentication": "0.0200", "service": "0"},
    "UZ": {"marketing": "0.0600", "utility": "0.0200", "authentication": "0.0200", "service": "0"},
}

STANDARD = {
    "order_ready": ("utility", {
        "ru": "Здравствуйте, {{1}}! Ваш заказ №{{2}} готов к выдаче.",
        "ky": "Саламатсызбы, {{1}}! Сиздин №{{2}} буюртмаңыз даяр.",
        "uz": "Assalomu alaykum, {{1}}! Sizning №{{2}} buyurtmangiz tayyor."}),
    "debt_reminder": ("utility", {
        "ru": "Здравствуйте, {{1}}! Напоминаем о долге {{2}} сом, срок оплаты {{3}}.",
        "ky": "Саламатсызбы, {{1}}! {{2}} сом карызыңызды эскертебиз, төлөө мөөнөтү {{3}}.",
        "uz": "Assalomu alaykum, {{1}}! {{2}} som qarzingiz haqida eslatamiz, to'lov muddati {{3}}."}),
    "receipt": ("utility", {
        "ru": "{{1}}, спасибо за покупку! Ваш чек на {{2}} сом: {{3}}",
        "ky": "{{1}}, сатып алганыңыз үчүн рахмат! Чегиңиз {{2}} сом: {{3}}",
        "uz": "{{1}}, xaridingiz uchun rahmat! Chekingiz {{2}} som: {{3}}"}),
    "thanks": ("marketing", {
        "ru": "{{1}}, спасибо, что выбрали нас! Будем рады видеть вас снова.",
        "ky": "{{1}}, бизди тандаганыңыз үчүн рахмат! Дагы келиңиз.",
        "uz": "{{1}}, bizni tanlaganingiz uchun rahmat! Yana kutib qolamiz."}),
}


def credentials(channel):
    """(provider, waba_id, phone_number_id, token) для WhatsApp-канала."""
    if channel.kind not in WA_KINDS:
        raise ApiError("kind_not_supported", "Шаблоны есть только у каналов WhatsApp.", data={"channel_kind": channel.kind})
    sec = crypto.get_secrets(channel)
    ext = channel.external or {}
    if channel.provider == Channel.Provider.D360:
        token, provider = sec.get("api_key", ""), "360dialog"
    elif channel.provider == Channel.Provider.META:
        token, provider = sec.get("access_token", ""), "meta"
    else:
        raise ApiError("templates_not_supported", "Для этого провайдера шаблоны ведутся в его кабинете.",
                       data={"provider": channel.provider})
    if not token:
        raise ApiError("channel_error", "У канала нет токена доступа.", data={"channel": str(channel.id)})
    return provider, ext.get("waba_id", ""), ext.get("phone_number_id", ""), token


def count_params(components) -> int:
    n = 0
    for c in components or []:
        if (c.get("type") or "").upper() == "BODY":
            idx = [int(x) for x in PARAM_RE.findall(c.get("text") or "")]
            n = max(n, max(idx) if idx else 0)
    return n


def body_text(components) -> str:
    for c in components or []:
        if (c.get("type") or "").upper() == "BODY":
            return c.get("text") or ""
    return ""


def _upsert(channel, item):
    name, lang = item.get("name"), item.get("language")
    if not name or not lang:
        return None
    status = STATUS_MAP.get(str(item.get("status") or "").upper(), "pending")
    reason = ""
    if status == "rejected":
        reason = str(item.get("rejected_reason") or "")
        if reason in ("NONE", ""):
            reason = str(item.get("reason") or "")
    row, _ = WaTemplate.objects.update_or_create(
        channel=channel, name=name, language=lang,
        defaults={"company_id": channel.company_id, "category": str(item.get("category") or "utility").lower(),
                  "components": item.get("components") or [], "status": status, "rejected_reason": reason[:300],
                  "external_id": str(item.get("id") or "")[:64], "synced_at": timezone.now()})
    return row


def sync(channel) -> int:
    """Синхронизация с Meta/BSP: раз в час (задача) и по кнопке. Возвращает число шаблонов."""
    provider, waba_id, _pn, token = credentials(channel)
    try:
        items = meta_api.list_templates(provider, waba_id, token)
    except meta_api.MetaError as exc:
        raise ApiError("provider_error", str(exc), status_code=502, data={"code": exc.code})
    seen = [r.pk for r in (_upsert(channel, it) for it in items) if r]
    return len(seen)


def template_out(t) -> dict:
    return {"id": t.id, "name": t.name, "language": t.language, "category": t.category,
            "components": t.components, "status": t.status,
            "rejected_reason": t.rejected_reason or None, "params": count_params(t.components),
            "synced_at": t.synced_at.isoformat() if t.synced_at else None}


def validate_new(data):
    name = str(data.get("name") or "").strip()
    lang = str(data.get("language") or "").strip()
    cat = str(data.get("category") or "").strip().lower()
    comps = data.get("components")
    if not NAME_RE.match(name):
        raise ApiError("invalid", "name: латиница в нижнем регистре, цифры и _.", fields={"name": ["Недопустимое значение"]})
    if not lang or len(lang) > 10:
        raise ApiError("invalid", "language: например ru, ky, uz.", fields={"language": ["Обязательное поле"]})
    if cat not in CATEGORIES:
        raise ApiError("invalid", "category: marketing, utility или authentication.", fields={"category": ["Недопустимое значение"]})
    if not isinstance(comps, list) or not body_text(comps):
        raise ApiError("invalid", "components: нужен компонент BODY с текстом.", fields={"components": ["Нужен BODY"]})
    return name, lang, cat, comps


def create(channel, name, lang, cat, comps):
    """Создание шаблона у Meta/BSP; в базе — со статусом, который вернул провайдер (обычно pending)."""
    provider, waba_id, _pn, token = credentials(channel)
    existing = WaTemplate.objects.filter(channel=channel, name=name, language=lang).first()
    if existing and existing.status != "rejected":
        raise ApiError("template_exists", "Шаблон с таким именем и языком уже есть.", status_code=409)
    payload = {"name": name, "language": lang, "category": cat.upper(), "components": comps}
    try:
        res = meta_api.create_template(provider, waba_id, token, payload)
    except meta_api.MetaError as exc:
        raise ApiError("provider_error", str(exc), status_code=502 if not exc.permanent else 400, data={"code": exc.code})
    return _upsert(channel, {"id": res.get("id"), "name": name, "language": lang, "category": cat,
                             "components": comps, "status": res.get("status") or "PENDING"})


def create_standard(channel, languages=("ru", "ky", "uz")):
    """Набор магазина: «заказ готов», «долг», «чек», «спасибо» на ru, ky, uz. Уже созданные пропускаются."""
    created, skipped, failed = [], [], []
    for name, (cat, texts) in STANDARD.items():
        for lang in languages:
            if lang not in texts:
                continue
            if WaTemplate.objects.filter(channel=channel, name=name, language=lang).exclude(status="rejected").exists():
                skipped.append(f"{name}:{lang}")
                continue
            comps = [{"type": "BODY", "text": texts[lang],
                      "example": {"body_text": [["Асан"] + ["100"] * (count_params([{"type": "BODY", "text": texts[lang]}]) - 1)]}}]
            try:
                create(channel, name, lang, cat, comps)
                created.append(f"{name}:{lang}")
            except ApiError as exc:
                failed.append({"template": f"{name}:{lang}", "code": exc.code})
    return {"created": created, "skipped": skipped, "failed": failed}


def pricing(company, country=None) -> dict:
    card = getattr(settings, "INBOX_WA_RATE_CARD", None) or RATE_CARD
    country = (country or getattr(company, "country", "") or "KG").upper()[:2]
    rates = card.get(country) or card["default"]
    return {"country": country, "currency": "USD", "per_message": rates, "updated": RATE_CARD_UPDATED,
            "note": "Ориентир по rate card Meta; оплата идёт магазину у Meta или провайдера, NurCRM деньги не списывает."}


def estimate_cost(category, count, company):
    rates = pricing(company)["per_message"]
    return str((Decimal(rates.get(category, "0")) * count).quantize(Decimal("0.01")))


# ------------------------------------------------------------------ отправка шаблона (Б-75 п. 3, kind=template)

def prepare_template_message(conv, tdata):
    """Проверка шаблона и числа параметров. Возвращает (template_json, текст для ленты)."""
    channel = conv.channel
    if channel.kind not in WA_KINDS:
        raise ApiError("kind_not_supported", "Шаблоны поддерживает только WhatsApp.",
                       data={"kind": "template", "channel_kind": channel.kind})
    if not isinstance(tdata, dict):
        raise ApiError("invalid", "template: {name, language, params[]}.", fields={"template": ["Обязательное поле"]})
    name, lang = str(tdata.get("name") or ""), str(tdata.get("language") or "")
    params = tdata.get("params") or []
    if not isinstance(params, list):
        raise ApiError("invalid", "template.params: массив строк.", fields={"template.params": ["Ожидается массив"]})
    t = WaTemplate.objects.filter(channel=channel, name=name, language=lang).first()
    if t is None:
        raise ApiError("template_not_found", "Шаблон не найден в этом канале.", status_code=404)
    if t.status != "approved":
        raise ApiError("template_not_approved", "Шаблон ещё не одобрен.", data={"status": t.status})
    need = count_params(t.components)
    if len(params) != need:
        raise ApiError("template_params_mismatch", f"Шаблону нужно параметров: {need}, передано: {len(params)}.",
                       data={"expected": need, "got": len(params)})
    text = body_text(t.components)
    for i, p in enumerate(params, 1):
        text = text.replace("{{%d}}" % i, str(p))
    return {"name": name, "language": lang, "params": [str(p) for p in params], "category": t.category}, text
