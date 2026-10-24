"""
Касса NurMarket, этап 3 (BACKEND_API.md, разделы 5, 7, 8):

    GET/POST     /api/main/products/{id}/variants/            размеры и цвета
    PATCH/DELETE /api/main/products/{id}/variants/{vid}/
    GET          /api/main/products/{id}/stocks/              остаток: зал магазина + склады
    GET/POST     /api/main/stock-transfers/                   перемещение между складами
    POST         /api/main/pos/sales/{id}/exchange/           обмен
    GET/POST     /api/main/appointments/                      запись клиентов
    PATCH/DELETE /api/main/appointments/{id}/
    POST         /api/main/appointments/{id}/to-sale/
    GET/POST     /api/main/work-orders/                            заказ-наряды
    GET/PATCH    /api/main/work-orders/{id}/
    POST         /api/main/work-orders/{id}/issue/
"""
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F, OuterRef, Q, Subquery, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.kassa_views import (
    PosQuickCheckoutAPIView,
    QuickCheckoutItemSerializer,
    _company,
    _idempotency_key,
    resolve_performers,
)
from apps.main.models import (
    Cart,
    CartItem,
    Client,
    MarketAppointment,
    Product,
    ProductStock,
    ProductVariant,
    Sale,
    SaleExchange,
    SaleReturn,
    MarketStockTransfer,
    MarketStockTransferItem,
    Warehouse,
    WorkOrder,
)
from apps.main.pos_serializers import MoneyField, QtyField, _is_owner_like
from apps.main.pos_utils import money, qty3

ZERO = Decimal("0.00")


def _open_shift(company, user):
    from apps.main.pos_views import _find_open_shift_for_cashier

    shift = _find_open_shift_for_cashier(company=company, cashier=user)
    if shift is None:
        raise ValidationError({"detail": "Смена не открыта. Сначала откройте смену на кассе."})
    return shift


def _new_cart(company, user, shift):
    return Cart.objects.create(
        company=company,
        user=user,
        status=Cart.Status.ACTIVE,
        branch=shift.branch,
        shift=shift,
        is_default=False,
    )


def _checkout_payload(payment, total, prepaid=ZERO):
    """
    Данные для SaleCheckoutAPIView.checkout: `prepaid` идёт строкой «зачёт»,
    остаток — способом из payment.
    """
    method = (payment or {}).get("method") or Sale.PaymentMethod.CASH
    if method in (Sale.PaymentMethod.MIXED, Sale.PaymentMethod.DEBT, Sale.PaymentMethod.OFFSET):
        raise ValidationError({"payment": "Для этой операции доступны: наличные или один безналичный способ."})
    rest = money(total - prepaid)
    lines = []
    if prepaid > 0:
        lines.append({"method": Sale.PaymentMethod.OFFSET, "amount": str(money(prepaid))})
    if rest > 0:
        lines.append({"method": method, "amount": str(rest)})
    if not lines:
        lines.append({"method": Sale.PaymentMethod.OFFSET, "amount": "0.00"})
    data = {"payments": lines}
    if method == Sale.PaymentMethod.CASH and rest > 0:
        received = (payment or {}).get("received")
        data["cash_received"] = str(received if received is not None else rest)
    return data


# ======================================================================
# Размеры и цвета
# ======================================================================

