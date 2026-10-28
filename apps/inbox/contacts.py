"""Контакты каналов: автосвязь с клиентом, согласия, /stop (Б-74)."""
import re
import uuid as uuidlib

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.main.phone_utils import normalize_phone_e164

from .models import Channel, ChannelContact

STOP_WORDS = {"/stop", "стоп", "stop"}
WIDGET_START = re.compile(r"^w_([A-Za-z0-9_-]{20,40})")
START_LABEL = re.compile(r"^s_([0-9a-fA-F-]{32,36})")


def normalize_phone(raw) -> str:
    """0700… → +996700…; wa_id (996700…) → +996700…"""
    return normalize_phone_e164(raw)


def is_stop(text) -> bool:
    return (text or "").strip().lower() in STOP_WORDS


def get_or_create_contact(channel, external_id, *, display_name="", username="", phone="", language="",
                          start_payload=""):
    """Контакт канала (channel, external_id). Новый контакт — сразу пытаемся связать с клиентом."""
    external_id = str(external_id)
    contact = ChannelContact.objects.filter(channel=channel, external_id=external_id).first()
    created = False
    if contact is None:
        try:
            with transaction.atomic():
                contact = ChannelContact.objects.create(
                    channel=channel, company_id=channel.company_id, external_id=external_id,
                    display_name=(display_name or "")[:255], username=(username or "")[:128],
                    phone=(phone or "")[:32], language=(language or "ru")[:8],
                )
            created = True
        except IntegrityError:
            contact = ChannelContact.objects.get(channel=channel, external_id=external_id)
    changed = []
    for field, val in (("display_name", display_name), ("username", username)):
        if val and getattr(contact, field) != val[:255]:
            setattr(contact, field, val[:255])
            changed.append(field)
    if phone and contact.phone != phone[:32]:
        contact.phone = phone[:32]
        changed.append("phone")
    if changed:
        contact.save(update_fields=changed)
    if not contact.client_id and (created or start_payload or phone):
        auto_link(contact, start_payload=start_payload)
    return contact, created


def auto_link(contact, start_payload=""):
    """Метка start=s_<id> → telegram_chat_id → нормализованный телефон. Несколько совпадений — не связываем."""
    from apps.main.models import Client

    if contact.client_id:
        return contact.client
    clients = Client.objects.filter(company_id=contact.company_id)
    m = START_LABEL.match((start_payload or "").strip())
    if m:
        try:
            cid = uuidlib.UUID(m.group(1))
        except ValueError:
            cid = None
        found = clients.filter(id=cid).first() if cid else None
        if found:
            link(contact, found)
            return found
    wm = WIDGET_START.match((start_payload or "").strip())
    if wm:  # «Продолжить в Telegram» из чата витрины (Б-83 п. 5)
        from .models import WidgetSession
        ws = WidgetSession.objects.filter(link_token=wm.group(1)).select_related("contact").first()
        if ws and ws.company_id == contact.company_id:
            if ws.contact.client_id:
                link(contact, ws.contact.client)
                return ws.contact.client
            if ws.contact.phone and not contact.phone:
                contact.phone = ws.contact.phone
                contact.save(update_fields=["phone"])
    kind = contact.channel.kind
    if kind.startswith("telegram"):
        found = list(clients.filter(telegram_chat_id=contact.external_id)[:3])
        if len(found) == 1:
            link(contact, found[0])
            return found[0]
    phone = normalize_phone(contact.phone or (contact.external_id if kind.startswith("whatsapp") else ""))
    if phone:
        found = list(clients.filter(phone_normalized=phone)[:6])
        if len(found) == 1:
            link(contact, found[0])
            return found[0]
        if len(found) > 1:
            contact.client_candidates = [{"id": str(c.id), "full_name": c.full_name} for c in found]
            contact.save(update_fields=["client_candidates"])
    return None


def link(contact, client):
    """Связать контакт с клиентом; Telegram → записать Client.telegram_chat_id, если пусто."""
    contact.client = client
    contact.client_candidates = []
    contact.save(update_fields=["client", "client_candidates"])
    kind = contact.channel.kind
    upd = []
    if kind.startswith("telegram") and not getattr(client, "telegram_chat_id", None):
        client.telegram_chat_id = contact.external_id
        upd.append("telegram_chat_id")
    elif kind.startswith("whatsapp") and not getattr(client, "whatsapp_phone", None) and contact.phone:
        client.whatsapp_phone = contact.phone
        upd.append("whatsapp_phone")
    elif kind == Channel.Kind.INSTAGRAM and not getattr(client, "instagram_username", None) and contact.username:
        client.instagram_username = contact.username
        upd.append("instagram_username")
    if upd:
        client.save(update_fields=upd)


def unlink(contact):
    contact.client = None
    contact.save(update_fields=["client"])


def set_consents(contact, *, service=None, marketing=None, source):
    if service is not None:
        contact.consent_service = bool(service)
    if marketing is not None:
        contact.consent_marketing = bool(marketing)
    contact.consents_updated_at = timezone.now()
    contact.consents_source = source
    contact.save(update_fields=["consent_service", "consent_marketing", "consents_updated_at", "consents_source"])


def serialize_contact(c, with_candidates=True) -> dict:
    out = {
        "id": c.id,
        "channel": str(c.channel_id),
        "external_id": c.external_id,
        "phone": c.phone or None,
        "username": c.username or None,
        "display_name": c.display_name,
        "avatar_url": c.avatar_url or None,
        "client": {"id": str(c.client_id), "full_name": c.client.full_name} if c.client_id else None,
        "language": c.language,
        "consents": {
            "service": c.consent_service, "marketing": c.consent_marketing,
            "updated_at": c.consents_updated_at.isoformat() if c.consents_updated_at else None,
            "source": c.consents_source or None,
        },
        "blocked": c.blocked,
        "created_at": c.created_at.isoformat(),
    }
    if with_candidates:
        out["client_candidates"] = c.client_candidates or []
    return out
