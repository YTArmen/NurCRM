"""
ТЗ 10.11: правка и аннулирование возвратов в течение 24 часов (касса NurCRM).

Возврат поставщику (этот файл):
  PATCH suppliers/returns/<id>/            полный новый состав строк (If-Match, Idempotency-Key, X-Confirm-Token)
  POST  suppliers/returns/<id>/void/       аннулирование (soft delete, status=voided)
  GET   suppliers/returns/<id>/history/    журнал версий (только владелец/администратор)

Правила: срок считает только сервер (created_at + 24 ч), PIN-токен одноразовый (ReturnConfirmToken),
склад/касса/долг пересчитываются в одной транзакции под блокировкой документа и товаров.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from core.exceptions import ApiError

EDIT_WINDOW = timedelta(hours=24)
CONFIRM_TTL = 120
_Q2 = Decimal("0.01")
_Z = Decimal("0.00")

SCOPE_EDIT = "return_edit"
SCOPE_VOID = "return_void"
OBJ_SUPPLIER = "supplier_return"
OBJ_POS = "pos_return"


def _s(v):
    return str(Decimal(str(v or 0)).quantize(_Q2, rounding=ROUND_HALF_UP))


def is_owner_like(user):
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "role", None) in ("owner", "admin")
        or getattr(user, "owned_company_id", None)
    )


# ---------- срок ----------
def editable_until(closed_at):
    return closed_at + EDIT_WINDOW


def window_error(closed_at):
    now = timezone.now()
    until = editable_until(closed_at)
    if now > until:
        return ApiError(
            "edit_window_expired", "Прошло 24 часа с момента проведения — правка и удаление закрыты.",
            status_code=409, top={"editable_until": until.isoformat(), "server_now": now.isoformat()},
        )
    return None


# ---------- токен подтверждения PIN ----------
def issue_confirm_token(*, user, approved_by, scope, object_type, object_id, version):
    from apps.main.models import ReturnConfirmToken

    company = getattr(user, "company", None) or getattr(user, "owned_company", None)
    return ReturnConfirmToken.objects.create(
        company=company, user=user, approved_by=approved_by, scope=scope, object_type=object_type,
        object_id=object_id, version=int(version), expires_at=timezone.now() + timedelta(seconds=CONFIRM_TTL),
    )


def consume_confirm_token(request, *, scope, object_type, object_id, version):
    """Гасит токен (одноразовый) и возвращает его. Нет токена → 428, чужой/просроченный/использованный → 403."""
    from apps.main.models import ReturnConfirmToken

    raw = (request.META.get("HTTP_X_CONFIRM_TOKEN") or "").strip()
    if not raw:
        raise ApiError("confirm_required", "Нужно подтвердить действие PIN-кодом руководителя.", status_code=428)
    try:
        tok = ReturnConfirmToken.objects.select_for_update().filter(pk=raw).first()
    except Exception:  # не uuid
        tok = None
    ok = (
        tok is not None and tok.used_at is None and tok.expires_at > timezone.now()
        and tok.user_id == request.user.pk and tok.scope == scope
        and tok.object_type == object_type and str(tok.object_id) == str(object_id)
        and tok.version == int(version)
    )
    if not ok:
        raise ApiError("confirm_invalid", "Токен подтверждения недействителен: истёк, уже использован или от другого документа.",
                       status_code=403)
    tok.used_at = timezone.now()
    tok.save(update_fields=["used_at"])
    return tok


# ---------- версия ----------
def parse_if_match(request, body_version=None):
    raw = (request.META.get("HTTP_IF_MATCH") or "").strip().strip('"').removeprefix("W/").strip('"')
    if not raw and body_version is not None:
        raw = str(body_version)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def require_version(request, doc_version, body_version=None):
    v = parse_if_match(request, body_version)
    if v is None:
        raise ApiError("version_required", "Нужна версия документа (If-Match).", status_code=428,
                       top={"current_version": doc_version})
    if v != doc_version:
        raise ApiError("version_conflict", "Документ уже изменён другим сотрудником. Обновите карточку.",
                       status_code=412, top={"current_version": doc_version})
    return v


# ---------- аудит ----------
def client_meta(request):
    ip = (request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR") or "").split(",")[0].strip()
    dev = (request.META.get("HTTP_X_DEVICE_ID") or request.META.get("HTTP_X_CLIENT_ID") or "")[:128]
    return dev, ip[:64]


def write_audit(request, *, company, object_type, object_id, number, v_before, v_after, action,
                before, after, diff, reason, created_by, approved_by):
    from apps.main.models import ReturnAuditLog

    dev, ip = client_meta(request)
    return ReturnAuditLog.objects.create(
        company=company, object_type=object_type, object_id=object_id, number=number or "",
        version_before=v_before, version_after=v_after, action=action, snapshot_before=before,
        snapshot_after=after, diff=diff, reason=reason or "", created_by=created_by,
        performed_by=request.user, approved_by=approved_by, device_id=dev, ip=ip,
    )


def check_reason(value, field="reason"):
    text = (value or "").strip() if isinstance(value, str) else ""
    if not (5 <= len(text) <= 500):
        raise ApiError("validation_error", "Причина — от 5 до 500 знаков.", status_code=400,
                       fields={field: ["Причина — от 5 до 500 знаков."]})
    return text


# ======================================================================
#  Возврат поставщику
# ======================================================================
def supplier_snapshot(ret):
    items = []
    total = _Z
    for it in ret.items.select_related("product").order_by("id"):
        price = Decimal(str(it.purchase_price or 0))
        line = (Decimal(str(it.qty)) * price)
        total += line
        items.append({
            "id": str(it.id), "product_id": str(it.product_id), "product_name": it.product.name,
            "receipt_item_id": str(it.receipt_item_id) if it.receipt_item_id else None,
            "qty": str(it.qty), "purchase_price": str(price), "line_total": _s(line),
        })
    return {
        "id": str(ret.id), "number": ret.number, "supplier_id": str(ret.supplier_id),
        "receipt_id": str(ret.receipt_id) if ret.receipt_id else None, "reason": ret.reason,
        "comment": ret.comment, "compensation": ret.compensation, "status": ret.status,
        "version": ret.version, "total_amount": _s(total), "items": items,
    }


def supplier_total(ret):
    total = _Z
    for it in ret.items.all():
        total += Decimal(str(it.qty)) * Decimal(str(it.purchase_price or 0))
    return total.quantize(_Q2, rounding=ROUND_HALF_UP)


def supplier_edit_state(ret, user):
    """can_edit / can_void / edit_block_reason / сроки — для карточки и списка."""
    now = timezone.now()
    until = editable_until(ret.created_at)
    reason = None
    if ret.status == ret.Status.VOIDED:
        reason = "already_voided"
    elif ret.status != ret.Status.POSTED:
        reason = "already_voided"
    elif now > until and not is_owner_like(user):
        reason = "edit_window_expired"
    elif not getattr(user, "is_authenticated", False):
        reason = "permission_denied"
    # sret2111: владелец/админ правит и после 24 ч (PIN + журнал), касса видит owner_override
    owner_override = reason is None and now > until
    return {
        "owner_override": owner_override,
        "editable_fields": ["items", "qty", "purchase_price", "new_lines", "reason", "compensation", "comment"],
        "version": ret.version, "closed_at": ret.created_at.isoformat(), "editable_until": until.isoformat(),
        "server_now": now.isoformat(), "can_edit": reason is None, "can_void": reason is None,
        "edit_block_reason": reason,
        "voided_at": ret.voided_at.isoformat() if ret.voided_at else None,
        "voided_by_name": _uname(ret.voided_by), "void_reason": ret.void_reason or "",
        "void_approved_by_name": _uname(ret.void_approved_by),
        "edited_at": ret.edited_at.isoformat() if ret.edited_at else None,
        "edited_by_name": _uname(ret.edited_by), "edit_count": ret.edit_count,
    }


def _uname(u):
    if u is None:
        return None
    full = f"{getattr(u, 'first_name', '') or ''} {getattr(u, 'last_name', '') or ''}".strip()
    return full or getattr(u, "email", None) or str(u.pk)


def _lock_supplier_return(view, pk):
    from django.shortcuts import get_object_or_404
    from apps.main.models import SupplierReturn

    qs = view._filter_qs_company_branch(SupplierReturn.objects.select_for_update(of=("self",)))
    return get_object_or_404(qs, pk=pk)


def _blocking_checks(ret, owner_override=False):
    """Общие запреты правки/аннулирования (после блокировки документа)."""
    if ret.status != ret.Status.POSTED:
        raise ApiError("already_voided", "Возврат уже аннулирован.", status_code=409)
    err = None if owner_override else window_error(ret.created_at)
    if err:
        raise err
    from apps.warehouse.services import ensure_period_open_api  # закрытый период (ТЗ «Бухгалтер»)
    from rest_framework.exceptions import APIException

    try:
        ensure_period_open_api(company=ret.company, dates=[timezone.now()], user=None)
    except APIException as exc:
        raise ApiError("period_closed", "Период закрыт — правка невозможна.", status_code=409,
                       top={"detail_original": str(getattr(exc, "detail", ""))})


def _reverse_cash(ret, user, version, amount):
    """Встречная проводка к приходу в кассу от возврата (та же касса). Возвращает созданный CashFlow."""
    from apps.construction.auto_cashflow import create_auto_cashflow
    from apps.construction.models import CashFlow

    if amount <= 0:
        return None
    base = (
        CashFlow.objects.filter(company=ret.company, source_kind=CashFlow.SourceKind.SUPPLIER_RETURN,
                                source_id=str(ret.id), type=CashFlow.Type.INCOME)
        .exclude(status=CashFlow.Status.REJECTED).order_by("created_at").first()
    )
    if base is None:
        return None
    return create_auto_cashflow(
        company=ret.company, branch=ret.branch, cashbox=base.cashbox, user=user, shift=None,
        type=CashFlow.Type.EXPENSE, amount=amount, source_kind=CashFlow.SourceKind.SUPPLIER_RETURN,
        source_id=str(ret.id), name=f"Сторно возврата поставщику №{ret.number} (v{version})",
        source_business_operation_id="Возврат поставщику", affects_shift_drawer=False,
        idempotency_key=f"supplier-return-edit:{ret.id}:v{version}:storno",
    )


def _post_cash(ret, user, version, amount):
    from apps.construction.auto_cashflow import create_auto_cashflow
    from apps.construction.models import CashFlow

    if amount <= 0:
        return None
    base = (
        CashFlow.objects.filter(company=ret.company, source_kind=CashFlow.SourceKind.SUPPLIER_RETURN,
                                source_id=str(ret.id), type=CashFlow.Type.INCOME)
        .exclude(status=CashFlow.Status.REJECTED).order_by("created_at").first()
    )
    return create_auto_cashflow(
        company=ret.company, branch=ret.branch, cashbox=getattr(base, "cashbox", None) or ret.cashbox,
        cashbox_role=None if (base or ret.cashbox_id) else "expense_variable", user=user, shift=None,
        type=CashFlow.Type.INCOME, amount=amount, source_kind=CashFlow.SourceKind.SUPPLIER_RETURN,
        source_id=str(ret.id), name=f"Возврат поставщику №{ret.number} (правка v{version})",
        source_business_operation_id="Возврат поставщику", affects_shift_drawer=False,
        idempotency_key=f"supplier-return-edit:{ret.id}:v{version}:post",
    )


def _offset_and_remember(ret, amount):
    """Зачесть долг на сумму и запомнить по каким сделкам (для следующей правки)."""
    from apps.main.models import ClientDeal
    from apps.main.views import _offset_supplier_debt

    before = {d.pk: (d.prepayment or _Z) for d in ClientDeal.objects.filter(
        company=ret.company, client=ret.supplier, kind=ClientDeal.Kind.DEBT)}
    applied = _offset_supplier_debt(ret.company, ret.supplier, ret, amount)
    allocs = []
    for d in ClientDeal.objects.filter(pk__in=list(before)):
        diff = (d.prepayment or _Z) - before[d.pk]
        if diff > 0:
            allocs.append({"deal_id": str(d.pk), "amount": str(diff)})
    ret.debt_allocations = allocs
    ret.save(update_fields=["debt_allocations"])
    return applied


def _emit_debt_changed(ret, user):
    from apps.integrations.events import emit_event

    emit_event(ret.company_id, "supplier_debt_changed", {"supplier": ret.supplier_id, "return": ret.id, "actor": user.id})


def void_supplier_return(view, request, pk):
    from apps.main.models import Product, SupplierReturn

    data = request.data or {}
    with transaction.atomic():
        ret = _lock_supplier_return(view, pk)
        require_version(request, ret.version, data.get("version"))
        reason = check_reason(data.get("reason"))
        tok = consume_confirm_token(request, scope=SCOPE_VOID, object_type=OBJ_SUPPLIER, object_id=ret.pk, version=ret.version)
        _blocking_checks(ret)
        before = supplier_snapshot(ret)
        total = supplier_total(ret)
        items = list(ret.items.all())
        list(Product.objects.select_for_update().filter(pk__in=[i.product_id for i in items]).values_list("pk", flat=True))
        for it in items:  # вернуть списанное на склад
            Product.objects.filter(pk=it.product_id).update(quantity=F("quantity") + it.qty, updated_at=timezone.now())
        new_v = ret.version + 1
        if ret.compensation == SupplierReturn.Compensation.CASH:
            _reverse_cash(ret, request.user, new_v, total)
        elif ret.compensation == SupplierReturn.Compensation.DEBT_OFFSET and total > 0:
            _unwind_debt_amount(ret, total)
            ret.debt_allocations = []
        ret.status = SupplierReturn.Status.VOIDED
        ret.version = new_v
        ret.voided_at = timezone.now()
        ret.voided_by = request.user
        ret.void_approved_by = tok.approved_by
        ret.void_reason = reason
        ret.save()
        after = supplier_snapshot(ret)
        write_audit(request, company=ret.company, object_type=OBJ_SUPPLIER, object_id=ret.pk,
                    number=str(ret.number or ""), v_before=new_v - 1, v_after=new_v, action="voided",
                    before=before, after=after, diff=[{"field": "status", "old": "posted", "new": "voided"}],
                    reason=reason, created_by=ret.created_by, approved_by=tok.approved_by)
        if ret.compensation == SupplierReturn.Compensation.DEBT_OFFSET:
            _emit_debt_changed(ret, request.user)
    return ret


def _parse_edit_items(ret, body):
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError("validation_error", "Укажите строки возврата (items).", status_code=400,
                       fields={"items": ["Нужен непустой список строк."]})
    existing = {str(i.id): i for i in ret.items.all()}
    out = []
    seen = set()
    for idx, row in enumerate(items):
        if not isinstance(row, dict):
            raise ApiError("validation_error", "Строка должна быть объектом.", status_code=400)
        lid = str(row.get("id") or "")
        if not lid:
            out.append(_new_line(ret, row, idx))  # sret2111: новая строка
            continue
        if lid not in existing or lid in seen:
            raise ApiError("validation_error", f"Строка {lid} не найдена в документе.", status_code=400,
                           fields={f"items[{idx}].id": ["Нет такой строки."]})
        seen.add(lid)
        try:
            qty = Decimal(str(row.get("qty", existing[lid].qty))).quantize(Decimal("0.001"))
        except Exception:
            raise ApiError("validation_error", "Некорректное количество.", status_code=400,
                           fields={f"items[{idx}].qty": ["Некорректное число."]})
        if qty <= 0:
            raise ApiError("validation_error", "Количество должно быть больше 0.", status_code=400,
                           fields={f"items[{idx}].qty": ["Должно быть > 0."]})
        price_raw = row.get("purchase_price", existing[lid].purchase_price)
        try:
            price = Decimal(str(price_raw if price_raw is not None else 0)).quantize(Decimal("0.0001"))
        except Exception:
            raise ApiError("validation_error", "Некорректная цена.", status_code=400,
                           fields={f"items[{idx}].purchase_price": ["Некорректное число."]})
        if price < 0:
            raise ApiError("validation_error", "Цена не может быть отрицательной.", status_code=400,
                           fields={f"items[{idx}].purchase_price": ["Должна быть >= 0."]})
        out.append((existing[lid], qty, price))
    return out


class _NewLine:
    """sret2111: строка, которой ещё нет в документе (создаётся при сохранении)."""
    pk = None

    def __init__(self, product, receipt_item):
        self.product = product
        self.product_id = product.pk
        self.receipt_item = receipt_item
        self.receipt_item_id = getattr(receipt_item, "pk", None)
        self.qty = Decimal("0")
        self.purchase_price = None


def _new_line(ret, row, idx):
    from apps.main.models import Product, SupplierReceiptItem

    pid = row.get("product") or row.get("product_id")
    product = Product.objects.filter(company_id=ret.company_id, pk=pid).first() if pid else None
    if product is None:
        raise ApiError("validation_error", "Товар новой строки не найден.", status_code=400,
                       fields={f"items[{idx}].product": ["Нет такого товара."]})
    ri = None
    rid = row.get("receipt_item") or row.get("receipt_item_id")
    if rid:
        ri = SupplierReceiptItem.objects.filter(pk=rid, product_id=product.pk,
                                                receipt__company_id=ret.company_id).first()
        if ri is None:
            raise ApiError("validation_error", "Строка прихода не найдена.", status_code=400,
                           fields={f"items[{idx}].receipt_item": ["Нет такой строки прихода."]})
    try:
        qty = Decimal(str(row.get("qty"))).quantize(Decimal("0.001"))
    except Exception:
        raise ApiError("validation_error", "Некорректное количество.", status_code=400,
                       fields={f"items[{idx}].qty": ["Некорректное число."]})
    if qty <= 0:
        raise ApiError("validation_error", "Количество должно быть больше 0.", status_code=400,
                       fields={f"items[{idx}].qty": ["Должно быть > 0."]})
    price_raw = row.get("purchase_price")
    if price_raw in (None, ""):
        price_raw = (ri.purchase_price if ri is not None else product.purchase_price) or 0
    try:
        price = Decimal(str(price_raw)).quantize(Decimal("0.0001"))
    except Exception:
        raise ApiError("validation_error", "Некорректная цена.", status_code=400,
                       fields={f"items[{idx}].purchase_price": ["Некорректное число."]})
    if price < 0:
        raise ApiError("validation_error", "Цена не может быть отрицательной.", status_code=400,
                       fields={f"items[{idx}].purchase_price": ["Должна быть >= 0."]})
    return (_NewLine(product, ri), qty, price)


def edit_supplier_return(view, request, pk):
    from apps.main.models import Product, SupplierReturn, SupplierReturnItem

    body = request.data or {}
    with transaction.atomic():
        ret = _lock_supplier_return(view, pk)
        require_version(request, ret.version, body.get("version"))
        reason = check_reason(body.get("edit_reason"), "edit_reason")
        tok = consume_confirm_token(request, scope=SCOPE_EDIT, object_type=OBJ_SUPPLIER, object_id=ret.pk, version=ret.version)
        # sret2111: после 24 ч правит только владелец/админ (сам или подтвердил PIN-ом)
        owner_override = is_owner_like(request.user) or is_owner_like(getattr(tok, "approved_by", None))
        _blocking_checks(ret, owner_override=owner_override)
        rows = _parse_edit_items(ret, body)
        new_reason = body.get("reason", ret.reason)
        if new_reason not in SupplierReturn.Reason.values:
            raise ApiError("validation_error", "Неизвестная причина возврата.", status_code=400,
                           fields={"reason": list(SupplierReturn.Reason.values)})
        new_comp = body.get("compensation", ret.compensation)
        if new_comp not in SupplierReturn.Compensation.values:
            raise ApiError("validation_error", "Неизвестный способ компенсации.", status_code=400,
                           fields={"compensation": list(SupplierReturn.Compensation.values)})
        if "document_date" in body or "date" in body:
            raise ApiError("field_not_editable", "Дату возврата сервер не хранит отдельно — её нельзя изменить.",
                           status_code=400, fields={"document_date": ["Не редактируется."]})
        before = supplier_snapshot(ret)
        old_total = supplier_total(ret)
        old_items = {i.pk: i for i in ret.items.all()}

        # склад: остаток после «вернуть старое, списать новое» не должен уйти в минус
        pids = {i.product_id for i in old_items.values()} | {r[0].product_id for r in rows}
        prods = {p.pk: p for p in Product.objects.select_for_update().filter(pk__in=pids)}
        delta = {}
        for it in old_items.values():
            delta[it.product_id] = delta.get(it.product_id, Decimal("0")) + Decimal(str(it.qty))
        for it, qty, _price in rows:
            delta[it.product_id] = delta.get(it.product_id, Decimal("0")) - qty
        for pid, d in delta.items():
            p = prods.get(pid)
            if p is not None and Decimal(str(p.quantity or 0)) + d < 0:
                raise ApiError("negative_stock", f"Не хватает остатка по товару «{p.name}».", status_code=409,
                               top={"product_id": str(pid), "available": str(Decimal(str(p.quantity or 0)) + sum(
                                   (Decimal(str(i.qty)) for i in old_items.values() if i.product_id == pid), Decimal('0')))})
        # лимит возврата по строке прихода (другие проведённые возвраты не трогаем)
        for it, qty, _price in rows:
            if it.receipt_item_id:
                purchased = Decimal(str(it.receipt_item.qty))
                other = SupplierReturnItem.objects.filter(
                    receipt_item_id=it.receipt_item_id, supplier_return__status=SupplierReturn.Status.POSTED,
                ).exclude(supplier_return=ret).aggregate(s=Sum("qty"))["s"] or Decimal("0")
                maxq = max(Decimal("0"), purchased - other)
                if qty > maxq:
                    raise ApiError("exceeds_returnable", f"Больше, чем можно вернуть по приходу (максимум {maxq}).",
                                   status_code=409, top={"product_id": str(it.product_id), "max_qty": str(maxq)})

        diff = []
        removed = set(old_items) - {r[0].pk for r in rows}
        for it_id in removed:
            diff.append({"line_id": str(it_id), "field": "removed", "old": str(old_items[it_id].qty), "new": None})
        for it, qty, price in rows:
            if it.pk is None:
                diff.append({"line_id": None, "field": "added", "product_id": str(it.product_id),
                             "old": None, "new": str(qty), "purchase_price": str(price)})
                continue
            if Decimal(str(it.qty)) != qty:
                diff.append({"line_id": str(it.pk), "field": "qty", "old": str(it.qty), "new": str(qty)})
            if Decimal(str(it.purchase_price or 0)) != price:
                diff.append({"line_id": str(it.pk), "field": "purchase_price", "old": str(it.purchase_price), "new": str(price)})
        new_comment = body.get("comment", ret.comment)
        if (new_comment or "") != (ret.comment or ""):
            diff.append({"field": "comment", "old": ret.comment, "new": new_comment})
        if new_reason != ret.reason:
            diff.append({"field": "reason", "old": ret.reason, "new": new_reason})
        if new_comp != ret.compensation:
            diff.append({"field": "compensation", "old": ret.compensation, "new": new_comp})
        if owner_override and timezone.now() > editable_until(ret.created_at):
            diff.append({"field": "owner_override", "old": None, "new": True})

        # склад: сторно старого + списание нового (одним сдвигом на товар)
        for pid, d in delta.items():
            if d:
                Product.objects.filter(pk=pid).update(quantity=F("quantity") + d, updated_at=timezone.now())
        SupplierReturnItem.objects.filter(pk__in=removed).delete()
        for it, qty, price in rows:
            if it.pk is None:  # sret2111: новая строка
                SupplierReturnItem.objects.create(supplier_return=ret, product_id=it.product_id,
                                                  receipt_item_id=it.receipt_item_id, qty=qty, purchase_price=price)
                continue
            it.qty, it.purchase_price = qty, price
            it.save(update_fields=["qty", "purchase_price"])

        new_total = supplier_total(ret)
        new_v = ret.version + 1
        old_comp = ret.compensation
        if old_comp == new_comp:
            if ret.compensation == SupplierReturn.Compensation.CASH:
                if new_total != old_total:
                    _reverse_cash(ret, request.user, new_v, old_total)
                    _post_cash(ret, request.user, new_v, new_total)
            elif ret.compensation == SupplierReturn.Compensation.DEBT_OFFSET:
                if new_total != old_total:
                    _unwind_debt_amount(ret, old_total)  # откат по старой сумме (строки уже изменены)
                    ret.debt_allocations = []
                    if new_total > 0:
                        _offset_and_remember(ret, new_total)
                    _emit_debt_changed(ret, request.user)
        else:
            # sret2111: смена компенсации — откатить старую по старой сумме, провести новую по новой
            if old_comp == SupplierReturn.Compensation.CASH:
                _reverse_cash(ret, request.user, new_v, old_total)
            elif old_comp == SupplierReturn.Compensation.DEBT_OFFSET:
                _unwind_debt_amount(ret, old_total)
                ret.debt_allocations = []
                _emit_debt_changed(ret, request.user)
            ret.compensation = new_comp
            if new_comp == SupplierReturn.Compensation.CASH:
                _post_cash(ret, request.user, new_v, new_total)
            elif new_comp == SupplierReturn.Compensation.DEBT_OFFSET:
                if new_total > 0:
                    _offset_and_remember(ret, new_total)
                _emit_debt_changed(ret, request.user)
        ret.reason = new_reason
        ret.comment = new_comment or ""
        ret.version = new_v
        ret.edited_at = timezone.now()
        ret.edited_by = request.user
        ret.edit_count = (ret.edit_count or 0) + 1
        ret.save()
        after = supplier_snapshot(ret)
        write_audit(request, company=ret.company, object_type=OBJ_SUPPLIER, object_id=ret.pk,
                    number=str(ret.number or ""), v_before=new_v - 1, v_after=new_v, action="edited",
                    before=before, after=after, diff=diff, reason=reason, created_by=ret.created_by,
                    approved_by=tok.approved_by)
    return ret


def _unwind_debt_amount(ret, amount):
    """_unwind_debt для заданной суммы (строки документа уже изменены)."""
    from apps.main.models import ClientDeal

    allocs = ret.debt_allocations
    left = Decimal(str(amount))
    if allocs:
        for a in allocs:
            deal = ClientDeal.objects.select_for_update().filter(pk=a["deal_id"]).first()
            if deal is None:
                continue
            amt = Decimal(str(a["amount"]))
            if (deal.prepayment or _Z) < amt:
                raise ApiError("debt_already_paid", "Зачёт долга нельзя откатить: долг по сделке уже погашен оплатой.",
                               status_code=409)
            deal.prepayment = (deal.prepayment or _Z) - amt
            deal.save(update_fields=["prepayment", "updated_at"])
            left -= amt
        return
    deals = list(ClientDeal.objects.select_for_update()
                 .filter(company=ret.company, client=ret.supplier, kind=ClientDeal.Kind.DEBT).order_by("-created_at"))
    for deal in deals:
        if left <= 0:
            break
        take = min(left, deal.prepayment or _Z)
        if take <= 0:
            continue
        deal.prepayment = (deal.prepayment or _Z) - take
        deal.save(update_fields=["prepayment", "updated_at"])
        left -= take
    if left > 0:
        raise ApiError("debt_already_paid", "Зачёт долга нельзя откатить: долг по сделкам уже погашен оплатой.",
                       status_code=409)


def history_payload(company, object_type, object_id):
    from apps.main.models import ReturnAuditLog

    rows = ReturnAuditLog.objects.filter(company=company, object_type=object_type, object_id=object_id) \
        .select_related("performed_by", "approved_by").order_by("version_after", "at")
    out = []
    for r in rows:
        total = (r.snapshot_after or {}).get("total_amount")
        out.append({
            "version": r.version_after, "action": r.action, "at": r.at.isoformat(),
            "user_name": _uname(r.performed_by), "approved_by_name": _uname(r.approved_by),
            "reason": r.reason, "total": total, "snapshot": r.snapshot_after, "diff": r.diff or [],
            "device_id": r.device_id, "ip": r.ip,
        })
    return out
