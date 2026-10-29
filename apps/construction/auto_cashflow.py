from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Any, Dict, List, Optional, Union

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.construction.models import Cashbox, CashFlow, CashShift

logger = logging.getLogger(__name__)

_Q2 = Decimal("0.01")

POS_SOURCE_KINDS = {
    CashFlow.SourceKind.POS_SALE,
    CashFlow.SourceKind.POS_PREPAYMENT,
    CashFlow.SourceKind.DEBT_REPAYMENT,
    "pos_sale",
    "pos_prepayment",
    "debt_repayment",
}

EXPENSE_VARIABLE_KINDS = {
    CashFlow.SourceKind.WAREHOUSE_PURCHASE,
    CashFlow.SourceKind.PROCUREMENT_RECEIPT,
    CashFlow.SourceKind.DEFECT_WRITEOFF,
    CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT,
    CashFlow.SourceKind.SUPPLIER_RETURN,
    "warehouse_purchase",
    "procurement_receipt",
    "defect_writeoff",
    "supplier_debt_payment",
    "supplier_return",
}


def resolve_auto_cashflow_status(company) -> str:
    """
    Status policy:
    - If cashflow_requests_enabled is False (default) -> approved
    - Else (cashflow_requests_enabled is True):
        - If company.subscription_plan.name == "Старт" (case-insensitive) -> approved
        - Else (auto operations) -> pending
    """
    if not company:
        return CashFlow.Status.APPROVED

    if not getattr(company, "cashflow_requests_enabled", False):
        return CashFlow.Status.APPROVED

    sub_plan = getattr(company, "subscription_plan", None)
    if sub_plan and getattr(sub_plan, "name", None):
        plan_name = str(sub_plan.name).strip().lower()
        if plan_name in ("старт", "start"):
            return CashFlow.Status.APPROVED
    return CashFlow.Status.PENDING


