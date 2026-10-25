"""Аналитика (ТЗ §3.8, §5.4): всё считается из начислений, оплат, расходов и договоров, без ручной истории.

Определения:
- Начислено = Σ Charge (не аннулированные) по месяцу начисления `period`: аренда, коммунальные, пеня, штрафы.
- Собрано = Σ проведённых оплат арендаторов (tenant != null) по дате оплаты. Прочий доход = оплаты без арендатора.
- Расходы = Σ Expense (не аннулированные) по дате. Амортизация = стоимость / (срок × 12) за месяцы жизни актива.
- Прибыль = Начислено + Прочий доход − Расходы − Амортизация.
- Заполняемость = сданная площадь / вся площадь; договор занимает помещение с start_date по min(end_date, terminated_at).
"""
import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from ..models import (Asset, Charge, Employee, Expense, Lease, Payment, PayrollLine, Remark, Tenant, Unit)
from .charges import month_end, month_start
from .debt import calc_penalty
from .money import calc_rent, round_int
from .reminders import _rates, tenant_states

TABS = ["overview", "revenue", "expenses", "tenants", "units"]
PERIODS = ["month", "quarter", "year"]


# ------------------------------------------------------------------ период


def period_bounds(period: str, d: date):
    if period == "month":
        return month_start(d), month_end(d)
    if period == "quarter":
        m0 = (d.month - 1) // 3 * 3 + 1
        return date(d.year, m0, 1), month_end(date(d.year, m0 + 2, 1))
    return date(d.year, 1, 1), date(d.year, 12, 31)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def last_12_months(end: date):
    first = add_months(month_start(end), -11)
    return [add_months(first, i) for i in range(12)]


def pct(a, b):
    return round(a * 100 / b, 1) if b else 0.0


class Scope:
    """Компания + филиалы + период. Все выборки идут через него — «Все филиалы» и филиал считаются одинаково."""

    def __init__(self, company, branch_ids, period, on_date, today):
        self.company, self.ids = company, branch_ids
        self.period, self.today = period, today
        self.start, self.end = period_bounds(period, on_date)
        self.ref = min(self.end, today)  # дата «на момент» для долга и заполняемости
        self.months = last_12_months(self.end)
        self.settings = company.arenda_settings
        self._leases = None

    def _b(self, qs, field="branch_id"):
        return qs if self.ids is None else qs.filter(**{f"{field}__in": self.ids})

    def charges(self, start=None, end=None):
        qs = self._b(Charge.objects.filter(company=self.company, voided_at__isnull=True))
        return qs.filter(period__gte=start or self.start, period__lte=end or self.end)

    def payments(self, start=None, end=None):
        qs = self._b(Payment.objects.filter(company=self.company, status="posted"))
        return qs.filter(paid_at__date__gte=start or self.start, paid_at__date__lte=end or self.end)

    def expenses(self, start=None, end=None):
        qs = self._b(Expense.objects.filter(company=self.company, voided_at__isnull=True))
        return qs.filter(date__gte=start or self.start, date__lte=end or self.end)

    def units(self):
        return list(self._b(Unit.objects.filter(company=self.company, archived_at__isnull=True)
                            .select_related("unit_type")))

    def leases(self):
        if self._leases is None:
            qs = self._b(Lease.objects.filter(company=self.company, status__in=["active", "terminated"],
                                              archived_at__isnull=True)
                         .select_related("tenant").prefetch_related("units"))
            self._leases = list(qs)
        return self._leases


def lease_covers(l, d: date) -> bool:
    end = l.end_date
    if l.status == "terminated" and l.terminated_at:
        end = min(end, l.terminated_at) if end else l.terminated_at
    return l.start_date <= d and (end is None or d <= end)


def occupied_unit_ids(sc: Scope, d: date) -> set:
    out = set()
    for l in sc.leases():
        if lease_covers(l, d):
            out.update(u.id for u in l.units.all())
    return out


def depreciation_month(assets, month: date) -> int:
    total = 0
    for a in assets:
        first = month_start(a.purchase_date)
        n = a.life_years * 12
        if first <= month < add_months(first, n):
            total += round_int(Decimal(a.cost) / n)
    return total


def depreciation(sc: Scope, start=None, end=None) -> int:
    assets = list(sc._b(Asset.objects.filter(company=sc.company, archived_at__isnull=True)))
    m, last, total = month_start(start or sc.start), end or sc.end, 0
    while m <= last:
        total += depreciation_month(assets, m)
        m = add_months(m, 1)
    return total


