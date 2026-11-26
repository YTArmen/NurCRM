"""ТЗ Н-29: заказ поставщику как документ — api/main/purchase-orders/."""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Client, Product, PurchaseOrder, PurchaseOrderLine
from apps.main.views import CompanyBranchRestrictedMixin


class PurchaseOrderPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "limit"
    max_page_size = 100


class PurchaseOrderLineSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    product_name = serializers.CharField(source="product.name", read_only=True)
    qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))
    purchase_price = serializers.DecimalField(max_digits=11, decimal_places=3, required=False, allow_null=True, min_value=Decimal("0"))
    received_qty = serializers.DecimalField(max_digits=12, decimal_places=3, required=False, min_value=Decimal("0"))
    # ТЗ 26.10 Б-53 п.5: единица закупки (qty и purchase_price — в ней; хранится в базовых)
    sale_unit_id = serializers.IntegerField(required=False, allow_null=True, write_only=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["product"] = str(instance.product_id)
        return data


class PurchaseOrderSerializer(serializers.ModelSerializer):
    supplier = serializers.PrimaryKeyRelatedField(queryset=Client.objects.all())
    lines = PurchaseOrderLineSerializer(many=True, required=False)
    created_by = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    supplier_name = serializers.CharField(source="supplier.full_name", read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = ["id", "number", "supplier", "supplier_name", "status", "lines", "note",
                  "created_by", "created_by_name", "sent_at", "created_at", "updated_at"]
        read_only_fields = ["id", "number", "sent_at", "created_at", "updated_at"]

    def get_created_by(self, obj):
        return str(obj.created_by_id) if obj.created_by_id else None

    def get_created_by_name(self, obj):
        u = obj.created_by
        return (u.get_full_name() if u else None) or None


class PurchaseOrderBase(CompanyBranchRestrictedMixin):
    permission_classes = [permissions.IsAuthenticated]

    def _orders(self):
        return self._filter_qs_company_branch(
            PurchaseOrder.objects.select_related("supplier", "created_by").prefetch_related("lines__product")
        )

    def _check_refs(self, data):
        """Поставщик и товары — только своей компании/филиала."""
        sup = data.get("supplier")
        if sup is not None:
            ok = self._filter_qs_company_branch(Client.objects.all()).filter(
                id=sup.id, type=Client.StatusClient.SUPPLIERS).exists()
            if not ok:
                raise ValidationError({"code": "supplier_not_found", "detail": "Поставщик не найден.", "fields": {"supplier": ["Поставщик не найден."]}})
        pids = [ln["product"].id for ln in data.get("lines") or []]
        if pids:
            found = set(self._filter_qs_company_branch(Product.objects.all()).filter(id__in=pids).values_list("id", flat=True))
            missing = [str(p) for p in pids if p not in found]
            if missing:
                raise ValidationError({"code": "product_not_found", "detail": "Товары не найдены: " + ", ".join(missing),
                                       "fields": {"lines": ["Товары не найдены."]}})

    @staticmethod
    def _replace_lines(order, lines):
        from apps.main.tz26.hw_units.receipts import convert_order_lines

        lines = convert_order_lines(lines)  # ТЗ 26.10 Б-53 п.5
        old = {}
        for ln in order.lines.all():
            old[ln.product_id] = old.get(ln.product_id, Decimal("0")) + ln.received_qty
        order.lines.all().delete()
        PurchaseOrderLine.objects.bulk_create([
            PurchaseOrderLine(
                order=order, product=ln["product"], qty=ln["qty"], purchase_price=ln.get("purchase_price"),
                received_qty=ln["received_qty"] if "received_qty" in ln else old.get(ln["product"].id, Decimal("0")),
            ) for ln in lines
        ])

    @staticmethod
    def _apply_status(order, new_status):
        if new_status and new_status != order.status:
            order.status = new_status
        if order.status in (PurchaseOrder.Status.SENT, PurchaseOrder.Status.PARTIAL, PurchaseOrder.Status.RECEIVED) and not order.sent_at:
            order.sent_at = timezone.now()


class PurchaseOrderListCreateAPIView(PurchaseOrderBase, APIView):
    """
    GET  /api/main/purchase-orders/?supplier=&status=&date_from=&date_to=&search=&page=&limit=
    POST /api/main/purchase-orders/  {supplier, status?, lines:[{product, qty, purchase_price}], note}
    Номер (number) выдаёт сервер: 1, 2, 3… внутри компании.
    """

    def get(self, request):
        qs = self._orders()
        qp = request.query_params
        if qp.get("supplier"):
            qs = qs.filter(supplier_id=qp["supplier"])
        st = (qp.get("status") or "").strip().lower()
        if st:
            if st not in PurchaseOrder.Status.values:
                raise ValidationError({"code": "invalid_param", "detail": "status: draft|sent|partial|received|cancelled.", "field": "status"})
            qs = qs.filter(status=st)
        for key, lookup in (("date_from", "created_at__date__gte"), ("date_to", "created_at__date__lte")):
            raw = (qp.get(key) or "").strip()
            if raw:
                d = parse_date(raw)
                if d is None:
                    raise ValidationError({"code": "invalid_param", "detail": f"{key}: YYYY-MM-DD.", "field": key})
                qs = qs.filter(**{lookup: d})
        search = (qp.get("search") or "").strip()
        if search:
            q = (Q(note__icontains=search) | Q(supplier__full_name__icontains=search)
                 | Q(supplier__llc__icontains=search) | Q(lines__product__name__icontains=search))
            if search.lstrip("#").isdigit():
                q |= Q(number=int(search.lstrip("#")))
            qs = qs.filter(q).distinct()
        qs = qs.order_by("-created_at", "-number")
        pg = PurchaseOrderPagination()
        page = pg.paginate_queryset(qs, request, view=self)
        return pg.get_paginated_response(PurchaseOrderSerializer(page, many=True).data)

    def post(self, request):
        ser = PurchaseOrderSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        self._check_refs(d)
        company, branch = self._company(), self._auto_branch()
        for _ in range(5):
            try:
                with transaction.atomic():
                    nxt = (PurchaseOrder.objects.filter(company=company).aggregate(m=Max("number"))["m"] or 0) + 1
                    order = PurchaseOrder(company=company, branch=branch, number=nxt, supplier=d["supplier"],
                                          note=d.get("note") or "", created_by=request.user)
                    self._apply_status(order, d.get("status") or PurchaseOrder.Status.DRAFT)
                    order.save()
                    self._replace_lines(order, d.get("lines") or [])
                break
            except IntegrityError:
                continue  # гонка по номеру — берём следующий
        else:
            raise ValidationError({"code": "number_conflict", "detail": "Не удалось выдать номер заказа, повторите."})
        order = self._orders().get(pk=order.pk)
        return Response(PurchaseOrderSerializer(order).data, status=status.HTTP_201_CREATED)


class PurchaseOrderDetailAPIView(PurchaseOrderBase, APIView):
    """GET/PATCH/PUT/DELETE /api/main/purchase-orders/<uuid>/ . Удалить можно черновик или отменённый."""

    def _get(self, pk):
        return get_object_or_404(self._orders(), pk=pk)

    def get(self, request, pk):
        return Response(PurchaseOrderSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        with transaction.atomic():
            order = get_object_or_404(self._orders().select_for_update(of=("self",)), pk=pk)
            ser = PurchaseOrderSerializer(order, data=request.data, partial=True)
            ser.is_valid(raise_exception=True)
            d = ser.validated_data
            if order.status in (PurchaseOrder.Status.CANCELLED, PurchaseOrder.Status.RECEIVED) and (
                "lines" in d or "supplier" in d
            ):
                raise ValidationError({"code": "order_closed", "detail": "Заказ закрыт: строки и поставщика менять нельзя."})
            self._check_refs(d)
            if "supplier" in d:
                order.supplier = d["supplier"]
            if "note" in d:
                order.note = d["note"] or ""
            self._apply_status(order, d.get("status"))
            order.save()
            if "lines" in d:
                self._replace_lines(order, d["lines"])
        return Response(PurchaseOrderSerializer(self._get(pk)).data)

    put = patch

    def delete(self, request, pk):
        order = self._get(pk)
        if order.status not in (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.CANCELLED):
            raise ValidationError({"code": "order_not_deletable", "detail": "Удалить можно только черновик или отменённый заказ."})
        order.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def apply_receipt_to_order(order, receipt_items):
    """
    ТЗ Н-29: прибавить принятое количество к строкам заказа и выставить статус.
    receipt_items — [(product_id, qty)]. Излишек сверх заказа записывается в последнюю строку товара.
    """
    lines = list(order.lines.select_for_update().order_by("id"))
    for pid, qty in receipt_items:
        mine = [ln for ln in lines if ln.product_id == pid]
        left = Decimal(str(qty))
        for i, ln in enumerate(mine):
            if left <= 0:
                break
            room = max(Decimal("0"), ln.qty - ln.received_qty)
            take = left if i == len(mine) - 1 else min(room, left)
            ln.received_qty += take
            left -= take
    for ln in lines:
        ln.save(update_fields=["received_qty"])
    if lines and all(ln.received_qty >= ln.qty for ln in lines):
        order.status = PurchaseOrder.Status.RECEIVED
    elif any(ln.received_qty > 0 for ln in lines):
        order.status = PurchaseOrder.Status.PARTIAL
    order.save(update_fields=["status", "updated_at"])
    # ТЗ 26.10 Б-60: приход по заказу поставщику → строки заказов покупателей «готовы»
    from apps.main.tz26.cust_orders.services import on_supplier_order_received
    on_supplier_order_received(order)
