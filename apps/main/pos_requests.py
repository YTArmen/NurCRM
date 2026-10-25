"""
ТЗ Н-31: запросы «на кассу». Владелец просит кассира выполнить операцию (оплата долга клиента,
выплата зарплаты, оплата поставщику, оплата проката, изъятие из кассы).

  POST pos/requests/                 создать (владелец/админ)       → pending
  GET  pos/requests/?status=&cashbox=  список
  POST pos/requests/{id}/accept/     принять кассиром в ОТКРЫТОЙ смене этой кассы → деньги
  POST pos/requests/{id}/decline/    отклонить (кассир кассы или владелец)
  POST pos/requests/{id}/cancel/     отменить (автор или владелец)

Статусы: pending → accepted | declined | canceled | expired (по истечении expires_at).
Повторный accept → 200 + replayed:true, деньги второй раз не двигаются.

Деньги при accept сейчас делают: debt_payment (существующий clients/{id}/pay-debt/) и
cash_out (расход «из ящика смены»). Остальные виды — только статусы (result.executed=false).
"""
import json
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.test import APIRequestFactory

from apps.main.models import Client, PosRequest
from apps.main.pos_serializers import _is_owner_like

DEFAULT_TTL = timedelta(hours=24)
EXECUTING_KINDS = ("debt_payment", "cash_out")


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _q2(v):
    try:
        d = Decimal(str(v)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError({"amount": "Неверная сумма.", "code": "amount_invalid"})
    if d <= 0:
        raise ValidationError({"amount": "Сумма должна быть больше нуля.", "code": "amount_invalid"})
    return d


def _open_shift(user, cashbox):
    from apps.construction.models import CashShift
    return CashShift.objects.select_related("cashbox").filter(
        cashbox=cashbox, cashier=user, status=CashShift.Status.OPEN).first()


def _expire_if_needed(obj):
    if obj.status == PosRequest.Status.PENDING and obj.expires_at and obj.expires_at <= timezone.now():
        obj.status = PosRequest.Status.EXPIRED
        obj.decided_at = timezone.now()
        obj.save(update_fields=["status", "decided_at"])
        _notify(obj)
    return obj


def _notify(obj):
    from apps.integrations.events import emit_event
    emit_event(obj.company_id, "pos_request_changed", {
        "entity": "pos_request", "entity_id": str(obj.id), "request": str(obj.id),
        "status": obj.status, "kind": obj.kind, "cashbox": str(obj.cashbox_id),
        "amount": obj.amount, "branch": obj.branch_id, "actor": obj.decided_by_id or obj.created_by_id,
    })


def serialize_request(obj) -> dict:
    return {
        "id": str(obj.id),
        "kind": obj.kind,
        "status": obj.status,
        "cashbox": str(obj.cashbox_id),
        "amount": f"{obj.amount:.2f}",
        "payload": obj.payload or {},
        "comment": obj.comment,
        "created_by": str(obj.created_by_id) if obj.created_by_id else None,
        "created_at": obj.created_at.isoformat(),
        "expires_at": obj.expires_at.isoformat() if obj.expires_at else None,
        "decided_by": str(obj.decided_by_id) if obj.decided_by_id else None,
        "decided_at": obj.decided_at.isoformat() if obj.decided_at else None,
        "shift": str(obj.shift_id) if obj.shift_id else None,
        "result": obj.result or {},
    }


class PosRequestCreateSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=PosRequest.Kind.choices)
    cashbox = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=255)
    payload = serializers.DictField(required=False)
    ttl_minutes = serializers.IntegerField(required=False, min_value=1, max_value=60 * 24 * 7)


class PosRequestListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request.user)
        if company is None:
            return Response({"detail": "Нет компании."}, status=403)
        qs = PosRequest.objects.filter(company=company)
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        if request.query_params.get("cashbox"):
            qs = qs.filter(cashbox_id=request.query_params["cashbox"])
        if not _is_owner_like(request.user):
            from apps.construction.models import CashShift
            boxes = CashShift.objects.filter(cashier=request.user, status=CashShift.Status.OPEN) \
                .values_list("cashbox_id", flat=True)
            qs = qs.filter(cashbox_id__in=list(boxes))
        items = [_expire_if_needed(o) for o in qs[:200]]
        return Response({"results": [serialize_request(o) for o in items]})

    @transaction.atomic
    def post(self, request):
        from apps.construction.models import Cashbox
        company = _company(request.user)
        if company is None:
            return Response({"detail": "Нет компании."}, status=403)
        if not _is_owner_like(request.user):
            return Response({"detail": "Создавать запросы может владелец или администратор.", "code": "forbidden"},
                            status=403)
        ser = PosRequestCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        cashbox = Cashbox.objects.filter(id=d["cashbox"], company=company).first()
        if cashbox is None:
            raise ValidationError({"cashbox": "Касса не найдена.", "code": "cashbox_not_found"})
        amount = _q2(d["amount"])
        payload = d.get("payload") or {}
        if d["kind"] == PosRequest.Kind.DEBT_PAYMENT:
            cid = payload.get("client")
            if not cid or not Client.objects.filter(id=cid, company=company).exists():
                raise ValidationError({"payload": "Нужен payload.client — клиент компании.", "code": "client_required"})
        obj = PosRequest.objects.create(
            company=company, branch=cashbox.branch, kind=d["kind"], cashbox=cashbox, amount=amount,
            payload=json.loads(json.dumps(payload, default=str)), comment=d.get("comment", ""),
            created_by=request.user,
            expires_at=timezone.now() + (timedelta(minutes=d["ttl_minutes"]) if d.get("ttl_minutes") else DEFAULT_TTL),
        )
        _notify(obj)
        return Response(serialize_request(obj), status=status.HTTP_201_CREATED)


class _PosRequestActionBase(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, pk):
        company = _company(request.user)
        if company is None:
            raise Http404
        obj = PosRequest.objects.select_for_update().filter(pk=pk, company=company).select_related("cashbox").first()
        if obj is None:
            raise Http404
        return _expire_if_needed(obj)

    @staticmethod
    def _conflict(obj, code="not_pending"):
        return Response({"detail": f"Запрос уже в статусе «{obj.status}».", "code": code, "status": obj.status},
                        status=status.HTTP_409_CONFLICT)


