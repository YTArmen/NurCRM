"""ИИ-ответчик ящика (Б-79). Модель — только через механизм серверного бота
(apps.main.telegram_bot.services.ai_service: ключ компании или общий ключ сервера).

Инструменты ИИ — ТОЛЬКО чтение данных компании. Цифры в ответе обязаны быть в ответах инструментов.
Персональные данные в модель уходят кодами [Д1]. Заказ (order.create) ИИ не создаёт: «заказ» — тема для человека.
"""
import logging
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings as dj_settings
from django.utils import timezone

from core.exceptions import ApiError

from .models import AiRun, Conversation, Message

logger = logging.getLogger("inbox.ai")

TOPICS = ("price", "stock", "location", "hours", "address", "delivery", "order")
TOPIC_WORDS = {
    "order": ("заказ", "оформ", "закажу", "заказать", "купить", "куплю", "возьму"),
    "location": ("где лежит", "ячейк", "полк", "в каком ряду", "где найти"),
    "price": ("сколько стоит", "цена", "цену", "почём", "почем", "стоимост", "сколько", "баа", "price"),
    "stock": ("налич", "есть ли", "остаток", "остатк", "осталось", "есть в", "барбы", "stock"),
    "hours": ("график", "до скольки", "работает", "часы работы", "режим работы", "открыт", "закрыт"),
    "address": ("адрес", "где наход", "как добраться", "как проехать", "где вы"),
    "delivery": ("достав", "курьер", "самовывоз"),
}
HANDOFF_WORDS = {
    "operator": ("оператор", "человек", "менеджер", "живой", "позови", "продавца"),
    "complaint": ("жалоб", "ужасн", "обман", "безобраз", "претензи", "мошенник"),
    "refund_pay": ("возврат", "вернуть деньги", "верните", "оплат", "перевед", "переведу", "реквизит"),
}
DEBT_WORDS = ("долг", "должен", "карыз", "бонус", "баланс")
SHARED_NOTICE = "Я помощник магазина, позову продавца по просьбе."
NO_DATA = "Уточню у продавца и вернусь к вам."
HANDOFF_TEXT = "Передаю ваш вопрос продавцу, он скоро ответит."
REFUSE_PRIVATE = "Эти данные я могу показать только самому клиенту после привязки номера. Позову продавца."
HOURLY_LIMIT = 20
NUM = re.compile(r"\d[\d\s]*(?:[.,]\d+)?")


# ------------------------------------------------------------------ настройки канала

def validate_ai_settings(out: dict) -> dict:
    """Проверка ai_* настроек канала (вызывается из channels.validate_settings)."""
    topics = out.get("ai_topics_auto", ["price", "stock", "location", "hours", "address", "delivery"])
    if not isinstance(topics, list) or any(t not in TOPICS for t in topics):
        raise ApiError("invalid", "ai_topics_auto: список из " + ", ".join(TOPICS) + ".",
                       fields={"settings.ai_topics_auto": ["Недопустимое значение"]})
    out["ai_topics_auto"] = topics
    try:
        out["handoff_amount"] = int(out.get("handoff_amount", 10000))
        if out["handoff_amount"] < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ApiError("invalid", "handoff_amount: число ≥ 0.", fields={"settings.handoff_amount": ["Недопустимое значение"]})
    sched = out.get("ai_schedule", {"mode": "always"})
    if not isinstance(sched, dict) or sched.get("mode", "always") not in ("always", "outside_working_hours", "custom"):
        raise ApiError("invalid", "ai_schedule.mode: always, outside_working_hours или custom.",
                       fields={"settings.ai_schedule": ["Недопустимое значение"]})
    out["ai_schedule"] = sched
    try:
        out["ai_monthly_budget_tokens"] = int(out.get("ai_monthly_budget_tokens", 0) or 0)
    except (TypeError, ValueError):
        raise ApiError("invalid", "ai_monthly_budget_tokens: число.",
                       fields={"settings.ai_monthly_budget_tokens": ["Недопустимое значение"]})
    return out


