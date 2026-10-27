"""Celery-задачи ящика, этап 2 (Б-78…Б-86). Расписание — в отчёте (core/settings.py править нельзя)."""
from celery import shared_task


@shared_task(name="apps.inbox.tasks.ai_auto_reply")
def ai_auto_reply(conversation_id, message_id):
    """Б-79: автоответ ИИ на входящее (ai_mode=auto)."""
    from . import ai
    return ai.auto_reply(conversation_id, message_id)


@shared_task(name="apps.inbox.tasks.sync_wa_templates")
def sync_wa_templates():
    """Б-78: раз в час — статусы шаблонов WhatsApp у Meta/BSP."""
    from . import templates
    from .models import Channel
    n = 0
    for ch in Channel.objects.filter(kind__in=templates.WA_KINDS, status="active"):
        try:
            n += templates.sync(ch)
        except Exception:
            import logging
            logging.getLogger("inbox").warning("sync_wa_templates: канал %s пропущен", ch.id)
    return n


@shared_task(name="apps.inbox.tasks.refresh_instagram_tokens")
def refresh_instagram_tokens():
    """Б-86: раз в сутки — продлить токены Instagram, которым осталось ≤ 10 дней."""
    from . import meta_channels
    return meta_channels.refresh_instagram_tokens()


@shared_task(name="apps.inbox.tasks.process_campaigns")
def process_campaigns():
    """Б-81: каждую минуту — отправка запланированных рассылок (тихие часы, ступень WhatsApp, 20 ч на получателя)."""
    from . import campaigns
    from .models import Campaign
    n = 0
    for c in Campaign.objects.filter(status__in=("scheduled", "sending")).select_related("channel", "template", "company"):
        try:
            n += campaigns.process(c)
        except Exception:
            import logging
            logging.getLogger("inbox").exception("process_campaigns: рассылка %s", c.id)
    return n
