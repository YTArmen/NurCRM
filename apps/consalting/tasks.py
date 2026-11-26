"""Celery-сканы воронки (Фаза 5): риск бездействия, просроченные задачи, SLA."""
import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

from .models import (
    LeadConsalting, LeadTaskConsalting, AutomationRuleConsalting,
    FunnelStageConsalting, SubscriptionConsalting, SubscriptionPaymentConsalting,
)
from .funnel.events import emit
from .funnel.state_machine import ACTIVE_TYPES

CLOSED_STATUSES = (LeadConsalting.Status.WON, LeadConsalting.Status.LOST)


@shared_task
def process_wazzup_webhook(payload):
    """Обработка вебхука Wazzup вне HTTP-запроса (совместимый режим)."""
    from .funnel.wazzup import WazzupConsaltingService
    WazzupConsaltingService.handle_wazzup_webhook(payload)


@shared_task
def notify_inbound_message(lead_id, owner_id=None, text="", phone=""):
    """Системные уведомления о входящем сообщении — вне горячего пути вебхука.

    Если у лида есть владелец — уведомляем только его; иначе рассылаем всем
    сотрудникам компании (это и есть дорогая часть: N вставок + N WS-рассылок).
    """
    from .models import LeadConsalting
    from .funnel import realtime
    from apps.users.models import User
    from apps.main.realtime import create_and_publish_notification

    lead = LeadConsalting.objects.select_related("company").filter(id=lead_id).first()
    if not lead:
        return

    title = f"📩 Сообщение от лида: {lead.full_name}"
    body = text or "Входящее медиасообщение"
    common = dict(
        company=lead.company,
        title=title,
        message=body,
        type="lead_message",
        level="info",
        url=f"/consalting/leads/{lead.id}",
        data={"lead_id": str(lead.id), "phone": phone},
    )

    owner = User.objects.filter(id=owner_id).first() if owner_id else None
    if owner:
        try:
            create_and_publish_notification(user=owner, **common)
        except Exception as e:
            logger.warning("notify_inbound_message: owner notify failed: %s", e)
        try:
            realtime.notify_user(
                owner.id,
                "lead.message_received",
                {
                    "id": str(lead.id),
                    "title": title,
                    "message": body,
                    "full_name": lead.full_name,
                    "phone": lead.phone,
                    "lead_id": str(lead.id),
                    "created_at": timezone.now().isoformat(),
                },
            )
        except Exception:
            pass
        return

    for u in User.objects.filter(company=lead.company, is_active=True).iterator():
        try:
            create_and_publish_notification(user=u, **common)
        except Exception as e:
            logger.warning("notify_inbound_message: notify user %s failed: %s", u.id, e)


@shared_task
def outbound_bookkeeping(wa_message_id, user_id=None, channel_id=""):
    """Побочный учёт исходящего сообщения — вне критического пути отправки.

    Лента активности, перевод входящей заявки и лида в «в работе», обновление
    карточки канбана. Ничего из этого не нужно, чтобы чат мгновенно показал
    сообщение, поэтому выполняется фоном (иначе задерживало ack на десятки мс).
    """
    from .models import (
        WhatsAppMessageConsalting, InboundLeadConsalting, LeadActivityConsalting,
    )
    from .funnel.activity import ActivityLogger
    from .funnel import realtime
    from apps.users.models import User

    wa_message = (
        WhatsAppMessageConsalting.objects.select_related("lead").filter(id=wa_message_id).first()
    )
    if not wa_message or not wa_message.lead:
        return
    lead = wa_message.lead
    actor = User.objects.filter(id=user_id).first() if user_id else None

    try:
        ActivityLogger.log(
            lead=lead,
            activity_type=LeadActivityConsalting.Type.MESSAGE,
            actor=actor,
            title="Wazzup (исходящее)",
            body=wa_message.text,
            payload={
                "direction": "outbound",
                "message_id": wa_message.message_id,
                "status": wa_message.status,
                "channel_id": channel_id,
            },
        )
    except Exception as e:
        logger.warning("outbound_bookkeeping: activity log failed: %s", e)

    clean_phone = "".join(filter(str.isdigit, lead.phone or ""))
    clean_phone_10 = clean_phone[-10:] if len(clean_phone) >= 10 else clean_phone
    if clean_phone_10:
        inbound_lead = InboundLeadConsalting.objects.filter(
            company_id=lead.company_id, phone__icontains=clean_phone_10
        ).exclude(
            status__in=[InboundLeadConsalting.Status.CONVERTED, InboundLeadConsalting.Status.REJECTED]
        ).first()
        if inbound_lead and inbound_lead.status in [
            InboundLeadConsalting.Status.NEW, InboundLeadConsalting.Status.ASSIGNED
        ]:
            inbound_lead.status = InboundLeadConsalting.Status.IN_WORK
            inbound_lead.save(update_fields=["status", "updated_at"])

    if lead.status in ["new", "NEW"]:
        lead.status = "in_work"
        lead.save(update_fields=["status", "updated_at"])

    try:
        realtime.lead_updated(lead)
    except Exception:
        pass


