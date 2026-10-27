"""Б-81: рассылки и напоминания о долге. Согласия, тихие часы, не чаще раза в 20 ч на получателя,
скорость по ступени WhatsApp, каждое сообщение — в диалоге получателя."""
import logging
import re
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from core.exceptions import ApiError

from . import channels as ch_svc
from . import services, templates as tpl
from .models import Campaign, CampaignRecipient, ChannelContact, Conversation, Message, WaTemplate

logger = logging.getLogger("inbox")
KINDS = ("promo", "service", "debt_reminder")
CHANNEL_KINDS = ("telegram_bot", "telegram_business", "whatsapp_cloud", "whatsapp_bsp")
QUIET_DEFAULT = {"from": "21:00", "to": "09:00"}
MIN_GAP = timedelta(hours=20)
TIER_LIMITS = {"TIER_50": 50, "TIER_250": 250, "TIER_1K": 1000, "TIER_10K": 10000, "TIER_100K": 100000}
VAR_RE = re.compile(r"\{([^{}]+)\}")
DEBT_PARAMS = ["{имя}", "{долг}", "{срок}"]


# ------------------------------------------------------------------ тихие часы

def _hm(s, default):
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default


def _quiet(c):
    q = c.quiet_hours if c.quiet_hours is not None else {}
    if q.get("enabled") is False:
        return None
    return _hm(q.get("from", QUIET_DEFAULT["from"]), 21 * 60), _hm(q.get("to", QUIET_DEFAULT["to"]), 9 * 60)


def in_quiet(c, dt) -> bool:
    qh = _quiet(c)
    if qh is None:
        return False
    a, b = qh
    cur = timezone.localtime(dt)
    m = cur.hour * 60 + cur.minute
    return (a <= m < b) if a <= b else (m >= a or m < b)


