"""Расходы, налоговый календарь, амортизация, итог месяца (ТЗ §3.6, §5.4). Всё в целых тыйынах."""
import calendar
from datetime import date

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ..models import Asset, Charge, Expense, ExpenseCategory, Payment, TaxPayment
from .charges import month_end, month_start

TAX_CATEGORY = "Налоги"
SALARY_CATEGORY = "Зарплата"
TAX_DUE_DAY = 25  # налоги с зарплаты платятся до 25-го числа следующего месяца


def parse_month(raw, default=None) -> date:
    """'YYYY-MM' (или 'YYYY-MM-DD') -> первое число месяца."""
    if not raw:
        return month_start(default or timezone.localdate())
    try:
        y, m = str(raw)[:7].split("-")
        return date(int(y), int(m), 1)
    except (ValueError, TypeError):
        raise ValidationError({"month": ["Ожидается формат ГГГГ-ММ."]})


def add_months(d: date, n: int) -> date:
    idx = d.year * 12 + (d.month - 1) + n
    return date(idx // 12, idx % 12 + 1, 1)


def clamp_day(month: date, day: int) -> date:
    """День месяца с учётом его длины; 0 = последний день."""
    last = calendar.monthrange(month.year, month.month)[1]
    return date(month.year, month.month, last if day <= 0 else min(day, last))


# ---------------------------------------------------------------- амортизация


def months_elapsed(asset_purchase: date, month: date) -> int:
    """Сколько месяцев амортизации накоплено на конец month. Начисление идёт со следующего месяца после покупки."""
    return (month.year - asset_purchase.year) * 12 + (month.month - asset_purchase.month)


def accumulated_depreciation(cost: int, life_years: int, k: int) -> int:
    """Накопленная амортизация за k месяцев: floor(cost*k/N), N = лет*12. На k>=N = cost (остаток 0)."""
    n = max(int(life_years), 1) * 12
    k = max(0, min(k, n))
    return cost * k // n


def depreciation_for_month(asset, month: date) -> int:
    """Амортизация конкретного месяца. Линейно; остаток тыйынов от деления уходит в последние месяцы,
    сумма за весь срок точно равна стоимости, после срока — 0."""
    k = months_elapsed(asset.purchase_date, month)
    if k < 1:
        return 0
    return accumulated_depreciation(asset.cost, asset.life_years, k) - accumulated_depreciation(
        asset.cost, asset.life_years, k - 1)


def asset_state(asset, month: date) -> dict:
    k = months_elapsed(asset.purchase_date, month)
    acc = accumulated_depreciation(asset.cost, asset.life_years, k)
    return {"monthly": depreciation_for_month(asset, month), "accumulated": acc, "residual": asset.cost - acc}


def total_depreciation(company, month: date, branch_ids=None) -> int:
    qs = Asset.objects.filter(company=company, archived_at__isnull=True)
    if branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)
    return sum(depreciation_for_month(a, month) for a in qs)


# ---------------------------------------------------------------- расходы


def expense_qs(company, branch_ids=None):
    qs = Expense.objects.filter(company=company)
    if branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)
    return qs


def category_by_name(company, name) -> ExpenseCategory:
    cat, created = ExpenseCategory.objects.get_or_create(company=company, name=name, defaults={"is_system": True})
    if cat.archived_at:
        cat.archived_at = None
        cat.save(update_fields=["archived_at"])
    return cat


def expenses_by_category(company, month: date, branch_ids=None) -> dict:
    qs = expense_qs(company, branch_ids).filter(voided_at__isnull=True, date__gte=month, date__lte=month_end(month))
    rows = qs.values("category_id", "category__name").annotate(s=Sum("amount")).order_by("category__name")
    items = [{"category": str(r["category_id"]), "name": r["category__name"], "total": int(r["s"])} for r in rows]
    return {"month": month.strftime("%Y-%m"), "total": sum(i["total"] for i in items), "by_category": items}


