"""Ядро ящика: диалоги, входящие, статусы, исходящая очередь (Б-75, Б-76, Б-77)."""
import hashlib
import json
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.exceptions import ApiError

from . import access, contacts as contacts_svc
from .channels import touch_health
from .models import (
    OPEN_STATUSES, Channel, ChannelContact, Conversation, ConversationRead, Message, PendingStatus,
)

logger = logging.getLogger("inbox")

REOPEN_WINDOW = timedelta(hours=24)
WINDOW_24H_KINDS = ("whatsapp_cloud", "whatsapp_bsp", "instagram")
IG_HUMAN_AGENT_WINDOW = timedelta(days=7)
STATUS_RANK = {"queued": 0, "failed": 0.5, "sent": 1, "delivered": 2, "read": 3}
RETRY_DELAYS = (1, 5, 30, 120)  # секунды; после 4 повторов — failed


# ------------------------------------------------------------------ события (Б-77)

def emit(conv, type_, **data):
    """Событие в ленту изменений (apps.main.change_feed). Не бросает исключений."""
    try:
        from apps.main.change_feed import record_change
        payload = {"conversation": conv.id, "channel": str(conv.channel_id), "status": conv.status,
                   "assignee": str(conv.assignee_id) if conv.assignee_id else None}
        payload.update(data)
        record_change(conv.company_id, type_, branch_id=conv.branch_id, entity="conversation",
                      entity_id=conv.id, data=payload)
    except Exception:
        logger.exception("inbox emit failed: %s", type_)


# ------------------------------------------------------------------ диалоги

def _auto_assignee(channel):
    mode = (channel.settings or {}).get("auto_assign", "none")
    if mode == "none":
        return None
    from django.contrib.auth import get_user_model
    users = get_user_model().objects.filter(company_id=channel.company_id, is_active=True)
    if mode == "branch" and channel.branch_id:
        users = users.filter(branch_memberships__branch_id=channel.branch_id)
    users = [u for u in users[:200] if access.has(u, "can_reply_inbox")]
    if not users:
        return None
    load = dict(
        Conversation.objects.filter(channel__company_id=channel.company_id, assignee__in=users, status__in=OPEN_STATUSES)
        .values_list("assignee").annotate(n=Count("id"))
    )
    return min(users, key=lambda u: (load.get(u.id, 0), str(u.id)))


def open_conversation(contact, now=None):
    """Правило одного открытого диалога на (канал, контакт): Б-75 п. 6.
    Закрытый ≤ 24 ч назад — переоткрыть, раньше — новый."""
    now = now or timezone.now()
    channel = contact.channel
    conv = Conversation.objects.filter(channel=channel, contact=contact, status__in=OPEN_STATUSES).first()
    if conv:
        return conv, False
    last = Conversation.objects.filter(channel=channel, contact=contact, status="closed").order_by("-closed_at", "-id").first()
    if last and last.closed_at and now - last.closed_at <= REOPEN_WINDOW:
        last.status, last.closed_reason, last.closed_at = "open", "", None
        last.save(update_fields=["status", "closed_reason", "closed_at", "updated_at"])
        return last, False
    try:
        with transaction.atomic():
            conv = Conversation.objects.create(
                company_id=channel.company_id, channel=channel, contact=contact, branch_id=channel.branch_id,
                assignee=_auto_assignee(channel),
            )
        return conv, True
    except IntegrityError:
        return Conversation.objects.get(channel=channel, contact=contact, status__in=OPEN_STATUSES), False


def _brief(text, kind):
    t = (text or "").strip()
    return (t or f"[{kind}]")[:200]


def _touch_conv(conv, msg):
    conv.last_message_at = msg.created_at
    conv.last_message_text = _brief(msg.text, msg.kind)
    conv.last_message_direction = msg.direction
    conv.last_message_sender = msg.sender


# ------------------------------------------------------------------ входящие