@shared_task(bind=True, acks_late=True, max_retries=3, default_retry_delay=5)
def send_wazzup_message(self, wa_message_id, account_id, text, content_uri, media_type="", content_type=""):
    """Реальная отправка исходящего сообщения в Wazzup API вне HTTP-запроса.

    Гарантии:
      * идемпотентность — при повторной доставке (acks_late) уже обработанное
        сообщение (не PENDING) пропускается, второй раз в Wazzup не уходит;
      * статус ВСЕГДА разрешается в SENT/FAILED и транслируется как
        ``message_status`` — сообщение не остаётся вечно PENDING;
      * сетевые сбои и 5xx ретраятся, 4xx — сразу FAILED;
      * валидный payload — Wazzup требует непустой text ЛИБО contentUri
        (иначе 400 INVALID_MESSAGE_DATA).
    """
    import requests
    from .models import WhatsAppMessageConsalting, WazzupAccountConsalting
    from .funnel.wazzup import WazzupConsaltingService, _broadcast_message_status
    from .funnel import realtime

    S = WhatsAppMessageConsalting.Status
    wa_message = (
        WhatsAppMessageConsalting.objects.select_related("lead")
        .filter(id=wa_message_id)
        .first()
    )
    if not wa_message:
        logger.warning("send_wazzup_message: message %s no longer exists", wa_message_id)
        return

    # Идемпотентность: повторная доставка задачи после уже обработанного сообщения
    if wa_message.status != S.PENDING:
        logger.info("send_wazzup_message: %s already %s, skip", wa_message_id, wa_message.status)
        return

    account = WazzupAccountConsalting.objects.filter(id=account_id).first()
    lead = wa_message.lead
    clean_phone = "".join(filter(str.isdigit, (lead.phone if lead else "") or ""))

    def _finalize(status):
        wa_message.status = status
        update_fields = ["message_id", "status"]
        if hasattr(wa_message, "provider"):
            update_fields.append("provider")
        wa_message.save(update_fields=update_fields)
        cid = account.company_id if account else wa_message.company_id
        try:
            _broadcast_message_status(cid, wa_message, clean_phone)
        except Exception as e:
            logger.warning("broadcast message_status failed: %s", e)
        if wa_message.lead_id:
            try:
                realtime.lead_updated(wa_message.lead)
            except Exception:
                pass

    def _try_green_api_fallback(reason_str=""):
        """Попытка резервной отправки через GREEN-API, если Wazzup не сработал."""
        if not account or not getattr(account, "green_api_enabled", True):
            logger.info("GreenAPI fallback skipped: disabled for account %s", account_id)
            _finalize(S.FAILED)
            return False

        id_inst = (getattr(account, "green_api_id_instance", "") or "").strip()
        tok_inst = (getattr(account, "green_api_token_instance", "") or "").strip()
        if not id_inst or not tok_inst:
            logger.warning("GreenAPI fallback skipped: credentials unconfigured for account %s", account_id)
            _finalize(S.FAILED)
            return False

        base_url = (getattr(account, "green_api_url", "") or "https://api.greenapi.com").rstrip('/')
        media_base_url = (getattr(account, "green_api_media_url", "") or base_url).rstrip('/')

        formatted_phone = clean_phone
        if not formatted_phone.endswith('@c.us') and not formatted_phone.endswith('@g.us'):
            if formatted_phone.startswith('0') and len(formatted_phone) == 10:
                formatted_phone = '996' + formatted_phone[1:]
            elif len(formatted_phone) == 9:
                formatted_phone = '996' + formatted_phone
            chat_id = f"{formatted_phone}@c.us"
        else:
            chat_id = formatted_phone

        logger.info(
            "Attempting GreenAPI fallback for msg %s (reason: %s, instance: %s)",
            wa_message_id, reason_str, id_inst
        )

        try:
            if content_uri:
                g_url = f"{media_base_url}/waInstance{id_inst}/sendFileByUrl/{tok_inst}"
                filename = content_uri.split("/")[-1].split("?")[0] or "file"
                g_payload = {
                    "chatId": chat_id,
                    "urlFile": content_uri,
                    "fileName": filename,
                }
                if body:
                    g_payload["caption"] = body
            else:
                g_url = f"{base_url}/waInstance{id_inst}/sendMessage/{tok_inst}"
                g_payload = {
                    "chatId": chat_id,
                    "message": body,
                }

            g_res = requests.post(g_url, json=g_payload, timeout=12.0)
            if g_res.status_code == 200:
                g_data = g_res.json() if g_res.content else {}
                id_msg = g_data.get("idMessage") or g_data.get("id")
                if id_msg:
                    wa_message.message_id = str(id_msg)
                if hasattr(wa_message, "provider"):
                    wa_message.provider = "greenapi"
                logger.info("GreenAPI fallback SUCCESS for msg %s: idMessage=%s", wa_message_id, id_msg)
                _finalize(S.SENT)
                return True
            else:
                logger.error("GreenAPI fallback HTTP %s for msg %s: %s", g_res.status_code, wa_message_id, g_res.text)
                _finalize(S.FAILED)
                return False
        except Exception as ge:
            logger.error("GreenAPI fallback exception for msg %s: %s", wa_message_id, ge)
            _finalize(S.FAILED)
            return False

    # Предусловия: без аккаунта/телефона/содержимого отправить нельзя → FAILED
    body = (text or "").strip()
    if not clean_phone or (not body and not content_uri):
        logger.error("send_wazzup_message: bad preconditions (phone/body) for %s → FAILED", wa_message_id)
        _finalize(S.FAILED)
        return

    # Пробуем Wazzup если api_url и api_key присутствуют
    wazzup_ok = False
    if account and getattr(account, "api_url", None) and getattr(account, "api_key", None):
        api_payload = {
            "channelId": account.channel_id,
            "chatId": clean_phone,
            "chatType": account.integration_type,
        }
        # Wazzup v3 explicitly rejects requests containing both text and
        # contentUri.  A media caption is persisted in CRM but is not part of
        # this API request.
        if body and not content_uri:
            api_payload["text"] = body
        if content_uri:
            api_payload["contentUri"] = content_uri
        api_payload["crmMessageId"] = str(wa_message.id)

        url = f"{account.api_url.rstrip('/')}/v3/message"
        headers = {
            "Authorization": f"Bearer {account.api_key}",
            "Content-Type": "application/json",
        }

        try:
            res = requests.post(url, json=api_payload, headers=headers, timeout=10.0)
            if res.status_code in (200, 201):
                data = res.json() if res.content else {}
                wz_id = data.get("messageId") or data.get("id")
                if wz_id:
                    wa_message.message_id = str(wz_id)
                if hasattr(wa_message, "provider"):
                    wa_message.provider = "wazzup"
                _finalize(S.SENT)
                try:
                    WazzupConsaltingService.mark_chat_read(account, clean_phone)
                except Exception as e:
                    logger.warning("mark_chat_read failed: %s", e)
                wazzup_ok = True
            else:
                logger.warning("Wazzup HTTP %s for msg %s: %s", res.status_code, wa_message_id, res.text)
        except Exception as e:
            logger.warning("Wazzup request failed for msg %s: %s", wa_message_id, e)

    if not wazzup_ok:
        _try_green_api_fallback(reason_str="Wazzup failed or unconfigured")


