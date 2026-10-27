# apps/cafe/views.py
from decimal import Decimal, InvalidOperation
import json
import uuid
import re

from django.db import transaction, IntegrityError
from django.db.models.deletion import ProtectedError
from django.db.models import Q, Count, Avg, ExpressionWrapper, DurationField, F, Value, DecimalField
from django.db.models.functions import Coalesce
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
import django_filters
from rest_framework import filters, generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
import logging

logger = logging.getLogger(__name__)

from apps.users.models import Branch
from .cache_utils import invalidate_cafe_analytics_cache
from .models import (
    Zone, Table, Booking, Warehouse, Purchase,
    Category, MenuItem, Ingredient,
    Order, OrderItem, CafeClient,
    OrderHistory, OrderItemHistory, OrderDebtPayment,
    OrderRefund, OrderItemRefund, OrderCheckoutPayment,
    KitchenTask, NotificationCafe,
    InventorySession, Equipment, EquipmentInventorySession, Kitchen,
    CafeReceiptPrinterSettings,
    CafeExpense, CafeWaiterPayProfile, OrderCheckoutPayment,
    Preparation, PreparationIngredient, PreparationProcessing, ProcessingType,
    DishIngredient, DishIngredientProcessing,
)
from .serializers import (
    ZoneSerializer, TableSerializer, BookingSerializer,
    WarehouseSerializer, PurchaseSerializer,
    CategorySerializer, MenuItemSerializer, IngredientInlineSerializer,
    OrderSerializer, OrderItemInlineSerializer,
    CafeClientSerializer,
    OrderHistorySerializer, OrderHistoryUpdateSerializer,
    KitchenTaskSerializer, NotificationCafeSerializer,
    InventorySessionSerializer, EquipmentSerializer,
    EquipmentInventorySessionSerializer, KitchenSerializer,
    OrderPaySerializer,
    OrderPayDebtSerializer,
    CafeReceiptPrinterSettingsSerializer,
    CafeExpenseSerializer, CafeWaiterPayProfileSerializer,
    PreparationSerializer, PreparationReceiveSerializer, ProcessingTypeSerializer,
    PreparationIngredientSerializer, PreparationIngredientCreateUpdateSerializer,
    PreparationTechCardSerializer,
    DishIngredientSerializer, DishIngredientProcessingCreateSerializer,
    DishCostSerializer, DishCalculatePreviewSerializer,
    TechCardsExportRequestSerializer, TechCardsExportResponseSerializer,
)
from apps.utils import _is_owner_like


def _can_cafe_order_pay(user) -> bool:
    if _is_owner_like(user):
        return True
    return bool(getattr(user, "can_view_cafe_order_pay", False))


def _can_cafe_order_return(user) -> bool:
    if _is_owner_like(user):
        return True
    return bool(getattr(user, "can_view_cafe_order_return", False))

from .services.costing import (
    recalculate_dish,
    recalculate_preparation,
    recalculate_preparation_tree,
    recalculate_preparations_for_warehouse,
    calculate_margin,
    calculate_ingredient,
)
from .services.stock import consume_dish_for_order, receive_preparation
from .services.warehouse_expense import (
    attach_expense_to_response,
    create_warehouse_initial_expense,
    create_warehouse_receipt_expense,
    equipment_on_create,
    equipment_on_price_update,
    equipment_receive,
)


_NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def _decimal_from_warehouse_remainder(raw) -> Decimal:
    """
    Warehouse.remainder в cafe — CharField. На практике там часто лежит число строкой,
    иногда с пробелами/единицами. Пробуем вытащить первое число.
    """
    if raw is None:
        return Decimal("0")
    if isinstance(raw, Decimal):
        return raw
    s = str(raw).strip()
    if not s:
        return Decimal("0")
    m = _NUM_RE.search(s)
    if not m:
        return Decimal("0")
    try:
        return Decimal(m.group(0).replace(",", "."))
    except InvalidOperation:
        return Decimal("0")


def deduct_ingredients_for_order(order: Order):
    """
    Списывает со склада ингредиенты по заказу.
    Запускать ТОЛЬКО внутри transaction.atomic().
    """
    items = (
        order.items
        .select_related("menu_item")
        .prefetch_related(
            "menu_item__ingredients__product",
            "menu_item__dish_ingredients__product",
            "menu_item__dish_ingredients__preparation",
            "menu_item__dish_ingredients__processings__processing_type",
            "menu_item__dish_ingredients__processings__preparation_processing",
        )
        .all()
    )
    for it in items:
        if it.is_rejected or it.line_kind == OrderItem.LineKind.SERVICE or not it.menu_item_id:
            continue
        qty = Decimal(str(it.quantity or 0))
        if qty <= 0:
            continue
        consume_dish_for_order(it.menu_item, qty)


def _cafe_assign_cash_shift(order: Order, shift_id, company, active_branch):
    """Опциональная привязка заказа к смене construction.CashShift (для отчёта смены)."""
    if not shift_id:
        return
    from apps.construction.models import CashShift

    sh = CashShift.objects.filter(pk=shift_id, company=company).first()
    if not sh:
        raise ValidationError({"cash_shift_id": "Смена не найдена."})
    if active_branch is not None and sh.branch_id not in (None, active_branch.id):
        raise ValidationError({"cash_shift_id": "Смена привязана к другому филиалу."})
    order.cash_shift_id = sh.id


try:
    from apps.users.permissions import IsCompanyOwnerOrAdmin
except Exception:
    class IsCompanyOwnerOrAdmin(permissions.BasePermission):  # fallback, пускает только staff/superuser
        def has_permission(self, request, view):
            u = request.user
            return bool(u and u.is_authenticated and (u.is_staff or u.is_superuser))


# --------- company + branch (как в барбере/букинге) ---------
from apps.users.permissions import read_only_guard  # noqa: E402
class CompanyBranchQuerysetMixin:
    permission_classes = [permissions.IsAuthenticated]

    def _user(self):
        return getattr(self.request, "user", None)

    def _user_company(self):
        """
        Компания пользователя:
          - сначала owned_company / company
          - если нет, пробуем взять через user.branch.company
        """
        u = self._user()
        if not u or not getattr(u, "is_authenticated", False):
            return None

        company = getattr(u, "company", None) or getattr(u, "owned_company", None)
        if company:
            return company

        br = getattr(u, "branch", None)
        if br is not None:
            return getattr(br, "company", None)

        return None

    def _model_has_field(self, qs, field_name: str) -> bool:
        model = getattr(qs, "model", None)
        if not model:
            return False
        return any(getattr(f, "name", None) == field_name for f in model._meta.get_fields())

    def _fixed_branch_from_user(self, company):
        """
        «Жёсткий» филиал сотрудника (который нельзя менять ?branch):

          - user.primary_branch() / user.primary_branch (если принадлежит company)
          - user.branch (если принадлежит company)
          - если есть branch_ids и там ровно один филиал этой компании — считаем его фиксированным
        """
        user = self._user()
        if not user or not company:
            return None

        company_id = getattr(company, "id", None)

        primary = getattr(user, "primary_branch", None)
        if callable(primary):
            try:
                val = primary()
                if val and getattr(val, "company_id", None) == company_id:
                    return val
            except Exception:
                pass

        if primary and not callable(primary) and getattr(primary, "company_id", None) == company_id:
            return primary

        if hasattr(user, "branch"):
            b = getattr(user, "branch")
            if b and getattr(b, "company_id", None) == company_id:
                return b

        branch_ids = getattr(user, "branch_ids", None)
        if isinstance(branch_ids, (list, tuple)) and len(branch_ids) == 1:
            try:
                return Branch.objects.get(id=branch_ids[0], company_id=company_id)
            except Branch.DoesNotExist:
                pass

        return None

    def _active_branch(self):
        """
        Определяем активный филиал:

          1) жёстко назначенный филиал (primary / user.branch / единственный из branch_ids)
          2) если жёсткого нет — ?branch=<uuid>, если филиал принадлежит компании
          3) иначе None (работаем по всей компании)

        ВАЖНО: если нашли филиал — он точно из компании пользователя.
        """
        request = self.request
        company = self._user_company()
        if not company:
            setattr(request, "branch", None)
            return None

        company_id = getattr(company, "id", None)

        fixed = self._fixed_branch_from_user(company)
        if fixed is not None:
            setattr(request, "branch", fixed)
            return fixed

        branch_id = None
        if hasattr(request, "query_params"):
            branch_id = request.query_params.get("branch")
        elif hasattr(request, "GET"):
            branch_id = request.GET.get("branch")

        if branch_id:
            try:
                br = Branch.objects.get(id=branch_id, company_id=company_id)
                setattr(request, "branch", br)
                return br
            except (Branch.DoesNotExist, ValueError):
                pass

        setattr(request, "branch", None)
        return None

    def get_queryset(self):
        qs = super().get_queryset()
        company = self._user_company()
        if not company:
            return qs.none()

        if self._model_has_field(qs, "company"):
            qs = qs.filter(company=company)

        # ВНИМАНИЕ: текущая логика — "жёстко только филиал" для branch-моделей.
        # История заказов обходит это ограничение (см. OrderHistoryListView/ClientOrderHistoryListView).
        if self._model_has_field(qs, "branch"):
            active_branch = self._active_branch()
            if active_branch is not None:
                qs = qs.filter(branch=active_branch)

        return qs

    def perform_create(self, serializer):
        company = self._user_company()
        if not company:
            raise permissions.PermissionDenied("У пользователя не задана компания.")

        try:
            model_fields = set(f.name for f in serializer.Meta.model._meta.get_fields())
        except Exception:
            model_fields = set()

        kwargs = {"company": company}

        if "branch" in model_fields:
            active_branch = self._active_branch()
            if active_branch is not None:
                kwargs["branch"] = active_branch

        serializer.save(**kwargs)

    def perform_update(self, serializer):
        company = self._user_company()
        if not company:
            raise permissions.PermissionDenied("У пользователя не задана компания.")
        serializer.save(company=company)