@transaction.atomic
def void_expense(expense: Expense, reason: str) -> Expense:
    """Сторно расхода. Связанные выплаты/налоги снова становятся неоплаченными."""
    if expense.voided_at:
        return expense
    expense.voided_at = timezone.now()
    expense.void_reason = reason
    expense.save(update_fields=["voided_at", "void_reason", "updated_at"])
    from ..models import PayrollLine
    for line in PayrollLine.objects.filter(advance_expense=expense):
        line.advance_expense, line.advance_paid = None, 0
        line.save(update_fields=["advance_expense", "advance_paid", "updated_at"])
    for line in PayrollLine.objects.filter(settle_expense=expense):
        line.settle_expense, line.paid = None, 0
        line.save(update_fields=["settle_expense", "paid", "updated_at"])
    TaxPayment.objects.filter(expense=expense).update(paid=False, paid_at=None, expense=None)
    return expense


# ---------------------------------------------------------------- налоговый календарь


@transaction.atomic
def pay_tax(tax: TaxPayment, *, branch, method="cash", paid_date=None, user=None) -> Expense:
    """Отметка «оплачено» создаёт расход в «Налоги» (один раз)."""
    if tax.paid and tax.expense_id and not tax.expense.voided_at:
        return tax.expense
    exp = Expense.objects.create(
        company=tax.company, branch=branch, category=category_by_name(tax.company, TAX_CATEGORY),
        date=paid_date or timezone.localdate(), amount=tax.amount, method=method,
        description=tax.tax[:255], created_by=user)
    tax.paid, tax.paid_at, tax.expense = True, timezone.now(), exp
    tax.save(update_fields=["paid", "paid_at", "expense", "updated_at"])
    return exp


def sync_payroll_taxes(company, month: date, branch_ids=None, user=None) -> None:
    """Удержания (Соцфонд работника, подоходный) и взнос работодателя за месяц -> налоговый календарь.
    Идемпотентно: ключ dedupe_key; оплаченные записи не трогаем, у неоплаченных обновляем сумму."""
    from .payroll import compute_sheet
    sheet = compute_sheet(company, month, branch_ids)
    per_branch = {}
    for ln in sheet["lines"]:
        b = per_branch.setdefault(ln["branch"], {"social": 0, "income_tax": 0, "employer": 0})
        b["social"] += ln["social"]
        b["income_tax"] += ln["income_tax"]
        b["employer"] += ln["employer_contribution"]
    due = clamp_day(add_months(month, 1), TAX_DUE_DAY)
    ym = month.strftime("%Y-%m")
    titles = {
        "social": f"Соцфонд (удержание с работников) за {ym}",
        "income_tax": f"Подоходный налог за {ym}",
        "employer": f"Соцфонд (взнос работодателя) за {ym}",
    }
    from apps.users.models import Branch
    for branch_id, sums in per_branch.items():
        for kind, amount in sums.items():
            key = f"pr:{ym}:{kind}:{branch_id.hex}"
            existing = TaxPayment.objects.filter(company=company, dedupe_key=key).first()
            if existing is None:
                if amount > 0:
                    TaxPayment.objects.create(
                        company=company, branch=Branch.objects.get(id=branch_id), tax=titles[kind], due_date=due,
                        amount=amount, source="payroll", dedupe_key=key, created_by=user)
            elif not existing.paid and existing.amount != amount:
                existing.amount = amount
                existing.save(update_fields=["amount", "updated_at"])


# ---------------------------------------------------------------- итог месяца


def month_summary(company, month: date, branch_ids=None) -> dict:
    """Прибыль = начислено + прочий доход − расходы − амортизация (ТЗ §5.4)."""
    me = month_end(month)
    cq = Charge.objects.filter(company=company, voided_at__isnull=True, period=month)
    pq = Payment.objects.filter(company=company, status="posted", tenant__isnull=True,
                                paid_at__date__gte=month, paid_at__date__lte=me)
    eq = expense_qs(company, branch_ids).filter(voided_at__isnull=True, date__gte=month, date__lte=me)
    if branch_ids is not None:
        cq, pq = cq.filter(branch_id__in=branch_ids), pq.filter(branch_id__in=branch_ids)
    charged = int(cq.aggregate(s=Sum("amount"))["s"] or 0)
    other = int(pq.aggregate(s=Sum("amount"))["s"] or 0)
    expenses = int(eq.aggregate(s=Sum("amount"))["s"] or 0)
    depreciation = total_depreciation(company, month, branch_ids)
    return {"month": month.strftime("%Y-%m"), "charged": charged, "other_income": other, "expenses": expenses,
            "depreciation": depreciation, "net_profit": charged + other - expenses - depreciation}
