"""
Публикация market/POS-событий в колокольчик и WebSocket (P0/P1 из
docs/backend/kassa/realtime-notifications-backend.md).

Принцип: **одна запись Notification на получателя** + публикация в его личную
WS-группу (`notif_user_<id>`). Так бейдж колокольчика после перезагрузки
страницы (REST `unread_count`) всегда совпадает с тем, что пользователь получил
по сокету — без «фантомных» broadcast-уведомлений, которых нет в БД.

Все функции best-effort: сбой публикации никогда не роняет бизнес-операцию.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction

logger = logging.getLogger("nurcrm.websocket.notifications")

# ─── Типы событий (совпадают с FE enum MARKET_REALTIME_EVENTS) ───
SALE_CREATED = "market.sale.created"
PAYMENT_RECEIVED = "market.payment.received"
CASHFLOW_CREATED = "market.cashflow.created"
CASHFLOW_PENDING = "market.cashflow.pending"
PRODUCT_RECEIVED = "market.product.received"
PRODUCT_STATUS_CHANGED = "market.product.status_changed"
PRODUCT_WRITTEN_OFF = "market.product.written_off"
SUPPLIER_RETURN = "market.supplier.return"
DEBT_CREATED = "market.debt.created"
DEBT_PAID = "market.debt.paid"
DEBT_OVERDUE = "market.debt.overdue"
SHIFT_OPENED = "market.shift.opened"
SHIFT_CLOSED = "market.shift.closed"
STOCK_LOW = "market.stock.low"
TRANSFER_RECEIVED = "market.transfer.received"
PRODUCT_EXPIRING = "market.product.expiring"
TARIFF_EXPIRING = "tariff.expiring"

OWNER_LIKE_ROLES = ("owner", "admin")


def fmt_money(value) -> str:
    """1250 → «1 250,00» (узкий пробел как разделитель разрядов, запятая — дробь)."""
    try:
        dec = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except Exception:
        return "0,00"
    whole, _, frac = f"{dec:.2f}".partition(".")
    neg = whole.startswith("-")
    whole = whole.lstrip("-")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return ("-" if neg else "") + " ".join(groups) + "," + frac


# ─────────────────────────── получатели ───────────────────────────

def owner_like_users(company, branch=None, exclude=None):
    """Владельцы и администраторы компании (опционально — своего филиала)."""
    from apps.construction.batch_cache import memo, ref
    key = ("owners", ref(company), ref(branch), tuple(sorted(ref(u) for u in (exclude or []) if u is not None)))
    return memo(key, lambda: _owner_like_users(company, branch, exclude))


def _owner_like_users(company, branch=None, exclude=None):
    from apps.users.models import User

    if not company:
        return []
    qs = User.objects.filter(
        company=company,
        role__in=OWNER_LIKE_ROLES,
        is_active=True,
    )
    users = {u.id: u for u in qs}

    owner = getattr(company, "owner", None)
    if owner is not None and getattr(owner, "is_active", True):
        users.setdefault(owner.id, owner)

    exclude_ids = {getattr(u, "id", u) for u in (exclude or []) if u is not None}
    return [u for uid, u in users.items() if uid not in exclude_ids]


def cashbox_users(cashbox, exclude=None):
    """Кассиры, у которых открыта смена на этой кассе (аналог группы cashbox:{id})."""
    if cashbox is None:
        return []
    try:
        from apps.construction.models import CashShift

        qs = (
            CashShift.objects
            .filter(cashbox=cashbox, status=CashShift.Status.OPEN, cashier__isnull=False)
            .select_related("cashier")
        )
        users = {sh.cashier_id: sh.cashier for sh in qs if sh.cashier and sh.cashier.is_active}
    except Exception:
        logger.warning("cashbox_users failed for cashbox=%s", getattr(cashbox, "id", None), exc_info=True)
        return []

    exclude_ids = {getattr(u, "id", u) for u in (exclude or []) if u is not None}
    return [u for uid, u in users.items() if uid not in exclude_ids]


# ─────────────────────────── публикация ───────────────────────────

def _dedupe_exists(company, user, event_type, source_id) -> bool:
    if not source_id:
        return False
    from apps.main.models import Notification

    return Notification.objects.filter(
        company=company,
        user=user,
        type=event_type,
        data__source_id=str(source_id),
    ).exists()


def _publish_now(*, company, branch, actor, recipients, event_type, title, message,
                 level, url, cta_label, meta, category):
    from apps.main.models import Notification
    from apps.main.realtime import publish_notification

    created = []
    for user in recipients:
        if user is None:
            continue
        try:
            if _dedupe_exists(company, user, event_type, meta.get("source_id")):
                continue
            notification = Notification.objects.create(
                company=company,
                branch=branch,
                user=user,
                actor=actor,
                message=message,
                title=title,
                category=category,
                type=event_type,
                level=level,
                url=url,
                data=dict(meta, cta_label=cta_label) if cta_label else dict(meta),
            )
            publish_notification(notification)
            push_unread_count(user)
            created.append(notification)
        except Exception:
            logger.error(
                "Failed to publish %s to user=%s", event_type, getattr(user, "id", None), exc_info=True
            )
    return created


def publish_event(*, company, event_type, title, recipients, message="", level="info",
                  url="", cta_label="", meta=None, branch=None, actor=None,
                  category="system", source_kind="", source_id=None):
    """
    Создаёт уведомления получателям и шлёт их в WS после коммита транзакции.

    Идемпотентность: повторная публикация того же ``(company, user, event_type,
    source_id)`` не создаёт дубль — ключ хранится в ``Notification.data.source_id``.
    """
    recipients = [u for u in (recipients or []) if u is not None]
    if not company or not recipients:
        return

    payload_meta = dict(meta or {})
    payload_meta.setdefault("company_id", str(company.id))
    if branch is not None:
        payload_meta.setdefault("branch_id", str(branch.id))
    if source_kind:
        payload_meta.setdefault("source_kind", source_kind)
    if source_id:
        payload_meta.setdefault("source_id", str(source_id))

    def _run():
        try:
            _publish_now(
                company=company, branch=branch, actor=actor, recipients=recipients,
                event_type=event_type, title=title, message=message, level=level,
                url=url, cta_label=cta_label, meta=payload_meta, category=category,
            )
        except Exception:
            logger.error("publish_event(%s) failed", event_type, exc_info=True)

    try:
        if transaction.get_connection().in_atomic_block:
            transaction.on_commit(_run)
        else:
            _run()
    except Exception:
        _run()


def push_unread_count(user) -> None:
    """Шлёт клиенту актуальный бейдж: {"type": "unread_count", "count": N}."""
    if user is None:
        return
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        from apps.main.models import Notification
        from apps.main.realtime import user_group_name

        layer = get_channel_layer()
        if layer is None:
            return
        count = Notification.objects.filter(user=user, is_read=False).count()
        async_to_sync(layer.group_send)(
            user_group_name(getattr(user, "id", user)),
            {"type": "unread", "count": count},
        )
    except Exception:
        logger.warning("push_unread_count failed for user=%s", getattr(user, "id", None), exc_info=True)


# ─────────────────── доменные хелперы (точки вызова) ───────────────────

def _cashbox_title(cashbox) -> str:
    if cashbox is None:
        return "Касса"
    name = (getattr(cashbox, "name", "") or "").strip()
    if name:
        return name
    branch = getattr(cashbox, "branch", None)
    return f"Касса филиала {branch.name}" if branch else "Касса"


def notify_sale_created(sale, actor=None) -> None:
    """`market.sale.created` — чек пробит. Кому: владельцы/админы + кассиры этой кассы,
    кроме самого продавца (он и так видит результат на экране)."""
    try:
        company = sale.company
        cashbox = getattr(sale, "cashbox", None)
        exclude = [u for u in (actor, getattr(sale, "user", None)) if u is not None]
        recipients = {u.id: u for u in owner_like_users(company, exclude=exclude)}
        for u in cashbox_users(cashbox, exclude=exclude):
            recipients.setdefault(u.id, u)
        if not recipients:
            return
        publish_event(
            company=company,
            branch=getattr(sale, "branch", None),
            actor=actor or getattr(sale, "user", None),
            recipients=list(recipients.values()),
            event_type=SALE_CREATED,
            title="Новая продажа",
            message=f"{_cashbox_title(cashbox)}: {fmt_money(sale.total)} сом",
            level="info",
            url="/crm/market/cashier",
            cta_label="Открыть кассу",
            source_kind="pos_sale",
            source_id=sale.id,
            meta={
                "sale_id": str(sale.id),
                "cashbox_id": str(sale.cashbox_id) if sale.cashbox_id else None,
                "shift_id": str(sale.shift_id) if sale.shift_id else None,
                "amount": f"{Decimal(str(sale.total or 0)):.2f}",
                "payment_method": getattr(sale, "payment_method", "") or "",
            },
        )
    except Exception:
        logger.error("notify_sale_created failed for sale=%s", getattr(sale, "id", None), exc_info=True)


def notify_cashflow(cashflow, actor=None) -> None:
    """`market.cashflow.created` / `market.cashflow.pending` — авто-операция по кассе.
    Кому: владельцы/админы (у них подтверждение заявок в /crm/pending)."""
    try:
        from apps.construction.models import CashFlow

        company = cashflow.company
        is_pending = cashflow.status == CashFlow.Status.PENDING
        is_income = cashflow.type == CashFlow.Type.INCOME

        # Подтверждённый приход по чеку уже покрыт market.sale.created — второй
        # тост на ту же продажу только шумит. Заявки (pending) шлём всегда:
        # по ним владельцу нужно действие в /crm/pending.
        if not is_pending and cashflow.source_kind in (
            CashFlow.SourceKind.POS_SALE, CashFlow.SourceKind.POS_PREPAYMENT,
        ):
            return
        recipients = owner_like_users(company, exclude=[actor] if actor else None)
        if not recipients:
            return

        sign = "+" if is_income else "−"
        amount = f"{sign}{fmt_money(cashflow.amount)} сом"
        name = (cashflow.name or "").strip() or ("Приход" if is_income else "Расход")
        if is_pending:
            title = "Операция ждёт подтверждения"
            url, cta_label, level = "/crm/pending", "Подтвердить", "warning"
        else:
            title = "Приход в кассу" if is_income else "Расход из кассы"
            url, cta_label, level = "/crm/kassa", "Открыть кассу", "success" if is_income else "info"

        publish_event(
            company=company,
            branch=getattr(cashflow, "branch", None),
            actor=actor,
            recipients=recipients,
            event_type=CASHFLOW_PENDING if is_pending else CASHFLOW_CREATED,
            title=title,
            message=f"{name}: {amount} ({_cashbox_title(getattr(cashflow, 'cashbox', None))})",
            level=level,
            url=url,
            cta_label=cta_label,
            source_kind=cashflow.source_kind or "cashflow",
            source_id=f"cashflow:{cashflow.id}",
            meta={
                "cashflow_id": str(cashflow.id),
                "cashbox_id": str(cashflow.cashbox_id) if cashflow.cashbox_id else None,
                "shift_id": str(cashflow.shift_id) if cashflow.shift_id else None,
                "amount": f"{Decimal(str(cashflow.amount or 0)):.2f}",
                "flow_type": cashflow.type,
                "status": cashflow.status,
            },
        )
    except Exception:
        logger.error("notify_cashflow failed for cf=%s", getattr(cashflow, "id", None), exc_info=True)


def notify_shift_opened(shift, actor=None) -> None:
    _notify_shift(shift, actor=actor, opened=True)


def notify_shift_closed(shift, actor=None) -> None:
    _notify_shift(shift, actor=actor, opened=False)


def _notify_shift(shift, *, actor=None, opened: bool) -> None:
    try:
        company = shift.company
        cashier = getattr(shift, "cashier", None)
        cashier_name = ""
        if cashier is not None:
            cashier_name = (
                f"{(cashier.first_name or '').strip()} {(cashier.last_name or '').strip()}".strip()
                or getattr(cashier, "email", "") or ""
            )
        recipients = owner_like_users(company, exclude=[actor] if actor else None)
        if not recipients:
            return

        cb = _cashbox_title(getattr(shift, "cashbox", None))
        if opened:
            title, level = "Смена открыта", "info"
            message = f"{cb}: смену открыл(а) {cashier_name}".strip()
            meta = {}
        else:
            try:
                expected = Decimal(str(shift.calc_live_totals().get("drawer_expected_cash") or 0))
            except Exception:
                expected = Decimal(str(getattr(shift, "opening_cash", 0) or 0)) + Decimal(
                    str(getattr(shift, "cash_sales_total", 0) or 0)
                )
            closing = Decimal(str(getattr(shift, "closing_cash", 0) or 0))
            diff = closing - expected
            title = "Смена закрыта"
            level = "warning" if diff != 0 else "info"
            message = f"{cb}: выручка {fmt_money(shift.sales_total)} сом, в кассе {fmt_money(closing)} сом"
            if diff != 0:
                message += f" (расхождение {fmt_money(diff)} сом)"
            meta = {"cash_diff": f"{diff:.2f}", "sales_total": f"{Decimal(str(shift.sales_total or 0)):.2f}"}

        publish_event(
            company=company,
            branch=getattr(shift, "branch", None),
            actor=actor or cashier,
            recipients=recipients,
            event_type=SHIFT_OPENED if opened else SHIFT_CLOSED,
            title=title,
            message=message,
            level=level,
            url="/crm/kassa",
            cta_label="Открыть кассу",
            source_kind="cash_shift",
            source_id=f"shift:{shift.id}:{'open' if opened else 'close'}",
            meta=dict(meta, shift_id=str(shift.id),
                      cashbox_id=str(shift.cashbox_id) if shift.cashbox_id else None,
                      cashier_name=cashier_name),
        )
    except Exception:
        logger.error("_notify_shift failed for shift=%s", getattr(shift, "id", None), exc_info=True)


def notify_debt_overdue(installment) -> None:
    """Уведомление о просрочке взноса (market.debt.overdue)."""
    try:
        deal = installment.deal
        company = deal.company
        branch = deal.branch
        recipients = owner_like_users(company)
        if not recipients:
            return

        client_name = deal.client.full_name if deal.client else "Клиент"
        overdue_amt = installment.remaining_for_period
        days = installment.overdue_days

        publish_event(
            company=company,
            branch=branch,
            recipients=recipients,
            event_type=DEBT_OVERDUE,
            title="Просрочен платёж по долгу",
            message=f"{client_name}: взнос №{installment.number} просрочен на {days} дн. (остаток {fmt_money(overdue_amt)} сом)",
            level="warning",
            url=f"/crm/clients/{deal.client_id}" if deal.client_id else "/crm/debts",
            cta_label="Открыть долг",
            source_kind="deal_installment",
            source_id=f"installment:{installment.id}",
            meta={
                "deal_id": str(deal.id),
                "installment_id": str(installment.id),
                "installment_number": installment.number,
                "due_date": str(installment.due_date),
                "promised_date": str(installment.promised_date) if installment.promised_date else None,
                "overdue_days": days,
                "overdue_amount": f"{overdue_amt:.2f}",
                "client_id": str(deal.client_id) if deal.client_id else None,
                "client_name": client_name,
            },
        )
    except Exception:
        logger.error("notify_debt_overdue failed for installment=%s", getattr(installment, "id", None), exc_info=True)