# ==================== Настройки принтера кассы (чековый принтер) ====================
class ReceiptPrinterSettingsView(CompanyBranchQuerysetMixin, APIView):
    """
    GET  /cafe/receipt-printer/ — получить настройки принтера кассы (по компании).
    PATCH /cafe/receipt-printer/ — сохранить/обновить настройки (printer обязателен).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = self._user_company()
        if not company:
            return Response(
                {"ok": False, "error": "У пользователя не задана компания."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            settings_obj = CafeReceiptPrinterSettings.objects.get(company=company)
        except CafeReceiptPrinterSettings.DoesNotExist:
            return Response(
                {"printer": "", "bridge_url": "", "updated_at": None},
                status=status.HTTP_200_OK,
            )
        serializer = CafeReceiptPrinterSettingsSerializer(instance=settings_obj)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request):
        company = self._user_company()
        if not company:
            return Response(
                {"ok": False, "error": "У пользователя не задана компания."},
                status=status.HTTP_403_FORBIDDEN,
            )
        printer_raw = request.data.get("printer")
        if printer_raw is None or not str(printer_raw).strip():
            return Response(
                {"ok": False, "error": "Поле printer обязательно."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = CafeReceiptPrinterSettingsSerializer(
            data=request.data,
            partial=True,
            context={"request": request},
        )
        if not serializer.is_valid():
            errors = serializer.errors
            msg = errors.get("printer", errors.get("bridge_url", errors))
            if isinstance(msg, list):
                msg = msg[0] if msg else "Ошибка валидации."
            return Response(
                {"ok": False, "error": str(msg)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        settings_obj, _ = CafeReceiptPrinterSettings.objects.get_or_create(
            company=company,
            defaults={"printer": "", "bridge_url": ""},
        )
        settings_obj.printer = serializer.validated_data.get("printer", "")
        settings_obj.bridge_url = serializer.validated_data.get("bridge_url", "")
        settings_obj.save(update_fields=["printer", "bridge_url", "updated_at"])
        out = CafeReceiptPrinterSettingsSerializer(instance=settings_obj).data
        return Response(out, status=status.HTTP_200_OK)


# ==================== CafeClient ====================
class CafeClientListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = CafeClient.objects.all()
    serializer_class = CafeClientSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["name", "phone"]
    search_fields = ["name", "phone"]
    ordering_fields = ["name", "id"]


class CafeClientRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = CafeClient.objects.all()
    serializer_class = CafeClientSerializer


# --------- Filters ---------
class _CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    """Поддержка query param вида: ?status__in=open,closed"""


class OrderFilter(django_filters.FilterSet):
    # статус (точно)
    status = django_filters.CharFilter(field_name="status")
    # статус (несколько)
    status__in = _CharInFilter(field_name="status", lookup_expr="in")

    # диапазон дат (удобные имена параметров)
    created_at_from = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_at_to = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="lte")

    table_session_id = django_filters.UUIDFilter(field_name="table_session_id")

    class Meta:
        model = Order
        fields = ["table", "waiter", "client", "guests", "status", "table_session_id"]


class ClientOrderListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    """
    /cafe/clients/<uuid:pk>/orders/
    GET  — список заказов клиента
    POST — создать заказ этому клиенту (company и client проставляются автоматически)
    """
    queryset = (
        Order.objects
        .select_related("table", "waiter", "company", "client")
        .prefetch_related("items__menu_item")
    )
    serializer_class = OrderSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = OrderFilter
    ordering_fields = ["created_at", "guests", "id"]

    def _get_client(self):
        company = self._user_company()
        return generics.get_object_or_404(CafeClient, pk=self.kwargs["pk"], company=company)

    def get_queryset(self):
        return super().get_queryset().filter(client=self._get_client())

    def perform_create(self, serializer):
        client = self._get_client()
        super().perform_create(serializer)
        order = serializer.instance
        order.client = client
        order.company = client.company
        order.save(update_fields=["client", "company"])
        # Пересчитываем сумму заказа
        order.recalc_total()
        order.save(update_fields=["total_amount"])
        
        # Устанавливаем стол как занятый при создании заказа
        if order.table_id:
            _sync_table_status(order.table_id)
        
        # Отправляем WebSocket уведомление о создании заказа
        send_order_created_notification(order)


# -------- История заказов клиента (вложенно) --------
class ClientOrderHistoryListView(CompanyBranchQuerysetMixin, generics.ListAPIView):
    """
    /cafe/clients/<uuid:pk>/orders/history/
    История (архив) заказов конкретного клиента в рамках компании пользователя.

    ВАЖНО: здесь НЕ используем super().get_queryset(), потому что миксин
    режет branch до "только branch=active_branch" и убивает global-историю.
    """
    serializer_class = OrderHistorySerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["table", "waiter", "created_at", "archived_at", "guests"]
    ordering_fields = ["created_at", "archived_at", "id"]

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return OrderHistory.objects.none()

        client = generics.get_object_or_404(CafeClient, pk=self.kwargs["pk"], company=company)

        qs = (
            OrderHistory.objects
            .select_related("client", "table", "waiter", "company")
            .filter(company=company, client=client)
        )

        active_branch = self._active_branch()
        if active_branch is not None:
            qs = qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))

        return qs.order_by("-created_at")


# -------- Общая история заказов по компании --------
class OrderHistoryListView(CompanyBranchQuerysetMixin, generics.ListAPIView):
    """
    /cafe/orders/history/
    История (архив) всех заказов компании.

    ВАЖНО: здесь НЕ используем super().get_queryset() по той же причине:
    миксин "жёстко только филиал" убивает global-историю и/или историю других филиалов.
    """
    serializer_class = OrderHistorySerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["client", "table", "waiter", "created_at", "archived_at", "guests"]
    search_fields = ["client__name", "client__phone"]
    ordering_fields = ["created_at", "archived_at", "id"]

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return OrderHistory.objects.none()

        qs = (
            OrderHistory.objects
            .select_related("client", "table", "waiter", "company")
            .filter(company=company)
        )

        active_branch = self._active_branch()
        if active_branch is not None:
            qs = qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))

        return qs.order_by("-created_at")


class OrderHistoryRetrieveUpdateView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateAPIView):
    """
    GET/PATCH /cafe/orders/history/<uuid:pk>/
    Чтение — как у списка; правка снимка — только владелец/админ/staff.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return OrderHistoryUpdateSerializer
        return OrderHistorySerializer

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return OrderHistory.objects.none()
        qs = (
            OrderHistory.objects.filter(company=company)
            .select_related("client", "table", "waiter", "company")
            .prefetch_related("items")
        )
        active_branch = self._active_branch()
        if active_branch is not None:
            qs = qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
        return qs.order_by("-created_at")

    def update(self, request, *args, **kwargs):
        if not _is_owner_like(request.user):
            return Response(
                {"detail": "Редактирование архива доступно только владельцу или администратору."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().update(request, *args, **kwargs)


# ==================== Zone ====================
class ZoneListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = Zone.objects.all()
    serializer_class = ZoneSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["title"]
    search_fields = ["title"]
    ordering_fields = ["title", "id"]


class ZoneRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Zone.objects.all()
    serializer_class = ZoneSerializer


# ==================== Table ====================
class TableListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = Table.objects.select_related("zone", "company").all()
    serializer_class = TableSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["zone", "number", "places", "status"]
    search_fields = ["zone__title"]
    ordering_fields = ["number", "places", "status", "id"]
    
    def perform_create(self, serializer):
        super().perform_create(serializer)
        table = serializer.instance
        # Отправляем WebSocket уведомление о создании стола
        send_table_created_notification(table)


class TableRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Table.objects.select_related("zone", "company").all()
    serializer_class = TableSerializer
    
    def perform_update(self, serializer):
        super().perform_update(serializer)
        table = serializer.instance
        # Отправляем WebSocket уведомление об обновлении стола
        send_table_updated_notification(table)


# ==================== Booking ====================
class BookingListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = Booking.objects.select_related("table", "company").all()
    serializer_class = BookingSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["date", "time", "table", "status", "guests"]
    search_fields = ["guest", "phone"]
    ordering_fields = ["date", "time", "guests", "id"]


class BookingRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Booking.objects.select_related("table", "company").all()
    serializer_class = BookingSerializer


# ==================== Warehouse ====================
class WarehouseListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = Warehouse.objects.all()
    serializer_class = WarehouseSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["title", "unit", "supplier"]
    search_fields = ["title", "unit", "supplier"]
    ordering_fields = ["title", "id"]

    def perform_create(self, serializer):
        try:
            obj = serializer.save()
        except IntegrityError as e:
            msg = str(e)
            if "uniq_warehouse_title_" in msg:
                raise ValidationError({"title": "Склад с таким названием уже существует в этой компании или филиале."})
            raise
        remainder = _decimal_from_warehouse_remainder(obj.remainder)
        if remainder > 0 and (obj.unit_price or Decimal("0")) >= 0:
            req = getattr(self, "request", None)
            user = req.user if req and req.user.is_authenticated else None
            create_warehouse_initial_expense(warehouse=obj, user=user)
            invalidate_cafe_analytics_cache(obj.company_id)


class WarehouseRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Warehouse.objects.all()
    serializer_class = WarehouseSerializer

    def perform_update(self, serializer):
        inst = serializer.instance
        old_price = Decimal(inst.unit_price or 0) if inst else Decimal("0")
        old_remainder = _decimal_from_warehouse_remainder(inst.remainder) if inst else Decimal("0")
        try:
            obj = serializer.save()
        except IntegrityError as e:
            msg = str(e)
            if "uniq_warehouse_title_" in msg:
                raise ValidationError({"title": "Склад с таким названием уже существует в этой компании или филиале."})
            raise
        new_price = Decimal(obj.unit_price or 0)
        if old_price != new_price:
            with transaction.atomic():
                try:
                    recalculate_preparations_for_warehouse(obj)
                except ValueError as e:
                    raise ValidationError({"detail": str(e)})
        new_remainder = _decimal_from_warehouse_remainder(obj.remainder)
        delta_qty = new_remainder - old_remainder
        if delta_qty > 0:
            req = getattr(self, "request", None)
            user = req.user if req and req.user.is_authenticated else None
            unit_price = new_price
            if "unit_price" not in serializer.validated_data:
                unit_price = new_price
            create_warehouse_receipt_expense(
                warehouse=obj,
                quantity=delta_qty,
                unit_price=unit_price,
                user=user,
                source=CafeExpense.Source.WAREHOUSE_RECEIPT,
                note=f"Склад: приход {delta_qty} {obj.unit}",
                skip_remainder_update=True,
            )
            invalidate_cafe_analytics_cache(obj.company_id)


class WarehouseStockAdjustView(CompanyBranchQuerysetMixin, APIView):
    """
    POST /cafe/warehouse/<uuid:pk>/adjust/
    Тело: {"remainder": "12.5"} — зафиксировать остаток, или {"adjust_by": "-0.5"} — изменить на дельту.
    Доступ: владелец / администратор / staff (см. apps.utils._is_owner_like).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        if not _is_owner_like(request.user):
            return Response(
                {"detail": "Недостаточно прав для корректировки склада."},
                status=status.HTTP_403_FORBIDDEN,
            )
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)
        wh = generics.get_object_or_404(Warehouse.objects.filter(company=company), pk=pk)
        active_branch = self._active_branch()
        if active_branch is not None:
            if wh.branch_id not in (None, active_branch.id):
                return Response({"detail": "Позиция другого филиала."}, status=status.HTTP_404_NOT_FOUND)
        else:
            if wh.branch_id is not None:
                return Response(
                    {"detail": "Позиция привязана к филиалу — выберите филиал в контексте запроса."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        current = _decimal_from_warehouse_remainder(wh.remainder)
        new_val = None
        if "remainder" in request.data and request.data.get("remainder") is not None:
            try:
                raw = request.data.get("remainder")
                new_val = Decimal(str(raw).replace(",", ".").strip())
            except Exception:
                return Response({"detail": "Некорректное значение remainder."}, status=status.HTTP_400_BAD_REQUEST)
        elif "adjust_by" in request.data and request.data.get("adjust_by") is not None:
            try:
                delta = Decimal(str(request.data.get("adjust_by")).replace(",", ".").strip())
            except Exception:
                return Response({"detail": "Некорректное значение adjust_by."}, status=status.HTTP_400_BAD_REQUEST)
            new_val = current + delta
        else:
            return Response({"detail": "Укажите remainder или adjust_by."}, status=status.HTTP_400_BAD_REQUEST)

        wh.remainder = str(new_val)
        wh.save(update_fields=["remainder"])
        invalidate_cafe_analytics_cache(company.id)
        return Response(WarehouseSerializer(wh, context={"request": request}).data, status=status.HTTP_200_OK)


# ==================== Purchase ====================
class PurchaseListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = Purchase.objects.all()
    serializer_class = PurchaseSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["supplier"]
    search_fields = ["supplier"]
    ordering_fields = ["price", "id"]


class PurchaseRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Purchase.objects.all()
    serializer_class = PurchaseSerializer


# ==================== Cafe operational expenses / зарплата официантов ====================
class CafeExpenseListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = CafeExpenseSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["category", "branch"]
    ordering_fields = ["expense_date", "created_at", "amount", "id"]

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return CafeExpense.objects.none()
        qs = CafeExpense.objects.filter(company=company)
        b = self._active_branch()
        if b is not None:
            qs = qs.filter(Q(branch=b) | Q(branch__isnull=True))
        else:
            qs = qs.filter(branch__isnull=True)
        return qs.order_by("-expense_date", "-created_at")


class CafeExpenseRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = CafeExpenseSerializer

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return CafeExpense.objects.none()
        qs = CafeExpense.objects.filter(company=company)
        b = self._active_branch()
        if b is not None:
            qs = qs.filter(Q(branch=b) | Q(branch__isnull=True))
        else:
            qs = qs.filter(branch__isnull=True)
        return qs


@read_only_guard("can_edit_salary")
class CafeWaiterPayProfileListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = CafeWaiterPayProfileSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["user", "branch"]
    ordering_fields = ["id"]

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return CafeWaiterPayProfile.objects.none()
        qs = CafeWaiterPayProfile.objects.filter(company=company)
        b = self._active_branch()
        if b is not None:
            qs = qs.filter(Q(branch=b) | Q(branch__isnull=True))
        else:
            qs = qs.filter(branch__isnull=True)
        return qs


@read_only_guard("can_edit_salary")
class CafeWaiterPayProfileRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = CafeWaiterPayProfileSerializer

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return CafeWaiterPayProfile.objects.none()
        qs = CafeWaiterPayProfile.objects.filter(company=company)
        b = self._active_branch()
        if b is not None:
            qs = qs.filter(Q(branch=b) | Q(branch__isnull=True))
        else:
            qs = qs.filter(branch__isnull=True)
        return qs


# ==================== Category ====================
class CategoryListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["title"]
    search_fields = ["title"]
    ordering_fields = ["title", "id"]


class CategoryRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


# ==================== MenuItem (+ ingredients nested) ====================
class MenuItemListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = (
        MenuItem.objects
        .select_related("category", "company")
        .prefetch_related(
            "ingredients__product",
            "dish_ingredients__product",
            "dish_ingredients__preparation",
            "dish_ingredients__processings__processing_type",
            "dish_ingredients__processings__preparation_processing",
        )
        .all()
    )
    serializer_class = MenuItemSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["category", "kitchen", "is_active", "title"]
    search_fields = ["title", "category__title"]
    ordering_fields = ["title", "price", "is_active", "id"]

    def get_queryset(self):
        """
        Для меню обычно нужно видеть и "глобальные" позиции (branch=NULL),
        даже когда выбран конкретный филиал (?branch=...).
        """
        qs = self.queryset.all()
        company = self._user_company()
        if not company:
            return qs.none()

        qs = qs.filter(company=company)

        active_branch = self._active_branch()
        if active_branch is not None:
            qs = qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))

        return qs


class MenuItemRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = (
        MenuItem.objects
        .select_related("category", "company")
        .prefetch_related(
            "ingredients__product",
            "dish_ingredients__product",
            "dish_ingredients__preparation",
            "dish_ingredients__processings__processing_type",
            "dish_ingredients__processings__preparation_processing",
        )
        .all()
    )
    serializer_class = MenuItemSerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        snapshot_title = (instance.title or "").strip() or "Позиция меню"
        snapshot_price = instance.price if instance.price is not None else Decimal("0")

        try:
            with transaction.atomic():
                # Задачи кухни привязаны через PROTECT — удаляем (в т.ч. завершённые).
                KitchenTask.objects.filter(menu_item=instance).delete()

                # Строки заказов с этим блюдом → услуга с текстовым названием и ценой (снимок).
                OrderItem.objects.filter(
                    menu_item=instance,
                    line_kind=OrderItem.LineKind.MENU,
                ).update(
                    line_kind=OrderItem.LineKind.SERVICE,
                    menu_item=None,
                    service_title=snapshot_title,
                    unit_price=Coalesce(
                        F("unit_price"),
                        Value(snapshot_price, output_field=DecimalField(max_digits=12, decimal_places=2)),
                    ),
                )

                instance.delete()
        except ProtectedError:
            return Response(
                {
                    "detail": (
                        "Нельзя удалить позицию меню: остались связанные записи, которые не удалось отвязать. "
                        "Попробуйте деактивировать позицию (is_active=false)."
                    ),
                    "order_items_count": OrderItem.objects.filter(menu_item_id=instance.pk).count(),
                    "kitchen_tasks_count": KitchenTask.objects.filter(menu_item_id=instance.pk).count(),
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)


# ==================== Ingredient ====================
class IngredientListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = IngredientInlineSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["menu_item", "product"]
    ordering_fields = ["id"]

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return Ingredient.objects.none()
        active_branch = self._active_branch()
        qs = Ingredient.objects.select_related("menu_item", "product")
        qs = qs.filter(menu_item__company=company, product__company=company)
        if active_branch is not None:
            return qs.filter(
                Q(menu_item__branch=active_branch) | Q(menu_item__branch__isnull=True),
                Q(product__branch=active_branch) | Q(product__branch__isnull=True),
            )
        return qs.filter(menu_item__branch__isnull=True, product__branch__isnull=True)


class IngredientRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = IngredientInlineSerializer

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return Ingredient.objects.none()
        active_branch = self._active_branch()
        qs = Ingredient.objects.select_related("menu_item", "product").filter(
            menu_item__company=company, product__company=company
        )
        if active_branch is not None:
            return qs.filter(
                Q(menu_item__branch=active_branch) | Q(menu_item__branch__isnull=True),
                Q(product__branch=active_branch) | Q(product__branch__isnull=True),
            )
        return qs.filter(menu_item__branch__isnull=True, product__branch__isnull=True)


# ==================== Preparations ====================
class PreparationListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = PreparationSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["source_product", "is_active"]
    search_fields = ["name"]
    ordering_fields = ["name", "created_at", "id"]

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return Preparation.objects.none()
        b = self._active_branch()
        qs = Preparation.objects.select_related("source_product").prefetch_related("processings").filter(company=company)
        if b is not None:
            return qs.filter(Q(branch=b) | Q(branch__isnull=True))
        return qs.filter(branch__isnull=True)

    def perform_create(self, serializer):
        company = self._user_company()
        if not company:
            raise permissions.PermissionDenied("У пользователя не задана компания.")

        try:
            model_fields = set(f.name for f in serializer.Meta.model._meta.get_fields())
        except Exception:
            model_fields = set()

        kwargs = {"company": company}
        if "branch" in model_fields:
            active_branch = self._active_branch()
            if active_branch is not None:
                kwargs["branch"] = active_branch

        with transaction.atomic():
            obj: Preparation = serializer.save(**kwargs)
            try:
                recalculate_preparation(obj, save=True)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})


class PreparationRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PreparationSerializer

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return Preparation.objects.none()
        b = self._active_branch()
        qs = Preparation.objects.select_related("source_product").prefetch_related("processings").filter(company=company)
        if b is not None:
            return qs.filter(Q(branch=b) | Q(branch__isnull=True))
        return qs.filter(branch__isnull=True)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            self.perform_destroy(instance)
        except ProtectedError:
            return Response(
                {
                    "detail": (
                        "Нельзя удалить заготовку: она указана в техкартах блюд (ингредиент типа preparation). "
                        "Сначала удалите её из состава этих блюд."
                    ),
                    "dish_ingredient_count": DishIngredient.objects.filter(preparation=instance).count(),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_update(self, serializer):
        with transaction.atomic():
            obj: Preparation = serializer.save()
            try:
                recalculate_preparation_tree(obj)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})


def _preparation_queryset_for_user(mixin, company):
    b = mixin._active_branch()
    qs = Preparation.objects.select_related("source_product").prefetch_related(
        "processings", "ingredients__product", "ingredients__child_preparation",
    ).filter(company=company)
    if b is not None:
        return qs.filter(Q(branch=b) | Q(branch__isnull=True))
    return qs.filter(branch__isnull=True)


def _build_preparation_tech_card(prep: Preparation) -> dict:
    items = []
    for row in prep.ingredients.select_related("product", "child_preparation").order_by("created_at"):
        if row.product_id:
            items.append({
                "id": row.id,
                "type": "product",
                "name": row.product.title,
                "quantity": row.quantity,
                "unit": row.unit,
                "waste_percent": row.waste_percent,
                "unit_cost": row.unit_cost,
                "ingredient_cost": row.ingredient_cost,
                "processing_cost": row.processing_cost,
                "total_cost": row.total_cost,
            })
        else:
            items.append({
                "id": row.id,
                "type": "preparation",
                "name": row.child_preparation.name,
                "quantity": row.quantity,
                "unit": row.unit,
                "waste_percent": row.waste_percent,
                "unit_cost": row.unit_cost,
                "ingredient_cost": row.ingredient_cost,
                "processing_cost": row.processing_cost,
                "total_cost": row.total_cost,
            })
    return {
        "id": prep.id,
        "name": prep.name,
        "type": "preparation",
        "output_quantity": prep.output_quantity,
        "output_unit": prep.output_unit,
        "total_cost": prep.total_cost,
        "unit_cost": prep.unit_cost,
        "stock_quantity": prep.stock_quantity,
        "items": items,
    }


class PreparationTechCardView(CompanyBranchQuerysetMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)
        prep = generics.get_object_or_404(_preparation_queryset_for_user(self, company), pk=pk)
        with transaction.atomic():
            try:
                recalculate_preparation_tree(prep)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})
        prep.refresh_from_db()
        data = _build_preparation_tech_card(prep)
        return Response(PreparationTechCardSerializer(data).data)


class PreparationIngredientListCreateView(CompanyBranchQuerysetMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, preparation_id):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)
        prep = generics.get_object_or_404(_preparation_queryset_for_user(self, company), pk=preparation_id)
        rows = prep.ingredients.select_related("product", "child_preparation").order_by("created_at")
        return Response(
            PreparationIngredientSerializer(rows, many=True, context={"request": request}).data
        )

    def post(self, request, preparation_id):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)
        prep = generics.get_object_or_404(_preparation_queryset_for_user(self, company), pk=preparation_id)
        ser = PreparationIngredientCreateUpdateSerializer(
            data=request.data,
            context={"request": request, "preparation": prep},
        )
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            row = PreparationIngredient.objects.create(preparation=prep, **ser.validated_data)
            try:
                recalculate_preparation_tree(prep)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})
        row.refresh_from_db()
        return Response(
            PreparationIngredientSerializer(row, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class PreparationIngredientRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PreparationIngredientCreateUpdateSerializer

    def get_serializer_class(self):
        if self.request.method == "GET":
            return PreparationIngredientSerializer
        return PreparationIngredientCreateUpdateSerializer

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return PreparationIngredient.objects.none()
        b = self._active_branch()
        qs = PreparationIngredient.objects.select_related(
            "preparation", "product", "child_preparation",
        ).filter(preparation__company=company)
        if b is not None:
            return qs.filter(Q(preparation__branch=b) | Q(preparation__branch__isnull=True))
        return qs.filter(preparation__branch__isnull=True)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.kwargs.get("pk"):
            row = self.get_queryset().filter(pk=self.kwargs["pk"]).first()
            if row:
                ctx["preparation"] = row.preparation
        return ctx

    def perform_update(self, serializer):
        prep = serializer.instance.preparation
        serializer.context["preparation"] = prep
        with transaction.atomic():
            row: PreparationIngredient = serializer.save()
            try:
                recalculate_preparation_tree(row.preparation)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})

    def perform_destroy(self, instance):
        prep = instance.preparation
        with transaction.atomic():
            super().perform_destroy(instance)
            try:
                recalculate_preparation_tree(prep)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})


class PreparationReceiveView(CompanyBranchQuerysetMixin, APIView):
    """
    POST /cafe/preparations/<uuid:pk>/receive/
    Оприходовать (произвести) заготовку: списывает сырьё со склада по входу и
    увеличивает остаток заготовки по выходу (накопительно).
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)

        b = self._active_branch()
        qs = Preparation.objects.select_related("source_product").prefetch_related("processings").filter(company=company)
        if b is not None:
            qs = qs.filter(Q(branch=b) | Q(branch__isnull=True))
        else:
            qs = qs.filter(branch__isnull=True)

        ser = PreparationReceiveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        with transaction.atomic():
            # of=("self",): FOR UPDATE только по строке Preparation. Без этого Postgres
            # падает — select_related("source_product") даёт LEFT JOIN по nullable FK,
            # а FOR UPDATE нельзя наложить на nullable-сторону outer join.
            prep: Preparation = generics.get_object_or_404(
                qs.prefetch_related("ingredients").select_for_update(of=("self",)), pk=pk
            )
            vd = ser.validated_data
            try:
                receive_preparation(
                    prep,
                    batch_output_quantity=vd.get("batch_output_quantity"),
                    input_quantity=vd.get("input_quantity"),
                    output_quantity=vd.get("output_quantity"),
                    processing_cost=vd.get("processing_cost"),
                )
            except ValidationError:
                raise
            except ValueError as e:
                raise ValidationError({"detail": str(e)})
            prep.refresh_from_db()

        return Response(PreparationSerializer(prep).data, status=status.HTTP_200_OK)


# ==================== Processing types ====================
class ProcessingTypeListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = ProcessingTypeSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_active", "charge_type"]
    search_fields = ["name"]
    ordering_fields = ["name", "id"]

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return ProcessingType.objects.none()
        b = self._active_branch()
        qs = ProcessingType.objects.filter(company=company)
        if b is not None:
            return qs.filter(Q(branch=b) | Q(branch__isnull=True))
        return qs.filter(branch__isnull=True)


class ProcessingTypeRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ProcessingTypeSerializer

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return ProcessingType.objects.none()
        b = self._active_branch()
        qs = ProcessingType.objects.filter(company=company)
        if b is not None:
            return qs.filter(Q(branch=b) | Q(branch__isnull=True))
        return qs.filter(branch__isnull=True)

    def perform_update(self, serializer):
        obj: ProcessingType = serializer.save()
        # Автопересчёт блюд, где используется этот тип обработки
        ing_ids = DishIngredientProcessing.objects.filter(processing_type=obj).values_list("ingredient_id", flat=True).distinct()
        dish_ids = DishIngredient.objects.filter(id__in=ing_ids).values_list("dish_id", flat=True).distinct()
        for d in MenuItem.objects.filter(id__in=dish_ids).all():
            try:
                recalculate_dish(d, save=True)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})