def resolve_cashbox(
    *,
    company,
    context: Optional[dict] = None,
    source_kind: Optional[str] = None,
    amount: Optional[Union[Decimal, str, int, float]] = None,
    user=None,
    branch=None,
    cashbox_id=None,
    require_cashbox: bool = True,
) -> Optional[Cashbox]:
    """
    Resolve cashbox based on company, context (branch_id, cashbox_role, shift_id, cashier_id),
    and source_kind according to the specification.
    """
    if amount is not None:
        try:
            amt_dec = Decimal(str(amount))
            if amt_dec <= Decimal("0"):
                return None
        except Exception:
            pass

    ctx = dict(context or {})
    if cashbox_id:
        ctx["cashbox_id"] = cashbox_id
    if user:
        ctx["user"] = user
    if branch:
        ctx["branch"] = branch
        if hasattr(branch, "id"):
            ctx["branch_id"] = str(branch.id)

    # 1. Explicit override cashbox_id
    explicit_id = ctx.get("cashbox_id") or ctx.get("cashbox")
    if explicit_id:
        if isinstance(explicit_id, Cashbox):
            if not explicit_id.is_active:
                raise DRFValidationError({"detail": "Касса находится в архиве.", "code": "cashbox_inactive"})
            return explicit_id
        cb = Cashbox.objects.filter(id=explicit_id, company=company).first()
        if cb:
            if not cb.is_active:
                raise DRFValidationError({"detail": "Касса находится в архиве.", "code": "cashbox_inactive"})
            return cb
        if require_cashbox:
            raise DRFValidationError({"detail": "Касса не найдена или не принадлежит компании.", "code": "cashbox_not_found"})

    # Debt payments use the company debt cashbox if configured.  Resolving the
    # shift is deliberately left to create_auto_cashflow so drawer accounting
    # continues to use shift_id even when its cashbox differs.
    if source_kind in (CashFlow.SourceKind.DEBT_REPAYMENT, "debt_repayment"):
        cb = Cashbox.objects.filter(
            company=company, role=Cashbox.CashboxRole.DEBT, is_active=True,
        ).order_by("-created_at").first()
        if cb:
            return cb

    if source_kind in (CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT, "supplier_debt_payment"):
        cb = Cashbox.objects.filter(
            company=company, role=Cashbox.CashboxRole.DEBT, is_active=True,
        ).order_by("-created_at").first()
        if cb:
            return cb

    # ТЗ 29.10 Б-13: приход «вне смены» — ни смена, ни касса филиала не подставляются, берём «Основную кассу»
    detached = bool(ctx.get("detached"))

    # 2. Open shift check
    shift_obj = None if detached else ctx.get("shift")
    shift_id = None if detached else ctx.get("shift_id")
    if shift_obj and getattr(shift_obj, "cashbox", None) and getattr(shift_obj.cashbox, "is_active", True):
        return shift_obj.cashbox
    if shift_id:
        sh = CashShift.objects.filter(id=shift_id, company=company, status=CashShift.Status.OPEN).select_related("cashbox").first()
        if sh and sh.cashbox and sh.cashbox.is_active:
            return sh.cashbox

    u = None if detached else (ctx.get("user") or user)
    if u and getattr(u, "is_authenticated", False):
        sh_user_qs = CashShift.objects.filter(
            company=company,
            cashier=u,
            status=CashShift.Status.OPEN,
        ).select_related("cashbox")
        b_id = ctx.get("branch_id")
        if b_id:
            sh_user_qs = sh_user_qs.filter(Q(branch_id=b_id) | Q(cashbox__branch_id=b_id))
        sh = sh_user_qs.first()
        if sh and sh.cashbox and sh.cashbox.is_active:
            return sh.cashbox

    # 3. POS operations: pos_sale, pos_prepayment, debt_repayment
    if source_kind in POS_SOURCE_KINDS or not source_kind:
        # 3a. Branch cashbox
        b_id = None if detached else ctx.get("branch_id")
        if b_id:
            cb = (
                Cashbox.objects.filter(company=company, branch_id=b_id, role=Cashbox.CashboxRole.POS_BRANCH, is_active=True)
                .order_by("-created_at")
                .first()
            )
            if cb:
                return cb
            cb = (
                Cashbox.objects.filter(company=company, branch_id=b_id, is_active=True)
                .exclude(role__in=[Cashbox.CashboxRole.EXPENSE_VARIABLE, Cashbox.CashboxRole.EXPENSE_FIXED, Cashbox.CashboxRole.SAFE, Cashbox.CashboxRole.BANK])  # ТЗ Н-33: сейф и р/с не ящик кассы
                .order_by("-created_at")
                .first()
            )
            if cb:
                return cb

        # 3b. pos_main cashbox
        cb = (
            Cashbox.objects.filter(company=company, role=Cashbox.CashboxRole.POS_MAIN, is_active=True)
            .order_by("-created_at")
            .first()
        )
        if cb:
            return cb

        cb = (
            Cashbox.objects.filter(company=company, name__icontains="основн", is_active=True)
            .order_by("-created_at")
            .first()
        )
        if cb:
            return cb

        cb = (
            Cashbox.objects.filter(company=company, is_active=True)
            .exclude(role__in=[Cashbox.CashboxRole.EXPENSE_VARIABLE, Cashbox.CashboxRole.EXPENSE_FIXED, Cashbox.CashboxRole.SAFE, Cashbox.CashboxRole.BANK])  # ТЗ Н-33: сейф и р/с не ящик кассы
            .order_by("created_at")
            .first()
        )
        if cb:
            return cb

    # 4. Expense variable kinds: warehouse_purchase, procurement_receipt, defect_writeoff, supplier_debt_payment, supplier_return
    role = ctx.get("cashbox_role")
    if source_kind in EXPENSE_VARIABLE_KINDS or role == Cashbox.CashboxRole.EXPENSE_VARIABLE:
        cb = (
            Cashbox.objects.filter(company=company, role=Cashbox.CashboxRole.EXPENSE_VARIABLE, is_active=True)
            .order_by("-created_at")
            .first()
        )
        if cb:
            return cb
        cb = (
            Cashbox.objects.filter(company=company, name__icontains="переменн", is_active=True)
            .order_by("-created_at")
            .first()
        )
        if cb:
            return cb
        cb = (
            Cashbox.objects.filter(company=company, is_consumption=True, is_active=True)
            .order_by("-created_at")
            .first()
        )
        if cb:
            return cb

    # 5. Expense fixed
    if role == Cashbox.CashboxRole.EXPENSE_FIXED:
        cb = (
            Cashbox.objects.filter(company=company, role=Cashbox.CashboxRole.EXPENSE_FIXED, is_active=True)
            .order_by("-created_at")
            .first()
        )
        if cb:
            return cb
        cb = (
            Cashbox.objects.filter(company=company, name__icontains="постоянн", is_active=True)
            .order_by("-created_at")
            .first()
        )
        if cb:
            return cb

    # 6. Role pos_branch or pos_main specified in context
    if role == Cashbox.CashboxRole.POS_BRANCH:
        b_id = ctx.get("branch_id")
        cb_qs = Cashbox.objects.filter(company=company, role=Cashbox.CashboxRole.POS_BRANCH, is_active=True)
        if b_id:
            cb_qs = cb_qs.filter(branch_id=b_id)
        cb = cb_qs.order_by("-created_at").first()
        if cb:
            return cb
        if b_id:
            cb = Cashbox.objects.filter(company=company, branch_id=b_id, is_active=True).order_by("-created_at").first()
            if cb:
                return cb

    if role == Cashbox.CashboxRole.POS_MAIN:
        cb = (
            Cashbox.objects.filter(company=company, role=Cashbox.CashboxRole.POS_MAIN, is_active=True)
            .order_by("-created_at")
            .first()
        )
        if cb:
            return cb

    # 7. General fallback: first cashbox of company
    cb = Cashbox.objects.filter(company=company, is_active=True).order_by("created_at").first()
    if cb:
        return cb

    if require_cashbox:
        raise DRFValidationError({"detail": "Не удалось определить кассу для операции", "code": "cashbox_unresolvable"})

    return None


