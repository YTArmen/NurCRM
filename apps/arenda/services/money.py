"""Деньги: всё в целых тыйынах."""
from decimal import ROUND_HALF_UP, Decimal


def to_decimal(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def round_int(x) -> int:
    """Округление к целому «по-школьному» (0.5 -> 1)."""
    return int(to_decimal(x).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calc_rent(area, rate: int) -> int:
    """Аренда в месяц = площадь (м²) × ставка за м² (тыйыны)."""
    return round_int(to_decimal(area) * int(rate))