# ==================== Dish ingredients (new) ====================
class DishIngredientCreateForDishView(CompanyBranchQuerysetMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)
        b = self._active_branch()
        dish = generics.get_object_or_404(MenuItem.objects.filter(company=company), pk=pk)
        if b is not None and dish.branch_id not in (None, b.id):
            return Response({"detail": "Блюдо другого филиала."}, status=status.HTTP_404_NOT_FOUND)

        ser = DishIngredientSerializer(data={**request.data, "dish": str(dish.id)}, context={"request": request})
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            ing: DishIngredient = ser.save()
            try:
                recalculate_dish(dish, save=True)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})
        return Response(DishIngredientSerializer(ing, context={"request": request}).data, status=status.HTTP_201_CREATED)


def _legacy_ingredient_as_dish_ingredient_dict(legacy: Ingredient) -> dict:
    """
    Совместимость API: фронт вызывает /dish-ingredients/<id>/, а старые блюда хранят строки в Ingredient (legacy).
    Отдаём те же ключи, что DishIngredientSerializer.
    """
    legacy.refresh_from_db()
    pr = legacy.product
    amt = Decimal(legacy.amount or 0)
    up = Decimal(pr.unit_price or 0)
    ing_cost = (amt * up).quantize(Decimal("0.01"))
    return {
        "id": legacy.id,
        "dish": legacy.menu_item_id,
        "ingredient_type": DishIngredient.IngredientType.PRODUCT,
        "product": legacy.product_id,
        "product_title": pr.title,
        "preparation": None,
        "preparation_name": None,
        "quantity": amt,
        "unit": (legacy.unit or pr.unit or ""),
        "unit_cost": up.quantize(Decimal("0.0001")),
        "ingredient_cost": ing_cost,
        "processing_cost": Decimal("0.00"),
        "total_cost": ing_cost,
        "processings": [],
        "created_at": None,
        "updated_at": None,
    }


class DishIngredientRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = DishIngredientSerializer

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return DishIngredient.objects.none()
        b = self._active_branch()
        qs = DishIngredient.objects.select_related("dish", "product", "preparation").filter(dish__company=company)
        if b is not None:
            return qs.filter(Q(dish__branch=b) | Q(dish__branch__isnull=True))
        return qs.filter(dish__branch__isnull=True)

    def _legacy_ingredient_queryset(self):
        """Тот же scope, что IngredientRetrieveUpdateDestroyView (старые ингредиенты блюда)."""
        company = self._user_company()
        if not company:
            return Ingredient.objects.none()
        active_branch = self._active_branch()
        qs = Ingredient.objects.select_related("menu_item", "product").filter(
            menu_item__company=company, product__company=company
        )
        if active_branch is not None:
            return qs.filter(
                Q(menu_item__branch=active_branch) | Q(menu_item__branch__isnull=True),
                Q(product__branch=active_branch) | Q(product__branch__isnull=True),
            )
        return qs.filter(menu_item__branch__isnull=True, product__branch__isnull=True)

    def retrieve(self, request, *args, **kwargs):
        pk = kwargs.get("pk")
        obj = self.get_queryset().filter(pk=pk).first()
        if obj:
            serializer = self.get_serializer(obj)
            return Response(serializer.data)
        legacy = self._legacy_ingredient_queryset().filter(pk=pk).first()
        if legacy:
            return Response(_legacy_ingredient_as_dish_ingredient_dict(legacy))
        return Response({"detail": "No DishIngredient matches the given query."}, status=status.HTTP_404_NOT_FOUND)

    def destroy(self, request, *args, **kwargs):
        pk = kwargs.get("pk")
        obj = self.get_queryset().filter(pk=pk).first()
        if obj:
            self.perform_destroy(obj)
            return Response(status=status.HTTP_204_NO_CONTENT)
        legacy = self._legacy_ingredient_queryset().filter(pk=pk).first()
        if legacy:
            dish = legacy.menu_item
            with transaction.atomic():
                legacy.delete()
                try:
                    recalculate_dish(dish, save=True)
                except ValueError as e:
                    raise ValidationError({"detail": str(e)})
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response({"detail": "No DishIngredient matches the given query."}, status=status.HTTP_404_NOT_FOUND)

    def update(self, request, *args, **kwargs):
        pk = kwargs.get("pk")
        if self.get_queryset().filter(pk=pk).exists():
            return super().update(request, *args, **kwargs)
        return self._legacy_update(request, pk, partial=False)

    def partial_update(self, request, *args, **kwargs):
        pk = kwargs.get("pk")
        if self.get_queryset().filter(pk=pk).exists():
            return super().partial_update(request, *args, **kwargs)
        return self._legacy_update(request, pk, partial=True)

    def _legacy_update(self, request, pk, *, partial: bool):
        legacy = self._legacy_ingredient_queryset().filter(pk=pk).first()
        if not legacy:
            return Response({"detail": "No DishIngredient matches the given query."}, status=status.HTTP_404_NOT_FOUND)
        data = dict(request.data)
        if data.get("ingredient_type") == DishIngredient.IngredientType.PREPARATION or data.get("preparation"):
            raise ValidationError({"detail": "Для старого ингредиента (Ingredient) можно указать только продукт."})
        if "quantity" in data and "amount" not in data:
            data["amount"] = data.pop("quantity")
        unit_override = data.pop("unit", None)
        data.pop("dish", None)
        data.pop("ingredient_type", None)
        data.pop("preparation", None)
        ser = IngredientInlineSerializer(
            legacy, data=data, partial=partial, context={"request": request}
        )
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            ser.save()
            if unit_override is not None:
                legacy.unit = str(unit_override)
                legacy.save(update_fields=["unit"])
            try:
                recalculate_dish(legacy.menu_item, save=True)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})
        legacy.refresh_from_db()
        return Response(_legacy_ingredient_as_dish_ingredient_dict(legacy))

    def perform_update(self, serializer):
        with transaction.atomic():
            ing: DishIngredient = serializer.save()
            try:
                recalculate_dish(ing.dish, save=True)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})

    def perform_destroy(self, instance):
        dish = instance.dish
        with transaction.atomic():
            super().perform_destroy(instance)
            try:
                recalculate_dish(dish, save=True)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})


