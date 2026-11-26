"""Celery-чистка заданий печати (хранение 7 дней). Добавить в CELERY_BEAT_SCHEDULE — см. отчёт."""
from celery import shared_task


@shared_task(name="apps.main.tz26.pos_print.tasks.purge_print_jobs")
def purge_print_jobs():
    from apps.main.tz26.pos_print.services import purge_old_jobs

    return purge_old_jobs()
