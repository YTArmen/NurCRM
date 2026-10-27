"""Б-62: Celery-задачи уведомлений о задачах сотрудникам.

Нужно в core/settings.py: добавить 'apps.main.tz26.staff.tasks' в CELERY_IMPORTS и в CELERY_BEAT_SCHEDULE
запись 'staff-task-reminders' (apps.main.tz26.staff.tasks.send_due_task_reminders, каждую минуту).
"""
import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from . import notify

logger = logging.getLogger(__name__)


@shared_task(name="apps.main.tz26.staff.tasks.notify_task_assigned")
def notify_task_assigned(task_id: str):
    from apps.main.models import Task

    task = Task.objects.select_related("assigned_to").filter(id=task_id).first()
    if task is None or task.assigned_to is None or task.status == "done":
        return {"skipped": True}
    sent = notify.send_telegram(task.company_id, task.assigned_to, notify.task_text(task, "assigned"))
    notify.emit_task_event(task, "task.assigned")
    return {"sent": sent}


@shared_task(name="apps.main.tz26.staff.tasks.send_task_reminder")
def send_task_reminder(task_id: str):
    """Напоминание по одной задаче; повторно не шлёт (reminded_at)."""
    from apps.main.models import Task

    now = timezone.now()
    with transaction.atomic():
        task = Task.objects.select_for_update().select_related("assigned_to").filter(id=task_id).first()
        if (task is None or task.remind_at is None or task.remind_at > now or task.reminded_at is not None
                or task.assigned_to is None or task.status == "done"):
            return {"skipped": True}
        Task.objects.filter(pk=task.pk).update(reminded_at=now)
    sent = notify.send_telegram(task.company_id, task.assigned_to, notify.task_text(task, "reminder"))
    notify.emit_task_event(task, "task.reminder")
    return {"sent": sent}


@shared_task(name="apps.main.tz26.staff.tasks.send_due_task_reminders")
def send_due_task_reminders():
    """Периодически (раз в минуту): все задачи, у которых наступил remind_at."""
    from apps.main.models import Task

    ids = list(
        Task.objects.filter(remind_at__lte=timezone.now(), reminded_at__isnull=True, assigned_to__isnull=False)
        .exclude(status="done").values_list("id", flat=True)[:500]
    )
    done = 0
    for tid in ids:
        try:
            if not send_task_reminder(str(tid)).get("skipped"):
                done += 1
        except Exception:
            logger.exception("task reminder failed: %s", tid)
    return {"reminded": done}


def schedule_notifications(task, *, notify_now: bool, plan_reminder: bool):
    """Вызывается после коммита: сообщение исполнителю и (если задан remind_at) напоминание по времени."""
    try:
        if notify_now:
            notify_task_assigned.delay(str(task.id))
        if plan_reminder and task.remind_at and task.assigned_to_id:
            send_task_reminder.apply_async(args=[str(task.id)], eta=task.remind_at)
    except Exception:
        logger.exception("task notification schedule failed")
