"""
ТЗ Н-22: клиентские документы — КП, счёт, накладная.

    GET/POST   /api/main/client-documents/        фильтры: client, type, status, search; пагинация count/results
    GET/PATCH/DELETE /api/main/client-documents/{id}/   (DELETE — мягкое, ?include_deleted=1 в списке)
    POST       /api/main/client-documents/{id}/pay/  {amount, payment_method?, shift?}  + Idempotency-Key

Номер выдаёт сервер (сквозной по компании и типу). Оплата — приход в кассу (наличные — в ящик смены)
и рост paid_amount; при полной оплате статус становится paid.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.kassa_views import _company
from apps.main.models import Client, ClientDocument, ClientDocumentPayment
from apps.main.pos_serializers import MoneyField, QtyField
from apps.main.pos_utils import money

ZERO = Decimal("0.00")


class _Pagination(LimitOffsetPagination):
    default_limit = 100
    max_limit = 500


class DocLineSerializer(serializers.Serializer):
    product = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255, allow_blank=True, required=False, default="")
    qty = QtyField(required=False, default=Decimal("1"))
    price = MoneyField(required=False, default=ZERO)
    discount = MoneyField(required=False, default=ZERO)

    def validate(self, attrs):
        attrs["qty"] = attrs.get("qty") if attrs.get("qty") is not None else Decimal("1")
        attrs["price"] = attrs.get("price") if attrs.get("price") is not None else ZERO
        attrs["discount"] = attrs.get("discount") if attrs.get("discount") is not None else ZERO
        if attrs["qty"] <= 0:
            raise serializers.ValidationError({"qty": "Количество должно быть больше нуля."})
        if attrs["price"] < 0 or attrs["discount"] < 0:
            raise serializers.ValidationError("Цена и скидка не могут быть отрицательными.")
        if not attrs.get("name") and not attrs.get("product"):
            raise serializers.ValidationError({"name": "Укажите name или product."})
        return attrs


class ClientDocumentSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=ClientDocument.Type.choices)
    client = serializers.UUIDField(required=False, allow_null=True)
    lines = DocLineSerializer(many=True, required=False)
    discount = MoneyField(required=False, default=ZERO)
    valid_until = serializers.DateField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    status = serializers.ChoiceField(choices=ClientDocument.Status.choices, required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate_discount(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Скидка не может быть отрицательной.")
        return value


def _lines_total(doc) -> Decimal:
    total = ZERO
    for ln in doc.lines or []:
        total += Decimal(str(ln.get("qty", 1))) * Decimal(str(ln.get("price", 0))) - Decimal(str(ln.get("discount", 0)))
    return money(max(ZERO, total - (doc.discount or ZERO)))


def _payload(d: ClientDocument):
    total = _lines_total(d)
    return {
        "id": str(d.id),
        "type": d.type,
        "number": d.number,
        "client": str(d.client_id) if d.client_id else None,
        "client_name": d.client.full_name if d.client_id else None,
        "lines": d.lines or [],
        "discount": str(d.discount),
        "total": str(total),
        "valid_until": d.valid_until.isoformat() if d.valid_until else None,
        "due_date": d.due_date.isoformat() if d.due_date else None,
        "status": d.status,
        "paid_amount": str(d.paid_amount),
        "note": d.note,
        "payments": [
            {"id": str(p.id), "amount": str(p.amount), "payment_method": p.payment_method,
             "shift": str(p.shift_id) if p.shift_id else None, "at": p.created_at.isoformat()}
            for p in d.payments.all()
        ],
        "is_deleted": d.is_deleted,
        "created_at": d.created_at.isoformat(),
        "updated_at": d.updated_at.isoformat(),
    }


def _lines_json(lines):
    return [
        {
            "product": str(ln["product"]) if ln.get("product") else None,
            "name": ln.get("name") or "",
            "qty": str(ln["qty"]),
            "price": str(money(ln["price"])),
            "discount": str(money(ln["discount"])),
        }
        for ln in lines
    ]


def _get_client(company, raw):
    if not raw:
        return None
    client = Client.objects.filter(pk=raw, company=company).first()
    if client is None:
        raise ValidationError({"client": "Клиент не найден."})
    return client


class ClientDocumentListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request)
        qp = request.query_params
        qs = ClientDocument.objects.filter(company=company).select_related("client").prefetch_related("payments")
        if qp.get("include_deleted") not in ("1", "true"):
            qs = qs.filter(is_deleted=False)
        if qp.get("client"):
            qs = qs.filter(client_id=qp["client"])
        if qp.get("type"):
            qs = qs.filter(type=qp["type"])
        if qp.get("status"):
            qs = qs.filter(status=qp["status"])
        search = (qp.get("search") or "").strip()
        if search:
            cond = Q(client__full_name__icontains=search) | Q(note__icontains=search)
            if search.isdigit():
                cond |= Q(number=int(search))
            qs = qs.filter(cond)
        allowed = {"created_at", "updated_at", "number", "due_date"}
        fields = [f.strip() for f in (qp.get("ordering") or "").split(",") if f.strip().lstrip("-") in allowed]
        qs = qs.order_by(*fields, "-created_at") if fields else qs.order_by("-created_at")
        paginator = _Pagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response([_payload(d) for d in page])

    def post(self, request):
        company = _company(request)
        ser = ClientDocumentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        client = _get_client(company, data.get("client"))
        if data.get("status") == ClientDocument.Status.PAID:
            raise ValidationError({"status": "Статус «paid» ставится оплатой (pay)."})
        for attempt in range(3):
            try:
                with transaction.atomic():
                    last = (
                        ClientDocument.objects.select_for_update()
                        .filter(company=company, type=data["type"])
                        .aggregate(m=Max("number"))["m"]
                    )
                    doc = ClientDocument.objects.create(
                        company=company,
                        type=data["type"],
                        number=(last or 0) + 1,
                        client=client,
                        lines=_lines_json(data.get("lines") or []),
                        discount=money(data.get("discount") or ZERO),
                        valid_until=data.get("valid_until"),
                        due_date=data.get("due_date"),
                        status=data.get("status") or ClientDocument.Status.DRAFT,
                        note=data.get("note") or "",
                        created_by=request.user,
                    )
                break
            except IntegrityError:
                if attempt == 2:
                    raise
        return Response(_payload(doc), status=status.HTTP_201_CREATED)


class ClientDocumentDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, pk, lock=False):
        qs = ClientDocument.objects.filter(is_deleted=False).select_related("client").prefetch_related("payments")
        if lock:
            qs = ClientDocument.objects.select_for_update().filter(is_deleted=False)
        return get_object_or_404(qs, pk=pk, company=_company(request))

    def get(self, request, pk):
        return Response(_payload(self._get(request, pk)))

    def patch(self, request, pk):
        company = _company(request)
        ser = ClientDocumentSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if "type" in data:
            raise ValidationError({"type": "Тип документа менять нельзя."})
        if request.data.get("paid_amount") is not None or request.data.get("number") is not None:
            raise ValidationError({"detail": "number и paid_amount меняются только сервером."})
        if data.get("status") == ClientDocument.Status.PAID:
            raise ValidationError({"status": "Статус «paid» ставится оплатой (pay)."})
        with transaction.atomic():
            doc = self._get(request, pk, lock=True)
            if "client" in data:
                doc.client = _get_client(company, data["client"])
            if "lines" in data:
                doc.lines = _lines_json(data["lines"])
            for f in ("discount", "valid_until", "due_date", "status", "note"):
                if f in data:
                    setattr(doc, f, money(data[f] or ZERO) if f == "discount" else data[f])
            doc.save()
        return Response(_payload(self._get(request, pk)))

    def delete(self, request, pk):
        with transaction.atomic():
            doc = self._get(request, pk, lock=True)
            doc.is_deleted = True
            doc.deleted_at = timezone.now()
            doc.save(update_fields=["is_deleted", "deleted_at", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClientDocumentPaySerializer(serializers.Serializer):
    amount = MoneyField()
    payment_method = serializers.ChoiceField(
        choices=["cash", "transfer", "card"], required=False, default="cash"
    )


class ClientDocumentPayAPIView(APIView):
    """POST /api/main/client-documents/{id}/pay/ {amount, payment_method?, shift?}, Idempotency-Key."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from apps.construction.auto_cashflow import create_auto_cashflow
        from apps.construction.models import CashFlow
        from apps.main.pos_views import _find_open_shift_for_cashier
        from apps.main.views import _receipt_drawer_shift

        company = _company(request)
        ser = ClientDocumentPaySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        amount = money(ser.validated_data["amount"])
        method = ser.validated_data["payment_method"]
        if amount <= 0:
            raise ValidationError({"amount": "Сумма должна быть больше нуля."})
        idem = (request.headers.get("Idempotency-Key") or "").strip()[:128] or None

        with transaction.atomic():
            doc = get_object_or_404(
                ClientDocument.objects.select_for_update(), pk=pk, company=company, is_deleted=False
            )
            if idem and ClientDocumentPayment.objects.filter(document=doc, idempotency_key=idem).exists():
                doc = ClientDocument.objects.select_related("client").prefetch_related("payments").get(pk=doc.pk)
                return Response({**_payload(doc), "replayed": True})
            if doc.type != ClientDocument.Type.INVOICE:
                raise ValidationError({"code": "not_invoice", "detail": "Оплата принимается только по счёту."})
            if doc.status == ClientDocument.Status.CANCELED:
                raise ValidationError({"code": "document_canceled", "detail": "Документ отменён."})
            total = _lines_total(doc)
            if doc.paid_amount + amount > total:
                raise ValidationError(
                    {"code": "overpayment", "detail": f"Остаток к оплате {money(total - doc.paid_amount)}."}
                )
            shift = _receipt_drawer_shift(request, company)
            if shift is None:
                shift = _find_open_shift_for_cashier(company=company, cashier=request.user)
            pay = ClientDocumentPayment.objects.create(
                document=doc, amount=amount, payment_method=method, shift=shift,
                idempotency_key=idem, created_by=request.user,
            )
            create_auto_cashflow(
                company=company,
                branch=getattr(shift, "branch", None),
                cashbox=getattr(shift, "cashbox", None),
                user=request.user,
                shift=shift,
                type=CashFlow.Type.INCOME,
                amount=amount,
                source_kind=CashFlow.SourceKind.MANUAL,
                source_id=str(pay.id),
                payment_method=method,
                affects_shift_drawer=(method == "cash" and shift is not None),
                name=f"Оплата счёта №{doc.number}",
                source_business_operation_id="client_document_pay",
            )
            doc.paid_amount = money(doc.paid_amount + amount)
            if total > 0 and doc.paid_amount >= total:
                doc.status = ClientDocument.Status.PAID
            doc.save(update_fields=["paid_amount", "status", "updated_at"])
        doc = ClientDocument.objects.select_related("client").prefetch_related("payments").get(pk=doc.pk)
        return Response(_payload(doc))
