"""Отправка исходящих в WhatsApp и Instagram (Б-78, Б-85, Б-86). Telegram — в tasks.send_outbound."""
import re

from . import crypto, meta_api
from .models import WaTemplate


def _phone(contact) -> str:
    return re.sub(r"\D", "", contact.external_id or contact.phone or "")


def whatsapp_payload(msg, contact):
    base = {"messaging_product": "whatsapp", "to": _phone(contact)}
    if msg.kind == "template":
        t = msg.template or {}
        tpl = WaTemplate.objects.filter(channel_id=msg.channel_id, name=t.get("name"), language=t.get("language")).first()
        comps = []
        params = t.get("params") or []
        if params:
            comps.append({"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]})
        return {**base, "type": "template", "template": {"name": t.get("name"), "language": {"code": t.get("language")},
                                                         "components": comps}}
    if msg.kind == "file":
        att = (msg.attachments or [{}])[0]
        link = att.get("url") or ""
        return {**base, "type": "document", "document": {"link": link, "filename": att.get("name") or "file",
                                                         "caption": msg.text or ""}}
    payload = {**base, "type": "text", "text": {"body": msg.text, "preview_url": False}}
    if msg.reply_to_id and msg.reply_to and msg.reply_to.external_message_id:
        payload["context"] = {"message_id": msg.reply_to.external_message_id}
    return payload


def send_whatsapp(msg, contact):
    """Возвращает external id сообщения (wamid)."""
    channel = msg.channel
    sec = crypto.get_secrets(channel)
    if channel.provider == "360dialog":
        provider, token = "360dialog", sec.get("api_key", "")
    else:
        provider, token = "meta", sec.get("access_token", "")
    if not token:
        raise meta_api.MetaError("У канала нет токена доступа.", permanent=True)
    res = meta_api.send_whatsapp(provider, (channel.external or {}).get("phone_number_id", ""), token,
                                 whatsapp_payload(msg, contact))
    ids = res.get("messages") or []
    return (ids[0].get("id") if ids else "") or ""


def send_instagram(msg, contact):
    channel = msg.channel
    token = crypto.get_secrets(channel).get("access_token", "")
    if not token:
        raise meta_api.MetaError("У канала нет токена доступа.", permanent=True)
    payload = {"recipient": {"id": contact.external_id}, "message": {"text": msg.text}}
    if msg.human_agent:
        payload["messaging_type"] = "MESSAGE_TAG"
        payload["tag"] = "HUMAN_AGENT"
    res = meta_api.ig_send(token, payload)
    return res.get("message_id") or ""
