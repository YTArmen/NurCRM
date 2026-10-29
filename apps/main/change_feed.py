"""
ТЗ Н-31: лента изменений для касс.

Одно место записи — ``record_change``: вставка в ``ChangeEvent`` после коммита
транзакции (id растут в порядке коммита, курсор не «теряет» события) и пуш в
WebSocket-группу компании. Сюда же пишет ``emit_event`` (все старые события
вебхуков попадают в ленту) и сигналы ниже (новые типы).

Типы в ленте — со знаком «_» вместо точки: sale_paid, shift_opened, stock_low, ...
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models.signals import post_delete, post_init, post_save, m2m_changed
from django.dispatch import receiver

logger = logging.getLogger("nurcrm.change_feed")

# Новые типы (помимо старых событий вебхуков)
NEW_EVENT_TYPES = (
    "client_changed",  # ТЗ 29.10 Б-102
    "bonus_changed",
    "staff_changed",
    "cash_moved",
    "debt_paid",
    "debt_created",
    "app_settings_changed",
    "product_changed",
    "stock_changed",
    "rental_created",
    "rental_updated",
    "rental_extended",
    "supplier_debt_changed",
    "features_changed",
    "pos_request_changed",
)

RETENTION_DAYS = 7


def company_ws_group(company_id) -> str:
    return f"chg_company_{company_id}"


def feed_type(event: str) -> str:
    return event.replace(".", "_")


def _json_safe(data):
    from apps.integrations.events import _json_safe as js
    return js(data or {})


def record_change(company_id, type_, *, branch_id=None, actor_id=None, entity="", entity_id="", data=None):
    """Пишет событие в ленту после коммита. Никогда не бросает исключений."""
    if not company_id:
        return
    payload = _json_safe(data)

    def _write():
        try:
            from apps.main.models import ChangeEvent

            ev = ChangeEvent.objects.create(
                company_id=company_id,
                branch_id=branch_id or None,
                type=type_,
                actor_id=actor_id or None,
                entity=str(entity or "")[:48],
                entity_id=str(entity_id or "")[:64],
                data=payload,
            )
        except Exception:
            logger.exception("change feed write failed: %s", type_)
            return
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            layer = get_channel_layer()
            if layer:
                async_to_sync(layer.group_send)(
                    company_ws_group(company_id),
                    {"type": "change.event", "event": serialize_event(ev)},
                )
        except Exception:
            logger.debug("change feed ws push failed", exc_info=True)

    try:
        transaction.on_commit(_write)
    except Exception:
        logger.exception("change feed on_commit failed: %s", type_)


def serialize_event(ev) -> dict:
    from django.utils import timezone

    return {
        "id": ev.id,
        "type": ev.type,
        "at": timezone.localtime(ev.created_at).isoformat(timespec="seconds"),
        "branch": str(ev.branch_id) if ev.branch_id else None,
        "actor": str(ev.actor_id) if ev.actor_id else None,
        "entity": ev.entity,
        "entity_id": ev.entity_id,
        "data": ev.data or {},
    }


def record_from_emit(company_id, event, data):
    """Вызывается из emit_event: достаёт филиал/автора/сущность из данных события."""
    d = data or {}
    branch = d.get("branch") or d.get("branch_id")
    actor = d.get("actor") or d.get("cashier") or d.get("user")
    entity, entity_id = d.get("entity") or "", d.get("entity_id") or ""
    for key in () if entity else ("sale", "shift", "product", "return", "order", "appointment", "deal", "client"):
        if d.get(key) is not None:
            entity, entity_id = key, d.get(key)
            break
    if event.startswith("shift."):
        entity, entity_id = "shift", d.get("shift", "")
    record_change(
        company_id, feed_type(event),
        branch_id=branch if _is_pk(branch) else None,
        actor_id=actor if _is_pk(actor) else None,
        entity=entity, entity_id=entity_id, data=d,
    )


def _is_pk(v):
    return v is not None and v != "" and not isinstance(v, (dict, list))


# ------------------------------ сигналы ------------------------------

def _emit(company_id, type_, *, branch_id=None, actor_id=None, entity="", entity_id="", data=None):
    """Событие нового типа: лента + вебхуки (через emit_event)."""
    from apps.integrations.events import emit_event

    d = dict(data or {})
    d.setdefault("entity", entity)
    d.setdefault("entity_id", str(entity_id))
    if branch_id:
        d.setdefault("branch", branch_id)
    if actor_id:
        d.setdefault("actor", actor_id)
    emit_event(company_id, type_, d)


@receiver(post_save, sender="main.DealPayment")
def _deal_payment_saved(sender, instance, created, **kw):
    if created and instance.kind == "pay":
        _emit(instance.company_id, "debt_paid", branch_id=instance.branch_id, actor_id=instance.created_by_id,
              entity="deal", entity_id=instance.deal_id,
              data={"deal": instance.deal_id, "amount": instance.amount, "payment_method": instance.payment_method})


@receiver(post_save, sender="main.ClientDeal")
def _client_deal_saved(sender, instance, created, **kw):
    if created and instance.kind == "debt":
        _emit(instance.company_id, "debt_created", branch_id=instance.branch_id,
              entity="deal", entity_id=instance.pk,
              data={"deal": instance.pk, "client": instance.client_id, "amount": instance.amount,
                    "prepayment": instance.prepayment})


@receiver(post_save, sender="main.DebtPayment")
def _debt_payment_saved(sender, instance, created, **kw):
    if created:
        _emit(instance.company_id, "debt_paid", branch_id=instance.branch_id,
              entity="debt", entity_id=instance.debt_id,
              data={"debt": instance.debt_id, "amount": instance.amount, "payment_method": instance.payment_method})


@receiver(post_save, sender="main.Debt")
def _debt_saved(sender, instance, created, **kw):
    if created:
        _emit(instance.company_id, "debt_created", branch_id=getattr(instance, "branch_id", None),
              entity="debt", entity_id=instance.pk, data={"debt": instance.pk})


def _settings_key_versions(instance):
    """{ключ: версия} из служебного _meta (ТЗ Н-17); секреты и служебные ключи не берём."""
    st = instance.__dict__.get("settings") or {}
    meta = (st.get("_meta") or {}) if isinstance(st, dict) else {}
    out = {}
    for k, v in (st.items() if isinstance(st, dict) else []):
        if k.startswith("_") or k == "secrets" or k.startswith("secret_"):
            continue
        out[k] = (meta.get(k) or {}).get("version") if isinstance(meta.get(k), dict) else None
    return out


@receiver(post_init, sender="users.CompanyAppSettings")
def _settings_snapshot(sender, instance, **kw):
    instance._chg_kv = _settings_key_versions(instance)


@receiver(post_save, sender="users.CompanyAppSettings")
def _app_settings_saved(sender, instance, created, **kw):
    # ТЗ 29.10 Б-102: keys[] — какие ключи изменились (только имена, без значений и секретов)
    prev = getattr(instance, "_chg_kv", None) or {}
    cur = _settings_key_versions(instance)
    keys = sorted(k for k in cur if k not in prev or prev[k] != cur[k] or cur[k] is None)
    keys += sorted(k for k in prev if k not in cur)
    instance._chg_kv = cur
    _emit(instance.company_id, "app_settings_changed", branch_id=instance.branch_id,
          actor_id=instance.updated_by_id, entity="app_settings", entity_id=instance.app,
          data={"app": instance.app, "version": instance.version, "keys": keys[:100]})


@receiver(post_save, sender="users.CompanyAddon")
@receiver(post_delete, sender="users.CompanyAddon")
def _addon_changed(sender, instance, **kw):
    _emit(instance.company_id, "features_changed", entity="feature", entity_id=instance.code,
          data={"code": instance.code})


# --- товары ---
_TRACKED = ("price", "quantity", "barcode", "wholesale_price", "discount_percent")


def _snap(instance):
    d = instance.__dict__
    return {k: d[k] for k in _TRACKED if k in d}  # отложенные (defer/only) поля пропускаем


@receiver(post_init, sender="main.Product")
def _product_snapshot(sender, instance, **kw):
    instance._chg_snapshot = _snap(instance)


def _product_what(instance):
    prev = getattr(instance, "_chg_snapshot", None)
    if not prev:
        return []
    new = _snap(instance)

    def diff(k):
        return k in prev and k in new and prev[k] != new[k]

    what = []
    if diff("price") or diff("wholesale_price"):
        what.append("price")
    if diff("discount_percent"):
        what.append("promo")
    if diff("quantity"):
        what.append("quantity")
    if diff("barcode"):
        what.append("barcode")
    return what


@receiver(post_save, sender="main.Product")
def _product_saved(sender, instance, created, **kw):
    if created:
        whats = ["created"]
    else:
        whats = _product_what(instance)
    instance._chg_snapshot = _snap(instance)
    for w in whats:
        _emit(instance.company_id, "product_changed", branch_id=instance.branch_id,
              entity="product", entity_id=instance.pk, data={"what": w, "product": instance.pk})
    if "quantity" in whats:
        _emit(instance.company_id, "stock_changed", branch_id=instance.branch_id,
              entity="product", entity_id=instance.pk,
              data={"product": instance.pk, "quantity": instance.quantity})


@receiver(post_delete, sender="main.Product")
def _product_deleted(sender, instance, **kw):
    _emit(instance.company_id, "product_changed", branch_id=instance.branch_id,
          entity="product", entity_id=instance.pk, data={"what": "deleted", "product": instance.pk})


@receiver(post_save, sender="main.ProductVariant")
@receiver(post_delete, sender="main.ProductVariant")
def _variant_changed(sender, instance, **kw):
    _emit(instance.company_id, "product_changed", entity="product", entity_id=instance.product_id,
          data={"what": "variants", "product": instance.product_id, "variant": instance.pk})


@receiver(post_save, sender="main.ProductPromotionTier")
@receiver(post_delete, sender="main.ProductPromotionTier")
def _tier_changed(sender, instance, **kw):
    cid = instance.product.company_id if instance.product_id else None
    if cid:
        _emit(cid, "product_changed", entity="product", entity_id=instance.product_id,
              data={"what": "promo", "product": instance.product_id})


@receiver(post_save, sender="main.PromoRule")
@receiver(post_delete, sender="main.PromoRule")
def _promo_rule_changed(sender, instance, **kw):
    cid = getattr(instance, "company_id", None)
    if cid:
        _emit(cid, "product_changed", entity="promo", entity_id=instance.pk, data={"what": "promo"})


@receiver(post_save, sender="main.ProductAlternateBarcode")
@receiver(post_delete, sender="main.ProductAlternateBarcode")
def _alt_barcode_changed(sender, instance, **kw):
    cid = getattr(instance, "company_id", None)
    if cid:
        _emit(cid, "product_changed", entity="product", entity_id=instance.product_id,
              data={"what": "barcode", "product": instance.product_id})


@receiver(post_save, sender="main.ProductStock")
def _stock_saved(sender, instance, **kw):
    _emit(instance.company_id, "stock_changed", entity="product", entity_id=instance.product_id,
          data={"product": instance.product_id, "variant": instance.variant_id,
                "warehouse": instance.warehouse_id, "quantity": instance.quantity})


# --- прокат, поставщики ---
@receiver(post_save, sender="main.Rental")
def _rental_saved(sender, instance, created, **kw):
    _emit(instance.company_id, "rental_created" if created else "rental_updated", branch_id=instance.branch_id,
          entity="rental", entity_id=instance.pk, data={"rental": instance.pk, "status": instance.status})


@receiver(post_save, sender="main.RentalExtension")
def _rental_extension_saved(sender, instance, created, **kw):
    if created:
        _emit(getattr(instance, "company_id", None) or _rental_company(instance), "rental_extended",
              entity="rental", entity_id=instance.rental_id, data={"rental": instance.rental_id})


def _rental_company(ext):
    return ext.rental.company_id


@receiver(post_save, sender="main.SupplierReceipt")
def _supplier_receipt_saved(sender, instance, created, **kw):
    if created:
        _emit(instance.company_id, "supplier_debt_changed", branch_id=instance.branch_id,
              actor_id=instance.created_by_id, entity="supplier", entity_id=instance.supplier_id,
              data={"supplier": instance.supplier_id, "receipt": instance.pk})


# ---------------- ТЗ 29.10 Б-102: клиенты, бонусы, сотрудники, внесение/изъятие ----------------

@receiver(post_save, sender="main.Client")
@receiver(post_delete, sender="main.Client")
def _client_changed(sender, instance, **kw):
    if instance.company_id:
        _emit(instance.company_id, "client_changed", branch_id=getattr(instance, "branch_id", None),
              entity="client", entity_id=instance.pk, data={"client": instance.pk})


@receiver(post_save, sender="main.ClientBonusTransaction")
def _bonus_saved(sender, instance, created, **kw):
    if created:
        _emit(instance.company_id, "bonus_changed", actor_id=instance.user_id,
              entity="client", entity_id=instance.client_id, data={"client": instance.client_id})


@receiver(post_save, sender="users.User")
@receiver(post_delete, sender="users.User")
def _staff_changed(sender, instance, **kw):
    # вход (last_login) — не изменение сотрудника
    uf = kw.get("update_fields")
    if uf is not None and set(uf) <= {"last_login"}:
        return
    cid = instance.__dict__.get("company_id")
    if cid:
        _emit(cid, "staff_changed", entity="user", entity_id=instance.pk, data={"user": instance.pk})


@receiver(post_save, sender="construction.CashFlow")
def _cash_moved(sender, instance, created, **kw):
    if created and instance.source_kind in ("shift_drawer_inflow", "shift_drawer_outflow"):
        _emit(instance.company_id, "cash_moved", branch_id=getattr(instance, "branch_id", None),
              entity="cashbox", entity_id=instance.cashbox_id,
              data={"cashbox": instance.cashbox_id,
                    "state": "cash_in" if instance.source_kind == "shift_drawer_inflow" else "cash_out"})
