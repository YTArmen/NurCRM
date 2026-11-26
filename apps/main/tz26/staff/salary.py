"""
ТЗ 26.10 Б-63: расчёт зарплатной ведомости — сменная ставка, штрафы/премии/авансы, старые расходы с меткой
`[#user период]`, выписка по сотруднику (`market-salary/statement/`).

Одно число «осталось к выплате» считается здесь для всех клиентов:
    left = начислено (оклад + процент + за товар + за смену) + премии − штрафы − авансы − выплачено.
"""
import re
from datetime import date as _date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

Q2 = Decimal("0.01")
ZERO = Decimal("0.00")

# метка, которую пишут касса и мобилка в название расхода: [#<uuid сотрудника> 01.10.2026–31.10.2026]
_D = r"(?:\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})"
TAG_RE = re.compile(
    rf"\[#(?P<uid>[0-9a-fA-F]{{8}}-?[0-9a-fA-F]{{4}}-?[0-9a-fA-F]{{4}}-?[0-9a-fA-F]{{4}}-?[0-9a-fA-F]{{12}})"
    rf"(?:\s+(?P<a>{_D})?\s*[–—-]\s*(?P<b>{_D})?)?\s*\]"
)


def money(v) -> str:
    return f"{Decimal(v).quantize(Q2, rounding=ROUND_HALF_UP):.2f}"


def _parse_tag_date(s):
    if not s:
        return None
    try:
        if "." in s:
            return datetime.strptime(s, "%d.%m.%Y").date()
        return _date.fromisoformat(s)
    except ValueError:
        return None


def _norm_uid(s: str) -> str:
    s = s.replace("-", "").lower()
    return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"


# ------------------------------------------------------------------ смены
def shift_stats_bulk(company, user_ids, start, end):
    """
    Закрытые смены сотрудников за период [start, end): день смены — по дате открытия.
    → {user_id: (число смен, часы Decimal)}
    """
    from apps.construction.models import CashShift

    out = {}
    if not user_ids:
        return out
    rows = CashShift.objects.filter(
        company=company, cashier_id__in=list(user_ids), status=CashShift.Status.CLOSED,
        closed_at__isnull=False, opened_at__gte=start, opened_at__lt=end,
    ).values_list("cashier_id", "opened_at", "closed_at")
    for uid, opened, closed in rows:
        n, h = out.get(uid, (0, Decimal("0")))
        secs = max((closed - opened).total_seconds(), 0)
        out[uid] = (n + 1, h + Decimal(str(secs)) / Decimal("3600"))
    return {k: (n, h.quantize(Q2, rounding=ROUND_HALF_UP)) for k, (n, h) in out.items()}