def system_note(conv, text):
    """Системная строка в диалоге (не уходит наружу)."""
    msg = Message.objects.create(
        conversation=conv, channel_id=conv.channel_id, company_id=conv.company_id, direction="out",
        sender="system", kind="note", text=text, status="sent",
    )
    return msg


def ingest_inbound(channel, external_id, *, message_external_id="", text="", kind="text", attachments=None,
                   display_name="", username="", phone="", language="", start_payload="", created_at=None):
    """Входящее сообщение. Дубль по (channel, external_message_id) — не создаётся. Возвращает (message, created)."""
    if message_external_id and Message.objects.filter(channel=channel, external_message_id=message_external_id).exists():
        return Message.objects.get(channel=channel, external_message_id=message_external_id), False
    now = timezone.now()
    try:
        with transaction.atomic():
            contact, _ = contacts_svc.get_or_create_contact(
                channel, external_id, display_name=display_name, username=username, phone=phone,
                language=language, start_payload=start_payload)
            # блокировка контакта: сообщения одного диалога обрабатываются строго по очереди
            ChannelContact.objects.select_for_update().filter(pk=contact.pk).first()
            conv, conv_created = open_conversation(contact, now)
            msg = Message.objects.create(
                conversation=conv, channel=channel, company_id=channel.company_id, direction="in",
                sender="contact", kind=kind, text=text or "", attachments=attachments or [],
                status="delivered", external_message_id=message_external_id,
            )
            _touch_conv(conv, msg)
            conv.last_inbound_at = now
            conv.last_inbound_message_id = msg.id
            if conv.waiting_since is None:
                conv.waiting_since = now
            if channel.kind in WINDOW_24H_KINDS:
                conv.window_expires_at = now + REOPEN_WINDOW
            if conv.status in ("pending", "snoozed"):
                conv.status, conv.snoozed_until = "open", None
            conv.save()
            _apply_pending(channel, msg)
            _consent_commands(contact, conv, text)
    except IntegrityError:
        # гонка: то же сообщение уже записал другой воркер
        existing = Message.objects.filter(channel=channel, external_message_id=message_external_id).first()
        if existing:
            return existing, False
        raise
    touch_health(channel.id, inbound=True)
    emit(conv, "inbox_message_created", message=msg.id, direction="in", new_conversation=conv_created)
    if (channel.settings or {}).get("ai_mode") == "auto" and not channel.bot_settings_id and msg.text:
        try:
            from .tasks_ext import ai_auto_reply
            ai_auto_reply.delay(conv.id, msg.id)
        except Exception:
            logger.exception("inbox: не удалось поставить автоответ ИИ в очередь")
    try:
        from . import push
        push.notify_new_inbound(conv, msg)
    except Exception:
        logger.exception("inbox push failed")
    return msg, True


def _consent_commands(contact, conv, text):
    """/stop, СТОП, STOP → отказ от служебных и рекламных; /start возвращает служебные."""
    t = (text or "").strip().lower()
    if contacts_svc.is_stop(t):
        contacts_svc.set_consents(contact, service=False, marketing=False, source="customer")
        system_note(conv, "Покупатель отписался (СТОП): сообщения отключены.")
    elif t.startswith("/start") and not contact.consent_service:
        contacts_svc.set_consents(contact, service=True, source="customer")
        system_note(conv, "Покупатель снова подписался (/start).")


def record_bot_message(channel, external_id, text, *, sender="ai", message_external_id=""):
    """Ответ серверного бота/ИИ покупателю — в диалог (Б-76 п. 7). Статус sent, наружу не отправляется."""
    contact = ChannelContact.objects.filter(channel=channel, external_id=str(external_id)).first()
    if contact is None:
        return None
    now = timezone.now()
    with transaction.atomic():
        conv, _ = open_conversation(contact, now)
        msg = Message.objects.create(
            conversation=conv, channel=channel, company_id=channel.company_id, direction="out", sender=sender,
            kind="text", text=text or "", status="sent", sent_at=now, external_message_id=message_external_id,
        )
        _touch_conv(conv, msg)
        if sender == "ai":
            conv.waiting_since = None
        conv.save()
    touch_health(channel.id, outbound=True)
    emit(conv, "inbox_message_created", message=msg.id, direction="out", sender=sender)
    return msg