def key_warning(channel):
    """Бесплатный общий ключ для переписки покупателей — с явным предупреждением владельцу (п. 7)."""
    if (channel.settings or {}).get("ai_mode", "off") == "off":
        return None
    try:
        from apps.main.telegram_bot.models import TelegramBotSettings
        bs = TelegramBotSettings.objects.filter(company_id=channel.company_id).first()
        own = bool(bs and bs.ai_key_set)
    except Exception:
        own = False
    return None if own else "shared_key"


def _hm(s):
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def in_working_hours(channel, now=None) -> bool:
    now = timezone.localtime(now or timezone.now())
    wh = (channel.settings or {}).get("working_hours") or {}
    if not wh:
        return True
    days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    rng = wh.get(days[now.weekday()], wh) if isinstance(wh, dict) else None
    if isinstance(rng, dict):
        a, b = _hm(rng.get("from")), _hm(rng.get("to"))
    elif isinstance(rng, (list, tuple)) and len(rng) == 2:
        a, b = _hm(rng[0]), _hm(rng[1])
    else:
        a = b = None
    if a is None or b is None:
        return True
    cur = now.hour * 60 + now.minute
    return a <= cur < b if a <= b else (cur >= a or cur < b)


def schedule_allows_auto(channel, now=None) -> bool:
    sched = (channel.settings or {}).get("ai_schedule") or {"mode": "always"}
    mode = sched.get("mode", "always")
    if mode == "always":
        return True
    if mode == "outside_working_hours":
        return not in_working_hours(channel, now)
    cur_dt = timezone.localtime(now or timezone.now())
    a, b, cur = _hm(sched.get("from")), _hm(sched.get("to")), cur_dt.hour * 60 + cur_dt.minute
    if a is None or b is None:
        return True
    return a <= cur < b if a <= b else (cur >= a or cur < b)


# ------------------------------------------------------------------ классификация

def detect_topic(text: str):
    t = (text or "").lower()
    for topic in ("order", "location", "hours", "address", "delivery", "stock", "price"):
        if any(w in t for w in TOPIC_WORDS[topic]):
            return topic
    return None


def handoff_reason(text: str, channel):
    t = (text or "").lower()
    for reason, words in HANDOFF_WORDS.items():
        if any(w in t for w in words):
            return reason
    limit = int((channel.settings or {}).get("handoff_amount", 10000) or 0)
    if limit and any(w in t for w in ("сом", "сумм", "руб")):
        for m in NUM.finditer(t):
            digits = re.sub(r"\s", "", m.group()).replace(",", ".")
            try:
                if Decimal(digits) > limit:
                    return "amount"
            except Exception:
                continue
    return None


def _debt_question(text):
    t = (text or "").lower()
    return any(w in t for w in DEBT_WORDS)


# ------------------------------------------------------------------ инструменты (только чтение)

def _show_qty(channel):
    return bool((channel.settings or {}).get("ai_show_stock_qty"))


def _product_row(p, show_qty):
    row = {"id": str(p.id), "name": p.name, "price": str(p.price), "unit": p.unit or "",
           "in_stock": (p.quantity or 0) > 0}
    if show_qty:
        row["quantity"] = str(p.quantity)
    variants = []
    for v in p.variants.filter(is_active=True)[:20]:
        r = {"size": v.size, "color": v.color, "price": str(v.price if v.price is not None else p.price),
             "in_stock": (v.quantity or 0) > 0}
        if show_qty:
            r["quantity"] = str(v.quantity)
        variants.append(r)
    if variants:
        row["variants"] = variants
    return row


def tool_catalog_search(company, channel, contact, args):
    from apps.main.ai_agent_api import search_products
    q = str(args.get("q") or "").strip()
    rows = [_product_row(p, _show_qty(channel)) for _sc, p in search_products(company, q, limit=5)] if q else []
    return {"results": rows}


