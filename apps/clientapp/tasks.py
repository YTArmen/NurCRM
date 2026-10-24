import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("clientapp.tasks")


def enqueue(task, *args):
    """Ставит задачу в очередь; если брокер недоступен — только лог (продажу не блокируем)."""
    try:
        task.delay(*args)
    except Exception as exc:
        logger.warning("clientapp: cannot enqueue %s: %s", getattr(task, "name", task), exc)


def enqueue_later(task, countdown, *args):
    """То же, что enqueue, но с задержкой (секунды)."""
    try:
        task.apply_async(args=args, countdown=countdown)
    except Exception as exc:
        logger.warning("clientapp: cannot enqueue %s: %s", getattr(task, "name", task), exc)


@shared_task(name="apps.clientapp.tasks.send_sale_push", ignore_result=True)
def send_sale_push(sale_id):
    """Одно уведомление по продаже (начисление + списание одним текстом)."""
    from .push import push_for_sale

    try:
        return push_for_sale(sale_id)
    except Exception:
        logger.exception("send_sale_push failed for %s", sale_id)
        return 0


@shared_task(name="apps.clientapp.tasks.send_bonus_push", ignore_result=True)
def send_bonus_push(tx_id):
    """Совместимость (задачи, поставленные старым кодом до обновления воркера)."""
    from .push import push_for_bonus_tx

    try:
        return push_for_bonus_tx(tx_id)
    except Exception:
        logger.exception("send_bonus_push failed for %s", tx_id)
        return 0


@shared_task(name="apps.clientapp.tasks.process_referral_reward", ignore_result=True)
def process_referral_reward(sale_id):
    from .services import process_referral_for_sale

    try:
        ref = process_referral_for_sale(sale_id)
        return ref.pk if ref else None
    except Exception:
        logger.exception("process_referral_reward failed for %s", sale_id)
        return None


@shared_task(
    name="apps.clientapp.tasks.geocode_shop",
    bind=True,
    max_retries=5,
    default_retry_delay=5,
    ignore_result=True,
)
def geocode_shop(self, row_id):
    from .geocode import geocode_row

    try:
        return geocode_row(row_id)
    except RuntimeError as exc:  # занято / 429 — повторим позже
        raise self.retry(exc=exc, countdown=5 + self.request.retries * 10)


@shared_task(name="apps.clientapp.tasks.geocode_missing_shops", ignore_result=True)
def geocode_missing_shops(limit=30):
    """Периодически: адрес изменился (в т.ч. адрес филиала/компании) или координат ещё нет."""
    from .geocode import geocode_row, needs_geocode
    from .models import AppShopSettings

    done = 0
    for row in AppShopSettings.objects.select_related("company", "branch").order_by("updated_at"):
        if done >= limit:
            break
        if not needs_geocode(row):
            continue
        try:
            geocode_row(row.pk)
        except RuntimeError:
            break
        done += 1
    return done


@shared_task(name="apps.clientapp.tasks.cleanup_client_app", ignore_result=True)
def cleanup_client_app():
    from .models import AppQrToken
    from .telegram import expire_old_nonces

    expire_old_nonces()
    AppQrToken.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()
