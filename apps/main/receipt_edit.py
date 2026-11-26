"""
ТЗ редактор накладных 30.10 (вариант А; раньше — краткий пункт ТЗ 31.10 п.21): правка проведённого прихода.

PATCH /api/main/suppliers/receipts/<uuid>/
  Заголовки: Idempotency-Key — обязателен (кроме dry_run); If-Match: "<version>" (или "version" в теле).
  Тело:
    version, reason (обязательно, 5–500 знаков; для dry_run — нет),
    reason_code: qty_typo | price_typo | wrong_product | supplier_corrected | short_delivery | other,
    dry_run (или ?dry_run=1) — только посчитать, ничего не менять, ключ повтора не занимается,
    supplier_id, document_number, document_date (YYYY-MM-DD | null), comment,
    payment: {type: cash|debt|partial, paid_amount, cashbox_id, shift_id, due_date},
    lines: [{id, qty, purchase_price} | {id, delete: true} | {product, qty, purchase_price, client_row_id}]
           (только изменённые строки; старое имя "items" — синоним; line_total строки тоже понимаем),
    update_product_purchase_price: if_latest (по умолчанию) | always | never,
    source: kassa|owner|web|phone, device: "Касса-1".

  Ответ 200: {receipt, deltas{stock[], purchase_price[], total, supplier_debt, cashflows[]}, movements[],
              warnings[], replayed, dry_run}. Повтор тем же ключом — тот же ответ, replayed: true.

GET  suppliers/receipts/<id>/versions/        — список версий (версия 1 — исходный документ)
GET  suppliers/receipts/<id>/versions/<n>/    — снимок версии
POST suppliers/receipts/<id>/revert/ {to_version, reason, reason_code?, version?, dry_run?} — откат = новая правка
GET  suppliers/receipts/changes/?since=       — лента правок приходов (receipt_changed)

Проверки по порядку: ключ повтора → право → причина → окно → версия → период → возвраты → строки → деньги.
Ошибки — core.exceptions.ApiError {code, detail, data}:
  403 permission_denied {required}           — нет права (сейчас только владелец)
  400 reason_required                        — нет причины 5–500 знаков
  400 idempotency_key_required               — нет Idempotency-Key (кроме dry_run)
  409 edit_window_expired {created_at, window, expired_at}
  412 (If-Match) / 409 (version в теле) version_conflict {current_version, edited_by, edited_at}
  423 period_closed {closed_until}
  409 receipt_fully_returned
  409 has_returns {lines: [{id, returned_qty}]} — построчно: кол-во ниже возвращённого, смена цены, удаление;
      смена поставщика/оплаты при любом возврате
  409 negative_stock {items: [{product_id, name, stock_now, qty_delta, min_qty}]}
  409 debt_already_paid {deal_id, paid, new_total}
  409 variants_not_supported                 — кол-во строки товара с вариантами (размер/цвет)
  409 idempotency_conflict                   — тот же ключ с другим телом (apps/integrations/idempotency.py)
  422 validation_error / fractional_not_allowed {fields}

Окно правки: Company.receipt_edit_window ("24h" | "48h" | "always", меняет только владелец);
не задано — старое значение app-settings "nurmarket-mobile".receipt_edit_window; иначе 24h.
"""
import html
import logging
import threading
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.db.models import F, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.response import Response

from apps.main.models import (
    Client,
    ClientDeal,
    Product,
    ProductVariant,
    StockMovement,
    SupplierReceipt,
    SupplierReceiptItem,
    SupplierReceiptVersion,
    SupplierReturn,
    SupplierReturnItem,
    record_stock_movement,
)
from apps.users.permissions import PURCHASE_EDIT_FIELD as EDIT_PERMISSION
from apps.users.permissions import can_edit_purchase_documents  # noqa: F401  (импортируют и отсюда)
from core.exceptions import ApiError

logger = logging.getLogger(__name__)

EDIT_WINDOW_APP = "nurmarket-mobile"
EDIT_WINDOW_KEY = "receipt_edit_window"
EDIT_WINDOWS = {"24h": 24, "48h": 48, "always": None}
DEFAULT_EDIT_WINDOW = "24h"
REASON_CODES = ("qty_typo", "price_typo", "wrong_product", "supplier_corrected", "short_delivery", "other")
REASON_LABELS = {
    "qty_typo": "опечатка в количестве",
    "price_typo": "опечатка в цене",
    "wrong_product": "не тот товар",
    "supplier_corrected": "поставщик прислал исправленную накладную",
    "short_delivery": "недовоз",
    "other": "другое",
}
PRICE_RULES = ("if_latest", "always", "never")
PAYMENT_TYPES = {"cash": "cash", "transfer": "cash", "debt": "debt", "partial": "partial", "prepayment": "partial"}
REASON_MIN, REASON_MAX = 5, 500
FRACTION_UNITS = {"кг", "kg", "г", "гр", "g", "л", "l", "мл", "ml", "м", "m", "м2", "м²", "м3", "м³", "пог.м", "п.м"}

_Q2 = Decimal("0.01")
_Q3 = Decimal("0.001")
_Q4 = Decimal("0.0001")
_Z = Decimal("0")


# ------------------------------------------------------------------ настройки и право

def receipt_edit_window(company) -> str:
    """Действующее окно правки компании (company — объект или id).
    01.11 (Умар): срока больше нет — у кого есть право, правит без ограничения по времени. Всегда "always";
    настройка Company.receipt_edit_window хранится, но не действует."""
    return "always"
    from apps.users.models import Company, CompanyAppSettings

    if not isinstance(company, Company):
        company = Company.objects.filter(pk=company).only("id", "receipt_edit_window").first()
        if company is None:
            return DEFAULT_EDIT_WINDOW
    value = str(getattr(company, "receipt_edit_window", None) or "").strip().lower()
    if value in EDIT_WINDOWS:
        return value
    # запасное: вчерашняя настройка в общих app-settings кассы
    row = CompanyAppSettings.objects.filter(company_id=company.id, branch__isnull=True, app=EDIT_WINDOW_APP).first()
    value = str(((row.settings or {}) if row else {}).get(EDIT_WINDOW_KEY) or "").strip().lower()
    return value if value in EDIT_WINDOWS else DEFAULT_EDIT_WINDOW


def edit_window_until(receipt, window):
    hours = EDIT_WINDOWS.get(window)
    if hours is None or receipt.created_at is None:
        return None
    return receipt.created_at + timedelta(hours=hours)


def receipt_etag(receipt) -> str:
    return f'"{receipt.version}"'


def returned_by_item(item_ids):
    """{receipt_item_id: возвращено поставщику (проведённые возвраты)}."""
    if not item_ids:
        return {}
    rows = (
        SupplierReturnItem.objects.filter(receipt_item_id__in=list(item_ids), supplier_return__status="posted")
        .values("receipt_item_id").annotate(s=Sum("qty"))
    )
    return {r["receipt_item_id"]: Decimal(str(r["s"] or 0)) for r in rows}


def edit_block_reason(receipt, user, *, window=None, closed_until=None, returned=None, items=None):
    """Почему пользователь сейчас не может править приход (код) или None. Для GET (can_edit)."""
    if not can_edit_purchase_documents(user):
        return "permission_denied"
    items = list(receipt.items.all()) if items is None else items
    if returned is None:
        returned = returned_by_item([i.id for i in items])
    if items and all(returned.get(i.id, _Z) >= Decimal(str(i.qty)) for i in items):
        return "receipt_fully_returned"
    window = window or receipt_edit_window(receipt.company_id)
    until = edit_window_until(receipt, window)
    if until is not None and timezone.now() > until:
        return "edit_window_expired"
    if closed_until is not None and receipt.created_at is not None:
        from apps.warehouse import op_permissions as perms
        from apps.warehouse.services import document_local_date

        if document_local_date(receipt.created_at) <= closed_until and not perms.has_op_permission(
            user, perms.POST_CLOSED_PERIOD
        ):
            return "period_closed"
    return None


# ------------------------------------------------------------------ мелочи

def _truthy(raw):
    return raw is True or str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _s(v, q=_Q2):
    return None if v is None else f"{Decimal(str(v)).quantize(q, rounding=ROUND_HALF_UP)}"


def _user_ref(user):
    if user is None:
        return None
    name = f"{(getattr(user, 'first_name', '') or '').strip()} {(getattr(user, 'last_name', '') or '').strip()}".strip()
    return {"id": str(user.pk), "name": name or getattr(user, "email", "") or ""}


def allows_fraction(product) -> bool:
    """Дробное количество (3 знака) — только у весовых/мерных единиц."""
    if getattr(product, "is_weight", False):
        return True
    unit = (getattr(product, "unit", "") or "").strip().lower().rstrip(".")
    return unit in FRACTION_UNITS


def _parse_dec(raw, places):
    """(Decimal, None) или (None, текст ошибки)."""
    if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
        return None, "Ожидается число."
    try:
        d = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None, "Ожидается число."
    if not d.is_finite():
        return None, "Ожидается число."
    if d != d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):
        return None, f"Не больше {places} знаков после запятой."
    return d, None


def _parse_uuid(raw):
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_version(raw):
    if raw in (None, ""):
        return None
    s = str(raw).strip()
    if s.startswith("W/"):
        s = s[2:]
    s = s.strip('"').strip()
    if s.lower().startswith("v"):
        s = s[1:]
    try:
        return int(s)
    except (TypeError, ValueError):
        raise _validation({"version": ["If-Match / version — номер версии прихода."]})