def resolve_auto_cashbox(
    company,
    user=None,
    branch=None,
    cashbox_id=None,
    require_cashbox: bool = True,
) -> Optional[Cashbox]:
    """
    Backward-compatible order of resolution:
    1. Explicit cashbox_id in body/request.
    2. Cashbox of open shift of current cashier/user.
    3. If require_cashbox is True -> 400 {"cashbox_id": ["cashbox_required"]}.
    4. Otherwise None.
    """
    if cashbox_id:
        qs = Cashbox.objects.filter(id=cashbox_id, company=company)
        if branch is not None:
            qs = qs.filter(branch=branch)
        cashbox = qs.first()
        if cashbox:
            if not cashbox.is_active:
                if require_cashbox:
                    raise DRFValidationError({"detail": "Касса находится в архиве.", "code": "cashbox_inactive"})
                return None
            return cashbox
        if require_cashbox:
            raise DRFValidationError({"cashbox_id": ["cashbox_required"]})

    if user and getattr(user, "is_authenticated", False):
        shift_qs = CashShift.objects.filter(
            company=company,
            cashier=user,
            status=CashShift.Status.OPEN,
        ).select_related("cashbox")
        if branch is not None:
            shift_qs = shift_qs.filter(branch=branch)
        shift = shift_qs.first()
        if shift and shift.cashbox and shift.cashbox.is_active:
            return shift.cashbox

    if require_cashbox:
        raise DRFValidationError({"cashbox_id": ["cashbox_required"]})

    return None


