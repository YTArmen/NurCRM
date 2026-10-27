"""Представление диалогов и сообщений для API (Б-75)."""
from . import access


def user_brief(u):
    if u is None:
        return None
    name = f"{getattr(u, 'first_name', '') or ''} {getattr(u, 'last_name', '') or ''}".strip() or getattr(u, "email", "")
    return {"id": str(u.id), "name": name}


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) <= 5:
        return "***"
    return f"+{digits[:3]}{'*' * (len(digits) - 5)}{digits[-2:]}"


def message_out(m) -> dict:
    return {
        "id": m.id,
        "direction": m.direction,
        "sender": m.sender,
        "sender_user": user_brief(m.sender_user) if m.sender_user_id else None,
        "kind": m.kind,
        "text": m.text,
        "attachments": m.attachments or [],
        "template": m.template,
        "reply_to": m.reply_to_id,
        "status": m.status,
        "error": m.error,
        "human_agent": m.human_agent,
        "created_at": m.created_at.isoformat(),
    }


def conversation_out(c, user, unread=0, see_phone=None) -> dict:
    if see_phone is None:
        see_phone = access.can_see_phone(user)
    contact = c.contact
    phone = contact.phone or ""
    client = contact.client if contact.client_id else None
    return {
        "id": c.id,
        "channel": {"id": str(c.channel_id), "kind": c.channel.kind, "name": c.channel.name},
        "contact": {
            "id": contact.id, "display_name": contact.display_name, "username": contact.username or None,
            "phone_masked": (phone if see_phone else mask_phone(phone)) or None,
        },
        "client": {"id": str(client.id), "full_name": client.full_name} if client else None,
        "status": c.status,
        "snoozed_until": c.snoozed_until.isoformat() if c.snoozed_until else None,
        "closed_reason": c.closed_reason or None,
        "assignee": user_brief(c.assignee) if c.assignee_id else None,
        "labels": [{"id": l.id, "name": l.name, "color": l.color} for l in c.labels.all()],
        "branch": str(c.branch_id) if c.branch_id else None,
        "last_message": {
            "text": c.last_message_text, "direction": c.last_message_direction or None,
            "sender": c.last_message_sender or None,
            "created_at": c.last_message_at.isoformat() if c.last_message_at else None,
        } if c.last_message_at else None,
        "unread_count": unread,
        "waiting_since": c.waiting_since.isoformat() if c.waiting_since else None,
        "window_expires_at": c.window_expires_at.isoformat() if c.window_expires_at else None,
        "needs_human": c.needs_human,
        "deal": str(c.deal_id) if c.deal_id else None,
        "order": str(c.order_id) if c.order_id else None,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }
