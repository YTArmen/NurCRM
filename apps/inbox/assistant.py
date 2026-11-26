"""ИИ-помощник витрины (ТЗ 01.11.2026, п. 6–9): прокси к Gemini, защита от злоупотреблений, согласие, очистка.

Правила:
- ключ Gemini — ТОЛЬКО ключ самого магазина (настройки его Telegram-бота). Общего ключа NurCRM здесь нет, ключ
  никогда не попадает в ответы;
- модель вызывает функции search_products, get_product, get_size_chart, get_shop_info, add_to_cart, handoff;
- цены, остатки и суммы в тексте ответа сверяются с данными функций ЭТОГО ЖЕ хода, расхождение → «уточню у продавца»
  и в журнале ходов blocked=hallucination_blocked;
- корзину ведёт фронт: add_to_cart только возвращает данные строки.
"""
import hashlib
import logging
import os
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.utils import timezone

from core.exceptions import ApiError

from . import ai as inbox_ai
from . import services
from .models import AssistantSettings, AssistantTurn, ChatConsentLog, Conversation, WidgetSession

logger = logging.getLogger("inbox.assistant")

LANGS = ("ru", "ky", "en", "uz", "kk")  # языки витрины (design_schema.LANGUAGES)
KNOWLEDGE_MAX = 2000
SESSION_PER_MIN = 6
SESSION_PER_DAY = 40
IP_PER_DAY = 120
DEFAULT_DAILY_CAP = 500
CACHE_TTL = 600
RETENTION_DAYS = 90
MAX_QUESTION = 500
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

UNKNOWN_TEXT = "Уточню у продавца."
UNAVAILABLE_TEXT = "Помощник сейчас недоступен. Напишите продавцу — он ответит."
BUSY_TEXT = "Помощник занят. Свяжитесь с продавцом напрямую."


# ------------------------------------------------------------------ настройки

def get_settings(company) -> AssistantSettings:
    obj, _ = AssistantSettings.objects.get_or_create(company=company, defaults={"daily_cap": DEFAULT_DAILY_CAP})
    return obj


def shop_key(company) -> str:
    """Ключ Gemini ТОЛЬКО самого магазина. Без запасного общего ключа."""
    from apps.main.telegram_bot.models import TelegramBotSettings
    bs = TelegramBotSettings.objects.filter(company_id=company.id).first()
    return (bs.ai_key if bs else "") or ""