def _active_open_leads():
    return LeadConsalting.objects.filter(
        stage__stage_type__in=ACTIVE_TYPES, closed_at__isnull=True,
    ).exclude(status__in=CLOSED_STATUSES)


@shared_task
def scan_no_activity():
    """Лиды без активности дольше порога → событие no_activity (движок решает действия).

    Грубый floor=24ч отбирает кандидатов дёшево; точный порог каждого правила
    (`conditions.hours`) проверяется в conditions.match внутри движка.
    """
    now = timezone.now()
    fired = 0
    company_ids = list(
        AutomationRuleConsalting.objects.filter(
            is_active=True, trigger=AutomationRuleConsalting.Trigger.NO_ACTIVITY
        ).values_list("company_id", flat=True).distinct()
    )
    if not company_ids:
        return 0

    floor = now - timedelta(hours=24)
    qs = _active_open_leads().filter(company_id__in=company_ids).filter(
        Q(last_activity_at__lt=floor) | Q(last_activity_at__isnull=True, created_at__lt=floor)
    ).iterator()

    for lead in qs:
        emit("no_activity", lead)
        fired += 1
    return fired


@shared_task
def scan_overdue_tasks():
    """Открытые задачи с истёкшим сроком → статус OVERDUE + событие task_overdue."""
    now = timezone.now()
    fired = 0
    overdue = LeadTaskConsalting.objects.filter(
        status=LeadTaskConsalting.Status.OPEN, due_date__lt=now
    ).select_related("lead").iterator()
    for task in overdue:
        LeadTaskConsalting.objects.filter(pk=task.pk).update(
            status=LeadTaskConsalting.Status.OVERDUE
        )
        emit("task_overdue", task.lead, task_id=str(task.id))
        fired += 1
    return fired


