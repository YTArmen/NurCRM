from __future__ import annotations

from typing import Optional

# Допустимые способы оплаты согласно debt-payment-method-backend.md
VALID_DEBT_PAYMENT_METHODS = {
    "cash",
    "mbank",
    "optima",
    "obank",
    "bakai",
    "demir",
    "other",
    "transfer",
}

DEBT_PAYMENT_METHODS = VALID_DEBT_PAYMENT_METHODS
DEBT_PAYMENT_DEFAULT = "cash"
DEBT_PAYMENT_FALLBACK = "transfer"


def normalize_debt_payment_method(raw: Optional[str]) -> str:
    """
    Нормализует payment_method согласно debt-payment-method-backend.md:
    "cash" | "mbank" | "optima" | "obank" | "bakai" | "demir" | "other" | "transfer"

    - Отсутствие / None / "" / "   " -> "cash" (по умолчанию, сохранение совместимости)
    - Допустимое значение -> точное значение
    - Неизвестное значение -> "transfer" (безнал, не падает 400 согласно вопросу D2)
    """
    if raw is None:
        return DEBT_PAYMENT_DEFAULT
    val = str(raw).strip().lower()
    if not val:
        return DEBT_PAYMENT_DEFAULT
    if val in VALID_DEBT_PAYMENT_METHODS:
        return val
    return DEBT_PAYMENT_FALLBACK


# Способы предоплаты при продаже в долг согласно debt-prepayment-method-backend.md
VALID_DEBT_PREPAYMENT_METHODS = {
    "cash",
    "mbank",
    "optima",
    "obank",
    "bakai",
    "transfer",
}


def normalize_debt_prepayment_method(raw: Optional[str]) -> str:
    """
    Нормализует prepayment_method согласно debt-prepayment-method-backend.md:
    "cash" | "mbank" | "optima" | "obank" | "bakai" | "transfer"

    - Отсутствие / None / "" / "   " -> "cash" (по умолчанию, старые клиенты)
    - Допустимое значение -> точное значение
    - Неизвестное значение -> "transfer" (не 400)
    """
    if raw is None:
        return "cash"
    val = str(raw).strip().lower()
    if not val:
        return "cash"
    if val in VALID_DEBT_PREPAYMENT_METHODS:
        return val
    return "transfer"


def pick_debt_method(data):
    """ТЗ Н-13: способ оплаты из `method` или `payment_method`.

    Оба заданы и различаются → 400 field_conflict. Возвращает строку или None.
    """
    from core.exceptions import ApiError

    def _clean(v):
        return str(v).strip().lower() if v not in (None, "") and str(v).strip() else None

    m = _clean(data.get("method"))
    pm = _clean(data.get("payment_method"))
    if m and pm and m != pm:
        raise ApiError(
            "field_conflict",
            "Поля method и payment_method заданы с разными значениями — передайте одно.",
            data={"method": m, "payment_method": pm},
            top={"method": m, "payment_method": pm},
        )
    return m or pm


def user_display_name(user):
    if user is None:
        return None
    return (
        getattr(user, "get_full_name", lambda: "")()
        or getattr(user, "email", None)
        or getattr(user, "username", None)
        or str(user.pk)
    )


def debt_receipt_info(user, cashflows, payments=None):
    """ТЗ Н-13: received_by / received_at / shift / cashbox для ответа об оплате долга.

    `payments` — созданные (или, при повторе, прежние) DealPayment: из них берём
    того, кто принял, и время; без них — текущий пользователь и «сейчас».
    """
    from django.utils import timezone

    payments = list(payments or [])
    receiver = (payments[0].created_by if payments and payments[0].created_by_id else None) or user
    received_at = payments[0].created_at if payments and getattr(payments[0], "created_at", None) else timezone.now()
    cf = next((c for c in cashflows if c is not None), None)
    shift_cf = next((c for c in cashflows if c is not None and c.shift_id), None)
    return {
        "received_by": {"id": str(receiver.pk), "name": user_display_name(receiver)} if receiver is not None else None,
        "received_at": received_at.isoformat(),
        "shift": str(shift_cf.shift_id) if shift_cf else None,
        "cashbox": str(cf.cashbox_id) if cf and cf.cashbox_id else None,
    }


def debt_detached_client(request, company=None) -> bool:
    """ТЗ 29.10 Б-13: можно ли владельцу принять долг «вне смены» (shift=NULL).
    Да — программа владельца, мобилка и сайт (заголовок X-NurMarket-Client), а также когда у него нет
    своей открытой смены (деньги больше некуда положить). Касса и старые клиенты без заголовка при
    открытой смене владельца — как раньше, в эту смену: владелец часто сам стоит за кассой, а старая
    касса shift не присылает."""
    from core.api_compat import parse_client

    try:
        kind = parse_client(request)[0]
    except Exception:  # noqa: BLE001
        kind = None
    if kind in ("owner", "mobile", "site"):
        return True
    from apps.construction.models import CashShift

    qs = CashShift.objects.filter(cashier=request.user, status=CashShift.Status.OPEN)
    if company is not None:
        qs = qs.filter(company=company)
    return not qs.exists()
