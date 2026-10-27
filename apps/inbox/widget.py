"""Б-83: чат на витрине. Сеансы, сообщения посетителя, long-poll, контакт."""
import hashlib
import re
import secrets
import time
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.exceptions import ApiError

from . import contacts as contacts_svc
from . import services
from .models import Channel, ChannelContact, Message, WidgetSession, WidgetSettings

SESSION_TTL = timedelta(days=30)
MSG_PER_MIN = 20
SESSIONS_PER_IP_MIN = 5
MAX_TEXT = 2000
LANGS = ("ru", "ky", "en", "tr", "uz")
URL_RE = re.compile(r"(https?://|www\.)", re.I)


class RateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def session_by_token(token):
    """Сеанс по токену посетителя (не истёкший) или None."""
    if not token or len(token) < 20:
        return None
    s = (WidgetSession.objects.select_related("contact", "channel", "company")
         .filter(token_hash=_hash(token)).first())
    if s is None or s.expires_at <= timezone.now():
        return None
    return s


def settings_for(company) -> WidgetSettings:
    obj, _ = WidgetSettings.objects.get_or_create(company=company)
    return obj


def settings_out(w: WidgetSettings) -> dict:
    return {"enabled": w.enabled, "greeting": w.greeting or {}, "placement": w.placement or [],
            "offer_telegram": w.offer_telegram, "offer_whatsapp": w.offer_whatsapp,
            "collect_phone": w.collect_phone, "consent_text": w.consent_text or {}}


def apply_settings(w: WidgetSettings, data: dict):
    if "enabled" in data:
        w.enabled = bool(data["enabled"])
    for key in ("greeting", "consent_text"):
        if key in data:
            v = data[key]
            if not isinstance(v, dict) or any(k not in LANGS or not isinstance(x, str) or len(x) > 500 for k, x in v.items()):
                raise ApiError("invalid", f"{key}: объект {{ru,ky,en,tr,uz}} со строками до 500 символов.",
                               fields={key: ["Недопустимое значение"]})
            setattr(w, key, v)
    if "placement" in data:
        v = data["placement"]
        if not isinstance(v, list) or any(x not in ("all", "product", "cart") for x in v):
            raise ApiError("invalid", "placement: all, product, cart.", fields={"placement": ["Недопустимое значение"]})
        w.placement = v
    for key in ("offer_telegram", "offer_whatsapp"):
        if key in data:
            setattr(w, key, bool(data[key]))
    if "collect_phone" in data:
        if data["collect_phone"] not in ("off", "optional", "required"):
            raise ApiError("invalid", "collect_phone: off, optional, required.",
                           fields={"collect_phone": ["Недопустимое значение"]})
        w.collect_phone = data["collect_phone"]
    w.save()


def widget_channel(company) -> Channel:
    ch = Channel.objects.filter(company=company, kind=Channel.Kind.WEB_WIDGET).first()
    if ch is None:
        ch = Channel.objects.create(company=company, kind=Channel.Kind.WEB_WIDGET, provider=Channel.Provider.OWN,
                                    name="Чат на витрине", status=Channel.Status.ACTIVE,
                                    settings={"ai_mode": "off"})
    return ch


def require_enabled(company) -> WidgetSettings:
    w = settings_for(company)
    if not w.enabled:
        raise ApiError("widget_disabled", "Чат на витрине выключен.", status_code=404)
    return w


# ------------------------------------------------------------------ сеансы

def create_session(company, ip, page="") -> tuple:
    """Новый сеанс; 5 сеансов в минуту с IP. Возвращает (session, token)."""
    now = timezone.now()
    if ip:
        recent = list(WidgetSession.objects.filter(ip=ip, created_at__gte=now - timedelta(minutes=1))
                      .order_by("created_at").values_list("created_at", flat=True))
        if len(recent) >= SESSIONS_PER_IP_MIN:
            wait = int((recent[0] + timedelta(minutes=1) - now).total_seconds()) + 1
            raise RateLimited(max(wait, 1))
    channel = widget_channel(company)
    token = secrets.token_urlsafe(32)  # 256 бит
    sid = uuid.uuid4()
    with transaction.atomic():
        contact, _ = contacts_svc.get_or_create_contact(channel, f"v_{sid.hex}", display_name="Посетитель витрины")
        s = WidgetSession.objects.create(id=sid, company=company, channel=channel, contact=contact,
                                         token_hash=_hash(token), expires_at=now + SESSION_TTL, ip=ip or "",
                                         page=(page or "")[:300])
    return s, token