def _product_by(company, ref):
    import uuid

    from apps.main.models import Product
    ref = str(ref or "").strip()
    qs = Product.objects.filter(company=company)
    try:
        p = qs.filter(pk=uuid.UUID(ref)).first()
    except (ValueError, AttributeError):
        p = None
    return p or qs.filter(name__iexact=ref).first() or qs.filter(barcode=ref).first()


def _branch(company, ref):
    if not ref:
        return None
    from apps.users.models import Branch
    try:
        return Branch.objects.filter(company=company, pk=ref).first()
    except Exception:
        return None


def tool_stock_get(company, channel, contact, args):
    p = _product_by(company, args.get("product"))
    if not p:
        return {"error": "product_not_found"}
    out = {"product": p.name, "in_stock": (p.quantity or 0) > 0}
    if _show_qty(channel):
        out["quantity"] = str(p.quantity)
    return out


def tool_location_get(company, channel, contact, args):
    p = _product_by(company, args.get("product"))
    if not p:
        return {"error": "product_not_found"}
    from apps.main.tz26.hw_catalog.models import ProductStorageLocation
    branch = _branch(company, args.get("branch")) or channel.branch
    qs = ProductStorageLocation.objects.filter(product=p)
    row = (qs.filter(branch=branch).first() if branch else None) or qs.filter(branch__isnull=True).first()
    return {"product": p.name, "location": row.location if row else None}


def tool_shop_info(company, channel, contact, args):
    out = {"name": company.name, "address": company.address or None, "phone": company.phone or None,
           "working_hours": (channel.settings or {}).get("working_hours") or None}
    try:
        from apps.main.showcase import services as svc
        cart = svc.published_document(company).get("cart") or {}
        out["delivery"] = cart.get("delivery")
        out["payment"] = cart.get("payment") or cart.get("payment_methods")
    except Exception:
        pass
    return out


def tool_client_own_summary(company, channel, contact, args):
    """Долг и бонусы — только для клиента, связанного с ЭТИМ контактом. Аргументов, выбирающих клиента, нет."""
    client = contact.client if contact.client_id else None
    if client is None:
        return {"error": "not_linked", "detail": "Контакт не связан с клиентом: данные недоступны."}
    from apps.main.ai_agent_api import client_debt_summary
    d = client_debt_summary(company, client)
    return {"client": "[Д1]", "debt": d.get("debt_total"), "next_due_date": d.get("next_due_date"),
            "bonus_balance": str(getattr(client, "bonus_balance", 0) or 0)}


TOOLS = {
    "catalog_search": (tool_catalog_search, "catalog.search", "Поиск товаров компании по названию: цена, варианты, наличие.",
                       {"q": {"type": "string", "description": "Название товара"}}, ["q"]),
    "stock_get": (tool_stock_get, "stock.get", "Наличие товара (есть/нет или количество).",
                  {"product": {"type": "string"}, "branch": {"type": "string"}}, ["product"]),
    "location_get": (tool_location_get, "location.get", "Ячейка хранения товара.",
                     {"product": {"type": "string"}, "branch": {"type": "string"}}, ["product"]),
    "shop_info": (tool_shop_info, "shop.info", "Адрес, часы работы, телефон, доставка, оплата.", {}, []),
    "client_own_summary": (tool_client_own_summary, "client.own_summary",
                           "Долг и бонусы текущего покупателя (только его собственные).", {}, []),
}


def tool_declarations():
    out = []
    for name, (_fn, _dotted, desc, props, req) in TOOLS.items():
        d = {"name": name, "description": desc}
        if props:
            d["parameters"] = {"type": "object", "properties": props, "required": req}
        out.append(d)
    return out


# ------------------------------------------------------------------ лимиты и бюджет