class DishIngredientProcessingCreateView(CompanyBranchQuerysetMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Синхронизирует request.branch с ?branch= / филиалом пользователя — иначе валидатор
        # processing_type отсекает филиальные ProcessingType (см. сериализатор).
        self._active_branch()
        ser = DishIngredientProcessingCreateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            ing = ser.validated_data["ingredient"]
            kwargs = {"ingredient": ing, "cost": Decimal("0.00")}
            if ser.validated_data.get("processing_type") is not None:
                kwargs["processing_type"] = ser.validated_data["processing_type"]
            if ser.validated_data.get("preparation_processing") is not None:
                kwargs["preparation_processing"] = ser.validated_data["preparation_processing"]
            DishIngredientProcessing.objects.create(**kwargs)
            try:
                recalculate_dish(ing.dish, save=True)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})
        return Response({"detail": "ok"}, status=status.HTTP_201_CREATED)


class DishIngredientProcessingDeleteView(CompanyBranchQuerysetMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)
        obj = generics.get_object_or_404(DishIngredientProcessing.objects.select_related("ingredient__dish"), pk=pk)
        if obj.ingredient.dish.company_id != company.id:
            return Response({"detail": "Не найдено."}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            dish = obj.ingredient.dish
            obj.delete()
            try:
                recalculate_dish(dish, save=True)
            except ValueError as e:
                raise ValidationError({"detail": str(e)})
        return Response(status=status.HTTP_204_NO_CONTENT)


# ==================== Dish cost ====================
class DishCostView(CompanyBranchQuerysetMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)
        b = self._active_branch()
        dish = generics.get_object_or_404(MenuItem.objects.filter(company=company), pk=pk)
        if b is not None and dish.branch_id not in (None, b.id):
            return Response({"detail": "Не найдено."}, status=status.HTTP_404_NOT_FOUND)

        try:
            recalculate_dish(dish, save=True)
        except ValueError as e:
            raise ValidationError({"detail": str(e)})
        payload = {
            "dish_id": dish.id,
            "cost_price": dish.cost_price,
            "sale_price": dish.price,
            "margin_amount": dish.margin_amount,
            "margin_percent": dish.margin_percent,
        }
        return Response(DishCostSerializer(payload).data, status=status.HTTP_200_OK)


def _menu_items_queryset_for_user(mixin, company):
    qs = (
        MenuItem.objects
        .select_related("category", "company")
        .prefetch_related(
            "ingredients__product",
            "dish_ingredients__product",
            "dish_ingredients__preparation",
            "dish_ingredients__processings__processing_type",
            "dish_ingredients__processings__preparation_processing",
        )
        .filter(company=company)
    )
    b = mixin._active_branch()
    if b is not None:
        qs = qs.filter(Q(branch=b) | Q(branch__isnull=True))
    return qs


def _menu_item_image_url(dish, request):
    if dish.image and hasattr(dish.image, "url"):
        url = dish.image.url
        return request.build_absolute_uri(url) if request else url
    return None


def _dish_tech_card_ingredients(dish, context):
    if dish.dish_ingredients.exists():
        qs = (
            dish.dish_ingredients
            .select_related("product", "preparation")
            .prefetch_related(
                "processings__processing_type",
                "processings__preparation_processing",
            )
            .order_by("created_at")
        )
        return DishIngredientSerializer(qs, many=True, context=context).data
    legacy = dish.ingredients.select_related("product").order_by("id")
    return IngredientInlineSerializer(legacy, many=True, context=context).data


def _build_dish_tech_card_item(dish, request):
    recalculate_dish(dish, save=True)
    category = dish.category
    return {
        "id": dish.id,
        "title": dish.title,
        "image_url": _menu_item_image_url(dish, request),
        "category_title": category.title if category else "",
        "cost": {
            "cost_price": dish.cost_price,
            "sale_price": dish.price,
            "margin_amount": dish.margin_amount,
            "margin_percent": dish.margin_percent,
        },
        "ingredients": _dish_tech_card_ingredients(dish, {"request": request}),
    }


class TechCardsExportView(CompanyBranchQuerysetMixin, APIView):
    """
    Полные техкарты блюд для PDF и детального просмотра.
    POST /cafe/tech-cards/export/
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)

        ser = TechCardsExportRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        qs = _menu_items_queryset_for_user(self, company)

        search = (data.get("search") or "").strip()
        if search:
            qs = qs.filter(title__icontains=search)

        category_id = data.get("category_id")
        if category_id:
            qs = qs.filter(category_id=category_id)

        if data.get("is_all"):
            dishes = list(qs.order_by("title"))
        else:
            dish_ids = data.get("dish_ids") or []
            dishes = list(qs.filter(id__in=dish_ids).order_by("title"))
            if len(dishes) != len(set(dish_ids)):
                found = {d.id for d in dishes}
                missing = [str(i) for i in dish_ids if i not in found]
                if missing:
                    raise ValidationError(
                        {"dish_ids": f"Не найдены блюда: {', '.join(missing)}"}
                    )

        items = []
        try:
            with transaction.atomic():
                for dish in dishes:
                    items.append(_build_dish_tech_card_item(dish, request))
        except ValueError as e:
            raise ValidationError({"detail": str(e)})

        payload = {"count": len(items), "items": items}
        return Response(
            TechCardsExportResponseSerializer(payload).data,
            status=status.HTTP_200_OK,
        )


class DishCalculatePreviewView(CompanyBranchQuerysetMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)

        ser = DishCalculatePreviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        other = ser.validated_data.get("other_expenses") or Decimal("0.00")
        sale_price = ser.validated_data.get("sale_price")
        ingredients = ser.validated_data.get("ingredients") or []

        total = Decimal("0.00")
        breakdown = []
        b = self._active_branch()

        for row in ingredients:
            itype = (row.get("ingredient_type") or "").strip()
            qty = Decimal(str(row.get("quantity") or "0").replace(",", "."))
            unit = (row.get("unit") or "").strip().lower()
            pt_ids = row.get("processing_type_ids") or []
            pp_ids = row.get("preparation_processing_ids") or []

            ing = DishIngredient(
                dish=MenuItem(company=company, branch=b),
                ingredient_type=itype,
                quantity=qty,
                unit=unit,
            )
            if itype == DishIngredient.IngredientType.PRODUCT:
                pid = row.get("product")
                ing.product = Warehouse.objects.filter(company=company, id=pid).first()
            elif itype == DishIngredient.IngredientType.PREPARATION:
                prid = row.get("preparation")
                ing.preparation = Preparation.objects.filter(company=company, id=prid).first()

            # имитируем processings
            ing._prefetched_objects_cache = {}
            proc_list = []
            if itype == DishIngredient.IngredientType.PRODUCT:
                pts = list(ProcessingType.objects.filter(company=company, id__in=pt_ids))
                for pt in pts:
                    proc_list.append(DishIngredientProcessing(ingredient=ing, processing_type=pt, cost=Decimal("0.00")))
            elif itype == DishIngredient.IngredientType.PREPARATION and ing.preparation_id:
                pps = list(
                    PreparationProcessing.objects.filter(
                        id__in=pp_ids,
                        preparation_id=ing.preparation_id,
                    )
                )
                for pp in pps:
                    proc_list.append(
                        DishIngredientProcessing(
                            ingredient=ing, preparation_processing=pp, cost=Decimal("0.00")
                        )
                    )
            # подсунем их calculate_ingredient через related manager: проще пересчитать отдельно
            # считаем cost вручную аналогично сервису
            from .services.costing import _norm_unit as _nu
            from .services.costing import convert_quantity as _cq

            if itype == DishIngredient.IngredientType.PRODUCT and ing.product:
                src_unit_cost = Decimal(ing.product.unit_price or 0).quantize(Decimal("0.0001"))
                src_unit = _nu(ing.product.unit)
                qty_in_src_unit = _cq(qty, unit, src_unit)
                ingredient_cost = (src_unit_cost * qty_in_src_unit).quantize(Decimal("0.01"))
                unit_cost = src_unit_cost
            elif itype == DishIngredient.IngredientType.PREPARATION and ing.preparation:
                src_unit_cost = Decimal(ing.preparation.unit_cost or 0).quantize(Decimal("0.0001"))
                src_unit = _nu(ing.preparation.output_unit)
                qty_in_src_unit = _cq(qty, unit, src_unit)
                ingredient_cost = (src_unit_cost * qty_in_src_unit).quantize(Decimal("0.01"))
                unit_cost = src_unit_cost
            else:
                raise ValidationError({"ingredients": "Некорректный источник ингредиента."})

            processing_total = Decimal("0.00")
            for p in proc_list:
                if p.preparation_processing_id:
                    rowp = p.preparation_processing
                    if rowp.charge_type == PreparationProcessing.ChargeType.FIXED:
                        c = Decimal(rowp.cost or 0)
                    else:
                        c = Decimal(rowp.cost or 0) * qty
                else:
                    pt = p.processing_type
                    if pt.charge_type == ProcessingType.ChargeType.FIXED:
                        c = Decimal(pt.cost or 0)
                    else:
                        c = Decimal(pt.cost or 0) * qty
                processing_total += c
            processing_total = processing_total.quantize(Decimal("0.01"))
            row_total = (ingredient_cost + processing_total).quantize(Decimal("0.01"))

            total += row_total
            breakdown.append(
                {
                    "ingredient_type": itype,
                    "quantity": str(qty),
                    "unit": unit,
                    "unit_cost": str(unit_cost),
                    "ingredient_cost": str(ingredient_cost),
                    "processing_cost": str(processing_total),
                    "total_cost": str(row_total),
                }
            )

        cost_price = (total + other).quantize(Decimal("0.01"))
        if sale_price is not None:
            sp = Decimal(sale_price).quantize(Decimal("0.001"))
        else:
            sp = None
        if sp is not None:
            margin_amount = (sp - cost_price).quantize(Decimal("0.01"))
            margin_percent = (margin_amount / sp * Decimal("100")).quantize(Decimal("0.01")) if sp else Decimal("0.00")
        else:
            margin_amount = None
            margin_percent = None
        return Response(
            {
                "ingredients": breakdown,
                "other_expenses": str(other),
                "cost_price": str(cost_price),
                "sale_price": (str(sp) if sp is not None else None),
                "margin_amount": (str(margin_amount) if margin_amount is not None else None),
                "margin_percent": (str(margin_percent) if margin_percent is not None else None),
            },
            status=status.HTTP_200_OK,
        )


def _sync_table_status(table_id):
    """
    Синхронизировать статус стола по факту наличия открытых заказов.

    Правило:
      - если есть хотя бы один Order(status=OPEN) на этом столе -> BUSY
      - иначе -> FREE
    """
    if not table_id:
        return

    with transaction.atomic():
        table = Table.objects.select_for_update().get(id=table_id)
        has_open_orders = Order.objects.filter(
            table_id=table_id,
            status=Order.Status.OPEN,
        ).exists()

        new_status = Table.Status.BUSY if has_open_orders else Table.Status.FREE
        if table.status != new_status:
            table.status = new_status
            table.save(update_fields=["status"])
            send_table_status_changed_notification(table)


# ==================== Order ====================
class OrderListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = (
        Order.objects
        .select_related("table", "waiter", "company", "client")
        .prefetch_related("items__menu_item")
    )
    serializer_class = OrderSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = OrderFilter
    # Параметр ?search= с фронта (целое число стола через SearchFilter не ищем — у Table.number IntegerField).
    search_fields = [
        "client__name",
        "client__phone",
        "waiter__email",
        "waiter__first_name",
        "waiter__last_name",
    ]
    ordering_fields = ["created_at", "guests", "id"]
    
    def perform_create(self, serializer):
        super().perform_create(serializer)
        order = serializer.instance
        # Пересчитываем сумму заказа
        order.recalc_total()
        order.save(update_fields=["total_amount"])
        
        # Устанавливаем стол как занятый при создании заказа
        if order.table_id:
            _sync_table_status(order.table_id)
        
        # Отправляем WebSocket уведомление о создании заказа
        send_order_created_notification(order)


class OrderRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = (
        Order.objects
        .select_related("table", "waiter", "company", "client")
        .prefetch_related("items__menu_item")
    )
    serializer_class = OrderSerializer
    
    def perform_update(self, serializer):
        old_status = None
        old_table_id = None
        if serializer.instance:
            old_status = serializer.instance.status
            old_table_id = serializer.instance.table_id
        
        super().perform_update(serializer)
        order = serializer.instance

        # Фиксируем отмену: кто и когда отменил
        if order.status == Order.Status.CANCELLED and old_status != Order.Status.CANCELLED:
            order.canceled_at = order.canceled_at or timezone.now()
            if getattr(self.request, "user", None) and getattr(self.request.user, "is_authenticated", False):
                order.canceled_by = self.request.user
            order.save(update_fields=["canceled_at", "canceled_by", "updated_at"])

        # Пересчитываем сумму заказа при обновлении
        order.recalc_total()
        order.save(update_fields=["total_amount"])

        # Если заказ закрыт или отменен - отменяем незавершенные задачи кухни
        if order.status in [Order.Status.CLOSED, Order.Status.CANCELLED]:
            if old_status not in [Order.Status.CLOSED, Order.Status.CANCELLED]:
                unfinished_tasks = KitchenTask.objects.filter(
                    order=order,
                    status__in=[KitchenTask.Status.PENDING, KitchenTask.Status.IN_PROGRESS],
                )
                if unfinished_tasks.exists():
                    unfinished_tasks.update(status=KitchenTask.Status.CANCELLED)

        # Синхронизируем статус стола(ов) по факту открытых заказов.
        affected_table_ids = set()
        if old_table_id:
            affected_table_ids.add(old_table_id)
        if order.table_id:
            affected_table_ids.add(order.table_id)
        for table_id in affected_table_ids:
            _sync_table_status(table_id)
        
        # Отправляем WebSocket уведомление об обновлении заказа
        send_order_updated_notification(order)
    
    def perform_destroy(self, instance):
        table_id = instance.table_id
        super().perform_destroy(instance)
        
        # При удалении заказа освобождаем стол, если нет других открытых заказов на этот стол
        if table_id:
            _sync_table_status(table_id)


def _order_items_queryset_for_user(company, active_branch):
    qs = OrderItem.objects.select_related("order", "menu_item").filter(order__company=company, company=company)
    menu_branch_ok = (
        Q(line_kind=OrderItem.LineKind.SERVICE)
        | Q(menu_item__branch=active_branch)
        | Q(menu_item__branch__isnull=True)
    )
    if active_branch is not None:
        return qs.filter(Q(order__branch=active_branch) | Q(order__branch__isnull=True)).filter(menu_branch_ok)
    return qs.filter(order__branch__isnull=True).filter(
        Q(line_kind=OrderItem.LineKind.SERVICE) | Q(menu_item__branch__isnull=True)
    )


def _recalc_order_after_items_change(order_id):
    order = Order.objects.filter(pk=order_id).first()
    if not order:
        return
    order.recalc_total()
    order.save(update_fields=["total_amount", "updated_at"])
    if order.is_paid or order.status != Order.Status.OPEN:
        _cafe_archive_order_snapshot(order)
    send_order_updated_notification(order)


# ==================== OrderItem ====================
class OrderItemListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = OrderItemInlineSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["order", "menu_item"]
    ordering_fields = ["id"]

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return OrderItem.objects.none()
        return _order_items_queryset_for_user(company, self._active_branch())

    def perform_create(self, serializer):
        super().perform_create(serializer)
        _recalc_order_after_items_change(serializer.instance.order_id)


class OrderItemRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = OrderItemInlineSerializer

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return OrderItem.objects.none()
        return _order_items_queryset_for_user(company, self._active_branch())

    def perform_update(self, serializer):
        super().perform_update(serializer)
        _recalc_order_after_items_change(serializer.instance.order_id)

    def perform_destroy(self, instance):
        if not _is_owner_like(self.request.user) and (instance.order.is_paid or instance.order.status != Order.Status.OPEN):
            raise ValidationError({"detail": "Можно менять позиции только у открытого неоплаченного заказа."})
        oid = instance.order_id
        super().perform_destroy(instance)
        _recalc_order_after_items_change(oid)


def _cafe_order_item_unit_price(it: OrderItem) -> Decimal:
    if it.line_kind == OrderItem.LineKind.SERVICE:
        return it.unit_price or Decimal("0")
    if it.unit_price is not None:
        return it.unit_price
    if it.menu_item_id:
        return it.menu_item.price
    return Decimal("0")


def _cafe_clients_in_scope(company, active_branch):
    qs = CafeClient.objects.filter(company=company)
    if active_branch is not None:
        qs = qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
    return qs


def _cafe_order_checkout_payload(order: Order) -> dict:
    disc = order.discount_amount or Decimal("0")
    final_amt = (order.total_amount or Decimal("0")) - disc
    pm = order.payment_method or ""
    pm_labels = dict(Order.PaymentMethod.choices)
    payload = {
        "id": str(order.id),
        "status": order.status,
        "is_paid": order.is_paid,
        "paid_at": order.paid_at,
        "payment_method": pm,
        "payment_method_label": pm_labels.get(pm, pm),
        "total_amount": str(order.total_amount),
        "discount_amount": str(disc),
        "final_amount": str(final_amt),
        "paid_amount": str(order.paid_amount or Decimal("0")),
        "refunded_amount": str(order.refunded_amount or Decimal("0")),
        "net_paid_amount": str(order.net_paid_amount),
        "has_refunds": order.has_refunds,
        "is_fully_refunded": order.is_fully_refunded,
        "balance_due": str(order.balance_due),
        "cash_shift_id": str(order.cash_shift_id) if order.cash_shift_id else None,
    }
    if pm == Order.PaymentMethod.SPLIT:
        payload["payments"] = [
            {"method": p.payment_method, "amount": f"{p.amount:.2f}"}
            for p in order.checkout_payments.order_by("created_at")
        ]
    return payload


def _cafe_archive_order_snapshot(order: Order):
    waiter_label = ""
    if order.waiter_id:
        full = getattr(order.waiter, "get_full_name", lambda: "")() or ""
        email = getattr(order.waiter, "email", "") or ""
        waiter_label = full or email or str(order.waiter_id)

    canceled_by_label = ""
    if getattr(order, "canceled_by_id", None):
        u = getattr(order, "canceled_by", None)
        if u:
            full = getattr(u, "get_full_name", lambda: "")() or ""
            email = getattr(u, "email", "") or ""
            canceled_by_label = full or email or str(order.canceled_by_id)
        else:
            canceled_by_label = str(order.canceled_by_id)

    oh, _created = OrderHistory.objects.update_or_create(
        original_order_id=order.id,
        defaults={
            "company": order.company,
            "branch": order.branch,
            "client": order.client,
            "table": order.table,
            "table_number": (order.table.number if order.table_id else None),
            "waiter": order.waiter,
            "waiter_label": waiter_label,
            "guests": order.guests,
            "created_at": order.created_at,
            "status": order.status,
            "is_paid": order.is_paid,
            "paid_at": order.paid_at,
            "payment_method": order.payment_method,
            "total_amount": order.total_amount,
            "discount_amount": order.discount_amount,
            "paid_amount": order.paid_amount or Decimal("0"),
            "refunded_amount": getattr(order, "refunded_amount", None) or Decimal("0"),
            "canceled_at": getattr(order, "canceled_at", None),
            "canceled_by": getattr(order, "canceled_by", None),
            "canceled_by_label": canceled_by_label,
        },
    )

    OrderItemHistory.objects.filter(order_history=oh).delete()
    items = []
    for it in order.items.select_related("menu_item"):
        if it.line_kind == OrderItem.LineKind.SERVICE:
            items.append(
                OrderItemHistory(
                    order_history=oh,
                    menu_item=None,
                    line_kind=it.line_kind,
                    menu_item_title=(it.service_title or "").strip() or "Услуга",
                    menu_item_price=it.unit_price or Decimal("0"),
                    quantity=it.quantity,
                    menu_item_is_sold_by_weight=False,
                    menu_item_sale_unit="kg",
                    refunded_quantity=getattr(it, "refunded_quantity", 0) or 0,
                    is_rejected=it.is_rejected,
                    rejection_reason=it.rejection_reason or "",
                )
            )
        else:
            mi = it.menu_item
            items.append(
                OrderItemHistory(
                    order_history=oh,
                    menu_item=mi,
                    line_kind=it.line_kind,
                    menu_item_title=mi.title if mi else "",
                    menu_item_price=mi.price if mi else Decimal("0"),
                    quantity=it.quantity,
                    menu_item_is_sold_by_weight=bool(it.menu_item_is_sold_by_weight),
                    menu_item_sale_unit=(it.menu_item_sale_unit or "kg"),
                    refunded_quantity=getattr(it, "refunded_quantity", 0) or 0,
                    is_rejected=it.is_rejected,
                    rejection_reason=it.rejection_reason or "",
                )
            )
    if items:
        OrderItemHistory.objects.bulk_create(items)


class OrderPayView(CompanyBranchQuerysetMixin, APIView):
    """
    POST /cafe/orders/<uuid:pk>/pay/

    Полная оплата (как раньше):
      {"payment_method": "cash|card|transfer", "discount_amount": "0.00", "close_order": true}

    В долг (нужен клиент кафе — в заказе или client_id):
      {"payment_method": "debt", "discount_amount": "0", "close_order": true}

    Предоплата + долг (payment_method=debt):
      {"payment_method": "debt", "prepaid_amount": "500.00", "prepaid_payment_method": "cash",
       "idempotency_key": "<uuid>", ...}

    Обычная оплата не на полную сумму (остаток в долг):
      {"payment_method": "cash", "pay_now": "300.00", "idempotency_key": "<uuid>", "client_id": "..."}

    Дальнейшие взносы: POST /cafe/orders/<id>/pay-debt/

    ВАЖНО: архивируем в OrderHistory сразу (история НЕ зависит от удаления заказа).
    """
    def post(self, request, pk):
        if not _can_cafe_order_pay(request.user):
            return Response(
                {"detail": "Недостаточно прав для проведения оплаты заказа."},
                status=status.HTTP_403_FORBIDDEN,
            )
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)

        active_branch = self._active_branch()

        ser = OrderPaySerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        payment_method = ser.validated_data.get("payment_method", "cash")
        discount_amount = ser.validated_data.get("discount_amount", Decimal("0"))
        close_order = ser.validated_data.get("close_order", True)
        client_id = ser.validated_data.get("client_id")
        pay_now = ser.validated_data.get("pay_now")
        prepaid_amount = ser.validated_data.get("prepaid_amount")
        prepaid_pm = ser.validated_data.get("prepaid_payment_method")
        idem = ser.validated_data.get("idempotency_key")
        cash_shift_id = ser.validated_data.get("cash_shift_id")
        split_payments = ser.validated_data.get("payments")

        with transaction.atomic():
            locked_qs = (
                Order.objects
                .select_for_update(of=("self",))
                .select_related("table", "client", "waiter")
                .prefetch_related("items__menu_item__ingredients__product")
                .filter(company=company)
            )
            if active_branch is not None:
                locked_qs = locked_qs.filter(branch=active_branch)
            order = generics.get_object_or_404(locked_qs, pk=pk)

            if order.is_paid:
                return Response({"detail": "Заказ уже оплачен."}, status=status.HTTP_400_BAD_REQUEST)

            if order.status == Order.Status.CLOSED and not order.is_paid:
                return Response(
                    {
                        "detail": "Заказ закрыт с долгом. Дальнейшая оплата — POST "
                        f"/cafe/orders/{order.id}/pay-debt/"
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if (order.paid_amount or Decimal("0")) > 0 and not order.is_paid:
                return Response(
                    {
                        "detail": "Уже есть внесённые суммы по долгу. Используйте POST "
                        f"/cafe/orders/{order.id}/pay-debt/"
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if client_id:
                client = generics.get_object_or_404(_cafe_clients_in_scope(company, active_branch), pk=client_id)
                order.client = client

            order.recalc_total()

            if discount_amount and discount_amount > order.total_amount:
                return Response({"detail": "Скидка больше суммы заказа."}, status=status.HTTP_400_BAD_REQUEST)

            final_amt = ((order.total_amount or Decimal("0")) - (discount_amount or Decimal("0"))).quantize(
                Decimal("0.01")
            )
            if final_amt < 0:
                return Response(
                    {"detail": "Итог после скидки не может быть отрицательным."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            order.discount_amount = discount_amount or Decimal("0")

            if cash_shift_id:
                _cafe_assign_cash_shift(order, cash_shift_id, company, active_branch)

            def _record_prepay(amount: Decimal, pm: str, idempotency_key):
                if not idempotency_key:
                    raise ValidationError(
                        {"idempotency_key": "Обязателен при внесении денег в счёт долга (предоплата / pay_now)."}
                    )
                if OrderDebtPayment.objects.filter(order=order, idempotency_key=idempotency_key).exists():
                    order.refresh_from_db()
                    return True
                try:
                    with transaction.atomic():
                        OrderDebtPayment.objects.create(
                            company=order.company,
                            branch=order.branch,
                            order=order,
                            amount=amount,
                            payment_method=pm,
                            idempotency_key=idempotency_key,
                            created_by=request.user if request.user.is_authenticated else None,
                            note="",
                        )
                except IntegrityError:
                    order.refresh_from_db()
                    return True
                return False

            split_parts_to_save = None

            if payment_method == Order.PaymentMethod.SPLIT:
                parts = split_payments or []
                total_parts = sum(p["amount"] for p in parts).quantize(Decimal("0.01"))
                if total_parts != final_amt:
                    return Response(
                        {"detail": "Сумма payments должна совпадать с итогом к оплате."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                order.payment_method = Order.PaymentMethod.SPLIT
                order.paid_amount = final_amt
                order.is_paid = True
                order.paid_at = timezone.now()
                split_parts_to_save = parts
            elif payment_method == Order.PaymentMethod.DEBT:
                prepaid = (prepaid_amount if prepaid_amount is not None else Decimal("0")).quantize(Decimal("0.01"))
                if prepaid < 0 or prepaid > final_amt:
                    return Response(
                        {"detail": "Некорректная предоплата относительно итога."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if prepaid < final_amt and not order.client_id:
                    return Response(
                        {"detail": "Для долга укажите гостя (client в заказе или client_id)."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if prepaid >= final_amt:
                    # Полностью закрыто предоплатой — без строки OrderDebtPayment (как обычная оплата).
                    order.payment_method = prepaid_pm or Order.PaymentMethod.CASH
                    order.paid_amount = final_amt
                    order.is_paid = True
                    order.paid_at = timezone.now()
                else:
                    if prepaid > 0:
                        dup = _record_prepay(prepaid, prepaid_pm, idem)
                        if dup:
                            send_order_updated_notification(order)
                            return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)
                    order.payment_method = Order.PaymentMethod.DEBT
                    order.paid_amount = prepaid
                    order.is_paid = False
                    order.paid_at = None
            else:
                if pay_now is not None:
                    pn = Decimal(str(pay_now)).quantize(Decimal("0.01"))
                else:
                    pn = final_amt
                if pn < 0 or pn > final_amt:
                    return Response(
                        {"detail": "Сумма pay_now некорректна относительно итога."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if pn < final_amt:
                    if not order.client_id:
                        return Response(
                            {"detail": "Для частичной оплаты укажите гостя (client или client_id)."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    dup = _record_prepay(pn, payment_method, idem)
                    if dup:
                        send_order_updated_notification(order)
                        return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)
                    order.payment_method = Order.PaymentMethod.DEBT
                    order.paid_amount = pn
                    order.is_paid = False
                    order.paid_at = None
                else:
                    order.payment_method = payment_method
                    order.paid_amount = final_amt
                    order.is_paid = True
                    order.paid_at = timezone.now()

            if not order.stock_deducted:
                deduct_ingredients_for_order(order)
                order.stock_deducted = True

            if close_order:
                order.status = Order.Status.CLOSED

            _pay_uf = [
                "total_amount",
                "discount_amount",
                "paid_amount",
                "stock_deducted",
                "is_paid",
                "paid_at",
                "payment_method",
                "status",
                "client",
                "updated_at",
            ]
            if order.cash_shift_id:
                _pay_uf.append("cash_shift_id")
            order.save(update_fields=_pay_uf)

            if split_parts_to_save:
                OrderCheckoutPayment.objects.filter(order=order).delete()
                OrderCheckoutPayment.objects.bulk_create([
                    OrderCheckoutPayment(
                        order=order,
                        payment_method=p["method"],
                        amount=p["amount"].quantize(Decimal("0.01")),
                    )
                    for p in split_parts_to_save
                ])

            if close_order:
                unfinished_tasks = KitchenTask.objects.filter(
                    order=order,
                    status__in=[KitchenTask.Status.PENDING, KitchenTask.Status.IN_PROGRESS],
                )
                if unfinished_tasks.exists():
                    unfinished_tasks.update(status=KitchenTask.Status.CANCELLED)

            if order.table_id:
                _sync_table_status(order.table_id)

            _cafe_archive_order_snapshot(order)

        invalidate_cafe_analytics_cache(order.company_id)
        send_order_updated_notification(order)

        return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)


class OrderPayDebtView(CompanyBranchQuerysetMixin, APIView):
    """
    POST /cafe/orders/<uuid:pk>/pay-debt/
    Частичное или полное погашение долга по заказу (после первичного pay с долгом).

    Body:
      {
        "amount": "100.00",
        "payment_method": "cash|card|transfer",
        "idempotency_key": "<uuid>",
        "note": "",
        "cash_received": "100.00"   // только для cash
      }
    """

    def post(self, request, pk):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)

        active_branch = self._active_branch()
        ser = OrderPayDebtSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        amount = ser.validated_data["amount"].quantize(Decimal("0.01"))
        pm = ser.validated_data["payment_method"]
        idem = ser.validated_data["idempotency_key"]
        note = ser.validated_data.get("note") or ""
        debt_shift_id = ser.validated_data.get("cash_shift_id")

        with transaction.atomic():
            locked_qs = (
                Order.objects.select_for_update(of=("self",))
                .select_related("table", "client", "waiter")
                .prefetch_related("items__menu_item")
                .filter(company=company)
            )
            if active_branch is not None:
                locked_qs = locked_qs.filter(branch=active_branch)
            order = generics.get_object_or_404(locked_qs, pk=pk)

            if order.is_paid:
                return Response({"detail": "Заказ уже полностью оплачен."}, status=status.HTTP_400_BAD_REQUEST)

            order.recalc_total()
            final_amt = order.final_amount.quantize(Decimal("0.01"))
            paid_before = (order.paid_amount or Decimal("0")).quantize(Decimal("0.01"))
            balance = (final_amt - paid_before).quantize(Decimal("0.01"))
            if balance <= 0:
                return Response({"detail": "Нечего гасить — остаток долга ноль."}, status=status.HTTP_400_BAD_REQUEST)
            if amount > balance:
                return Response(
                    {"detail": f"Сумма больше остатка ({balance})."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if debt_shift_id and not order.cash_shift_id:
                _cafe_assign_cash_shift(order, debt_shift_id, company, active_branch)
                order.save(update_fields=["cash_shift_id", "updated_at"])

            if OrderDebtPayment.objects.filter(order=order, idempotency_key=idem).exists():
                order.refresh_from_db()
                _cafe_archive_order_snapshot(order)
                send_order_updated_notification(order)
                return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)

            try:
                with transaction.atomic():
                    OrderDebtPayment.objects.create(
                        company=order.company,
                        branch=order.branch,
                        order=order,
                        amount=amount,
                        payment_method=pm,
                        idempotency_key=idem,
                        created_by=request.user if request.user.is_authenticated else None,
                        note=note,
                    )
            except IntegrityError:
                order.refresh_from_db()
                _cafe_archive_order_snapshot(order)
                send_order_updated_notification(order)
                return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)

            new_paid = (paid_before + amount).quantize(Decimal("0.01"))
            order.paid_amount = new_paid
            if new_paid >= final_amt:
                order.is_paid = True
                order.paid_at = timezone.now()
                order.payment_method = pm
            _debt_uf = ["paid_amount", "is_paid", "paid_at", "payment_method", "updated_at"]
            if order.cash_shift_id:
                _debt_uf.append("cash_shift_id")
            order.save(update_fields=_debt_uf)

            if order.table_id:
                _sync_table_status(order.table_id)

            _cafe_archive_order_snapshot(order)

        invalidate_cafe_analytics_cache(order.company_id)
        send_order_updated_notification(order)
        return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)


class OrderClosedListView(CompanyBranchQuerysetMixin, generics.ListAPIView):
    """
    GET /cafe/orders/closed/
    Живые закрытые/отменённые/оплаченные заказы (не архив).
    """
    queryset = (
        Order.objects
        .select_related("table", "waiter", "company", "client")
        .prefetch_related("items__menu_item")
    )
    serializer_class = OrderSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["table", "waiter", "client", "guests", "created_at", "status", "is_paid"]
    search_fields = ["client__name", "client__phone"]
    ordering_fields = ["created_at", "id"]

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(Q(status__in=[Order.Status.CLOSED, Order.Status.CANCELLED]) | Q(is_paid=True))


class OrderRefundView(CompanyBranchQuerysetMixin, APIView):
    """
    POST /cafe/orders/<uuid:pk>/refund/
    Возврат денег по сумме чека (без привязки к строке). Для возврата конкретного блюда см. .../refund-item/.

    Body:
      {
        "amount": "100.00",
        "payment_method": "cash|card|transfer",
        "idempotency_key": "<uuid>",
        "note": ""
      }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)

        active_branch = self._active_branch()
        from .serializers import OrderRefundSerializer
        ser = OrderRefundSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        amount = ser.validated_data["amount"].quantize(Decimal("0.01"))
        pm = ser.validated_data["payment_method"]
        idem = ser.validated_data["idempotency_key"]
        note = ser.validated_data.get("note") or ""

        with transaction.atomic():
            locked_qs = (
                Order.objects.select_for_update(of=("self",))
                .select_related("table", "client", "waiter")
                .filter(company=company)
            )
            if active_branch is not None:
                locked_qs = locked_qs.filter(branch=active_branch)
            order = generics.get_object_or_404(locked_qs, pk=pk)

            if not order.is_paid:
                return Response({"detail": "Возврат доступен только для полностью оплаченного заказа."}, status=400)

            gross_paid = (order.paid_amount or Decimal("0")).quantize(Decimal("0.01"))
            refunded = (order.refunded_amount or Decimal("0")).quantize(Decimal("0.01"))
            refundable = (gross_paid - refunded).quantize(Decimal("0.01"))
            if refundable <= 0:
                return Response({"detail": "Нечего возвращать — сумма возврата уже равна оплате."}, status=400)
            if amount > refundable:
                return Response({"detail": f"Сумма больше доступного возврата ({refundable})."}, status=400)

            if OrderRefund.objects.filter(order=order, idempotency_key=idem).exists():
                order.refresh_from_db()
                _cafe_archive_order_snapshot(order)
                send_order_updated_notification(order)
                return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)

            try:
                OrderRefund.objects.create(
                    company=order.company,
                    branch=order.branch,
                    order=order,
                    amount=amount,
                    payment_method=pm,
                    idempotency_key=idem,
                    created_by=request.user if request.user.is_authenticated else None,
                    note=note,
                )
            except IntegrityError:
                order.refresh_from_db()
                _cafe_archive_order_snapshot(order)
                send_order_updated_notification(order)
                return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)

            order.refunded_amount = (refunded + amount).quantize(Decimal("0.01"))
            order.save(update_fields=["refunded_amount", "updated_at"])
            _cafe_archive_order_snapshot(order)

        invalidate_cafe_analytics_cache(order.company_id)
        send_order_updated_notification(order)
        return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)


