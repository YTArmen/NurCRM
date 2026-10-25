"""
Чистые функции долга, статуса и пени (ТЗ §5.2). Без обращения к БД.

Модель: все начисления (не аннулированные), отсортированные по сроку, закрываются
проведёнными оплатами по очереди «самые старые первыми»; остаток оплат — аванс.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional, Tuple

from .money import round_int, to_decimal

SOON_DAYS = 7
LEASE_EXPIRING_DAYS = 45

OK, DUE_SOON, OVERDUE = "ok", "due_soon", "overdue"


@dataclass(frozen=True)
class DebtInfo:
    debt: int = 0              # начисления с наступившим сроком минус оплаты (>= 0)
    advance: int = 0           # переплата (аванс)
    status: str = OK
    overdue_days: int = 0      # от самого старого неоплаченного срока
    overdue_base: int = 0      # просроченный остаток без пени (база для расчёта пени)
    oldest_due: Optional[date] = None


def allocate_fifo(charges: Iterable[Tuple[date, int, str]], paid_total: int):
    """charges: (due_date, amount, kind). Возвращает ([(due, kind, остаток)], аванс)."""
    items = sorted(charges, key=lambda c: c[0])
    left = int(paid_total)
    out = []
    for due, amount, kind in items:
        covered = min(left, amount) if amount > 0 else 0
        left -= covered
        out.append((due, kind, amount - covered))
    return out, left


def compute_debt(charges: Iterable[Tuple[date, int, str]], paid_total: int, today: date,
                 soon_days: int = SOON_DAYS) -> DebtInfo:
    remaining, advance = allocate_fifo(charges, paid_total)
    debt = 0
    overdue_base = 0
    oldest = None
    soon = False
    for due, kind, rest in remaining:
        if rest <= 0:
            continue
        if due <= today:
            debt += rest
        if due < today:
            if oldest is None:
                oldest = due
            if kind != "penalty":
                overdue_base += rest
        elif due <= today + timedelta(days=soon_days):
            soon = True
    if oldest is not None:
        status, days = OVERDUE, (today - oldest).days
    elif soon:
        status, days = DUE_SOON, 0
    else:
        status, days = OK, 0
    return DebtInfo(debt=debt, advance=advance, status=status, overdue_days=days,
                    overdue_base=overdue_base, oldest_due=oldest)


def calc_penalty(debt: int, rate_percent, days: int) -> int:
    """Пеня = долг × ставка в день (%) × дни просрочки. Только расчёт, в долг не попадает."""
    if debt <= 0 or days <= 0:
        return 0
    return round_int(to_decimal(debt) * to_decimal(rate_percent) / Decimal(100) * days)


def lease_display_status(stored: str, start: date, end: Optional[date], today: date,
                         expiring_days: int = LEASE_EXPIRING_DAYS) -> str:
    if stored in ("draft", "terminated"):
        return stored
    if end is not None:
        if end < today:
            return "expired"
        if (end - today).days <= expiring_days:
            return "expiring"
    return "active"


def lease_days_left(end: Optional[date], today: date) -> Optional[int]:
    """Дней до конца договора; None для бессрочного или истёкшего (в UI «истёк»)."""
    if end is None or end < today:
        return None
    return (end - today).days