def _sum(qs, field="amount"):
    from django.db.models import Sum

    return int(qs.aggregate(s=Sum(field))["s"] or 0)


def _month_sums(model_qs_fn, months, date_field=None):
    return [model_qs_fn(m, month_end(m)) for m in months]


def money_block(sc: Scope, start=None, end=None):
    charged = _sum(sc.charges(start, end))
    tenant_pay = _sum(sc.payments(start, end).filter(tenant__isnull=False))
    other = _sum(sc.payments(start, end).filter(tenant__isnull=True))
    exp = _sum(sc.expenses(start, end))
    dep = depreciation(sc, start, end)
    return {"charged": charged, "collected": tenant_pay, "other_income": other, "expenses": exp,
            "depreciation": dep, "profit": charged + other - exp - dep}


# ------------------------------------------------------------------ состояние на дату


def occupancy_at(sc: Scope, units, d: date):
    occ = occupied_unit_ids(sc, d)
    total = sum((u.area for u in units), Decimal(0))
    rented = sum((u.area for u in units if u.id in occ), Decimal(0))
    return float(round(rented / total * 100, 1)) if total else 0.0, total, rented


def debt_block(sc: Scope):
    st = tenant_states(sc.company, sc.ref, sc.ids, sc.settings.soon_days)
    return st, sum(s["info"].debt for s in st.values())


# ------------------------------------------------------------------ вкладки


def overview(sc: Scope):
    units = sc.units()
    occ, total_area, rented_area = occupancy_at(sc, units, sc.ref)
    money = money_block(sc)
    states, debt = debt_block(sc)
    overdue = [t for t, s in states.items() if s["info"].status == "overdue"]
    soon = [t for t, s in states.items() if s["info"].status == "due_soon"]
    exp_days = sc.settings.lease_expiring_days
    expiring = [l for l in sc.leases() if l.status == "active" and l.end_date
                and sc.ref <= l.end_date <= sc.ref + timedelta(days=exp_days)]
    occupied = occupied_unit_ids(sc, sc.ref)
    free = [u for u in units if u.id not in occupied]
    attention = []
    if overdue:
        attention.append({"kind": "overdue", "count": len(overdue), "text": f"Просрочили оплату: {len(overdue)}"})
    if soon:
        attention.append({"kind": "due_soon", "count": len(soon), "text": f"Скоро срок оплаты: {len(soon)}"})
    if expiring:
        attention.append({"kind": "expiring_leases", "count": len(expiring),
                          "text": f"Договоры заканчиваются в ближайшие {exp_days} дн.: {len(expiring)}"})
    if free:
        attention.append({"kind": "free_units", "count": len(free), "text": f"Свободных помещений: {len(free)}"})
    return {
        "revenue": money["charged"] + money["other_income"], "charged": money["charged"],
        "collected": money["collected"], "other_income": money["other_income"],
        "expenses": money["expenses"], "depreciation": money["depreciation"], "profit": money["profit"],
        "occupancy_percent": occ, "total_area": str(total_area), "rented_area": str(rented_area),
        "debt": debt, "overdue_tenants": len(overdue), "due_soon_tenants": len(soon),
        "attention": attention,
    }


def _unit_allocation(sc: Scope):
    """{lease_id: [(floor, type_name, area)]} для разнесения начислений по этажам и типам."""
    out = {}
    for l in sc.leases():
        out[l.id] = [(u.floor, u.unit_type.name if u.unit_type else "Без типа", u.area) for u in l.units.all()]
    return out


def _split(amount, weights):
    """Разделить целое amount пропорционально весам без потери копеек."""
    total = sum(weights)
    if not weights or total <= 0:
        return [0] * len(weights)
    parts = [int(Decimal(amount) * w / total) for w in weights]
    parts[-1] += amount - sum(parts)
    return parts