def ai_paused(channel, external_id) -> bool:
    """Бот/ИИ молчат ai_pause_minutes после ответа человека (Б-76 п. 7)."""
    conv = (Conversation.objects.filter(channel=channel, contact__external_id=str(external_id), status__in=OPEN_STATUSES)
            .only("ai_paused_until").first())
    return bool(conv and conv.ai_paused_until and conv.ai_paused_until > timezone.now())


# ------------------------------------------------------------------ статусы

def _apply_pending(channel, msg):
    if not msg.external_message_id:
        return
    for p in PendingStatus.objects.filter(channel=channel, external_message_id=msg.external_message_id).order_by("id"):
        _set_status(msg, p.status, p.error)
    PendingStatus.objects.filter(channel=channel, external_message_id=msg.external_message_id).delete()


def _set_status(msg, new, error=None) -> bool:
    cur = msg.status
    if new not in STATUS_RANK:
        return False
    if new == "failed":
        if STATUS_RANK[cur] >= STATUS_RANK["delivered"]:
            return False
    elif STATUS_RANK[new] <= STATUS_RANK[cur]:
        return False
    msg.status = new
    if new == "failed":
        msg.error = error or {"code": "channel_error", "detail": "Не доставлено"}
    elif cur == "failed":
        msg.error = None
    msg.save(update_fields=["status", "error"])
    return True


def apply_status(channel, external_message_id, status, error=None) -> str:
    """Статус от провайдера: монотонно sent < delivered < read; failed отдельно;
    статус раньше сообщения — сохраняем и применим, когда сообщение появится."""
    msg = Message.objects.filter(channel=channel, external_message_id=external_message_id).select_related("conversation").first()
    if msg is None:
        PendingStatus.objects.create(channel=channel, external_message_id=external_message_id, status=status, error=error)
        return "pending"
    if _set_status(msg, status, error):
        emit(msg.conversation, "inbox_message_status", message=msg.id, message_status=msg.status)
        return "applied"
    return "ignored"


# ------------------------------------------------------------------ видимость (Б-75 права)

def visible_conversations(user, company):
    qs = Conversation.objects.filter(company=company)
    if access.has(user, "can_view_all_conversations"):
        return qs
    branches = access.user_branch_ids(user)
    wrote = Message.objects.filter(conversation=OuterRef("pk"), sender_user=user)
    return qs.filter(
        Q(assignee=user)
        | Q(assignee__isnull=True, branch__isnull=True)
        | Q(assignee__isnull=True, branch_id__in=branches)
        | Q(Exists(wrote))
    )


def read_pointer(user):
    return Subquery(ConversationRead.objects.filter(conversation=OuterRef("pk"), user=user)
                    .values("last_read_message_id")[:1])


def with_unread(qs, user):
    return qs.annotate(_lr=Coalesce(read_pointer(user), 0)).annotate(
        _has_unread=Q(last_inbound_message_id__gt=F("_lr")))


def unread_counts(user, company) -> dict:
    qs = visible_conversations(user, company).exclude(status="closed")
    unread = qs.annotate(_lr=Coalesce(read_pointer(user), 0)).filter(last_inbound_message_id__gt=F("_lr"))
    return {
        "total": unread.count(),
        "mine": unread.filter(assignee=user).count(),
        "unassigned": unread.filter(assignee__isnull=True).count(),
        "needs_human": qs.filter(needs_human=True).count(),
    }