class OrderItemRefundView(CompanyBranchQuerysetMixin, APIView):
    """
    POST /cafe/orders/<uuid:pk>/refund-item/
    Возврат по одной позиции заказа (например, 1 из 4 блюд).

    Body:
      {
        "order_item_id": "<uuid>",
        "quantity": 1,
        "payment_method": "cash|card|transfer",
        "idempotency_key": "<uuid>",
        "note": ""
      }
    quantity — необязательно; по умолчанию возвращается оставшееся количество по строке.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        if not _can_cafe_order_return(request.user):
            return Response(
                {"detail": "Недостаточно прав для возврата по заказу."},
                status=status.HTTP_403_FORBIDDEN,
            )
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)

        active_branch = self._active_branch()
        from .serializers import OrderItemRefundSerializer
        ser = OrderItemRefundSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        order_item_id = ser.validated_data["order_item_id"]
        qty_req = ser.validated_data.get("quantity")
        pm = ser.validated_data["payment_method"]
        idem = ser.validated_data["idempotency_key"]
        note = ser.validated_data.get("note") or ""

        with transaction.atomic():
            locked_qs = (
                Order.objects.select_for_update(of=("self",))
                .select_related("table", "client", "waiter")
                .prefetch_related("items__menu_item")
                .filter(company=company)
            )
            if active_branch is not None:
                locked_qs = locked_qs.filter(branch=active_branch)
            order = generics.get_object_or_404(locked_qs, pk=pk)

            if not order.is_paid:
                return Response({"detail": "Возврат доступен только для полностью оплаченного заказа."}, status=400)

            item = OrderItem.objects.select_for_update(of=("self",)).select_related("menu_item").filter(
                pk=order_item_id, order_id=order.id, company=company,
            ).first()
            if not item:
                return Response({"detail": "Позиция не найдена или не из этого заказа."}, status=404)

            if item.is_rejected:
                return Response({"detail": "По отказанной позиции возврат не оформляется."}, status=400)

            from .weight import quantize_quantity, validate_refund_quantity

            remaining = quantize_quantity(
                Decimal(item.quantity or 0) - Decimal(item.refunded_quantity or 0)
            )
            if remaining <= 0:
                return Response({"detail": "По этой строке уже всё возвращено."}, status=400)

            sold = bool(item.menu_item_is_sold_by_weight)
            sale_unit = (item.menu_item_sale_unit or "kg").strip().lower()
            if qty_req is None:
                qty = remaining
            else:
                from rest_framework.exceptions import ValidationError as DRFValidationError

                try:
                    qty = validate_refund_quantity(
                        qty_req,
                        is_sold_by_weight=sold,
                        sale_unit=sale_unit,
                        remaining=remaining,
                    )
                except DRFValidationError as exc:
                    return Response(exc.detail, status=400)

            unit = _cafe_order_item_unit_price(item).quantize(Decimal("0.01"))
            line_amt = (unit * Decimal(qty)).quantize(Decimal("0.01"))
            if line_amt <= 0:
                return Response({"detail": "Некорректная сумма строки для возврата."}, status=400)

            gross_paid = (order.paid_amount or Decimal("0")).quantize(Decimal("0.01"))
            refunded = (order.refunded_amount or Decimal("0")).quantize(Decimal("0.01"))
            refundable = (gross_paid - refunded).quantize(Decimal("0.01"))
            if line_amt > refundable:
                return Response(
                    {"detail": f"Сумма возврата больше доступной по чеку ({refundable})."},
                    status=400,
                )

            if OrderItemRefund.objects.filter(order_item=item, idempotency_key=idem).exists():
                order.refresh_from_db()
                _cafe_archive_order_snapshot(order)
                send_order_updated_notification(order)
                return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)

            try:
                OrderItemRefund.objects.create(
                    company=order.company,
                    branch=order.branch,
                    order=order,
                    order_item=item,
                    quantity=qty,
                    amount=line_amt,
                    payment_method=pm,
                    idempotency_key=idem,
                    created_by=request.user if request.user.is_authenticated else None,
                    note=note,
                )
            except IntegrityError:
                order.refresh_from_db()
                _cafe_archive_order_snapshot(order)
                send_order_updated_notification(order)
                return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)

            item.refunded_quantity = (item.refunded_quantity or 0) + qty
            item.save(update_fields=["refunded_quantity"])
            order.refunded_amount = (refunded + line_amt).quantize(Decimal("0.01"))
            order.save(update_fields=["refunded_amount", "updated_at"])
            _cafe_archive_order_snapshot(order)

        invalidate_cafe_analytics_cache(order.company_id)
        send_order_updated_notification(order)
        return Response(_cafe_order_checkout_payload(order), status=status.HTTP_200_OK)


# ==================== Kitchen (повар) ====================
class KitchenTaskListView(CompanyBranchQuerysetMixin, generics.ListAPIView):
    """
    Лента задач для повара:
      - по умолчанию pending + in_progress
      - ?mine=1 — только мои
      - ?status=... — конкретный статус
    """
    queryset = KitchenTask.objects.select_related('order__table', 'menu_item', 'waiter', 'cook')
    serializer_class = KitchenTaskSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    # status фильтруем вручную (поддержка списка через запятую), поэтому тут не включаем
    filterset_fields = ['cook', 'waiter', 'menu_item', 'order']
    ordering_fields = ['created_at', 'started_at', 'finished_at', 'status']

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        # Поддерживаем:
        #   - ?status=pending            (один статус)
        #   - ?status=pending,in_progress (список через запятую)
        #   - ?status=pending&status=in_progress (повторяющийся параметр)
        status_list = self.request.query_params.getlist('status')
        if len(status_list) == 1 and status_list[0] and ',' in status_list[0]:
            status_list = [s.strip() for s in status_list[0].split(',') if s.strip()]
        mine = self.request.query_params.get('mine')

        if status_list:
            if len(status_list) == 1:
                qs = qs.filter(status=status_list[0])
            else:
                qs = qs.filter(status__in=status_list)
        else:
            qs = qs.filter(status__in=[KitchenTask.Status.PENDING, KitchenTask.Status.IN_PROGRESS])

        if mine in ('1', 'true', 'True'):
            qs = qs.filter(cook=user)
        else:
            qs = qs.filter(Q(cook__isnull=True) | Q(cook=user))
        return qs


def _claim_base_q(company, active_branch):
    """Базовый Q для claim: pending, cook пустой, компания и branch."""
    base_q = Q(company=company, status=KitchenTask.Status.PENDING, cook__isnull=True)
    if active_branch is not None:
        base_q &= (Q(branch=active_branch) | Q(branch__isnull=True))
    else:
        base_q &= Q(branch__isnull=True)
    return base_q


class KitchenTaskClaimBulkView(CompanyBranchQuerysetMixin, APIView):
    """
    POST /cafe/kitchen/tasks/claim/
    Bulk: взять несколько задач в работу. Тело: {"task_ids": ["uuid", ...]}.
    Только задачи в статусе pending без назначенного повара (и по branch пользователя).
    """
    def post(self, request):
        company = self._user_company()
        if not company:
            return Response(
                {"ok": False, "error": "У пользователя не задана компания."},
                status=status.HTTP_403_FORBIDDEN,
            )
        task_ids = request.data.get("task_ids") if isinstance(request.data, dict) else None
        if not task_ids or not isinstance(task_ids, list):
            return Response(
                {"ok": False, "error": "Передайте массив task_ids."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = request.user
        active_branch = self._active_branch()
        base_q = _claim_base_q(company, active_branch)

        with transaction.atomic():
            to_claim = list(
                KitchenTask.objects.select_for_update(of=("self",)).select_related(
                    "order__table", "menu_item", "waiter", "cook"
                ).filter(base_q, pk__in=task_ids)
            )
            now = timezone.now()
            for task in to_claim:
                task.status = KitchenTask.Status.IN_PROGRESS
                task.cook = user
                task.started_at = now
                task.save(update_fields=["status", "cook", "started_at"])
            updated = [KitchenTaskSerializer(t, context={"request": request}).data for t in to_claim]
        return Response({"claimed": updated, "count": len(updated)}, status=status.HTTP_200_OK)


class KitchenTaskClaimView(CompanyBranchQuerysetMixin, APIView):
    """
    POST /cafe/kitchen/tasks/<uuid:pk>/claim/
    Берёт задачу в работу: pending -> in_progress, cook = request.user
    Учитывает branch: если у пользователя выбран филиал — только задачи этого филиала (или branch=NULL).
    """
    def post(self, request, pk):
        company = self._user_company()
        if not company:
            return Response(
                {"detail": "У пользователя не задана компания."},
                status=status.HTTP_403_FORBIDDEN,
            )
        user = request.user
        base_q = _claim_base_q(company, self._active_branch()) & Q(pk=pk)

        with transaction.atomic():
            updated = (
                KitchenTask.objects
                .select_for_update()
                .filter(base_q)
                .update(
                    status=KitchenTask.Status.IN_PROGRESS,
                    cook=user, started_at=timezone.now()
                )
            )
            if not updated:
                return Response(
                    {"detail": "Задачу уже взяли или статус не 'pending'."},
                    status=status.HTTP_409_CONFLICT
                )

        obj = KitchenTask.objects.select_related('order__table', 'menu_item', 'waiter', 'cook').get(pk=pk)
        return Response(KitchenTaskSerializer(obj, context={'request': request}).data)


def _mark_task_ready(task, request):
    """Отмечает одну задачу как готовую, создаёт уведомление и отправляет WebSocket. Вызывать внутри atomic по необходимости."""
    task.status = KitchenTask.Status.READY
    task.finished_at = timezone.now()
    task.save(update_fields=["status", "finished_at"])
    if task.waiter_id:
        table_num = task.order.table.number if task.order_id and task.order.table_id else None
        NotificationCafe.objects.create(
            company=task.company,
            branch=task.branch,
            recipient=task.waiter,
            type="kitchen_ready",
            message=f"Готово: {task.menu_item.title} (стол {table_num or '—'})",
            payload={
                "task_id": str(task.id),
                "order_id": str(task.order_id),
                "table": table_num,
                "menu_item": task.menu_item.title,
                "unit_index": task.unit_index,
            },
        )
    send_kitchen_task_ready_notification(task)
    return KitchenTaskSerializer(task, context={"request": request}).data


class KitchenTaskReadyBulkView(CompanyBranchQuerysetMixin, APIView):
    """
    POST /cafe/kitchen/tasks/ready/
    Bulk: отметить несколько задач как готовые. Тело: {"task_ids": ["uuid", ...]}.
    Только задачи в статусе in_progress и с cook = текущий пользователь.
    """
    def post(self, request):
        company = self._user_company()
        if not company:
            return Response(
                {"ok": False, "error": "У пользователя не задана компания."},
                status=status.HTTP_403_FORBIDDEN,
            )
        task_ids = request.data.get("task_ids") if isinstance(request.data, dict) else None
        if not task_ids or not isinstance(task_ids, list):
            return Response(
                {"ok": False, "error": "Передайте массив task_ids."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = request.user
        with transaction.atomic():
            tasks = list(
                KitchenTask.objects.select_for_update(of=("self",)).select_related(
                    "order__table", "menu_item", "waiter", "cook"
                ).filter(
                    pk__in=task_ids,
                    company=company,
                    cook=user,
                    status=KitchenTask.Status.IN_PROGRESS,
                )
            )
            updated = []
            for task in tasks:
                _mark_task_ready(task, request)
                updated.append(KitchenTaskSerializer(task, context={"request": request}).data)
        return Response({"updated": updated, "count": len(updated)}, status=status.HTTP_200_OK)


class KitchenTaskReadyView(CompanyBranchQuerysetMixin, APIView):
    """
    POST /cafe/kitchen/tasks/<uuid:pk>/ready/
    Отмечает задачу как готовую: in_progress -> ready, уведомляет официанта.
    """
    def post(self, request, pk):
        company = self._user_company()
        user = request.user
        with transaction.atomic():
            task = generics.get_object_or_404(
                KitchenTask.objects.select_for_update(of=("self",)).select_related(
                    "order__table", "menu_item", "waiter", "cook"
                ),
                pk=pk, company=company, cook=user, status=KitchenTask.Status.IN_PROGRESS
            )
            data = _mark_task_ready(task, request)
        return Response(data)


class KitchenTaskRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET/PATCH/DELETE /cafe/kitchen/tasks/<uuid:pk>/
    Позволяет получать, обновлять (включая статус) и удалять задачи кухни.
    """
    queryset = KitchenTask.objects.select_related('order__table', 'menu_item', 'waiter', 'cook')
    serializer_class = KitchenTaskSerializer

    def perform_update(self, serializer):
        old_status = None
        old_started_at = None
        old_finished_at = None
        if serializer.instance:
            old_status = serializer.instance.status
            old_started_at = serializer.instance.started_at
            old_finished_at = serializer.instance.finished_at
        
        # Определяем, нужно ли устанавливать started_at или finished_at
        new_status = serializer.validated_data.get('status', old_status)
        update_fields = []
        
        # Если статус меняется на IN_PROGRESS, устанавливаем started_at и cook
        if new_status == KitchenTask.Status.IN_PROGRESS and old_status != new_status:
            if not old_started_at:
                serializer.validated_data['started_at'] = timezone.now()
                update_fields.append('started_at')
            if not serializer.instance.cook_id:
                serializer.validated_data['cook'] = self.request.user
                update_fields.append('cook')
        
        # Если статус меняется на READY, устанавливаем finished_at
        if new_status == KitchenTask.Status.READY and old_status != new_status:
            if not old_finished_at:
                serializer.validated_data['finished_at'] = timezone.now()
                update_fields.append('finished_at')
        
        # Выполняем обновление
        super().perform_update(serializer)
        task = serializer.instance
        
        # Отправляем уведомление официанту при переходе в READY
        if new_status == KitchenTask.Status.READY and old_status != new_status and task.waiter_id:
            table_num = task.order.table.number if task.order_id and task.order.table_id else None
            NotificationCafe.objects.create(
                company=task.company,
                branch=task.branch,
                recipient=task.waiter,
                type='kitchen_ready',
                message=f'Готово: {task.menu_item.title} (стол {table_num or "—"})',
                payload={
                    "task_id": str(task.id),
                    "order_id": str(task.order_id),
                    "table": table_num,
                    "menu_item": task.menu_item.title,
                    "unit_index": task.unit_index,
                }
            )

        # WebSocket событие о готовности блюда (для поваров/монитора)
        if new_status == KitchenTask.Status.READY and old_status != new_status:
            send_kitchen_task_ready_notification(task)


