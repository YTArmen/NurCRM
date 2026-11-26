"""
ТЗ кассы 05.11 п.38 / Б-94: автоотмена заказов «Примерить в магазине» (delivery_type=try_on),
которые остались в статусе new/accepted после hold_until. Остаток, зарезервированный при заказе, возвращается.
Срок брони по умолчанию — settings.SHOWCASE_TRY_ON_HOLD_HOURS (24 часа). Запуск: celery beat, каждые 10 минут.
"""
import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def cancel_expired_try_on(now=None) -> int:
    from apps.main.models import ShowcaseOrder
    from apps.main.variant_utils import order_stock_lines, release_stock

    now = now or timezone.now()
    ids = list(
        ShowcaseOrder.objects.filter(
            delivery_type=ShowcaseOrder.DeliveryType.TRY_ON,
            status__in=[ShowcaseOrder.Status.NEW, ShowcaseOrder.Status.ACCEPTED],
            hold_until__isnull=False,
            hold_until__lt=now,
        ).values_list("pk", flat=True)[:500]
    )
    done = 0
    for pk in ids:
        try:
            with transaction.atomic():
                order = ShowcaseOrder.objects.select_for_update().filter(pk=pk).first()
                if order is None or order.status not in (ShowcaseOrder.Status.NEW, ShowcaseOrder.Status.ACCEPTED):
                    continue  # за это время заказ уже обработали
                if order.stock_reserved:
                    release_stock(order_stock_lines(order))
                    order.stock_reserved = False
                order.status = ShowcaseOrder.Status.CANCELED
                order.save(update_fields=["status", "stock_reserved", "updated_at"])
                done += 1
        except Exception:
            logger.exception("try_on auto-cancel failed for order %s", pk)
    return done


@shared_task(name="apps.main.tz31_tryon_tasks.cancel_expired_try_on_orders")
def cancel_expired_try_on_orders():
    return cancel_expired_try_on()
