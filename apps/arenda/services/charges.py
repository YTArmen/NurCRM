"""Ежемесячные начисления аренды (ТЗ §5.1). Чистые функции + идемпотентная генерация."""
import calendar
from datetime import date
from decimal import Decimal
from typing import Optional

from django.db import IntegrityError, transaction

from .money import calc_rent, round_int, to_decimal


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def due_date_for(period: date, pay_day: int) -> date:
    last = calendar.monthrange(period.year, period.month)[1]
    return period.replace(day=max(1, min(int(pay_day or 1), last)))


def prorata_amount(monthly: int, period: date, lease_start: date, lease_end: Optional[date]) -> int:
    """Аренда за месяц period; неполные первый/последний месяцы — пропорционально дням."""
    ms, me = month_start(period), month_end(period)
    first = max(ms, lease_start)
    last = min(me, lease_end) if lease_end else me
    if last < first:
        return 0
    days = (last - first).days + 1
    dim = me.day
    if days >= dim:
        return int(monthly)
    return round_int(to_decimal(monthly) * days / dim)


def indexed_amount(base: int, indexation_percent, lease_start: date, period: date) -> int:
    """Раз в год с даты начала аренда растёт на % (с месяца годовщины)."""
    pct = to_decimal(indexation_percent or 0)
    if pct <= 0:
        return int(base)
    years = period.year - lease_start.year - (1 if period.month < lease_start.month else 0)
    if years <= 0:
        return int(base)
    return round_int(to_decimal(base) * (Decimal(1) + pct / 100) ** years)


def lease_monthly_rent(lease) -> int:
    """Аренда в месяц: сумма из договора или Σ(площадь × ставка) по помещениям."""
    if lease.rent_monthly is not None:
        return int(lease.rent_monthly)
    return sum(calc_rent(u.area, u.rate) for u in lease.units.all())


def charge_amount_for_period(lease, period: date) -> int:
    base = indexed_amount(lease_monthly_rent(lease), lease.indexation_percent, lease.start_date, period)
    return prorata_amount(base, period, lease.start_date, lease.end_date)


def ensure_rent_charge(lease, period: date, created_by=None):
    """Создаёт начисление аренды за период, если его ещё нет. Возвращает (charge|None, created)."""
    from ..models import Charge

    period = month_start(period)
    amount = charge_amount_for_period(lease, period)
    if amount <= 0:
        return None, False
    existing = Charge.objects.filter(lease=lease, kind="rent", period=period).first()
    if existing:
        return existing, False
    try:
        with transaction.atomic():
            ch = Charge.objects.create(
                company_id=lease.company_id, branch_id=lease.branch_id, tenant_id=lease.tenant_id,
                lease=lease, kind="rent", period=period, amount=amount,
                due_date=due_date_for(period, lease.pay_day),
                description=f"Аренда за {period:%m.%Y}", created_by=created_by,
            )
        return ch, True
    except IntegrityError:
        return Charge.objects.filter(lease=lease, kind="rent", period=period).first(), False


def generate_monthly_charges(on_date: date, company=None) -> dict:
    """Начисления за месяц on_date по всем действующим договорам. Идемпотентно."""
    from ..models import Lease

    period = month_start(on_date)
    qs = (Lease.objects.filter(status="active", archived_at__isnull=True, start_date__lte=month_end(period))
          .exclude(end_date__lt=period).prefetch_related("units"))
    if company is not None:
        qs = qs.filter(company=company)
    created = skipped = 0
    for lease in qs:
        _, was_created = ensure_rent_charge(lease, period)
        created += int(was_created)
        skipped += int(not was_created)
    return {"period": period.isoformat(), "created": created, "skipped": skipped}


def lease_current_rent(lease, today: date) -> int:
    """Аренда в месяц на сегодня (с индексацией, без пропорции)."""
    return indexed_amount(lease_monthly_rent(lease), lease.indexation_percent, lease.start_date, today)