class ProductVariantSerializer(serializers.ModelSerializer):
    # ТЗ Н-27: сколько штук варианта сейчас в резерве заказов витрины (уже списаны с quantity)
    reserved = serializers.SerializerMethodField()

    def get_reserved(self, obj):
        val = getattr(obj, "reserved_qty", None)
        if val is None:
            from apps.main.models import ShowcaseOrderItem

            val = ShowcaseOrderItem.objects.filter(variant_id=obj.pk, order__stock_reserved=True).aggregate(s=Sum("qty"))["s"]
        return str(Decimal(str(val or 0)).quantize(Decimal("0.001")))

    class Meta:
        model = ProductVariant
        fields = ("id", "product", "size", "color", "color_hex", "sort_order", "barcode", "quantity", "reserved", "price", "is_active", "created_at", "updated_at")
        read_only_fields = ("id", "product", "reserved", "created_at", "updated_at")

    def validate_barcode(self, value):
        value = (value or "").strip() or None
        if value is None:
            return None
        company_id = self.context["product"].company_id
        clash_variant = ProductVariant.objects.filter(company_id=company_id, barcode=value)
        if self.instance is not None:
            clash_variant = clash_variant.exclude(pk=self.instance.pk)
        if clash_variant.exists() or Product.objects.filter(company_id=company_id, barcode=value).exists():
            raise serializers.ValidationError("Штрихкод уже используется в компании.")
        return value

    def validate_quantity(self, value):
        if value < 0:
            raise serializers.ValidationError("Остаток не может быть отрицательным.")
        return value

    def validate_color_hex(self, value):
        # ТЗ Н-26: необязательный #RRGGBB
        value = (value or "").strip()
        if value and not re.match(r"^#[0-9a-fA-F]{6}$", value):
            raise serializers.ValidationError("Цвет должен быть в формате #RRGGBB.")
        return value.upper()


def sync_product_quantity_from_variants(product_id):
    """Если у товара есть варианты — его остаток равен сумме их остатков."""
    # ТЗ Н-27: считаем только активные варианты
    from apps.main.variant_utils import recalc_product_quantity

    recalc_product_quantity(product_id)


def _company_product(request, pk):
    return get_object_or_404(Product, pk=pk, company=_company(request))


class ProductVariantListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        product = _company_product(request, pk)
        from apps.main.models import ShowcaseOrderItem

        reserved = (
            ShowcaseOrderItem.objects.filter(variant_id=OuterRef("pk"), order__stock_reserved=True)
            .order_by().values("variant_id").annotate(s=Sum("qty")).values("s")
        )
        rows = ProductVariant.objects.filter(product=product).annotate(reserved_qty=Subquery(reserved))
        return Response(ProductVariantSerializer(rows, many=True).data)

    def post(self, request, pk):
        product = _company_product(request, pk)
        ser = ProductVariantSerializer(data=request.data, context={"product": product})
        ser.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                v = ser.save(product=product, company_id=product.company_id)
                sync_product_quantity_from_variants(product.id)
        except IntegrityError:
            raise ValidationError({"detail": "Такой размер и цвет у товара уже есть."})
        return Response(ProductVariantSerializer(v).data, status=status.HTTP_201_CREATED)


class ProductVariantDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, pk, vid):
        product = _company_product(request, pk)
        return product, get_object_or_404(ProductVariant, pk=vid, product=product)

    def patch(self, request, pk, vid):
        product, v = self._get(request, pk, vid)
        ser = ProductVariantSerializer(v, data=request.data, partial=True, context={"product": product})
        ser.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                ser.save()
                sync_product_quantity_from_variants(product.id)
        except IntegrityError:
            raise ValidationError({"detail": "Такой размер и цвет у товара уже есть."})
        return Response(ProductVariantSerializer(v).data)

    def delete(self, request, pk, vid):
        product, v = self._get(request, pk, vid)
        with transaction.atomic():
            if v.sale_items.exists() or v.cart_items.exists():
                # по варианту были продажи — скрываем, историю не трогаем
                v.is_active = False
                v.quantity = Decimal("0")
                v.save(update_fields=["is_active", "quantity", "updated_at"])
            else:
                v.delete()
            sync_product_quantity_from_variants(product.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ======================================================================
# Остатки по складам и перемещение
# ======================================================================

class ProductStocksAPIView(APIView):
    """GET /api/main/products/{id}/stocks/ — зал магазина (Product.quantity) + склады."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        product = _company_product(request, pk)
        stocks = ProductStock.objects.filter(product=product).select_related("warehouse", "variant")
        warehouses = [
            {
                "warehouse": str(s.warehouse_id),
                "name": s.warehouse.name,
                "variant": str(s.variant_id) if s.variant_id else None,
                "quantity": str(qty3(s.quantity)),
            }
            for s in stocks
        ]
        shop = qty3(product.quantity or 0)
        in_warehouses = sum((qty3(s.quantity) for s in stocks), Decimal("0"))
        return Response({
            "product": str(product.id),
            "shop": str(shop),
            "warehouses": warehouses,
            "total": str(qty3(shop + in_warehouses)),
            "variants": [
                {"variant": str(v.id), "size": v.size, "color": v.color, "shop": str(qty3(v.quantity))}
                for v in product.variants.filter(is_active=True)
            ],
        })


class TransferItemSerializer(serializers.Serializer):
    product = serializers.UUIDField()
    variant = serializers.UUIDField(required=False, allow_null=True)
    qty = QtyField()


class StockTransferSerializer(serializers.Serializer):
    # null = торговый зал магазина
    from_warehouse = serializers.UUIDField(required=False, allow_null=True, source="from_id")
    to_warehouse = serializers.UUIDField(required=False, allow_null=True, source="to_id")
    items = TransferItemSerializer(many=True, allow_empty=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def to_internal_value(self, data):
        # принимаем и короткие ключи из BACKEND_API.md: {from, to, items}
        if hasattr(data, "copy"):
            data = data.copy()
        for short, full in (("from", "from_warehouse"), ("to", "to_warehouse")):
            if short in data and full not in data:
                data[full] = data[short]
        return super().to_internal_value(data)


def _transfer_payload(t):
    return {
        "id": str(t.id),
        "from_warehouse": str(t.from_warehouse_id) if t.from_warehouse_id else None,
        "to_warehouse": str(t.to_warehouse_id) if t.to_warehouse_id else None,
        "note": t.note,
        "items": [
            {"product": str(i.product_id), "variant": str(i.variant_id) if i.variant_id else None, "qty": str(qty3(i.quantity))}
            for i in t.items.all()
        ],
        "created_by": str(t.created_by_id) if t.created_by_id else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _move_stock(company, warehouse, product, variant, delta):
    """Меняет остаток места хранения на delta. warehouse=None — зал магазина."""
    if warehouse is None:
        target = variant if variant is not None else product
        current = type(target).objects.select_for_update().filter(pk=target.pk).values_list("quantity", flat=True).first()
        if (Decimal(str(current or 0)) + delta) < 0:
            raise ValidationError({"items": f"Недостаточно остатка «{product.name}» в магазине."})
        type(target).objects.filter(pk=target.pk).update(quantity=F("quantity") + delta)
        if variant is not None:
            Product.objects.filter(pk=product.pk).update(quantity=F("quantity") + delta)
        if company:
            try:
                from django.core.cache import cache
                cache.delete(f"tg_catalog_data:{getattr(company, 'id', company)}")
            except Exception:
                pass
        return
    row, _ = ProductStock.objects.select_for_update().get_or_create(
        company=company, warehouse=warehouse, product=product, variant=variant,
        defaults={"quantity": Decimal("0")},
    )
    if row.quantity + delta < 0:
        raise ValidationError({"items": f"Недостаточно остатка «{product.name}» на складе «{warehouse.name}»."})
    row.quantity = row.quantity + delta
    row.save(update_fields=["quantity", "updated_at"])


class StockTransferListCreateAPIView(APIView):
    """
    GET  /api/main/stock-transfers/
    POST /api/main/stock-transfers/ {"from": <склад|null>, "to": <склад|null>, "items": [{product, variant?, qty}]}
    null — торговый зал магазина (остаток, который продаёт касса).
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request)
        rows = MarketStockTransfer.objects.filter(company=company).prefetch_related("items")[:200]
        return Response([_transfer_payload(t) for t in rows])

    def post(self, request):
        company = _company(request)
        key = _idempotency_key(request, required=False)
        if key:
            prior = MarketStockTransfer.objects.filter(company=company, idempotency_key=key).first()
            if prior:
                return Response(_transfer_payload(prior))
        ser = StockTransferSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if data.get("from_id") == data.get("to_id"):
            raise ValidationError({"to_warehouse": "Склад отправления и получения совпадают."})

        def _wh(wid):
            return get_object_or_404(Warehouse, pk=wid, company=company) if wid else None

        src, dst = _wh(data.get("from_id")), _wh(data.get("to_id"))
        try:
            with transaction.atomic():
                t = MarketStockTransfer.objects.create(
                    company=company, from_warehouse=src, to_warehouse=dst,
                    note=data.get("note") or "", idempotency_key=key, created_by=request.user,
                )
                for idx, it in enumerate(data["items"]):
                    qty = qty3(it["qty"])
                    if qty <= 0:
                        raise ValidationError({"items": {idx: "Количество должно быть больше нуля."}})
                    product = Product.objects.filter(pk=it["product"], company=company).first()
                    if product is None or product.kind == Product.Kind.SERVICE:
                        raise ValidationError({"items": {idx: "Товар не найден."}})
                    variant = None
                    if it.get("variant"):
                        variant = ProductVariant.objects.filter(pk=it["variant"], product=product).first()
                        if variant is None:
                            raise ValidationError({"items": {idx: "Вариант не найден."}})
                    _move_stock(company, src, product, variant, -qty)
                    _move_stock(company, dst, product, variant, qty)
                    MarketStockTransferItem.objects.create(transfer=t, product=product, variant=variant, quantity=qty)
        except IntegrityError:
            prior = MarketStockTransfer.objects.filter(company=company, idempotency_key=key).first()
            if prior:
                return Response(_transfer_payload(prior))
            raise
        return Response(_transfer_payload(t), status=status.HTTP_201_CREATED)