@shared_task
def scan_sla_breach():
    """Лиды, превысившие SLA текущей стадии → событие sla_breach."""
    now = timezone.now()
    fired = 0
    stages = {
        s.id: s.sla_hours
        for s in FunnelStageConsalting.objects.filter(sla_hours__isnull=False)
    }
    if not stages:
        return 0
    qs = _active_open_leads().filter(
        stage_id__in=list(stages.keys()), stage_entered_at__isnull=False
    ).iterator()
    for lead in qs:
        sla = stages.get(lead.stage_id)
        if sla and (now - lead.stage_entered_at) >= timedelta(hours=sla):
            emit("sla_breach", lead, sla_hours=sla)
            fired += 1
    return fired


@shared_task
def scan_unanswered_leads(threshold_minutes=15):
    """
    Проверка лидов, которым не ответили в течение N минут после входящего сообщения.
    """
    from .models import WhatsAppMessageConsalting
    from apps.main.models import Notification
    from apps.users.models import User
    from apps.main.realtime import create_and_publish_notification

    now = timezone.now()
    threshold = now - timedelta(minutes=threshold_minutes)
    fired = 0

    open_leads = LeadConsalting.objects.filter(
        closed_at__isnull=True
    ).exclude(
        status__in=[LeadConsalting.Status.WON, LeadConsalting.Status.LOST]
    ).select_related("company", "owner").iterator()

    for lead in open_leads:
        last_msg = (
            WhatsAppMessageConsalting.objects.filter(lead=lead)
            .order_by("-created_at")
            .first()
        )
        if last_msg and last_msg.direction == WhatsAppMessageConsalting.Direction.INBOUND:
            if last_msg.created_at <= threshold:
                # Исключаем спам: проверяем, не отправлялось ли аналогичное алерт-уведомление за последние 15 мин
                recent_notif = Notification.objects.filter(
                    company=lead.company,
                    type="unanswered_lead_alert",
                    data__lead_id=str(lead.id),
                    created_at__gte=last_msg.created_at,  # одно напоминание на одно входящее
                ).exists()

                if recent_notif:
                    continue

                target_users = []
                if lead.owner:
                    target_users = [lead.owner]
                else:
                    target_users = list(User.objects.filter(company=lead.company, is_active=True))

                for u in target_users:
                    try:
                        create_and_publish_notification(
                            company=lead.company,
                            user=u,
                            title=f"⏰ Внимание: Лид без ответа > {threshold_minutes} мин!",
                            message=f"Клиент {lead.full_name} ({lead.phone}) ожидает вашего ответа более {threshold_minutes} минут.",
                            type="unanswered_lead_alert",
                            level="warning",
                            url=f"/consalting/leads/{lead.id}",
                            data={"lead_id": str(lead.id), "phone": lead.phone}
                        )
                    except Exception:
                        pass
                fired += 1
    return fired