def _month_start(now=None):
    now = timezone.localtime(now or timezone.now())
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def check_limits(conv, channel):
    """20 вопросов в час с контакта; бюджет компании в месяц (токены)."""
    now = timezone.now()
    n = AiRun.objects.filter(conversation__contact_id=conv.contact_id, created_at__gte=now - timedelta(hours=1)
                             ).exclude(outcome="error").count()
    limit = int(getattr(dj_settings, "INBOX_AI_HOURLY_LIMIT", HOURLY_LIMIT))
    if n >= limit:
        raise ApiError("ai_rate_limited", "Слишком много вопросов ИИ с этого контакта за час.", status_code=429,
                       data={"limit": limit})
    budget = int((channel.settings or {}).get("ai_monthly_budget_tokens", 0) or 0)
    if budget:
        used = sum(AiRun.objects.filter(company_id=conv.company_id, created_at__gte=_month_start(now))
                   .values_list("tokens", flat=True))
        if used >= budget:
            exceed_budget(channel)
            raise ApiError("ai_budget_exceeded", "Месячный бюджет ИИ исчерпан, ИИ выключен.", status_code=402,
                           data={"budget_tokens": budget, "used_tokens": used})


def exceed_budget(channel):
    """ai_mode=off и уведомление владельцу."""
    s = dict(channel.settings or {})
    if s.get("ai_mode") == "off":
        return
    s["ai_mode"] = "off"
    channel.settings = s
    channel.save(update_fields=["settings", "updated_at"])
    notify_owner(channel.company_id, "⚠️ Месячный бюджет ИИ в «Чатах» исчерпан: ИИ-ответчик выключен.")


def notify_owner(company_id, text):
    try:
        from apps.main.telegram_bot.models import TelegramBotSettings
        from apps.main.telegram_bot.services import telegram_api
        bot = TelegramBotSettings.objects.filter(company_id=company_id).first()
        if bot and bot.owner_chat_id and bot.token:
            telegram_api.send_message(bot.token, bot.owner_chat_id, text, parse_mode=None)
    except Exception:
        logger.exception("inbox.ai: не удалось уведомить владельца")


# ------------------------------------------------------------------ запрос к модели

def _api_key(channel):
    """Ключ — как у серверного бота: ключ компании из его настроек, иначе общий ключ сервера."""
    from apps.main.telegram_bot.models import TelegramBotSettings
    from apps.main.telegram_bot.services import ai_service
    bs = TelegramBotSettings.objects.filter(company_id=channel.company_id).first()
    return ai_service.get_effective_ai_key(bs.ai_key if bs else None)


def _system_prompt(company):
    return (
        f"Ты ИИ-помощник магазина «{company.name}» в чате с покупателем. Имя покупателя — [Д1].\n"
        "ПРАВИЛА:\n"
        "1. Цены, наличие, адрес, часы — ТОЛЬКО из ответов инструментов. Цифры, которых нет в ответах инструментов, называть нельзя.\n"
        "2. Если данных нет — ответь ровно одним словом НЕТ_ДАННЫХ. Если не понял вопрос — ровно НЕ_ПОНЯЛ.\n"
        "3. Не обещай скидки, сроки и резерв товара. Не называй чужие данные, выручку, долги других людей.\n"
        "4. Посторонние темы (не про магазин) — вежливо откажись одной фразой.\n"
        "5. Отвечай кратко, на языке покупателя, без Markdown. Заказ сам не оформляй — предложи позвать продавца.\n"
        "6. Личные данные покупателя в ответах — только кодом [Д1]."
    )


def _numbers(s):
    """Числа из текста (≥ 2 цифр): «1 900» → 1900; дробную часть отдельным числом не считаем."""
    t = re.sub(r"(?<=\d)[  ](?=\d{3}(?!\d))", "", s or "")
    t = re.sub(r"(\d)[.,]\d+", r"\1", t)
    return {n for n in re.findall(r"\d+", t) if len(n) >= 2}


def _numbers_grounded(answer, question, tool_log):
    """Каждое число в ответе должно быть в вопросе или в результатах инструментов."""
    allowed = _numbers(question)
    for t in tool_log:
        allowed |= _numbers(str(t.get("result_full", "")))
    return _numbers(answer) <= allowed


