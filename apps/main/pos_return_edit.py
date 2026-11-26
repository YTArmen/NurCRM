"""
ТЗ 10.11: правка и аннулирование «Возврата от клиента» (SaleReturn) в течение 24 часов.

PATCH pos/returns/<id>/         полный новый состав: items [{sale_item_id, quantity}]
POST  pos/returns/<id>/void/    аннулирование (запись остаётся, status=voided, суммы обнуляются)
GET   pos/returns/<id>/history/ журнал версий (владелец/администратор)

Принцип — «отменить старое, провести новое» в одной транзакции под блокировкой чека:
  1) строки чека, шапка и оплаты возвращаются к снимку `sale_before` (состояние до этого возврата);
  2) остаток на складе снимается обратно (то, что возврат вернул на склад);
  3) деньги: на каждое движение возврата — встречное (то же кассе/смене/способу);
  4) при правке — обычный `_execute_sale_return` проводит новый состав, результат переносится в ту же запись.

Не поддерживается (409 с кодом в edit_block_reason): возвраты до 10.11 (нет снимка/списка движений),
чеки с долгом (сделка по чеку), агентские чеки, если после возврата был другой возврат по чеку,
если смена с деньгами возврата уже закрыта.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.main import return_edit as re_
from core.exceptions import ApiError

_Z = Decimal("0.00")


def _models():
    from apps.main.models import ClientDeal, Product, ProductVariant, Sale, SaleItem, SalePayment, SaleReturn
    return ClientDeal, Product, ProductVariant, Sale, SaleItem, SalePayment, SaleReturn


def pos_snapshot(r):
    return {
        "id": str(r.id), "sale_id": str(r.sale_id), "sale_number": getattr(r.sale, "doc_number", None),
        "status": r.status, "version": r.version, "is_full": r.is_full, "is_defect": r.is_defect,
        "reason": r.reason, "refund_method": r.refund_method,
        "refund_cash": str(r.refund_cash), "refund_noncash": str(r.refund_noncash),
        "total_amount": str(r.returned_amount), "items": list(r.returned_items or []),
    }


def block_reason(r):
    """Код причины, почему нельзя править/аннулировать, либо None. Окно проверяется отдельно (window_error)."""
    ClientDeal, _P, _PV, Sale, _SI, _SP, SaleReturn = _models()
    if r.status != "posted":
        return "already_voided"
    if timezone.now() > re_.editable_until(r.created_at):
        return "edit_window_expired"
    if not r.sale_before or r.cashflow_ids is None:
        return "legacy_return_not_supported"
    sale = r.sale
    if ClientDeal.objects.filter(sale=sale).exists():
        return "debt_sale_not_supported"
    if sale.agent_allocations.exists():
        return "agent_sale_not_supported"
    if SaleReturn.objects.filter(sale=sale, status="posted", created_at__gt=r.created_at).exclude(pk=r.pk).exists():
        return "has_later_documents"
    from apps.construction.models import CashFlow, CashShift
    for cf in CashFlow.objects.filter(pk__in=r.cashflow_ids).select_related("shift"):
        if cf.shift_id and cf.shift.status != CashShift.Status.OPEN:
            return "shift_closed"
    return None


def edit_state(r, user):
    now = timezone.now()
    reason = block_reason(r)
    if reason is None and not getattr(user, "is_authenticated", False):
        reason = "permission_denied"
    return {
        "status": r.status, "version": r.version, "closed_at": r.created_at.isoformat(),
        "editable_until": re_.editable_until(r.created_at).isoformat(), "server_now": now.isoformat(),
        "can_edit": reason is None, "can_void": reason is None, "edit_block_reason": reason,
        "voided_at": r.voided_at.isoformat() if r.voided_at else None,
        "voided_by_name": re_._uname(r.voided_by), "void_reason": r.void_reason or "",
        "void_approved_by_name": re_._uname(r.void_approved_by),
        "edited_at": r.edited_at.isoformat() if r.edited_at else None,
        "edited_by_name": re_._uname(r.edited_by), "edit_count": r.edit_count,
    }


def _lock_return(view, pk):
    from django.shortcuts import get_object_or_404
    _CD, _P, _PV, Sale, _SI, _SP, SaleReturn = _models()

    ret = get_object_or_404(SaleReturn.objects.select_for_update(of=("self",)).filter(company=view._company()), pk=pk)
    # доступ к чеку — по филиалу, как у обычного возврата
    get_object_or_404(view._filter_qs_company_branch(Sale.objects.all()), pk=ret.sale_id)
    return ret


def _stock_back(item, qty):
    """Обратное к _restock_product_for_sale_item_return: снять со склада то, что возврат туда вернул."""
    from apps.main.pos_utils import qty3
    _CD, Product, ProductVariant, _S, _SI, _SP, _SR = _models()
    from apps.main.pos_views import line_qty_consume_units

    if not item.product_id:
        return
    if item.product and item.product.kind == Product.Kind.SERVICE:
        return
    delta = line_qty_consume_units(qty, getattr(item, "sale_package", None))
    if getattr(item, "sale_unit_id", None):
        from apps.main.tz26.hw_units.units import unit_factor
        delta = qty3(delta * unit_factor(item))
    if delta <= 0:
        return
    prod = Product.objects.select_for_update().get(pk=item.product_id)
    if Decimal(str(prod.quantity or 0)) - delta < 0:
        raise ApiError("negative_stock", f"Не хватает остатка по товару «{prod.name}»: возвращённое уже продано.",
                       status_code=409, top={"product_id": str(prod.pk), "available": str(prod.quantity)})
    Product.objects.filter(pk=prod.pk).update(quantity=F("quantity") - delta, updated_at=timezone.now())
    if getattr(item, "variant_id", None):
        ProductVariant.objects.filter(pk=item.variant_id).update(quantity=F("quantity") - delta, updated_at=timezone.now())


def _undo_return(r, sale, user, new_v):
    """Вернуть чек, склад и деньги к состоянию до этого возврата (возврат-запись не трогаем)."""
    from apps.main.serials import unmark_return

    unmark_return(r)  # 13.11.2026: серийники снова числятся проданными
    from apps.main.sale_original import _restore_item
    _CD, _P, _PV, Sale, SaleItem, SalePayment, SaleReturn = _models()
    from apps.construction.auto_cashflow import create_auto_cashflow
    from apps.construction.models import CashFlow

    snap = r.sale_before or {}
    # 1) строки чека
    existing = {str(i.id): i for i in SaleItem.objects.select_for_update().filter(sale=sale)}
    for d in snap.get("items", []):
        cur = existing.get(str(d["id"]))
        if cur is not None:
            cur.quantity = Decimal(str(d["quantity"]))
            cur.line_discount = Decimal(str(d.get("line_discount") or 0))
            cur.save(update_fields=["quantity", "line_discount"])
        else:
            restored = _restore_item(sale, d)
            restored.save(force_insert=True)
    # 2) склад: снимаем то, что возврат вернул (брак на склад не возвращается)
    items_now = {str(i.id): i for i in SaleItem.objects.filter(sale=sale).select_related("product", "sale_package")}
    for row in (r.returned_items or []):
        if not row.get("restock"):
            continue
        it = items_now.get(str(row.get("sale_item")))
        if it is not None:
            _stock_back(it, Decimal(str(row.get("qty") or 0)))
    # 3) шапка и оплаты
    sale.subtotal = Decimal(str(snap.get("subtotal") or 0))
    sale.discount_total = Decimal(str(snap.get("discount_total") or 0))
    sale.tax_total = Decimal(str(snap.get("tax_total") or 0))
    sale.total = Decimal(str(snap.get("total") or 0))
    sale.cash_received = Decimal(str(snap.get("cash_received") or 0))
    if sale.consultant_id and getattr(sale, "consultant_commission_enabled", False) and getattr(sale, "consultant_commission_percent", None):
        pct = Decimal(str(sale.consultant_commission_percent))
        sale.consultant_commission_amount = (Decimal(str(sale.total)) * pct / Decimal("100")).quantize(Decimal("0.01")) if pct > 0 else _Z
    others = SaleReturn.objects.filter(sale=sale, status="posted").exclude(pk=r.pk).exists()
    sale.status = getattr(Sale.Status, "PARTIALLY_RETURNED", "partially_returned") if others else Sale.Status.PAID
    sale.save(update_fields=["subtotal", "discount_total", "tax_total", "total", "cash_received",
                             "consultant_commission_amount", "status"])
    sale.payments.all().delete()
    for p in snap.get("payments", []):
        SalePayment.objects.create(sale=sale, company=sale.company, method=p["method"], amount=Decimal(str(p["amount"])))
    # 4) деньги: встречное движение на каждое движение возврата
    for cf in CashFlow.objects.filter(pk__in=r.cashflow_ids).select_related("shift", "cashbox"):
        opposite = CashFlow.Type.INCOME if cf.type == CashFlow.Type.EXPENSE else CashFlow.Type.EXPENSE
        create_auto_cashflow(
            company=sale.company, branch=sale.branch, cashbox=cf.cashbox, user=user, shift=cf.shift,
            type=opposite, amount=cf.amount, source_kind=CashFlow.SourceKind.POS_SALE_RETURN,
            source_id=str(sale.id), idempotency_key=f"pos-return-edit:{r.id}:v{new_v}:{cf.pk}",
            affects_shift_drawer=bool(cf.affects_shift_drawer), payment_method=cf.payment_method,
            name=f"Сторно возврата по чеку №{sale.doc_number or sale.id} (v{new_v})",
            source_business_operation_id="pos_sale_return",
        )


def _zero_return(r):
    r.returned_amount = _Z
    r.refund_cash = _Z
    r.refund_noncash = _Z
    r.returned_items = []
    r.items_payload = None
    r.cashflow_ids = []


def _common_checks(request, r, version_body, scope):
    re_.require_version(request, r.version, version_body)
    tok = re_.consume_confirm_token(request, scope=scope, object_type=re_.OBJ_POS, object_id=r.pk, version=r.version)
    if r.status != "posted":
        raise ApiError("already_voided", "Возврат уже аннулирован.", status_code=409)
    err = re_.window_error(r.created_at)
    if err:
        raise err
    reason = block_reason(r)
    if reason:
        raise ApiError(reason, _BLOCK_TEXT.get(reason, "Правка невозможна."), status_code=409)
    from apps.warehouse.services import ensure_period_open_api
    from rest_framework.exceptions import APIException
    try:
        ensure_period_open_api(company=r.company, dates=[timezone.now()], user=None)
    except APIException:
        raise ApiError("period_closed", "Период закрыт — правка невозможна.", status_code=409)
    return tok


_BLOCK_TEXT = {
    "legacy_return_not_supported": "Возврат создан до включения правки — исправить его нельзя.",
    "debt_sale_not_supported": "Правка возврата по чеку в долг пока не поддерживается.",
    "agent_sale_not_supported": "Правка возврата по агентскому чеку не поддерживается.",
    "has_later_documents": "После этого возврата по чеку был другой возврат — сначала исправьте последний.",
    "shift_closed": "Смена, в которой выдали деньги по возврату, уже закрыта.",
}


def _feed_sale_changed(r, sale, user, action):
    """feed2011: лента/поток — чек изменился (возврат исправлен или аннулирован)."""
    try:
        from apps.main.change_feed import record_change

        record_change(sale.company_id, "sale_changed", branch_id=sale.branch_id, actor_id=getattr(user, "pk", None),
                      entity="sale", entity_id=sale.pk,
                      data={"sale": str(sale.pk), "number": sale.doc_number, "return": str(r.pk), "action": action,
                            "version": r.version, "shift": str(r.shift_id) if getattr(r, "shift_id", None) else None})
    except Exception:  # noqa: BLE001 — лента не роняет операцию
        pass


def void_pos_return(view, request, pk):
    body = request.data or {}
    with transaction.atomic():
        r = _lock_return(view, pk)
        reason = re_.check_reason(body.get("reason"))
        tok = _common_checks(request, r, body.get("version"), re_.SCOPE_VOID)
        _CD, _P, _PV, Sale, _SI, _SP, SaleReturn = _models()
        sale = Sale.objects.select_for_update().get(pk=r.sale_id)
        before = pos_snapshot(r)
        new_v = r.version + 1
        _undo_return(r, sale, request.user, new_v)
        _zero_return(r)
        r.status = "voided"
        r.version = new_v
        r.voided_at = timezone.now()
        r.voided_by = request.user
        r.void_approved_by = tok.approved_by
        r.void_reason = reason
        r.save()
        re_.write_audit(request, company=r.company, object_type=re_.OBJ_POS, object_id=r.pk,
                        number=str(sale.doc_number or ""), v_before=new_v - 1, v_after=new_v, action="voided",
                        before=before, after=pos_snapshot(r),
                        diff=[{"field": "status", "old": "posted", "new": "voided"}], reason=reason,
                        created_by=r.user, approved_by=tok.approved_by)
        _feed_sale_changed(r, sale, request.user, "return_voided")
    return r


def _parse_items(sale, body):
    from apps.main.pos_views import _parse_partial_return_items
    from rest_framework.exceptions import ValidationError as DRFValidationError

    raw = body.get("items")
    if not isinstance(raw, list) or not raw:
        raise ApiError("validation_error", "Укажите состав возврата (items). Чтобы убрать возврат совсем — аннулируйте его.",
                       status_code=400, fields={"items": ["Нужен непустой список."]})
    try:
        return _parse_partial_return_items({"items": raw})
    except DRFValidationError as exc:
        raise ApiError("validation_error", "Некорректный состав возврата.", status_code=400, fields={"items": [str(exc.detail)]})


def edit_pos_return(view, request, pk):
    from apps.main.pos_views import _execute_sale_return
    from rest_framework.exceptions import ValidationError as DRFValidationError
    from django.core.exceptions import ValidationError as DjangoValidationError

    body = request.data or {}
    with transaction.atomic():
        r = _lock_return(view, pk)
        reason = re_.check_reason(body.get("edit_reason"), "edit_reason")
        tok = _common_checks(request, r, body.get("version"), re_.SCOPE_EDIT)
        _CD, _P, _PV, Sale, _SI, _SP, SaleReturn = _models()
        sale = Sale.objects.select_for_update().get(pk=r.sale_id)
        partial = _parse_items(sale, body)
        before = pos_snapshot(r)
        new_v = r.version + 1

        _undo_return(r, sale, request.user, new_v)
        sale.refresh_from_db()
        key = f"{r.idempotency_key}:v{new_v}"
        refund_method = r.refund_method if (r.refund_method and r.refund_method != str(sale.payment_method)) else None
        try:
            _execute_sale_return(
                sale, partial, is_defect=r.is_defect, user=request.user, strict_errors=True,
                payload={"idempotency_key": key, "refund_method": refund_method, "reason": r.reason},
            )
        except (DRFValidationError, DjangoValidationError) as exc:
            if isinstance(exc, ApiError):
                raise
            detail = getattr(exc, "detail", None) or getattr(exc, "message_dict", None) or str(exc)
            raise ApiError("validation_error", "Новый состав возврата не прошёл проверку.", status_code=400,
                           fields={"items": [str(detail)]})
        new = SaleReturn.objects.get(company=sale.company, idempotency_key=key)
        for f in ("returned_amount", "is_full", "items_payload", "returned_items", "refund_method", "refund_cash",
                  "refund_noncash", "shift", "cashflow_ids"):
            setattr(r, f, getattr(new, f))
        new.delete()  # временная запись; документ остаётся прежним (тот же id)
        r.version = new_v
        r.edited_at = timezone.now()
        r.edited_by = request.user
        r.edit_count = (r.edit_count or 0) + 1
        r.save()
        after = pos_snapshot(r)
        diff = _diff(before, after)
        re_.write_audit(request, company=r.company, object_type=re_.OBJ_POS, object_id=r.pk,
                        number=str(sale.doc_number or ""), v_before=new_v - 1, v_after=new_v, action="edited",
                        before=before, after=after, diff=diff, reason=reason, created_by=r.user,
                        approved_by=tok.approved_by)
        _feed_sale_changed(r, sale, request.user, "return_edited")
    return r


def _diff(before, after):
    old = {str(x.get("sale_item")): Decimal(str(x.get("qty") or 0)) for x in before.get("items", [])}
    new = {str(x.get("sale_item")): Decimal(str(x.get("qty") or 0)) for x in after.get("items", [])}
    out = []
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            out.append({"line_id": k, "field": "qty", "old": str(old[k]) if k in old else None,
                        "new": str(new[k]) if k in new else None})
    if before.get("total_amount") != after.get("total_amount"):
        out.append({"field": "total_amount", "old": before.get("total_amount"), "new": after.get("total_amount")})
    return out