def _validation(fields, code="validation_error", detail="Проверьте данные правки."):
    return ApiError(code, detail, status_code=422, data={"fields": fields}, fields=fields)


def _line_amount(qty, purchase_price, line_total, product):
    """Сумма строки без округления: как в приходе (line_total, иначе qty × закупка, иначе закупка товара)."""
    if line_total is not None:
        return Decimal(str(line_total))
    pp = purchase_price if purchase_price is not None else (getattr(product, "purchase_price", None) or _Z)
    return Decimal(str(qty)) * Decimal(str(pp))


def _total(amounts):
    return sum(amounts, _Z).quantize(_Q2, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ разбор тела

def parse_body(data, query_params=None):
    data = data if hasattr(data, "get") else {}
    qp = query_params or {}
    fields = {}
    out = {"dry_run": _truthy(qp.get("dry_run")) or _truthy(data.get("dry_run"))}
    out["version"] = data.get("version")
    out["reason"] = str(data.get("reason") or "").strip()
    rc = str(data.get("reason_code") or "").strip().lower()
    if rc and rc not in REASON_CODES:
        fields["reason_code"] = ["Допустимо: " + ", ".join(REASON_CODES) + "."]
    out["reason_code"] = rc or ("other" if out["reason"] else "")
    rule = str(data.get("update_product_purchase_price") or "if_latest").strip().lower()
    if rule not in PRICE_RULES:
        fields["update_product_purchase_price"] = ["Допустимо: if_latest, always, never."]
    out["price_rule"] = rule
    out["source"] = str(data.get("source") or "").strip().lower()[:16]
    out["device"] = str(data.get("device") or "").strip()[:128]

    header = {}
    if "document_number" in data:
        header["document_number"] = str(data.get("document_number") or "").strip()[:64]
    if "document_date" in data:
        raw = data.get("document_date")
        if raw in (None, ""):
            header["document_date"] = None
        else:
            d = parse_date(str(raw)[:10])
            if d is None:
                fields["document_date"] = ["Дата накладной: YYYY-MM-DD."]
            header["document_date"] = d
    if "comment" in data:
        header["comment"] = str(data.get("comment") or "")
    out["header"] = header

    # 01.11: скидка шапки — сумма | "10%" | discount_percent; пустое/0 = убрать скидку
    out["discount_req"] = None
    out["discount_rescale"] = data.get("discount_rescale") is None or _truthy(data.get("discount_rescale"))
    out["update_sale"] = data.get("update_product_sale_price") is None or _truthy(data.get("update_product_sale_price"))
    if "discount" in data or "discount_percent" in data:
        raw_d, raw_p = data.get("discount"), data.get("discount_percent")
        money, pct = _Z, None
        if isinstance(raw_d, str) and raw_d.strip().endswith("%"):
            pct, err = _parse_dec(raw_d.strip()[:-1], 2)
            if err or pct < 0 or pct > 100:
                fields["discount"] = [err or "Процент от 0 до 100."]
        elif raw_p not in (None, ""):
            pct, err = _parse_dec(raw_p, 2)
            if err or pct < 0 or pct > 100:
                fields["discount_percent"] = [err or "Процент от 0 до 100."]
        elif raw_d not in (None, ""):
            money, err = _parse_dec(raw_d, 2)
            if err or money < 0:
                fields["discount"] = [err or "Скидка не может быть отрицательной."]
        out["discount_req"] = {"money": money, "pct": pct}

    out["supplier_id"] = None
    if data.get("supplier_id") not in (None, ""):
        sid = _parse_uuid(data.get("supplier_id"))
        if sid is None:
            fields["supplier_id"] = ["Ожидается id поставщика."]
        out["supplier_id"] = sid

    out["payment"] = None
    pay = data.get("payment")
    if pay not in (None, "", {}):
        if not isinstance(pay, dict):
            fields["payment"] = ["Ожидается объект {type, paid_amount, cashbox_id, shift_id, due_date}."]
        else:
            ptype = PAYMENT_TYPES.get(str(pay.get("type") or "").strip().lower())
            p = {"type": ptype, "paid_amount": None, "cashbox_id": None, "shift_id": None, "due_date": None}
            if ptype is None:
                fields["payment.type"] = ["Допустимо: cash, debt, partial."]
            if pay.get("paid_amount") not in (None, ""):
                p["paid_amount"], err = _parse_dec(pay.get("paid_amount"), 2)
                if err or (p["paid_amount"] is not None and p["paid_amount"] < 0):
                    fields["payment.paid_amount"] = [err or "Не может быть отрицательной."]
            if ptype == "partial" and p["paid_amount"] is None and "payment.paid_amount" not in fields:
                fields["payment.paid_amount"] = ["Для partial нужна сумма оплаты."]
            for k in ("cashbox_id", "shift_id"):
                if pay.get(k) not in (None, ""):
                    p[k] = _parse_uuid(pay.get(k))
                    if p[k] is None:
                        fields[f"payment.{k}"] = ["Ожидается id."]
            if pay.get("due_date") not in (None, ""):
                p["due_date"] = parse_date(str(pay.get("due_date"))[:10])
                if p["due_date"] is None:
                    fields["payment.due_date"] = ["YYYY-MM-DD."]
            out["payment"] = p

    raw_lines = data.get("lines") if "lines" in data else data.get("items")
    lines = []
    if raw_lines not in (None, ""):
        if not isinstance(raw_lines, (list, tuple)):
            fields["lines"] = ["Ожидается список строк."]
            raw_lines = []
        for i, row in enumerate(raw_lines):
            key = f"lines[{i}]"
            if not isinstance(row, dict):
                fields[key] = ["Ожидается объект строки."]
                continue
            ln = {"index": i, "id": None, "product": None, "delete": _truthy(row.get("delete")),
                  "client_row_id": str(row.get("client_row_id") or "")[:64] or None}
            if row.get("id") not in (None, ""):
                ln["id"] = _parse_uuid(row.get("id"))
                if ln["id"] is None:
                    fields[f"{key}.id"] = ["Ожидается id строки прихода."]
            elif row.get("product") not in (None, ""):
                ln["product"] = _parse_uuid(row.get("product"))
                if ln["product"] is None:
                    fields[f"{key}.product"] = ["Ожидается id товара."]
                if ln["delete"]:
                    fields[f"{key}.delete"] = ["Удалить можно только существующую строку (id)."]
                if "qty" not in row:
                    fields[f"{key}.qty"] = ["Нужно количество."]
            else:
                fields[key] = ["Нужен id строки или product."]
                continue
            if "qty" in row and not ln["delete"]:
                ln["qty"], err = _parse_dec(row.get("qty"), 3)
                if err or ln["qty"] <= 0:
                    fields[f"{key}.qty"] = [err or "Количество больше нуля."]
            if "purchase_price" in row and not ln["delete"]:
                if row.get("purchase_price") is None and ln["id"] is not None:
                    fields[f"{key}.purchase_price"] = ["Цена закупки больше нуля."]
                elif row.get("purchase_price") is not None:
                    ln["purchase_price"], err = _parse_dec(row.get("purchase_price"), 4)
                    if err or ln["purchase_price"] <= 0:
                        fields[f"{key}.purchase_price"] = [err or "Цена закупки больше нуля."]
            if "sale_price" in row and not ln["delete"]:
                ln["sale_price"], err = _parse_dec(row.get("sale_price"), 3)
                if err or ln["sale_price"] < 0:
                    fields[f"{key}.sale_price"] = [err or "Цена продажи не может быть отрицательной."]
            if "line_total" in row and not ln["delete"]:
                if row.get("line_total") is None:
                    ln["line_total"] = None
                else:
                    ln["line_total"], err = _parse_dec(row.get("line_total"), 4)
                    if err or ln["line_total"] < 0:
                        fields[f"{key}.line_total"] = [err or "Сумма строки не может быть отрицательной."]
            lines.append(ln)
    out["lines"] = lines
    if fields:
        raise _validation(fields)
    return out


# ------------------------------------------------------------------ снимки версий

def _payment_type(receipt):
    from apps.main.views import _receipt_payment_info

    try:
        return (_receipt_payment_info([receipt]).get(str(receipt.id)) or {}).get("payment_type")
    except Exception:  # pragma: no cover
        return None


def receipt_snapshot(receipt, items=None):
    items = list(receipt.items.select_related("product").order_by("id")) if items is None else items
    lines = []
    amounts = []
    for it in items:
        amt = _line_amount(it.qty, it.purchase_price, it.line_total, it.product)
        amounts.append(amt)
        lines.append({
            "id": str(it.id), "product_id": str(it.product_id), "product_name": it.product.name,
            "unit": getattr(it.product, "unit", "") or "",
            "qty": _s(it.qty, _Q3), "purchase_price": _s(it.purchase_price, _Q4),
            "line_total": _s(it.line_total, _Q4), "amount": _s(amt),
            "sale_price": _s(it.sale_price, _Q3),
        })
    supplier = receipt.supplier
    return {
        "supplier_id": str(receipt.supplier_id),
        "supplier_name": (supplier.full_name or getattr(supplier, "title", "") or "") if supplier else "",
        "document_number": receipt.document_number or "",
        "document_date": receipt.document_date.isoformat() if receipt.document_date else None,
        "comment": receipt.comment or "",
        "payment_type": _payment_type(receipt),
        "discount": _s(receipt.discount),
        "discount_percent": _s(receipt.discount_percent),
        "total": _s(_total(amounts)),
        "lines": lines,
    }


def _ensure_base_version(receipt):
    """Версия 1 (или текущая, если приход правили до появления истории) — снимок до первой правки."""
    if SupplierReceiptVersion.objects.filter(receipt=receipt).exists():
        return
    snap = receipt_snapshot(receipt)
    first = (receipt.version or 1) <= 1
    SupplierReceiptVersion.objects.create(
        receipt=receipt, company_id=receipt.company_id, version=receipt.version or 1,
        action="create" if first else "baseline",
        at=receipt.created_at if first else (receipt.edited_at or timezone.now()),
        user_id=receipt.created_by_id if first else receipt.edited_by_id,
        total=Decimal(snap["total"]), snapshot=snap,
    )


# ------------------------------------------------------------------ правка

def edit_receipt(view, request, pk, body, *, action="edit"):
    with transaction.atomic():
        return _edit(view, request, pk, body, action)


def _version_conflict(receipt, http):
    return ApiError(
        "version_conflict", "Приход уже изменили с другого устройства. Обновите и повторите.", status_code=http,
        data={"current_version": receipt.version, "edited_by": _user_ref(receipt.edited_by),
              "edited_at": receipt.edited_at.isoformat() if receipt.edited_at else None},
    )


def _edit(view, request, pk, body, action):
    from apps.main.views import _find_receipt_deal

    user = request.user
    dry_run = body["dry_run"]
    qs = view._filter_qs_company_branch(SupplierReceipt.objects.all()).select_for_update()
    receipt = get_object_or_404(qs.exclude(deleted_receipt_ids_q()), pk=pk)  # 05.11 п.6
    company = receipt.company

    # 1. право
    if not can_edit_purchase_documents(user):
        raise ApiError("permission_denied", "Править приходы может только владелец (право «Редактор документов закупок»).",
                       status_code=403, data={"required": EDIT_PERMISSION})
    # 2. причина
    if not dry_run and not (REASON_MIN <= len(body["reason"]) <= REASON_MAX):
        raise ApiError("reason_required", f"Укажите причину правки ({REASON_MIN}–{REASON_MAX} знаков).",
                       status_code=400)
    # 3. окно
    window = receipt_edit_window(company)
    until = edit_window_until(receipt, window)
    if until is not None and timezone.now() > until:
        raise ApiError("edit_window_expired", f"Приход можно править только {EDIT_WINDOWS[window]} ч после проведения.",
                       status_code=409, data={"created_at": receipt.created_at.isoformat(), "window": window,
                                              "expired_at": until.isoformat()})
    # 4. версия: If-Match → 412, version в теле → 409
    if_match = _parse_version(request.headers.get("If-Match"))
    if if_match is not None and if_match != receipt.version:
        raise _version_conflict(receipt, status.HTTP_412_PRECONDITION_FAILED)
    body_version = _parse_version(body.get("version"))
    if if_match is None and body_version is not None and body_version != receipt.version:
        raise _version_conflict(receipt, status.HTTP_409_CONFLICT)
    # ТЗ кассы 05.11 п.7: касса/программа владельца/мобилка без версии — 428 (сайт пока без требования)
    if not dry_run and if_match is None and body_version is None and _version_required(request):
        raise ApiError("version_required", "Передайте версию прихода (If-Match или version) — иначе можно затереть чужую правку.",
                       status_code=428, data={"current_version": receipt.version})

    header = dict(body["header"])
    # 5. закрытый период склада (дата проведения, старая и новая дата накладной)
    from apps.warehouse.op_permissions import BusinessRuleError
    from apps.warehouse.services import ensure_period_open

    try:
        ensure_period_open(company=company, user=user,
                           dates=[receipt.created_at, receipt.document_date, header.get("document_date")])
    except BusinessRuleError as exc:
        raise ApiError(exc.api_code, str(exc), status_code=423, data=dict(exc.extra or {}))

    # 6. возвраты
    current_items = list(receipt.items.select_related("product").order_by("id"))
    current = {str(i.id): i for i in current_items}
    returned = returned_by_item([i.id for i in current_items])
    if current_items and all(returned.get(i.id, _Z) >= Decimal(str(i.qty)) for i in current_items):
        raise ApiError("receipt_fully_returned", "Приход полностью возвращён поставщику — править нечего.",
                       status_code=409)
    any_returns = any(v > 0 for v in returned.values()) or SupplierReturn.objects.filter(
        receipt=receipt, status=SupplierReturn.Status.POSTED).exists()

    # 7. план строк
    plan = []
    touched = set()
    fields = {}
    for ln in body["lines"]:
        key = f"lines[{ln['index']}]"
        if ln["id"] is not None:
            item = current.get(str(ln["id"]))
            if item is None or str(ln["id"]) in touched:
                fields[f"{key}.id"] = ["Нет такой строки в приходе."]
                continue
            touched.add(str(ln["id"]))
            if ln["delete"]:
                plan.append({"action": "delete", "item": item, "product_id": item.product_id, "index": ln["index"]})
                continue
            qty = ln.get("qty", Decimal(str(item.qty)))
            pp = ln["purchase_price"] if "purchase_price" in ln else item.purchase_price
            lt = ln["line_total"] if "line_total" in ln else item.line_total
            if "line_total" in ln and lt is not None and "purchase_price" not in ln:
                pp = (Decimal(str(lt)) / Decimal(str(qty))).quantize(_Q4, rounding=ROUND_HALF_UP)
            elif "qty" in ln and "line_total" not in ln and item.line_total is not None and qty != Decimal(str(item.qty)):
                lt = None  # сумма по накладной относилась к старому количеству — дальше qty × цена
            plan.append({"action": "update", "item": item, "product_id": item.product_id, "index": ln["index"],
                         "qty": qty, "purchase_price": pp, "line_total": lt,
                         **({"sale_price": ln["sale_price"]} if "sale_price" in ln else {})})
        else:
            qty = ln["qty"]
            pp = ln.get("purchase_price")
            lt = ln.get("line_total")
            if lt is not None and pp is None:
                pp = (Decimal(str(lt)) / Decimal(str(qty))).quantize(_Q4, rounding=ROUND_HALF_UP)
            plan.append({"action": "add", "item": None, "product_id": ln["product"], "index": ln["index"],
                         "qty": qty, "purchase_price": pp, "line_total": lt, "client_row_id": ln.get("client_row_id"),
                         **({"sale_price": ln["sale_price"]} if "sale_price" in ln else {})})
    if fields:
        raise _validation(fields)
    # строки, где по факту ничего не меняется, не трогаем
    plan = [p for p in plan if p["action"] != "update" or (
        p["qty"] != Decimal(str(p["item"].qty))
        or _s(p["purchase_price"], _Q4) != _s(p["item"].purchase_price, _Q4)
        or _s(p["line_total"], _Q4) != _s(p["item"].line_total, _Q4)
        or ("sale_price" in p and _s(p["sale_price"], _Q3) != _s(p["item"].sale_price, _Q3))
    )]

    # 01.11: скидка шапки — пересчёт закупки строк (как при проведении) и запись в шапку
    disc_change = None
    if body["discount_req"] is not None:
        plan, disc_change = _discount_plan(receipt, plan, current, body["discount_req"], body["discount_rescale"])

    deleted = {str(p["item"].id) for p in plan if p["action"] == "delete"}
    added = [p for p in plan if p["action"] == "add"]
    if action != "delete" and plan and len(current) - len(deleted) + len(added) <= 0:
        raise _validation({"lines": ["В приходе должна остаться хотя бы одна строка."]})

    product_ids = {p["product_id"] for p in plan}
    prod_qs = view._filter_qs_company_branch(Product.objects.all()).select_for_update()
    products = {p.id: p for p in prod_qs.filter(id__in=product_ids)} if product_ids else {}
    missing = [p for p in plan if p["product_id"] not in products]
    if missing:
        raise _validation({f"lines[{p['index']}].product": ["Товар не найден или недоступен."] for p in missing})

    frac = {f"lines[{p['index']}].qty": ["Дробное количество — только у весовых товаров (кг, л, м)."]
            for p in plan if p["action"] != "delete" and p["qty"] != p["qty"].to_integral_value()
            and not allows_fraction(products[p["product_id"]])}
    if frac:
        raise _validation(frac, code="fractional_not_allowed", detail="Дробное количество — только у весовых товаров.")

    # построчные возвраты: кол-во ниже возвращённого, смена цены/суммы, удаление
    ret_lines = []
    for p in plan:
        item = p["item"]
        if item is None:
            continue
        ret = returned.get(item.id, _Z)
        if ret <= 0:
            continue
        bad = p["action"] == "delete" or p["qty"] < ret or (
            _s(p["purchase_price"], _Q4) != _s(item.purchase_price, _Q4)
            or _s(p["line_total"], _Q4) != _s(item.line_total, _Q4)
        )
        if bad:
            ret_lines.append({"id": str(item.id), "returned_qty": _s(ret, _Q3)})
    if ret_lines:
        raise ApiError("has_returns", "По строке был возврат поставщику: цену менять и удалять строку нельзя, "
                                      "количество — не ниже возвращённого.",
                       status_code=409, data={"lines": ret_lines})

    # поставщик
    new_supplier = None
    if body["supplier_id"] is not None and body["supplier_id"] != receipt.supplier_id:
        new_supplier = (view._filter_qs_company_branch(Client.objects.all())
                        .filter(id=body["supplier_id"], type=Client.StatusClient.SUPPLIERS).first())
        if new_supplier is None:
            raise _validation({"supplier_id": ["Поставщик не найден."]})
    payment = body["payment"]
    if (new_supplier is not None or payment is not None) and any_returns:
        raise ApiError("has_returns", "По приходу были возвраты поставщику — поставщика и оплату менять нельзя.",
                       status_code=409, data={"lines": [
                           {"id": str(i.id), "returned_qty": _s(returned.get(i.id, _Z), _Q3)}
                           for i in current_items if returned.get(i.id, _Z) > 0]})

    # разница остатков по товарам
    stock_delta = {}
    for p in plan:
        old_qty = Decimal(str(p["item"].qty)) if p["item"] is not None else _Z
        new_qty = _Z if p["action"] == "delete" else p["qty"]
        p["delta"] = new_qty - old_qty
        if p["delta"]:
            stock_delta[p["product_id"]] = stock_delta.get(p["product_id"], _Z) + p["delta"]

    variant_locked = [str(pid) for pid, d in stock_delta.items()
                      if d and ProductVariant.objects.filter(product_id=pid).exists()]
    if variant_locked:
        raise ApiError("variants_not_supported",
                       "У товара есть варианты (размер/цвет): количество меняйте возвратом или новым приходом.",
                       status_code=409, data={"products": variant_locked})

    negative = []
    for pid, d in stock_delta.items():
        prod = products[pid]
        now_qty = Decimal(str(prod.quantity or 0))
        if d < 0 and now_qty + d < 0:
            mine = [p for p in plan if p["product_id"] == pid and p["item"] is not None]
            old = sum((Decimal(str(p["item"].qty)) for p in mine), _Z)
            min_qty = max(_Z, old - now_qty, *[returned.get(p["item"].id, _Z) for p in mine])
            negative.append({"product_id": str(pid), "name": prod.name, "stock_now": _s(now_qty, _Q3),
                             "qty_delta": _s(d, _Q3), "min_qty": _s(min_qty, _Q3)})
    batch_moves = _batch_moves(plan)
    for mv in batch_moves:
        if mv["batch"].remaining + mv["delta"] < 0:
            negative.append({"product_id": str(mv["batch"].product_id), "name": products[mv["batch"].product_id].name,
                             "batch": mv["batch"].label, "stock_now": _s(mv["batch"].remaining, _Q3),
                             "qty_delta": _s(mv["delta"], _Q3),
                             "min_qty": _s(max(_Z, Decimal(str(mv["old_qty"])) - mv["batch"].remaining), _Q3)})
    if negative:
        raise ApiError("negative_stock", "Товар уже продан или списан: остаток ушёл бы в минус.",
                       status_code=409, data={"items": negative})

    # суммы прихода
    old_amounts = {k: _line_amount(i.qty, i.purchase_price, i.line_total, i.product) for k, i in current.items()}
    new_amounts = dict(old_amounts)
    for p in plan:
        if p["action"] == "delete":
            new_amounts.pop(str(p["item"].id), None)
        else:
            k = str(p["item"].id) if p["item"] is not None else f"new-{p['index']}"
            new_amounts[k] = _line_amount(p["qty"], p["purchase_price"], p["line_total"], products[p["product_id"]])
    total_before, total_after = _total(old_amounts.values()), _total(new_amounts.values())
    diff = total_after - total_before

    # 8. деньги
    from apps.construction.models import CashFlow

    deal = _find_receipt_deal(company, receipt.supplier_id, receipt.id)
    if deal is not None and deal.kind != ClientDeal.Kind.DEBT:
        deal = None
    if deal is not None:
        deal = ClientDeal.objects.select_for_update().get(pk=deal.pk)
    flows = list(
        CashFlow.objects.filter(company=company, source_kind=CashFlow.SourceKind.PROCUREMENT_RECEIPT,
                                source_id=str(receipt.id))
        .exclude(status=CashFlow.Status.REJECTED).select_related("shift", "cashbox").order_by("created_at")
    )
    paid_cash = sum((cf.amount for cf in flows if cf.type == CashFlow.Type.EXPENSE), _Z) - sum(
        (cf.amount for cf in flows if cf.type == CashFlow.Type.INCOME), _Z)
    deal_paid = (deal.paid_total + (deal.prepayment or _Z)) if deal is not None else _Z
    if (new_supplier is not None or payment is not None) and deal is not None and deal_paid > 0:
        raise ApiError("debt_already_paid", "По долгу за этот приход уже были оплаты — поставщика и оплату менять нельзя.",
                       status_code=409, data={"deal_id": str(deal.id), "paid": _s(deal_paid), "new_total": _s(total_after)})

    warnings = []
    cash_delta = _Z
    deal_plan = None  # {"op": diff|set|create|delete, ...}
    debt_before = deal.amount if deal is not None else None
    debt_after = debt_before
    if payment is None:
        if deal is not None:
            if diff:
                new_amount = (deal.amount or _Z) + diff
                if new_amount < 0 or new_amount - (deal.prepayment or _Z) < deal.paid_total:
                    raise ApiError("debt_already_paid", "По долгу за этот приход уже заплачено больше новой суммы.",
                                   status_code=409,
                                   data={"deal_id": str(deal.id), "paid": _s(deal_paid + max(_Z, paid_cash)),
                                         "new_total": _s(total_after)})
                deal_plan = {"op": "diff", "diff": diff}
                debt_after = new_amount
        elif paid_cash > 0:
            cash_delta = diff
        elif diff:
            warnings.append({"code": "payment_not_tracked"})  # оплата прихода на сервере не записана
    else:
        ptype = payment["type"]
        if ptype == "cash":
            target_paid, target_debt = total_after, _Z
        elif ptype == "debt":
            target_paid, target_debt = _Z, total_after
        else:
            target_paid = payment["paid_amount"]
            if not (_Z < target_paid < total_after):
                raise _validation({"payment.paid_amount": ["Для partial — больше 0 и меньше суммы прихода."]})
            target_debt = total_after - target_paid
        cash_delta = target_paid - paid_cash
        if target_debt > 0:
            deal_plan = {"op": "set" if deal is not None else "create", "amount": target_debt,
                         "due_date": payment["due_date"]}
        elif deal is not None:
            deal_plan = {"op": "delete"}
        debt_after = target_debt if target_debt > 0 else (_Z if deal is not None else None)
        if debt_before is None and target_debt > 0:
            debt_before = _Z

    cash_plan = _cash_plan(company, receipt, cash_delta, flows, payment, warnings) if cash_delta else []

    # 9. закупка в карточке
    price_plan = _price_plan(receipt, plan, products, current_items, body["price_rule"])

    # предупреждение: продажи после прихода по затронутым товарам
    affected = {p["product_id"] for p in plan} | {e["product_id_raw"] for e in price_plan}
    if affected:
        from apps.main.models import Sale

        cnt = (Sale.objects.filter(company=company, created_at__gt=receipt.created_at,
                                   items__product_id__in=list(affected))
               .exclude(status=Sale.Status.CANCELED).values("id").distinct().count())
        if cnt:
            warnings.append({"code": "sales_after_receipt", "count": cnt})

    stock_rows = []
    for pid, d in stock_delta.items():
        before = Decimal(str(products[pid].quantity or 0))
        stock_rows.append({"product_id": str(pid), "name": products[pid].name, "qty_before": _s(before, _Q3),
                           "qty_delta": _s(d, _Q3), "qty_after": _s(before + d, _Q3)})
    header_changes = {}
    for k, v in header.items():
        old = getattr(receipt, k)
        if (old.isoformat() if hasattr(old, "isoformat") else old) != (v.isoformat() if hasattr(v, "isoformat") else v):
            header_changes[k] = {"before": old.isoformat() if hasattr(old, "isoformat") else old,
                                 "after": v.isoformat() if hasattr(v, "isoformat") else v}
    if new_supplier is not None:
        header_changes["supplier_id"] = {"before": str(receipt.supplier_id), "after": str(new_supplier.id)}
    if payment is not None:
        header_changes["payment"] = {"before": _payment_type(receipt), "after": payment["type"]}
    if disc_change is not None:
        for k in ("discount", "discount_percent"):
            old, new = disc_change["before"][k], disc_change[k]
            if old != new:
                header_changes[k] = {"before": _s(old), "after": _s(new)}
    sale_plan = _sale_plan(receipt, plan, products, price_plan, body["update_sale"], warnings)

    deltas = {
        "stock": stock_rows,
        "purchase_price": [{k: v for k, v in e.items() if k != "product_id_raw"} for e in price_plan],
        "total": {"before": _s(total_before), "after": _s(total_after), "delta": _s(diff)},
        "supplier_debt": None if debt_before is None and debt_after is None else {
            "deal_id": str(deal.id) if deal is not None else None,
            "before": _s(debt_before or _Z), "after": _s(debt_after or _Z),
            "delta": _s((debt_after or _Z) - (debt_before or _Z)),
        },
        "cashflows": [{"id": None, "type": c["type"], "amount": _s(c["amount"]),
                       "cashbox": str(c["cashbox"].id) if c.get("cashbox") is not None else None,
                       "shift": str(c["shift"].id) if c.get("shift") is not None else None} for c in cash_plan],
        "header": header_changes,
        "sale_price": [{k: v for k, v in e.items() if k != "product_id_raw" and not k.startswith("_")} for e in sale_plan],
        "lines": [_line_change(p, products) for p in plan],
    }

    if dry_run:
        return _response(request, receipt, deltas, [], warnings, dry_run=True)

    # ---------------- запись ----------------
    _ensure_base_version(receipt)
    new_version = (receipt.version or 1) + 1
    now = timezone.now()
    from apps.main.services.product_expiry_batches import receive_stock

    running = {pid: Decimal(str(products[pid].quantity or 0)) for pid in products}
    movements = []
    num = receipt.number_info.get("number_display") if hasattr(receipt, "number_info") else receipt.number
    move_comment = f"Правка прихода №{num} (v{new_version}): {body['reason']}"[:1000]
    supplier_for_lines = new_supplier or receipt.supplier
    for p in plan:
        prod = products[p["product_id"]]
        if p["action"] == "delete":
            p["item"].delete()
        elif p["action"] == "update":
            item = p["item"]
            item.qty, item.purchase_price, item.line_total = p["qty"], p["purchase_price"], p["line_total"]
            fl = ["qty", "purchase_price", "line_total"]
            if "sale_price" in p:
                item.sale_price = p["sale_price"]
                fl.append("sale_price")
            if "landed_price" in p:
                item.landed_price = p["landed_price"]
                fl.append("landed_price")
            item.save(update_fields=fl)
        else:
            p["item"] = SupplierReceiptItem.objects.create(
                receipt=receipt, product=prod, qty=p["qty"], purchase_price=p["purchase_price"],
                line_total=p["line_total"], previous_sale_price=prod.price,
                sale_price=p["sale_price"] if "sale_price" in p else prod.price,
                markup_percent=prod.markup_percent, landed_price=p.get("landed_price"),
            )
            try:
                prod.suppliers.add(supplier_for_lines)
            except Exception:
                pass
            receive_stock(prod, p["qty"], source_kind="purchase", source_id=receipt.id, user=user)
        if p["action"] != "add" and p["delta"]:
            _adjust_expiry(prod, receipt, p["delta"])
        if p["delta"]:
            before = running[prod.id]
            running[prod.id] = before + p["delta"]
            mv = record_stock_movement(
                company=company, branch=receipt.branch, type=StockMovement.Type.RECEIPT_CORRECTION,
                object_id=prod.id, created_by=user, qty_before=before, change=p["delta"],
                qty_after=running[prod.id], product_name=prod.name,
                source_type="supplier", source_id=supplier_for_lines.id,
                source_name=(supplier_for_lines.full_name or "")[:255],
                comment=move_comment, ref_type="receipt_correction", ref_id=receipt.id,
            )
            movements.append(str(mv.id))

    for mv in batch_moves:
        from apps.main.tz26.hw_batches.models import ProductBatch, ProductBatchReceiptLine

        ProductBatch.objects.filter(pk=mv["batch"].pk).update(
            remaining=F("remaining") + mv["delta"], received_total=F("received_total") + mv["delta"])
        if mv["new_qty"] is not None:
            ProductBatchReceiptLine.objects.filter(pk=mv["line"].pk).update(quantity=mv["new_qty"])

    for pid, d in stock_delta.items():
        Product.objects.filter(pk=pid).update(quantity=F("quantity") + d, updated_at=now)

    for e in price_plan:
        if not e["applied"]:
            continue
        prod = products[e["product_id_raw"]]
        new_pp = Decimal(e["after"])
        upd = {"purchase_price": new_pp, "updated_at": now}
        price = Decimal(str(prod.price or 0))
        if new_pp > 0 and price > 0:
            upd["markup_percent"] = ((price - new_pp) / new_pp * Decimal("100")).quantize(_Q4, rounding=ROUND_HALF_UP)
        Product.objects.filter(pk=prod.pk).update(**upd)

    _apply_sale_plan(request, company, sale_plan, products, now)

    # долг поставщику
    old_supplier_id = receipt.supplier_id
    if deal is not None and new_supplier is not None:
        name = new_supplier.full_name or getattr(new_supplier, "title", "") or "Поставщик"
        ClientDeal.objects.filter(pk=deal.pk).update(client=new_supplier, title=f"Долг {name}", updated_at=now)
        deal.client = new_supplier
        deal.client_id = new_supplier.id
    if deal_plan is not None:
        deal = _apply_deal_plan(deal_plan, deal, receipt, new_supplier or receipt.supplier)

    # деньги кассы
    created_flows = _apply_cash_plan(cash_plan, company, receipt, user, new_version)

    # журнал закупок поставщика: поставщик и способ оплаты
    if new_supplier is not None or payment is not None:
        _move_purchase_journal(receipt, current_items, new_supplier,
                               {"cash": "cash", "debt": "debt", "partial": "prepayment"}.get(payment["type"])
                               if payment else None)

    # шапка, версия
    for k, v in header.items():
        setattr(receipt, k, v)
    upd_fields = [*header.keys(), "version", "edit_count", "edited_at", "edited_by"]
    if disc_change is not None:
        receipt.discount, receipt.discount_percent = disc_change["discount"], disc_change["discount_percent"]
        upd_fields += ["discount", "discount_percent"]
    if new_supplier is not None:
        receipt.supplier = new_supplier
        upd_fields.append("supplier")
    receipt.version = new_version
    receipt.edit_count = (receipt.edit_count or 0) + 1
    receipt.edited_at = now
    receipt.edited_by = user
    receipt.save(update_fields=upd_fields)

    snap = receipt_snapshot(receipt)
    SupplierReceiptVersion.objects.create(
        receipt=receipt, company_id=company.id, version=new_version, action=action, at=now, user=user,
        device=body["device"], source=body["source"], reason=body["reason"][:REASON_MAX],
        reason_code=body["reason_code"], total=Decimal(snap["total"]), snapshot={**snap, "header_diff": header_changes},
        lines_diff=deltas["lines"],
    )

    deltas["cashflows"] = [{"id": str(cf.id), "type": cf.type, "amount": _s(cf.amount),
                            "cashbox": str(cf.cashbox_id) if cf.cashbox_id else None,
                            "shift": str(cf.shift_id) if cf.shift_id else None} for cf in created_flows]
    if deltas["supplier_debt"] is not None:
        deltas["supplier_debt"]["deal_id"] = str(deal.id) if deal is not None else None

    # журнал действий
    try:
        from apps.construction import audit as admin_audit

        admin_audit.log_request(
            request, action, "supplier_receipt", receipt.id, f"Приход №{num}",
            before={"version": new_version - 1, "total": _s(total_before)},
            after={"version": new_version, "total": _s(total_after), "diff": {"lines": deltas["lines"],
                                                                              "header": header_changes},
                   "reason": body["reason"], "reason_code": body["reason_code"], "device": body["device"],
                   "source": body["source"]},
            company=company,
        )
    except Exception:
        logger.exception("receipt edit audit failed")

    from apps.main.cache_utils import invalidate_cache_pattern

    invalidate_cache_pattern(f"products:list:{company.id}:")
    invalidate_cache_pattern(f"analytics:market:{company.id}:")
    # bek1911: и после коммита — запрос списка во время правки не должен закешировать старую закупку
    transaction.on_commit(lambda: (invalidate_cache_pattern(f"products:list:{company.id}:"),
                                   invalidate_cache_pattern(f"analytics:market:{company.id}:")))

    _emit_events(receipt, user, now, stock_delta, running, price_plan, old_supplier_id,
                 money_changed=bool(deal_plan or new_supplier or created_flows))
    _notify_owner(receipt, user, total_before, total_after, body)

    return _response(request, receipt, deltas, movements, warnings, dry_run=False)


def _response(request, receipt, deltas, movements, warnings, *, dry_run):
    from apps.main.serializers import SupplierReceiptReadSerializer

    fresh = receipt_queryset().get(pk=receipt.pk)
    data = SupplierReceiptReadSerializer(fresh, context={"request": request}).data
    data["lines"] = data.get("items") or []
    body = {"receipt": data, "deltas": deltas, "movements": movements, "warnings": warnings,
            "replayed": False, "dry_run": dry_run}
    resp = Response(body)
    resp["ETag"] = receipt_etag(fresh)
    return resp


def receipt_queryset():
    """Приход с суммой и строками — как в GET suppliers/receipts/ (без фильтра компании: pk уже проверен)."""
    from django.db.models import Sum as _Sum

    from apps.main.views import _RECEIPT_PREFETCH, _supplier_receipt_line_total_expr

    return (
        SupplierReceipt.objects.select_related("supplier", "company", "branch", "created_by", "edited_by")
        .prefetch_related(*_RECEIPT_PREFETCH)
        .annotate(total_amount=_Sum(_supplier_receipt_line_total_expr("items__")))
    )


def _line_change(p, products):
    item = p["item"]
    prod = products[p["product_id"]]
    out = {
        "action": p["action"], "id": str(item.id) if item is not None else None,
        "client_row_id": p.get("client_row_id"),
        "product_id": str(prod.id), "product_name": prod.name,
        "qty_before": _s(item.qty, _Q3) if item is not None else None,
        "qty_after": None if p["action"] == "delete" else _s(p["qty"], _Q3),
        "purchase_price_before": _s(item.purchase_price, _Q4) if item is not None else None,
        "purchase_price_after": None if p["action"] == "delete" else _s(p["purchase_price"], _Q4),
        "line_total_before": _s(item.line_total) if item is not None else None,
        "line_total_after": None if p["action"] == "delete" else _s(p["line_total"]),
    }
    if "sale_price" in p:
        out["sale_price_before"] = _s(item.sale_price, _Q3) if item is not None else None
        out["sale_price_after"] = _s(p["sale_price"], _Q3)
    return out


def _sale_plan(receipt, plan, products, price_plan, update_product, warnings):
    """
    01.11: цена продажи строки. Как при проведении (price/selling_price в строке): цена продажи товара и наценка
    от закупки; update_product_sale_price=false — меняем только строку документа. Несколько строк одного
    товара — действует последняя. Приход не последний по товару — предупреждение sale_price_not_latest.
    """
    rows = [p for p in plan if "sale_price" in p and p["action"] != "delete"]
    last = {}
    for p in rows:
        last[p["product_id"]] = p
    out = []
    for p in rows:
        prod = products[p["product_id"]]
        item = p["item"]
        new = Decimal(str(p["sale_price"])).quantize(_Q2, rounding=ROUND_HALF_UP)
        before = Decimal(str(prod.price or 0))
        latest = not SupplierReceiptItem.objects.filter(
            product_id=prod.id, receipt__created_at__gt=receipt.created_at).exclude(receipt_id=receipt.id).exists()
        applied = bool(update_product) and last[p["product_id"]] is p
        if applied and not latest and not any(w.get("code") == "sale_price_not_latest" for w in warnings):
            warnings.append({"code": "sale_price_not_latest"})
        eff = next((Decimal(e["after"]) for e in price_plan if e["product_id_raw"] == prod.id), None)
        out.append({"line_id": str(item.id) if item is not None else None, "product_id": str(prod.id),
                    "product_id_raw": prod.id, "name": prod.name,
                    "before": _s(item.sale_price, _Q3) if item is not None else None,
                    "after": _s(p["sale_price"], _Q3),
                    "product_price_before": _s(before), "product_price_after": _s(new if applied else before),
                    "applied": applied, "latest": latest, "_new": new, "_eff": eff})
    return out


def _apply_sale_plan(request, company, sale_plan, products, now):
    for e in sale_plan:
        if not e["applied"]:
            continue
        prod = products[e["product_id_raw"]]
        new = e["_new"]
        upd = {"price": new, "updated_at": now}
        pp = Product.objects.filter(pk=prod.pk).values_list("purchase_price", flat=True).first()
        if pp is not None and Decimal(str(pp)) > 0:
            pp = Decimal(str(pp))
            upd["markup_percent"] = ((new - pp) / pp * Decimal("100")).quantize(_Q4, rounding=ROUND_HALF_UP)
        old = Decimal(str(prod.price or 0))
        Product.objects.filter(pk=prod.pk).update(**upd)
        if old.quantize(_Q2) != new:
            try:
                from apps.construction import audit as admin_audit

                admin_audit.log_request(request, "price_change", "product", prod.pk, prod.name,
                                        before={"price": str(old)}, after={"price": str(new)}, company=company)
            except Exception:
                logger.exception("receipt edit sale price audit failed")
        try:
            from apps.main.change_feed import _emit

            _emit(company.id, "product_changed", branch_id=None, entity="product", entity_id=prod.pk,
                  data={"what": "price", "product": prod.pk})
        except Exception:
            logger.exception("receipt edit sale price event failed")


def _batch_moves(plan):
    """Партии (ТЗ 26.10 Б-61) строк, у которых меняется количество: остаток партии двигается на разницу."""
    try:
        from apps.main.tz26.hw_batches.models import ProductBatchReceiptLine
    except Exception:  # pragma: no cover
        return []
    ids = [p["item"].id for p in plan if p["item"] is not None and p["delta"]]
    if not ids:
        return []
    lines = {
        ln.receipt_item_id: ln
        for ln in ProductBatchReceiptLine.objects.select_related("batch").select_for_update().filter(receipt_item_id__in=ids)
    }
    out = []
    for p in plan:
        ln = lines.get(p["item"].id) if p["item"] is not None else None
        if ln is None:
            continue
        new_qty = None if p["action"] == "delete" else Decimal(str(p["qty"]))
        out.append({"batch": ln.batch, "line": ln, "delta": Decimal(str(p["delta"])), "new_qty": new_qty,
                    "old_qty": p["item"].qty})
    return out


def _adjust_expiry(product, receipt, delta):
    """Партия срока годности прихода (best-effort, как consume_stock): остаток на разницу, не ниже 0."""
    from apps.main.models import ProductExpiryBatch

    batch = (
        ProductExpiryBatch.objects.select_for_update()
        .filter(product=product, source_kind="purchase", source_id=str(receipt.id))
        .order_by("received_at").first()
    )
    if batch is None:
        if delta > 0:
            from apps.main.services.product_expiry_batches import receive_stock

            receive_stock(product, delta, source_kind="purchase", source_id=receipt.id)
        return
    d = Decimal(str(delta))
    batch.quantity = max(_Z, batch.quantity + d)
    batch.remaining_quantity = max(_Z, batch.remaining_quantity + d)
    batch.save(update_fields=["quantity", "remaining_quantity"])


def _extra_total(receipt):
    total = _Z
    for e in receipt.extra_costs or []:
        try:
            total += Decimal(str((e or {}).get("amount") or 0))
        except (InvalidOperation, ValueError, AttributeError):
            pass
    return total


def _discount_plan(receipt, plan, current, req, rescale):
    """
    01.11: смена скидки шапки. Как при проведении (_receipt_discount_extras): закупка строк хранится уже
    с учётом скидки и доп. расходов, поправка (доп. расходы − скидка) раскладывается на строки пропорционально
    сумме строки. Здесь: старая поправка снимается, новая раскладывается — строки масштабируются на
    (база + новая поправка) / (база + старая поправка). Строки, правленные в этом же запросе, берутся как есть.
    → (plan, {"discount": Decimal, "discount_percent": Decimal|None, "before": {...}} | None)
    """
    extra = _extra_total(receipt)
    old_disc = Decimal(str(receipt.discount or 0))
    old_pct = None if receipt.discount_percent is None else Decimal(str(receipt.discount_percent))

    # живые строки после правок: {key: [qty, pp, lt, product, plan_entry|None, item|None]}
    by_item = {str(p["item"].id): p for p in plan if p["item"] is not None}
    live = []
    for k, item in current.items():
        p = by_item.get(k)
        if p is not None and p["action"] == "delete":
            continue
        if p is not None:
            live.append({"p": p, "item": item, "qty": p["qty"], "pp": p["purchase_price"], "lt": p["line_total"],
                         "product": item.product})
        else:
            live.append({"p": None, "item": item, "qty": Decimal(str(item.qty)), "pp": item.purchase_price,
                         "lt": item.line_total, "product": item.product})
    for p in plan:
        if p["action"] == "add":
            prod = Product.objects.filter(pk=p["product_id"]).only("id", "purchase_price").first()
            live.append({"p": p, "item": None, "qty": p["qty"], "pp": p["purchase_price"], "lt": p["line_total"],
                         "product": prod})
    for row in live:
        row["amount"] = _line_amount(row["qty"], row["pp"], row["lt"], row["product"])
    stored = sum((r["amount"] for r in live), _Z)
    subtotal = stored - (extra - old_disc)  # сумма строк до скидки и доп. расходов

    if req["pct"] is not None:
        new_pct = req["pct"]
        new_disc = (subtotal * new_pct / Decimal("100")).quantize(_Q2, rounding=ROUND_HALF_UP)
    else:
        new_pct, new_disc = None, req["money"]
    if new_disc > subtotal:
        raise _validation({"discount": ["Скидка больше суммы прихода."]}, code="discount_exceeds_total",
                          detail="Скидка больше суммы прихода.")
    same = new_disc == old_disc and new_pct == old_pct
    if same:
        return plan, None
    change = {"discount": new_disc, "discount_percent": new_pct,
              "before": {"discount": old_disc, "discount_percent": old_pct}}
    if not rescale or stored <= 0 or new_disc == old_disc:
        return plan, change
    ratio = (subtotal + (extra - new_disc)) / stored
    if ratio == 1:
        return plan, change
    landed = bool(extra or new_disc)
    extra_idx = 100000
    for row in live:
        qty, pp, lt = row["qty"], row["pp"], row["lt"]
        if pp is not None:
            new_pp = (Decimal(str(pp)) * ratio).quantize(_Q4, rounding=ROUND_HALF_UP)
        else:
            new_pp = (row["amount"] * ratio / Decimal(str(qty))).quantize(_Q4, rounding=ROUND_HALF_UP) if qty else None
        new_lt = None if lt is None else (Decimal(str(lt)) * ratio).quantize(_Q4, rounding=ROUND_HALF_UP)
        entry = row["p"]
        if entry is None:
            extra_idx += 1
            entry = {"action": "update", "item": row["item"], "product_id": row["item"].product_id,
                     "index": extra_idx, "qty": qty, "purchase_price": pp, "line_total": lt}
            plan.append(entry)
        entry["purchase_price"], entry["line_total"] = new_pp, new_lt
        entry["landed_price"] = new_pp if landed else None
    return plan, change


def _purchase_price_before(receipt, pid):
    """
    19.11.2026 (bek1911): закупка товара перед проведением прихода — по журналу остатков (main_stockledger,
    пишет триггер, unit_cost = закупка карточки в момент движения). Своя строка прихода — POST supplier-receipt
    в пределах минуты до created_at; берём строку журнала перед ней, кроме строк приходов и их правок
    (правка пишет закупку ДО отката). Журнала нет (старый приход) — None.
    """
    try:
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute(
                "SELECT occurred_at FROM main_stockledger WHERE product_id = %s AND source = 'supplier-receipt' "
                "AND qty_delta > 0 AND occurred_at BETWEEN %s AND %s ORDER BY occurred_at DESC LIMIT 1",
                [pid, receipt.created_at - timedelta(seconds=60), receipt.created_at + timedelta(seconds=5)])
            own = cur.fetchone()
            if own is None:
                return None
            cur.execute(
                "SELECT unit_cost FROM main_stockledger WHERE product_id = %s AND occurred_at < %s "
                "AND unit_cost IS NOT NULL AND source NOT IN ('supplier-receipt', 'supplier-receipt-detail') "
                "ORDER BY occurred_at DESC LIMIT 1", [pid, own[0]])
            row = cur.fetchone()
        return Decimal(str(row[0])) if row is not None else None
    except Exception:
        logger.exception("purchase price before receipt failed")
        return None


def _price_plan(receipt, plan, products, current_items, rule):
    """
    Закупка в карточке по update_product_purchase_price:
      if_latest — только если этот приход последний по товару; always — всегда; never — никогда.
    Удалили все строки товара в последнем приходе — откат к закупке предыдущего прихода.
    """
    out = []
    deleted_ids = {p["item"].id for p in plan if p["action"] == "delete"}
    for pid in dict.fromkeys(p["product_id"] for p in plan):
        prod = products[pid]
        mine = [p for p in plan if p["product_id"] == pid]
        live = [p for p in mine if p["action"] != "delete" and p["purchase_price"] is not None]
        candidate = None
        if live:
            candidate = Decimal(str(live[-1]["purchase_price"]))
            if not any(p["action"] == "add" or _s(p["purchase_price"], _Q4) != _s(p["item"].purchase_price, _Q4)
                       for p in live):
                continue  # цена строк товара не менялась
        else:
            rest = [i for i in current_items if i.product_id == pid and i.id not in deleted_ids]
            if rest:
                continue  # у товара в приходе остались строки — закупку не трогаем
            prev = (SupplierReceiptItem.objects.filter(product_id=pid, receipt__created_at__lt=receipt.created_at,
                                                      purchase_price__isnull=False)
                    .exclude(receipt_id=receipt.id).order_by("-receipt__created_at", "-id").first())
            if prev is not None:
                candidate = Decimal(str(prev.purchase_price))
            else:
                # 19.11.2026 (bek1911): прошлых приходов нет (закупку ставили в карточке) — по журналу остатков
                candidate = _purchase_price_before(receipt, pid)
                if candidate is None:
                    continue
        before = Decimal(str(prod.purchase_price or 0))
        if candidate == before:
            continue
        latest = not SupplierReceiptItem.objects.filter(
            product_id=pid, receipt__created_at__gt=receipt.created_at).exclude(receipt_id=receipt.id).exists()
        applied = rule == "always" or (rule == "if_latest" and latest)
        if not live and rule != "never":
            applied = latest  # откат к прошлому приходу — только если правим последний приход
        out.append({"product_id": str(pid), "product_id_raw": pid, "name": prod.name,
                    "before": _s(before, _Q4), "after": _s(candidate if applied else before, _Q4),
                    "proposed": _s(candidate, _Q4), "applied": applied, "latest": latest})
    return out


def _cash_plan(company, receipt, amount, flows, payment, warnings):
    """
    Проводка разницы оплаченного прихода: доплата — расход, уменьшение — приход (сторно) в ту же кассу.
    Смена прихода закрыта — в основную кассу компании (роль expense_variable) + предупреждение.
    """
    from apps.construction.models import CashFlow, Cashbox, CashShift

    if not amount:
        return []
    row = {"type": CashFlow.Type.EXPENSE if amount > 0 else CashFlow.Type.INCOME, "amount": abs(amount),
           "cashbox": None, "shift": None, "cashbox_role": None, "drawer": False}
    base = next((cf for cf in flows if cf.type == CashFlow.Type.EXPENSE), None)
    if payment is not None and payment.get("cashbox_id"):
        row["cashbox"] = Cashbox.objects.filter(company=company, pk=payment["cashbox_id"]).first()
        if row["cashbox"] is None:
            raise _validation({"payment.cashbox_id": ["Касса не найдена."]})
        if payment.get("shift_id"):
            sh = CashShift.objects.filter(company=company, pk=payment["shift_id"], cashbox=row["cashbox"],
                                          status=CashShift.Status.OPEN).first()
            if sh is not None:
                row["shift"], row["drawer"] = sh, True
    elif base is not None:
        if base.shift_id and base.shift.status != CashShift.Status.OPEN:
            row["cashbox_role"] = "expense_variable"
            warnings.append({"code": "shift_closed_money_to_main"})
        else:
            row["cashbox"] = base.cashbox
            if base.shift_id:
                row["shift"], row["drawer"] = base.shift, True
    else:
        row["cashbox_role"] = "expense_variable"
    return [row]


def _apply_cash_plan(cash_plan, company, receipt, user, version):
    from apps.construction.auto_cashflow import create_auto_cashflow
    from apps.construction.models import CashFlow

    out = []
    supplier = receipt.supplier
    name = supplier.full_name or getattr(supplier, "title", "") or "Поставщик"
    for c in cash_plan:
        what = "доплата" if c["type"] == CashFlow.Type.EXPENSE else "сторно"
        cf = create_auto_cashflow(
            company=company, branch=receipt.branch, cashbox=c["cashbox"], cashbox_role=c["cashbox_role"],
            user=user, shift=c["shift"], type=c["type"], amount=c["amount"],
            source_kind=CashFlow.SourceKind.PROCUREMENT_RECEIPT, source_id=str(receipt.id),
            name=f"Правка прихода поставщика {name} (v{version}): {what}",
            source_business_operation_id="Закупки", affects_shift_drawer=c["drawer"],
            idempotency_key=f"receipt-edit:{receipt.id}:v{version}:{c['type']}",
        )
        if cf is not None:
            out.append(cf)
    return out


def _apply_deal_plan(deal_plan, deal, receipt, supplier):
    from apps.main.views import _receipt_marker

    op = deal_plan["op"]
    if op == "diff":
        _apply_deal_diff(deal, deal_plan["diff"])
        return ClientDeal.objects.get(pk=deal.pk)
    if op == "delete":
        ClientDeal.objects.filter(pk=deal.pk).delete()
        return deal
    if op == "set":
        deal.amount = deal_plan["amount"].quantize(_Q2, rounding=ROUND_HALF_UP)
        deal.prepayment = _Z
        if deal_plan.get("due_date"):
            deal.first_due_date = deal_plan["due_date"]
        deal.save()
        return deal
    name = supplier.full_name or getattr(supplier, "title", "") or "Поставщик"
    deal = ClientDeal(
        company=receipt.company, branch=receipt.branch, client=supplier,
        title=f"Долг {name}", kind=ClientDeal.Kind.DEBT,
        amount=deal_plan["amount"].quantize(_Q2, rounding=ROUND_HALF_UP), prepayment=_Z,
        debt_months=1, interval_months=1,
        first_due_date=deal_plan.get("due_date") or (timezone.localdate() + timedelta(days=30)),
        auto_schedule=True, note=f"Приход поставщика {_receipt_marker(receipt.id)}",
    )
    deal.save()
    return deal


def _apply_deal_diff(deal, diff):
    """
    Сумма долга по приходу меняется на diff. Без платежей — обычное сохранение (график пересоберётся).
    С платежами график не пересобираем (модель это запрещает): разница ложится на последние
    неоплаченные взносы, остатки balance_after пересчитываются.
    """
    from apps.main.models import DealInstallment

    deal = ClientDeal.objects.select_for_update().get(pk=deal.pk)
    has_payments = deal.payments.exists() or deal.paid_total > 0
    new_amount = (deal.amount or _Z) + diff
    if not has_payments:
        deal.amount = new_amount
        deal.save()
        return
    ClientDeal.objects.filter(pk=deal.pk).update(amount=new_amount, updated_at=timezone.now())
    insts = list(DealInstallment.objects.select_for_update().filter(deal=deal).order_by("number"))
    if not insts:
        return
    if diff > 0:
        last = insts[-1]
        last.amount = (last.amount or _Z) + diff
    else:
        left = -diff
        for inst in reversed(insts):
            if left <= 0:
                break
            room = (inst.amount or _Z) - (inst.paid_amount or _Z)
            take = min(room, left) if room > 0 else _Z
            inst.amount -= take
            left -= take
    balance = new_amount - (deal.prepayment or _Z)
    for inst in insts:
        balance = (balance - inst.amount).quantize(_Q2)
        inst.balance_after = balance
    DealInstallment.objects.bulk_update(insts, ["amount", "balance_after"])


def _move_purchase_journal(receipt, items, new_supplier, payment_type):
    """Строки журнала закупок (SupplierPurchase), записанные при проведении прихода (минута после created_at)."""
    from apps.main.models import SupplierPurchase

    qs = SupplierPurchase.objects.filter(
        company_id=receipt.company_id, supplier_id=receipt.supplier_id,
        product_id__in=[i.product_id for i in items],
        purchased_at__gte=receipt.created_at, purchased_at__lte=receipt.created_at + timedelta(seconds=60),
    )
    upd = {}
    if new_supplier is not None:
        upd["supplier"] = new_supplier
    if payment_type:
        upd["payment_type"] = payment_type
    if upd:
        qs.update(**upd)


def _emit_events(receipt, user, at, stock_delta, running, price_plan, old_supplier_id, money_changed):
    """Лента изменений + WebSocket: receipt_changed, остатки, закупка, долг поставщика."""
    try:
        from apps.main.change_feed import _emit

        cid, bid = receipt.company_id, receipt.branch_id
        _emit(cid, "receipt_changed", branch_id=bid, actor_id=getattr(user, "pk", None),
              entity="supplier_receipt", entity_id=receipt.id,
              data={"receipt_id": str(receipt.id), "version": receipt.version, "supplier_id": str(receipt.supplier_id),
                    "edited_by": _user_ref(user), "at": at.isoformat()})
        for pid in stock_delta:
            _emit(cid, "stock_changed", branch_id=bid, entity="product", entity_id=pid,
                  data={"product": pid, "quantity": running.get(pid)})
        for e in price_plan:
            if e["applied"]:
                _emit(cid, "product_changed", branch_id=bid, entity="product", entity_id=e["product_id_raw"],
                      data={"what": "purchase_price", "product": e["product_id_raw"]})
        if money_changed:
            for sid in {old_supplier_id, receipt.supplier_id}:
                _emit(cid, "supplier_debt_changed", branch_id=bid, entity="supplier", entity_id=sid,
                      data={"supplier": sid, "receipt": receipt.id})
    except Exception:
        logger.exception("receipt edit events failed")


def _money(v):
    return f"{Decimal(str(v)).quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}".replace(",", " ")


def _notify_owner(receipt, user, total_before, total_after, body):
    """Владельцу в Telegram-бот компании (раздел 2.7): после коммита, в фоне, ошибки не мешают правке."""
    supplier = receipt.supplier
    num = receipt.number_info.get("number_display") if hasattr(receipt, "number_info") else receipt.number
    diff = total_after - total_before
    sign = "+" if diff > 0 else ("−" if diff < 0 else "±")
    who = (_user_ref(user) or {}).get("name") or ""
    reason = body["reason"]
    label = REASON_LABELS.get(body["reason_code"])
    if label and label != "другое" and label not in reason.lower():
        reason = f"{label}: {reason}"
    text = (
        f"✎ Приход №{html.escape(str(num))} от «{html.escape(supplier.full_name or 'Поставщик')}» исправлен: "
        f"{_money(total_before)} → {_money(total_after)} сом ({sign}{_money(abs(diff))}). "
        f"Причина: {html.escape(reason)}. Кто: {html.escape(who)}"
        + (f", ПК: {html.escape(body['device'])}" if body["device"] else "") + "."
    )
    company = receipt.company
    key = f"receipt_edit:{receipt.id}:v{receipt.version}"

    def _send():
        from django.db import connection

        try:
            from apps.main.telegram_bot.services.owner_notify import notify_owner

            notify_owner(company, text, dedupe_key=key)
        except Exception:
            logger.exception("receipt edit telegram notify failed")
        finally:
            connection.close()

    try:
        transaction.on_commit(lambda: threading.Thread(target=_send, daemon=True).start())
    except Exception:  # pragma: no cover
        logger.exception("receipt edit notify on_commit failed")


# ------------------------------------------------------------------ вход: PATCH и откат

def _with_idempotency(view, request, impl, *args):
    """Idempotency-Key обязателен: повтор → прежний ответ с replayed: true, другой тело → 409 idempotency_conflict."""
    from apps.integrations.idempotency import _request_key, idempotent

    if not _request_key(request, True):
        raise ApiError("idempotency_key_required", "Нужен заголовок Idempotency-Key (uuid правки).", status_code=400)
    return idempotent(body_key=True, replay_flag=True)(impl)(view, request, *args)


def _version_required(request):
    from core.api_compat import parse_client

    name = parse_client(request)[0]
    return name is not None and name not in ("web", "site", "market", "front")


def deleted_receipt_ids_q():
    """ТЗ кассы 05.11 п.6: удалённый приход — с версией action=delete (строк нет, остатки откатаны)."""
    from django.db.models import Exists, OuterRef

    return Exists(SupplierReceiptVersion.objects.filter(receipt_id=OuterRef("pk"), action="delete"))


def handle_delete(view, request, pk):
    """
    ТЗ кассы 05.11 п.6: DELETE suppliers/receipts/{id}/ {reason, version?} + Idempotency-Key.
    Это правка «удалить все строки»: остатки и партии откатываются, оплата наличными возвращается
    в кассу, долг по приходу уменьшается до нуля; версия action=delete + журнал действий.
    Нельзя: товар уже продан/списан (negative_stock), был возврат поставщику (has_returns),
    по долгу уже платили (debt_already_paid), окно правки/закрытый период — как у PATCH.
    """
    data = request.data if hasattr(request.data, "get") else {}
    qs = view._filter_qs_company_branch(SupplierReceipt.objects.all())
    receipt = get_object_or_404(qs.exclude(deleted_receipt_ids_q()), pk=pk)
    raw = {
        "reason": data.get("reason") or request.query_params.get("reason"),
        "reason_code": data.get("reason_code") or "",
        "version": data.get("version"),
        "source": data.get("source") or "", "device": data.get("device") or "",
        "dry_run": data.get("dry_run"),
        "lines": [{"id": str(i), "delete": True} for i in receipt.items.values_list("id", flat=True)],
    }
    body = parse_body(raw, request.query_params)
    if body["dry_run"]:
        return edit_receipt(view, request, pk, body, action="delete")

    def _impl(v, req, rid):
        resp = edit_receipt(v, req, rid, body, action="delete")
        resp.data["deleted"] = True
        return resp

    return _with_idempotency(view, request, _impl, pk)


def handle_patch(view, request, pk):
    body = parse_body(request.data, request.query_params)
    if body["dry_run"]:
        return edit_receipt(view, request, pk, body, action="edit")

    def _impl(v, req, rid):
        return edit_receipt(v, req, rid, body, action="edit")

    return _with_idempotency(view, request, _impl, pk)


def _version_row(receipt, n):
    row = SupplierReceiptVersion.objects.filter(receipt=receipt, version=n).select_related("user").first()
    if row is not None:
        return row
    if n == (receipt.version or 1) and not SupplierReceiptVersion.objects.filter(receipt=receipt).exists():
        snap = receipt_snapshot(receipt)  # приход ещё не правили — текущее состояние и есть версия 1
        return SupplierReceiptVersion(receipt=receipt, company_id=receipt.company_id, version=n, action="create",
                                      at=receipt.created_at, user=receipt.created_by, total=Decimal(snap["total"]),
                                      snapshot=snap)
    return None


def revert_body(receipt, snapshot, data):
    """Тело правки, приводящее приход к составу снимка версии."""
    snap_lines = {ln["id"]: ln for ln in snapshot.get("lines") or []}
    cur = {str(i.id): i for i in receipt.items.order_by("id")}
    lines = []
    for lid, item in cur.items():
        s = snap_lines.get(lid)
        if s is None:
            lines.append({"id": lid, "delete": True})
        elif (_s(item.qty, _Q3) != s.get("qty") or _s(item.purchase_price, _Q4) != s.get("purchase_price")
              or _s(item.line_total, _Q4) != s.get("line_total")
              or (s.get("sale_price") is not None and _s(item.sale_price, _Q3) != s["sale_price"])):
            row = {"id": lid, "qty": s.get("qty"), "line_total": s.get("line_total")}
            if s.get("purchase_price") is not None:
                row["purchase_price"] = s["purchase_price"]
            if s.get("sale_price") is not None:
                row["sale_price"] = s["sale_price"]
            lines.append(row)
    for lid, s in snap_lines.items():
        if lid not in cur:
            row = {"product": s["product_id"], "qty": s["qty"], "line_total": s.get("line_total")}
            if s.get("purchase_price") is not None:
                row["purchase_price"] = s["purchase_price"]
            lines.append(row)
    out = {k: data.get(k) for k in ("reason", "reason_code", "version", "dry_run", "source", "device",
                                    "update_product_purchase_price") if k in data}
    out["lines"] = lines
    if "discount" in snapshot:  # снимки до 01.11 скидки не знают — тогда шапку не трогаем
        if (_s(receipt.discount) != snapshot.get("discount")
                or _s(receipt.discount_percent) != snapshot.get("discount_percent")):
            out["discount"] = snapshot.get("discount") or "0"
            out["discount_percent"] = snapshot.get("discount_percent")
            out["discount_rescale"] = False  # закупка строк восстанавливается из снимка как есть
    out["update_product_sale_price"] = data.get("update_product_sale_price", False)  # откат карточку товара не трогает
    if (receipt.document_number or "") != (snapshot.get("document_number") or ""):
        out["document_number"] = snapshot.get("document_number") or ""
    cur_date = receipt.document_date.isoformat() if receipt.document_date else None
    if cur_date != snapshot.get("document_date"):
        out["document_date"] = snapshot.get("document_date")
    if (receipt.comment or "") != (snapshot.get("comment") or ""):
        out["comment"] = snapshot.get("comment") or ""
    if snapshot.get("supplier_id") and snapshot["supplier_id"] != str(receipt.supplier_id):
        out["supplier_id"] = snapshot["supplier_id"]
    return out