def _check_msg_rate(session):
    now = timezone.now()
    recent = list(Message.objects.filter(conversation__contact_id=session.contact_id, direction="in",
                                         created_at__gte=now - timedelta(minutes=1))
                  .order_by("created_at").values_list("created_at", flat=True))
    if len(recent) >= MSG_PER_MIN:
        raise RateLimited(max(int((recent[0] + timedelta(minutes=1) - now).total_seconds()) + 1, 1))


def _suspect_spam(session, text) -> bool:
    if len(URL_RE.findall(text)) >= 3:
        return True
    same = Message.objects.filter(conversation__contact_id=session.contact_id, direction="in", text=text,
                                  created_at__gte=timezone.now() - timedelta(minutes=10)).count()
    return same >= 2


def post_message(session, text, client_msg_id) -> tuple:
    """Сообщение посетителя; дубль по client_msg_id возвращает прежнее (created=False)."""
    text = (text or "").strip()
    cmid = str(client_msg_id or "").strip()
    if not cmid or len(cmid) > 64:
        raise ApiError("invalid", "client_msg_id: обязательная строка до 64 символов.",
                       fields={"client_msg_id": ["Обязательное поле"]})
    if not text:
        raise ApiError("invalid", "Пустое сообщение.", fields={"text": ["Обязательное поле"]})
    if len(text) > MAX_TEXT:
        raise ApiError("invalid", f"Сообщение длиннее {MAX_TEXT} символов.", fields={"text": ["Слишком длинное"]})
    ext = f"w:{session.id.hex}:{cmid}"
    dup = Message.objects.filter(channel=session.channel, external_message_id=ext).first()
    if dup:
        return dup, False
    _check_msg_rate(session)
    if not session.spam and _suspect_spam(session, text):
        WidgetSession.objects.filter(pk=session.pk).update(spam=True)
        session.spam = True
    msg, created = services.ingest_inbound(session.channel, session.contact.external_id, message_external_id=ext,
                                           text=text, display_name=session.contact.display_name)
    if session.spam and created:
        conv = msg.conversation
        if conv.needs_human:
            conv.needs_human = False
            conv.save(update_fields=["needs_human", "updated_at"])
    return msg, created


def visitor_message_out(m) -> dict:
    """Что видит посетитель: без имён сотрудников, токенов и служебных строк."""
    role = "visitor" if m.direction == "in" else ("assistant" if m.sender == "ai" else "seller")
    return {"id": m.id, "sender": role, "text": m.text, "kind": m.kind, "created_at": m.created_at.isoformat()}


def visible_messages(session, after=0, limit=50):
    qs = (Message.objects.filter(conversation__contact_id=session.contact_id, id__gt=after)
          .exclude(kind="note").exclude(sender="system").exclude(status="failed").order_by("id")[:limit])
    return list(qs)


def poll(session, after, wait_seconds):
    """Long-poll: ждём новых сообщений до wait_seconds (≤ 25 с)."""
    deadline = time.monotonic() + min(max(wait_seconds, 0), 25)
    while True:
        rows = visible_messages(session, after)
        if rows or time.monotonic() >= deadline:
            return rows
        time.sleep(1)


def save_contact(session, name, phone, consent, w: WidgetSettings):
    contact = session.contact
    phone = (phone or "").strip()
    if phone and consent is not True:
        raise ApiError("consent_required", "Нужно согласие на обработку номера (consent: true).",
                       fields={"consent": ["Обязательно при указании телефона"]})
    if w.collect_phone == "required" and not phone and not contact.phone:
        raise ApiError("invalid", "Укажите телефон.", fields={"phone": ["Обязательное поле"]})
    changed = []
    if name and name.strip():
        contact.display_name = name.strip()[:255]
        changed.append("display_name")
    if phone:
        contact.phone = (contacts_svc.normalize_phone(phone) or phone)[:32]
        changed.append("phone")
    if changed:
        contact.save(update_fields=changed)
    if phone:
        contacts_svc.set_consents(contact, service=True, source="widget")
        contacts_svc.auto_link(contact)  # телефон → автосвязь с клиентом (Б-74)
    return contact


def telegram_link(session, w):
    """t.me/<бот>?start=w_<токен>. Токен ссылки отдельный (не visitor_token): он безопасен для публикации."""
    if not w.offer_telegram:
        return None
    ch = Channel.objects.filter(company_id=session.company_id, kind=Channel.Kind.TELEGRAM_BOT).first()
    bot = ((ch.external or {}).get("bot_username") if ch else None) or ""
    if not bot:
        return None
    if not session.link_token:
        session.link_token = secrets.token_urlsafe(24)
        session.save(update_fields=["link_token"])
    return f"https://t.me/{bot}?start=w_{session.link_token}"