def globally_disabled() -> bool:
    """Общий выключатель: env SHOWCASE_ASSISTANT_DISABLED=1 или settings.SHOWCASE_ASSISTANT_ENABLED=False."""
    if (os.getenv("SHOWCASE_ASSISTANT_DISABLED") or "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return getattr(dj_settings, "SHOWCASE_ASSISTANT_ENABLED", True) is False


def settings_out(a: AssistantSettings, company) -> dict:
    key_set = bool(shop_key(company))
    return {"enabled": a.enabled, "daily_cap": a.daily_cap, "knowledge": a.knowledge or {},
            "knowledge_max_chars": KNOWLEDGE_MAX, "languages": list(LANGS),
            "key_set": key_set,  # сам ключ не отдаётся никогда
            "globally_disabled": globally_disabled(),
            "available": bool(a.enabled and key_set and not globally_disabled())}


def apply_settings(a: AssistantSettings, data: dict):
    if "enabled" in data:
        a.enabled = bool(data["enabled"])
    if "daily_cap" in data:
        try:
            cap = int(data["daily_cap"])
            if not 1 <= cap <= 100000:
                raise ValueError
        except (TypeError, ValueError):
            raise ApiError("invalid", "daily_cap: число от 1 до 100000.", fields={"daily_cap": ["Недопустимое значение"]})
        a.daily_cap = cap
    if "knowledge" in data:
        v = data["knowledge"]
        if not isinstance(v, dict) or any(k not in LANGS or not isinstance(x, str) for k, x in v.items()):
            raise ApiError("invalid", f"knowledge: объект {{{', '.join(LANGS)}}} со строками.",
                           fields={"knowledge": ["Недопустимое значение"]})
        for k, x in v.items():
            if len(x) > KNOWLEDGE_MAX:
                raise ApiError("invalid", f"knowledge.{k}: не больше {KNOWLEDGE_MAX} символов.",
                               fields={f"knowledge.{k}": ["Слишком длинный текст"]})
        a.knowledge = {k: x.strip() for k, x in v.items() if x.strip()}
    a.save()


# ------------------------------------------------------------------ Turnstile

def turnstile_verify(token, ip) -> bool:
    """Cloudflare Turnstile. Секрет — env TURNSTILE_SECRET_KEY; не задан → проверка пропускается (предупреждение)."""
    secret = (os.getenv("TURNSTILE_SECRET_KEY") or getattr(dj_settings, "TURNSTILE_SECRET_KEY", "") or "").strip()
    if not secret:
        logger.warning("TURNSTILE_SECRET_KEY не задан: проверка Turnstile пропущена")
        return True
    if not token:
        return False
    try:
        import httpx
        r = httpx.post(TURNSTILE_URL, data={"secret": secret, "response": token, "remoteip": ip or ""}, timeout=8.0)
        return bool(r.status_code == 200 and r.json().get("success"))
    except Exception:
        logger.warning("Turnstile: проверка не удалась", exc_info=True)
        return False


# ------------------------------------------------------------------ контакты «связаться с продавцом»

def handoff_contacts(company, session=None) -> dict:
    from .models import Channel
    out = {"phone": company.phone or None, "telegram_url": None, "whatsapp_url": None, "chat": True}
    ch = Channel.objects.filter(company_id=company.id, kind=Channel.Kind.TELEGRAM_BOT).first()
    bot = ((ch.external or {}).get("bot_username") if ch else None) or ""
    if bot:
        out["telegram_url"] = f"https://t.me/{bot}"
    digits = re.sub(r"\D", "", company.phone or "")
    if len(digits) >= 9:
        out["whatsapp_url"] = f"https://wa.me/{digits}"
    return out


# ------------------------------------------------------------------ инструменты (только чтение)

class Turn:
    """Данные одного хода: что вернули функции (для сверки чисел и карточек)."""

    def __init__(self, company, lang, key_hidden):
        self.company, self.lang = company, lang
        self.tool_results = []   # str(result) для сверки чисел
        self.tools_called = []
        self.cards = {}         # id → карточка
        self.cart_actions = []
        self.handoff = None
        self._hidden = key_hidden


def _hidden_products(company):
    try:
        from apps.main.showcase import services as svc
        return set(svc.get_public_catalog(company).hidden_products)
    except Exception:
        return set()


def _image_url(p):
    img = p.images.filter(image__isnull=False).exclude(image="").order_by("-is_primary", "created_at").first() \
        if hasattr(p, "images") else None
    try:
        return img.image.url if img else None
    except Exception:
        return None


def _card(p):
    variants = []
    for v in p.variants.filter(is_active=True)[:30]:
        variants.append({"id": str(v.id), "size": v.size, "color": v.color,
                         "price": str(v.price if v.price is not None else p.price), "in_stock": (v.quantity or 0) > 0})
    card = {"id": str(p.id), "name": p.name, "price": str(p.price), "unit": p.unit or "",
            "in_stock": (p.quantity or 0) > 0, "image_url": _image_url(p)}
    if variants:
        card["variants"] = variants
    return card


def _find_product(turn, ref):
    p = inbox_ai._product_by(turn.company, ref)
    if p is not None and str(p.id) in turn._hidden:
        return None
    return p


def t_search_products(turn, args):
    from apps.main.ai_agent_api import search_products
    q = str(args.get("query") or args.get("q") or "").strip()[:200]
    rows = []
    if q:
        for _sc, p in search_products(turn.company, q, limit=6):
            if str(p.id) in turn._hidden:
                continue
            full = type(p).objects.get(pk=p.pk)
            c = _card(full)
            turn.cards[c["id"]] = c
            rows.append({k: v for k, v in c.items() if k != "image_url"})
    return {"results": rows[:5]}


def t_get_product(turn, args):
    p = _find_product(turn, args.get("product"))
    if p is None:
        return {"error": "product_not_found"}
    c = _card(p)
    turn.cards[c["id"]] = c
    out = {k: v for k, v in c.items() if k != "image_url"}
    desc = (getattr(p, "description", "") or "")[:500]
    if desc:
        out["description"] = desc
    return out


def t_get_size_chart(turn, args):
    p = _find_product(turn, args.get("product"))
    if p is None:
        return {"error": "product_not_found"}
    from apps.main.showcase.size_charts import public_size_chart
    chart = public_size_chart(p)
    return {"product": p.name, "size_chart": chart} if chart else {"product": p.name, "size_chart": None}


def t_get_shop_info(turn, args):
    company = turn.company
    out = {"name": company.name, "address": company.address or None, "phone": company.phone or None}
    try:
        from apps.main.showcase import services as svc
        cart = svc.published_document(company).get("cart") or {}
        out["delivery"] = cart.get("delivery")
        out["payment"] = cart.get("payment") or cart.get("payment_methods")
    except Exception:
        pass
    k = get_settings(company).knowledge or {}
    rules = k.get(turn.lang) or None
    if not rules:
        # ТЗ 01.11 п.8: касса шлёт «Правила магазина» в дизайн сайта (shop.info.knowledge)
        try:
            from apps.main.showcase.services import get_shop_knowledge
            rules = get_shop_knowledge(company, turn.lang) or None
        except Exception:
            rules = None
    out["rules"] = rules or k.get("ru") or None
    return out


def t_add_to_cart(turn, args):
    p = _find_product(turn, args.get("product"))
    if p is None:
        return {"error": "product_not_found"}
    try:
        qty = max(1, min(int(args.get("qty") or 1), 99))
    except (TypeError, ValueError):
        qty = 1
    price = p.price
    variant_id = None
    var = None
    ref = str(args.get("variant") or "").strip()
    if ref:
        for v in p.variants.filter(is_active=True):
            if ref in (str(v.id), v.size, f"{v.size} {v.color}".strip(), v.color):
                var = v
                break
        if var is None:
            return {"error": "variant_not_found"}
        variant_id = str(var.id)
        if var.price is not None:
            price = var.price
        in_stock = (var.quantity or 0) > 0
    else:
        if p.variants.filter(is_active=True).exists():
            return {"error": "variant_required", "detail": "У товара есть размеры/цвета: уточните вариант."}
        in_stock = (p.quantity or 0) > 0
    if not in_stock:
        return {"error": "out_of_stock", "product": p.name}
    total = (Decimal(price) * qty).quantize(Decimal("0.01"))
    line = {"product": str(p.id), "variant": variant_id, "qty": qty, "name": p.name, "price": str(price),
            "line_total": str(total)}
    turn.cart_actions.append({"type": "add_to_cart", **line})
    c = _card(p)
    turn.cards[c["id"]] = c
    return {"added": line}


def t_handoff(turn, args):
    turn.handoff = {"requested": True, "reason": str(args.get("reason") or "")[:200]}
    return {"ok": True}


TOOLS = {
    "search_products": (t_search_products, "Поиск товаров магазина по названию: цена, остаток, размеры/цвета.",
                        {"query": {"type": "string", "description": "Что ищет покупатель"}}, ["query"]),
    "get_product": (t_get_product, "Карточка товара по id или названию: цена, остаток, варианты.",
                    {"product": {"type": "string"}}, ["product"]),
    "get_size_chart": (t_get_size_chart, "Таблица размеров товара.", {"product": {"type": "string"}}, ["product"]),
    "get_shop_info": (t_get_shop_info, "Адрес, телефон, доставка, оплата и «Правила магазина» (обмен, возврат, примерка, часы).",
                      {}, []),
    "add_to_cart": (t_add_to_cart, "Положить товар в корзину покупателя (корзину ведёт сайт).",
                    {"product": {"type": "string"}, "variant": {"type": "string", "description": "id, размер или цвет"},
                     "qty": {"type": "integer"}}, ["product"]),
    "handoff": (t_handoff, "Позвать продавца-человека (жалоба, оплата, возврат, нестандартный вопрос).",
                {"reason": {"type": "string"}}, []),
}


def tool_declarations():
    out = []
    for name, (_fn, desc, props, req) in TOOLS.items():
        d = {"name": name, "description": desc}
        if props:
            d["parameters"] = {"type": "object", "properties": props, "required": req}
        out.append(d)
    return out


def _system_prompt(company, lang):
    return (
        f"Ты ИИ-помощник интернет-магазина «{company.name}». Отвечай на языке покупателя (по умолчанию «{lang}»), кратко, без Markdown.\n"
        "ПРАВИЛА:\n"
        "1. Цены, наличие, суммы, адрес, часы — ТОЛЬКО из ответов функций. Цифр, которых нет в ответах функций, называть нельзя.\n"
        "2. Для вопросов про товар вызови search_products/get_product; про размеры — get_size_chart; про доставку, обмен, возврат, "
        "примерку, адрес — get_shop_info.\n"
        "3. Положить в корзину можно только через add_to_cart после согласия покупателя; оформляет заказ сам покупатель на сайте.\n"
        "4. Не обещай скидки, сроки и резерв. Жалоба, оплата, возврат, непонятный вопрос — вызови handoff.\n"
        "5. Посторонние темы — вежливо откажись одной фразой."
    )


# ------------------------------------------------------------------ сверка чисел

_NUM = re.compile(r"\d+")


def numbers_of(s):
    """Числа (≥ 2 цифр): «1 900» → 1900; дробная часть отдельным числом не считается."""
    t = re.sub(r"(?<=\d)[  ](?=\d{3}(?!\d))", "", s or "")
    t = re.sub(r"(\d)[.,]\d+", r"\1", t)
    return {n for n in _NUM.findall(t) if len(n) >= 2}


def numbers_grounded(answer, question, tool_results) -> bool:
    allowed = numbers_of(question)
    for r in tool_results:
        allowed |= numbers_of(r)
    return numbers_of(answer) <= allowed


# ------------------------------------------------------------------ модель

def _call_model(api_key, system, contents, execute):
    """Единственное место сетевого вызова Gemini (в тестах подменяется)."""
    from apps.main.telegram_bot.services import ai_service
    return ai_service.generate_chat_response_with_tools(
        api_key=api_key, system_instruction=system, contents=contents, tools=tool_declarations(),
        execute_tool_fn=execute, temperature=0.2, max_tokens=600)


def run_model(company, question, lang, history=None) -> dict:
    """Один ход модели с функциями. Возвращает dict ответа (без ключа) + служебное _tokens/_tools/_blocked."""
    turn = Turn(company, lang, _hidden_products(company))

    def execute(name, args):
        spec = TOOLS.get(name)
        turn.tools_called.append(name)
        if not spec:
            return {"error": "unknown_tool"}
        try:
            result = spec[0](turn, args or {})
        except Exception:
            logger.exception("assistant tool %s failed", name)
            result = {"error": "tool_failed"}
        turn.tool_results.append(str(result))
        return result

    contents = list(history or [])
    if not contents or contents[-1]["role"] != "user":
        contents.append({"role": "user", "parts": [{"text": question}]})
    system = _system_prompt(company, lang)
    text, _model, _called = _call_model(shop_key(company), system, contents, execute)
    text = (text or "").strip()
    tokens = max(1, (len(system) + sum(len(c["parts"][0].get("text", "")) for c in contents if c.get("parts")
                                       and isinstance(c["parts"][0], dict)) + len(text)) // 4)
    blocked = ""
    if not text:
        text = UNKNOWN_TEXT
    elif not numbers_grounded(text, question, turn.tool_results):
        logger.warning("assistant: hallucination_blocked company=%s", company.id)
        text, blocked = UNKNOWN_TEXT, "hallucination_blocked"
    return {"answer": text, "cards": list(turn.cards.values())[:5], "cart_actions": turn.cart_actions,
            "handoff": turn.handoff, "_tokens": tokens, "_tools": turn.tools_called, "_blocked": blocked}


# ------------------------------------------------------------------ лимиты

def check_limits(company, a, session, ip):
    """None, если можно; иначе короткое имя причины."""
    now = timezone.now()
    base = AssistantTurn.objects.filter(counted=True)
    if session is not None:
        s = base.filter(session=session)
        if s.filter(created_at__gte=now - timedelta(minutes=1)).count() >= SESSION_PER_MIN:
            return "session_minute"
        if s.filter(created_at__gte=now - timedelta(days=1)).count() >= SESSION_PER_DAY:
            return "session_day"
    day = now - timedelta(days=1)
    if ip and base.filter(ip=ip, created_at__gte=day).count() >= IP_PER_DAY:
        return "ip_day"
    if base.filter(company=company, created_at__gte=day).count() >= (a.daily_cap or DEFAULT_DAILY_CAP):
        return "shop_day"
    return None


def _cache_key(company, lang, question):
    norm = re.sub(r"\s+", " ", question.strip().lower())
    return f"sc_ai:{company.id}:{lang}:{hashlib.sha1(norm.encode()).hexdigest()}"


def _log(company, session, ip, question, outcome, **kw):
    return AssistantTurn.objects.create(company=company, session=session, ip=(ip or "")[:64],
                                        question=question[:MAX_QUESTION], outcome=outcome, **kw)


def _unavailable(company, session, text=UNAVAILABLE_TEXT):
    return {"status": "unavailable", "unavailable": True, "answer": text, "cards": [], "cart_actions": [],
            "handoff": {"requested": True, "reason": "assistant_unavailable", "contacts": handoff_contacts(company, session)}}


def _busy(company, session, reason):
    return {"status": "busy", "busy": True, "reason": reason, "answer": BUSY_TEXT, "cards": [], "cart_actions": [],
            "handoff": {"requested": True, "reason": "busy", "contacts": handoff_contacts(company, session)}}


def availability(company, a):
    """None, если помощник доступен; иначе причина."""
    if globally_disabled():
        return "global_off"
    if not a.enabled:
        return "disabled"
    if not shop_key(company):
        return "no_key"
    return None


def ask(session, text, lang, ip, client_msg_id=None) -> dict:
    """Вопрос посетителя помощнику: пишет сообщения в ящик (продавец видит), соблюдает лимиты и кэш."""
    from . import widget
    company = session.company
    text = (text or "").strip()
    lang = lang if lang in LANGS else "ru"
    if len(text) > MAX_QUESTION:
        raise ApiError("invalid", f"Вопрос длиннее {MAX_QUESTION} символов.", fields={"text": ["Слишком длинный"]})
    cmid = client_msg_id or f"ai{os.urandom(6).hex()}"
    try:
        msg, created = widget.post_message(session, text, cmid)
    except widget.RateLimited:
        _log(company, session, ip, text, "busy", counted=False)
        return _busy(company, session, "message_rate")
    if not created:
        return {"status": "duplicate", "answer": "", "cards": [], "cart_actions": [], "handoff": None}
    a = get_settings(company)
    why = availability(company, a)
    if why:
        _log(company, session, ip, text, "unavailable", counted=False)
        return _unavailable(company, session)
    if services.ai_paused(session.channel, session.contact.external_id):
        return {"status": "human", "answer": "", "cards": [], "cart_actions": [], "handoff": None}
    reason = check_limits(company, a, session, ip)
    if reason:
        _log(company, session, ip, text, "busy", counted=False)
        return _busy(company, session, reason)

    key = _cache_key(company, lang, text)
    try:
        hit = cache.get(key)
    except Exception:
        hit = None
    if hit:
        _log(company, session, ip, text, "cached", tools=["cache"])
        res = dict(hit)
        res["cached"] = True
    else:
        conv = Conversation.objects.filter(contact_id=session.contact_id).order_by("-id").first()
        try:
            raw = run_model(company, text, lang, history=inbox_ai._history(conv) if conv else None)
        except Exception:
            logger.warning("assistant: модель недоступна", exc_info=True)
            _log(company, session, ip, text, "error", counted=False)
            return _unavailable(company, session)
        tools, tokens, blocked = raw.pop("_tools"), raw.pop("_tokens"), raw.pop("_blocked")
        handoff = raw["handoff"]
        if handoff:
            handoff["contacts"] = handoff_contacts(company, session)
            if conv:
                services.set_needs_human(conv, True)
        res = {"status": "ok", **raw}
        _log(company, session, ip, text, "handoff" if handoff else "answered", tokens=tokens, tools=tools, blocked=blocked)
        if not (handoff or blocked or raw["cart_actions"]):
            try:
                cache.set(key, res, CACHE_TTL)
            except Exception:
                logger.warning("assistant: кэш недоступен")
    if res.get("answer"):
        try:
            services.record_bot_message(session.channel, session.contact.external_id, res["answer"], sender="ai",
                                        message_external_id=f"wai:{session.id.hex}:{msg.id}")
        except Exception:
            logger.exception("assistant: не удалось записать ответ в ящик")
    return res


def test_question(company, text, lang="ru") -> dict:
    """«Проверить помощника»: пробный вопрос владельца. Лимиты не тратит, в диалоги не пишет."""
    a = get_settings(company)
    why = availability(company, a)
    if why:
        return {"ok": False, "reason": why, "answer": "", "cards": []}
    try:
        raw = run_model(company, (text or "").strip()[:MAX_QUESTION] or "Здравствуйте", lang if lang in LANGS else "ru")
    except Exception as exc:
        logger.warning("assistant test: %s", exc)
        return {"ok": False, "reason": "model_error", "answer": "", "cards": []}
    _log(company, None, "", text or "", "answered", tokens=raw["_tokens"], tools=raw["_tools"], blocked=raw["_blocked"],
         counted=False)
    return {"ok": True, "answer": raw["answer"], "cards": raw["cards"], "tools": raw["_tools"],
            "blocked": raw["_blocked"] or None}


# ------------------------------------------------------------------ данные посетителя

def log_consent(session, ip, consent=True, kind="phone_to_seller"):
    return ChatConsentLog.objects.create(company_id=session.company_id, session=session, kind=kind, consent=consent,
                                         ip=(ip or "")[:64])


def delete_session_data(session) -> dict:
    """«Удалить мой чат»: контакт посетителя вместе с диалогом, сообщениями и сеансом. Журнал согласий остаётся
    (без ссылки на сеанс), текст вопросов в журнале ходов стирается."""
    AssistantTurn.objects.filter(session=session).update(question="")
    contact = session.contact
    n = contact.delete()[0]
    return {"deleted": n}


def purge_old(days=RETENTION_DAYS, now=None) -> dict:
    """Хранение 90 дней: истёкшие/старые сеансы витрины с их диалогами и старые записи журнала ходов. Повторный
    запуск безопасен (удаляет только то, что старше срока)."""
    from .models import ChannelContact
    now = now or timezone.now()
    cutoff = now - timedelta(days=days)
    contact_ids = list(WidgetSession.objects.filter(created_at__lt=cutoff).values_list("contact_id", flat=True).distinct())
    # посетитель, который писал позже границы, не трогаем
    from .models import Message
    keep = set(Message.objects.filter(conversation__contact_id__in=contact_ids, created_at__gte=cutoff)
               .values_list("conversation__contact_id", flat=True))
    stale = [c for c in contact_ids if c not in keep]
    sessions = WidgetSession.objects.filter(contact_id__in=stale).count()
    if stale:
        ChannelContact.objects.filter(pk__in=stale).delete()
    turns = AssistantTurn.objects.filter(created_at__lt=cutoff).delete()[0]
    return {"contacts": len(stale), "sessions": sessions, "turns": turns}  # журнал согласий не чистим