class KitchenTaskMonitorView(CompanyBranchQuerysetMixin, generics.ListAPIView):
    """
    /cafe/kitchen/tasks/monitor/
    Видят владелец/админ/staff. Все задачи по компании.
    """
    permission_classes = [permissions.IsAuthenticated, IsCompanyOwnerOrAdmin]
    queryset = KitchenTask.objects.select_related('order__table', 'menu_item', 'waiter', 'cook')
    serializer_class = KitchenTaskSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['status', 'cook', 'waiter', 'menu_item', 'order']
    ordering_fields = ['created_at', 'started_at', 'finished_at', 'status']

    def get_queryset(self):
        qs = super().get_queryset()
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        return qs


class KitchenAnalyticsBaseView(CompanyBranchQuerysetMixin, APIView):
    """Агрегация по cook или waiter. ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD"""
    group_field = None  # 'cook' или 'waiter'

    def get(self, request):
        if not self.group_field:
            return Response({"detail": "group_field not set"}, status=500)

        qs = KitchenTask.objects.filter(company=self._user_company())
        active_branch = self._active_branch()
        if active_branch is not None:
            qs = qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
        else:
            qs = qs.filter(branch__isnull=True)

        df = request.query_params.get('date_from')
        dt = request.query_params.get('date_to')
        if df:
            qs = qs.filter(created_at__date__gte=df)
        if dt:
            qs = qs.filter(created_at__date__lte=dt)

        lead_time = ExpressionWrapper(F('finished_at') - F('started_at'), output_field=DurationField())
        data = (
            qs.values(self.group_field)
            .annotate(
                total=Count('id'),
                taken=Count('id', filter=Q(status__in=[KitchenTask.Status.IN_PROGRESS, KitchenTask.Status.READY])),
                ready=Count('id', filter=Q(status=KitchenTask.Status.READY)),
                avg_lead=Avg(lead_time, filter=Q(status=KitchenTask.Status.READY)),
            )
            .order_by('-ready', '-total')
        )

        result = []
        for row in data:
            uid = row[self.group_field]
            result.append({
                self.group_field: uid,
                "total": row["total"],
                "taken": row["taken"],
                "ready": row["ready"],
                "avg_lead_seconds": (row["avg_lead"].total_seconds() if row["avg_lead"] else None),
            })
        return Response(result)