class PosRequestAcceptView(_PosRequestActionBase):
    @transaction.atomic
    def post(self, request, pk):
        obj = self._get(request, pk)
        if obj.status == PosRequest.Status.ACCEPTED:
            return Response({**serialize_request(obj), "replayed": True})
        if obj.status != PosRequest.Status.PENDING:
            return self._conflict(obj)
        shift = _open_shift(request.user, obj.cashbox)
        if shift is None:
            return Response({"detail": "Принять можно только в открытой смене на этой кассе.",
                             "code": "shift_required"}, status=status.HTTP_409_CONFLICT)

        result = {"executed": False}
        if obj.kind == PosRequest.Kind.CASH_OUT:
            result = self._cash_out(request, obj, shift)
        elif obj.kind == PosRequest.Kind.DEBT_PAYMENT:
            result, err = self._debt_payment(request, obj, shift)
            if err is not None:
                transaction.set_rollback(True)
                return err
        obj.status = PosRequest.Status.ACCEPTED
        obj.decided_by = request.user
        obj.decided_at = timezone.now()
        obj.shift = shift
        obj.result = result
        obj.save(update_fields=["status", "decided_by", "decided_at", "shift", "result"])
        _notify(obj)
        return Response({**serialize_request(obj), "replayed": False})

    @staticmethod
    def _cash_out(request, obj, shift):
        """Расход из ящика смены: expected_cash уменьшается на сумму."""
        from apps.construction.auto_cashflow import create_auto_cashflow
        from apps.construction.models import CashFlow
        cf = create_auto_cashflow(
            company=obj.company, branch=shift.cashbox.branch, cashbox=shift.cashbox, user=request.user,
            shift=shift, type=CashFlow.Type.EXPENSE, amount=obj.amount,
            source_kind=CashFlow.SourceKind.SHIFT_DRAWER_OUTFLOW, source_id=str(obj.id),
            name=obj.comment or "Изъятие по запросу владельца",
            source_business_operation_id="Запрос на кассу",
            affects_shift_drawer=True, idempotency_key=f"posreq:{obj.id}", payment_method="cash",
        )
        return {"executed": True, "cashflow": str(cf.id) if cf else None}

    @staticmethod
    def _debt_payment(request, obj, shift):
        """Существующая логика clients/{id}/pay-debt/ — от имени принявшего кассира, в его смене."""
        from apps.main.kassa_views import ClientPayDebtAPIView

        p = obj.payload or {}
        body = {"amount": str(obj.amount), "method": p.get("method") or "cash",
                "shift": str(shift.id), "cashbox": str(obj.cashbox_id),
                "note": obj.comment or "Оплата по запросу владельца"}
        raw = APIRequestFactory().post("/", json.dumps(body), content_type="application/json",
                                       HTTP_IDEMPOTENCY_KEY=f"posreq-{obj.id}")
        inner = Request(raw, parsers=[JSONParser()])
        inner._user = request.user
        inner._request.user = request.user
        inner._pos_request_accept = True  # см. ClientDealsPayAnyAPIView.pay
        try:
            resp = ClientPayDebtAPIView().post(inner, p["client"])
        except ValidationError as exc:
            return None, Response({"detail": "Оплата долга не прошла.", "code": "execution_failed",
                                   "errors": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Http404:
            return None, Response({"detail": "Клиент не найден.", "code": "client_not_found"}, status=404)
        if resp.status_code >= 400:
            return None, Response({"detail": "Оплата долга не прошла.", "code": "execution_failed",
                                   "errors": resp.data}, status=status.HTTP_400_BAD_REQUEST)
        return {"executed": True, "paid": resp.data.get("paid"), "left": resp.data.get("left")}, None


class PosRequestDeclineView(_PosRequestActionBase):
    @transaction.atomic
    def post(self, request, pk):
        obj = self._get(request, pk)
        if obj.status == PosRequest.Status.DECLINED:
            return Response({**serialize_request(obj), "replayed": True})
        if obj.status != PosRequest.Status.PENDING:
            return self._conflict(obj)
        if not (_is_owner_like(request.user) or _open_shift(request.user, obj.cashbox)):
            return Response({"detail": "Нет доступа к этой кассе.", "code": "forbidden"}, status=403)
        obj.status = PosRequest.Status.DECLINED
        obj.decided_by = request.user
        obj.decided_at = timezone.now()
        reason = str(request.data.get("reason") or "")[:255]
        obj.result = {"reason": reason} if reason else {}
        obj.save(update_fields=["status", "decided_by", "decided_at", "result"])
        _notify(obj)
        return Response({**serialize_request(obj), "replayed": False})


class PosRequestCancelView(_PosRequestActionBase):
    @transaction.atomic
    def post(self, request, pk):
        obj = self._get(request, pk)
        if obj.status == PosRequest.Status.CANCELED:
            return Response({**serialize_request(obj), "replayed": True})
        if obj.status != PosRequest.Status.PENDING:
            return self._conflict(obj)
        if not (_is_owner_like(request.user) or obj.created_by_id == request.user.id):
            return Response({"detail": "Отменить может автор или владелец.", "code": "forbidden"}, status=403)
        obj.status = PosRequest.Status.CANCELED
        obj.decided_by = request.user
        obj.decided_at = timezone.now()
        obj.save(update_fields=["status", "decided_by", "decided_at"])
        _notify(obj)
        return Response({**serialize_request(obj), "replayed": False})
