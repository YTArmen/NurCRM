"""Агрегаты с БД: долги арендаторов, статусы помещений, показатели шахматки."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from ..models import Charge, Lease, Payment, Unit
from .charges import month_end, month_start
from .debt import DebtInfo, calc_penalty, compute_debt


def rented_leases(company, today, branch_ids=None):
    """Договоры, занимающие помещения сейчас: активные, начавшиеся (истёкшие остаются до расторжения)."""
    qs = (Lease.objects.filter(company=company, status="active", archived_at__isnull=True, start_date__lte=today)
          .select_related("tenant").prefetch_related("units"))
    if branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)
    return list(qs)


def debt_map(company, today, branch_ids=None, tenant_ids=None, soon_days=7):
    """{tenant_id: DebtInfo} — из начислений и проведённых оплат (с учётом филиала)."""
    cq = Charge.objects.filter(company=company, voided_at__isnull=True)
    pq = Payment.objects.filter(company=company, status="posted", tenant__isnull=False)
    if branch_ids is not None:
        cq, pq = cq.filter(branch_id__in=branch_ids), pq.filter(branch_id__in=branch_ids)
    if tenant_ids is not None:
        cq, pq = cq.filter(tenant_id__in=tenant_ids), pq.filter(tenant_id__in=tenant_ids)
    by_tenant = defaultdict(list)
    for t, due, amount, kind in cq.values_list("tenant_id", "due_date", "amount", "kind"):
        by_tenant[t].append((due, amount, kind))
    paid = {r["tenant_id"]: r["s"] for r in pq.values("tenant_id").annotate(s=Sum("amount"))}
    out = {}
    for t in set(by_tenant) | set(paid):
        out[t] = compute_debt(by_tenant.get(t, []), paid.get(t, 0), today, soon_days)
    return out


def penalty_for(info: DebtInfo, rate) -> int:
    return calc_penalty(info.overdue_base, rate, info.overdue_days)


def unit_state_map(company, today, branch_ids=None, soon_days=7):
    """{unit_id: {status, overdue_days, tenant}} для не архивных помещений."""
    units = Unit.objects.filter(company=company, archived_at__isnull=True)
    if branch_ids is not None:
        units = units.filter(branch_id__in=branch_ids)
    leases = rented_leases(company, today, branch_ids)
    debts = debt_map(company, today, branch_ids, {l.tenant_id for l in leases}, soon_days)
    by_unit = {}
    for lease in leases:
        info = debts.get(lease.tenant_id, DebtInfo())
        for u in lease.units.all():
            by_unit[u.id] = (lease, info)
    out = {}
    for u in units:
        hit = by_unit.get(u.id)
        if hit:
            lease, info = hit
            out[u.id] = {"status": {"ok": "rented"}.get(info.status, info.status),
                         "overdue_days": info.overdue_days, "tenant": lease.tenant}
        else:
            if u.reserved_until:
                reserved = u.reserved_until >= today
            else:
                reserved = bool(u.reserved_for)
            out[u.id] = {"status": "reserved" if reserved else "free", "overdue_days": 0, "tenant": None}
    return out


def kpis(company, today, branch_ids=None, soon_days=7):
    units = Unit.objects.filter(company=company, archived_at__isnull=True)
    if branch_ids is not None:
        units = units.filter(branch_id__in=branch_ids)
    state = unit_state_map(company, today, branch_ids, soon_days)
    total_area = Decimal(0)
    rented_area = Decimal(0)
    for u in units:
        total_area += u.area
        if state.get(u.id, {}).get("status") in ("rented", "due_soon", "overdue"):
            rented_area += u.area
    occupancy = float(round(rented_area / total_area * 100, 1)) if total_area else 0.0
    ms = month_start(today)
    cq = Charge.objects.filter(company=company, voided_at__isnull=True, period=ms)
    pq = Payment.objects.filter(company=company, status="posted", paid_at__date__gte=ms,
                                paid_at__date__lte=month_end(today))
    if branch_ids is not None:
        cq, pq = cq.filter(branch_id__in=branch_ids), pq.filter(branch_id__in=branch_ids)
    charged = cq.aggregate(s=Sum("amount"))["s"] or 0
    received = pq.aggregate(s=Sum("amount"))["s"] or 0
    debts = debt_map(company, today, branch_ids, None, soon_days)
    return {
        "occupancy_percent": occupancy,
        "charged_month": int(charged),
        "received_month": int(received),
        "tenants_debt": int(sum(i.debt for i in debts.values())),
    }