class KitchenAnalyticsByCookView(KitchenAnalyticsBaseView):
    group_field = 'cook'


class KitchenAnalyticsByWaiterView(KitchenAnalyticsBaseView):
    group_field = 'waiter'


class NotificationListView(CompanyBranchQuerysetMixin, generics.ListAPIView):
    serializer_class = NotificationCafeSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    ordering_fields = ['created_at']

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return NotificationCafe.objects.none()
        return NotificationCafe.objects.filter(company=company, recipient=self.request.user).order_by('-created_at')


class InventorySessionListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    """
    GET  /cafe/inventory/sessions/      — список актов инвентаризации (по компании/филиалу)
    POST /cafe/inventory/sessions/      — создать акт с items
    """
    queryset = InventorySession.objects.select_related("company", "branch", "created_by").all()
    serializer_class = InventorySessionSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_confirmed", "created_at", "confirmed_at"]
    search_fields = ["comment"]
    ordering_fields = ["created_at", "confirmed_at", "id"]


class InventorySessionRetrieveView(CompanyBranchQuerysetMixin, generics.RetrieveAPIView):
    queryset = InventorySession.objects.select_related("company", "branch", "created_by").prefetch_related("items__product")
    serializer_class = InventorySessionSerializer


class InventorySessionConfirmView(CompanyBranchQuerysetMixin, generics.GenericAPIView):
    queryset = InventorySession.objects.all()
    serializer_class = InventorySessionSerializer

    def post(self, request, *args, **kwargs):
        session = self.get_object()
        if session.is_confirmed:
            return Response({"detail": "Уже подтверждено."}, status=status.HTTP_200_OK)
        session.confirm(user=request.user)
        data = self.get_serializer(session).data
        return Response(data, status=status.HTTP_200_OK)