# ======================================================================
# Обмен
# ======================================================================

class ExchangeReturnItemSerializer(serializers.Serializer):
    item = serializers.UUIDField()
    qty = QtyField()


class ExchangeSerializer(serializers.Serializer):
    return_items = ExchangeReturnItemSerializer(many=True, allow_empty=False)
    new_items = QuickCheckoutItemSerializer(many=True, allow_empty=False)
    payment = serializers.DictField(required=False)


class SaleExchangeAPIView(APIView):
    """
    POST /api/main/pos/sales/{id}/exchange/
    {"return_items": [{"item", "qty"}], "new_items": [{"product", "variant", "qty"}], "payment": {"method": "cash"}}
    → {"exchange", "difference"}  (> 0 доплата, < 0 сдача)

    Возвращённая сумма засчитывается в новый чек («зачёт»), деньгами проходит только разница.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from apps.construction.auto_cashflow import create_auto_cashflow
        from apps.construction.models import CashFlow
        from apps.main.pos_views import SaleCheckoutAPIView, _execute_sale_return

        company = _company(request)
        key = _idempotency_key(request, required=False)
        if key:
            prior = SaleExchange.objects.filter(company=company, idempotency_key=key).first()
            if prior:
                return Response(_exchange_payload(prior))
        ser = ExchangeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        with transaction.atomic():
            sale = get_object_or_404(Sale.objects.select_for_update(), pk=pk, company=company)
            if sale.status not in (Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED):
                raise ValidationError({"detail": "Обмен возможен только по оплаченному чеку."})
            shift = _open_shift(company, request.user)

            # 1) возврат: деньги не выдаём, сумма идёт в зачёт
            ret_key = f"exchange:{key or uuid.uuid4()}"
            partial = [(r["item"], qty3(r["qty"])) for r in data["return_items"]]
            _execute_sale_return(
                sale,
                partial,
                user=request.user,
                payload={"idempotency_key": ret_key, "refund_method": "offset", "reason": "Обмен"},
            )
            sale_return = SaleReturn.objects.get(company=company, idempotency_key=ret_key)
            returned = money(sale_return.returned_amount or ZERO)

            # 2) новый чек
            cart = _new_cart(company, request.user, shift)
            PosQuickCheckoutAPIView()._add_items(cart, data["new_items"], False, None)
            cart.recalc()
            new_total = money(cart.total or ZERO)
            credit = min(returned, new_total)
            resp = SaleCheckoutAPIView().checkout(
                request, cart.id, _checkout_payload(data.get("payment"), new_total, credit), allow_offset=True
            )
            if resp.status_code >= 400:
                transaction.set_rollback(True)
                return resp
            new_sale = Sale.objects.get(pk=resp.data["sale_id"])

            # 3) если новый товар дешевле — отдаём сдачу наличными из ящика
            change = money(returned - new_total)
            if change > 0:
                create_auto_cashflow(
                    company=company,
                    branch=shift.branch,
                    cashbox=shift.cashbox,
                    user=request.user,
                    shift=shift,
                    type=CashFlow.Type.EXPENSE,
                    amount=change,
                    source_kind=CashFlow.SourceKind.POS_SALE_RETURN,
                    source_id=str(sale.id),
                    idempotency_key=f"{ret_key}:change",
                    affects_shift_drawer=True,
                    payment_method="cash",
                    name=f"Сдача по обмену, чек №{sale.doc_number or sale.id}",
                    source_business_operation_id="pos_sale_return",
                )

            ex = SaleExchange.objects.create(
                company=company,
                original_sale=sale,
                new_sale=new_sale,
                sale_return=sale_return,
                returned_amount=returned,
                new_amount=new_total,
                difference=money(new_total - returned),
                idempotency_key=key,
                created_by=request.user,
            )
        from apps.main.cache_utils import invalidate_cache_pattern

        invalidate_cache_pattern(f"analytics:market:{company.id}:")
        invalidate_cache_pattern(f"products:list:{company.id}:")
        return Response(_exchange_payload(ex), status=status.HTTP_201_CREATED)


def _exchange_payload(ex):
    return {
        "exchange": str(ex.id),
        "original_sale": str(ex.original_sale_id),
        "new_sale": str(ex.new_sale_id) if ex.new_sale_id else None,
        "new_sale_number": ex.new_sale.doc_number if ex.new_sale_id else None,
        "return": str(ex.sale_return_id) if ex.sale_return_id else None,
        "returned_amount": str(ex.returned_amount),
        "new_amount": str(ex.new_amount),
        "difference": str(ex.difference),
    }


# ======================================================================
# Запись клиентов
# ======================================================================

class AppointmentSerializer(serializers.ModelSerializer):
    client = serializers.PrimaryKeyRelatedField(queryset=Client.objects.none(), required=False, allow_null=True)
    service = serializers.PrimaryKeyRelatedField(queryset=Product.objects.none())
    performer = serializers.UUIDField(required=False, allow_null=True, source="performer_id")
    end = serializers.DateTimeField(required=False)
    client_name = serializers.CharField(source="client.full_name", read_only=True, default=None)
    service_name = serializers.CharField(source="service.name", read_only=True)

    class Meta:
        model = MarketAppointment
        fields = (
            "id", "client", "client_name", "service", "service_name", "performer",
            "start", "end", "status", "note", "cart", "created_at", "updated_at",
        )
        read_only_fields = ("id", "cart", "created_at", "updated_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = self.context["company"]
        self.fields["client"].queryset = Client.objects.filter(company=company)
        self.fields["service"].queryset = Product.objects.filter(company=company)

    def validate(self, attrs):
        company = self.context["company"]
        inst = self.instance
        service = attrs.get("service") or (inst.service if inst else None)
        start = attrs.get("start") or (inst.start if inst else None)
        if "performer_id" in attrs and attrs["performer_id"]:
            resolve_performers(company.id, [attrs["performer_id"]])
        performer_id = attrs.get("performer_id", inst.performer_id if inst else None)
        if "end" not in attrs and ("start" in attrs or "service" in attrs or inst is None):
            attrs["end"] = start + timedelta(minutes=(service.duration_min or 60))
        end = attrs.get("end") or (inst.end if inst else None)
        if end <= start:
            raise serializers.ValidationError({"end": "Окончание должно быть позже начала."})
        status_ = attrs.get("status") or (inst.status if inst else MarketAppointment.Status.BOOKED)
        active = (MarketAppointment.Status.BOOKED, MarketAppointment.Status.CAME)
        if performer_id and status_ in active:
            clash = MarketAppointment.objects.filter(
                company=company, performer_id=performer_id, status__in=active, start__lt=end, end__gt=start
            )
            if inst is not None:
                clash = clash.exclude(pk=inst.pk)
            if clash.exists():
                raise serializers.ValidationError({"start": "У мастера уже есть запись на это время."})
        return attrs


class AppointmentListCreateAPIView(APIView):
    """GET /api/main/appointments/?date=2026-09-28&performer=…&status=…   POST — записать клиента."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request)
        qs = MarketAppointment.objects.filter(company=company).select_related("client", "service")
        qp = request.query_params
        if qp.get("date"):
            d = parse_date(qp["date"])
            if d is None:
                raise ValidationError({"date": "Формат YYYY-MM-DD."})
            day_start = timezone.make_aware(datetime.combine(d, datetime.min.time()))
            qs = qs.filter(start__gte=day_start, start__lt=day_start + timedelta(days=1))
        for param in ("performer", "client"):
            if qp.get(param):
                try:
                    qs = qs.filter(**{f"{param}_id": uuid.UUID(qp[param])})
                except ValueError:
                    raise ValidationError({param: "Некорректный UUID."})
        if qp.get("status"):
            qs = qs.filter(status__in=qp["status"].split(","))
        ctx = {"company": company}
        return Response(AppointmentSerializer(qs[:1000], many=True, context=ctx).data)

    def post(self, request):
        from apps.integrations.events import emit_event

        company = _company(request)
        ser = AppointmentSerializer(data=request.data, context={"company": company})
        ser.is_valid(raise_exception=True)
        ap = ser.save(company=company, created_by=request.user)
        emit_event(company.id, "appointment.created", {
            "appointment": ap.id, "client": ap.client_id, "service": ap.service_id,
            "performer": ap.performer_id, "start": ap.start, "end": ap.end, "status": ap.status,
        })
        return Response(AppointmentSerializer(ap, context={"company": company}).data, status=status.HTTP_201_CREATED)


class AppointmentDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, pk):
        company = _company(request)
        ap = get_object_or_404(MarketAppointment, pk=pk, company=company)
        ser = AppointmentSerializer(ap, data=request.data, partial=True, context={"company": company})
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def delete(self, request, pk):
        company = _company(request)
        get_object_or_404(MarketAppointment, pk=pk, company=company).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AppointmentToSaleAPIView(APIView):
    """
    POST /api/main/appointments/{id}/to-sale/ → {"sale": <id корзины>}
    Кладёт услугу с мастером в новую корзину смены; оплата — обычным pos/sales/{id}/checkout/.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        company = _company(request)
        with transaction.atomic():
            ap = get_object_or_404(MarketAppointment.objects.select_for_update(), pk=pk, company=company)
            if ap.status in (MarketAppointment.Status.CANCELED, MarketAppointment.Status.NO_SHOW):
                raise ValidationError({"detail": "Запись отменена."})
            if ap.cart_id and Cart.objects.filter(pk=ap.cart_id, status=Cart.Status.ACTIVE).exists():
                return Response({"sale": str(ap.cart_id)})
            cart = _new_cart(company, request.user, _open_shift(company, request.user))
            CartItem(
                company=company,
                branch=cart.branch,
                cart=cart,
                product=ap.service,
                quantity=Decimal("1"),
                unit_price=ap.service.price or ZERO,
                performer_id=ap.performer_id,
            ).save(skip_full_clean=True)
            cart.recalc()
            ap.cart = cart
            ap.status = MarketAppointment.Status.CAME
            ap.save(update_fields=["cart", "status", "updated_at"])
        return Response({"sale": str(cart.id)}, status=status.HTTP_201_CREATED)


# ======================================================================
# Заказ-наряды
# ======================================================================

class WorkOrderCreateSerializer(serializers.Serializer):
    client = serializers.UUIDField(required=False, allow_null=True)
    items = QuickCheckoutItemSerializer(many=True, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    prepayment = MoneyField(required=False, default=ZERO)
    prepayment_method = serializers.CharField(required=False, allow_blank=True, default="cash")
    status = serializers.ChoiceField(
        choices=[WorkOrder.Status.ACCEPTED, WorkOrder.Status.IN_WORK, WorkOrder.Status.READY],
        required=False,
        default=WorkOrder.Status.ACCEPTED,
    )


def _items_json(items):
    out = []
    for it in items:
        row = {k: (str(v) if isinstance(v, (Decimal, uuid.UUID)) else v) for k, v in it.items() if v is not None}
        out.append(row)
    return out


def _items_total(company, items):
    total = ZERO
    for it in items:
        if it.get("price") is not None:
            price = Decimal(str(it["price"]))
        elif it.get("variant"):
            v = ProductVariant.objects.filter(pk=it["variant"], company=company).select_related("product").first()
            if v is None:
                raise ValidationError({"items": "Вариант не найден."})
            price = Decimal(str(v.effective_price or 0))
        elif it.get("product"):
            p = Product.objects.filter(pk=it["product"], company=company).first()
            if p is None:
                raise ValidationError({"items": "Товар не найден."})
            price = Decimal(str(p.price or 0))
        else:
            price = ZERO
        total += price * Decimal(str(it["qty"])) - Decimal(str(it.get("discount") or 0))
    return money(total)


def _work_order_payload(o):
    return {
        "id": str(o.id),
        "number": o.number,
        "status": o.status,
        "client": str(o.client_id) if o.client_id else None,
        "client_name": o.client.full_name if o.client_id else None,
        "items": o.items,
        "description": o.description,
        "total": str(o.total),
        "prepayment": str(o.prepayment),
        "prepayment_method": o.prepayment_method,
        "left_to_pay": str(money(o.total - o.prepayment)),
        "sale": str(o.sale_id) if o.sale_id else None,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "issued_at": o.issued_at.isoformat() if o.issued_at else None,
    }


def _prepayment_flow(order, user, shift, amount, method, *, refund=False):
    from apps.construction.auto_cashflow import create_auto_cashflow
    from apps.construction.models import CashFlow

    return create_auto_cashflow(
        company=order.company,
        branch=shift.branch,
        cashbox=shift.cashbox,
        user=user,
        shift=shift,
        type=CashFlow.Type.EXPENSE if refund else CashFlow.Type.INCOME,
        amount=amount,
        source_kind=CashFlow.SourceKind.WORK_ORDER_PREPAYMENT,
        source_id=str(order.id),
        payment_method=method,
        affects_shift_drawer=(method == "cash"),
        name=f"{'Возврат предоплаты' if refund else 'Предоплата'} по заказ-наряду №{order.number}",
        source_business_operation_id="work_order",
    )


class WorkOrderListCreateAPIView(APIView):
    """GET /api/main/work-orders/?status=…&client=…   POST — принять заказ (предоплата в текущую смену)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request)
        qs = WorkOrder.objects.filter(company=company).select_related("client")
        qp = request.query_params
        if qp.get("status"):
            qs = qs.filter(status__in=qp["status"].split(","))
        if qp.get("client"):
            qs = qs.filter(client_id=qp["client"])
        if qp.get("number", "").isdigit():
            qs = qs.filter(number=int(qp["number"]))
        return Response([_work_order_payload(o) for o in qs[:500]])

    def post(self, request):
        company = _company(request)
        ser = WorkOrderCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        items = data.get("items") or []
        resolve_performers(company.id, [it.get("performer") for it in items])
        client = get_object_or_404(Client, pk=data["client"], company=company) if data.get("client") else None
        total = _items_total(company, items)
        prepayment = money(data.get("prepayment") or ZERO)
        if prepayment < 0 or (items and prepayment > total):
            raise ValidationError({"prepayment": "Предоплата не может быть больше суммы заказа."})

        for attempt in range(3):
            try:
                with transaction.atomic():
                    last = WorkOrder.objects.select_for_update().filter(company=company).order_by("-number").first()
                    shift = _open_shift(company, request.user) if prepayment > 0 else None
                    order = WorkOrder.objects.create(
                        company=company,
                        branch=getattr(shift, "branch", None),
                        number=(last.number + 1) if last else 1,
                        client=client,
                        status=data["status"],
                        items=_items_json(items),
                        description=data.get("description") or "",
                        total=total,
                        prepayment=prepayment,
                        prepayment_method=(data.get("prepayment_method") or "cash") if prepayment > 0 else "",
                        created_by=request.user,
                    )
                    if prepayment > 0:
                        _prepayment_flow(order, request.user, shift, prepayment, order.prepayment_method)
                break
            except IntegrityError:
                if attempt == 2:
                    raise
        return Response(_work_order_payload(order), status=status.HTTP_201_CREATED)


class WorkOrderUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[c for c in WorkOrder.Status.values if c != WorkOrder.Status.ISSUED], required=False)
    items = QuickCheckoutItemSerializer(many=True, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    client = serializers.UUIDField(required=False, allow_null=True)


class WorkOrderDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        return Response(_work_order_payload(get_object_or_404(WorkOrder, pk=pk, company=_company(request))))

    def patch(self, request, pk):
        company = _company(request)
        ser = WorkOrderUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        with transaction.atomic():
            order = get_object_or_404(WorkOrder.objects.select_for_update(), pk=pk, company=company)
            if order.status in (WorkOrder.Status.ISSUED, WorkOrder.Status.CANCELED):
                raise ValidationError({"detail": "Заказ уже закрыт."})
            fields = []
            if "items" in data:
                resolve_performers(company.id, [it.get("performer") for it in data["items"]])
                order.items = _items_json(data["items"])
                order.total = _items_total(company, data["items"])
                if order.prepayment > order.total:
                    raise ValidationError({"items": "Сумма заказа меньше внесённой предоплаты."})
                fields += ["items", "total"]
            if "description" in data:
                order.description = data["description"]
                fields.append("description")
            if "client" in data:
                order.client = get_object_or_404(Client, pk=data["client"], company=company) if data["client"] else None
                fields.append("client")
            if "status" in data:
                if data["status"] == WorkOrder.Status.CANCELED and order.prepayment > 0:
                    # отмена — возвращаем предоплату из кассы текущей смены
                    _prepayment_flow(
                        order, request.user, _open_shift(company, request.user),
                        order.prepayment, order.prepayment_method or "cash", refund=True,
                    )
                order.status = data["status"]
                fields.append("status")
            if fields:
                order.save(update_fields=fields + ["updated_at"])
        return Response(_work_order_payload(order))


class WorkOrderIssueAPIView(APIView):
    """
    POST /api/main/work-orders/{id}/issue/ {"payment": {"method": "cash", "received": "…"}}
    Выдача: чек на весь заказ, предоплата засчитывается строкой «зачёт», доплата — указанным способом.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from apps.main.pos_views import SaleCheckoutAPIView

        company = _company(request)
        with transaction.atomic():
            order = get_object_or_404(WorkOrder.objects.select_for_update(), pk=pk, company=company)
            if order.status == WorkOrder.Status.ISSUED and order.sale_id:
                return Response({**_work_order_payload(order), "replayed": True})
            if order.status == WorkOrder.Status.CANCELED:
                raise ValidationError({"detail": "Заказ отменён."})
            if not order.items:
                raise ValidationError({"items": "В заказе нет позиций."})
            items_ser = QuickCheckoutItemSerializer(data=order.items, many=True)
            items_ser.is_valid(raise_exception=True)

            cart = _new_cart(company, request.user, _open_shift(company, request.user))
            PosQuickCheckoutAPIView()._add_items(cart, items_ser.validated_data, False, None)
            cart.recalc()
            total = money(cart.total or ZERO)
            if order.prepayment > total:
                raise ValidationError({"detail": "Предоплата больше суммы заказа — исправьте позиции."})
            data = _checkout_payload(request.data.get("payment"), total, order.prepayment)
            if order.client_id:
                data["client_id"] = str(order.client_id)
            resp = SaleCheckoutAPIView().checkout(request, cart.id, data, allow_offset=True)
            if resp.status_code >= 400:
                transaction.set_rollback(True)
                return resp
            order.sale_id = resp.data["sale_id"]
            order.total = total
            order.status = WorkOrder.Status.ISSUED
            order.issued_at = timezone.now()
            order.save(update_fields=["sale", "total", "status", "issued_at", "updated_at"])
        body = _work_order_payload(order)
        body["sale_number"] = Sale.objects.filter(pk=order.sale_id).values_list("doc_number", flat=True).first()
        body["change"] = resp.data.get("change")
        return Response(body, status=status.HTTP_201_CREATED)