def create_auto_cashflow(
    *,
    company,
    branch=None,
    cashbox=None,
    cashbox_id=None,
    cashbox_role=None,
    context=None,
    user=None,
    shift=None,
    shift_id=None,
    type: str,
    amount: Union[Decimal, str, int, float],
    source_kind: str,
    source_id: str,
    name: str = "",
    source_business_operation_id: Optional[str] = None,
    category=None,
    affects_shift_drawer: Optional[bool] = None,
    idempotency_key: Optional[str] = None,
    payment_method: Optional[str] = None,
    detached: bool = False,
) -> Optional[CashFlow]:
    """
    Creates an auto-cashflow record within the transaction.
    Guarantees idempotency on idempotency_key (if provided) or (company, source_kind, source_id, type, amount).
    """
    if amount is None:
        return None
    try:
        amount_dec = Decimal(str(amount)).quantize(_Q2, rounding=ROUND_HALF_UP)
    except Exception:
        amount_dec = Decimal("0.00")

    if amount_dec <= Decimal("0.00"):
        return None

    # Idempotency check
    if idempotency_key:
        existing = CashFlow.objects.filter(
            company=company,
            idempotency_key=str(idempotency_key),
        ).first()
        if existing:
            return existing
    else:
        # Без ключа дублем считаем только ту же операцию тем же способом оплаты;
        # отклонённое движение не мешает провести новое событие.
        existing = CashFlow.objects.filter(
            company=company,
            source_kind=source_kind,
            source_id=str(source_id),
            type=type,
            amount=amount_dec,
            payment_method=(str(payment_method).strip().lower() if payment_method else None),
        ).exclude(status=CashFlow.Status.REJECTED).first()
        if existing:
            return existing

    # Resolve cashbox
    if cashbox is not None:
        if not getattr(cashbox, "is_active", True):
            raise DRFValidationError({"detail": "Касса находится в архиве.", "code": "cashbox_inactive"})
    else:
        ctx = dict(context or {})
        if detached:
            ctx["detached"] = True  # ТЗ 29.10 Б-13
        if cashbox_id:
            ctx["cashbox_id"] = cashbox_id
        if cashbox_role:
            ctx["cashbox_role"] = cashbox_role
        if branch:
            ctx["branch"] = branch
            if hasattr(branch, "id"):
                ctx["branch_id"] = str(branch.id)
        if shift:
            ctx["shift"] = shift
            if hasattr(shift, "id"):
                ctx["shift_id"] = str(shift.id)
        elif shift_id:
            ctx["shift_id"] = str(shift_id)
        if user:
            ctx["user"] = user

        cashbox = resolve_cashbox(
            company=company,
            context=ctx,
            source_kind=source_kind,
            amount=amount_dec,
            require_cashbox=True,
        )

    actual_branch = branch or (cashbox.branch if cashbox else None)

    # Shift & cashier logic
    actual_shift = None
    if detached:
        # ТЗ 29.10 Б-13: деньги принял владелец «в руки» — ни в одну смену не попадают (shift=NULL)
        pass
    elif source_kind not in EXPENSE_VARIABLE_KINDS:
        actual_shift = shift
        if actual_shift is None and shift_id:
            actual_shift = CashShift.objects.filter(id=shift_id, company=company).first()
        if actual_shift is None and cashbox and user and getattr(user, "is_authenticated", False):
            actual_shift = CashShift.objects.filter(
                cashbox=cashbox,
                cashier=user,
                status=CashShift.Status.OPEN,
            ).first()

        if actual_shift and cashbox and actual_shift.cashbox_id != cashbox.id:
            actual_shift = CashShift.objects.filter(
                cashbox=cashbox,
                cashier=user,
                status=CashShift.Status.OPEN,
            ).first() if (user and getattr(user, "is_authenticated", False)) else None
    elif shift is not None and affects_shift_drawer and cashbox and shift.cashbox_id == cashbox.id:
        # Явная оплата из ящика открытой смены (закупка «из кассы смены», ТЗ Н-03):
        # один расход, привязанный к смене и входящий в expected_cash.
        actual_shift = shift

    actual_cashier = (
        user
        if (user and getattr(user, "is_authenticated", False))
        else (actual_shift.cashier if actual_shift else None)
    )

    pm_clean = (str(payment_method).strip().lower() if payment_method else None)

    # Determine affects_shift_drawer
    if actual_shift is None:
        drawer_flag = False
    elif affects_shift_drawer is not None:
        drawer_flag = affects_shift_drawer
    elif source_kind in (CashFlow.SourceKind.SHIFT_DRAWER_OUTFLOW, CashFlow.SourceKind.SHIFT_DRAWER_INFLOW):
        drawer_flag = True
    elif source_kind in (CashFlow.SourceKind.DEBT_REPAYMENT, CashFlow.SourceKind.POS_PREPAYMENT):
        drawer_flag = (pm_clean == "cash" or pm_clean is None)
    else:
        drawer_flag = False

    status = resolve_auto_cashflow_status(company)

    default_names = {
        CashFlow.SourceKind.POS_SALE: "Продажа",
        CashFlow.SourceKind.POS_PREPAYMENT: "Предоплата",
        CashFlow.SourceKind.DEBT_REPAYMENT: "Оплата долга",
        CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT: "Оплата долга поставщику",
        CashFlow.SourceKind.WAREHOUSE_PURCHASE: "Склад",
        CashFlow.SourceKind.PROCUREMENT_RECEIPT: "Закупки",
        CashFlow.SourceKind.SUPPLIER_RETURN: "Возврат поставщику",
        CashFlow.SourceKind.DEFECT_WRITEOFF: "Списание брака",
        CashFlow.SourceKind.PRODUCT_RETURN: "Возврат товара",
        CashFlow.SourceKind.SHIFT_DRAWER_OUTFLOW: "Расход из кассы",
        CashFlow.SourceKind.SHIFT_DRAWER_INFLOW: "Внесение в кассу",
        CashFlow.SourceKind.POS_SALE_RETURN: "Возврат продажи",
        CashFlow.SourceKind.MANUAL: "Ручная операция",
    }

    final_name = (name or "").strip() or default_names.get(source_kind, "Авто-операция")
    legacy_op_id = source_business_operation_id or default_names.get(source_kind, "Авто-операция")

    if category is None:
        from apps.construction.categories import category_for_source

        category = category_for_source(company, source_kind)

    cf = CashFlow(
        company=company,
        branch=actual_branch,
        cashbox=cashbox,
        shift=actual_shift,
        cashier=actual_cashier,
        type=type,
        name=final_name,
        amount=amount_dec,
        status=status,
        affects_shift_drawer=drawer_flag,
        source_kind=source_kind,
        source_id=str(source_id),
        source_business_operation_id=legacy_op_id,
        category=category,
        idempotency_key=str(idempotency_key) if idempotency_key else None,
        payment_method=pm_clean,
    )
    cf.save()

    # Колокольчик/WS: market.cashflow.created | market.cashflow.pending
    try:
        from apps.main.notifications_market import notify_cashflow

        notify_cashflow(cf, actor=actual_cashier)
    except Exception:
        logging.getLogger("nurcrm.websocket.notifications").error(
            "notify_cashflow hook failed for cf=%s", cf.id, exc_info=True
        )

    return cf