def next_allowed(c, dt):
    """Ближайший момент вне тихих часов (не раньше dt)."""
    if not in_quiet(c, dt):
        return dt
    _a, b = _quiet(c)
    cur = timezone.localtime(dt)
    cand = cur.replace(hour=b // 60, minute=b % 60, second=0, microsecond=0)
    if cand <= cur:
        cand += timedelta(days=1)
    return cand


def start_time(c, now):
    return next_allowed(c, max(c.scheduled_at or now, now))


# ------------------------------------------------------------------ валидация и представление

def validate(company, data, current=None) -> dict:
    out = {}
    if current is None or "channel" in data:
        ch = ch_svc.Channel.objects.filter(company=company, pk=data.get("channel")).first() if data.get("channel") else None
        if ch is None:
            raise ApiError("invalid", "Укажите канал компании.", fields={"channel": ["Канал не найден"]})
        ch_svc.ensure_channel_allowed_for_broadcast(ch)  # неофициальный → 400 channel_not_allowed
        if ch.kind not in CHANNEL_KINDS:
            raise ApiError("channel_not_allowed", "Рассылки возможны в Telegram и WhatsApp (официальные каналы).",
                           data={"reason": "kind_not_supported", "channel_kind": ch.kind})
        out["channel"] = ch
    ch = out.get("channel") or current.channel
    if "kind" in data or current is None:
        kind = data.get("kind") or "promo"
        if kind not in KINDS:
            raise ApiError("invalid", "kind: promo, service или debt_reminder.", fields={"kind": ["Недопустимое значение"]})
        out["kind"] = kind
    kind = out.get("kind") or current.kind
    for key in ("name", "text"):
        if key in data:
            out[key] = str(data[key] or "")[:4000 if key == "text" else 128]
    if "segment" in data:
        seg = data["segment"] or {}
        if not isinstance(seg, dict) or set(seg) - {"bought_since", "debtors", "label", "branch", "client_ids"}:
            raise ApiError("invalid", "segment: bought_since, debtors, label, branch, client_ids.",
                           fields={"segment": ["Недопустимое значение"]})
        out["segment"] = seg
    if "quiet_hours" in data:
        q = data["quiet_hours"] or {}
        if not isinstance(q, dict):
            raise ApiError("invalid", "quiet_hours: {from, to}.", fields={"quiet_hours": ["Недопустимое значение"]})
        out["quiet_hours"] = q
    if "scheduled_at" in data:
        from django.utils.dateparse import parse_datetime
        dt = parse_datetime(str(data["scheduled_at"])) if data["scheduled_at"] else None
        if data["scheduled_at"] and not dt:
            raise ApiError("invalid", "scheduled_at: дата ISO 8601.", fields={"scheduled_at": ["Неверная дата"]})
        if dt and timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        out["scheduled_at"] = dt
    if "template_params" in data:
        if not isinstance(data["template_params"], list):
            raise ApiError("invalid", "template_params: массив.", fields={"template_params": ["Ожидается массив"]})
        out["template_params"] = [str(x) for x in data["template_params"]]
    if ch.kind in tpl.WA_KINDS:
        if "template_id" in data or current is None:
            t = WaTemplate.objects.filter(channel=ch, pk=data.get("template_id")).first() if data.get("template_id") else None
            if t is None:
                raise ApiError("invalid", "Для WhatsApp нужен template_id одобренного шаблона.",
                               fields={"template_id": ["Обязательное поле"]})
            if t.status != "approved":
                raise ApiError("template_not_approved", "Шаблон ещё не одобрен.", data={"status": t.status})
            want = "marketing" if kind == "promo" else "utility"
            if kind == "promo" and t.category != "marketing" or kind != "promo" and t.category == "marketing":
                raise ApiError("invalid", f"Для рассылки kind={kind} нужен шаблон категории {want}.",
                               fields={"template_id": ["Не та категория шаблона"]})
            out["template"] = t
    elif (current is None or "text" in data) and not (out.get("text") or (current and current.text)):
        raise ApiError("invalid", "Для Telegram нужен text.", fields={"text": ["Обязательное поле"]})
    if kind == "debt_reminder" and ch.kind in tpl.WA_KINDS and "template_params" not in data and not (current and current.template_params):
        out["template_params"] = list(DEBT_PARAMS)
    return out


def campaign_out(c) -> dict:
    return {"id": c.id, "name": c.name, "kind": c.kind, "channel": str(c.channel_id),
            "template_id": c.template_id, "template_params": c.template_params, "text": c.text,
            "segment": c.segment or {}, "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
            "quiet_hours": c.quiet_hours or QUIET_DEFAULT, "status": c.status, "stats": stats(c),
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "finished_at": c.finished_at.isoformat() if c.finished_at else None, "created_at": c.created_at.isoformat()}


# ------------------------------------------------------------------ аудитория

def _debtor_client_ids(company):
    from apps.main.kassa_views import _open_debts
    from apps.main.models import Client
    return {row[0] for row in _open_debts([company.id], Client.objects.filter(company=company).values("id")) if row[2] > 0}


def segment_contacts(c):
    seg = dict(c.segment or {})
    qs = ChannelContact.objects.filter(channel=c.channel).select_related("client")
    if seg.get("client_ids"):
        qs = qs.filter(client_id__in=seg["client_ids"])
    if seg.get("debtors") or c.kind == "debt_reminder":
        qs = qs.filter(client_id__in=_debtor_client_ids(c.company))
    if seg.get("bought_since"):
        from apps.main.models import Sale
        qs = qs.filter(client_id__in=Sale.objects.filter(company=c.company, created_at__date__gte=seg["bought_since"],
                                                         client__isnull=False).exclude(status="canceled").values("client_id"))
    if seg.get("label"):
        lab = seg["label"]
        cq = Conversation.objects.filter(company=c.company, **({"labels__id": lab} if str(lab).isdigit() else {"labels__name": lab}))
        qs = qs.filter(id__in=cq.values("contact_id"))
    if seg.get("branch"):
        qs = qs.filter(id__in=Conversation.objects.filter(company=c.company, branch_id=seg["branch"]).values("contact_id"))
    return qs.distinct()


def classify(c, contact, now):
    """None — можно слать, иначе причина исключения."""
    if contact.blocked:
        return "blocked"
    ok = contact.consent_marketing if c.kind == "promo" else contact.consent_service
    if not ok:
        stopped = not contact.consent_service and not contact.consent_marketing and contact.consents_source == "customer"
        return "stopped" if stopped else "no_consent"
    recent = CampaignRecipient.objects.filter(contact=contact, status__in=("sent", "queued"), sent_at__gte=now - MIN_GAP)
    if recent.exclude(campaign=c).exists() or recent.filter(campaign=c).exists():
        return "recent_20h"
    return None


def audience(c, now=None):
    now = now or timezone.now()
    eligible, excluded = [], {"no_consent": 0, "stopped": 0, "blocked": 0, "recent_20h": 0}
    for contact in segment_contacts(c):
        why = classify(c, contact, now)
        if why:
            excluded[why] += 1
        else:
            eligible.append(contact)
    return eligible, excluded


def preview(c, now=None) -> dict:
    now = now or timezone.now()
    eligible, excluded = audience(c, now)
    start = start_time(c, now)
    deferred = in_quiet(c, max(c.scheduled_at or now, now))
    cost = "0.00"
    if c.template_id:
        cost = tpl.estimate_cost(c.template.category, len(eligible), c.company)
    return {"targeted": len(eligible) + sum(excluded.values()), "recipients": len(eligible),
            "excluded": {**excluded, "quiet_hours": len(eligible) if deferred else 0},
            "starts_at": start.isoformat(), "quiet_hours_deferred": deferred,
            "cost_estimate": {"amount": cost, "currency": "USD"} if c.template_id else None}


# ------------------------------------------------------------------ запуск

def start(c, user=None):
    if c.status not in ("draft",):
        raise ApiError("invalid_status", "Запустить можно только черновик.", status_code=409, data={"status": c.status})
    ch_svc.ensure_channel_allowed_for_broadcast(c.channel)
    if c.channel.status in ("disabled", "error"):
        raise ApiError("channel_error", "Канал выключен или с ошибкой.", data={"status": c.channel.status})
    eligible, _ = audience(c)
    if not eligible:
        raise ApiError("no_recipients", "Нет получателей с согласием.")
    c.status = "scheduled"
    c.save(update_fields=["status"])
    return c


def _values(company, contact):
    client = contact.client if contact.client_id else None
    vals = {"имя": (client.full_name if client else contact.display_name) or "", "магазин": company.name or "",
            "долг": "", "срок": "", "номер_заказа": "", "сумма": ""}
    if client:
        from apps.main.ai_agent_api import client_debt_summary
        d = client_debt_summary(company, client)
        vals["долг"], vals["срок"] = d.get("debt_total") or "", d.get("next_due_date") or ""
    return vals


def _render(text, vals):
    return VAR_RE.sub(lambda m: vals.get(m.group(1), m.group(0)), text or "")


def _snapshot(c, now):
    for contact in segment_contacts(c):
        why = classify(c, contact, now)
        try:
            with transaction.atomic():
                CampaignRecipient.objects.create(
                    campaign=c, contact=contact, client_id=contact.client_id, dedupe_key=f"{c.id}:{contact.id}",
                    status="skipped" if why else "queued", skip_reason=why or "")
        except IntegrityError:
            continue


def _tier_room(c, now):
    cap = TIER_LIMITS.get(((c.channel.health or {}).get("messaging_tier") or "").upper())
    if cap is None:
        return None
    used = CampaignRecipient.objects.filter(campaign__channel=c.channel, status="sent", sent_at__gte=now - timedelta(hours=24)).count()
    return max(cap - used, 0)


def send_one(c, r, now):
    contact = r.contact
    why = classify_for_send(c, contact, r, now)
    if why:
        r.status, r.skip_reason = "skipped", why
        r.save(update_fields=["status", "skip_reason"])
        return False
    conv, _ = services.open_conversation(contact, now)
    vals = _values(c.company, contact)
    if c.template_id:
        params = [_render(p, vals) for p in (c.template_params or [])]
        text = tpl.body_text(c.template.components)
        for i, p in enumerate(params, 1):
            text = text.replace("{{%d}}" % i, p)
        kind = "template"
        template = {"name": c.template.name, "language": c.template.language, "params": params,
                    "category": c.template.category}
    else:
        kind, text, template = "text", _render(c.text, vals), None
    msg = Message.objects.create(conversation=conv, channel=c.channel, company=c.company, direction="out", sender="user",
                                 sender_user=c.created_by, kind=kind, text=text, template=template, status="queued")
    services._touch_conv(conv, msg)
    conv.save()
    r.message, r.status, r.sent_at = msg, "sent", now
    r.save(update_fields=["message", "status", "sent_at"])
    services.emit(conv, "inbox_message_created", message=msg.id, direction="out", sender="user", campaign=c.id)
    transaction.on_commit(lambda: services.enqueue_send(msg.id))
    return True


def classify_for_send(c, contact, r, now):
    contact.refresh_from_db()
    if contact.blocked:
        return "blocked"
    ok = contact.consent_marketing if c.kind == "promo" else contact.consent_service
    if not ok:
        return "no_consent"
    if CampaignRecipient.objects.filter(contact=contact, status="sent", sent_at__gte=now - MIN_GAP).exclude(pk=r.pk).exists():
        return "recent_20h"
    return None


def process(c, now=None, batch=200) -> int:
    """Один шаг обработки рассылки; возвращает число отправленных сообщений."""
    now = now or timezone.now()
    c.refresh_from_db()
    if c.status not in ("scheduled", "sending") or now < start_time(c, now):
        return 0
    try:
        ch_svc.ensure_channel_allowed_for_broadcast(c.channel)
    except ApiError:
        c.status = "canceled"
        c.save(update_fields=["status"])
        return 0
    if c.status == "scheduled":
        c.status, c.started_at = "sending", now
        c.save(update_fields=["status", "started_at"])
        _snapshot(c, now)
    if in_quiet(c, now):
        return 0  # тихие часы: продолжим утром
    room = _tier_room(c, now)
    sent = 0
    for r in c.recipients.filter(status="queued").select_related("contact", "contact__client")[:batch]:
        if room is not None and sent >= room:
            break
        with transaction.atomic():
            if send_one(c, r, now):
                sent += 1
    if not c.recipients.filter(status="queued").exists():
        c.status, c.finished_at = "done", now
        c.save(update_fields=["status", "finished_at"])
    return sent


def cancel(c):
    if c.status in ("done", "canceled"):
        raise ApiError("invalid_status", "Рассылка уже завершена.", status_code=409, data={"status": c.status})
    c.status, c.finished_at = "canceled", timezone.now()
    c.save(update_fields=["status", "finished_at"])
    c.recipients.filter(status="queued").update(status="skipped", skip_reason="canceled")


def stats(c) -> dict:
    rows = c.recipients.exclude(status__in=("skipped",)).select_related("message")
    replied = c.recipients.filter(message__isnull=False).filter(Exists(
        Message.objects.filter(conversation_id=OuterRef("message__conversation_id"), direction="in",
                               id__gt=OuterRef("message_id")))).count()
    sent = delivered = read = failed = 0
    for r in rows:
        ms = r.message.status if r.message_id else ""
        if ms == "failed" or r.status == "failed":
            failed += 1
        elif r.status == "sent":
            sent += 1
            delivered += ms in ("delivered", "read")
            read += ms == "read"
    cost = tpl.estimate_cost(c.template.category, len(eligible), c.company)
    return {"targeted": len(eligible) + sum(excluded.values()), "recipients": len(eligible),
            "excluded": {**excluded, "quiet_hours": len(eligible) if deferred else 0},
            "starts_at": start.isoformat(), "quiet_hours_deferred": deferred,
            "cost_estimate": {"amount": cost, "currency": "USD"} if c.template_id else None}


# ------------------------------------------------------------------ запуск

def start(c, user=None):
    if c.status not in ("draft",):
        raise ApiError("invalid_status", "Запустить можно только черновик.", status_code=409, data={"status": c.status})
    ch_svc.ensure_channel_allowed_for_broadcast(c.channel)
    if c.channel.status in ("disabled", "error"):
        raise ApiError("channel_error", "Канал выключен или с ошибкой.", data={"status": c.channel.status})
    eligible, _ = audience(c)
    if not eligible:
        raise ApiError("no_recipients", "Нет получателей с согласием.")
    c.status = "scheduled"
    c.save(update_fields=["status"])
    return c


def _values(company, contact):
    client = contact.client if contact.client_id else None
    vals = {"имя": (client.full_name if client else contact.display_name) or "", "магазин": company.name or "",
            "долг": "", "срок": "", "номер_заказа": "", "сумма": ""}
    if client:
        from apps.main.ai_agent_api import client_debt_summary
        d = client_debt_summary(company, client)
        vals["долг"], vals["срок"] = d.get("debt_total") or "", d.get("next_due_date") or ""
    return vals


def _render(text, vals):
    return VAR_RE.sub(lambda m: vals.get(m.group(1), m.group(0)), text or "")


def _snapshot(c, now):
    for contact in segment_contacts(c):
        why = classify(c, contact, now)
        try:
            with transaction.atomic():
                CampaignRecipient.objects.create(
                    campaign=c, contact=contact, client_id=contact.client_id, dedupe_key=f"{c.id}:{contact.id}",
                    status="skipped" if why else "queued", skip_reason=why or "")
        except IntegrityError:
            continue


def _tier_room(c, now):
    cap = TIER_LIMITS.get(((c.channel.health or {}).get("messaging_tier") or "").upper())
    if cap is None:
        return None
    used = CampaignRecipient.objects.filter(campaign__channel=c.channel, status="sent", sent_at__gte=now - timedelta(hours=24)).count()
    return max(cap - used, 0)


def send_one(c, r, now):
    contact = r.contact
    why = classify_for_send(c, contact, r, now)
    if why:
        r.status, r.skip_reason = "skipped", why
        r.save(update_fields=["status", "skip_reason"])
        return False
    conv, _ = services.open_conversation(contact, now)
    vals = _values(c.company, contact)
    if c.template_id:
        params = [_render(p, vals) for p in (c.template_params or [])]
        text = tpl.body_text(c.template.components)
        for i, p in enumerate(params, 1):
            text = text.replace("{{%d}}" % i, p)
        kind = "template"
        template = {"name": c.template.name, "language": c.template.language, "params": params,
                    "category": c.template.category}
    else:
        kind, text, template = "text", _render(c.text, vals), None
    msg = Message.objects.create(conversation=conv, channel=c.channel, company=c.company, direction="out", sender="user",
                                 sender_user=c.created_by, kind=kind, text=text, template=template, status="queued")
    services._touch_conv(conv, msg)
    conv.save()
    r.message, r.status, r.sent_at = msg, "sent", now
    r.save(update_fields=["message", "status", "sent_at"])
    services.emit(conv, "inbox_message_created", message=msg.id, direction="out", sender="user", campaign=c.id)
    transaction.on_commit(lambda: services.enqueue_send(msg.id))
    return True


def classify_for_send(c, contact, r, now):
    contact.refresh_from_db()
    if contact.blocked:
        return "blocked"
    ok = contact.consent_marketing if c.kind == "promo" else contact.consent_service
    if not ok:
        return "no_consent"
    if CampaignRecipient.objects.filter(contact=contact, status="sent", sent_at__gte=now - MIN_GAP).exclude(pk=r.pk).exists():
        return "recent_20h"
    return None


def process(c, now=None, batch=200) -> int:
    """Один шаг обработки рассылки; возвращает число отправленных сообщений."""
    now = now or timezone.now()
    c.refresh_from_db()
    if c.status not in ("scheduled", "sending") or now < start_time(c, now):
        return 0
    try:
        ch_svc.ensure_channel_allowed_for_broadcast(c.channel)
    except ApiError:
        c.status = "canceled"
        c.save(update_fields=["status"])
        return 0
    if c.status == "scheduled":
        c.status, c.started_at = "sending", now
        c.save(update_fields=["status", "started_at"])
        _snapshot(c, now)
    if in_quiet(c, now):
        return 0  # тихие часы: продолжим утром
    room = _tier_room(c, now)
    sent = 0
    for r in c.recipients.filter(status="queued").select_related("contact", "contact__client")[:batch]:
        if room is not None and sent >= room:
            break
        with transaction.atomic():
            if send_one(c, r, now):
                sent += 1
    if not c.recipients.filter(status="queued").exists():
        c.status, c.finished_at = "done", now
        c.save(update_fields=["status", "finished_at"])
    return sent


def cancel(c):
    if c.status in ("done", "canceled"):
        raise ApiError("invalid_status", "Рассылка уже завершена.", status_code=409, data={"status": c.status})
    c.status, c.finished_at = "canceled", timezone.now()
    c.save(update_fields=["status", "finished_at"])
    c.recipients.filter(status="queued").update(status="skipped", skip_reason="canceled")


def stats(c) -> dict:
    rows = c.recipients.exclude(status__in=("skipped",)).select_related("message")
    replied = c.recipients.filter(message__isnull=False).filter(Exists(
        Message.objects.filter(conversation_id=OuterRef("message__conversation_id"), direction="in",
                               id__gt=OuterRef("message_id")))).count()
    sent = delivered = read = failed = 0
    for r in rows:
        st = r.message.status if r.message_id else r.status
        if st == "failed":
            failed += 1
        elif st in ("sent", "delivered", "read"):
            sent += 1
            delivered += st in ("delivered", "read")
            read += st == "read"
    cost = tpl.estimate_cost(c.template.category, sent, c.company) if c.template_id else "0.00"
    return {"targeted": rows.count(), "sent": sent, "delivered": delivered, "read": read,
            "replied": replied, "failed": failed, "skipped": c.recipients.filter(status="skipped").count(),
            "cost_estimate": cost}
