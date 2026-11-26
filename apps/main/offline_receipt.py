"""13.11.2026 П0-2 (ТЗ сервер — единое): приход, принятый кассой без связи, — с настоящим временем.

POST suppliers/<id>/receipt/ {offline: true, received_at: ISO-8601 с поясом, ...}
- время прихода -> created_at прихода, purchased_at истории закупок, created_at расхода из кассы и долга поставщику;
  срок долга по умолчанию считается от этой даты;
- старше 7 суток -> 400 offline_too_old; дата в закрытом периоде или смена уже закрыта -> 423 period_closed;
- без offline и без received_at — всё как раньше.
"""
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.exceptions import ApiError

MAX_AGE = timedelta(days=7)
CLOCK_SKEW = timedelta(minutes=5)


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def offline_received_at(request, company):
    """None (обычный приход) или aware datetime прихода без связи. Ошибки — ApiError до любых записей."""
    data = request.data if hasattr(request.data, "get") else {}
    off = _truthy(data.get("offline"))
    raw = data.get("received_at")
    if not off and not raw:
        return None
    if not raw:
        raise ApiError("validation_error", "Для прихода без связи нужно received_at.",
                       fields={"received_at": ["Обязательно при offline=true."]})
    at = parse_datetime(str(raw))
    if at is None:
        raise ApiError("validation_error", "received_at: ISO-8601 с часовым поясом.",
                       fields={"received_at": ["ISO-8601 с часовым поясом."]})
    if timezone.is_naive(at):
        at = timezone.make_aware(at)
    now = timezone.now()
    if at > now + CLOCK_SKEW:
        raise ApiError("validation_error", "received_at в будущем.", fields={"received_at": ["Время в будущем."]})
    if not off and at >= now - CLOCK_SKEW:
        return None  # свежая дата — обычный приход
    if at < now - MAX_AGE:
        raise ApiError("offline_too_old", "Приход старше 7 суток — оформите его вручную.", status_code=400,
                       data={"max_days": MAX_AGE.days, "received_at": at.isoformat()},
                       fields={"received_at": ["Старше 7 суток."]})
    from apps.warehouse.services import get_closed_until

    closed = get_closed_until(company)
    if closed is not None and timezone.localdate(at) <= closed:
        raise ApiError("period_closed", f"Период до {closed:%d.%m.%Y} закрыт — приход этой датой не проводится.",
                       status_code=423, data={"closed_until": closed.isoformat(), "reason": "period_closed"})
    shift_id = data.get("shift") or data.get("shift_id")
    if shift_id:
        from apps.construction.models import CashShift

        try:
            sh = CashShift.objects.filter(company=company, id=shift_id).only("status").first()
        except Exception:
            sh = None
        if sh is not None and sh.status != CashShift.Status.OPEN:
            raise ApiError("period_closed", "Смена уже закрыта — расход в неё не записать. Проведите приход без смены.",
                           status_code=423, data={"reason": "shift_closed", "shift": str(shift_id)})
    return at


def default_due(at, days=30):
    return timezone.localdate(at) + timedelta(days=days)


def backdate(receipt, at, *, cashflows=(), deal=None, purchases=()):
    """Переносит записи прихода на время at (update — без auto_now_add)."""
    from apps.main.models import ClientDeal, SupplierPurchase, SupplierReceipt

    SupplierReceipt.objects.filter(pk=receipt.pk).update(created_at=at)
    receipt.created_at = at
    ids = [p.pk for p in purchases if p is not None]
    if ids:
        SupplierPurchase.objects.filter(pk__in=ids).update(purchased_at=at)
    cf_ids = [c.pk for c in cashflows if c is not None]
    if cf_ids:
        from apps.construction.models import CashFlow

        CashFlow.objects.filter(pk__in=cf_ids).update(created_at=at)
    if deal is not None:
        ClientDeal.objects.filter(pk=deal.pk).update(created_at=at)