def serialize_auto_cashflows(cashflows: List[Optional[CashFlow]]) -> List[Dict[str, Any]]:
    """
    Serializes cashflow objects into the standard API response format.
    """
    res = []
    for cf in cashflows:
        if cf is None:
            continue
        cb_name = None
        if cf.cashbox:
            if cf.cashbox.branch:
                cb_name = f"Касса филиала {cf.cashbox.branch.name}"
            else:
                cb_name = cf.cashbox.name or "Касса"
        res.append({
            "id": str(cf.id),
            "type": cf.type,
            "amount": f"{cf.amount:.2f}",
            "cashbox": str(cf.cashbox_id) if cf.cashbox_id else None,
            "cashbox_name": cb_name or "Касса",
            "status": cf.status,
            "source_kind": cf.source_kind or "",
            "source_id": cf.source_id or "",
            "shift_id": str(cf.shift_id) if cf.shift_id else None,
            "name": cf.name or "",
            "payment_method": getattr(cf, "payment_method", None) or "",
            "affects_shift_drawer": bool(getattr(cf, "affects_shift_drawer", False)),
        })
    return res


def handle_cashflow_reject(cashflow: CashFlow, user=None):
    """
    Reject cascade:
    - pos_sale, pos_prepayment: delete related sale
    - debt_repayment: rollback deal/debt payment (customer)
    - supplier_debt_payment: rollback deal payment (supplier)
    - warehouse_purchase: delete product
    - procurement_receipt, supplier_return, defect_writeoff, product_return: unpost document
    """
    source_kind = cashflow.source_kind
    if not source_kind:
        legacy_map = {
            "Продажа": CashFlow.SourceKind.POS_SALE,
            "Оплата долга": CashFlow.SourceKind.DEBT_REPAYMENT,
            "Склад": CashFlow.SourceKind.WAREHOUSE_PURCHASE,
            "Закупки": CashFlow.SourceKind.PROCUREMENT_RECEIPT,
            "Возврат поставщику": CashFlow.SourceKind.SUPPLIER_RETURN,
            "Списание брака": CashFlow.SourceKind.DEFECT_WRITEOFF,
            "Возврат товара": CashFlow.SourceKind.PRODUCT_RETURN,
        }
        op_id = (cashflow.source_business_operation_id or "").strip()
        source_kind = legacy_map.get(op_id)

    source_id = cashflow.source_id or cashflow.source_cashbox_flow_id
    if not source_id and not source_kind:
        return

    company = cashflow.company

    if source_kind == CashFlow.SourceKind.POS_SALE_RETURN:
        from rest_framework.exceptions import ValidationError as DRFValidationError
        raise DRFValidationError(
            {"detail": "Невозможно отклонить возврат чека: склад и долг уже изменены."},
            code="reject_cascade_failed",
        )

    if source_kind in (CashFlow.SourceKind.POS_SALE, CashFlow.SourceKind.POS_PREPAYMENT):
        from apps.main.models import Sale
        from rest_framework.exceptions import ValidationError as DRFValidationError
        if source_id:
            sale = Sale.objects.filter(company=company, id=source_id).first()
            if sale:
                if sale.status in (Sale.Status.CANCELED, getattr(Sale.Status, "PARTIALLY_RETURNED", "partially_returned")) or sale.returns.exists():
                    raise DRFValidationError(
                        {"detail": "Невозможно отклонить продажу: чек уже возвращён или отменён."},
                        code="reject_cascade_failed",
                    )
                sale.delete()

    elif source_kind in (CashFlow.SourceKind.DEBT_REPAYMENT, CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT):
        from apps.main.models import ClientDeal, DealPayment, Debt, DebtPayment
        key = cashflow.idempotency_key or ""
        if key.startswith("deal-pay:"):
            pay = DealPayment.objects.filter(company=company, id=key.split(":", 1)[1]).first()
            if pay:
                inst = pay.installment
                if inst:
                    new_paid = max(Decimal("0.00"), (inst.paid_amount or Decimal("0.00")) - pay.amount)
                    inst.paid_amount = new_paid
                    if new_paid < inst.amount:
                        inst.paid_on = None
                    inst.save(update_fields=["paid_amount", "paid_on"])
                pay.delete()
        elif key.startswith("sale-pay-debt:") and source_id:
            # pos/sales/{id}/pay-debt/: один приход гасил несколько взносов сделки
            from uuid import UUID, uuid5
            from apps.main.models import DealInstallment
            base = UUID(key.split(":", 1)[1])
            inst_ids = list(DealInstallment.objects.filter(deal_id=source_id).values_list("id", flat=True))
            keys = [uuid5(base, str(i)) for i in inst_ids]
            for pay in DealPayment.objects.filter(deal_id=source_id, idempotency_key__in=keys).select_related("installment"):
                inst = pay.installment
                if inst:
                    new_paid = max(Decimal("0.00"), (inst.paid_amount or Decimal("0.00")) - pay.amount)
                    inst.paid_amount = new_paid
                    if new_paid < inst.amount:
                        inst.paid_on = None
                    inst.save(update_fields=["paid_amount", "paid_on"])
                pay.delete()
        elif key.startswith("debt-pay:"):
            DebtPayment.objects.filter(company=company, id=key.split(":", 1)[1]).delete()
        elif source_id:
            deal = ClientDeal.objects.filter(company=company, id=source_id).first()
            if deal:
                pay = (
                    DealPayment.objects.filter(deal=deal, kind=DealPayment.Kind.PAY)
                    .order_by("-paid_date", "-id")
                    .first()
                )
                if pay:
                    inst = pay.installment
                    if inst:
                        new_paid = max(Decimal("0.00"), (inst.paid_amount or Decimal("0.00")) - pay.amount)
                        inst.paid_amount = new_paid
                        if new_paid < inst.amount:
                            inst.paid_on = None
                        inst.save(update_fields=["paid_amount", "paid_on"])
                    pay.delete()
            else:
                debt = Debt.objects.filter(company=company, id=source_id).first()
                if debt:
                    dp = DebtPayment.objects.filter(debt=debt).order_by("-paid_at", "-id").first()
                    if dp:
                        dp.delete()

    elif source_kind == CashFlow.SourceKind.WAREHOUSE_PURCHASE:
        from apps.main.models import Product
        if source_id:
            prod = Product.objects.filter(company=company, id=source_id).first()
            if prod:
                try:
                    prod.delete()
                except ProtectedError:
                    raise DRFValidationError({
                        "detail": f"Невозможно удалить товар «{prod.name}»: на него ссылаются другие объекты."
                    })

    elif source_kind in (
        CashFlow.SourceKind.PROCUREMENT_RECEIPT,
        CashFlow.SourceKind.SUPPLIER_RETURN,
        CashFlow.SourceKind.DEFECT_WRITEOFF,
        CashFlow.SourceKind.PRODUCT_RETURN,
    ):
        from apps.warehouse import models as wh_models
        from apps.warehouse import services as wh_services
        if source_id:
            doc = wh_models.Document.objects.filter(company=company, id=source_id).first()
            if doc:
                try:
                    wh_services.unpost_document(doc)
                    doc.status = wh_models.Document.Status.REJECTED
                    doc.save(update_fields=["status"])
                except Exception as exc:
                    raise DRFValidationError({
                        "detail": f"Невозможно откатить документ {doc.number}: {exc}"
                    })