def page_unread(conv_ids, user) -> dict:
    lr = Subquery(ConversationRead.objects.filter(conversation=OuterRef("conversation_id"), user=user)
                  .values("last_read_message_id")[:1])
    rows = (Message.objects.filter(conversation_id__in=conv_ids, direction="in", sender="contact")
            .annotate(_lr=Coalesce(lr, 0)).filter(id__gt=F("_lr"))
            .values("conversation_id").annotate(n=Count("id")))
    return {r["conversation_id"]: r["n"] for r in rows}


def mark_read(conv, user):
    last = Message.objects.filter(conversation=conv).order_by("-id").values_list("id", flat=True).first() or 0
    row, _ = ConversationRead.objects.get_or_create(conversation=conv, user=user)
    if row.last_read_message_id < last:
        row.last_read_message_id = last
        row.save(update_fields=["last_read_message_id", "updated_at"])


# ------------------------------------------------------------------ исходящие

def _body_hash(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def create_outgoing(conv, user, data, idempotency_key=""):
    """Сообщение сотрудника в диалог (Б-75 п. 3). Возвращает (message, replayed)."""
    kind = data.get("kind") or "text"
    if kind not in ("text", "note", "file", "template"):
        raise ApiError("invalid", "kind: text, note, file или template.", fields={"kind": ["Недопустимое значение"]})
    body_hash = _body_hash({"c": conv.id, **{k: data.get(k) for k in ("kind", "text", "media_id", "template", "reply_to")}})
    if idempotency_key:
        prev = Message.objects.filter(company_id=conv.company_id, idempotency_key=idempotency_key).first()
        if prev:
            if prev.conversation_id != conv.id or prev.idempotency_hash != body_hash:
                raise ApiError("idempotency_conflict", "Этот Idempotency-Key уже использован для другого запроса.", status_code=409)
            return prev, True
    text = str(data.get("text") or "").strip()
    channel, contact = conv.channel, conv.contact
    if data.get("quick_reply_id") and kind == "text":
        from . import quick as quick_svc
        text = quick_svc.render_for_send(conv, data["quick_reply_id"], text).strip()
    attachments, template = [], None
    if kind == "file":
        from . import actions
        attachments = [actions.file_attachment(conv, data.get("media_id"))]
    elif kind == "template":
        from . import templates as tpl_svc
        template, text = tpl_svc.prepare_template_message(conv, data.get("template"))
    elif not text:
        raise ApiError("invalid", "Пустой текст.", fields={"text": ["Обязательное поле"]})
    now = timezone.now()
    human_agent = False
    if kind != "note":
        if channel.status == "disabled":
            raise ApiError("channel_error", "Канал выключен.", data={"channel": str(channel.id)})
        if contact.blocked:
            raise ApiError("contact_blocked", "Покупатель заблокировал бота или контакт заблокирован.")
        if not contact.consent_service:
            raise ApiError("consent_required", "Покупатель отказался от сообщений (СТОП). Пока он не напишет снова, отправка невозможна.")
        if channel.kind in WINDOW_24H_KINDS and kind != "template":
            expires = conv.window_expires_at
            if not expires or expires <= now:
                if channel.kind == "instagram" and conv.last_inbound_at and now - conv.last_inbound_at <= IG_HUMAN_AGENT_WINDOW:
                    human_agent = True  # 24 ч…7 дней: только человек, с тегом human_agent
                else:
                    raise ApiError("window_closed", "Окно 24 ч закрыто — выберите шаблон.",
                                   data={"window_expires_at": expires.isoformat() if expires else None})
    reply_to = None
    if data.get("reply_to"):
        reply_to = Message.objects.filter(conversation=conv, pk=data["reply_to"]).first()
        if not reply_to:
            raise ApiError("invalid", "reply_to не найден в этом диалоге.", fields={"reply_to": ["Нет такого сообщения"]})
    try:
        with transaction.atomic():
            msg = Message.objects.create(
                conversation=conv, channel=channel, company_id=conv.company_id, direction="out", sender="user",
                sender_user=user, kind=kind, text=text, reply_to=reply_to, human_agent=human_agent,
                attachments=attachments, template=template,
                status="sent" if kind == "note" else "queued", idempotency_key=idempotency_key,
                idempotency_hash=body_hash if idempotency_key else "",
            )
            _touch_conv(conv, msg)
            if kind != "note":
                conv.waiting_since = None
                conv.needs_human = False
                pause = int((channel.settings or {}).get("ai_pause_minutes", 30) or 0)
                conv.ai_paused_until = now + timedelta(minutes=pause) if pause else None
                if conv.assignee_id is None:
                    conv.assignee = user
                if conv.first_taken_at is None:
                    conv.first_taken_at = now
                if conv.status in ("pending", "snoozed"):
                    conv.status, conv.snoozed_until = "open", None
            conv.save()
    except IntegrityError:
        prev = Message.objects.filter(company_id=conv.company_id, idempotency_key=idempotency_key).first()
        if prev:
            return prev, True
        raise
    emit(conv, "inbox_message_created", message=msg.id, direction="out", sender="user")
    if kind != "note":
        transaction.on_commit(lambda: enqueue_send(msg.id))
    if data.get("ai_run_id"):
        from . import ai as ai_svc
        ai_svc.mark_sent(data["ai_run_id"], conv, msg, user)
    return msg, False


def enqueue_send(message_id, countdown=0):
    try:
        from .tasks import send_outbound
        send_outbound.apply_async(args=[message_id], countdown=countdown)
    except Exception:
        logger.exception("inbox: не удалось поставить отправку в очередь (message=%s); подберёт sweep_outbound", message_id)


# ------------------------------------------------------------------ действия над диалогом

def take(conv_id, user) -> Conversation:
    """Атомарное «взять себе»: побеждает один; второму — 409 already_assigned."""
    now = timezone.now()
    n = Conversation.objects.filter(pk=conv_id, assignee__isnull=True).update(
        assignee=user, first_taken_at=now, updated_at=now)
    conv = Conversation.objects.select_related("channel", "contact").get(pk=conv_id)
    if n == 0 and conv.assignee_id != user.id:
        raise ApiError("already_assigned", "Диалог уже взял другой сотрудник.", status_code=409,
                       data={"assignee": str(conv.assignee_id)})
    if n:
        emit(conv, "inbox_conversation_updated", change="assignee")
    return conv


def set_status(conv, status, snoozed_until=None, closed_reason=""):
    if status not in ("open", "pending", "snoozed", "closed"):
        raise ApiError("invalid", "status: open, pending, snoozed или closed.", fields={"status": ["Недопустимое значение"]})
    now = timezone.now()
    if status == "snoozed":
        if not snoozed_until or snoozed_until <= now:
            raise ApiError("invalid", "Для snoozed нужна будущая дата snoozed_until.", fields={"snoozed_until": ["Укажите будущую дату"]})
        conv.snoozed_until = snoozed_until
    else:
        conv.snoozed_until = None
    if status == "closed":
        reason = closed_reason or "other"
        if reason not in ("sold", "no_answer", "spam", "other"):
            raise ApiError("invalid", "closed_reason: sold, no_answer, spam или other.", fields={"closed_reason": ["Недопустимое значение"]})
        conv.closed_reason, conv.closed_at, conv.waiting_since = reason, now, None
    else:
        conv.closed_reason, conv.closed_at = "", None
    conv.status = status
    try:
        with transaction.atomic():
            conv.save()
    except IntegrityError:
        raise ApiError("open_conversation_exists", "С этим контактом уже есть открытый диалог.", status_code=409)
    emit(conv, "inbox_conversation_updated", change="status")


def set_needs_human(conv, value=True):
    """Метка «нужен человек» (ставит ИИ/бот); событие inbox.needs_human."""
    if conv.needs_human != value:
        conv.needs_human = value
        conv.save(update_fields=["needs_human", "updated_at"])
        if value:
            emit(conv, "inbox_needs_human")
