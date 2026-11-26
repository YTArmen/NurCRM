# apps/main/api/utils.py
from typing import Optional

from django.utils.timezone import localtime
from decimal import Decimal

from apps.main.models import Sale


def _to_float(x):
    try:
        if isinstance(x, Decimal):
            return float(x)
        return float(x or 0)
    except Exception:
        return 0.0

def _pick(*vals, default=None):
    for v in vals:
        if v is not None:
            return v
    return default


def _user_display_name(user) -> Optional[str]:
    """Имя для чека: ФИО, иначе email / username."""
    if not user:
        return None
    full = f"{(getattr(user, 'first_name', '') or '').strip()} {(getattr(user, 'last_name', '') or '').strip()}".strip()
    if full:
        return full
    try:
        fn = (user.get_full_name() or "").strip()
        if fn and fn != getattr(user, "email", ""):
            return fn
    except Exception:
        pass
    for attr in ("email", "username", "full_name"):
        v = getattr(user, attr, None)
        if v and str(v).strip():
            return str(v).strip()
    return None


def _receipt_paid_cash_card(sale: Sale) -> tuple:
    """
    Суммы для JSON чека.
    Если есть строки SalePayment — берём фактические суммы по методам.
    Иначе — legacy-логика по payment_method.
    """
    if hasattr(sale, "cash_payment_amount") and hasattr(sale, "noncash_payment_amount"):
        lines = sale.payment_lines()
        if lines:
            return _to_float(sale.cash_payment_amount()), _to_float(sale.noncash_payment_amount())

    total_f = _to_float(getattr(sale, "total", 0))
    pm = getattr(sale, "payment_method", None) or Sale.PaymentMethod.CASH
    if pm == Sale.PaymentMethod.DEBT:
        return 0.0, 0.0
    if pm == Sale.PaymentMethod.CASH:
        return total_f, 0.0
    return 0.0, total_f


def _receipt_payments_payload(sale: Sale) -> list:
    lines = sale.payment_lines() if hasattr(sale, "payment_lines") else []
    out = []
    for line in lines:
        try:
            method_display = line.get_method_display()
        except Exception:
            method_display = str(line.method)
        out.append(
            {
                "method": line.method,
                "method_display": method_display,
                "amount": _to_float(line.amount),
            }
        )
    return out


def build_receipt_payload(sale, cashier_name=None, *, ensure_number: bool = True):
    """
    Формирует JSON для печати чека.

    - Всегда возвращает корректный doc_no.
      Если ensure_number=True, сначала присваивает сквозной номер (ensure_sale_doc_number).
    - Все денежные/числовые поля нормализуются в float.
    - Строки — в Unicode (JSON отдается UTF-8).
    - Добавлен флаг 'encoding': 'utf-8' для фронта.
    - Кассир: если cashier_name не передан — берётся с sale.user (кто оформил продажу).
    - Оплата: paid_cash / paid_card заполняются из payment_method и total (у Sale нет отдельных полей в БД).
    """
    # 1) гарантируем номер чека
    doc_no = str(getattr(sale, "doc_no", "") or "")
    if ensure_number:
        try:
            # скорректируй импорт под свой проект, если путь другой
            from apps.main.utils_numbers import ensure_sale_doc_number
            doc_no = str(ensure_sale_doc_number(sale))
        except Exception:
            # fallback: id продажи
            doc_no = doc_no or str(sale.id)
    else:
        doc_no = doc_no or str(sale.id)

    # 2) позиции
    items = []
    for it in sale.items.all():
        name = _pick(
            getattr(it, "name_snapshot", None),
            getattr(it, "name", None),
            getattr(getattr(it, "product", None), "name", None),
            default="Товар"
        )
        qty = _to_float(_pick(
            getattr(it, "quantity", None),
            getattr(it, "qty", None),
            getattr(it, "count", None),
            default=1
        ))
        price = _to_float(_pick(
            getattr(it, "unit_price", None),
            getattr(it, "price", None),
            default=0
        ))
        line_disc = _to_float(getattr(it, "line_discount", 0))
        # rcp2611: сумма строки по правилу чека (весовая — вниз до шага весов, остальные — до копейки), без хвостов float
        try:
            from apps.main.models import line_total_like_check
            line_total = float(line_total_like_check(it))
        except Exception:
            line_total = round(qty * price - line_disc, 2)
        price_manually_edited = bool(getattr(it, "price_manually_edited", False))
        price_override_reason = "manual" if price_manually_edited else ("discount" if line_disc > 0 else None)
        items.append(
            {
                "id": str(it.id),
                "item_id": str(it.id),
                "line_id": str(it.id),
                "sale_item_id": str(it.id),
                "name": str(name),
                "qty": qty,
                "quantity": qty,
                "price": price,
                "unit_price": price,
                "line_discount": line_disc,
                "line_total": line_total,
                "total": line_total,
                "price_manually_edited": price_manually_edited,
                "price_override_reason": price_override_reason,
            }
        )

    # 3) шапка/итоги
    created_at = getattr(sale, "created_at", None)
    from apps.main.receipt_header import receipt_vendor_header

    vh = receipt_vendor_header(sale)
    company_name = vh.get("brand") or ""

    resolved_cashier = (str(cashier_name).strip() if cashier_name is not None else "") or _user_display_name(
        getattr(sale, "user", None)
    )

    paid_cash, paid_card = _receipt_paid_cash_card(sale)
    pm = getattr(sale, "payment_method", None) or Sale.PaymentMethod.CASH
    try:
        pm_display = sale.get_payment_method_display()
    except Exception:
        pm_display = str(pm)

    cash_received_val = _to_float(getattr(sale, "cash_received", 0))
    cash_portion = _to_float(sale.cash_payment_amount()) if hasattr(sale, "cash_payment_amount") else paid_cash

    consultant_user = getattr(sale, "consultant", None)
    consultant_name = _user_display_name(consultant_user) if consultant_user else None

    payload = {
        # метка кодировки для фронта (браузерный клиент сможет выбрать UTF-8)
        "encoding": "utf-8",

        "doc_no": doc_no,
        "company": company_name,
        "inn": vh.get("inn") or None,
        "address": vh.get("address") or None,
        "created_at": localtime(created_at).strftime("%Y-%m-%d %H:%M:%S") if created_at else None,
        "cashier_name": resolved_cashier,
        "consultant_name": consultant_name,
        "cashier": {"id": str(sale.user_id) if getattr(sale, "user_id", None) else None, "name": resolved_cashier},
        "consultant": {"id": str(sale.consultant_id), "name": consultant_name} if consultant_user else None,

        "items": items,

        "discount": _to_float(
            getattr(sale, "discount", getattr(sale, "discount_total", 0))
        ),
        "tax": _to_float(
            getattr(sale, "tax", getattr(sale, "tax_total", 0))
        ),
        "payment_method": pm,
        "payment_method_display": pm_display,
        "payments": _receipt_payments_payload(sale),
        "paid_cash": paid_cash,
        "paid_card": paid_card,
        "cash_received": cash_received_val if cash_portion > 0 else 0.0,
        "change": _to_float(getattr(sale, "change", 0)),
    }

    # eKassa: отдаём то, что уже сохранено в Sale.ekassa_fiscal (если есть)
    try:
        from apps.ekassa.sale_bridge import enrich_ekassa_fiscal_with_item_ids
        ekassa_meta = enrich_ekassa_fiscal_with_item_ids(getattr(sale, "ekassa_fiscal", None), sale)
    except Exception:
        ekassa_meta = getattr(sale, "ekassa_fiscal", None)
    if ekassa_meta:
        payload["ekassa"] = ekassa_meta

    return payload