@shared_task
def send_inbound_lead_reminders():
    """
    Периодическая задача (Celery beat):
    Напоминания владельцам по отложенным лидам, срок которых наступил.
    """
    from apps.consalting.models import InboundLeadConsalting
    from apps.consalting.funnel import realtime
    from django.utils import timezone

    now = timezone.now()
    qs = InboundLeadConsalting.objects.filter(
        status=InboundLeadConsalting.Status.DEFERRED,
        remind_at__lte=now,
        reminded_at__isnull=True,
    ).select_related("owner")

    count = 0
    for lead in qs:
        if lead.owner_id:
            try:
                realtime.notify_user(
                    lead.owner_id,
                    "consulting.lead.remind",
                    {
                        "id": str(lead.id),
                        "full_name": lead.full_name,
                        "phone": lead.phone,
                        "status": lead.status,
                        "remind_at": lead.remind_at.isoformat() if lead.remind_at else None,
                        "defer_reason": lead.defer_reason,
                        "defer_comment": lead.defer_comment,
                    }
                )
            except Exception as e:
                logger.warning("Failed to notify user for lead remind %s: %s", lead.id, e)
        lead.reminded_at = now
        lead.save(update_fields=["reminded_at", "updated_at"])
        count += 1
    return f"Sent {count} lead reminders"


@shared_task
def accrue_base_salaries(month_str=None):
    """Ежемесячное авто-начисление окладов сотрудникам с enabled base_salary (§2.5)."""
    from .models import SalarySchemeConsalting, SalaryAccrualConsalting
    from django.utils import timezone

    now = timezone.now()
    if not month_str:
        month_str = now.strftime("%Y-%m")

    schemes = SalarySchemeConsalting.objects.filter(base_salary_enabled=True, base_salary__gt=0).select_related("user", "company")
    count = 0
    for scheme in schemes:
        accrual, created = SalaryAccrualConsalting.objects.get_or_create(
            user=scheme.user,
            kind=SalaryAccrualConsalting.Kind.SALARY,
            period_month=month_str,
            defaults={
                "company": scheme.company,
                "base_amount": scheme.base_salary,
                "percent": 0,
                "amount": scheme.base_salary,
                "status": SalaryAccrualConsalting.Status.ACCRUED,
            }
        )
        if created:
            count += 1
    return f"Accrued {count} base salaries for {month_str}"


@shared_task
def process_subscription_schedules():
    """Ежедневное обслуживание графиков абонентской платы (§5.3)."""
    from .models import SubscriptionConsalting, SubscriptionPaymentConsalting
    from .funnel.completion import generate_schedule

    today = timezone.localdate()

    # 1. Перевод planned -> overdue если due_date < today
    overdue_updated = SubscriptionPaymentConsalting.objects.filter(
        status=SubscriptionPaymentConsalting.Status.PLANNED,
        due_date__lt=today
    ).update(status=SubscriptionPaymentConsalting.Status.OVERDUE)

    logger.info("process_subscription_schedules: marked %s payments overdue", overdue_updated)

    # 2. Продление подписок (если менее 3 planned периодов)
    active_subs = SubscriptionConsalting.objects.filter(
        status=SubscriptionConsalting.Status.ACTIVE, autorenew=True
    )
    for sub in active_subs:
        planned_cnt = sub.payments.filter(status=SubscriptionPaymentConsalting.Status.PLANNED).count()
        if planned_cnt < 3:
            generate_schedule(sub, horizon_months=12)

    return f"Processed subscriptions: {overdue_updated} overdue marked"
