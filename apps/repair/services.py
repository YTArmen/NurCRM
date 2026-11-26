import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db.models import Max
from django.utils import timezone

Q2 = Decimal("0.01")
Z = Decimal("0.00")
CASH_METHODS = ("cash",)


def money(v, allow_zero=False):
    try:
        d = Decimal(str(v).replace(",", ".")).quantize(Q2, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if d < 0 or (d == 0 and not allow_zero):
        return None
    return d


def norm_serial(s):
    return re.sub(r"[\s\-]", "", str(s or "")).upper()[:128]


def next_number(model, company):
    """Нумерация по компании (решение 15.11, п.1). Вызывать внутри транзакции с select_for_update по компании."""
    return (model.objects.filter(company=company).aggregate(m=Max("number"))["m"] or 0) + 1


def lock_company(company):
    from apps.users.models import Company

    Company.objects.select_for_update().filter(pk=company.pk).first()


def open_shift(user, company, shift_id=None):
    from apps.construction.models import CashShift

    qs = CashShift.objects.select_related("cashbox", "branch").filter(company=company, status=CashShift.Status.OPEN)
    if shift_id:
        return qs.filter(pk=shift_id).first()
    return qs.filter(cashier=user).order_by("-opened_at").first()


def cash_move(*, company, user, shift, direction, amount, method, source_id, name, key):
    """Внесение (in) или изъятие (out) в смене — вариант А решения 15.11 (п.2). Безнал — мимо ящика смены."""
    from apps.construction.auto_cashflow import create_auto_cashflow
    from apps.construction.models import CashFlow

    cash = method in CASH_METHODS
    # Проверка соседней сессии 15.11: не SHIFT_DRAWER_INFLOW/OUTFLOW (отчёт смены показал бы их ручными «Внесениями»),
    # а MANUAL — в отчёте смены это «прочие» приходы/расходы ящика, expected_cash считается верно.
    # Безнал — без смены (shift=None): в ящик смены и в его отчёт не попадает.
    return create_auto_cashflow(
        company=company, branch=getattr(shift, "branch", None), cashbox=getattr(shift, "cashbox", None), user=user,
        shift=shift if cash else None, type=CashFlow.Type.INCOME if direction == "in" else CashFlow.Type.EXPENSE, amount=amount,
        source_kind=CashFlow.SourceKind.MANUAL, source_id=str(source_id), name=name,
        affects_shift_drawer=bool(cash and shift is not None), detached=not cash,
        idempotency_key=key, payment_method="cash" if cash else (method or "transfer"),
    )


def hist(obj, user, action, **extra):
    obj.history = (obj.history or []) + [{"at": timezone.now().isoformat(), "by": str(user.pk) if user else None,
                                          "action": action, **extra}]
