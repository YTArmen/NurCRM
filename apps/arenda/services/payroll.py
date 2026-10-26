"""Зарплатная ведомость (ТЗ §5.3). Ставки берутся из ArendaSettings. Всё в целых тыйынах.

Округление: каждая налоговая сумма округляется отдельно «по-школьному» до тыйына (round_int);
аванс — до 100 сом (10 000 тыйынов), но не больше «на руки».
  Соцфонд = round(Начислено × СФ%);  ПН = round((Начислено − Соцфонд) × ПН%);
  На руки = Начислено − Соцфонд − ПН;  Аванс = min(round100(Оклад × 40%), На руки);
  К выплате = На руки − Аванс;  Взнос работодателя = round(Начислено × 17,25%).
"""
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import Employee, Expense, Payroll, PayrollLine
from .charges import month_end
from .expenses import SALARY_CATEGORY, category_by_name, clamp_day
from .money import round_int, to_decimal

HUNDRED_SOM = 10000  # тыйынов


def calc_line(salary: int, bonus: int, s) -> dict:
    pct = lambda v: to_decimal(v) / 100
    accrued = int(salary) + int(bonus)
    social = round_int(accrued * pct(s.payroll_social_employee))
    income_tax = round_int((accrued - social) * pct(s.payroll_income_tax))
    net = accrued - social - income_tax
    raw = to_decimal(salary) * s.advance_percent / 100
    advance = min(round_int(raw / HUNDRED_SOM) * HUNDRED_SOM, net)
    return {"accrued": accrued, "social": social, "income_tax": income_tax, "net": net, "advance": advance,
            "to_pay": net - advance, "employer_contribution": round_int(accrued * pct(s.payroll_social_employer))}


def pay_dates(month: date, s):
    return clamp_day(month, s.advance_day), clamp_day(month, s.settle_day)


def _employees(company, month: date, branch_ids=None):
    # архив сотрудника = увольнение (fired_at задан): за месяцы работы он остаётся в ведомости и налогах
    qs = (Employee.objects.filter(company=company).filter(Q(archived_at__isnull=True) | Q(fired_at__isnull=False))
          .select_related("branch"))
    if branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)
    me = month_end(month)
    return [e for e in qs.order_by("full_name")
            if (e.hired_at is None or e.hired_at <= me) and (e.fired_at is None or e.fired_at >= month)]


def _stored_lines(company, month, branch_ids):
    qs = PayrollLine.objects.filter(company=company, payroll__month=month).select_related(
        "advance_expense", "settle_expense")
    if branch_ids is not None:
        qs = qs.filter(payroll__branch_id__in=branch_ids)
    return {ln.employee_id: ln for ln in qs}


def compute_sheet(company, month: date, branch_ids=None) -> dict:
    """Ведомость за месяц. Пока по сотруднику нет выплат — живой расчёт; после первой выплаты строка
    сохранена и цифры заморожены (смена оклада позже её не меняет)."""
    s = company.arenda_settings
    stored = _stored_lines(company, month, branch_ids)
    lines = []
    for e in _employees(company, month, branch_ids):
        st = stored.pop(e.id, None)
        if st:
            vals = {k: getattr(st, k) for k in ("salary", "bonus", "accrued", "social", "income_tax", "net",
                                                "advance", "to_pay", "employer_contribution")}
            adv_ok = bool(st.advance_expense_id and not st.advance_expense.voided_at)
            set_ok = bool(st.settle_expense_id and not st.settle_expense.voided_at)
            adv_paid, paid = st.advance_paid, st.paid
        else:
            vals = {"salary": e.salary, "bonus": e.bonus, **calc_line(e.salary, e.bonus, s)}
            adv_ok = set_ok = False
            adv_paid = paid = 0
        lines.append({"employee": str(e.id), "full_name": e.full_name, "position": e.position,
                      "branch": e.branch_id, **vals, "advance_paid": adv_paid, "paid": paid,
                      "advance_is_paid": adv_ok, "salary_is_paid": set_ok,
                      "full_cost": vals["accrued"] + vals["employer_contribution"]})
    keys = ("accrued", "social", "income_tax", "net", "advance", "to_pay", "employer_contribution",
            "advance_paid", "paid", "full_cost")
    totals = {k: sum(ln[k] for ln in lines) for k in keys}
    adv_date, set_date = pay_dates(month, s)
    return {"month": month.strftime("%Y-%m"), "advance_date": adv_date, "settle_date": set_date, "lines": lines,
            "totals": totals, "staff_cost": totals["full_cost"],
            "rates": {"social_employee": str(s.payroll_social_employee), "income_tax": str(s.payroll_income_tax),
                      "social_employer": str(s.payroll_social_employer), "advance_percent": s.advance_percent}}


def public_sheet(sheet: dict) -> dict:
    out = dict(sheet)
    out["advance_date"], out["settle_date"] = sheet["advance_date"].isoformat(), sheet["settle_date"].isoformat()
    out["lines"] = [{**ln, "branch": str(ln["branch"])} for ln in sheet["lines"]]
    return out


@transaction.atomic
def pay(company, month: date, kind: str, branch_ids=None, employee_ids=None, method="cash", user=None) -> dict:
    """Выплата аванса (kind='advance') или зарплаты ('salary'): расход в «Зарплата», один раз на
    сотрудника+месяц+вид. Возвращает {created: n, skipped: n}."""
    s = company.arenda_settings
    adv_date, set_date = pay_dates(month, s)
    category = category_by_name(company, SALARY_CATEGORY)
    created = skipped = 0
    paid_info = []
    for e in _employees(company, month, branch_ids):
        if employee_ids is not None and str(e.id) not in employee_ids:
            continue
        payroll, _ = Payroll.objects.get_or_create(
            company=company, branch=e.branch, month=month,
            defaults={"advance_date": adv_date, "settle_date": set_date, "created_by": user})
        line = PayrollLine.objects.select_for_update().filter(payroll=payroll, employee=e).first()
        if line is None:
            line = PayrollLine.objects.create(company=company, payroll=payroll, employee=e, salary=e.salary,
                                              bonus=e.bonus, created_by=user, **calc_line(e.salary, e.bonus, s))
        fk, amount_f, label, when = (("advance_expense", "advance_paid", "Аванс", adv_date) if kind == "advance"
                                     else ("settle_expense", "paid", "Зарплата", set_date))
        amount = line.advance if kind == "advance" else line.to_pay
        current = getattr(line, fk)
        if (current is not None and not current.voided_at) or amount <= 0:
            skipped += 1
            continue
        exp = Expense.objects.create(
            company=company, branch=e.branch, category=category, date=when, amount=amount, method=method,
            description=f"{label} за {month.strftime('%Y-%m')}: {e.full_name}"[:255], created_by=user)
        setattr(line, fk, exp)
        setattr(line, amount_f, amount)
        line.save(update_fields=[fk, amount_f, "updated_at"])
        created += 1
        paid_info.append({"employee": str(e.id), "amount": amount, "expense": str(exp.id)})
        _refresh_status(payroll)
    return {"created": created, "skipped": skipped, "payments": paid_info}


def _refresh_status(payroll: Payroll):
    lines = list(payroll.lines.select_related("advance_expense", "settle_expense"))
    if lines and all(ln.settle_expense_id or ln.to_pay == 0 for ln in lines):
        st = "closed"
    elif any(ln.advance_expense_id for ln in lines):
        st = "advance_paid"
    else:
        st = "draft"
    if payroll.status != st:
        payroll.status = st
        payroll.save(update_fields=["status", "updated_at"])
