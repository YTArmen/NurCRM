from celery import shared_task


@shared_task(name="apps.main.tz1029_bonus_tasks.expire_bonuses")
def expire_bonuses_task():
    """ТЗ 29.10 Б-99: ночное сгорание бонусов."""
    from apps.main.tz1029_bonus_expire import expire_bonuses
    return len(expire_bonuses())
