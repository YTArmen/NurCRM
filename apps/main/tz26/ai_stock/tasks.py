"""Б-65: Celery-задача чистки журнала ИИ (хранение 180 дней)."""
from celery import shared_task

from apps.main.tz26.ai_stock.services import purge_old


@shared_task(name="apps.main.tz26.ai_stock.tasks.purge_ai_actions")
def purge_ai_actions():
    return purge_old()
