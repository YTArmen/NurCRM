"""
Хуки приложения клиентов. Никогда не ломают продажу: всё после commit и в try/except.
"""
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.main.models import Client, ClientBonusTransaction, Sale
from apps.main.phone_utils import normalize_phone_e164
from apps.users.models import Branch, Company

from .models import AppCustomer, AppPushToken, AppShopSettings, ClientAppConfig, Referral

logger = logging.getLogger("clientapp.signals")
# Пауза перед уведомлением о продаже: даёт кассе прислать и начисление, и списание (ТЗ 14.10, п. 2.4).
SALE_PUSH_DELAY_SECONDS = int(getattr(settings, "CLIENT_APP_SALE_PUSH_DELAY_SECONDS", 8) or 0)


def _client_phone(client_id):
    phone = Client.objects.filter(pk=client_id).values_list("phone", flat=True).first()
    return normalize_phone_e164(phone)


@receiver(post_save, sender=ClientBonusTransaction, dispatch_uid="clientapp_bonus_push")
def bonus_tx_push(sender, instance, created, **kwargs):
    if not created or instance.sale_id is None:
        return
    if instance.reason not in (ClientBonusTransaction.Reason.EARN, ClientBonusTransaction.Reason.REDEEM):
        return
    sale_id, client_id = instance.sale_id, instance.client_id

    def _after_commit():
        try:
            phone = _client_phone(client_id)
            if not phone:
                return
            if not AppPushToken.objects.filter(customer__phone=phone, customer__deleted_at__isnull=True).exists():
                return
            from .tasks import enqueue, enqueue_later, send_sale_push

            # Одна продажа = одно уведомление: касса шлёт начисление и списание отдельными запросами,
            # поэтому задача ставится один раз на продажу и с задержкой, чтобы собрать оба движения.
            try:
                first = cache.add(f"capp:sale_push_queued:{sale_id}", 1, 24 * 3600)
            except Exception:
                first = True
            if not first:
                return
            if SALE_PUSH_DELAY_SECONDS > 0:
                enqueue_later(send_sale_push, SALE_PUSH_DELAY_SECONDS, str(sale_id))
            else:
                enqueue(send_sale_push, str(sale_id))
        except Exception:
            logger.exception("bonus push hook failed")

    transaction.on_commit(_after_commit)


@receiver(post_save, sender=Sale, dispatch_uid="clientapp_referral_reward")
def sale_referral_reward(sender, instance, **kwargs):
    if instance.client_id is None or instance.status not in (Sale.Status.PAID, Sale.Status.DEBT):
        return
    sale_id, client_id = instance.pk, instance.client_id

    def _after_commit():
        try:
            phone = _client_phone(client_id)
            if not phone:
                return
            if not Referral.objects.filter(
                invitee__phone=phone, invitee__deleted_at__isnull=True, rewarded_at__isnull=True
            ).exists():
                return
            from .tasks import enqueue, process_referral_reward

            enqueue(process_referral_reward, str(sale_id))
        except Exception:
            logger.exception("referral hook failed")

    transaction.on_commit(_after_commit)


def _invalidate(*args, **kwargs):
    from .services import invalidate_shops_cache

    invalidate_shops_cache()


for _model in (AppShopSettings, Branch, Company):
    post_save.connect(_invalidate, sender=_model, dispatch_uid=f"clientapp_shops_cache_save_{_model.__name__}")
    post_delete.connect(_invalidate, sender=_model, dispatch_uid=f"clientapp_shops_cache_del_{_model.__name__}")


# --- ФИО покупателя на старых кассах -------------------------------------------------
# Старые кассы читают из QR только телефон и заводят клиента без имени (пусто / номер / «Клиент»).
# Подставляем ФИО из профиля приложения; настоящее имя, введённое кассиром, не трогаем.


@receiver(pre_save, sender=Client, dispatch_uid="clientapp_client_name_from_app")
def client_name_from_app(sender, instance, **kwargs):
    try:
        from .services import app_name_for_phone, is_placeholder_name

        if instance.phone and is_placeholder_name(instance.full_name, instance.phone):
            name = app_name_for_phone(instance.phone)
            if name:
                instance.full_name = name[:255]
                update_fields = kwargs.get("update_fields")
                if update_fields is not None and "full_name" not in update_fields:
                    # save(update_fields=…) не запишет поле — допишем отдельно после сохранения
                    pk = instance.pk
                    transaction.on_commit(lambda: Client.objects.filter(pk=pk).update(full_name=name[:255]))
    except Exception:
        logger.warning("client_name_from_app failed", exc_info=True)


@receiver(post_save, sender=AppCustomer, dispatch_uid="clientapp_customer_name_to_clients")
def customer_name_to_clients(sender, instance, **kwargs):
    if not instance.full_name or not instance.phone or instance.deleted_at:
        return
    pk = instance.pk

    def _after_commit():
        try:
            from .services import fill_client_names_from_customer

            customer = AppCustomer.objects.filter(pk=pk).first()
            if customer:
                fill_client_names_from_customer(customer)
        except Exception:
            logger.warning("customer_name_to_clients failed", exc_info=True)

    transaction.on_commit(_after_commit)


@receiver(post_save, sender=ClientAppConfig, dispatch_uid="clientapp_config_shops_cache")
def config_changed(sender, instance, **kwargs):
    from .services import invalidate_shops_cache

    invalidate_shops_cache()
