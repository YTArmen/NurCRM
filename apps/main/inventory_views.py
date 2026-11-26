"""
Инвентаризация товаров CRM: выравнивание Product.quantity по фактическим учётным количествам.

14.11.2026 П0-4 К-9 (k9_1411):
- строка помнит момент подсчёта (counted_at, из запроса или «сейчас») и остаток на тот момент (quantity_at_count);
- при проведении: остаток = факт + движения после подсчёта (продажи/приходы, пока шёл подсчёт);
  movements_during_count = остаток перед проведением − quantity_at_count; adjust_for_movements=false — как раньше;
- повторное проведение проведённого акта — 200 с replayed=true (идемпотентно), а не 400;
- DELETE inventory/sessions/<id>/ — черновик и отменённый удаляются, проведённый — 409 inventory_applied.
"""
from decimal import Decimal

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.cache_utils import invalidate_cache_pattern
from apps.main.models import Product, ProductInventoryItem, ProductInventorySession
from apps.main.serializers import (
    ProductInventorySessionCreateSerializer,
    ProductInventorySessionReadSerializer,
)
from apps.main.views import CompanyBranchRestrictedMixin


class ProductInventorySessionListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    """
    GET  /api/main/inventory/sessions/  — список актов
    POST /api/main/inventory/sessions/  — черновик: { "note": "", "items": [ { "product_id", "quantity_fact" }, ... ] }
    """

    permission_classes = [permissions.IsAuthenticated]
    queryset = (
        ProductInventorySession.objects.select_related("company", "branch", "created_by")
        .prefetch_related("lines__product")
        .order_by("-created_at")
    )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ProductInventorySessionCreateSerializer
        return ProductInventorySessionReadSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        ser = ProductInventorySessionReadSerializer(queryset, many=True, context=self.get_serializer_context())
        return Response(ser.data)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        ser = ProductInventorySessionCreateSerializer(data=request.data, context=self.get_serializer_context())
        ser.is_valid(raise_exception=True)
        company = self._company()
        if company is None:
            return Response({"detail": "Компания не определена."}, status=status.HTTP_400_BAD_REQUEST)
        branch = self._auto_branch()
        note = ser.validated_data.get("note") or ""
        rows = ser.validated_data["items"]
        from apps.main.tz26.hw_catalog.services import check_location_prefix  # ТЗ 26.10 Б-58: ревизия ряда
        location_info = check_location_prefix(self, request, rows)

        for row in rows:
            pid = row["product_id"]
            pqs = self._filter_qs_company_branch(Product.objects.filter(id=pid))
            if not pqs.exists():
                return Response(
                    {"items": f"Товар {pid} недоступен в текущей компании/филиале."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        session = ProductInventorySession(
            company=company,
            branch=branch,
            created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
            status=ProductInventorySession.Status.DRAFT,
            note=note,
        )
        session.save()

        now = timezone.now()
        for row in rows:
            pid = row["product_id"]
            qf = Decimal(str(row["quantity_fact"])).quantize(Decimal("0.001"))  # ТЗ 31.10 п.19
            at = _counted_at(row.get("counted_at"), now)
            ProductInventoryItem.objects.create(session=session, product_id=pid, quantity_fact=qf,
                                                counted_at=at, quantity_at_count=_qty_at(pid, at, now))

        session.refresh_from_db()
        out = ProductInventorySessionReadSerializer(session, context=self.get_serializer_context())
        return Response({**out.data, **location_info}, status=status.HTTP_201_CREATED)


def _counted_at(raw, now):
    """Момент подсчёта строки: из запроса (не позже «сейчас», не раньше 7 суток) или сейчас."""
    from datetime import timedelta

    if isinstance(raw, str):
        raw = parse_datetime(raw)
    if raw is None:
        return now
    if timezone.is_naive(raw):
        raw = timezone.make_aware(raw)
    if raw > now or raw < now - timedelta(days=7):
        return now
    return raw


def _qty_at(product_id, at, now):
    """Остаток товара на момент at: текущий − движения журнала после at (журнал остатков, К-1)."""
    from django.db.models import Sum

    from apps.main.models import StockLedger

    cur = Product.objects.filter(pk=product_id).values_list("quantity", flat=True).first() or Decimal("0")
    if (now - at).total_seconds() < 1:
        return Decimal(str(cur)).quantize(Decimal("0.001"))
    after = (StockLedger.objects.filter(product_id=product_id, created_at__gt=at).exclude(kind="opening")
             .aggregate(s=Sum("qty_delta"))["s"] or Decimal("0"))
    return (Decimal(str(cur)) - Decimal(str(after))).quantize(Decimal("0.001"))


class ProductInventorySessionRetrieveAPIView(CompanyBranchRestrictedMixin, generics.RetrieveDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated]
    queryset = (
        ProductInventorySession.objects.select_related("company", "branch", "created_by")
        .prefetch_related("lines__product")
        .all()
    )
    serializer_class = ProductInventorySessionReadSerializer

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        qs = self._filter_qs_company_branch(ProductInventorySession.objects.select_for_update().all())
        session = get_object_or_404(qs, pk=kwargs.get("pk"))
        if session.status == ProductInventorySession.Status.APPLIED:
            return Response({"code": "inventory_applied",
                             "detail": "Проведённый акт удалить нельзя: остатки уже изменены."},
                            status=status.HTTP_409_CONFLICT)
        session.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProductInventorySessionApplyAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/inventory/sessions/<pk>/apply/
    Тело (опционально): { "allow_negative": false } — разрешить отрицательный остаток после проведения.
    """

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        qs = self._filter_qs_company_branch(ProductInventorySession.objects.select_for_update().all())
        session = get_object_or_404(qs, pk=pk)
        if session.status == ProductInventorySession.Status.APPLIED:  # К-9: повтор — итог, а не ошибка
            data = ProductInventorySessionReadSerializer(session, context={"request": request}).data
            return Response({**data, "replayed": True})
        if session.status != ProductInventorySession.Status.DRAFT:
            return Response(
                {"code": "inventory_not_draft", "detail": "Провести можно только акт в статусе «черновик»."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw_adj = request.data.get("adjust_for_movements", True)
        adjust = not (raw_adj is False or (isinstance(raw_adj, str) and raw_adj.lower() in ("0", "false", "no")))
        raw = request.data.get("allow_negative", False)
        allow_negative = raw is True or (isinstance(raw, str) and raw.lower() in ("1", "true", "yes"))

        for line in session.lines.select_related("product").all():
            pqs = self._filter_qs_company_branch(Product.objects.select_for_update().filter(pk=line.product_id))
            p = pqs.first()
            if not p:
                return Response(
                    {"detail": f"Товар {line.product_id} недоступен для проведения в этой компании/филиале."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qf = Decimal(str(line.quantity_fact or 0)).quantize(Decimal("0.001"))
            qb = Decimal(str(p.quantity or 0)).quantize(Decimal("0.001"))
            if adjust and line.quantity_at_count is not None:
                qf = (qf + qb - Decimal(str(line.quantity_at_count))).quantize(Decimal("0.001"))
            if not allow_negative and qf < 0:
                return Response(
                    {"detail": f"Отрицательный остаток для «{p.name}» запрещён. Передайте allow_negative=true или исправьте quantity_fact."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            line.quantity_before = qb
            line.quantity_applied = qf
            line.save(update_fields=["quantity_before", "quantity_applied"])
            p.quantity = qf
            p.save(update_fields=["quantity", "updated_at"])

        session.status = ProductInventorySession.Status.APPLIED
        session.applied_at = timezone.now()
        session.save(update_fields=["status", "applied_at", "updated_at"])

        invalidate_cache_pattern(f"analytics:market:{session.company_id}:")
        invalidate_cache_pattern(f"products:list:{session.company_id}:")

        session.refresh_from_db()
        data = ProductInventorySessionReadSerializer(session, context={"request": request}).data
        return Response({**data, "replayed": False})


class ProductInventorySessionCancelAPIView(CompanyBranchRestrictedMixin, APIView):
    """POST /api/main/inventory/sessions/<pk>/cancel/ — только черновик → отменён."""

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        qs = self._filter_qs_company_branch(ProductInventorySession.objects.select_for_update().all())
        session = get_object_or_404(qs, pk=pk)
        if session.status != ProductInventorySession.Status.DRAFT:
            return Response(
                {"detail": "Отменить можно только акт в статусе «черновик»."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        session.status = ProductInventorySession.Status.CANCELED
        session.save(update_fields=["status", "updated_at"])
        session.refresh_from_db()
        return Response(ProductInventorySessionReadSerializer(session, context={"request": request}).data)