def revenue(sc: Scope):
    months = []
    for m in sc.months:
        ch = _sum(sc.charges(m, month_end(m)))
        col = _sum(sc.payments(m, month_end(m)).filter(tenant__isnull=False))
        months.append({"month": m, "charged": ch, "collected": col, "collection_percent": pct(col, ch)})
    charged = _sum(sc.charges())
    collected = _sum(sc.payments().filter(tenant__isnull=False))
    alloc = _unit_allocation(sc)
    by_floor, by_type = defaultdict(int), defaultdict(int)
    for lease_id, amount in sc.charges().values_list("lease_id", "amount"):
        units = alloc.get(lease_id)
        if not units:
            by_floor[None] += amount
            by_type["Без помещения"] += amount
            continue
        for (floor, tname, _), part in zip(units, _split(amount, [u[2] for u in units])):
            by_floor[floor] += part
            by_type[tname] += part
    methods = defaultdict(lambda: {"amount": 0, "count": 0})
    for method, amount in sc.payments().values_list("method", "amount"):
        methods[method]["amount"] += amount
        methods[method]["count"] += 1
    return {
        "months": months, "charged": charged, "collected": collected, "collection_percent": pct(collected, charged),
        "by_floor": [{"floor": f, "amount": a} for f, a in sorted(by_floor.items(), key=lambda x: (x[0] is None, x[0]))],
        "by_unit_type": [{"unit_type": k, "amount": v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        "payment_methods": [{"method": k, "amount": v["amount"], "count": v["count"]} for k, v in methods.items()],
    }


def expenses(sc: Scope):
    total = _sum(sc.expenses())
    revenue_total = _sum(sc.charges()) + _sum(sc.payments().filter(tenant__isnull=True))
    cats = defaultdict(int)
    for name, amount in sc.expenses().values_list("category__name", "amount"):
        cats[name] += amount
    months = []
    for m in sc.months:
        e = _sum(sc.expenses(m, month_end(m)))
        r = _sum(sc.charges(m, month_end(m))) + _sum(sc.payments(m, month_end(m)).filter(tenant__isnull=True))
        months.append({"month": m, "expenses": e, "revenue": r, "share_of_revenue_percent": pct(e, r)})
    payroll_exp = sum(a for n, a in cats.items() if n == "Зарплата")
    employer = _sum(sc._b(PayrollLine.objects.filter(company=sc.company, payroll__month__gte=sc.start,
                                                      payroll__month__lte=sc.end),
                          field="payroll__branch_id"), "employer_contribution")
    headcount = sc._b(Employee.objects.filter(company=sc.company, archived_at__isnull=True,
                                              fired_at__isnull=True)).count()
    return {
        "total": total, "share_of_revenue_percent": pct(total, revenue_total), "revenue": revenue_total,
        "by_category": [{"category": k, "amount": v, "share_percent": pct(v, total)}
                        for k, v in sorted(cats.items(), key=lambda x: -x[1])],
        "months": months,
        "staff": {"salary_expenses": payroll_exp, "employer_contributions": employer,
                  "total_cost": payroll_exp + employer, "headcount": headcount,
                  "share_of_expenses_percent": pct(payroll_exp, total)},
    }


def _discipline(sc: Scope):
    """Дисциплина оплат по начислениям со сроком в периоде: оплата закрывает самые старые начисления первыми,
    дата «оплачено» = дата, когда накопленные оплаты покрыли накопленные начисления."""
    ch = defaultdict(list)
    for t, due, amount in sc._b(Charge.objects.filter(company=sc.company, voided_at__isnull=True)
                                .exclude(kind="fine")).values_list("tenant_id", "due_date", "amount"):
        ch[t].append((due, amount))
    pays = defaultdict(list)
    for t, paid_at, amount in sc._b(Payment.objects.filter(company=sc.company, status="posted",
                                                           tenant__isnull=False)
                                    ).order_by("paid_at").values_list("tenant_id", "paid_at", "amount"):
        pays[t].append((timezone_date(paid_at), amount))
    per = {}
    for t, items in ch.items():
        cum_paid, idx, acc = [], 0, 0
        for d, a in pays.get(t, []):
            acc += a
            cum_paid.append((acc, d))
        cum_charge = 0
        on_time = late = 0
        delays = []
        for due, amount in sorted(items):
            cum_charge += amount
            if not (sc.start <= due <= sc.end) or due > sc.ref:
                continue
            paid_day = next((d for tot, d in cum_paid if tot >= cum_charge), None)
            if paid_day is None:
                late += 1
                delays.append((sc.ref - due).days)
            elif paid_day <= due:
                on_time += 1
            else:
                late += 1
                delays.append((paid_day - due).days)
        if on_time + late:
            per[t] = {"on_time": on_time, "late": late, "avg_delay_days": round(sum(delays) / len(delays), 1) if delays else 0.0}
    return per


def timezone_date(dt):
    from django.utils import timezone

    return timezone.localtime(dt).date() if timezone.is_aware(dt) else dt.date()


def tenants(sc: Scope):
    states, _ = debt_block(sc)
    rates = _rates(sc.company, sc.ref, sc.settings, sc.ids)
    names = {t.id: t for t in Tenant.objects.filter(company=sc.company, id__in=list(states))}
    top = []
    for tid, st in states.items():
        info = st["info"]
        if info.debt > 0 and tid in names:
            rate = rates.get(tid, (sc.settings.penalty_rate, None))[0]
            top.append({"tenant": {"id": str(tid), "name": names[tid].name}, "debt": info.debt,
                        "overdue_days": info.overdue_days, "status": info.status,
                        "penalty_calc": calc_penalty(info.overdue_base, rate, info.overdue_days)})
    top.sort(key=lambda r: -r["debt"])
    disc = _discipline(sc)
    on_time = sum(v["on_time"] for v in disc.values())
    late = sum(v["late"] for v in disc.values())
    worst = sorted(disc.items(), key=lambda kv: (-kv[1]["late"], -kv[1]["avg_delay_days"]))[:10]
    exp_days = sc.settings.lease_expiring_days
    expiring = [{"lease_id": str(l.id), "label": l.label, "tenant": {"id": str(l.tenant_id), "name": l.tenant.name},
                 "end_date": l.end_date, "days_left": (l.end_date - sc.ref).days}
                for l in sorted((l for l in sc.leases() if l.status == "active" and l.end_date
                                 and sc.ref <= l.end_date <= sc.ref + timedelta(days=exp_days)),
                                key=lambda l: l.end_date)]
    rq = sc._b(Remark.objects.filter(company=sc.company, archived_at__isnull=True,
                                     date__gte=sc.start, date__lte=sc.end), field="tenant__branch_id")
    by_type = defaultdict(int)
    recent = []
    for r in rq.select_related("tenant").order_by("-date")[:200]:
        by_type[r.type] += 1
        if len(recent) < 10:
            recent.append({"id": str(r.id), "tenant": {"id": str(r.tenant_id), "name": r.tenant.name},
                           "type": r.type, "date": r.date, "fine_amount": r.fine_amount})
    return {
        "top_debtors": top[:10], "debtors_count": len(top),
        "discipline": {"on_time": on_time, "late": late, "on_time_percent": pct(on_time, on_time + late),
                       "worst": [{"tenant": {"id": str(t), "name": names[t].name if t in names else ""}, **v}
                                 for t, v in worst if t in names]},
        "expiring_leases": expiring,
        "remarks": {"total": sum(by_type.values()), "by_type": dict(by_type), "recent": recent,
                    "fines_total": _sum(sc.charges().filter(kind="fine"))},
    }


def units_tab(sc: Scope):
    units = sc.units()
    months = []
    for m in sc.months:
        d = min(month_end(m), sc.today)
        p, _, _ = occupancy_at(sc, units, d)
        months.append({"month": m, "occupancy_percent": p})
    occupied = occupied_unit_ids(sc, sc.ref)
    floors = defaultdict(lambda: {"total": Decimal(0), "rented": Decimal(0), "units": 0})
    for u in units:
        f = floors[u.floor]
        f["total"] += u.area
        f["units"] += 1
        if u.id in occupied:
            f["rented"] += u.area
    by_floor = [{"floor": k, "units": v["units"], "total_area": str(v["total"]), "rented_area": str(v["rented"]),
                 "occupancy_percent": float(round(v["rented"] / v["total"] * 100, 1)) if v["total"] else 0.0}
                for k, v in sorted(floors.items())]
    last_end = {}
    for l in sc.leases():
        end = l.end_date
        if l.status == "terminated" and l.terminated_at:
            end = min(end, l.terminated_at) if end else l.terminated_at
        if end and end < sc.ref:
            for u in l.units.all():
                last_end[u.id] = max(last_end.get(u.id, end), end)
    idle, lost = [], 0
    for u in units:
        if u.id in occupied:
            continue
        monthly = calc_rent(u.area, u.rate)
        lost += monthly
        since = last_end.get(u.id) or u.created_at.date()
        idle.append({"id": str(u.id), "number": u.number, "floor": u.floor, "area": str(u.area),
                     "unit_type": u.unit_type.name if u.unit_type else "", "rent_monthly": monthly,
                     "idle_days": max(0, (sc.ref - since).days)})
    idle.sort(key=lambda r: -r["idle_days"])
    months_in_period = {"month": 1, "quarter": 3, "year": 12}[sc.period]
    return {"months": months, "by_floor": by_floor, "idle_units": idle, "idle_count": len(idle),
            "lost_rent_monthly": lost, "lost_rent_period": lost * months_in_period}


def build(tab: str, sc: Scope):
    return {"overview": overview, "revenue": revenue, "expenses": expenses, "tenants": tenants,
            "units": units_tab}[tab](sc)
