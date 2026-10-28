"""
ТЗ Н-08: GET /api/main/idempotency/<key>/ — чем закончилась операция с этим ключом.

Касса после обрыва связи узнаёт исход, не повторяя операцию вслепую. Ищем по журналу
Idempotency-Key (BE2-11), затем по ключам, которые хранят сами документы
(продажа, возврат, движение денег, оплата сделки). Видна только своя компания.
"""
from django.core.exceptions import ValidationError
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import TTL
from apps.integrations.models import IdempotencyRecord


def _found(key, *, state, endpoint, status_code, created_at, response, created_by=None):
    return {
        "key": key,
        "found": True,
        "state": state,
        "endpoint": endpoint,
        "status_code": status_code,
        "created_at": created_at.isoformat() if created_at else None,
        "created_by": created_by,
        "response": response,
    }


def _user_ref(user):
    if user is None:
        return None
    name = (getattr(user, "get_full_name", lambda: "")() or getattr(user, "email", "") or "").strip()
    return {"id": str(user.pk), "name": name}


class IdempotencyLookupAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, key, *args, **kwargs):
        company_id = getattr(request.user, "company_id", None)
        key = (key or "").strip()
        if not company_id or not key:
            return Response({"detail": "Операции с таким ключом нет.", "code": "idempotency_key_not_found", "found": False}, status=status.HTTP_404_NOT_FOUND)

        rec = IdempotencyRecord.objects.filter(company_id=company_id, key=key).first()
        if rec is not None:
            state = rec.state
            if state == IdempotencyRecord.State.IN_PROGRESS and rec.status_code and rec.status_code >= 400:
                state = "failed"
            return Response(_found(
                key, state=state, endpoint=rec.scope, status_code=rec.status_code,
                created_at=rec.created_at, response=rec.response_body,
            ))

        from apps.main.models import DealPayment, Sale, SaleReturn
        from apps.construction.models import CashFlow

        sale = Sale.objects.filter(company_id=company_id, idempotency_key=key).select_related("user").first()
        if sale is not None:
            return Response(_found(
                key, state="done", endpoint="POST main/pos/checkout/", status_code=201,
                created_at=sale.created_at, created_by=_user_ref(sale.user),
                response={"id": str(sale.id), "number": sale.doc_number, "total": str(sale.total), "status": sale.status},
            ))
        ret = SaleReturn.objects.filter(company_id=company_id, idempotency_key=key).select_related("user").first()
        if ret is not None:
            return Response(_found(
                key, state="done", endpoint=f"POST main/pos/sales/{ret.sale_id}/return/", status_code=200,
                created_at=ret.created_at, created_by=_user_ref(ret.user),
                response=ret.response_data or {"id": str(ret.id), "amount": str(ret.returned_amount)},
            ))
        cf = CashFlow.objects.filter(company_id=company_id, idempotency_key=key).first()
        if cf is not None:
            return Response(_found(
                key, state="done", endpoint="POST construction/cashflows/", status_code=201,
                created_at=cf.created_at, created_by=_user_ref(getattr(cf, "cashier", None)),
                response={"id": str(cf.id), "type": cf.type, "amount": str(cf.amount), "status": cf.status},
            ))
        try:  # у оплат сделок ключ — UUID
            dp = DealPayment.objects.filter(company_id=company_id, idempotency_key=key).select_related("created_by").first()
        except (ValueError, ValidationError):
            dp = None
        if dp is not None:
            return Response(_found(
                key, state="done", endpoint=f"POST main/clients/…/deals/{dp.deal_id}/pay/", status_code=200,
                created_at=dp.created_at, created_by=_user_ref(dp.created_by),
                response={"id": str(dp.id), "deal": str(dp.deal_id), "amount": str(dp.amount)},
            ))

        return Response(
            {"detail": f"Операции с таким ключом нет (журнал хранится {TTL.days} дней).", "code": "idempotency_key_not_found", "found": False},
            status=status.HTTP_404_NOT_FOUND,
        )
