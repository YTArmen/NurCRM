"""Б-78: быстрые ответы и подстановка переменных."""
import re

from core.exceptions import ApiError

from . import access
from .models import QuickReply

LANGS = ("ru", "ky", "en", "tr", "uz")
VARIABLES = ("имя", "магазин", "долг", "срок", "номер_заказа", "сумма")
VAR_RE = re.compile(r"\{([^{}]+)\}")


def validate(data, partial=False) -> dict:
    out = {}
    if "shortcut" in data or not partial:
        sc = str(data.get("shortcut") or "").strip().lstrip("/")[:32]
        if not sc:
            raise ApiError("invalid", "Укажите shortcut.", fields={"shortcut": ["Обязательное поле"]})
        out["shortcut"] = sc
    if "title" in data:
        out["title"] = str(data["title"] or "")[:128]
    if "text" in data or not partial:
        text = data.get("text")
        if isinstance(text, str):
            text = {"ru": text}
        if not isinstance(text, dict) or not text or any(k not in LANGS or not isinstance(v, str) for k, v in text.items()):
            raise ApiError("invalid", "text: объект {ru,ky,en,tr,uz} со строками.", fields={"text": ["Недопустимое значение"]})
        if any(len(v) > 2000 for v in text.values()):
            raise ApiError("invalid", "Текст длиннее 2000 символов.", fields={"text": ["Слишком длинный"]})
        out["text"] = text
        used = sorted({v for t in text.values() for v in VAR_RE.findall(t)})
        unknown = [v for v in used if v not in VARIABLES]
        if unknown:
            raise ApiError("invalid", f"Неизвестная переменная: {{{unknown[0]}}}.", fields={"text": [f"Неизвестная переменная {unknown[0]}"]})
        out["variables"] = used
    return out


def out(q) -> dict:
    return {"id": q.id, "shortcut": q.shortcut, "title": q.title, "text": q.text, "variables": q.variables,
            "shared": q.owner_id is None}


def visible(user, company):
    from django.db.models import Q
    return QuickReply.objects.filter(company=company).filter(Q(owner__isnull=True) | Q(owner=user))


def can_edit(user, q) -> bool:
    return access.is_owner_like(user) if q.owner_id is None else q.owner_id == user.id or access.is_owner_like(user)


def _values(conv):
    contact = conv.contact
    client = contact.client if contact.client_id else None
    vals = {"имя": (client.full_name if client else contact.display_name) or "",
            "магазин": conv.company.name or "", "долг": "", "срок": "", "номер_заказа": "", "сумма": ""}
    if client:
        from apps.main.ai_agent_api import client_debt_summary
        d = client_debt_summary(conv.company, client)
        vals["долг"] = d.get("debt_total") or ""
        vals["срок"] = d.get("next_due_date") or ""
    if conv.order_id:
        vals["номер_заказа"] = str(conv.order.number)
        vals["сумма"] = str(conv.order.total)
    return vals


def render(q, conv, lang=None) -> str:
    texts = q.text or {}
    lang = lang or conv.contact.language or "ru"
    text = texts.get(lang) or texts.get("ru") or next(iter(texts.values()), "")
    vals = _values(conv)
    return VAR_RE.sub(lambda m: vals.get(m.group(1), m.group(0)), text)


def render_for_send(conv, quick_reply_id, text):
    """Подстановка переменных при отправке с quick_reply_id; явно переданный text главнее."""
    if text:
        return text
    q = QuickReply.objects.filter(pk=quick_reply_id, company_id=conv.company_id).first()
    if q is None:
        raise ApiError("invalid", "Быстрый ответ не найден.", fields={"quick_reply_id": ["Нет такого ответа"]})
    return render(q, conv)