# ==================== INVENTORY: оборудование ====================
class EquipmentListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = Equipment.objects.select_related("company", "branch")
    serializer_class = EquipmentSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["category", "condition", "is_active"]
    search_fields = ["title", "serial_number", "category", "notes"]
    ordering_fields = ["title", "condition", "is_active", "purchase_date", "id"]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        expense = equipment_on_create(equipment=serializer.instance, user=request.user)
        if expense:
            invalidate_cafe_analytics_cache(serializer.instance.company_id)
        data = attach_expense_to_response(dict(serializer.data), expense)
        return Response(data, status=status.HTTP_201_CREATED)


class EquipmentRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Equipment.objects.select_related("company", "branch")
    serializer_class = EquipmentSerializer

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        old_price = instance.price
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        expense = equipment_on_price_update(
            equipment=serializer.instance,
            old_price=old_price,
            user=request.user,
        )
        if expense:
            invalidate_cafe_analytics_cache(serializer.instance.company_id)
        data = attach_expense_to_response(dict(serializer.data), expense)
        return Response(data)


class EquipmentReceiveView(CompanyBranchQuerysetMixin, APIView):
    """POST /cafe/equipment/<uuid:pk>/receive/ — докупка оборудования с авто-расходом «Закупки»."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        company = self._user_company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)

        eq = generics.get_object_or_404(Equipment.objects.filter(company=company), pk=pk)
        active_branch = self._active_branch()
        if active_branch is not None and eq.branch_id not in (None, active_branch.id):
            return Response({"detail": "Позиция другого филиала."}, status=status.HTTP_404_NOT_FOUND)

        from .serializers import EquipmentReceiveSerializer
        ser = EquipmentReceiveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        expense = equipment_receive(
            equipment=eq,
            quantity=ser.validated_data["quantity"],
            unit_price=ser.validated_data["unit_price"],
            user=request.user,
            note=ser.validated_data.get("note") or "",
        )
        invalidate_cafe_analytics_cache(company.id)
        data = attach_expense_to_response(
            EquipmentSerializer(eq, context={"request": request}).data,
            expense,
        )
        return Response(data, status=status.HTTP_200_OK)


class EquipmentInventorySessionListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = EquipmentInventorySession.objects.select_related("company", "branch", "created_by")
    serializer_class = EquipmentInventorySessionSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_confirmed", "created_at", "confirmed_at"]
    search_fields = ["comment"]
    ordering_fields = ["created_at", "confirmed_at", "id"]


class EquipmentInventorySessionRetrieveView(CompanyBranchQuerysetMixin, generics.RetrieveAPIView):
    queryset = (
        EquipmentInventorySession.objects
        .select_related("company", "branch", "created_by")
        .prefetch_related("items__equipment")
    )
    serializer_class = EquipmentInventorySessionSerializer


class EquipmentInventorySessionConfirmView(CompanyBranchQuerysetMixin, generics.GenericAPIView):
    queryset = EquipmentInventorySession.objects.all()
    serializer_class = EquipmentInventorySessionSerializer

    def post(self, request, *args, **kwargs):
        session = self.get_object()
        if session.is_confirmed:
            return Response({"detail": "Уже подтверждено."}, status=status.HTTP_200_OK)
        session.confirm(user=request.user)
        data = self.get_serializer(session).data
        return Response(data, status=status.HTTP_200_OK)


# ==================== Kitchen ====================
class KitchenListCreateView(CompanyBranchQuerysetMixin, generics.ListCreateAPIView):
    queryset = Kitchen.objects.all()
    serializer_class = KitchenSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["title", "number"]
    search_fields = ["title"]
    ordering_fields = ["number", "title", "id"]

    def get_queryset(self):
        """
        Аналогично menu-items: при выбранном филиале показываем и кухни компании
        без филиала (branch=NULL), чтобы их можно было использовать во всех филиалах.
        """
        qs = self.queryset.all()
        company = self._user_company()
        if not company:
            return qs.none()

        qs = qs.filter(company=company)

        active_branch = self._active_branch()
        if active_branch is not None:
            qs = qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))

        return qs

    def perform_create(self, serializer):
        try:
            serializer.save()
        except IntegrityError as e:
            msg = str(e)
            if "uniq_kitchen_number_" in msg:
                raise ValidationError({"number": "Кухня с таким номером уже существует."})
            if "uniq_kitchen_title_" in msg:
                raise ValidationError({"title": "Кухня с таким названием уже существует."})
            raise


class KitchenRetrieveUpdateDestroyView(CompanyBranchQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Kitchen.objects.all()
    serializer_class = KitchenSerializer


# ==================== WebSocket уведомления ====================
def _order_ws_payload(order):
    return {
        "id": str(order.id),
        "status": order.status,
        "table_id": str(order.table_id) if order.table_id else None,
        "branch_id": str(order.branch_id) if order.branch_id else None,
        "total_amount": str(order.total_amount),
        "guests": order.guests,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }


def send_order_created_notification(order):
    """
    Отправляет WebSocket уведомление о создании заказа.
    """
    try:
        logger.debug(f"[send_order_created_notification] Starting: order_id={order.id}")
        channel_layer = get_channel_layer()
        if not channel_layer:
            logger.warning(f"[send_order_created_notification] Channel layer not configured")
            return

        company_id = str(order.company_id)
        branch_id = str(order.branch_id) if order.branch_id else None

        if branch_id:
            group_name = f"cafe_orders_{company_id}_{branch_id}"
        else:
            group_name = f"cafe_orders_{company_id}"

        logger.debug(f"[send_order_created_notification] Sending to group: {group_name}, order_id={order.id}")

        order_data = _order_ws_payload(order)

        logger.debug(f"[send_order_created_notification] Order data serialized: {len(str(order_data))} chars")

        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "order_created",
                "payload": {
                    "order": order_data,
                    "company_id": company_id,
                    "branch_id": branch_id,
                }
            }
        )
        logger.debug(f"[send_order_created_notification] Message sent to channel layer: group={group_name}")
    except Exception as e:
        logger.error(f"[send_order_created_notification] Error sending notification: {e}", exc_info=True)


def send_order_updated_notification(order):
    """
    Отправляет WebSocket уведомление об обновлении заказа.
    """
    try:
        logger.debug(f"[send_order_updated_notification] Starting: order_id={order.id}")
        channel_layer = get_channel_layer()
        if not channel_layer:
            logger.warning(f"[send_order_updated_notification] Channel layer not configured")
            return

        company_id = str(order.company_id)
        branch_id = str(order.branch_id) if order.branch_id else None

        if branch_id:
            group_name = f"cafe_orders_{company_id}_{branch_id}"
        else:
            group_name = f"cafe_orders_{company_id}"

        logger.debug(f"[send_order_updated_notification] Sending to group: {group_name}, order_id={order.id}")

        order_data = _order_ws_payload(order)

        logger.debug(f"[send_order_updated_notification] Order data serialized: {len(str(order_data))} chars")

        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "order_updated",
                "payload": {
                    "order": order_data,
                    "company_id": company_id,
                    "branch_id": branch_id,
                }
            }
        )
        logger.debug(f"[send_order_updated_notification] Message sent to channel layer: group={group_name}")
    except Exception as e:
        logger.error(f"[send_order_updated_notification] Error sending notification: {e}", exc_info=True)


def send_kitchen_task_ready_notification(task):
    """
    Отправляет WebSocket уведомление о готовности блюда (задачи кухни).
    Событие уходит:
      - в группу заказов (для официантов/зала): cafe_orders_{company_id}_{branch_id?}
      - в группу кухни (для поваров/монитора кухни): cafe_kitchen_{company_id}_{branch_id?}
    """
    try:
        logger.debug(f"[send_kitchen_task_ready_notification] Starting: task_id={task.id}")
        channel_layer = get_channel_layer()
        if not channel_layer:
            logger.warning("[send_kitchen_task_ready_notification] Channel layer not configured")
            return

        company_id = str(task.company_id)
        branch_id = str(task.branch_id) if task.branch_id else None

        # Формируем имя группы заказов
        if branch_id:
            orders_group_name = f"cafe_orders_{company_id}_{branch_id}"
        else:
            orders_group_name = f"cafe_orders_{company_id}"

        # Формируем имя группы кухни
        if branch_id:
            kitchen_group_name = f"cafe_kitchen_{company_id}_{branch_id}"
        else:
            kitchen_group_name = f"cafe_kitchen_{company_id}"

        # Сериализуем данные задачи кухни
        from .serializers import KitchenTaskSerializer
        serializer = KitchenTaskSerializer(task)
        task_data = serializer.data

        # Конвертируем UUID и Decimal в строки для msgpack сериализации
        task_data = json.loads(json.dumps(task_data, default=str))

        payload = {
            "task": task_data,
            "task_id": str(task.id),
            "order_id": str(task.order_id),
            "table": (task.order.table.number if task.order_id and task.order.table_id else None),
            "menu_item": (task.menu_item.title if task.menu_item_id else None),
            "unit_index": task.unit_index,
            "quantity": str(task.quantity),
            "menu_item_is_sold_by_weight": bool(
                getattr(task.menu_item, "is_sold_by_weight", False) if task.menu_item_id else False
            ),
            "menu_item_sale_unit": (
                (getattr(task.menu_item, "sale_unit", None) or "kg")
                if task.menu_item_id and getattr(task.menu_item, "is_sold_by_weight", False)
                else None
            ),
            "company_id": company_id,
            "branch_id": branch_id,
        }

        message = {"type": "kitchen_task_ready", "payload": payload}

        # 1) официанты/заказы
        async_to_sync(channel_layer.group_send)(orders_group_name, message)
        logger.debug(f"[send_kitchen_task_ready_notification] Message sent to channel layer: group={orders_group_name}")

        # 2) кухня/повара
        async_to_sync(channel_layer.group_send)(kitchen_group_name, message)
        logger.debug(f"[send_kitchen_task_ready_notification] Message sent to channel layer: group={kitchen_group_name}")
    except Exception as e:
        logger.error(f"[send_kitchen_task_ready_notification] Error sending notification: {e}", exc_info=True)


def send_table_created_notification(table):
    """
    Отправляет WebSocket уведомление о создании стола.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        
        company_id = str(table.company_id)
        branch_id = str(table.branch_id) if table.branch_id else None
        
        # Формируем имя группы
        if branch_id:
            group_name = f"cafe_tables_{company_id}_{branch_id}"
        else:
            group_name = f"cafe_tables_{company_id}"
        
        # Сериализуем данные стола
        from .serializers import TableSerializer
        serializer = TableSerializer(table)
        table_data = serializer.data
        
        # Конвертируем UUID и Decimal в строки для msgpack сериализации
        table_data = json.loads(json.dumps(table_data, default=str))
        
        # Отправляем уведомление в группу
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "table_created",
                "payload": {
                    "table": table_data,
                    "company_id": company_id,
                    "branch_id": branch_id,
                }
            }
        )
    except Exception as e:
        logger.error(f"[send_table_created_notification] Error: {e}", exc_info=True)


def send_table_updated_notification(table):
    """
    Отправляет WebSocket уведомление об обновлении стола.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        
        company_id = str(table.company_id)
        branch_id = str(table.branch_id) if table.branch_id else None
        
        # Формируем имя группы
        if branch_id:
            group_name = f"cafe_tables_{company_id}_{branch_id}"
        else:
            group_name = f"cafe_tables_{company_id}"
        
        # Сериализуем данные стола
        from .serializers import TableSerializer
        serializer = TableSerializer(table)
        table_data = serializer.data
        
        # Конвертируем UUID и Decimal в строки для msgpack сериализации
        table_data = json.loads(json.dumps(table_data, default=str))
        
        # Отправляем уведомление в группу
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "table_updated",
                "payload": {
                    "table": table_data,
                    "company_id": company_id,
                    "branch_id": branch_id,
                }
            }
        )
    except Exception as e:
        logger.error(f"[send_table_updated_notification] Error: {e}", exc_info=True)


def send_table_status_changed_notification(table):
    """
    Отправляет WebSocket уведомление об изменении статуса стола (FREE/BUSY).
    Это специальное уведомление для отслеживания занятости столов в реальном времени.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        
        company_id = str(table.company_id)
        branch_id = str(table.branch_id) if table.branch_id else None
        
        # Формируем имя группы для столов
        if branch_id:
            group_name = f"cafe_tables_{company_id}_{branch_id}"
        else:
            group_name = f"cafe_tables_{company_id}"
        
        # Также отправляем в группу заказов, так как изменение статуса стола связано с заказами
        if branch_id:
            orders_group_name = f"cafe_orders_{company_id}_{branch_id}"
        else:
            orders_group_name = f"cafe_orders_{company_id}"
        
        # Сериализуем данные стола
        from .serializers import TableSerializer
        serializer = TableSerializer(table)
        table_data = serializer.data
        
        # Конвертируем UUID и Decimal в строки для msgpack сериализации
        table_data = json.loads(json.dumps(table_data, default=str))
        
        # Отправляем уведомление в группу столов
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "table_status_changed",
                "payload": {
                    "table": table_data,
                    "table_id": str(table.id),
                    "table_number": table.number,
                    "status": table.status,
                    "status_display": table.get_status_display(),
                    "company_id": company_id,
                    "branch_id": branch_id,
                }
            }
        )
        
        # Отправляем уведомление в группу заказов (чтобы официанты видели изменения)
        async_to_sync(channel_layer.group_send)(
            orders_group_name,
            {
                "type": "table_status_changed",
                "payload": {
                    "table": table_data,
                    "table_id": str(table.id),
                    "table_number": table.number,
                    "status": table.status,
                    "status_display": table.get_status_display(),
                    "company_id": company_id,
                    "branch_id": branch_id,
                }
            }
        )
    except Exception as e:
        logger.error(f"[send_table_status_changed_notification] Error: {e}", exc_info=True)
