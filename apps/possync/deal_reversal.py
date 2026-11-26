"""
Обратная проводка в кассе при возврате оплаты по сделке и при удалении сделки с оплатами
(ТЗ веб-кассы, п. 12.1).

Выбрано: ОБРАТНАЯ ОПЕРАЦИЯ (а не пометка исходной). Исходный приход остаётся в журнале, рядом
появляется расход на ту же сумму в ту же кассу (по клиенту — расход, по поставщику — приход).
Так остаток кассы, сверка смены и аудит сходятся без правки уже закрытых операций. Ключ
идемпотентности привязан к платежу/сделке, повтор запроса дубль не создаёт.

- POST …/deals/{id}/refund/ (и unpay): после возврата взноса создаётся обратная операция на сумму возврата.
- DELETE …/deals/{id}/: по каждому способу оплаты считается чистая оплата (оплаты − возвраты);
  если она > 0 — обратная операция на эту сумму, затем сделка удаляется.
Ответ refund дополняется полем `reverse_cashflow` (id, type, amount, cashbox).
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from rest_framework import status

from apps.construction.auto_cashflow import create_auto_cashflow
from apps.construction.models import CashFlow
from apps.main.models import Client, ClientDeal, DealPayment
from apps.main.views import ClientDealRefundAPIView, ClientDealRetrieveUpdateDestroyAPIView

ZERO = Decimal("0.00")


def _is_supplier(deal) -> bool:
    c = deal.client
    return bool(c) and getattr(c, "type", None) in (Client.StatusClient.SUPPLIERS, "suppliers")


def _original_cashbox(deal, method):
    """Касса, куда пришли оплаты по сделке (чтобы вернуть деньги оттуда же)."""
    qs = CashFlow.objects.filter(
        company_id=deal.company_id, source_id=str(deal.id), idempotency_key__startswith="deal-pay:"
    ).select_related("cashbox").order_by("-created_at")
    flow = qs.filter(payment_method=method).first() if method else None
    flow = flow or qs.first()
    return flow.cashbox if flow else None


def reverse_cashflow(*, deal, amount, method, key, user, shift_id=None, cashbox_id=None, title="Возврат оплаты долга"):
    supplier = _is_supplier(deal)
    cashbox = None if cashbox_id else _original_cashbox(deal, method)
    return create_auto_cashflow(
        company=deal.company,
        branch=deal.branch,
        cashbox=cashbox,
        cashbox_id=cashbox_id,
        cashbox_role=None if (cashbox or cashbox_id) else ("expense_variable" if supplier else "pos_branch"),
        user=user,
        shift_id=shift_id,
        type=CashFlow.Type.INCOME if supplier else CashFlow.Type.EXPENSE,
        amount=amount,
        source_kind=CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT if supplier else CashFlow.SourceKind.DEBT_REPAYMENT,
        source_id=str(deal.id),
        idempotency_key=key,
        name=f"{title}: {deal.title or (deal.client.full_name if deal.client else 'Сделка')}",
        source_business_operation_id="Возврат оплаты долга",
        payment_method=method or "cash",
        affects_shift_drawer=((method or "cash") == "cash" and not supplier),
    )


class ClientDealRefundWithCashflowAPIView(ClientDealRefundAPIView):
    """Возврат оплаты по сделке + обратная операция в кассе (см. docstring модуля)."""

    def post(self, request, pk, *args, **kwargs):
        with transaction.atomic():
            response = super().post(request, pk, *args, **kwargs)
            if response.status_code != status.HTTP_200_OK:
                return response
            payment = None
            idem = request.data.get("idempotency_key") if hasattr(request.data, "get") else None
            if idem:
                payment = (
                    DealPayment.objects.select_related("deal__client", "deal__company", "deal__branch")
                    .filter(deal_id=pk, idempotency_key=idem, kind=DealPayment.Kind.REFUND)
                    .first()
                )
            if payment is None:
                return response
            flow = reverse_cashflow(
                deal=payment.deal, amount=payment.amount, method=payment.payment_method,
                key=f"deal-refund:{payment.id}", user=request.user,
                shift_id=request.data.get("shift_id"), cashbox_id=request.data.get("cashbox_id"),
            )
            if flow is not None and isinstance(getattr(response, "data", None), dict):
                response.data["reverse_cashflow"] = {
                    "id": str(flow.id), "type": flow.type, "amount": str(flow.amount),
                    "cashbox": str(flow.cashbox_id), "payment_method": flow.payment_method,
                }
            return response


class ClientDealDestroyWithCashflowAPIView(ClientDealRetrieveUpdateDestroyAPIView):
    """DELETE сделки с оплатами: сначала обратные операции в кассе на чистую сумму оплат."""

    def perform_destroy(self, instance):
        with transaction.atomic():
            net = defaultdict(lambda: ZERO)
            for p in DealPayment.objects.filter(deal=instance):
                sign = 1 if p.kind == DealPayment.Kind.PAY else -1
                net[(p.payment_method or "cash")] += sign * p.amount
            for method, amount in sorted(net.items()):
                if amount > 0:
                    reverse_cashflow(
                        deal=instance, amount=amount, method=method,
                        key=f"deal-delete:{instance.id}:{method}", user=self.request.user,
                        title="Возврат оплат при удалении сделки",
                    )
            super().perform_destroy(instance)