def per_shift_bonus_for(prof, shifts_n: int) -> Decimal:
    """Сменная ставка: полная ставка за каждую закрытую смену, без пропорции за короткую (решение по вопросу ТЗ №4)."""
    from apps.main.models import MarketSaleEmployeePayProfile as P

    if prof is None or prof.pay_scheme not in (P.PayScheme.PER_SHIFT, P.PayScheme.PER_SHIFT_PLUS_PERCENT):
        return ZERO
    return (Decimal(shifts_n) * (prof.per_shift_amount or ZERO)).quantize(Q2, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ старые расходы с меткой
def legacy_tagged_expenses(company, user_ids, start_date, end_date_excl):
    """
    Расходы кассы с меткой `[#user период]`, записанные до выкатки выплат (мимо market-salary/payouts/).
    Выплаты нового API (source_kind=salary_payout) не берём — они считаются своими записями.
    Попадают в период по концу периода из метки (нет метки периода — по дате создания).
    → [{uid, date, amount, name, id}]
    """
    from apps.construction.models import CashFlow

    wanted = {_norm_uid(str(u)) for u in user_ids}
    out = []
    qs = (
        CashFlow.objects.filter(company=company, type=CashFlow.Type.EXPENSE, name__contains="[#")
        .exclude(source_kind=CashFlow.SourceKind.SALARY_PAYOUT)
        .exclude(status=CashFlow.Status.REJECTED)
    )
    for cf in qs.only("id", "name", "amount", "created_at"):
        m = TAG_RE.search(cf.name or "")
        if not m:
            continue
        uid = _norm_uid(m.group("uid"))
        if uid not in wanted:
            continue
        d = _parse_tag_date(m.group("b")) or _parse_tag_date(m.group("a")) \
            or timezone.localtime(cf.created_at).date()
        if start_date <= d < end_date_excl:
            out.append({"uid": uid, "date": d, "amount": cf.amount, "name": cf.name, "id": str(cf.id)})
    return out


# ------------------------------------------------------------------ выплаты в строки ведомости
def apply_payouts(company, rows, start, end):
    """
    Добавляет в строки tab=salary (и выписки) выплаты, штрафы, премии, авансы, остаток.
    → (всего выплачено+авансы, всего осталось)
    """
    from apps.main.market_salary_views import salary_payout_totals

    sd = start.date() if hasattr(start, "date") else start
    ed = end.date() if hasattr(end, "date") else end
    uids = [r["user_id"] for r in rows]
    pay = salary_payout_totals(company, start, end, user_ids=uids)
    legacy = {}
    for it in legacy_tagged_expenses(company, uids, sd, ed):
        legacy[it["uid"]] = legacy.get(it["uid"], ZERO) + it["amount"]
    tot_paid, tot_left = ZERO, ZERO
    for r in rows:
        pt = pay.get(r["user_id"]) or {}
        accrued = Decimal(r["total"])
        paid = pt.get("salary", ZERO) + legacy.get(_norm_uid(r["user_id"]), ZERO)
        adv = pt.get("advance", ZERO)
        bon = pt.get("bonus", ZERO)
        ded = pt.get("deduction", ZERO)
        fines = pt.get("fine", ZERO) + ded
        left = accrued + bon - fines - paid - adv
        r.update({
            "accrued": money(accrued), "paid": money(paid), "advance": money(adv), "advances": money(adv),
            "bonus": money(bon), "bonuses": money(bon), "deductions": money(ded), "fines": money(fines),
            "left": money(left),
        })
        tot_paid += paid + adv
        tot_left += left
    return tot_paid, tot_left


# ------------------------------------------------------------------ выписка
def employee_row(request, company, employee, period):
    """Строка tab=salary по одному сотруднику (с выплатами). Нет данных — нулевая строка."""
    from apps.main.analytics_market import AnalyticsView, _get_active_branch

    try:
        branch = _get_active_branch(request)
    except PermissionDenied:  # сотрудник без филиала смотрит только свою строку - утечки нет
        branch = None
    data = AnalyticsView()._salary(request, company, branch, period)
    row = next((r for r in data.get("rows", []) if r["user_id"] == str(employee.id)), None)
    if row is None:
        z = money(ZERO)
        row = {
            "user_id": str(employee.id), "base_prorated": z, "percent_bonus": z, "per_item_bonus": z,
            "per_shift_bonus": z, "shifts": 0, "hours": z, "total": z,
        }
        apply_payouts(company, [row], period.start, period.end)
    return row


def build_statement(request, company, employee, period, with_lines=True):
    from apps.main.market_salary_views import payout_queryset
    from apps.main.models import _user_display_name

    r = employee_row(request, company, employee, period)
    sd, ed = period.start.date(), period.end.date()
    out = {
        "employee": {"id": str(employee.id), "name": _user_display_name(employee)},
        "period_start": sd.isoformat(),
        "period_end": (ed - timedelta(days=1)).isoformat(),
        "accrued_base": r["base_prorated"],
        "accrued_percent": r["percent_bonus"],
        "accrued_per_item": r["per_item_bonus"],
        "accrued_per_shift": r.get("per_shift_bonus", money(ZERO)),
        "accrued": r["total"],
        "bonuses": r["bonuses"],
        "fines": r["fines"],
        "advances": r["advances"],
        "paid": r["paid"],
        "left": r["left"],
        "shifts": r.get("shifts", 0),
        "hours": r.get("hours", money(ZERO)),
    }
    if with_lines:
        lines = []
        for t, title in (("base", "Оклад"), ("percent", "Процент от продаж"), ("per_item", "За проданный товар"),
                         ("per_shift", "За смены")):
            amount = out[f"accrued_{t}"]
            if Decimal(amount) != 0:
                lines.append({"type": "accrual", "kind": t, "title": title, "date": None, "amount": amount})
        for p in payout_queryset(company, period.start, period.end, [employee.id]).order_by("created_at"):
            d = p.date or p.period_to or timezone.localtime(p.created_at).date()
            lines.append({"type": "payout", "id": str(p.id), "kind": p.kind, "title": p.get_kind_display(),
                          "date": d.isoformat(), "amount": money(p.amount), "comment": p.comment,
                          "cashflow_id": str(p.cashflow_id) if p.cashflow_id else None})
        for it in legacy_tagged_expenses(company, [employee.id], sd, ed):
            lines.append({"type": "legacy_expense", "id": it["id"], "kind": "salary", "title": it["name"],
                          "date": it["date"].isoformat(), "amount": money(it["amount"]), "comment": ""})
        lines.sort(key=lambda x: (x["date"] or "", x["type"]))
        out["lines"] = lines
    return out