def _history(conv, limit=8):
    rows = list(Message.objects.filter(conversation=conv).exclude(kind="note").order_by("-id")[:limit])
    out = []
    for m in reversed(rows):
        if not m.text:
            continue
        role = "user" if m.direction == "in" else "model"
        if out and out[-1]["role"] == role:
            out[-1]["parts"][0]["text"] += "\n" + m.text
        else:
            out.append({"role": role, "parts": [{"text": m.text}]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _mask(text, contact):
    name = (contact.display_name or "").strip()
    if name and len(name) >= 2:
        text = re.sub(re.escape(name), "[Д1]", text or "", flags=re.I)
    return text or ""


def _unmask(text, contact):
    return (text or "").replace("[Д1]", (contact.display_name or "").strip())


def _call_model(channel, system, contents, execute):
    from apps.main.telegram_bot.services import ai_service
    key = _api_key(channel)
    if not key:
        raise RuntimeError("Ключ ИИ не задан.")
    with ai_service.CompanyAiConcurrencyLimit(channel.company_id):
        return ai_service.generate_chat_response_with_tools(
            api_key=key, system_instruction=system, contents=contents, tools=tool_declarations(),
            execute_tool_fn=execute, temperature=0.2, max_tokens=500)


def generate(conv, question, *, mode) -> AiRun:
    """Один вопрос → AiRun (ответ не отправляется). mode: draft | auto."""
    channel, contact, company = conv.channel, conv.contact, conv.company
    check_limits(conv, channel)
    run = AiRun(conversation=conv, channel=channel, company=company, mode=mode,
                question=_mask(question, contact)[:2000])

    def finish(answer, outcome, reason="", needs_human=False):
        run.answer, run.outcome, run.handoff_reason = answer, outcome, reason
        if needs_human:
            from . import services
            services.set_needs_human(conv, True)
        run.save()
        return run

    topic = detect_topic(question)
    reason = handoff_reason(question, channel)
    if reason:
        return finish(HANDOFF_TEXT, "handoff", reason, True)
    if _debt_question(question) and not contact.client_id:
        return finish(REFUSE_PRIVATE, "refused", "private_data", True)
    if mode == "auto" and topic not in (channel.settings or {}).get("ai_topics_auto", []):
        return finish(HANDOFF_TEXT, "handoff", "topic_not_allowed", True)

    tool_log = []

    def execute(name, args):
        spec = TOOLS.get(name)
        if not spec:
            return {"error": "unknown_tool"}
        try:
            result = spec[0](company, channel, contact, args or {})
        except Exception:
            logger.exception("inbox.ai tool %s failed", name)
            result = {"error": "tool_failed"}
        tool_log.append({"name": spec[1], "args": args or {}, "result_summary": str(result)[:200],
                         "result_full": str(result)})
        return result

    def public_log():
        return [{k: v for k, v in t.items() if k != "result_full"} for t in tool_log]

    contents = _history(conv)
    q = _mask(question, contact)
    if not contents or contents[-1]["role"] != "user" or q not in contents[-1]["parts"][0]["text"]:
        if contents and contents[-1]["role"] == "user":
            contents[-1]["parts"][0]["text"] += "\n" + q
        else:
            contents.append({"role": "user", "parts": [{"text": q}]})
    system = _system_prompt(company)
    try:
        text, model, _called = _call_model(channel, system, contents, execute)
    except Exception as exc:
        logger.warning("inbox.ai: модель недоступна: %s", exc)
        run.tools = public_log()
        return finish("", "error", "model_error")
    run.model = (model or "")[:64]
    run.tools = public_log()
    run.tokens = max(1, (len(system) + sum(len(c["parts"][0].get("text", "")) for c in contents) + len(text or "")) // 4)
    run.cost = Decimal(str(getattr(dj_settings, "INBOX_AI_COST_PER_1K_TOKENS", 0))) * run.tokens / 1000
    text = (text or "").strip()
    if text.startswith("НЕТ_ДАННЫХ") or not text:
        return finish(NO_DATA, "handoff", "no_data", True)
    if text.startswith("НЕ_ПОНЯЛ"):
        prev = list(AiRun.objects.filter(conversation=conv).order_by("-id").values_list("outcome", flat=True)[:2])
        if len(prev) == 2 and all(o == "unclear" for o in prev):
            return finish(HANDOFF_TEXT, "handoff", "not_understood", True)
        return finish("Не совсем понял вопрос. Уточните, пожалуйста, какой товар или что именно вас интересует.",
                      "unclear")
    if not _numbers_grounded(text, question, tool_log):
        return finish(NO_DATA, "handoff", "low_confidence", True)
    answer = _unmask(text, contact)
    if mode == "auto" and not AiRun.objects.filter(conversation=conv, mode="auto", outcome="answered").exists():
        answer = f"{SHARED_NOTICE}\n\n{answer}"
    return finish(answer, "answered")


# ------------------------------------------------------------------ отправка

def window_check_for_ai(conv, now=None):
    """Instagram/WhatsApp: ИИ после окна 24 ч писать не может (window_closed)."""
    from .services import WINDOW_24H_KINDS
    now = now or timezone.now()
    if conv.channel.kind in WINDOW_24H_KINDS and (not conv.window_expires_at or conv.window_expires_at <= now):
        raise ApiError("window_closed", "Окно 24 ч закрыто: ИИ отвечать не может.",
                       data={"window_expires_at": conv.window_expires_at.isoformat() if conv.window_expires_at else None})


def mark_sent(run_id, conv, msg, user):
    """Черновик ИИ отправил сотрудник (ai_run_id в Б-75)."""
    try:
        AiRun.objects.filter(pk=run_id, conversation=conv).update(sent_by="user", sent_by_user=user, sent_message=msg)
    except Exception:
        logger.exception("inbox.ai: mark_sent")


def send_auto(conv, run):
    """Автоответ: сообщение sender=ai в очередь исходящих."""
    from . import services
    channel, contact = conv.channel, conv.contact
    window_check_for_ai(conv)
    if channel.status == "disabled" or contact.blocked or not contact.consent_service:
        return None
    msg = Message.objects.create(conversation=conv, channel=channel, company_id=conv.company_id, direction="out",
                                 sender="ai", kind="text", text=run.answer, status="queued")
    services._touch_conv(conv, msg)
    conv.waiting_since = None
    conv.save()
    AiRun.objects.filter(pk=run.pk).update(sent_by="auto", sent_message=msg)
    services.emit(conv, "inbox_message_created", message=msg.id, direction="out", sender="ai")
    services.enqueue_send(msg.id)
    return msg


def auto_reply(conv_id, message_id):
    """Вызывается после входящего (Celery). Возвращает строку-результат."""
    conv = (Conversation.objects.select_related("channel", "channel__bot_settings", "contact", "contact__client", "company")
            .filter(pk=conv_id).first())
    msg = Message.objects.filter(pk=message_id, conversation_id=conv_id, direction="in").first() if conv else None
    if not conv or not msg or not msg.text:
        return "skipped"
    channel = conv.channel
    if (channel.settings or {}).get("ai_mode") != "auto" or channel.bot_settings_id:
        return "off"  # серверный бот отвечает своим конвейером
    if conv.ai_paused_until and conv.ai_paused_until > timezone.now():
        return "paused"
    if channel.kind == "web_widget":
        from .models import WidgetSession
        if WidgetSession.objects.filter(contact_id=conv.contact_id, spam=True).exists():
            return "spam"  # подозрение на спам: ИИ не тратим
    if not schedule_allows_auto(channel):
        return "outside_schedule"
    try:
        window_check_for_ai(conv)
        run = generate(conv, msg.text, mode="auto")
    except ApiError as exc:
        return exc.code
    if run.outcome != "error" and run.answer:
        send_auto(conv, run)
    return run.outcome
