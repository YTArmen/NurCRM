from apps.main.services_debt import debt_detached_client as _debt_detached_client  # ТЗ 29.10 Б-13
from apps.integrations.idempotency import idempotent
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP, InvalidOperation
from uuid import UUID, uuid4, uuid5

from django.db import transaction, IntegrityError
from django.db.models import Sum, Count, Avg, F, Q, Prefetch, Value as V, Value, BooleanField, Exists, OuterRef, Subquery, Case, When
from django.utils.dateparse import parse_date, parse_datetime
from django.utils import timezone
import datetime as _dt
from itertools import groupby
from typing import List, Optional, Dict, Any
from operator import attrgetter
from datetime import datetime, date as _date
from django.db.models.functions import Coalesce
import logging

from rest_framework import generics, permissions, filters, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.main.models import ProductDeletion
from apps.construction.trash import TrashDestroyMixin
from apps.users.pin import pin_guard
from apps.construction import audit as admin_audit
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.parsers import JSONParser, FormParser, MultiPartParser
from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from rest_framework import serializers
from apps.users.permissions import read_only_guard
from apps.users.permissions_flags import flag_guard  # ТЗ Н-16
from .filters import TransactionRecordFilter, DebtFilter, DebtPaymentFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import DecimalField, ExpressionWrapper
from rest_framework.pagination import CursorPagination, PageNumberPagination, LimitOffsetPagination
from django.core.paginator import InvalidPage
from apps.construction.models import Cashbox


from apps.main.services.item_make_processing import (
    calc_recipe_unit_cost,
    process_raw_item_make,
    item_make_recipe_ready_filter,
    assert_item_make_recipe_ready,
)
from apps.main.services.product_list_filters import apply_product_list_filters

from apps.users.models import Branch, User

from apps.main.bracket_parser import unflatten_bracket_data
from apps.main.models import (
    Contact, Pipeline, Deal, Task, Integration, Analytics,
    Order, Product, Review, Notification, Event,
    ProductBrand, ProductCategory, Warehouse, WarehouseEvent, Client, ClientDebtBulkPayment,
    GlobalProduct, GlobalBrand, GlobalCategory, ClientDeal, Bid, SocialApplications, TransactionRecord,
    ContractorWork, DealInstallment, DealInstallmentHistory, DebtPayment, Debt, ObjectSaleItem, ObjectSale, ObjectItem, ItemMake,
    ManufactureSubreal, Acceptance, ReturnFromAgent, AgentSaleAllocation, ProductImage,
    AgentRequestCart, AgentRequestItem, ProductPackage, ProductCharacteristics, DealPayment,
    ProductRecipeItem,
    ProductAlternateBarcode,
    ProductFavorite,
    MarketSaleEmployeePayProfile,
    Sale,
    SaleItem,
    SupplierReceipt,
    SupplierReceiptItem,
    ProductExpiryBatch,
    SupplierReturn,
    SupplierReturnItem,
    MarketProductFormLayout,
    KnowledgeBaseCourse,
    FinishedToRawTransfer,
    Inventory,
    InventoryItem,
    StockShortageEvent,
    StockMovement,
    record_stock_movement,
    ProductionRecord,
    record_production,
    SupplierPurchase,
    record_supplier_purchase,
)
from apps.main.serializers import (
    ContactSerializer, PipelineSerializer, DealSerializer, TaskSerializer,
    IntegrationSerializer, AnalyticsSerializer, OrderSerializer, ProductSerializer,
    ProductListSerializer, sync_product_promotion_tiers, sync_product_alternate_barcodes,
    ReviewSerializer, NotificationSerializer, EventSerializer,
    WarehouseSerializer, WarehouseEventSerializer,
    ProductCategorySerializer, ProductBrandSerializer,
    OrderItemSerializer, ClientSerializer, ClientDealSerializer, BidSerializers, SocialApplicationsSerializers,
    TransactionRecordSerializer, ContractorWorkSerializer, DebtSerializer, DebtPaymentSerializer,
    ObjectItemSerializer, ObjectSaleSerializer, ObjectSaleItemSerializer,
    BulkIdsSerializer, ItemMakeSerializer, ItemMakeProcessSerializer,
    BidPublicCreateSerializer,
    ManufactureSubrealSerializer, AcceptanceCreateSerializer, ReturnCreateSerializer,
    BulkSubrealCreateSerializer, AcceptanceReadSerializer, ReturnApproveSerializer, ReturnRejectSerializer, ReturnReadSerializer,
    AgentProductOnHandSerializer, AgentWithProductsSerializer, GlobalProductReadSerializer,
    ProductImageSerializer,
    AgentRequestCartApproveSerializer, AgentRequestCartRejectSerializer,
    AgentRequestCartSerializer, AgentRequestCartSubmitSerializer, AgentRequestItemSerializer, DealPayInputSerializer, DealPayAnyInputSerializer, DealRefundInputSerializer,
    MarketSaleEmployeePayProfileSerializer,
    SupplierReceiptCreateSerializer,
    SupplierReceiptReadSerializer,
    SupplierReturnReadSerializer,
    SupplierReturnCreateSerializer,
    ProductPurchaseBatchSerializer,
    ProductExpiryBatchSerializer,
    PublicKnowledgeBaseCourseSerializer,
    FinishedToRawTransferSerializer,
    FinishedToRawMoveInputSerializer,
    InventoryReadSerializer,
    InventoryCreateSerializer,
    StockShortageEventReadSerializer,
    StockShortageEventCreateSerializer,
    StockMovementReadSerializer,
    ProductionRecordReadSerializer,
    SupplierPurchaseReadSerializer,
)
from django.db.models import ProtectedError
from apps.utils import product_images_prefetch, _is_owner_like
from apps.main.analytics_agent import build_agent_analytics_payload, _parse_period, _compute_agent_on_hand
from apps.main.analytics_owner_production import build_owner_analytics_payload, _dt_range
from apps.main.recommendations import parse_upsell_fields
from apps.main.services import (
    _parse_bool_like,
    _parse_date_to_aware_datetime,
    _parse_kind,
    _parse_int_nonneg,
    _parse_decimal,
    _parse_decimal_nonneg,
)
    


# ===========================
#  Company + Branch mixin (как в barber)
# ===========================
_Q2 = Decimal("0.01")
_Q4 = Decimal("0.0001")


def _to_dec(v, default=Decimal("0")):
    if v in (None, "", "null"):
        return default
    return Decimal(str(v))


def _calc_price(purchase_price: Decimal, markup_percent: Decimal) -> Decimal:
    price = purchase_price * (Decimal("1") + markup_percent / Decimal("100"))
    return price.quantize(_Q2, rounding=ROUND_HALF_UP)


def _calc_markup(purchase_price: Decimal, price: Decimal) -> Decimal:
    if purchase_price <= 0:
        return Decimal("0.00")
    mp = (price / purchase_price - Decimal("1")) * Decimal("100")
    # ВАЖНО: наценка хранится точнее (4 знака), иначе при обратном пересчёте цены
    # (purchase_price + markup_percent) будут появляться «копейки» из-за округления процента.
    return mp.quantize(_Q4, rounding=ROUND_HALF_UP)


def _parse_payment_type(raw):
    """
    payment_type закупки: cash | transfer | debt | prepayment.
    Не передан или не распознан → cash (фронт шлёт поле не везде).
    """
    v = str(raw or "").strip().lower()
    if v in SupplierPurchase.PaymentType.values:
        return v
    return SupplierPurchase.PaymentType.CASH


def _receipt_drawer_shift(request, company):
    """
    ТЗ Н-03: приход «оплачено из ящика смены» — поле `shift` (uuid открытой смены).
    Смена должна быть открыта и принадлежать кассиру; владелец и админ могут
    платить из любой смены своей компании. Без поля — None (касса расходов).
    """
    raw = request.data.get("shift") or request.data.get("shift_id")
    if not raw:
        return None
    from apps.construction.models import CashShift

    try:
        shift = CashShift.objects.select_related("cashbox").filter(company=company, id=raw).first()
    except Exception:  # не uuid
        shift = None
    if shift is None:
        raise ValidationError({"code": "shift_not_found", "detail": "Смена не найдена."})
    if shift.status != CashShift.Status.OPEN:
        raise ValidationError({"code": "shift_closed", "detail": "Смена уже закрыта."})
    user = request.user
    if shift.cashier_id != user.id and getattr(user, "role", "") not in ("owner", "admin"):
        raise PermissionDenied({"code": "shift_not_owned", "detail": "Это смена другого кассира."})
    return shift


def _truthy(raw):
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _receipt_marker(receipt_id):
    """ТЗ Н-10/Н-12: метка прихода в note сделки (без миграции: связь сделка ↔ приход)."""
    return f"[receipt:{receipt_id}]"


def _find_receipt_deal(company, supplier_id, receipt_id):
    """Сделка поставщика, созданная по приходу (по метке в note), или None."""
    return (
        ClientDeal.objects.filter(
            company=company, client_id=supplier_id, note__contains=_receipt_marker(receipt_id)
        )
        .order_by("created_at")
        .first()
    )


def _supplier_debt_total(company, supplier_id):
    """Реальный долг поставщика: остаток по сделкам kind=debt (как kpis.debt)."""
    total = Decimal("0.00")
    deals = ClientDeal.objects.filter(
        company=company, client_id=supplier_id, kind=ClientDeal.Kind.DEBT
    ).prefetch_related("installments")
    for d in deals:
        rem = d.remaining_debt
        if rem > 0:
            total += rem
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _receipt_payment_info(receipts):
    """
    ТЗ Н-10: способ оплаты прихода для списка/карточки. Поля у прихода нет (без миграции),
    поэтому выводим: сделка по метке → debt/prepayment; расход (source_id = id прихода) → cash/prepayment;
    иначе — строки SupplierPurchase того же поставщика, записанные в момент прихода.
    Возвращает {receipt_id: {payment_type, paid_amount, debt_amount, deal_id, cashbox, cashbox_name, shift}}.
    """
    from datetime import timedelta
    from apps.construction.models import CashFlow

    receipts = list(receipts)
    out = {}
    if not receipts:
        return out
    ids = [str(r.id) for r in receipts]

    def _total(r):
        t = getattr(r, "total_amount", None)
        if t is not None:
            return Decimal(str(t)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tot = Decimal("0")
        for it in r.items.all():
            tot += Decimal(str(it.qty)) * Decimal(str(it.purchase_price if it.purchase_price is not None else 0))
        return tot.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    flows = {}
    for cf in CashFlow.objects.filter(
        source_kind=CashFlow.SourceKind.PROCUREMENT_RECEIPT, source_id__in=ids, type=CashFlow.Type.EXPENSE
    ).select_related("cashbox"):
        flows.setdefault(cf.source_id, []).append(cf)

    deal_q = Q()
    for i in ids:
        deal_q |= Q(note__contains=_receipt_marker(i))
    deals = {}
    for d in ClientDeal.objects.filter(deal_q).prefetch_related("installments"):
        for i in ids:
            if _receipt_marker(i) in (d.note or "") and i not in deals:
                deals[i] = d

    lo = min(r.created_at for r in receipts)
    hi = max(r.created_at for r in receipts) + timedelta(seconds=60)
    sup_ids = {r.supplier_id for r in receipts}
    purchases = list(
        SupplierPurchase.objects.filter(
            supplier_id__in=sup_ids, purchased_at__gte=lo, purchased_at__lte=hi
        ).values("supplier_id", "purchased_at", "payment_type")
    )
    # строка закупки принадлежит последнему приходу поставщика, созданному до неё
    rcpt_times = {}
    for sid, ca in SupplierReceipt.objects.filter(
        supplier_id__in=sup_ids, created_at__gte=lo, created_at__lte=hi
    ).values_list("supplier_id", "created_at"):
        rcpt_times.setdefault(sid, []).append(ca)

    for r in receipts:
        rid = str(r.id)
        total = _total(r)
        cfs = flows.get(rid, [])
        paid_cf = sum((cf.amount for cf in cfs), Decimal("0.00"))
        deal = deals.get(rid)
        ptype = None
        if deal is not None:
            ptype = "prepayment" if paid_cf > 0 else "debt"
            debt_amount = deal.debt_amount
        else:
            for pu in purchases:
                if (
                    pu["supplier_id"] == r.supplier_id
                    and r.created_at <= pu["purchased_at"] <= r.created_at + timedelta(seconds=60)
                    and not any(r.created_at < ca <= pu["purchased_at"] for ca in rcpt_times.get(r.supplier_id, []))
                ):
                    ptype = pu["payment_type"]
                    break
            if ptype is None:
                ptype = "prepayment" if (cfs and paid_cf < total) else ("cash" if cfs else None)
            debt_amount = None
        if ptype in ("cash", "transfer"):
            paid, debt_amount = total, Decimal("0.00")
        elif ptype == "debt" and debt_amount is None:
            paid, debt_amount = Decimal("0.00"), total
        elif ptype == "prepayment":
            paid = paid_cf if cfs else None
            if debt_amount is None and paid is not None:
                debt_amount = max(Decimal("0.00"), total - paid)
        else:
            paid = Decimal("0.00") if ptype == "debt" else None
        if ptype == "debt" and deal is not None:
            paid = Decimal("0.00")
        if ptype == "prepayment" and deal is not None:
            paid = paid_cf
        cf0 = cfs[0] if cfs else None
        out[rid] = {
            "payment_type": ptype,
            "paid_amount": f"{paid:.2f}" if paid is not None else None,
            "debt_amount": f"{debt_amount:.2f}" if debt_amount is not None else None,
            "deal_id": str(deal.id) if deal is not None else None,
            "cashbox": str(cf0.cashbox_id) if cf0 and cf0.cashbox_id else None,
            "cashbox_name": cf0.cashbox.name if cf0 and cf0.cashbox_id else None,
            "shift": str(cf0.shift_id) if cf0 and cf0.shift_id else None,
        }
    return out


def _parse_recipe_input(recipe_input):
    """
    Парсит массив recipe из запроса.
    Returns (entries, error_response) — error_response = Response или None.
    """
    if not recipe_input or not isinstance(recipe_input, list):
        return [], None

    seen_ids = set()
    recipe_entries = []
    for idx, entry in enumerate(recipe_input):
        if not isinstance(entry, dict):
            return None, Response(
                {"recipe": f"Элемент #{idx}: ожидается объект."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw_id = entry.get("id")
        raw_qty = entry.get("qty_per_unit")
        if not raw_id:
            return None, Response(
                {"recipe": f"Элемент #{idx}: отсутствует id."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if raw_qty is None:
            return None, Response(
                {"recipe": f"Элемент #{idx}: отсутствует qty_per_unit."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            qty_per_unit = Decimal(str(raw_qty))
        except Exception:
            return None, Response(
                {"recipe": f"Элемент #{idx}: qty_per_unit — неверный формат числа."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if qty_per_unit <= 0:
            return None, Response(
                {"recipe": f"Элемент #{idx}: qty_per_unit должен быть > 0."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        str_id = str(raw_id)
        if str_id in seen_ids:
            return None, Response(
                {"recipe": f"Элемент #{idx}: дубликат id={raw_id}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        seen_ids.add(str_id)
        recipe_entries.append({"id": str_id, "qty_per_unit": qty_per_unit})

    return recipe_entries, None


from apps.users.permissions import read_only_guard  # noqa: E402
class PublicKnowledgeBaseMixin:
    permission_classes = [permissions.AllowAny]
    serializer_class = PublicKnowledgeBaseCourseSerializer
    # multipart нужен для загрузки файлов превью уроков
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        return (
            KnowledgeBaseCourse.objects
            .all()
            .prefetch_related("lessons")
        )

    def get_serializer(self, *args, **kwargs):
        # Фронт шлёт вложенные уроки в bracket-notation: lessons[0][title],
        # lessons[0][thumbnail] и т.д. Собираем их обратно во вложенный dict.
        if "data" in kwargs:
            kwargs["data"] = unflatten_bracket_data(kwargs["data"])
        return super().get_serializer(*args, **kwargs)


class PublicKnowledgeBaseCourseListCreateAPIView(PublicKnowledgeBaseMixin, generics.ListCreateAPIView):
    ordering = ["-created_at"]


class PublicKnowledgeBaseCourseRetrieveAPIView(PublicKnowledgeBaseMixin, generics.RetrieveUpdateAPIView):
    lookup_url_kwarg = "course_id"

class AgentCartLockMixin:
    """
    Безопасная блокировка корзины:
    1) сначала выбираем корзину с фильтрами компании/филиала/прав
    2) потом отдельно лочим её чистым SELECT ... FOR UPDATE без join'ов
    """

    def _lock_cart_for_submit(self, request, pk):
        """
        Агент сабмитит ТОЛЬКО свою корзину.
        Владелец тоже может (разрешаем, вдруг нужно).
        """
        allowed_qs = AgentRequestCart.objects.all()
        allowed_qs = self._filter_qs_company_branch(allowed_qs)

        user = request.user
        if not _is_owner_like(user):
            allowed_qs = allowed_qs.filter(agent=user)

        # шаг 1: находим корзину с фильтрами доступа
        cart = get_object_or_404(allowed_qs, pk=pk)

        # шаг 2: чистый лок без join'ов
        locked_cart = (
            AgentRequestCart.objects
            .select_related(None)        # ВАЖНО: убираем автоджойны
            .select_for_update()
            .get(pk=cart.pk)
        )
        return locked_cart

    def _lock_cart_for_owner_action(self, request, pk):
        """
        approve / reject -> только для владельца/админа.
        """
        user = request.user
        if not _is_owner_like(user):
            raise PermissionDenied("Forbidden")

        allowed_qs = AgentRequestCart.objects.all()
        allowed_qs = self._filter_qs_company_branch(allowed_qs)

        cart = get_object_or_404(allowed_qs, pk=pk)

        locked_cart = (
            AgentRequestCart.objects
            .select_related(None)        # ВАЖНО
            .select_for_update()
            .get(pk=cart.pk)
        )
        return locked_cart


class CompanyBranchRestrictedMixin:
    """
    - Фильтрует queryset по компании и (если у модели есть поле branch) по «активному филиалу».
    - На create/save подставляет company и (если у модели есть поле branch) — текущий филиал.

    Активный филиал:

        1) «жёсткий» филиал сотрудника:
            - user.primary_branch() / user.primary_branch
            - первый филиал из branch_ids
            - первая запись из user.branch_memberships / user.branches (если есть такие связи)
            - request.branch (если мидлварь уже положила)
        2) ?branch=<uuid> в запросе (если филиал принадлежит компании,
           И у пользователя нет жёстко назначенного филиала)
        3) None (нет филиала — работаем по всей компании, но только с записями без branch)

    Логика выборки:
        - если branch определён → показываем только данные этого филиала;
        - если branch = None → показываем только данные без филиала (branch IS NULL).
    """

    permission_classes = [permissions.IsAuthenticated]

    # ----- helpers -----
    def _request(self):
        return getattr(self, "request", None)

    def _user(self):
        req = self._request()
        return getattr(req, "user", None) if req else None

    def _company(self):
        """
        Компания текущего пользователя.
        Если у юзера привязана компания (owned_company / company) — возвращаем её,
        даже для суперюзера, чтобы в стандартных CRM-списках отображались данные его компании.
        Для суперюзера БЕЗ компании -> None (без ограничения по компании).

        Если у юзера нет company, но есть филиал с company — берём её.
        """
        u = self._user()
        if not u or not getattr(u, "is_authenticated", False):
            return None

        company = getattr(u, "owned_company", None) or getattr(u, "company", None)
        if company:
            return company

        if getattr(u, "is_superuser", False):
            return None

        # fallback: пробуем достать компанию из его филиала (если есть связь)
        br = getattr(u, "branch", None)
        if br is not None:
            return getattr(br, "company", None)

        return None

    def _fixed_branch_from_user(self, company) -> Optional[Branch]:
        """
        «Жёстко» назначенный филиал сотрудника (который нельзя менять через ?branch):

         - user.primary_branch() или user.primary_branch
         - user.branch (если есть такое поле)
         - первый филиал из branch_ids (как в /me)
         - первая связь из user.branches / user.branch_memberships (если такие есть)
         - request.branch (если мидлварь уже положила)
        """
        req = self._request()
        user = self._user()
        if not user or not company:
            return None

        company_id = getattr(company, "id", None)

        # 1) user.primary_branch как метод
        primary = getattr(user, "primary_branch", None)
        if callable(primary):
            try:
                val = primary()
                if val and getattr(val, "company_id", None) == company_id:
                    return val
            except Exception:
                pass

        # 1b) user.primary_branch как атрибут
        if primary and not callable(primary) and getattr(primary, "company_id", None) == company_id:
            return primary

        # 1c) user.branch (если так хранится)
        if hasattr(user, "branch"):
            b = getattr(user, "branch")
            if b and getattr(b, "company_id", None) == company_id:
                return b

        # 1d) если у пользователя есть M2M/through связи на филиалы: user.branches
        try:
            if hasattr(user, "branches"):
                qs = user.branches.all()
                if company_id:
                    qs = qs.filter(company_id=company_id)
                b = qs.first()
                if b:
                    return b
        except Exception:
            pass

        # 1e) если есть user.branch_memberships -> branch
        try:
            if hasattr(user, "branch_memberships"):
                ms = (
                    user.branch_memberships
                    .select_related("branch")
                )
                if company_id:
                    ms = ms.filter(branch__company_id=company_id)
                m = ms.first()
                if m and getattr(m, "branch", None):
                    return m.branch
        except Exception:
            pass

        # 1f) если на модели User есть поле/свойство branch_ids (как в /me)
        #     выбираем первый филиал этой компании
        branch_ids = getattr(user, "branch_ids", None)
        if branch_ids:
            try:
                b = (
                    Branch.objects
                    .filter(id__in=list(branch_ids), company_id=company_id)
                    .first()
                )
                if b:
                    return b
            except Exception:
                # на всякий случай не роняем
                pass

        # 2) request.branch как результат работы middleware
        if req and hasattr(req, "branch"):
            b = getattr(req, "branch")
            if b and getattr(b, "company_id", None) == company_id:
                return b

        return None

    def _include_global(self) -> bool:
        req = self._request()
        if not req:
            return False
        qp = getattr(req, "query_params", None) or getattr(req, "GET", {})
        return (qp.get("include_global") or "").strip().lower() in ("1", "true", "yes", "on")

    def _auto_branch(self) -> Optional[Branch]:
        """
        Активный филиал:
          1) ?branch=<uuid> в запросе:
             - проверка валидности UUID -> 400
             - проверка принадлежности компании -> 404
             - проверка прав сотрудника с branch_ids -> 403
          2) «Жёсткий» филиал сотрудника (primary / branch / branch_ids / memberships / request.branch)
          3) None (нет филиала — глобальный режим по всей компании)
        """
        req = self._request()
        user = self._user()
        if not req or not user or not getattr(user, "is_authenticated", False):
            return None

        # чтобы не дергать логику по несколько раз на один запрос
        cached = getattr(req, "_cached_auto_branch", None)
        if cached is not None:
            return cached

        company = self._company()
        company_id = getattr(company, "id", None)

        branch_id = None
        if hasattr(req, "query_params"):
            branch_id = req.query_params.get("branch")
        elif hasattr(req, "GET"):
            branch_id = req.GET.get("branch")

        if branch_id:
            if str(branch_id).strip().lower() == "main":
                is_owner = (
                    getattr(user, "is_superuser", False)
                    or bool(getattr(user, "owned_company", None))
                    or getattr(user, "is_admin", False)
                    or getattr(user, "role", None) in ("owner", "admin", "OWNER", "ADMIN", "Владелец", "Администратор")
                )
                if not is_owner:
                    allowed_branches = set()
                    branch_ids = getattr(user, "branch_ids", None)
                    if isinstance(branch_ids, (list, tuple)):
                        allowed_branches.update(str(x) for x in branch_ids)
                    if hasattr(user, "branch_memberships"):
                        allowed_branches.update(
                            str(x) for x in user.branch_memberships.values_list("branch_id", flat=True)
                        )
                    if hasattr(user, "branches"):
                        allowed_branches.update(
                            str(x) for x in user.branches.values_list("id", flat=True)
                        )
                    if getattr(user, "branch_id", None):
                        allowed_branches.add(str(user.branch_id))

                    if allowed_branches:
                        raise PermissionDenied("У вас нет доступа к главному складу.")

                setattr(req, "branch", None)
                setattr(req, "_cached_auto_branch", None)
                setattr(req, "_branch_main", True)
                return None

            try:
                UUID(str(branch_id))
            except (ValueError, TypeError, AttributeError):
                raise ValidationError({"branch": ["Некорректный UUID филиала."]})

            try:
                br = Branch.objects.get(id=branch_id, company_id=company_id)
            except Branch.DoesNotExist:
                raise NotFound({"detail": "Филиал не найден."})

            # Check employee branch access
            is_owner = (
                getattr(user, "is_superuser", False)
                or bool(getattr(user, "owned_company", None))
                or getattr(user, "is_admin", False)
                or getattr(user, "role", None) in ("owner", "admin", "OWNER", "ADMIN", "Владелец", "Администратор")
            )
            if not is_owner:
                allowed_branches = set()
                branch_ids = getattr(user, "branch_ids", None)
                if isinstance(branch_ids, (list, tuple)):
                    allowed_branches.update(str(x) for x in branch_ids)
                if hasattr(user, "branch_memberships"):
                    allowed_branches.update(
                        str(x) for x in user.branch_memberships.values_list("branch_id", flat=True)
                    )
                if hasattr(user, "branches"):
                    allowed_branches.update(
                        str(x) for x in user.branches.values_list("id", flat=True)
                    )
                if getattr(user, "branch_id", None):
                    allowed_branches.add(str(user.branch_id))

                if allowed_branches and str(br.id) not in allowed_branches:
                    raise PermissionDenied("У вас нет доступа к этому филиалу.")

            setattr(req, "branch", br)
            setattr(req, "_cached_auto_branch", br)
            return br

        # 2) если параметр не передан — ищем жёстко назначенный филиал
        fixed_branch = self._fixed_branch_from_user(company)
        if fixed_branch is not None:
            setattr(req, "branch", fixed_branch)
            setattr(req, "_cached_auto_branch", fixed_branch)
            return fixed_branch

        # 3) никакого филиала → None (работаем по компании, но без филиалов)
        setattr(req, "_cached_auto_branch", None)
        return None

    @staticmethod
    def _model_has_field(model, field_name: str) -> bool:
        try:
            return any(f.name == field_name for f in model._meta.get_fields())
        except Exception:
            return False

    def _filter_qs_company_branch(
        self,
        qs,
        company_field: Optional[str] = None,
        branch_field: Optional[str] = None,
    ):
        """
        Ограничение queryset текущей company / branch.

        - если branch передан / активен:
            * по умолчанию строгая изоляция по филиалу: branch=branch;
            * если запрошен include_global=1: Q(branch=branch) | Q(branch__isnull=True);
        - если branch is None: не фильтруем по branch (вся компания).
        """
        company = self._company()
        branch = self._auto_branch()
        model = qs.model

        # company
        if company is not None:
            if company_field:
                qs = qs.filter(**{company_field: company})
            elif self._model_has_field(model, "company"):
                qs = qs.filter(company=company)

        # branch
        if branch_field:
            if branch is not None:
                if self._include_global():
                    qs = qs.filter(
                        Q(**{branch_field: branch})
                        | Q(**{f"{branch_field}__isnull": True})
                    )
                else:
                    qs = qs.filter(**{branch_field: branch})
            # branch is None → не фильтруем по branch
        elif self._model_has_field(model, "branch"):
            if branch is not None:
                if self._include_global():
                    qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    qs = qs.filter(branch=branch)
            # branch is None → не фильтруем по branch

        return qs

    def get_queryset(self):
        assert hasattr(self, "queryset") and self.queryset is not None, (
            f"{self.__class__.__name__} must define .queryset or override get_queryset()."
        )
        return self._filter_qs_company_branch(self.queryset.all())

    def get_serializer_context(self):
        # пробрасываем request, чтобы сериализаторы могли использовать company/branch/юзера
        ctx = super().get_serializer_context() if hasattr(super(), "get_serializer_context") else {}
        ctx["request"] = self.request
        return ctx

    def _save_with_company_branch(self, serializer, **extra):
        """
        Безопасно подставляет company/branch только если такие поля есть у модели.

        ВАЖНО:
        - если у пользователя есть активный филиал → всегда жёстко проставляем его в branch;
        - если филиала нет → branch не трогаем (можно создавать как глобальные, так и по филиалам,
          если это позволено сериализатором/валидаторами).
        """
        model = serializer.Meta.model
        kwargs = dict(extra)

        company = self._company()
        if self._model_has_field(model, "company") and company is not None:
            kwargs.setdefault("company", company)

        if self._model_has_field(model, "branch"):
            branch = self._auto_branch()
            if branch is not None:
                # сотрудник с филиалом — жёстко пишем его, игнорируя поле в payload
                kwargs["branch"] = branch
            # если branch is None — НЕ подставляем, пусть решает сериализатор/валидатор

        serializer.save(**kwargs)

    def perform_create(self, serializer):
        self._save_with_company_branch(serializer)

    def perform_update(self, serializer):
        self._save_with_company_branch(serializer)


# ========= Утилиты для выборок суперпользователя в некоторых вьюхах =========
def _get_company(user):
    """
    Помощник, если нужно руками получить "компанию пользователя" вне миксина.
    Для суперпользователя возвращаем None (без ограничения по company).

    NEW: если у юзера нет company, но есть branch с company — берём её.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "is_superuser", False):
        return None

    company = getattr(user, "owned_company", None) or getattr(user, "company", None)
    if company:
        return company

    br = getattr(user, "branch", None)
    if br is not None:
        return getattr(br, "company", None)

    return None

# ========= Утилиты для выборок суперпользователя в некоторых вьюхах =========



# ===========================
#  Contacts
# ===========================
class ContactListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = ContactSerializer
    queryset = Contact.objects.select_related("company", "branch", "owner").all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["name", "email", "phone", "client_company"]
    filterset_fields = "__all__"

    def perform_create(self, serializer):
        # owner + company/branch
        self._save_with_company_branch(serializer, owner=self.request.user)


class ContactRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ContactSerializer
    queryset = Contact.objects.select_related("company", "branch", "owner").all()


# ===========================
#  Pipelines
# ===========================
class PipelineListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = PipelineSerializer
    queryset = Pipeline.objects.select_related("company", "branch", "owner").all()
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    search_fields = ["name"]
    filterset_fields = "__all__"

    def perform_create(self, serializer):
        self._save_with_company_branch(serializer, owner=self.request.user)


class PipelineRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PipelineSerializer
    queryset = Pipeline.objects.select_related("company", "branch", "owner").all()


# ===========================
#  Deals
# ===========================
class DealListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = DealSerializer
    queryset = Deal.objects.select_related("company", "branch", "pipeline", "contact", "assigned_to").all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["title", "stage"]
    filterset_fields = "__all__"


class DealRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = DealSerializer
    queryset = Deal.objects.select_related("company", "branch", "pipeline", "contact", "assigned_to").all()


# ===========================
#  Tasks
# ===========================
class TaskListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = TaskSerializer
    queryset = Task.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["title", "description"]
    filterset_fields = "__all__"


class TaskRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = TaskSerializer
    queryset = Task.objects.select_related("company", "branch", "assigned_to", "deal").all()


# ===========================
#  Integrations / Analytics
# ===========================
class IntegrationListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = IntegrationSerializer
    queryset = Integration.objects.all()
    filter_backends = [DjangoFilterBackend]
    filterset_fields = "__all__"


class IntegrationRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = IntegrationSerializer
    queryset = Integration.objects.all()


class AnalyticsListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    serializer_class = AnalyticsSerializer
    queryset = Analytics.objects.all()
    filter_backends = [DjangoFilterBackend]
    filterset_fields = "__all__"


# ===========================
#  Orders
# ===========================
class OrderListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = OrderSerializer
    queryset = Order.objects.all().prefetch_related("items__product")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["order_number", "customer_name", "department", "phone"]
    filterset_fields = "__all__"

    def perform_create(self, serializer):
        # company/branch проставит миксин
        super().perform_create(serializer)


class OrderRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = OrderSerializer
    queryset = Order.objects.all().prefetch_related("items__product")


def _annotate_product_is_favorite(qs, company_id=None):
    if company_id:
        fav_ids = list(ProductFavorite.objects.filter(company_id=company_id).values_list('product_id', flat=True))
    else:
        fav_ids = list(ProductFavorite.objects.values_list('product_id', flat=True))
    if fav_ids:
        return qs.annotate(
            is_favorite=Case(
                When(id__in=fav_ids, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
    return qs.annotate(is_favorite=Value(False, output_field=BooleanField()))


def _filter_products_company_only(view, qs):
    """
    Products in main:
    - по умолчанию отдаем каталог компании (company-level items);
    - если передан ?branch=<uuid> или у пользователя активный филиал -> фильтруем по филиалу.
    """
    company = view._company()
    if company is not None:
        qs = qs.filter(company=company)

    branch = view._auto_branch()
    req = view._request()
    qp = getattr(req, "query_params", None)
    if qp is None and req and hasattr(req, "GET"):
        qp = req.GET
    branch_param = qp.get("branch") if qp else None

    # Удалённый товар с историей продаж остаётся в базе со статусом archived —
    # в каталоге его не показываем (архив — только по ?include_archived=1 или ?status=archived).
    show_archived = bool(qp) and (
        str(qp.get("include_archived") or "").strip().lower() in ("1", "true", "yes")
        or str(qp.get("status") or "").strip().lower() == Product.Status.ARCHIVED
    )
    if not show_archived:
        qs = qs.exclude(status=Product.Status.ARCHIVED)

    if branch_param:
        if str(branch_param).strip().lower() == "main":
            qs = qs.filter(branch__isnull=True)
        elif branch is not None:
            if view._include_global():
                qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
            else:
                qs = qs.filter(branch=branch)
        else:
            qs = qs.none()
    elif branch is not None:
        user = view._user()
        is_owner = (
            getattr(user, "is_superuser", False)
            or bool(getattr(user, "owned_company", None))
            or getattr(user, "is_admin", False)
            or getattr(user, "role", None) in ("owner", "admin", "OWNER", "ADMIN", "Владелец", "Администратор")
        )
        if not is_owner:
            if view._include_global():
                qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
            else:
                qs = qs.filter(branch=branch)

    return qs


class _IgnoreFormatNegotiation(DefaultContentNegotiation):
    """
    Игнорирует ?format= в content-negotiation DRF.

    По умолчанию DRF трактует ?format=<x> как выбор рендерера и отдаёт 404, если
    рендерера с таким именем нет. У scale-export параметр format=txt|xls — свой,
    прикладной, поэтому стандартную негоциацию по нему отключаем (иначе ?format=xls
    даёт 404 ещё до вьюхи).
    """

    def select_renderer(self, request, renderers, format_suffix=None):
        return (renderers[0], renderers[0].media_type)


class WeightProductsScaleExportAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/products/scale-export/

    Выгрузка весовых товаров компании (Product.is_weight=True) в .xls под PLU-менеджер
    весов Rongta RLS1100, затем импорт через SDK/ПО весов.

    PLU (Product.plu) пишется в поля «LF код» и «Код» — тот же номер, что касса читает
    из штрихкода весов, поэтому напечатанные весами штрихкоды находятся при сканировании.

    Query-параметры (все необязательны, дефолты — как в PLU-менеджере весов):
      - product_ids=<uuid,uuid>  экспорт только выбранных товаров
      - barcode_type=5           номер шаблона штрихкода на весах
      - department=21            отдел (участвует в префиксе штрихкода)
      - shelf_life_days=15       срок годности
      - tare=0                   тара
      - label_number=0           номер этикетки
      - unit=Kg                  единица измерения
      - packing_type=Нормальный  тип упаковки
      - translit=1               транслитерировать название в латиницу (весы часто печатают только ASCII)
      - assign_plu=1             присвоить plu весовым товарам без него
    """

    permission_classes = [permissions.IsAuthenticated]
    content_negotiation_class = _IgnoreFormatNegotiation

    def get(self, request, *args, **kwargs):
        from apps.main.services.scale_export import (
            build_weight_products_scale_txt,
            build_weight_products_xls,
            XlwtNotInstalled,
        )

        company = self._company()
        if company is None:
            return Response(
                {"detail": "Пользователь не привязан к компании."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qp = request.query_params

        product_ids = None
        raw_ids = (qp.get("product_ids") or "").strip()
        if raw_ids:
            product_ids = []
            for chunk in raw_ids.split(","):
                s = chunk.strip()
                if not s:
                    continue
                try:
                    product_ids.append(UUID(s))
                except (ValueError, TypeError):
                    raise ValidationError({"product_ids": [f"Неверный UUID: {s}"]})

        def _int_param(name, default, lo, hi):
            raw = (qp.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValidationError({name: ["Ожидается целое число."]})
            if not (lo <= value <= hi):
                raise ValidationError({name: [f"Допустимо от {lo} до {hi}."]})
            return value

        # Рабочий формат весов (UTF-16 текст, имя .xls) — под всеми привычными алиасами,
        # чтобы фронт с ?format=xls/txp/txt всё равно получал именно его.
        # Настоящий бинарный Excel (для просмотра человеком) — только под ?format=excel.
        fmt = (qp.get("format") or "txt").strip().lower()
        if fmt in ("txt", "txp", "xls", "rls"):
            fmt = "txt"
        elif fmt in ("excel", "xlsx"):
            fmt = "excel"
        else:
            raise ValidationError({"format": ["Допустимо: txt (для весов), excel (обычный Excel)."]})

        barcode_type = _int_param("barcode_type", 5, 0, 99)
        department = _int_param("department", 21, 0, 99)
        shelf_life_days = _int_param("shelf_life_days", 15, 0, 100000)
        unit_code = _int_param("unit_code", 4, 0, 99)
        translit_name = (qp.get("translit") or "1").strip().lower() not in ("0", "false", "no", "off")
        assign_plu = (qp.get("assign_plu") or "1").strip().lower() not in ("0", "false", "no", "off")

        try:
            if fmt == "txt":
                # Рабочий формат импорта PLU-менеджера весов Rongta RLS:
                # UTF-16 LE, TAB, CRLF, с заголовком. Файл именуется .xls (как экспорт ПО).
                content, count = build_weight_products_scale_txt(
                    company,
                    product_ids=product_ids,
                    barcode_type=barcode_type,
                    unit_code=unit_code,
                    department=department,
                    shelf_life_days=shelf_life_days,
                    translit_name=translit_name,
                    assign_plu=assign_plu,
                )
                content_type = "application/vnd.ms-excel"
                filename = "scale_weight_products.xls"
            else:  # excel — настоящий бинарный Excel, для просмотра, НЕ для весов
                tare = _int_param("tare", 0, 0, 100000)
                label_number = _int_param("label_number", 0, 0, 100000)
                unit = (qp.get("unit") or "Kg").strip() or "Kg"
                packing_type = (qp.get("packing_type") or "Нормальный").strip() or "Нормальный"
                include_header = (qp.get("header") or "1").strip().lower() not in ("0", "false", "no", "off")
                content, count = build_weight_products_xls(
                    company,
                    product_ids=product_ids,
                    barcode_type=barcode_type,
                    department=department,
                    shelf_life_days=shelf_life_days,
                    tare=tare,
                    label_number=label_number,
                    unit=unit,
                    packing_type=packing_type,
                    translit_name=translit_name,
                    assign_plu=assign_plu,
                    include_header=include_header,
                )
                content_type = "application/vnd.ms-excel"
                filename = "scale_weight_products_view.xls"
        except XlwtNotInstalled as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if count == 0:
            return Response(
                {"detail": "У компании нет весовых товаров (Product.is_weight=True) для выгрузки."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Scale-Products-Count"] = str(count)
        return response


# ===========================
#  Product create by barcode (ручной view)
# ===========================
def _barcode_lookup_variants(barcode: str):
    """Совместимые варианты UPC/EAN для поиска: 12/13 цифр, без / с ведущим нулём."""
    raw = (barcode or "").strip()
    if not raw:
        return []

    seen = set()
    variants = []

    def add(value):
        if value is None:
            return
        value = str(value).strip()
        if not value or value in seen:
            return
        seen.add(value)
        variants.append(value)

    add(raw)
    if raw.isdigit():
        add(raw.lstrip("0") or "0")
        add(raw.zfill(13))
        if len(raw) == 13 and raw.startswith("0"):
            add(raw[1:])
        if len(raw) == 12:
            add(raw.rjust(13, "0"))
            add(raw.zfill(13))
            if raw.startswith("0"):
                add(raw[1:])
    return variants


class ProductBarcodeAwareSearchFilter(filters.SearchFilter):
    """Разделяет точное сканирование ШК и обычный текстовый поиск.

    Если строка выглядит как цельный штрихкод (только цифры, длина ≥ 8),
    ищем по всем совместимым вариантам UPC/EAN: без / с ведущим нулём.
    """

    def filter_queryset(self, request, queryset, view):
        term = (request.query_params.get(self.search_param) or "").strip()
        if term and term.isdigit() and len(term) >= 8:
            variants = _barcode_lookup_variants(term)
            if variants:
                q = Q(barcode__in=variants) | Q(alternate_barcodes__barcode__in=variants)
                return queryset.filter(q).distinct()
        return super().filter_queryset(request, queryset, view)

    def get_search_fields(self, view, request):
        term = (request.query_params.get(self.search_param) or "").strip()
        if term.isdigit() and len(term) >= 8:
            return ["=barcode", "=alternate_barcodes__barcode"]
        return getattr(view, "search_fields", None)


def _cashflows_by_product_map(products):
    """
    Движения кассы по всем товарам страницы одним запросом.

    ProductSerializer.get_cashflows иначе делает запрос на каждый товар —
    на странице в 100 позиций это ~100 запросов и самая дорогая часть ответа.
    """
    from apps.construction.models import CashFlow
    from apps.construction.auto_cashflow import serialize_auto_cashflows

    ids = [str(p.id) for p in products]
    if not ids:
        return {}
    company_ids = {p.company_id for p in products if p.company_id}

    flows = CashFlow.objects.filter(source_id__in=ids)
    if company_ids:
        flows = flows.filter(company_id__in=company_ids)

    grouped = {}
    for cf in flows.select_related("cashbox", "cashbox__branch"):
        grouped.setdefault(cf.source_id, []).append(cf)
    return {pid: serialize_auto_cashflows(items) for pid, items in grouped.items()}


class ProductListPageNumberPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 100


class ProductListView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    serializer_class = ProductSerializer
    pagination_class = ProductListPageNumberPagination
    filter_backends = [ProductBarcodeAwareSearchFilter, filters.OrderingFilter]
    search_fields = ["name", "barcode", "article", "alternate_barcodes__barcode", "alternate_barcodes__name",
                     "attr_index__value"]  # ТЗ 26.10 Б-57: поиск и по значениям атрибутов
    ordering_fields = ["created_at", "updated_at", "price", "quantity"]
    # По умолчанию — по монотонному seq: детерминированный порядок «сначала новые»
    # без переупорядочивания строк с одинаковым created_at (fallback ниже — тоже -seq).
    ordering = ["-seq"]

    def get_queryset(self):
        qs = (
            Product.objects
            .select_related(
                "company",
                "branch",
                "brand",
                "category",
                "client",
                "created_by",
                "characteristics",  # OneToOne
            )
            .prefetch_related(
                "item_make",
                "packages",
                "recipe_items__item_make",
                # Раньше сериализовались на каждый товар без prefetch → N+1
                # (по ~100 доп. запросов на страницу): промо-ступени и поставщики.
                "promotion_tiers",
                Prefetch("suppliers", queryset=Client.objects.only("id", "full_name")),
                # Те же ~100 лишних запросов на страницу давали доп. штрихкоды и
                # партии срока годности — теперь грузятся одним запросом каждый.
                "alternate_barcodes",
                "sale_units",  # ТЗ 26.10 Б-53
                "expiry_batches",
                product_images_prefetch,
            )
        )
        if self._include_variants():
            qs = qs.prefetch_related("variants")
        qs = _filter_products_company_only(self, qs)
        from apps.main.tz26.hw_catalog.services import apply_catalog_filters  # ТЗ 26.10 Б-57/58: attr.*, location_prefix
        qs = apply_catalog_filters(self, qs)
        since = self._updated_since()
        if since is not None:
            qs = qs.filter(updated_at__gte=since)
        return _annotate_product_is_favorite(qs)

    def _include_variants(self) -> bool:
        """ТЗ Н-33: ?include=variants — в каждый товар добавляется массив variants (размеры/цвета)."""
        raw = self.request.query_params.get("include") or ""
        return "variants" in [x.strip().lower() for x in raw.split(",")]

    def _updated_since(self):
        """BE2-19: ?updated_since=<ISO-время> — только товары, изменённые с этого момента."""
        if not hasattr(self, "_since"):
            qp = self.request.query_params
            # ТЗ-28 §2.1: `updated_after` — то же, что updated_since; принимает ISO 8601
            # или целое — миллисекунды с эпохи (server_time из прошлого ответа или bootstrap).
            raw = (qp.get("updated_since") or qp.get("updated_after") or "").strip()
            self._since = None
            if raw:
                if raw.isdigit():
                    try:
                        dt = _dt.datetime.fromtimestamp(int(raw) / 1000, tz=_dt.timezone.utc)
                    except (OverflowError, OSError, ValueError):
                        raise ValidationError({"updated_since": "Некорректное время (мс с эпохи)."})
                else:
                    dt = parse_datetime(raw.replace(" ", "+"))
                    if dt is None:
                        raise ValidationError({"updated_since": "Ожидается дата-время ISO 8601."})
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt)
                self._since = dt
        return self._since

    def list(self, request, *args, **kwargs):
        # server_time берём до выборки: изменения во время запроса придут в следующий раз.
        server_time = timezone.now()
        response = super().list(request, *args, **kwargs)
        since = self._updated_since()
        if since is not None and isinstance(response.data, dict):
            company_id = getattr(request.user, "company_id", None)
            response.data["deleted"] = [
                str(pid) for pid in ProductDeletion.objects.filter(
                    company_id=company_id, deleted_at__gte=since
                ).values_list("product_id", flat=True).distinct()
            ]
            response.data["server_time"] = server_time.isoformat()
            # ТЗ-28 §2.1: курсор для следующего запроса (мс с эпохи) и версия каталога
            response.data["server_time_ms"] = int(server_time.timestamp() * 1000)
            try:
                from apps.main.price_check import catalog_version as _catalog_version
                response.data["catalog_version"] = _catalog_version(
                    _filter_products_company_only(self, Product.objects.all())
                )["version"]
            except Exception:  # noqa: BLE001 — версия вспомогательная, список важнее
                pass
        return response

    def paginate_queryset(self, queryset):
        page = super().paginate_queryset(queryset)
        self._page_for_cashflows = page
        return page

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self._include_variants():
            ctx["include_variants"] = True
        page = getattr(self, "_page_for_cashflows", None)
        if page:
            ctx["cashflows_by_product"] = _cashflows_by_product_map(page)
        return ctx

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)

        qp = self.request.query_params
        suppliers_csv = (qp.get("suppliers") or "").strip()
        supplier_one = (qp.get("supplier") or "").strip()
        supplier_ids = (
            [x.strip() for x in suppliers_csv.split(",") if x.strip()]
            if suppliers_csv
            else ([supplier_one] if supplier_one else [])
        )
        if supplier_ids:
            parsed_ids = []
            for s in supplier_ids:
                try:
                    parsed_ids.append(UUID(str(s)))
                except (ValueError, TypeError, AttributeError):
                    continue
            if not parsed_ids:
                qs = qs.none()
            else:
                company = self._company()
                if company is None:
                    qs = qs.none()
                else:
                    supplier_qs = Client.objects.filter(
                        company=company,
                        type=Client.StatusClient.SUPPLIERS,
                        id__in=parsed_ids,
                    )
                    allowed = list(supplier_qs.values_list("id", flat=True))
                    qs = qs.filter(client_id__in=allowed) if allowed else qs.none()

        hk = (qp.get("hotkey_group") or qp.get("group") or "").strip().upper()
        if hk and any(hk == c[0] for c in Product.HotkeyGroup.choices):
            qs = qs.filter(hotkey_group=hk)

        qs = apply_product_list_filters(qs, qp)

        # Всегда: избранные сверху, затем «сначала новые».
        # Явно фиксируем -created_at как fallback: если ordering-фильтр не проставил
        # сортировку (query.order_by пуст), порядок «новые первыми» не должен теряться.
        current = [o for o in (list(qs.query.order_by) or []) if "is_favorite" not in o]
        if not current:
            current = [F("seq").desc(nulls_last=True), "-created_at", "-id"]
        else:
            normalized_current = []
            for o in current:
                if o == "-seq":
                    normalized_current.extend([F("seq").desc(nulls_last=True), "-created_at", "-id"])
                elif o == "seq":
                    normalized_current.extend([F("seq").asc(nulls_last=True), "created_at", "id"])
                else:
                    normalized_current.append(o)
            current = normalized_current
        if any(
            isinstance(o, str) and o.lstrip("-") == "quantity"
            for o in current
        ):
            # Сортировка по остатку (блок «Заканчивается»): избранные сверху не нужны —
            # они сломали бы «N товаров с самым малым остатком»; seq — стабильные страницы.
            if not any(
                (isinstance(o, str) and o.lstrip("-") == "seq")
                or (hasattr(o, "expression") and getattr(o.expression, "name", None) == "seq")
                for o in current
            ):
                current.append("seq")
            return qs.order_by(*current)
        if any(
            isinstance(o, str) and o.lstrip("-") == "updated_at"
            for o in current
        ):
            # Б-46 (27.10.2026): ?ordering=-updated_at&page_size=1 — «версия каталога» кассы.
            # Избранные сверху ломали её: первым всегда шёл один и тот же избранный товар.
            return qs.order_by(*current, "-id")
        qs = qs.order_by("-is_favorite", *current)
        return qs


class CompactProductCursorPagination(CursorPagination):
    page_size = 30
    # Курсор по монотонному уникальному seq (не по created_at!): created_at не
    # уникален, и при массовом создании товары с одинаковым временем проскакивали
    # мимо границы курсора и «исчезали» из бесконечного скролла.
    ordering = "-seq"


class ProductCompactListView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """Компактный список товаров для бесконечного скролла — лёгкий сериализатор + курсорная пагинация."""
    serializer_class = ProductListSerializer
    pagination_class = CompactProductCursorPagination
    filter_backends = [ProductBarcodeAwareSearchFilter, filters.OrderingFilter]
    search_fields = ["name", "barcode", "alternate_barcodes__barcode", "alternate_barcodes__name"]
    ordering_fields = ["created_at", "updated_at", "price"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = (
            Product.objects
            .select_related("brand", "category")  # Оптимизация: загружаем brand и category одним запросом (на случай будущего использования)
            .only(
                "id",
                "kind",
                "name",
                "price",
                "wholesale_price",
                "quantity",
                "brand_id",
                "category_id",
                "code",
                "article",
                "company_id",
                "hotkey_group",
                "upsell_priority",
                "upsell_excluded",
                "expiration_date",
                "seq",  # нужен курсорной пагинации (ordering="-seq")
            )
            .prefetch_related(
                Prefetch(
                    "images",
                    queryset=ProductImage.objects.filter(is_primary=True).only("id", "image", "is_primary"),
                ),
            )
        )
        qs = _filter_products_company_only(self, qs)
        return _annotate_product_is_favorite(qs)

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        qp = self.request.query_params
        hk = (qp.get("hotkey_group") or qp.get("group") or "").strip().upper()
        if hk and any(hk == c[0] for c in Product.HotkeyGroup.choices):
            qs = qs.filter(hotkey_group=hk)
        current = list(qs.query.order_by) or []
        if not any("is_favorite" in o for o in current):
            qs = qs.order_by("-is_favorite", *current)
        return qs


# ТЗ Н-16: создание товара со сканера кассиру оставлено (решение Умара 25.10); правка и удаление — с правом «Товары».
@read_only_guard("can_edit_products")
class ProductCreateByBarcodeAPIView(CompanyBranchRestrictedMixin, generics.CreateAPIView):
    """
    Создание товара только по штрих-коду (если найден в глобальной базе).
    """
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        company = self._company()
        branch = None
        data = request.data

        barcode = (data.get("barcode") or "").strip()
        description = (data.get("description") or "").strip()
        article = (data.get("article") or "").strip()
        name = (data.get("name") or "").strip()

        if not barcode:
            return Response({"barcode": "Укажите штрих-код."}, status=status.HTTP_400_BAD_REQUEST)

        barcode_variants = _barcode_lookup_variants(barcode)

        # Дубликат внутри компании
        if Product.objects.filter(company=company).filter(Q(barcode__in=barcode_variants) | Q(alternate_barcodes__barcode__in=barcode_variants)).exists():
            return Response(
                {"barcode": "В вашей компании уже есть товар с таким штрих-кодом."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if ProductAlternateBarcode.objects.filter(company=company, barcode__in=barcode_variants).exists():
            return Response(
                {"barcode": "Этот штрих-код уже занят как дополнительный у другого товара."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gp = (
            GlobalProduct.objects
            .select_related("brand", "category")
            .filter(barcode__in=barcode_variants)
            .first()
        )
        if not gp:
            return Response(
                {"barcode": "Товар с таким штрих-кодом не найден в глобальной базе. Заполните карточку вручную."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # kind
        kind_value = _parse_kind(data.get("kind"), Product)

        # decimals
        try:
            purchase_price = _parse_decimal(data.get("purchase_price", 0), "purchase_price")
            markup_percent = _parse_decimal(data.get("markup_percent", 0), "markup_percent")
            discount_percent = _parse_decimal(data.get("discount_percent", 0), "discount_percent")
            wholesale_price = _parse_decimal(data.get("wholesale_price", 0), "wholesale_price")
        except ValueError as e:
            return Response({str(e): "Неверный формат числа."}, status=status.HTTP_400_BAD_REQUEST)

        # ====== FIX: двусторонняя логика price <-> markup_percent ======
        price_raw = data.get("price", None)

        # если прислали price — считаем markup_percent
        if price_raw not in (None, ""):
            try:
                price = _to_dec(price_raw)
            except Exception:
                return Response({"price": "Неверный формат цены продажи."}, status=status.HTTP_400_BAD_REQUEST)
            markup_percent = _calc_markup(purchase_price, price)
        else:
            # иначе считаем price из markup_percent
            price = _calc_price(purchase_price, markup_percent)

        # quantity (дробный остаток, до 3 знаков: 10.200)
        try:
            quantity = _parse_decimal_nonneg(data.get("quantity", 0), "quantity")
        except ValueError:
            return Response({"quantity": "Неверное количество."}, status=status.HTTP_400_BAD_REQUEST)

        # date -> aware datetime
        raw_date = data.get("date")
        try:
            date_value = _parse_date_to_aware_datetime(raw_date) if raw_date else timezone.now()
        except ValueError:
            return Response(
                {"date": "Неверный формат даты. Используйте YYYY-MM-DD или ISO datetime."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # unit/is_weight/country/expiration
        unit = (data.get("unit") or "шт.").strip()
        is_weight = _parse_bool_like(data.get("is_weight"))
        country = (data.get("country") or "").strip()

        expiration_raw = data.get("expiration_date")
        expiration_date = None
        if expiration_raw not in (None, ""):
            expiration_date = parse_date(str(expiration_raw))
            if not expiration_date:
                return Response(
                    {"expiration_date": "Неверный формат. Используйте YYYY-MM-DD."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # локальные справочники
        brand = ProductBrand.objects.get_or_create(company=company, name=gp.brand.name)[0] if gp.brand else None
        category = ProductCategory.objects.get_or_create(company=company, name=gp.category.name)[0] if gp.category else None

        # packages_input
        packages_input = data.get("packages_input") or data.get("packages")
        if not isinstance(packages_input, list):
            packages_input = []

        # create product
        product = Product.objects.create(
            company=company,
            branch=branch,
            kind=kind_value,

            name=name or gp.name,
            barcode=gp.barcode,
            brand=brand,
            category=category,

            article=article,
            description=description,

            unit=unit,
            is_weight=is_weight,

            purchase_price=purchase_price,
            markup_percent=markup_percent,
            price=price,
            wholesale_price=wholesale_price,
            discount_percent=discount_percent,

            quantity=quantity,
            country=country,
            expiration_date=expiration_date,

            date=date_value,
            created_by=request.user,
            stock=_parse_bool_like(data.get("stock", False)),
            **parse_upsell_fields(data),
        )

        from apps.main.tz26.hw_catalog.services import apply_request_data  # ТЗ 26.10 Б-57/58/59
        apply_request_data(self, request, data, product)

        # characteristics
        chars_data = data.get("characteristics")
        if isinstance(chars_data, dict):
            ProductCharacteristics.objects.update_or_create(
                product=product,
                defaults={
                    "company": company,
                    "branch": branch,
                    "height_cm": chars_data.get("height_cm") or None,
                    "width_cm": chars_data.get("width_cm") or None,
                    "depth_cm": chars_data.get("depth_cm") or None,
                    "factual_weight_kg": chars_data.get("factual_weight_kg") or None,
                    "description": chars_data.get("description") or "",
                },
            )

        # packages bulk_create
        packages_to_create = []
        for pkg in packages_input:
            if not isinstance(pkg, dict):
                continue
            name = (pkg.get("name") or "").strip()
            if not name:
                continue
            try:
                qip = int(pkg.get("quantity_in_package"))
            except (TypeError, ValueError):
                continue
            unit_pkg = (pkg.get("unit") or "").strip()
            piece_raw = pkg.get("piece_unit_price")
            if piece_raw in (None, "", "null"):
                return Response(
                    {
                        "packages_input": "Для каждой упаковки укажите piece_unit_price (цена за штуку при поштучной продаже).",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                piece_unit_price = Decimal(str(piece_raw))
            except Exception:
                return Response(
                    {"packages_input": "Неверный формат piece_unit_price в упаковке."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if piece_unit_price < 0:
                return Response(
                    {"packages_input": "piece_unit_price не может быть отрицательной."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            piece_unit_price = piece_unit_price.quantize(_Q2, rounding=ROUND_HALF_UP)

            packages_to_create.append(
                ProductPackage(
                    product=product,
                    company=company,
                    branch=branch,
                    name=name,
                    quantity_in_package=qip,
                    unit=unit_pkg,
                    piece_unit_price=piece_unit_price,
                )
            )

        if packages_to_create:
            ProductPackage.objects.bulk_create(packages_to_create)

        raw_promo = data.get("promotion_rules_input")
        if raw_promo is None:
            raw_promo = data.get("promotion_rules")
        try:
            sync_product_promotion_tiers(
                product,
                raw_promo,
                stock_enabled=bool(product.stock),
                partial=False,
            )
        except serializers.ValidationError as ve:
            return Response(
                ve.detail if isinstance(ve.detail, dict) else {"detail": ve.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if "alternate_barcodes" in data:
            raw_alts = data.get("alternate_barcodes")
            try:
                sync_product_alternate_barcodes(product, raw_alts)
            except serializers.ValidationError as ve:
                return Response(
                    ve.detail if isinstance(ve.detail, dict) else {"detail": ve.detail},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        from apps.main.tz26.hw_units.units import apply_product_extras  # ТЗ 26.10 Б-52, Б-53

        apply_product_extras(product, data)

        product = (
            _annotate_product_is_favorite(
                Product.objects.filter(pk=product.pk)
                .select_related("company", "branch", "brand", "category", "client", "created_by", "characteristics")
                .prefetch_related(
                    "item_make",
                    "packages",
                    "recipe_items__item_make",
                    "promotion_tiers",
                    "alternate_barcodes",
                    "sale_units",  # ТЗ 26.10 Б-53
                    product_images_prefetch,
                )
            )
            .get()
        )

        # auto-cashflow for warehouse purchase
        cashbox_id = data.get("cashbox_id")
        cashflow_amount_raw = data.get("cashflow_amount")
        cashflow_type = (data.get("cashflow_type") or "expense").strip().lower()

        from apps.construction.auto_cashflow import create_auto_cashflow
        from apps.construction.models import CashFlow, CashShift

        purchase_amt = None
        if cashflow_amount_raw not in (None, ""):
            try:
                purchase_amt = Decimal(str(cashflow_amount_raw))
            except Exception:
                purchase_amt = None
        elif data.get("payment_type") != "debt":
            if product.purchase_price and product.quantity and product.purchase_price > 0 and product.quantity > 0:
                purchase_amt = (Decimal(str(product.purchase_price)) * Decimal(str(product.quantity))).quantize(Decimal("0.01"))

        if purchase_amt and purchase_amt > 0:
            create_auto_cashflow(
                company=company,
                branch=branch,
                cashbox_id=cashbox_id,
                cashbox_role=data.get("cashbox_role") or "expense_variable",
                user=request.user,
                shift=None,
                type=CashFlow.Type.EXPENSE if cashflow_type == "expense" else CashFlow.Type.INCOME,
                amount=purchase_amt,
                source_kind=CashFlow.SourceKind.WAREHOUSE_PURCHASE,
                source_id=str(product.id),
                name=f"Закупка товара: {product.name}",
                source_business_operation_id="Склад",
                affects_shift_drawer=False,
            )

        ser = self.get_serializer(product, context=self.get_serializer_context())
        return Response(ser.data, status=status.HTTP_201_CREATED)


class ProductFavoriteAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/products/<product_id>/favorite/
    body: { "is_favorite": true|false }  (если не передали — переключает)

    Избранное общее на компанию товара (все сотрудники видят одно и то же).
    """

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(
            _filter_products_company_only(self, Product.objects.all()),
            id=product_id,
        )

        req_company = self._company()
        if req_company is not None and req_company.id != product.company_id:
            raise PermissionDenied("Товар не принадлежит вашей компании.")

        fav_company = product.company

        raw = request.data.get("is_favorite", None) if isinstance(request.data, dict) else None
        if raw is None:
            exists = ProductFavorite.objects.filter(company=fav_company, product=product).exists()
            if exists:
                ProductFavorite.objects.filter(company=fav_company, product=product).delete()
                return Response({"product_id": str(product.id), "is_favorite": False}, status=status.HTTP_200_OK)
            ProductFavorite.objects.create(company=fav_company, product=product)
            return Response({"product_id": str(product.id), "is_favorite": True}, status=status.HTTP_200_OK)

        if isinstance(raw, bool):
            is_fav = raw
        else:
            s = str(raw).strip().lower()
            if s in ("1", "true", "yes", "y", "да", "on"):
                is_fav = True
            elif s in ("0", "false", "no", "n", "нет", "off"):
                is_fav = False
            else:
                raise ValidationError({"is_favorite": "Ожидается boolean (true/false)."})
        if is_fav:
            ProductFavorite.objects.get_or_create(company=fav_company, product=product)
        else:
            ProductFavorite.objects.filter(company=fav_company, product=product).delete()

        return Response({"product_id": str(product.id), "is_favorite": is_fav}, status=status.HTTP_200_OK)


@read_only_guard("can_edit_salary")
class MarketSaleEmployeePayProfileListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = MarketSaleEmployeePayProfileSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["user", "branch"]
    ordering_fields = ["id"]

    def get_queryset(self):
        company = self._company()
        if not company:
            return MarketSaleEmployeePayProfile.objects.none()
        qs = MarketSaleEmployeePayProfile.objects.filter(company=company)
        branch = self._auto_branch()
        if branch is not None:
            qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
        # без активного филиала — все схемы компании (у каждой теперь виден branch)
        return qs.select_related("user", "branch").order_by("user_id", "-branch_id")

    def perform_create(self, serializer):
        serializer.save()


@read_only_guard("can_edit_salary")
class MarketSaleEmployeePayProfileRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = MarketSaleEmployeePayProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        company = self._company()
        if not company:
            return MarketSaleEmployeePayProfile.objects.none()
        qs = MarketSaleEmployeePayProfile.objects.filter(company=company)
        branch = self._auto_branch()
        if branch is not None:
            qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
        # без активного филиала — все схемы компании (у каждой теперь виден branch)
        return qs.select_related("user", "branch")


# ==========================
# Product create manual
# ==========================
# ТЗ Н-16: создание товара со сканера кассиру оставлено (решение Умара 25.10); правка и удаление — с правом «Товары».
@read_only_guard("can_edit_products")
class ProductCreateManualAPIView(CompanyBranchRestrictedMixin, generics.CreateAPIView):
    """
    Ручное создание товара + (опционально) добавление в глобальную базу.
    """
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]

    def _normalize_status(self, raw):
        if raw in (None, "", "null"):
            return None
        v = str(raw).strip().lower()
        mapping = {
            "pending":  Product.Status.PENDING,
            "accepted": Product.Status.ACCEPTED,
            "rejected": Product.Status.REJECTED,
            "ожидание": Product.Status.PENDING,
            "принят":   Product.Status.ACCEPTED,
            "отказ":    Product.Status.REJECTED,
        }
        if v in mapping:
            return mapping[v]
        codes = {c[0] for c in Product.Status.choices}
        if v in codes:
            return v
        raise ValueError(f"Недопустимый статус. Допустимые: {', '.join(sorted(codes))}.")

    @idempotent(body_key=True)  # ТЗ Н-08: повтор ключом — прежний ответ, до проверок
    def create(self, request, *args, **kwargs):
        # ТЗ 12.13: любой ответ-ошибка (>=400) после product.save() откатывает всю транзакцию,
        # чтобы не оставалось наполовину сохранённого товара (повтор плодил дубли).
        with transaction.atomic():
            response = self._create_manual(request, *args, **kwargs)
            if getattr(response, "status_code", 200) >= 400:
                transaction.set_rollback(True)
        return response

    def _create_manual(self, request, *args, **kwargs):
        company = self._company()
        branch = None
        data = request.data

        name = (data.get("name") or "").strip()
        description = (data.get("description") or "").strip()
        article = (data.get("article") or "").strip()

        if not name:
            return Response({"name": "Обязательное поле."}, status=status.HTTP_400_BAD_REQUEST)

        barcode = (data.get("barcode") or "").strip() or None
        if barcode and Product.objects.filter(company=company, barcode=barcode).exists():
            return Response(
                {"barcode": "В вашей компании уже есть товар с таким штрих-кодом."},
                status=status.HTTP_400_BAD_REQUEST
            )
        if barcode and ProductAlternateBarcode.objects.filter(company=company, barcode=barcode).exists():
            return Response(
                {"barcode": "Этот штрих-код уже занят как дополнительный у другого товара."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        alt_payload = data.get("alternate_barcodes") if "alternate_barcodes" in data else None

        # kind
        kind_value = _parse_kind(data.get("kind"), Product)

        recipe_input = data.get("recipe")
        recipe_entries, recipe_err = _parse_recipe_input(recipe_input)
        if recipe_err is not None:
            return recipe_err

        calc_from_recipe = _parse_bool_like(data.get("calc_purchase_price_from_recipe", True))
        purchase_price_explicit = data.get("purchase_price") not in (None, "")

        # decimals
        try:
            purchase_price = _parse_decimal(data.get("purchase_price", 0), "purchase_price")
            markup_percent = _parse_decimal(data.get("markup_percent", 0), "markup_percent")
            discount_percent = _parse_decimal(data.get("discount_percent", 0), "discount_percent")
            wholesale_price = _parse_decimal(data.get("wholesale_price", 0), "wholesale_price")
        except ValueError as e:
            return Response({str(e): "Неверный формат числа."}, status=status.HTTP_400_BAD_REQUEST)

        if recipe_entries and calc_from_recipe and not purchase_price_explicit:
            im_ids = [e["id"] for e in recipe_entries]
            ims_preview = ItemMake.objects.filter(id__in=im_ids, company=company)
            ims_preview_map = {str(im.id): im for im in ims_preview}
            missing = [eid for eid in im_ids if eid not in ims_preview_map]
            if missing:
                return Response(
                    {"recipe": f"Сырьё не найдено или принадлежит другой компании: {missing}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            for eid in im_ids:
                try:
                    assert_item_make_recipe_ready(ims_preview_map[eid])
                except ValueError as exc:
                    return Response({"recipe": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            purchase_price = calc_recipe_unit_cost(recipe_entries, ims_preview_map)

        # ====== FIX: двусторонняя логика price <-> markup_percent ======
        price_raw = data.get("price", None)
        price_provided = price_raw not in (None, "")
        if price_raw not in (None, ""):
            try:
                price = _to_dec(price_raw)
            except Exception:
                return Response({"price": "Неверный формат цены продажи."}, status=status.HTTP_400_BAD_REQUEST)
            markup_percent = _calc_markup(purchase_price, price)
        else:
            price = _calc_price(purchase_price, markup_percent)

        # quantity (дробный остаток, до 3 знаков: 10.200)
        if kind_value == Product.Kind.SERVICE:
            quantity = Decimal("0.000")
        else:
            try:
                quantity = _parse_decimal_nonneg(data.get("quantity", 0), "quantity")
            except ValueError:
                return Response({"quantity": "Неверное количество."}, status=status.HTTP_400_BAD_REQUEST)

        # status
        try:
            status_value = self._normalize_status(data.get("status"))
        except ValueError as e:
            return Response({"status": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # date (aware datetime)
        raw_date = data.get("date")
        try:
            date_value = _parse_date_to_aware_datetime(raw_date) if raw_date else timezone.now()
        except ValueError:
            return Response(
                {"date": "Неверный формат даты. Используйте YYYY-MM-DD или ISO datetime."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # unit/is_weight/country/expiration
        unit = (data.get("unit") or "шт.").strip()
        is_weight = _parse_bool_like(data.get("is_weight"))
        country = (data.get("country") or "").strip()

        expiration_raw = data.get("expiration_date")
        expiration_date = None
        if expiration_raw not in (None, ""):
            expiration_date = parse_date(str(expiration_raw))
            if not expiration_date:
                return Response(
                    {"expiration_date": "Неверный формат. Используйте YYYY-MM-DD."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # hotkey_group (F1…F12 для кассы); пусто/невалидно → None
        hotkey_group = (data.get("hotkey_group") or "").strip().upper() or None
        if hotkey_group is not None and not any(
            hotkey_group == c[0] for c in Product.HotkeyGroup.choices
        ):
            return Response(
                {"hotkey_group": "Недопустимая группа. Используйте F1–F12."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # brand/category (через global)
        brand_name = (data.get("brand_name") or "").strip()
        category_name = (data.get("category_name") or "").strip()
        g_brand = GlobalBrand.objects.get_or_create(name=brand_name)[0] if brand_name else None
        g_category = GlobalCategory.objects.get_or_create(name=category_name)[0] if category_name else None

        brand = ProductBrand.objects.get_or_create(company=company, name=g_brand.name)[0] if g_brand else None
        category = ProductCategory.objects.get_or_create(company=company, name=g_category.name)[0] if g_category else None

        # client
        client = None
        client_id = data.get("client")
        if client_id:
            client = get_object_or_404(Client, id=client_id, company=company)

        # packages_input
        packages_input = data.get("packages_input") or data.get("packages")
        if not isinstance(packages_input, list):
            packages_input = []

        # ТЗ 12.12: is_adult, minimum_quantity, plu, shelf_life_days
        extra_fields = {}
        if "is_adult" in data:
            extra_fields["is_adult"] = _parse_bool_like(data.get("is_adult"))
        if data.get("minimum_quantity") not in (None, ""):
            try:
                extra_fields["minimum_quantity"] = _parse_decimal_nonneg(data.get("minimum_quantity"), "minimum_quantity")
            except ValueError:
                return Response({"minimum_quantity": "Неверный минимальный остаток."}, status=status.HTTP_400_BAD_REQUEST)
        for int_key, int_label in (("plu", "ПЛУ"), ("shelf_life_days", "срок хранения")):
            raw_int = data.get(int_key)
            if raw_int not in (None, ""):
                try:
                    int_val = int(str(raw_int).strip())
                    if int_val < 0:
                        raise ValueError
                except ValueError:
                    return Response({int_key: f"Неверное значение: {int_label}."}, status=status.HTTP_400_BAD_REQUEST)
                extra_fields[int_key] = int_val

        product = Product(
            company=company,
            branch=branch,
            kind=kind_value,
            hotkey_group=hotkey_group,

            name=name,
            barcode=barcode,
            brand=brand,
            category=category,

            article=article,
            description=description,

            unit=unit,
            is_weight=is_weight,

            purchase_price=purchase_price,
            markup_percent=markup_percent,
            price=price,
            wholesale_price=wholesale_price,
            discount_percent=discount_percent,

            quantity=quantity,

            client=client,
            status=status_value,
            date=date_value,

            country=country,
            expiration_date=expiration_date,

            created_by=request.user,
            stock=_parse_bool_like(data.get("stock", False)),
            **extra_fields,
            **parse_upsell_fields(data),
        )
        if price_provided:
            setattr(product, "_manual_price", True)
        product.save()

        from apps.main.tz26.hw_catalog.services import apply_request_data  # ТЗ 26.10 Б-57/58/59
        apply_request_data(self, request, data, product)

        # characteristics
        chars_data = data.get("characteristics")
        if isinstance(chars_data, dict):
            ProductCharacteristics.objects.update_or_create(
                product=product,
                defaults={
                    "company": company,
                    "branch": branch,
                    "height_cm": chars_data.get("height_cm") or None,
                    "width_cm": chars_data.get("width_cm") or None,
                    "depth_cm": chars_data.get("depth_cm") or None,
                    "factual_weight_kg": chars_data.get("factual_weight_kg") or None,
                    "description": chars_data.get("description") or "",
                },
            )

        # ====== recipe (приоритет над item_make) ======
        if recipe_entries:
            # Verify all item_make ids exist and belong to company
            im_ids = [e["id"] for e in recipe_entries]
            ims_qs = ItemMake.objects.filter(id__in=im_ids, company=company).select_for_update()
            ims_map = {str(im.id): im for im in ims_qs}
            missing = [eid for eid in im_ids if eid not in ims_map]
            if missing:
                return Response(
                    {"recipe": f"Сырьё не найдено или принадлежит другой компании: {missing}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            for entry in recipe_entries:
                try:
                    assert_item_make_recipe_ready(ims_map[entry["id"]])
                except ValueError as exc:
                    return Response({"recipe": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            product_qty = Decimal(str(product.quantity or 0))

            # Check stock sufficiency and deduct
            for entry in recipe_entries:
                im = ims_map[entry["id"]]
                required = entry["qty_per_unit"] * product_qty
                if im.quantity < required:
                    return Response(
                        {"recipe": (
                            f"Недостаточно сырья «{im.name}»: "
                            f"требуется {required}, доступно {im.quantity}."
                        )},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            # Deduct raw materials and save recipe
            recipe_objects = []
            for entry in recipe_entries:
                im = ims_map[entry["id"]]
                required = entry["qty_per_unit"] * product_qty
                im.quantity -= required
                im.save(update_fields=["quantity", "updated_at"])
                recipe_objects.append(
                    ProductRecipeItem(
                        product=product,
                        item_make=im,
                        qty_per_unit=entry["qty_per_unit"],
                    )
                )
            ProductRecipeItem.objects.bulk_create(recipe_objects)

            # Also set the M2M item_make for backward compat
            product.item_make.set(list(ims_map.values()))

            # Журнал производства: ГП изготовлена из сырья (закупка готового товара сюда не попадает).
            # Себестоимость — стоимость фактически списанного сырья.
            if product_qty > 0:
                record_production(
                    company=company,
                    branch=branch,
                    product=product,
                    quantity=product_qty,
                    unit=product.unit or "",
                    cost_total=(
                        calc_recipe_unit_cost(recipe_entries, ims_map) * product_qty
                    ).quantize(_Q2, rounding=ROUND_HALF_UP),
                    produced_by=request.user,
                )

        else:
            # Backward compatibility: handle old item_make field (no recipe)
            item_make_input = data.get("item_make") or data.get("item_make_ids")
            if item_make_input:
                if isinstance(item_make_input, str):
                    item_make_ids = [item_make_input]
                elif isinstance(item_make_input, (list, tuple)):
                    item_make_ids = list(item_make_input)
                else:
                    item_make_ids = []

                ims = ItemMake.objects.filter(id__in=item_make_ids, company=company)
                if len(item_make_ids) != ims.count():
                    return Response(
                        {"item_make": "Один или несколько item_make не найдены или принадлежат другой компании."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                product.item_make.set(ims)

                # Legacy-поток без рецепта: сырьё не списывается, но товар изготовлен из сырья —
                # для отчёта это производство. Себестоимость берём из закупочной цены товара.
                legacy_qty = Decimal(str(product.quantity or 0))
                if legacy_qty > 0:
                    record_production(
                        company=company,
                        branch=branch,
                        product=product,
                        quantity=legacy_qty,
                        unit=product.unit or "",
                        cost_total=(
                            Decimal(str(product.purchase_price or 0)) * legacy_qty
                        ).quantize(_Q2, rounding=ROUND_HALF_UP),
                        produced_by=request.user,
                    )

        # Закупка готового товара у поставщика (без рецепта и без сырья) — в историю закупок.
        # Товар, изготовленный из сырья, закупкой не является: за него платили при закупке сырья.
        is_produced = bool(recipe_entries) or bool(data.get("item_make") or data.get("item_make_ids"))
        if client is not None and not is_produced:
            record_supplier_purchase(
                company=company,
                branch=branch,
                supplier=client,
                product=product,
                quantity=product.quantity,
                unit=product.unit or "",
                unit_price=product.purchase_price,
                payment_type=_parse_payment_type(data.get("payment_type")),
                created_by=request.user,
            )

        # packages
        packages_to_create = []
        for pkg in packages_input:
            if not isinstance(pkg, dict):
                continue
            name_pkg = (pkg.get("name") or "").strip()
            if not name_pkg:
                continue
            try:
                qip = int(pkg.get("quantity_in_package"))
            except (TypeError, ValueError):
                continue
            unit_pkg = (pkg.get("unit") or "").strip()
            piece_raw = pkg.get("piece_unit_price")
            if piece_raw in (None, "", "null"):
                return Response(
                    {
                        "packages_input": "Для каждой упаковки укажите piece_unit_price (цена за штуку при поштучной продаже).",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                piece_unit_price = Decimal(str(piece_raw))
            except Exception:
                return Response(
                    {"packages_input": "Неверный формат piece_unit_price в упаковке."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if piece_unit_price < 0:
                return Response(
                    {"packages_input": "piece_unit_price не может быть отрицательной."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            piece_unit_price = piece_unit_price.quantize(_Q2, rounding=ROUND_HALF_UP)

            packages_to_create.append(
                ProductPackage(
                    product=product,
                    company=company,
                    branch=branch,
                    name=name_pkg,
                    quantity_in_package=qip,
                    unit=unit_pkg,
                    piece_unit_price=piece_unit_price,
                )
            )

        if packages_to_create:
            ProductPackage.objects.bulk_create(packages_to_create)

        raw_promo = data.get("promotion_rules_input")
        if raw_promo is None:
            raw_promo = data.get("promotion_rules")
        try:
            sync_product_promotion_tiers(
                product,
                raw_promo,
                stock_enabled=bool(product.stock),
                partial=False,
            )
        except serializers.ValidationError as ve:
            return Response(
                ve.detail if isinstance(ve.detail, dict) else {"detail": ve.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if alt_payload is not None:
            try:
                sync_product_alternate_barcodes(product, alt_payload)
            except serializers.ValidationError as ve:
                return Response(
                    ve.detail if isinstance(ve.detail, dict) else {"detail": ve.detail},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        from apps.main.tz26.hw_units.units import apply_product_extras  # ТЗ 26.10 Б-52, Б-53

        apply_product_extras(product, data)

        # add to global product base (optional)
        if barcode:
            GlobalProduct.objects.get_or_create(
                barcode=barcode,
                defaults={"name": name, "brand": g_brand, "category": g_category},
            )

        # Refetch with all related data for serialization
        product = (
            _annotate_product_is_favorite(
                Product.objects.filter(pk=product.pk)
                .select_related("company", "branch", "brand", "category", "client", "created_by", "characteristics")
                .prefetch_related(
                    "item_make",
                    "packages",
                    "recipe_items__item_make",
                    "promotion_tiers",
                    "alternate_barcodes",
                    "sale_units",  # ТЗ 26.10 Б-53
                    product_images_prefetch,
                )
            )
            .get()
        )

        # auto-cashflow for warehouse purchase
        cashbox_id = data.get("cashbox_id")
        cashflow_amount_raw = data.get("cashflow_amount")
        cashflow_type = (data.get("cashflow_type") or "expense").strip().lower()

        from apps.construction.auto_cashflow import create_auto_cashflow
        from apps.construction.models import CashFlow, CashShift

        purchase_amt = None
        if cashflow_amount_raw not in (None, ""):
            try:
                purchase_amt = Decimal(str(cashflow_amount_raw))
            except Exception:
                purchase_amt = None
        elif not is_produced and data.get("payment_type") != "debt":
            if product.purchase_price and product.quantity and product.purchase_price > 0 and product.quantity > 0:
                purchase_amt = (Decimal(str(product.purchase_price)) * Decimal(str(product.quantity))).quantize(Decimal("0.01"))

        if purchase_amt and purchase_amt > 0:
            create_auto_cashflow(
                company=company,
                branch=branch,
                cashbox_id=cashbox_id,
                cashbox_role=data.get("cashbox_role") or "expense_variable",
                user=request.user,
                shift=None,
                type=CashFlow.Type.EXPENSE if cashflow_type == "expense" else CashFlow.Type.INCOME,
                amount=purchase_amt,
                source_kind=CashFlow.SourceKind.WAREHOUSE_PURCHASE,
                source_id=str(product.id),
                name=f"Закупка товара: {product.name}",
                source_business_operation_id="Склад",
                affects_shift_drawer=False,
            )

        ser = self.get_serializer(product, context=self.get_serializer_context())
        return Response(ser.data, status=status.HTTP_201_CREATED)


@flag_guard("can_view_products")  # ТЗ Н-16: запись товаров — только тем, у кого есть доступ к товарам
@read_only_guard("can_edit_products")
@pin_guard("DELETE")
class ProductRetrieveUpdateDestroyAPIView(TrashDestroyMixin, CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    trash_type = "product"
    serializer_class = ProductSerializer

    queryset = (
        Product.objects
        .select_related(
            "company",
            "branch",
            "brand",
            "category",
            "client",
            "created_by",
            "characteristics",
        )
        .prefetch_related(
            "item_make",
            "packages",
            "recipe_items__item_make",
            "promotion_tiers",
            "alternate_barcodes",
            "sale_units",  # ТЗ 26.10 Б-53
            product_images_prefetch,
        )
        .all()
    )

    def get_queryset(self):
        return _annotate_product_is_favorite(
            _filter_products_company_only(self, self.queryset.all())
        )

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        # В детальном просмотре товара отдаём историю закупок (партии).
        ctx["include_purchase_batches"] = True
        return ctx

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "Невозможно удалить товар, так как на него ссылаются другие объекты (например, заказы или передачи)."},
                status=status.HTTP_409_CONFLICT
            )


    def update(self, request, *args, **kwargs):
        # журнал (ТЗ п.8): цена/наценка и ручной остаток — было → стало
        before = admin_audit.product_values(kwargs.get(self.lookup_url_kwarg or self.lookup_field))
        response = self._update_impl(request, *args, **kwargs)
        if before is not None and 200 <= response.status_code < 300:
            try:
                p = Product.objects.select_related("company").get(pk=kwargs.get(self.lookup_url_kwarg or self.lookup_field))
                admin_audit.log_product_change(request, p, before)
            except Exception:
                pass
        return response

    @transaction.atomic
    def _update_impl(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        data = request.data

        recipe_input = data.get("recipe")
        has_recipe = recipe_input is not None

        if has_recipe:
            if not isinstance(recipe_input, list):
                return Response(
                    {"recipe": "Ожидается массив."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Parse and validate new recipe
            seen_ids = set()
            new_recipe_entries = []
            recipe_errors = []
            for idx, entry in enumerate(recipe_input):
                if not isinstance(entry, dict):
                    recipe_errors.append(f"Элемент #{idx}: ожидается объект.")
                    continue
                raw_id = entry.get("id")
                raw_qty = entry.get("qty_per_unit")
                if not raw_id:
                    recipe_errors.append(f"Элемент #{idx}: отсутствует id.")
                    continue
                if raw_qty is None:
                    recipe_errors.append(f"Элемент #{idx}: отсутствует qty_per_unit.")
                    continue
                try:
                    qty_per_unit = Decimal(str(raw_qty))
                except Exception:
                    recipe_errors.append(f"Элемент #{idx}: qty_per_unit — неверный формат.")
                    continue
                if qty_per_unit <= 0:
                    recipe_errors.append(f"Элемент #{idx}: qty_per_unit должен быть > 0.")
                    continue
                str_id = str(raw_id)
                if str_id in seen_ids:
                    recipe_errors.append(f"Элемент #{idx}: дубликат id={raw_id}.")
                    continue
                seen_ids.add(str_id)
                new_recipe_entries.append({"id": str_id, "qty_per_unit": qty_per_unit})

            if recipe_errors:
                return Response(
                    {"recipe": recipe_errors},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            company = self._company()
            check_ids = [e["id"] for e in new_recipe_entries]
            ims_check = ItemMake.objects.filter(id__in=check_ids, company=company)
            ims_check_map = {str(im.id): im for im in ims_check}
            for entry in new_recipe_entries:
                im = ims_check_map.get(entry["id"])
                if not im:
                    return Response(
                        {"recipe": f"Сырьё не найдено: {entry['id']}"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                try:
                    assert_item_make_recipe_ready(im)
                except ValueError as exc:
                    return Response({"recipe": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # ---- Read old state ----
        old_quantity = Decimal(str(instance.quantity or 0))
        old_recipe_qs = ProductRecipeItem.objects.filter(product=instance).select_related("item_make")
        old_recipe_map = {str(ri.item_make_id): ri.qty_per_unit for ri in old_recipe_qs}

        # ---- Determine new state ----
        new_quantity_raw = data.get("quantity")
        new_quantity = Decimal(str(new_quantity_raw)) if new_quantity_raw is not None else old_quantity

        if has_recipe:
            new_recipe_map = {e["id"]: e["qty_per_unit"] for e in new_recipe_entries}
        else:
            new_recipe_map = dict(old_recipe_map)

        # ---- Compute deltas ----
        all_item_ids = set(old_recipe_map.keys()) | set(new_recipe_map.keys())
        deltas = {}
        for im_id in all_item_ids:
            old_req = old_recipe_map.get(im_id, Decimal("0")) * old_quantity
            new_req = new_recipe_map.get(im_id, Decimal("0")) * new_quantity
            delta = new_req - old_req
            if delta != 0:
                deltas[im_id] = delta

        if deltas:
            company = self._company()
            # Lock rows for concurrent safety
            ims_qs = (
                ItemMake.objects
                .filter(id__in=deltas.keys(), company=company)
                .select_for_update()
            )
            ims_map = {str(im.id): im for im in ims_qs}

            # Check all exist
            missing = [eid for eid in deltas if eid not in ims_map]
            if missing:
                return Response(
                    {"recipe": f"Сырьё не найдено: {missing}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Check stock sufficiency for items that need deduction (delta > 0)
            insufficiency_errors = []
            for im_id, delta in deltas.items():
                if delta > 0:
                    im = ims_map[im_id]
                    if im.quantity < delta:
                        insufficiency_errors.append(
                            f"Недостаточно сырья «{im.name}»: "
                            f"нужно досписать {delta}, доступно {im.quantity}."
                        )

            if insufficiency_errors:
                return Response(
                    {"recipe": insufficiency_errors},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Apply deltas
            for im_id, delta in deltas.items():
                im = ims_map[im_id]
                im.quantity -= delta
                im.save(update_fields=["quantity", "updated_at"])

        # ---- Update recipe rows ----
        if has_recipe:
            ProductRecipeItem.objects.filter(product=instance).delete()
            if new_recipe_entries:
                company = self._company()
                ims_qs = ItemMake.objects.filter(
                    id__in=[e["id"] for e in new_recipe_entries],
                    company=company,
                )
                ims_map_for_recipe = {str(im.id): im for im in ims_qs}
                ProductRecipeItem.objects.bulk_create([
                    ProductRecipeItem(
                        product=instance,
                        item_make=ims_map_for_recipe[e["id"]],
                        qty_per_unit=e["qty_per_unit"],
                    )
                    for e in new_recipe_entries
                ])
                # Update M2M for backward compat
                instance.item_make.set(list(ims_map_for_recipe.values()))

        # ---- Let DRF serializer handle all other fields ----
        # Remove recipe from data so the serializer doesn't choke on it
        mutable_data = data.copy() if hasattr(data, "copy") else dict(data)
        mutable_data.pop("recipe", None)

        serializer = self.get_serializer(instance, data=mutable_data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        # Refetch with all prefetches
        instance = (
            _annotate_product_is_favorite(
                Product.objects.filter(pk=instance.pk)
                .select_related("company", "branch", "brand", "category", "client", "created_by", "characteristics")
                .prefetch_related(
                    "item_make",
                    "packages",
                    "recipe_items__item_make",
                    "promotion_tiers",
                    "alternate_barcodes",
                    "sale_units",  # ТЗ 26.10 Б-53
                    product_images_prefetch,
                )
            )
            .get()
        )
        data = self.get_serializer(instance).data
        # Б-06: price_changed считает сериализатор, который делал update, — переносим в ответ.
        if hasattr(serializer, "_price_changed"):
            data["price_changed"] = bool(serializer._price_changed)
        return Response(data, status=status.HTTP_200_OK)



@flag_guard("can_view_products")  # ТЗ Н-16: запись товаров — только тем, у кого есть доступ к товарам
@read_only_guard("can_edit_products")
class ProductBulkUpdateAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    PATCH /api/main/products/bulk-update/
    Body:
    {
      "ids": [...],
      "brand_name": "Name", "brand": "UUID/Name", "brand_id": "UUID",
      "category_name": "Name", "category": "UUID/Name", "category_id": "UUID",
      "client": "UUID", "client_id": "UUID", "supplier_id": "UUID", "supplier": "UUID/Name",
      "require_all": false
    }
    """
    def patch(self, request, *args, **kwargs):
        return self._bulk_update(request)
        
    def post(self, request, *args, **kwargs):
        return self._bulk_update(request)

    def _bulk_update(self, request):
        serializer = BulkIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        require_all = serializer.validated_data.get("require_all", False)
        
        if len(ids) > 1000:
            return Response(
                {"detail": "За один запрос можно изменить не более 1000 товаров"}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data
        has_brand = "brand_name" in data or "brand" in data or "brand_id" in data
        has_category = "category_name" in data or "category" in data or "category_id" in data
        has_client = "client" in data or "client_id" in data or "supplier_id" in data or "supplier" in data

        if not (has_brand or has_category or has_client):
            return Response(
                {"detail": "Не передано ни одного поля для изменения"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        all_requested_products = Product.objects.filter(id__in=ids)
        user = self._user()
        company = self._company() or (all_requested_products.first().company if all_requested_products.exists() else None)
        branch = getattr(request, "_cached_auto_branch", None)
        if branch is None:
            branch = self._auto_branch()

        is_owner = (
            not user
            or getattr(user, "is_superuser", False)
            or bool(getattr(user, "owned_company", None))
            or getattr(user, "is_admin", False)
            or str(getattr(user, "role", "")).lower() in ("owner", "admin", "rop", "владелец", "администратор")
        )

        company_kw = {"company": company} if company else {}
        update_fields = {}
        applied = {}

        # 1. Resolve brand
        if has_brand:
            brand_val = data.get("brand_name") or data.get("brand") or data.get("brand_id")
            if brand_val is None or str(brand_val).strip() == "" or str(brand_val).strip().lower() in ("null", "none"):
                update_fields["brand"] = None
                applied["brand"] = None
            else:
                brand_val_str = str(brand_val).strip()
                brand_obj = None
                try:
                    import uuid
                    uuid_val = uuid.UUID(brand_val_str)
                    brand_obj = ProductBrand.objects.filter(id=uuid_val, **company_kw).first()
                    if not brand_obj:
                        brand_obj = ProductBrand.objects.filter(id=uuid_val).first()
                except ValueError:
                    pass
                    
                if not brand_obj:
                    brand_obj = ProductBrand.objects.filter(name__iexact=brand_val_str, **company_kw).first()
                    
                if not brand_obj and company:
                    brand_obj, _ = ProductBrand.objects.get_or_create(
                        name=brand_val_str,
                        company=company,
                        defaults={"branch": branch if not is_owner else None}
                    )
                    
                if not brand_obj:
                    return Response({"detail": f"Бренд «{brand_val_str}» не найден"}, status=status.HTTP_400_BAD_REQUEST)

                update_fields["brand"] = brand_obj
                applied["brand"] = {"id": str(brand_obj.id), "name": brand_obj.name}

        # 2. Resolve category
        if has_category:
            cat_val = data.get("category_name") or data.get("category") or data.get("category_id")
            if cat_val is None or str(cat_val).strip() == "" or str(cat_val).strip().lower() in ("null", "none"):
                update_fields["category"] = None
                applied["category"] = None
            else:
                cat_val_str = str(cat_val).strip()
                cat_obj = None
                try:
                    import uuid
                    uuid_val = uuid.UUID(cat_val_str)
                    cat_obj = ProductCategory.objects.filter(id=uuid_val, **company_kw).first()
                    if not cat_obj:
                        cat_obj = ProductCategory.objects.filter(id=uuid_val).first()
                except ValueError:
                    pass
                    
                if not cat_obj:
                    cat_obj = ProductCategory.objects.filter(name__iexact=cat_val_str, **company_kw).first()
                    
                if not cat_obj and company:
                    cat_obj, _ = ProductCategory.objects.get_or_create(
                        name=cat_val_str,
                        company=company,
                        defaults={"branch": branch if not is_owner else None}
                    )
                    
                if not cat_obj:
                    return Response({"detail": f"Категория «{cat_val_str}» не найдена"}, status=status.HTTP_400_BAD_REQUEST)

                update_fields["category"] = cat_obj
                applied["category"] = {"id": str(cat_obj.id), "name": cat_obj.name}

        # 3. Resolve client / supplier
        if has_client:
            client_val = data.get("client") or data.get("client_id") or data.get("supplier_id") or data.get("supplier")
            if client_val is None or str(client_val).strip() == "" or str(client_val).strip().lower() in ("null", "none"):
                update_fields["client"] = None
                applied["client"] = None
            else:
                client_val_str = str(client_val).strip()
                client_obj = None
                try:
                    import uuid
                    uuid_val = uuid.UUID(client_val_str)
                    client_obj = Client.objects.filter(id=uuid_val, **company_kw).first()
                    if not client_obj:
                        client_obj = Client.objects.filter(id=uuid_val).first()
                except ValueError:
                    pass
                    
                if not client_obj:
                    client_obj = Client.objects.filter(full_name__iexact=client_val_str, **company_kw).first()
                    
                if not client_obj:
                    return Response({"detail": f"Поставщик «{client_val_str}» не найден"}, status=status.HTTP_400_BAD_REQUEST)

                update_fields["client"] = client_obj
                applied["client"] = {"id": str(client_obj.id), "full_name": client_obj.full_name}

        # 4. Filter products
        all_requested_products = Product.objects.filter(id__in=ids)
        
        valid_ids = []
        skipped = []
        
        for p in all_requested_products:
            if company and p.company_id and p.company_id != company.id:
                skipped.append({"id": str(p.id), "reason": "forbidden"})
            elif getattr(p, "is_active", True) == False:
                skipped.append({"id": str(p.id), "reason": "deleted"})
            elif not is_owner and branch is not None and p.branch_id and p.branch_id != branch.id:
                skipped.append({"id": str(p.id), "reason": "other_branch"})
            else:
                valid_ids.append(p.id)
                
        found_ids_set = {str(p.id) for p in all_requested_products}
        for req_id in ids:
            req_str = str(req_id)
            if req_str not in found_ids_set:
                skipped.append({"id": req_str, "reason": "not_found"})

        if require_all and skipped:
            return Response(
                {"detail": "Некоторые товары недоступны или не найдены", "skipped": skipped},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # 5. Bulk Update
        if valid_ids:
            with transaction.atomic():
                Product.objects.filter(id__in=valid_ids).update(**update_fields)
                if has_client and update_fields.get("client"):
                    c_obj = update_fields["client"]
                    for p in Product.objects.filter(id__in=valid_ids):
                        p.suppliers.add(c_obj)
                
            try:
                from apps.main.cache_utils import invalidate_cache_pattern
                cid = company.id if company else "*"
                invalidate_cache_pattern(f"analytics:market:{cid}:")
                invalidate_cache_pattern(f"products:list:{cid}:")
                invalidate_cache_pattern(f"products:*")
            except Exception:
                pass
                
        return Response({
            "updated": len(valid_ids),
            "failed": len(skipped),
            "requested": len(ids),
            "updated_ids": [str(v) for v in valid_ids],
            "skipped": skipped,
            "applied": applied
        }, status=status.HTTP_200_OK)


@flag_guard("can_view_products")  # ТЗ Н-16: запись товаров — только тем, у кого есть доступ к товарам
@read_only_guard("can_edit_products")
@pin_guard()
class ProductBulkDeleteAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    DELETE /api/main/products/bulk-delete/
    Body: {"ids": [...], "soft": false, "require_all": false}
    """
    def delete(self, request, *args, **kwargs):
        serializer = BulkIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        soft = serializer.validated_data["soft"]
        require_all = serializer.validated_data["require_all"]

        # в рамках компании и текущего филиала/глобальных
        qs = _filter_products_company_only(self, Product.objects.all()).filter(id__in=ids)
        found_map = {p.id: p for p in qs}
        not_found = [str(id_) for id_ in ids if id_ not in found_map]

        results = {"deleted": [], "protected": [], "not_found": not_found}

        from apps.construction import trash as _trash

        def _delete_one(p: Product):
            try:
                pname, ppk = p.name, p.pk
                with transaction.atomic():
                    entry = _trash.record(request, "product", p)
                    if soft and hasattr(p, "is_active"):
                        p.is_active = False
                        p.save(update_fields=["is_active"])
                    else:
                        p.delete()
                    _trash.finalize(entry, "product", p)
                admin_audit.log_request(request, "delete", "product", ppk, pname, company=p.company)
                results["deleted"].append(str(p.id))
            except ProtectedError:
                results["protected"].append(str(p.id))

        if require_all:
            try:
                with transaction.atomic():
                    for p in found_map.values():
                        _delete_one(p)
                    if results["protected"]:
                        raise ProtectedError("protected", None)
            except ProtectedError:
                return Response(
                    {
                        "detail": "Некоторые продукты защищены связями, удаление откатено.",
                        "protected": results["protected"],
                        "not_found": results["not_found"],
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(results, status=status.HTTP_200_OK)

        for p in found_map.values():
            _delete_one(p)

        http_status = status.HTTP_200_OK if not results["protected"] else status.HTTP_207_MULTI_STATUS
        return Response(results, status=http_status)


class ProductByBarcodeAPIView(CompanyBranchRestrictedMixin, generics.RetrieveAPIView):
    serializer_class = ProductSerializer
    lookup_field = "barcode"

    def get_queryset(self):
        qs = (
            Product.objects
            .select_related(
                "company",
                "branch",
                "brand",
                "category",
                "client",
                "created_by",
                "characteristics",
            )
            .prefetch_related(
                "item_make",
                "packages",
                "recipe_items__item_make",
                "promotion_tiers",
                "alternate_barcodes",
                "sale_units",  # ТЗ 26.10 Б-53
                product_images_prefetch,
            )
            .all()
        )
        return _annotate_product_is_favorite(_filter_products_company_only(self, qs))

    def get_object(self):
        import re
        from rest_framework.exceptions import NotFound

        barcode = self.kwargs.get("barcode")
        if not barcode:
            raise NotFound(detail="Штрих-код не указан")

        barcode = barcode.replace(' ', '')
        if 'Alt' in barcode:
            alt_codes = re.findall(r'Alt(\d+)', barcode)
            if alt_codes:
                try:
                    barcode = ''.join(chr(int(code)) for code in alt_codes)
                except ValueError:
                    pass

        variants = _barcode_lookup_variants(barcode)
        product = (
            self.get_queryset()
            .filter(Q(barcode__in=variants) | Q(alternate_barcodes__barcode__in=variants))
            .distinct()
            .first()
        )
        self._matched_variant = None
        self._matched_unit = None
        self._matched_by = "barcode" if product else None
        if product and product.barcode not in variants:
            self._matched_by = "alternate_barcode"
        if not product:
            # ТЗ Н-33: штрихкод размера/цвета (ProductVariant.barcode)
            from apps.main.models import ProductVariant as _PV

            pv = _PV.objects.filter(barcode__in=variants, is_active=True).select_related("product").first()
            if pv is not None:
                product = self.get_queryset().filter(pk=pv.product_id).first()
                if product is not None:
                    self._matched_variant = pv
                    self._matched_by = "variant_barcode"
        if not product:
            # ТЗ 26.10 Б-53 п.3: штрихкод единицы продажи (коробка/мешок)
            from apps.main.tz26.hw_units.units import find_sale_unit_by_barcode

            for code in variants:
                unit = find_sale_unit_by_barcode(self._company().id, code)
                if unit is not None:
                    product = self.get_queryset().filter(pk=unit.product_id).first()
                    if product is not None:
                        self._matched_unit, self._matched_by = unit, "sale_unit_barcode"
                        break
        if not product:
            raise NotFound(detail="Товар с таким штрих-кодом не найден")
        return product

    def retrieve(self, request, *args, **kwargs):
        """Ответ — как раньше (поля товара). ТЗ Н-33: плюс product/variant/matched_by."""
        instance = self.get_object()
        data = dict(self.get_serializer(instance).data)
        variant = getattr(self, "_matched_variant", None)
        from apps.main.kassa_stage3 import ProductVariantSerializer

        data["product"] = dict(data)
        if variant is not None:  # без совпадения по варианту ключа variant нет (контракт possync)
            data["variant"] = ProductVariantSerializer(variant, context={"product": instance}).data
        data["matched_by"] = getattr(self, "_matched_by", "barcode")
        if getattr(self, "_matched_unit", None) is not None:  # ТЗ 26.10 Б-53 п.3
            from apps.main.tz26.hw_units.units import sale_unit_scan_payload

            data.update(sale_unit_scan_payload(self._matched_unit))
        return Response(data)


class ProductWarehouseBarcodeAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /main/products/warehouse-barcode/<barcode>/
    Поиск товара по штрихкоду для склада маркета (/crm/sklad).
    Та же логика резолва, что POS scan, без корзины и продажи.
    """

    def _warehouse_product_queryset(self):
        qs = (
            Product.objects
            .select_related(
                "company",
                "branch",
                "brand",
                "category",
                "client",
                "created_by",
                "characteristics",
            )
            .prefetch_related(
                "item_make",
                "packages",
                "recipe_items__item_make",
                "promotion_tiers",
                "alternate_barcodes",
                product_images_prefetch,
            )
        )
        qs = _filter_products_company_only(self, qs)
        qs = qs.exclude(status=Product.Status.REJECTED)
        branch_param = (self.request.query_params.get("branch") or "").strip()
        if branch_param:
            try:
                branch_uuid = UUID(branch_param)
            except (ValueError, TypeError, AttributeError):
                raise ValidationError({"branch": "Некорректный UUID филиала."})
            qs = qs.filter(Q(branch_id=branch_uuid) | Q(branch__isnull=True))
        return _annotate_product_is_favorite(qs)

    def get(self, request, barcode, *args, **kwargs):
        from apps.main.pos_views import (
            _lookup_product_for_pos_scan,
            AmbiguousBarcode,
            _ambiguous_barcode_response,
        )

        barcode = (barcode or "").strip()
        if not barcode:
            return Response({"detail": "Пустой штрихкод."}, status=status.HTTP_400_BAD_REQUEST)

        company = self._company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_403_FORBIDDEN)

        try:
            product_stub, _scale_data, lookup_error = _lookup_product_for_pos_scan(company.id, barcode)
        except AmbiguousBarcode as exc:
            return _ambiguous_barcode_response(exc)
        if not product_stub:
            return Response(
                {"detail": lookup_error or "Товар с таким штрих-кодом не найден."},
                status=status.HTTP_404_NOT_FOUND,
            )

        product = self._warehouse_product_queryset().filter(pk=product_stub.pk).first()
        if not product:
            return Response(
                {"detail": "Товар с таким штрих-кодом не найден."},
                status=status.HTTP_404_NOT_FOUND,
            )

        data = ProductSerializer(product, context={"request": request}).data
        return Response({"product": data, "matched_barcode": barcode})


# ===========================
#  Reviews
# ===========================
class ReviewListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = ReviewSerializer
    queryset = Review.objects.select_related("company", "branch", "user", "product").all()
    filter_backends = [DjangoFilterBackend]
    filterset_fields = "__all__"

    def perform_create(self, serializer):
        self._save_with_company_branch(serializer, user=self.request.user)


class ReviewRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ReviewSerializer
    queryset = Review.objects.select_related("company", "branch", "user", "product").all()


# ===========================
#  Notifications
# ===========================
class NotificationListView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    serializer_class = NotificationSerializer
    queryset = Notification.objects.select_related("company", "branch", "user", "actor").all()
    pagination_class = LimitOffsetPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["category", "type", "level", "is_read"]

    def get_queryset(self):
        # Уведомления персональные: каждый пользователь (в т.ч. агент) видит только свои.
        qs = super().get_queryset().filter(user=self.request.user)
        cat = (self.request.query_params.get("category") or "").strip()
        if cat:
            qs = qs.filter(Q(category=cat) | Q(type=cat))
        return qs

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        # Точное число непрочитанных (для колокольчика) — независимо от фильтров/пагинации.
        unread = Notification.objects.filter(user=self.request.user, is_read=False).count()
        if isinstance(response.data, dict):
            response.data["unread_count"] = unread
        else:
            response.data = {"count": len(response.data), "unread_count": unread, "results": response.data}
        return response


class NotificationDetailView(CompanyBranchRestrictedMixin, generics.RetrieveDestroyAPIView):
    serializer_class = NotificationSerializer
    queryset = Notification.objects.select_related("company", "branch", "user", "actor").all()

    def get_queryset(self):
        return super().get_queryset().filter(user=self.request.user)


class NotificationUnreadCountView(APIView):
    """GET /api/main/notifications/unread-count/ -> {"count": N} (ТЗ 12.20). Опционально ?category=."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qs = Notification.objects.filter(user=request.user, is_read=False)
        cat = (request.query_params.get("category") or "").strip()
        if cat:
            qs = qs.filter(Q(category=cat) | Q(type=cat))
        return Response({"count": qs.count()})


class MarkAllNotificationsReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        cat = (request.query_params.get("category") or (request.data and request.data.get("category")) or "").strip()
        qs = Notification.objects.filter(user=request.user, is_read=False)
        if cat:
            qs = qs.filter(Q(category=cat) | Q(type=cat))
        qs.update(is_read=True)
        from apps.main.notifications_market import push_unread_count

        push_unread_count(request.user)
        unread = Notification.objects.filter(user=request.user, is_read=False).count()
        return Response(
            {"status": "Все уведомления прочитаны", "unread_count": unread},
            status=status.HTTP_200_OK,
        )


class MarkNotificationReadView(APIView):
    """POST /api/main/notifications/<uuid:pk>/read/ — отметить одно прочитанным."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        notification = Notification.objects.filter(pk=pk, user=request.user).first()
        if notification is None:
            return Response({"detail": "Уведомление не найдено."}, status=status.HTTP_404_NOT_FOUND)
        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=["is_read"])
            from apps.main.notifications_market import push_unread_count

            push_unread_count(request.user)
        unread = Notification.objects.filter(user=request.user, is_read=False).count()
        return Response(
            {"id": str(notification.id), "is_read": True, "unread_count": unread},
            status=status.HTTP_200_OK,
        )


class POSQuickSlotsAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    Управление быстрыми слотами кассы (F3 + 0..9):
    GET /api/main/pos/quick-slots/
    PUT /api/main/pos/quick-slots/
    """
    def get(self, request, *args, **kwargs):
        company = self._company()
        if not company:
            return Response({"slots": {str(i): None for i in range(10)}}, status=status.HTTP_200_OK)

        data = getattr(company, "pos_quick_slots", None) or {}
        if not isinstance(data, dict):
            data = {}
        slots = {str(i): data.get(str(i)) for i in range(10)}
        return Response({"slots": slots}, status=status.HTTP_200_OK)

    def put(self, request, *args, **kwargs):
        company = self._company()
        if not company:
            raise ValidationError({"detail": "Компания не определена."})

        new_slots = request.data.get("slots")
        if new_slots is None and isinstance(request.data, dict):
            new_slots = request.data

        if not isinstance(new_slots, dict):
            raise ValidationError({"slots": "Слоты должны быть объектом с ключами 0..9."})

        current_slots = getattr(company, "pos_quick_slots", None) or {}
        if not isinstance(current_slots, dict):
            current_slots = {}

        for k in range(10):
            key = str(k)
            if key in new_slots:
                val = new_slots[key]
                if val is not None and str(val).strip():
                    try:
                        val = str(UUID(str(val)))
                    except Exception:
                        raise ValidationError({"slots": f"Слот {key}: некорректный UUID товара."})
                else:
                    val = None
                current_slots[key] = val

        company.pos_quick_slots = current_slots
        company.save(update_fields=["pos_quick_slots"])

        slots = {str(i): current_slots.get(str(i)) for i in range(10)}
        return Response({"slots": slots}, status=status.HTTP_200_OK)


# ===========================
#  Events
# ===========================
class EventListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = EventSerializer
    queryset = Event.objects.select_related("company", "branch", "user").all()
    filter_backends = [DjangoFilterBackend]
    filterset_fields = "__all__"


class EventRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = EventSerializer
    queryset = Event.objects.select_related("company", "branch", "user").all()


# ===========================
#  Warehouses
# ===========================
class WarehouseListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = WarehouseSerializer
    queryset = Warehouse.objects.select_related("company", "branch").all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["name", "location"]
    filterset_fields = "__all__"


class WarehouseRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = WarehouseSerializer
    queryset = Warehouse.objects.select_related("company", "branch").all()


class WarehouseEventListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = WarehouseEventSerializer
    queryset = WarehouseEvent.objects.select_related("company", "branch", "warehouse", "responsible_person").all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["title", "client_name"]
    filterset_fields = "__all__"


class WarehouseEventRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = WarehouseEventSerializer
    queryset = WarehouseEvent.objects.select_related("company", "branch", "warehouse", "responsible_person").all()


# ===========================
#  Product taxonomies
# ===========================
@read_only_guard("can_edit_products")
class ProductCategoryListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = ProductCategorySerializer
    queryset = ProductCategory.objects.select_related("company", "branch", "parent").all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["name"]
    filterset_fields = "__all__"

    def perform_create(self, serializer):
        try:
            self._save_with_company_branch(serializer)
        except IntegrityError as e:
            msg = str(e)
            if (
                "uq_crm_category_name_global_per_company" in msg
                or "uq_crm_category_name_per_branch" in msg
            ):
                raise ValidationError({"name": "Категория с таким названием уже существует."})
            raise


@read_only_guard("can_edit_products")
class ProductCategoryRetrieveUpdateDestroyAPIView(TrashDestroyMixin, CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    trash_type = "category"
    serializer_class = ProductCategorySerializer
    queryset = ProductCategory.objects.select_related("company", "branch", "parent").all()

    def perform_update(self, serializer):
        try:
            self._save_with_company_branch(serializer)
        except IntegrityError as e:
            msg = str(e)
            if (
                "uq_crm_category_name_global_per_company" in msg
                or "uq_crm_category_name_per_branch" in msg
            ):
                raise ValidationError({"name": "Категория с таким названием уже существует."})
            raise


@read_only_guard("can_edit_products")
class ProductBrandListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = ProductBrandSerializer
    queryset = ProductBrand.objects.select_related("company", "branch").all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["name"]
    filterset_fields = "__all__"

    def perform_create(self, serializer):
        try:
            self._save_with_company_branch(serializer)
        except IntegrityError as e:
            msg = str(e)
            if (
                "uq_crm_brand_name_global_per_company" in msg
                or "uq_crm_brand_name_per_branch" in msg
            ):
                raise ValidationError({"name": "Бренд с таким названием уже существует."})
            raise


@read_only_guard("can_edit_products")
class ProductBrandRetrieveUpdateDestroyAPIView(TrashDestroyMixin, CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    trash_type = "brand"
    serializer_class = ProductBrandSerializer
    queryset = ProductBrand.objects.select_related("company", "branch").all()

    def perform_update(self, serializer):
        try:
            self._save_with_company_branch(serializer)
        except IntegrityError as e:
            msg = str(e)
            if (
                "uq_crm_brand_name_global_per_company" in msg
                or "uq_crm_brand_name_per_branch" in msg
            ):
                raise ValidationError({"name": "Бренд с таким названием уже существует."})
            raise


# ===========================
#  Product views
# ===========================


class ProductByGlobalBarcodeAPIView(CompanyBranchRestrictedMixin, generics.RetrieveAPIView):
    """
    GET /main/products/barcode/<barcode>/
    Возвращает товар ТОЛЬКО из глобальной базы (GlobalProduct).
    """
    serializer_class = GlobalProductReadSerializer
    lookup_field = "barcode"
    # важно определить queryset, иначе миксин выдаст AssertionError
    queryset = GlobalProduct.objects.select_related("brand", "category").all()

    def get_object(self):
        import re
        barcode = self.kwargs.get("barcode")
        if not barcode:
            raise NotFound(detail="Штрих-код не указан")

        barcode = barcode.replace(' ', '')
        if 'Alt' in barcode:
            alt_codes = re.findall(r'Alt(\d+)', barcode)
            if alt_codes:
                try:
                    barcode = ''.join(chr(int(code)) for code in alt_codes)
                except ValueError:
                    pass

        variants = _barcode_lookup_variants(barcode)
        obj = self.get_queryset().filter(barcode__in=variants).first()
        if not obj:
            raise NotFound(detail="Товар с таким штрих-кодом не найден в глобальной базе")
        return obj




# ===========================
#  Order analytics
# ===========================
class OrderAnalyticsView(APIView, CompanyBranchRestrictedMixin):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        # берём заказы в рамках company/branch по общей логике миксина
        orders = self._filter_qs_company_branch(Order.objects.all())

        start_date = request.query_params.get("start")
        end_date = request.query_params.get("end")
        status_filter = request.query_params.get("status")

        if start_date:
            start_date = parse_date(start_date)
            orders = orders.filter(date_ordered__gte=start_date)

        if end_date:
            end_date = parse_date(end_date)
            orders = orders.filter(date_ordered__lte=end_date)

        if status_filter:
            orders = orders.filter(status=status_filter)

        total_orders = orders.count()
        total_amount = orders.aggregate(total=Sum("items__total"))["total"] or 0
        average_amount = orders.aggregate(avg=Avg("items__total"))["avg"] or 0

        orders_by_status = orders.values("status").annotate(
            order_count=Count("id"),
            total_amount=Sum("items__total"),
            average_amount=Avg("items__total"),
        )

        response_data = {
            "filters": {
                "start_date": start_date,
                "end_date": end_date,
                "status": status_filter,
            },
            "summary": {
                "total_orders": total_orders,
                "total_amount": total_amount,
                "average_order_amount": average_amount,
            },
            "orders_by_status": list(orders_by_status),
        }

        return Response(response_data)


# ===========================
#  Clients
# ===========================
def _is_agent(user) -> bool:
    """
    Пользователь-агент — единственная роль, которая видит только "своих" клиентов
    (по полю salesperson). Все остальные сотрудники (кассир, менеджер, админ,
    владелец) работают с общим списком клиентов компании.
    """
    return getattr(user, "role", None) == "agent"


def _filter_clients_visible_for_user(qs, user):
    """
    Только агенты (role == "agent") видят исключительно своих клиентов.
    Все остальные сотрудники компании видят всех клиентов компании/филиала —
    ограничение выдачи делает только CompanyBranchRestrictedMixin (по компании),
    а не автор записи (created_by/salesperson).
    """
    if user and _is_agent(user):
        return qs.filter(salesperson=user)
    return qs


class ClientPhoneAwareSearchFilter(filters.SearchFilter):
    """
    ?search=0555 12-34-56 находит клиента, записанного как +996555123456 (и наоборот):
    если строка похожа на телефон — ищем по нормализованному номеру (последние 9 цифр).
    """

    def filter_queryset(self, request, queryset, view):
        from apps.main.phone_utils import looks_like_phone, phone_search_suffix

        term = (request.query_params.get(self.search_param) or "").strip()
        if looks_like_phone(term):
            suffix = phone_search_suffix(term)
            return queryset.filter(
                Q(phone_normalized__contains=suffix) | Q(phone__icontains=term)
            )
        return super().filter_queryset(request, queryset, view)


@read_only_guard("can_edit_clients")
class ClientListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    """
    GET  /api/main/clients/
    POST /api/main/clients/
    """
    serializer_class = ClientSerializer
    filter_backends = [DjangoFilterBackend, ClientPhoneAwareSearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "date", "type", "sector"]
    search_fields = ["full_name", "phone", "email"]
    ordering_fields = ["created_at", "updated_at", "date"]
    ordering = ["-created_at"]


    @idempotent
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def get_queryset(self):
        qs = self._filter_qs_company_branch(
            Client.objects.select_related("company", "branch").all()
        )
        sector_param = self.request.query_params.get("sector")
        if sector_param and sector_param != "all":
            qs = qs.filter(sector__in=[sector_param, "all"])
        return _filter_clients_visible_for_user(qs, self.request.user)

    def perform_create(self, serializer):
        kwargs = {}
        if not serializer.validated_data.get("sector"):
            kwargs["sector"] = "market"
        if not _is_owner_like(self.request.user):
            kwargs["salesperson"] = self.request.user
        self._save_with_company_branch(serializer, **kwargs)


@flag_guard("can_view_clients")  # ТЗ Н-16: правка/удаление клиента и поставщика; создание из чека не трогаем
@read_only_guard("can_edit_clients")
class ClientRetrieveUpdateDestroyAPIView(TrashDestroyMixin, CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    trash_type = "client"
    """
    GET    /api/main/clients/<uuid:pk>/
    PATCH  /api/main/clients/<uuid:pk>/
    PUT    /api/main/clients/<uuid:pk>/
    DELETE /api/main/clients/<uuid:pk>/
    """
    serializer_class = ClientSerializer

    def get_queryset(self):
        qs = self._filter_qs_company_branch(
            Client.objects.select_related("company", "branch").all()
        )
        return _filter_clients_visible_for_user(qs, self.request.user)

    def perform_update(self, serializer):
        # Агенту нельзя "перекидывать" клиента на другого salesperson.
        if not _is_owner_like(self.request.user):
            self._save_with_company_branch(serializer, salesperson=self.request.user)
            return
        self._save_with_company_branch(serializer)


class ClientSubscriptionScheduleAPIView(APIView):
    """
    График абонентских платежей клиента (из consalting-продаж с абонеткой).
    GET /api/main/clients/<uuid:client_id>/subscription-schedule/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, client_id):
        from apps.consalting.funnel.completion import build_subscription_schedule

        company = _get_company(request.user)
        qs = Client.objects.all()
        if company is not None:
            qs = qs.filter(company=company)
        client = get_object_or_404(qs, id=client_id)
        return Response({"items": build_subscription_schedule(client)})


def _deal_prefetch():
    return [
        Prefetch("installments", queryset=DealInstallment.objects.order_by("number")),
        Prefetch(
            "payments",
            queryset=DealPayment.objects.select_related("installment", "created_by").order_by("-created_at"),
        ),
    ]


# ===== Deals list/create =====

class ClientKPIsAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/clients/<client_id>/kpis/
    Вычисляет 3 KPI по сделкам клиента: debt, prepayment, sale
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, client_id, *args, **kwargs):
        company = self._company()
        branch = self._auto_branch()
        user = request.user

        if not company:
            raise serializers.ValidationError({"company": "У пользователя не задана компания."})

        client_qs = Client.objects.filter(company=company)
        if branch is not None:
            client_qs = client_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
        if not _is_owner_like(user):
            client_qs = client_qs.filter(salesperson=user)

        client = get_object_or_404(client_qs, id=client_id)

        deals = ClientDeal.objects.filter(company=company, client=client).prefetch_related("installments")

        debt_amount = Decimal("0.00")
        debt_count = 0

        prepayment_amount = Decimal("0.00")
        prepayment_count = 0

        sale_amount = Decimal("0.00")
        sale_count = 0

        for d in deals:
            kind = d.kind
            if kind == ClientDeal.Kind.CANCELED:
                continue
            if kind == ClientDeal.Kind.DEBT:
                if d.remaining_debt > Decimal("0.00"):
                    debt_count += 1
                    debt_amount += d.remaining_debt
                if d.prepayment and d.prepayment > Decimal("0.00"):
                    prepayment_amount += d.prepayment
                    prepayment_count += 1
            elif kind == ClientDeal.Kind.PREPAYMENT:
                prepayment_count += 1
                prepayment_amount += (d.amount or Decimal("0.00"))
            elif kind in (ClientDeal.Kind.SALE, ClientDeal.Kind.AMOUNT):
                sale_count += 1
                sale_amount += (d.amount or Decimal("0.00"))

        debt_amount = max(Decimal("0.00"), debt_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        prepayment_amount = max(Decimal("0.00"), prepayment_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        sale_amount = max(Decimal("0.00"), sale_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return Response({
            "debt": {"amount": str(debt_amount), "count": debt_count},
            "prepayment": {"amount": str(prepayment_amount), "count": prepayment_count},
            "sale": {"amount": str(sale_amount), "count": sale_count},
        }, status=status.HTTP_200_OK)


@read_only_guard("can_edit_clients")
class ClientDealListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    """
      GET  /api/main/deals/
      POST /api/main/deals/
      GET  /api/main/clients/<client_id>/deals/
      POST /api/main/clients/<client_id>/deals/
    """
    serializer_class = ClientDealSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["kind", "client"]
    search_fields = ["title", "note"]
    ordering_fields = ["created_at", "updated_at", "amount", "kind"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = (
            ClientDeal.objects
            .select_related("client")
            .prefetch_related(*_deal_prefetch())
        )
        qs = self._filter_qs_company_branch(qs)
        if not _is_owner_like(self.request.user):
            qs = qs.filter(client__salesperson=self.request.user)

        client_id = self.kwargs.get("client_id")
        if client_id:
            qs = qs.filter(client_id=client_id)
        return qs


    @idempotent(body_key=True)  # ТЗ Н-12: повтор с тем же ключом возвращает ту же сделку
    def create(self, request, *args, **kwargs):
        # ТЗ Н-12: если в запросе указан приход (receipt / receipt_id) — на приход одна сделка;
        # повтор → 409 deal_exists с id. Без прихода в теле поведение прежнее.
        receipt_ref = request.data.get("receipt") or request.data.get("receipt_id")
        if receipt_ref:
            company = self._company()
            try:
                receipt = SupplierReceipt.objects.filter(company=company, id=receipt_ref).first()
            except Exception:  # не uuid
                receipt = None
            if receipt is None:
                raise ValidationError({"code": "receipt_not_found", "detail": "Приход не найден."})
            existing = _find_receipt_deal(company, receipt.supplier_id, receipt.id)
            if existing is not None:
                return Response(
                    {"code": "deal_exists", "detail": "По этому приходу сделка уже создана.", "deal_id": str(existing.id)},
                    status=status.HTTP_409_CONFLICT,
                )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        fresh_instance = serializer.instance
        if receipt_ref and fresh_instance:
            # метка прихода в note (без update_fields через save(): не пересобирать график)
            marker = _receipt_marker(receipt.id)
            ClientDeal.objects.filter(pk=fresh_instance.pk).update(
                note=f"{fresh_instance.note or ''} {marker}".strip()
            )
        if fresh_instance:
            fresh_instance.refresh_from_db()
            output_serializer = self.get_serializer(fresh_instance)
            headers = self.get_success_headers(output_serializer.data)
            return Response(output_serializer.data, status=status.HTTP_201_CREATED, headers=headers)

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        company = getattr(user, "company", None) or getattr(user, "owned_company", None) or self._company()
        branch = self._auto_branch()
        client_id = self.kwargs.get("client_id")

        if not company:
            raise serializers.ValidationError({"company": "У пользователя не задана компания."})


        client = None
        if client_id:
            client_qs = Client.objects.filter(company=company)
            if not _is_owner_like(user):
                client_qs = client_qs.filter(salesperson=user)
            client = get_object_or_404(client_qs, id=client_id)
            if branch is not None and client.branch_id not in (None, branch.id):
                raise serializers.ValidationError({"client": "Клиент другого филиала."})
        else:
            client = serializer.validated_data.get("client")
            if not client or client.company_id != company.id:
                raise serializers.ValidationError({"client": "Клиент не найден в вашей компании."})
            if not _is_owner_like(user) and client.salesperson_id != user.id:
                raise serializers.ValidationError({"client": "Доступ запрещён: это не ваш клиент."})
            if branch is not None and client.branch_id not in (None, branch.id):
                raise serializers.ValidationError({"client": "Клиент другого филиала."})

        # ✅ Защита от дублирования сделок при продаже в долг (если фронтенд вызывает POST /deals/ сразу после checkout):
        kind = serializer.validated_data.get("kind")
        sale = serializer.validated_data.get("sale")
        if kind == ClientDeal.Kind.DEBT and client:
            target_deal = None
            if sale:
                target_deal = ClientDeal.objects.filter(sale=sale).first()
            if not target_deal:
                # Только дубль кассовой сделки (есть sale) на ту же сумму: ручной долг
                # на другую сумму раньше перезаписывал свежий долг с кассы.
                recent_deal = (
                    ClientDeal.objects.filter(
                        client=client,
                        kind=ClientDeal.Kind.DEBT,
                        sale__isnull=False,
                        amount=serializer.validated_data.get("amount"),
                    )
                    .order_by("-created_at")
                    .first()
                )
                if recent_deal and abs((timezone.now() - recent_deal.created_at).total_seconds()) <= 120:
                    target_deal = recent_deal

            if target_deal:
                serializer.update(target_deal, serializer.validated_data.copy())
                serializer.instance = target_deal
                return









        if client_id:
            serializer.save(company=company, branch=branch, client=client)
        else:
            serializer.save(company=company, branch=branch)


# ===== Deals retrieve/update/destroy =====

@read_only_guard("can_edit_clients")
class ClientDealRetrieveUpdateDestroyAPIView(
    CompanyBranchRestrictedMixin,
    generics.RetrieveUpdateDestroyAPIView,
):
    """
    GET    /api/main/clients/<client_id>/deals/<uuid:pk>/
    PATCH  /api/main/clients/<client_id>/deals/<uuid:pk>/
    PUT    /api/main/clients/<client_id>/deals/<uuid:pk>/
    DELETE /api/main/clients/<client_id>/deals/<uuid:pk>/
    """
    serializer_class = ClientDealSerializer

    def get_queryset(self):
        qs = (
            ClientDeal.objects
            .select_related("client")
            .prefetch_related(*_deal_prefetch())
        )
        qs = self._filter_qs_company_branch(qs)
        if not _is_owner_like(self.request.user):
            qs = qs.filter(client__salesperson=self.request.user)

        client_id = self.kwargs.get("client_id")
        if client_id:
            qs = qs.filter(client_id=client_id)

        return qs

    @transaction.atomic
    def perform_update(self, serializer):
        company = self._company()
        branch = self._auto_branch()
        user = self.request.user

        if not company:
            raise serializers.ValidationError({"company": "У пользователя не задана компания."})

        new_client = serializer.validated_data.get("client")
        if new_client:
            if new_client.company_id != company.id:
                raise serializers.ValidationError({"client": "Клиент принадлежит другой компании."})
            if not _is_owner_like(user) and new_client.salesperson_id != user.id:
                raise serializers.ValidationError({"client": "Доступ запрещён: это не ваш клиент."})
            if branch is not None and new_client.branch_id not in (None, branch.id):
                raise serializers.ValidationError({"client": "Клиент другого филиала."})

        deal = serializer.save(company=company, branch=branch)

        # сброс кеша prefetch — чтобы отдать свежие installments/payments
        deal.refresh_from_db()
        serializer.instance = deal


# ===== RESCHEDULE / PROMISED DATE (перенос срока по взносу) =====

@read_only_guard("can_edit_clients")
class ClientDealInstallmentUpdateAPIView(APIView, CompanyBranchRestrictedMixin):
    """
    PATCH /api/main/clients/<client_id>/deals/<deal_id>/installments/<installment_id>/
    PATCH /api/main/deals/<deal_id>/installments/<installment_id>/

    Перенос срока / обещанная дата по взносу согласно debt-reschedule-promise-backend.md (§2).
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, *args, **kwargs):
        client_id = kwargs.get("client_id")
        deal_id = kwargs.get("deal_id") or kwargs.get("pk")
        installment_id = kwargs.get("installment_id")

        deal_qs = self._filter_qs_company_branch(
            ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch())
        ).filter(pk=deal_id)
        if not _is_owner_like(request.user):
            deal_qs = deal_qs.filter(client__salesperson=request.user)

        if client_id:
            deal_qs = deal_qs.filter(client_id=client_id)

        deal = get_object_or_404(deal_qs)

        # 10. Сделка v1 -> 409 deal_not_v2
        if getattr(deal, "schedule_version", "v2") != "v2":
            return Response(
                {"detail": "Сделка версии v1 не поддерживает перенос отдельных взносов.", "code": "deal_not_v2"},
                status=status.HTTP_409_CONFLICT,
            )

        inst_qs = DealInstallment.objects.select_for_update().filter(deal=deal)
        inst = get_object_or_404(inst_qs, id=installment_id)

        # 2. Взнос закрыт полностью -> 409 installment_already_paid
        if inst.paid_on is not None or (inst.paid_amount and inst.paid_amount >= inst.amount):
            return Response(
                {"detail": "Взнос уже полностью оплачен.", "code": "installment_already_paid"},
                status=status.HTTP_409_CONFLICT,
            )

        raw_data = request.data or {}
        has_due_date = "due_date" in raw_data and raw_data["due_date"] is not None
        has_promised_date = "promised_date" in raw_data
        has_note = "note" in raw_data and bool(str(raw_data["note"]).strip())

        # 6. Минимум одно из due_date / promised_date / note обязательно
        if not has_due_date and not has_promised_date and not has_note:
            return Response(
                {"detail": "Необходимо указать хотя бы одно поле для изменения: due_date, promised_date или note.", "code": "nothing_to_change"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        today = timezone.localdate()

        new_due_date = None
        if has_due_date:
            val = raw_data["due_date"]
            if isinstance(val, str):
                new_due_date = parse_date(val)
            elif isinstance(val, (_date, datetime)):
                new_due_date = val.date() if isinstance(val, datetime) else val
            if not new_due_date:
                return Response(
                    {"detail": "Неверный формат даты due_date. Ожидается YYYY-MM-DD.", "code": "invalid_date"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if new_due_date < today:
                return Response(
                    {"detail": "Срок оплаты не может быть раньше сегодняшней даты.", "code": "date_in_past"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        new_promised_date = None
        if has_promised_date:
            val = raw_data["promised_date"]
            if val is not None and val != "":
                if isinstance(val, str):
                    new_promised_date = parse_date(val)
                elif isinstance(val, (_date, datetime)):
                    new_promised_date = val.date() if isinstance(val, datetime) else val
                if not new_promised_date:
                    return Response(
                        {"detail": "Неверный формат даты promised_date. Ожидается YYYY-MM-DD или null.", "code": "invalid_date"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if new_promised_date < today:
                    return Response(
                        {"detail": "Обещанная дата не может быть раньше сегодняшней даты.", "code": "date_in_past"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            else:
                new_promised_date = None

        idem = raw_data.get("idempotency_key")
        idem_uuid = None
        if idem:
            try:
                idem_uuid = UUID(str(idem))
            except Exception:
                return Response(
                    {"detail": "Неверный формат idempotency_key.", "code": "invalid_idempotency_key"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Идемпотентность: повтор с тем же ключом возвращает тот же результат, историю не дублирует
            if DealInstallmentHistory.objects.filter(installment=inst, idempotency_key=idem_uuid).exists():
                fresh = ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch()).get(pk=deal.pk)
                return Response(ClientDealSerializer(fresh, context={"request": request}).data, status=status.HTTP_200_OK)

        old_due = inst.due_date
        old_promised = inst.promised_date
        note = str(raw_data.get("note", "") or "").strip()

        updated_fields = []
        if has_due_date:
            inst.due_date = new_due_date
            updated_fields.append("due_date")

        if has_promised_date:
            inst.promised_date = new_promised_date
            updated_fields.append("promised_date")

        if updated_fields:
            inst.save(update_fields=updated_fields)

        # Сохранение записи в историю
        DealInstallmentHistory.objects.create(
            company=deal.company,
            branch=deal.branch,
            deal=deal,
            installment=inst,
            idempotency_key=idem_uuid,
            created_by=request.user,
            old_due_date=old_due,
            new_due_date=inst.due_date,
            old_promised_date=old_promised,
            new_promised_date=inst.promised_date,
            note=note,
        )

        fresh = ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch()).get(pk=deal.pk)
        return Response(ClientDealSerializer(fresh, context={"request": request}).data, status=status.HTTP_200_OK)


def _supplier_expense_cashflow(request, company, force_strict=False, **kwargs):
    """
    ТЗ Б-18 п. 3 / Б-02: расход при оплате сделки поставщика.
    Кассы нет: новый клиент (или флаг api_strict_2026_10) получает 400 cashbox_unresolvable, платёж откатывается
    (исключение уходит наружу из atomic-вью); старый клиент — как раньше: оплата проходит без расхода.
    """
    from rest_framework.exceptions import ValidationError as _DRFValidationError

    from apps.construction.auto_cashflow import create_auto_cashflow
    from core.api_compat import is_strict

    strict = force_strict or is_strict(request, company)
    try:
        with transaction.atomic():
            return create_auto_cashflow(company=company, **kwargs)
    except _DRFValidationError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code")
        code = getattr(code, "code", None) or str(code) if code is not None else None
        if strict or code != "cashbox_unresolvable":
            raise
        return None


# ===== PAY (создаём DealPayment + обновляем installment) =====

@read_only_guard("can_edit_clients")
class ClientDealPayAPIView(APIView, CompanyBranchRestrictedMixin):
    """
    POST /api/main/deals/<uuid:pk>/pay/
    POST /api/main/clients/<client_id>/deals/<uuid:pk>/pay/

    body:
    {
      "installment_id": "<uuid>" | null,
      "amount": "5000.00" | null,
      "date": "2025-11-10" | null,
      "idempotency_key": "<uuid>",   # ОБЯЗАТЕЛЬНО
      "note": "..." | ""
    }
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        client_id = kwargs.get("client_id")

        deal_qs = self._filter_qs_company_branch(
            ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch())
        ).filter(pk=pk)
        if not _is_owner_like(request.user):
            deal_qs = deal_qs.filter(client__salesperson=request.user)

        if client_id:
            deal_qs = deal_qs.filter(client_id=client_id)

        deal = get_object_or_404(deal_qs)

        if deal.kind != ClientDeal.Kind.DEBT:
            return Response(
                {"detail": "Оплата помесячно доступна только для сделок типа 'debt'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        inp = DealPayInputSerializer(data=request.data)
        inp.is_valid(raise_exception=True)
        data = inp.validated_data

        idem = data["idempotency_key"]
        paid_date = data.get("date") or timezone.localdate()
        note = data.get("note", "") or ""
        amount = data.get("amount", None)

        # идемпотентность
        if DealPayment.objects.filter(deal=deal, idempotency_key=idem).exists():
            fresh = ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch()).get(pk=deal.pk)
            return Response(ClientDealSerializer(fresh, context={"request": request}).data, status=status.HTTP_200_OK)

        inst_qs = DealInstallment.objects.select_for_update().filter(deal=deal)

        inst_id = data.get("installment_id")
        if inst_id:
            inst = get_object_or_404(inst_qs, id=inst_id)
        else:
            inst = (
                inst_qs
                .filter(paid_amount__lt=F("amount"))
                .order_by("number")
                .first()
            )
            if not inst:
                return Response({"detail": "Все взносы уже полностью оплачены."}, status=status.HTTP_400_BAD_REQUEST)

        total = (inst.amount or Decimal("0")).quantize(Decimal("0.01"))
        current_paid = (inst.paid_amount or Decimal("0")).quantize(Decimal("0.01"))
        remaining = (total - current_paid).quantize(Decimal("0.01"))

        if remaining <= 0:
            return Response({"detail": f"Взнос №{inst.number} уже полностью оплачен."}, status=status.HTTP_400_BAD_REQUEST)

        pay_amt = remaining if amount is None else Decimal(str(amount)).quantize(Decimal("0.01"))
        if pay_amt <= 0:
            return Response({"amount": "Сумма оплаты должна быть больше нуля."}, status=status.HTTP_400_BAD_REQUEST)
        if pay_amt > remaining:
            return Response(
                {
                    "amount": f"Сумма оплаты превышает остаток. Максимум: {remaining}.",
                    "code": "amount_exceeds_installment",  # ТЗ Н-13
                    "data": {"max": str(remaining)},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_pm = data.get("payment_method")
        from apps.main.services_debt import normalize_debt_payment_method
        pm = normalize_debt_payment_method(raw_pm)

        # audit
        try:
            deal_payment = DealPayment.objects.create(
                company=deal.company,
                branch=deal.branch,
                deal=deal,
                installment=inst,
                kind=DealPayment.Kind.PAY,
                amount=pay_amt,
                paid_date=paid_date,
                idempotency_key=idem,
                created_by=request.user,
                note=note,
                payment_method=pm,
            )
        except IntegrityError:
            fresh = ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch()).get(pk=deal.pk)
            prior = DealPayment.objects.filter(deal=deal, idempotency_key=idem).first()
            return Response(
                self._with_cashflow_id(ClientDealSerializer(fresh, context={"request": request}).data, prior),
                status=status.HTTP_200_OK,
            )

        # auto-cashflow: customer vs supplier
        is_supplier = False
        if deal.client and (
            getattr(deal.client, "type", None) == Client.StatusClient.SUPPLIERS
            or getattr(deal.client, "type", None) == "suppliers"
        ):
            is_supplier = True

        from apps.construction.auto_cashflow import create_auto_cashflow
        from apps.construction.models import CashFlow

        if is_supplier:
            # ТЗ Н-03/Н-11: «из ящика смены» — расход из кассы этой смены, один.
            drawer_shift = _receipt_drawer_shift(request, deal.company) if (pm or "cash") == "cash" else None
            _supplier_expense_cashflow(
                request, deal.company,
                branch=deal.branch,
                cashbox=drawer_shift.cashbox if drawer_shift else None,
                cashbox_id=data.get("cashbox_id"),
                cashbox_role=data.get("cashbox_role") or "expense_variable",
                user=request.user,
                shift=drawer_shift,
                type=CashFlow.Type.EXPENSE,
                amount=pay_amt,
                source_kind=CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT,
                source_id=str(deal.id),
                # Ключ — платёж, а не «сделка + сумма»: иначе взносы на одинаковую сумму
                # склеиваются в один приход и не попадают в смену (ТЗ ч.12, 2.1).
                idempotency_key=f"deal-pay:{deal_payment.id}",
                name=f"Оплата долга поставщику: {deal.title or (deal.client.full_name if deal.client else 'Сделка')}",
                source_business_operation_id="Оплата долга",
                payment_method=pm,
                affects_shift_drawer=drawer_shift is not None,
            )
        else:
            create_auto_cashflow(
                company=deal.company,
                branch=deal.branch,
                cashbox_id=data.get("cashbox_id"),
                cashbox_role=data.get("cashbox_role") or "pos_branch",
                shift_id=data.get("shift_id"),
                user=request.user,
                type=CashFlow.Type.INCOME,
                amount=pay_amt,
                source_kind=CashFlow.SourceKind.DEBT_REPAYMENT,
                source_id=str(deal.id),
                # Ключ — платёж, а не «сделка + сумма»: иначе взносы на одинаковую сумму
                # склеиваются в один приход и не попадают в смену (ТЗ ч.12, 2.1).
                idempotency_key=f"deal-pay:{deal_payment.id}",
                name=f"Оплата долга: {deal.title or (deal.client.full_name if deal.client else 'Сделка')}",
                source_business_operation_id="Оплата долга",
                payment_method=pm,
                affects_shift_drawer=(pm == "cash"),
            )

        # update installment
        new_paid = (current_paid + pay_amt).quantize(Decimal("0.01"))
        if new_paid >= total:
            inst.paid_amount = total
            inst.paid_on = paid_date
        else:
            inst.paid_amount = new_paid
            inst.paid_on = None
        inst.save(update_fields=["paid_amount", "paid_on"])

        fresh = ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch()).get(pk=deal.pk)
        return Response(
            self._with_cashflow_id(ClientDealSerializer(fresh, context={"request": request}).data, deal_payment),
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _with_cashflow_id(data, payment):
        """ТЗ Н-11: в ответ оплаты добавляется cashflow_id движения денег (расход для поставщика)."""
        from apps.construction.models import CashFlow
        cf = None
        if payment is not None:
            cf = CashFlow.objects.filter(
                company=payment.company, idempotency_key=f"deal-pay:{payment.pk}"
            ).first()
        out = dict(data)
        out["cashflow_id"] = str(cf.id) if cf else None
        return out


@read_only_guard("can_edit_clients")
class ClientDealsPayAnyAPIView(APIView, CompanyBranchRestrictedMixin):
    """Atomically distribute one payment across a client's debt installments.

    Deals are processed by their earliest still-unpaid due date, then by creation
    time.  A separate cashflow is recorded for every affected installment, so the
    audit trail remains linked to the exact DealPayment.
    """
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _serialize_deals(request, deals):
        return ClientDealSerializer(deals, many=True, context={"request": request}).data

    @staticmethod
    def _validate_cashbox_and_shift(request, data):
        """ТЗ Н-13: понятные коды для кассы/смены (cashbox_not_found, shift_closed, shift_not_owned)."""
        from apps.construction.models import CashShift as _Shift

        user = request.user
        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        cashbox_id = data.get("cashbox_id")
        if cashbox_id and company is not None and not Cashbox.objects.filter(id=cashbox_id, company=company).exists():
            return Response(
                {"detail": "Касса не найдена или не принадлежит компании.", "code": "cashbox_not_found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        shift_id = data.get("shift_id")
        if shift_id and company is not None:
            shift = _Shift.objects.filter(id=shift_id, company=company).first()
            if shift is None:
                return Response(
                    {"detail": "Смена не найдена.", "code": "shift_not_found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if shift.status != _Shift.Status.OPEN:
                return Response(
                    {"detail": "Смена закрыта.", "code": "shift_closed"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if shift.cashier_id != user.id and not _is_owner_like(user):
                return Response(
                    {"detail": "Эта смена открыта другим кассиром.", "code": "shift_not_owned"},
                    status=status.HTTP_403_FORBIDDEN,
                )
        return None

    @transaction.atomic
    def post(self, request, client_id, *args, **kwargs):
        from apps.main.services_debt import debt_receipt_info, normalize_debt_payment_method, pick_debt_method
        raw = {k: request.data.get(k) for k in request.data.keys()}
        # ТЗ 29.10 Б-13: способ оплаты — method или payment_method (разные → 400 field_conflict);
        # смена и касса — shift/cashbox наравне с shift_id/cashbox_id
        picked = pick_debt_method(raw)
        if picked:
            raw["payment_method"] = picked
        for src, dst in (("shift", "shift_id"), ("cashbox", "cashbox_id")):
            if raw.get(dst) in (None, "") and raw.get(src) not in (None, ""):
                raw[dst] = raw[src]
        resp = self.pay(request, client_id, raw)
        if resp.status_code == 200 and isinstance(resp.data, dict):
            # ТЗ 29.10 Б-13: дополняем ответ (прежние paid_total/deals остаются)
            pm = normalize_debt_payment_method(picked)
            resp.data.update({
                "paid": resp.data.get("paid_total"),
                "method": pm, "payment_method": pm,
                "replayed": bool(getattr(self, "replayed", False)),
                **debt_receipt_info(
                    request.user, self.cashflows, self.payments_made or self.replay_payments
                ),
            })
        return resp

    def pay(self, request, client_id, raw_data):
        """Распределяет оплату; распределение по сделкам кладёт в self.applied [(deal, amount)]."""
        self.applied = []
        self.payments_made = []  # ТЗ Н-11/Н-13: созданные DealPayment (pay-supplier-debt, received_by)
        self.cashflows = []  # ТЗ Н-13: созданные движения — для shift/cashbox в ответе
        self.replay_payments = []  # ТЗ Н-13: при повторе — прежние DealPayment (кто и когда принял)
        self.replayed = False
        inp = DealPayAnyInputSerializer(data=raw_data)
        inp.is_valid(raise_exception=True)
        data = inp.validated_data
        amount = Decimal(str(data["amount"])).quantize(Decimal("0.01"))
        if amount <= 0:
            return Response(
                {"amount": "Сумма оплаты должна быть больше нуля.", "code": "amount_invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        client_qs = self._filter_qs_company_branch(Client.objects.all())
        # ТЗ Н-31: запрос «на кассу» принимает кассир — ограничение по salesperson не применяем
        if not _is_owner_like(request.user) and not getattr(request, "_pos_request_accept", False):
            client_qs = client_qs.filter(salesperson=request.user)
        client = get_object_or_404(client_qs.select_for_update(), id=client_id)
        idem = data["idempotency_key"]

        # A key is scoped to the client.  This separate operation record is
        # needed because one operation can create several payments in one deal.
        prior_operation = ClientDebtBulkPayment.objects.filter(
            client=client, idempotency_key=idem
        ).first()
        if prior_operation:
            prior_deal_ids = [UUID(str(deal_id)) for deal_id in prior_operation.affected_deal_ids]
            prior_deals = list(
                ClientDeal.objects.filter(id__in=prior_deal_ids)
                .select_related("client")
                .prefetch_related(*_deal_prefetch())
            )
            by_id = {deal.id: deal for deal in prior_deals}
            ordered = [by_id[deal_id] for deal_id in prior_deal_ids if deal_id in by_id]
            # ТЗ Н-13: прежние платежи и движения этой операции (по ключам взносов)
            prior_keys = [
                uuid5(idem, f"{d.id}:{i.id}") for d in prior_deals for i in d.installments.all()
            ]
            self.replay_payments = list(
                DealPayment.objects.filter(deal_id__in=prior_deal_ids, idempotency_key__in=prior_keys)
                .select_related("created_by").order_by("created_at")
            )
            from apps.construction.models import CashFlow as _CF
            self.cashflows = list(_CF.objects.filter(
                company=client.company, source_kind=_CF.SourceKind.DEBT_REPAYMENT,
                source_id__in=[str(p.id) for p in self.replay_payments],
            ).order_by("created_at"))
            self.replayed = True  # ТЗ 29.10 Б-13: повтор отвечаем без проверки смены (она могла закрыться)
            return Response({"paid_total": str(prior_operation.amount), "deals": self._serialize_deals(request, ordered)})

        bad = self._validate_cashbox_and_shift(request, data)
        if bad is not None:
            return bad

        installments = list(
            DealInstallment.objects.select_for_update()
            .select_related("deal", "deal__client")
            .filter(deal__client=client, deal__kind=ClientDeal.Kind.DEBT, paid_amount__lt=F("amount"))
            .order_by("due_date", "deal__created_at", "number")
        )
        if not installments:
            return Response(
                {"detail": "У клиента нет непогашенных долгов.", "code": "no_debt"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        total_remaining = sum((inst.remaining_for_period for inst in installments), Decimal("0.00")).quantize(Decimal("0.01"))
        if amount > total_remaining:
            return Response(
                {
                    "amount": f"Сумма оплаты превышает суммарный остаток. Максимум: {total_remaining}.",
                    "code": "amount_exceeds_debt",
                    "data": {"max": str(total_remaining)},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.main.services_debt import normalize_debt_payment_method
        from core.api_compat import is_strict
        from apps.construction.models import CashShift as _CS
        pm0 = normalize_debt_payment_method(data.get("payment_method"))
        is_supplier = client.type == Client.StatusClient.SUPPLIERS
        # ТЗ 29.10 Б-13: без shift владелец принимает деньги «в руки» — они не попадают ни в одну смену
        # (shift=NULL, касса — «Основная касса компании»). Запрос «на кассу» (pos_requests) и кассир
        # идут как раньше: в свою открытую смену.
        detached = (
            not is_supplier
            and not data.get("shift_id")
            and not getattr(request, "_pos_request_accept", False)
            and _is_owner_like(request.user)
            and _debt_detached_client(request, client.company)
        )
        if (
            not is_supplier and not data.get("shift_id") and not detached and pm0 == "cash"
            and not getattr(request, "_pos_request_accept", False)
            and is_strict(request, client.company)
            and not _CS.objects.filter(company=client.company, cashier=request.user, status=_CS.Status.OPEN).exists()
        ):
            return Response(
                {"detail": "Смена не открыта. Откройте смену, чтобы принять наличные.", "code": "shift_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        operation = ClientDebtBulkPayment.objects.create(
            company=client.company,
            client=client,
            idempotency_key=idem,
            amount=amount,
        )

        from apps.main.services_debt import normalize_debt_payment_method
        from apps.construction.auto_cashflow import create_auto_cashflow
        from apps.construction.models import CashFlow

        pm = normalize_debt_payment_method(data.get("payment_method"))
        paid_date = data.get("date") or timezone.localdate()
        note = data.get("note", "") or ""
        left = amount
        affected_ids = []
        is_supplier = client.type == Client.StatusClient.SUPPLIERS
        for inst in installments:
            if left <= 0:
                break
            pay_amount = min(left, inst.remaining_for_period).quantize(Decimal("0.01"))
            if pay_amount <= 0:
                continue
            deal = inst.deal
            payment = DealPayment.objects.create(
                company=deal.company,
                branch=deal.branch,
                deal=deal,
                installment=inst,
                kind=DealPayment.Kind.PAY,
                amount=pay_amount,
                paid_date=paid_date,
                idempotency_key=uuid5(idem, f"{deal.id}:{inst.id}"),
                created_by=request.user,
                note=note,
                payment_method=pm,
            )
            new_paid = (inst.paid_amount + pay_amount).quantize(Decimal("0.01"))
            inst.paid_amount = min(new_paid, inst.amount)
            inst.paid_on = paid_date if inst.paid_amount >= inst.amount else None
            inst.save(update_fields=["paid_amount", "paid_on"])

            if is_supplier:
                # Долг поставщику - это РАСХОД (как в clients/{id}/deals/{deal}/pay/).
                drawer_shift = _receipt_drawer_shift(request, deal.company) if (pm or "cash") == "cash" else None
                _cf = _supplier_expense_cashflow(
                    request, deal.company,
                    force_strict=getattr(self, "force_strict", False),
                    branch=deal.branch,
                    cashbox=drawer_shift.cashbox if drawer_shift else None,
                    cashbox_id=data.get("cashbox_id"),
                    cashbox_role=data.get("cashbox_role") or "expense_variable",
                    user=request.user,
                    shift=drawer_shift,
                    type=CashFlow.Type.EXPENSE,
                    amount=pay_amount,
                    source_kind=CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT,
                    source_id=str(payment.id),
                    name=f"Оплата долга поставщику: {deal.title or client.full_name}",
                    source_business_operation_id="Оплата долга",
                    payment_method=pm,
                    affects_shift_drawer=drawer_shift is not None,
                )
                if _cf:
                    self.cashflows.append(_cf)
            else:
                _cf = create_auto_cashflow(
                    company=deal.company,
                    branch=deal.branch,
                    cashbox_id=data.get("cashbox_id"),
                    cashbox_role=data.get("cashbox_role") or "pos_branch",
                    shift_id=data.get("shift_id"),
                    user=request.user,
                    type=CashFlow.Type.INCOME,
                    amount=pay_amount,
                    source_kind=CashFlow.SourceKind.DEBT_REPAYMENT,
                    source_id=str(payment.id),
                    name=f"Оплата долга: {deal.title or client.full_name}",
                    source_business_operation_id="Оплата долга",
                    payment_method=pm,
                    affects_shift_drawer=(pm == "cash"),
                    detached=detached,
                )
                if _cf:
                    self.cashflows.append(_cf)
            if deal.id not in affected_ids:
                affected_ids.append(deal.id)
            self.applied.append((deal, pay_amount))
            self.payments_made.append(payment)
            left = (left - pay_amount).quantize(Decimal("0.01"))

        fresh_deals = list(
            ClientDeal.objects.filter(id__in=affected_ids)
            .select_related("client")
            .prefetch_related(*_deal_prefetch())
        )
        by_id = {deal.id: deal for deal in fresh_deals}
        ordered = [by_id[deal_id] for deal_id in affected_ids]
        operation.affected_deal_ids = [str(deal_id) for deal_id in affected_ids]
        operation.save(update_fields=["affected_deal_ids"])
        return Response({"paid_total": str(amount), "deals": self._serialize_deals(request, ordered)})


@read_only_guard("can_edit_clients")
class SupplierDebtPayAPIView(ClientDealsPayAnyAPIView):
    """
    ТЗ Н-11: POST /api/main/clients/<id>/pay-supplier-debt/ — оплата долга поставщику одной суммой.
    Тонкая обёртка над pay-any (гасит от старых взносов к новым, расход на каждый взнос,
    «из ящика» — по shift). Idempotency-Key (заголовок или idempotency_key) обязателен.
    body: {"amount", "method": "cash|transfer", "cashbox?", "shift?", "date?", "comment?"}
    → {paid, left, applied:[{deal, installment, amount}], cashflows:[{id, cashbox, shift, amount}],
       paid_by:{id, name}, replayed}
    """

    force_strict = True  # новый адрес: расход обязателен, кассы нет → 400 cashbox_unresolvable (Б-18)

    @idempotent(body_key=True, replay_flag=True)
    @transaction.atomic
    def post(self, request, client_id, *args, **kwargs):
        from apps.construction.models import CashFlow

        raw_key = request.headers.get("Idempotency-Key") or request.data.get("idempotency_key") or ""
        raw_key = str(raw_key).strip()
        if not raw_key:
            raise ValidationError({"code": "idempotency_key_required", "detail": "Нужен Idempotency-Key."})
        try:
            idem = UUID(raw_key)
        except ValueError:
            idem = uuid5(UUID(int=0), raw_key)

        client = get_object_or_404(self._filter_qs_company_branch(Client.objects.all()), id=client_id)
        if client.type != Client.StatusClient.SUPPLIERS:
            raise ValidationError({"code": "not_supplier", "detail": "Контрагент не поставщик."})

        body = {
            "amount": request.data.get("amount"),
            "idempotency_key": str(idem),
            "payment_method": request.data.get("method") or request.data.get("payment_method") or "cash",
            "date": request.data.get("date"),
            "note": request.data.get("comment") or request.data.get("note") or "",
            "cashbox_id": request.data.get("cashbox") or request.data.get("cashbox_id"),
            "shift_id": request.data.get("shift") or request.data.get("shift_id"),
        }
        body = {k: v for k, v in body.items() if v not in (None, "")}
        resp = self.pay(request, client_id, body)
        if resp.status_code != 200:
            return resp

        payments = list(getattr(self, "payments_made", []))
        # ТЗ Н-31: событие ленты — долг поставщику изменился
        from apps.integrations.events import emit_event as _emit_sup
        _emit_sup(client.company_id, "supplier_debt_changed", {
            "supplier": client.id, "paid": resp.data.get("paid_total"), "actor": request.user.id,
        })
        flows = list(CashFlow.objects.filter(
            source_kind=CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT,
            source_id__in=[str(p.id) for p in payments],
        ))
        user = request.user
        name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip() or getattr(user, "email", "")
        return Response({
            "paid": f"{Decimal(str(resp.data['paid_total'])):.2f}",
            "left": f"{_supplier_debt_total(client.company, client.id):.2f}",
            "applied": [
                {"deal": str(p.deal_id), "installment": str(p.installment_id), "amount": f"{p.amount:.2f}"}
                for p in payments
            ],
            "cashflows": [
                {"id": str(cf.id), "cashbox": str(cf.cashbox_id) if cf.cashbox_id else None,
                 "shift": str(cf.shift_id) if cf.shift_id else None, "amount": f"{cf.amount:.2f}"}
                for cf in flows
            ],
            "paid_by": {"id": str(user.id), "name": name},
            "replayed": False,
        })


# ===== REFUND (создаём DealPayment refund + уменьшаем paid_amount) =====

@read_only_guard("can_edit_clients")
class ClientDealRefundAPIView(APIView, CompanyBranchRestrictedMixin):
    """
    POST /api/main/deals/<uuid:pk>/refund/
    POST /api/main/clients/<client_id>/deals/<uuid:pk>/refund/

    body:
    {
      "installment_id": "<uuid>" | null,
      "amount": "5000.00" | null,      # если null — вернуть всё по взносу
      "date": "2025-11-10" | null,
      "idempotency_key": "<uuid>",     # ОБЯЗАТЕЛЬНО
      "note": "..." | ""
    }
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        client_id = kwargs.get("client_id")

        deal_qs = self._filter_qs_company_branch(
            ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch())
        ).filter(pk=pk)
        if not _is_owner_like(request.user):
            deal_qs = deal_qs.filter(client__salesperson=request.user)

        if client_id:
            deal_qs = deal_qs.filter(client_id=client_id)

        deal = get_object_or_404(deal_qs)

        if deal.kind != ClientDeal.Kind.DEBT:
            return Response(
                {"detail": "Возврат доступен только для сделок типа 'debt'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        inp = DealRefundInputSerializer(data=request.data)
        inp.is_valid(raise_exception=True)
        data = inp.validated_data

        idem = data["idempotency_key"]
        paid_date = data.get("date") or timezone.localdate()
        note = data.get("note", "") or ""
        amount = data.get("amount", None)

        if DealPayment.objects.filter(deal=deal, idempotency_key=idem).exists():
            fresh = ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch()).get(pk=deal.pk)
            return Response(ClientDealSerializer(fresh, context={"request": request}).data, status=status.HTTP_200_OK)

        inst_qs = DealInstallment.objects.select_for_update().filter(deal=deal)

        inst_id = data.get("installment_id")
        if inst_id:
            inst = get_object_or_404(inst_qs, id=inst_id)
        else:
            inst = (
                inst_qs
                .filter(Q(paid_amount__gt=0) | Q(paid_on__isnull=False))
                .order_by("-number")
                .first()
            )
            if not inst:
                return Response({"detail": "Нет оплаченных взносов для возврата."}, status=status.HTTP_400_BAD_REQUEST)

        total = (inst.amount or Decimal("0")).quantize(Decimal("0.01"))
        current_paid = (inst.paid_amount or Decimal("0")).quantize(Decimal("0.01"))

        if current_paid <= 0:
            return Response({"detail": "По этому взносу нечего возвращать."}, status=status.HTTP_400_BAD_REQUEST)

        refund_amt = current_paid if amount is None else Decimal(str(amount)).quantize(Decimal("0.01"))
        if refund_amt <= 0:
            return Response({"amount": "Сумма возврата должна быть больше нуля."}, status=status.HTTP_400_BAD_REQUEST)
        if refund_amt > current_paid:
            return Response({"amount": f"Сумма возврата больше оплаченного. Максимум: {current_paid}."}, status=status.HTTP_400_BAD_REQUEST)

        raw_pm = data.get("payment_method")
        from apps.main.services_debt import normalize_debt_payment_method
        pm = normalize_debt_payment_method(raw_pm)

        try:
            DealPayment.objects.create(
                company=deal.company,
                branch=deal.branch,
                deal=deal,
                installment=inst,
                kind=DealPayment.Kind.REFUND,
                amount=refund_amt,
                paid_date=paid_date,
                idempotency_key=idem,
                created_by=request.user,
                note=note,
                payment_method=pm,
            )
        except IntegrityError:
            fresh = ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch()).get(pk=deal.pk)
            return Response(ClientDealSerializer(fresh, context={"request": request}).data, status=status.HTTP_200_OK)

        new_paid = (current_paid - refund_amt).quantize(Decimal("0.01"))
        inst.paid_amount = new_paid

        # если не полностью оплачен — paid_on не должен стоять
        if new_paid < total:
            inst.paid_on = None

        inst.save(update_fields=["paid_amount", "paid_on"])

        fresh = ClientDeal.objects.select_related("client").prefetch_related(*_deal_prefetch()).get(pk=deal.pk)
        return Response(ClientDealSerializer(fresh, context={"request": request}).data, status=status.HTTP_200_OK)


# ===== Clients with debts =====

class ClientWithDebtsListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    serializer_class = ClientSerializer

    def get_queryset(self):
        qs = self._filter_qs_company_branch(
            Client.objects.select_related("company", "branch").all()
        )
        qs = _filter_clients_visible_for_user(qs, self.request.user)
        # unpaid = paid_on is null (включая частично оплаченные)
        qs = qs.filter(
            deals__kind=ClientDeal.Kind.DEBT,
            deals__installments__paid_on__isnull=True,
        ).distinct()
        # ТЗ Н-12: ?type=clients (по умолчанию, все кроме поставщиков) | suppliers
        kind = (self.request.query_params.get("type") or "clients").strip().lower()
        if kind == "suppliers":
            qs = qs.filter(type=Client.StatusClient.SUPPLIERS)
        elif kind == "clients":
            qs = qs.exclude(type=Client.StatusClient.SUPPLIERS)
        return qs

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        data = response.data
        rows = data.get("results") if isinstance(data, dict) and "results" in data else data
        if not isinstance(rows, list):
            return response
        ids = [r.get("id") for r in rows if isinstance(r, dict) and r.get("id")]
        totals = {}
        if ids:
            for d in ClientDeal.objects.filter(client_id__in=ids, kind=ClientDeal.Kind.DEBT).prefetch_related("installments"):
                rem = d.remaining_debt
                if rem > 0:
                    totals[str(d.client_id)] = totals.get(str(d.client_id), Decimal("0.00")) + rem
        for r in rows:
            if isinstance(r, dict):
                r["debt_total"] = f"{totals.get(str(r.get('id')), Decimal('0.00')):.2f}"
                r["direction"] = "we_owe" if r.get("type") == Client.StatusClient.SUPPLIERS else "they_owe"
        return response
# ===========================
#  Bids & Social Applications
# ===========================
class BidListCreateAPIView(generics.ListCreateAPIView):
    """
    GET  — список заявок (только авторизованные).
    POST — публичная отправка заявки с лендинга (без токена).
    """
    queryset = Bid.objects.all()

    def get_permissions(self):
        if self.request.method == "POST":
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return BidPublicCreateSerializer
        return BidSerializers


class BidRetrieveUpdateDestroyAPIView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = BidSerializers
    permission_classes = [permissions.IsAuthenticated]
    queryset = Bid.objects.all()


class SocialApplicationsListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = SocialApplicationsSerializers
    queryset = SocialApplications.objects.all()


class SocialApplicationsRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = SocialApplicationsSerializers
    queryset = SocialApplications.objects.all()


# ===========================
#  Transaction Records
# ===========================
class TransactionRecordListCreateView(generics.ListCreateAPIView, CompanyBranchRestrictedMixin):
    serializer_class = TransactionRecordSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = TransactionRecordFilter
    search_fields = ["name"]
    ordering_fields = ["date", "amount", "created_at", "id"]
    ordering = ["-date", "-created_at"]

    def get_queryset(self):
        user = self.request.user
        qs = TransactionRecord.objects.select_related("company", "department")
        if user.is_superuser:
            # суперюзер видит всё; но если нужно — можно ограничить по branch
            return qs
        return self._filter_qs_company_branch(qs)

    def perform_create(self, serializer):
        user = self.request.user
        company = _get_company(user)
        department = serializer.validated_data.get("department")

        if not user.is_superuser and not company:
            raise PermissionDenied("Нет прав создавать записи.")

        if company and department and department.company_id != company.id:
            raise PermissionDenied("Отдел принадлежит другой компании.")

        if user.is_superuser and not company and department is None:
            raise PermissionDenied("Укажите отдел, чтобы определить компанию записи.")

        # company/branch подставит миксин; если суперюзер без company — сериализатор подставит из department
        extra = {}
        if company is not None:
            extra["company"] = company
        self._save_with_company_branch(serializer, **extra)


class TransactionRecordRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView, CompanyBranchRestrictedMixin):
    serializer_class = TransactionRecordSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = TransactionRecord.objects.select_related("company", "department")
        if user.is_superuser:
            return qs
        return self._filter_qs_company_branch(qs)


# ===========================
#  Contractor Works
# ===========================
class ContractorWorkListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    """
      GET  /api/main/contractor-works/
      POST /api/main/contractor-works/
    """
    serializer_class = ContractorWorkSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    # department убран из фильтров
    filterset_fields = ["contractor_entity_type", "start_date", "end_date"]
    search_fields = [
        "title",
        "contractor_name",
        "contractor_phone",
        "contractor_entity_name",
        "description",
    ]
    ordering_fields = [
        "created_at",
        "updated_at",
        "amount",
        "start_date",
        "end_date",
        "planned_completion_date",
    ]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = ContractorWork.objects.select_related().all()
        # только company/branch-ограничение
        return self._filter_qs_company_branch(qs)

    @transaction.atomic
    def perform_create(self, serializer):
        # company/branch подставит миксин
        self._save_with_company_branch(serializer)


class ContractorWorkRetrieveUpdateDestroyAPIView(
    CompanyBranchRestrictedMixin,
    generics.RetrieveUpdateDestroyAPIView,
):
    """
    GET    /api/main/contractor-works/<uuid:pk>/
    PATCH  /api/main/contractor-works/<uuid:pk>/
    PUT    /api/main/contractor-works/<uuid:pk>/
    DELETE /api/main/contractor-works/<uuid:pk>/
    """
    serializer_class = ContractorWorkSerializer

    def get_queryset(self):
        qs = ContractorWork.objects.select_related().all()
        return self._filter_qs_company_branch(qs)

    @transaction.atomic
    def perform_update(self, serializer):
        # company/branch подставит миксин
        self._save_with_company_branch(serializer)


# ===========================
#  Debts
# ===========================
@read_only_guard("can_edit_clients")
class DebtListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    """
    GET  /api/main/debts/?search=...&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
    POST /api/main/debts/
    """
    serializer_class = DebtSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = DebtFilter
    search_fields = ["name", "phone"]
    ordering_fields = ["created_at", "updated_at", "amount"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return self._filter_qs_company_branch(
            Debt.objects.select_related("company", "branch").all()
        )


@read_only_guard("can_edit_clients")
class DebtRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET/PATCH/PUT/DELETE /api/main/debts/<uuid:pk>/
    """
    serializer_class = DebtSerializer

    def get_queryset(self):
        return self._filter_qs_company_branch(
            Debt.objects.select_related("company", "branch").all()
        )

    @transaction.atomic
    def perform_update(self, serializer):
        old_amount = serializer.instance.amount
        instance = serializer.save()
        new_amount = instance.amount

        raw_pm = self.request.data.get("payment_method")
        from apps.main.services_debt import normalize_debt_payment_method
        pm = normalize_debt_payment_method(raw_pm)

        if raw_pm is not None or not instance.payment_method:
            instance.payment_method = pm
            instance.save(update_fields=["payment_method"])

        # Если остаток долга уменьшился (погашение)
        if old_amount is not None and new_amount is not None and new_amount < old_amount:
            repaid_amount = (old_amount - new_amount).quantize(Decimal("0.01"))
            if repaid_amount > Decimal("0.00"):
                # ТЗ 12.2: PATCH с меньшей суммой - это погашение. Остаток = amount - оплаты, поэтому
                # после записи платежа возвращаем amount прежним, иначе остаток падал бы дважды.
                instance.amount = old_amount
                instance.save(update_fields=["amount", "updated_at"])
                debt_payment = instance.add_payment(
                    amount=repaid_amount,
                    paid_at=timezone.localdate(),
                    note=f"Погашение долга ({pm})",
                    payment_method=pm,
                )
                cashbox_id = self.request.data.get("cashbox_id")
                cashbox_role = self.request.data.get("cashbox_role") or "pos_main"
                shift_id = self.request.data.get("shift_id")
                from apps.construction.auto_cashflow import create_auto_cashflow
                from apps.construction.models import CashFlow

                create_auto_cashflow(
                    company=instance.company,
                    branch=instance.branch,
                    cashbox_id=cashbox_id,
                    cashbox_role=cashbox_role,
                    shift_id=shift_id,
                    user=self.request.user,
                    type=CashFlow.Type.INCOME,
                    amount=repaid_amount,
                    source_kind=CashFlow.SourceKind.DEBT_REPAYMENT,
                    source_id=str(instance.id),
                    idempotency_key=f"debt-pay:{debt_payment.id}",
                    name=f"Оплата долга: {instance.name or 'Долг'}",
                    source_business_operation_id="Оплата долга",
                    payment_method=pm,
                    affects_shift_drawer=(pm == "cash"),
                )


@read_only_guard("can_edit_clients")
class DebtPayAPIView(APIView, CompanyBranchRestrictedMixin):
    """
    POST /api/main/debts/<uuid:pk>/pay/
    Body: { "amount": "235.00", "paid_at": "2025-09-12", "note": "оплата с карты", "payment_method": "mbank" }
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk):
        # выбираем долг в рамках company/branch по общей логике
        qs = self._filter_qs_company_branch(Debt.objects.all())
        debt = get_object_or_404(qs, pk=pk)

        ser = DebtPaymentSerializer(data=request.data, context={"request": request, "debt": debt})
        ser.is_valid(raise_exception=True)

        raw_pm = request.data.get("payment_method")
        from apps.main.services_debt import normalize_debt_payment_method
        pm = normalize_debt_payment_method(raw_pm)

        from apps.warehouse.services import ensure_period_open_api
        ensure_period_open_api(
            company=debt.company,
            dates=[ser.validated_data.get("paid_at") or timezone.localdate()],
            user=request.user,
        )

        payment = DebtPayment.objects.create(
            company=debt.company,
            branch=debt.branch,
            debt=debt,
            amount=ser.validated_data["amount"],
            paid_at=ser.validated_data.get("paid_at") or timezone.localdate(),
            note=ser.validated_data.get("note", ""),
            payment_method=pm,
        )
        debt.payment_method = pm
        debt.save(update_fields=["payment_method"])

        cashbox_id = request.data.get("cashbox_id")
        cashbox_role = request.data.get("cashbox_role") or "pos_main"
        branch_id = request.data.get("branch_id") or (debt.branch_id if debt.branch_id else None)
        shift_id = request.data.get("shift_id")
        from apps.construction.auto_cashflow import create_auto_cashflow
        from apps.construction.models import CashFlow

        create_auto_cashflow(
            company=debt.company,
            branch=debt.branch,
            cashbox_id=cashbox_id,
            cashbox_role=cashbox_role,
            shift_id=shift_id,
            user=request.user,
            type=CashFlow.Type.INCOME,
            amount=ser.validated_data["amount"],
            source_kind=CashFlow.SourceKind.DEBT_REPAYMENT,
            source_id=str(debt.id),
            idempotency_key=f"debt-pay:{payment.id}",
            name=f"Оплата долга: {debt.name or 'Долг'}",
            source_business_operation_id="Оплата долга",
            payment_method=pm,
            affects_shift_drawer=(pm == "cash"),
        )

        return Response(DebtSerializer(debt, context={"request": request}).data, status=status.HTTP_201_CREATED)


class DebtPaymentListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    GET /api/main/debts/<uuid:pk>/payments/?date_from=&date_to=
    """
    serializer_class = DebtPaymentSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = DebtPaymentFilter
    ordering_fields = ["paid_at", "created_at", "amount"]
    ordering = ["-paid_at", "-created_at"]

    def get_queryset(self):
        # платежи конкретного долга в рамках компании/филиала
        debt_qs = self._filter_qs_company_branch(
            Debt.objects.select_related("company", "branch").all()
        )
        debt = get_object_or_404(debt_qs, pk=self.kwargs["pk"])
        return DebtPayment.objects.filter(company=debt.company, debt=debt)


# ===========================
#  Object items / sales
# ===========================
class ObjectItemListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = ObjectItemSerializer
    queryset = ObjectItem.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "description"]
    ordering_fields = ["date", "created_at", "updated_at", "price", "quantity", "name"]
    ordering = ["-date", "-created_at"]


class ObjectItemRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ObjectItemSerializer
    queryset = ObjectItem.objects.all()


class ObjectSaleListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = ObjectSaleSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["note", "client__full_name", "client__phone"]
    ordering_fields = ["sold_at", "created_at", "subtotal", "status"]
    ordering = ["-sold_at", "-created_at"]

    def get_queryset(self):
        qs = ObjectSale.objects.select_related("client").prefetch_related("items").all()
        return self._filter_qs_company_branch(qs)

    def perform_create(self, serializer):
        client = serializer.validated_data.get("client")
        if client.company_id != self._company().id:
            raise serializers.ValidationError({"client": "Клиент принадлежит другой компании."})
        self._save_with_company_branch(serializer)


class ObjectSaleRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ObjectSaleSerializer

    def get_queryset(self):
        qs = ObjectSale.objects.select_related("client").prefetch_related("items").all()
        return self._filter_qs_company_branch(qs)


class ObjectSaleAddItemAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/object-sales/<uuid:sale_id>/items/
    Body:
      { "object_item": "<uuid>", "unit_price": "200.00", "quantity": 2 }
    """
    def post(self, request, sale_id):
        sale_qs = self._filter_qs_company_branch(ObjectSale.objects.all())
        sale = get_object_or_404(sale_qs, id=sale_id)

        ser = ObjectSaleItemSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)

        obj_qs = self._filter_qs_company_branch(ObjectItem.objects.all())
        obj = get_object_or_404(obj_qs, id=ser.validated_data["object_item"].id)

        item = ObjectSaleItem.objects.create(
            sale=sale,
            object_item=obj,
            name_snapshot=obj.name,
            unit_price=ser.validated_data.get("unit_price") or obj.price,
            quantity=ser.validated_data["quantity"],
        )
        sale.recalc()
        return Response(ObjectSaleItemSerializer(item).data, status=status.HTTP_201_CREATED)


# ===========================
#  ItemMake
# ===========================
class ItemListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    """
    GET  /api/main/items-make/
    POST /api/main/items-make/
    """
    serializer_class = ItemMakeSerializer
    queryset = ItemMake.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "supplier__full_name", "products__name"]
    filterset_fields = ["unit", "price", "quantity", "products", "supplier", "kind", "needs_processing"]
    ordering_fields = ["created_at", "updated_at", "price", "quantity", "name"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = super().get_queryset().select_related("source", "supplier")
        qs = self._filter_qs_company_branch(qs).distinct()

        req = self.request
        for_recipe = req.query_params.get("for_recipe") if req else None
        if for_recipe in ("1", "true", "True", "yes"):
            qs = qs.filter(item_make_recipe_ready_filter())

        return qs

    def perform_create(self, serializer):
        # company/branch проставит миксин
        super().perform_create(serializer)

        # Новая позиция сырья с поставщиком — это закупка: пишем в историю поставщика.
        item = serializer.instance
        record_supplier_purchase(
            company=item.company,
            branch=item.branch,
            supplier=item.supplier,
            item_make=item,
            quantity=item.quantity,
            unit=item.unit or "",
            unit_price=item.price,
            payment_type=_parse_payment_type(self.request.data.get("payment_type")),
            created_by=self.request.user,
        )


class ItemRetrieveUpdateDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ItemMakeSerializer
    queryset = ItemMake.objects.all()

    def get_queryset(self):
        qs = super().get_queryset().select_related("source", "supplier")
        return qs


class ItemMakePurchaseAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/items-make/<uuid:pk>/purchase/

    Докупка существующего сырья: увеличивает остаток и пишет строку в историю
    закупок поставщика. Body: {quantity, unit_price?, payment_type?, supplier?}

    Отдельный эндпоинт, а не PATCH: правка карточки (в т.ч. ручная корректировка
    остатка) не должна попадать в историю закупок.
    """

    @transaction.atomic
    def post(self, request, pk=None):
        company = self._company()
        qs = self._filter_qs_company_branch(ItemMake.objects.all())
        item = get_object_or_404(qs.select_for_update(), pk=pk)

        try:
            quantity = Decimal(str(request.data.get("quantity")))
        except (TypeError, ValueError, InvalidOperation):
            return Response({"quantity": "Обязательное поле (число > 0)."}, status=status.HTTP_400_BAD_REQUEST)
        if quantity <= 0:
            return Response({"quantity": "Количество должно быть больше нуля."}, status=status.HTTP_400_BAD_REQUEST)

        raw_price = request.data.get("unit_price", request.data.get("price"))
        if raw_price in (None, ""):
            unit_price = Decimal(str(item.price or 0))
        else:
            try:
                unit_price = Decimal(str(raw_price))
            except (TypeError, ValueError, InvalidOperation):
                return Response({"unit_price": "Неверный формат цены."}, status=status.HTTP_400_BAD_REQUEST)
            if unit_price < 0:
                return Response({"unit_price": "Цена не может быть отрицательной."}, status=status.HTTP_400_BAD_REQUEST)

        supplier = item.supplier
        supplier_id = request.data.get("supplier")
        if supplier_id:
            supplier = get_object_or_404(Client, id=supplier_id, company=company)

        item.quantity = Decimal(str(item.quantity or 0)) + quantity
        update_fields = ["quantity", "updated_at"]
        if raw_price not in (None, ""):
            item.price = unit_price.quantize(_Q2, rounding=ROUND_HALF_UP)
            update_fields.append("price")
        if supplier is not None and supplier.id != item.supplier_id:
            item.supplier = supplier
            update_fields.append("supplier")
        item.save(update_fields=update_fields)

        record_supplier_purchase(
            company=company,
            branch=item.branch,
            supplier=supplier,
            item_make=item,
            quantity=quantity,
            unit=item.unit or "",
            unit_price=unit_price,
            payment_type=_parse_payment_type(request.data.get("payment_type")),
            created_by=request.user,
        )

        return Response(ItemMakeSerializer(item, context={"request": request}).data, status=status.HTTP_200_OK)


class ItemMakeProcessAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/items-make/<uuid:pk>/process/

    Обработка сырья: списание input_quantity с исходного сырья и зачисление
    output_quantity в обработанную позицию (с пересчётом цены за единицу).
    """

    @transaction.atomic
    def post(self, request, pk=None):
        company = self._company()
        qs = self._filter_qs_company_branch(ItemMake.objects.select_related("source"))
        source = get_object_or_404(qs, pk=pk)

        ser = ItemMakeProcessSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        vd = ser.validated_data

        target = None
        target_id = vd.get("target_item_make_id")
        if target_id:
            target = qs.filter(pk=target_id, kind=ItemMake.Kind.PROCESSED).first()
            if not target:
                return Response(
                    {"target_item_make_id": "Обработанное сырьё не найдено."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            source, processed = process_raw_item_make(
                source,
                input_quantity=vd["input_quantity"],
                output_quantity=vd["output_quantity"],
                name=(vd.get("name") or "").strip() or None,
                processing_cost=vd.get("processing_cost") or Decimal("0"),
                target=target,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        ctx = {"request": request}
        return Response(
            {
                "source": ItemMakeSerializer(source, context=ctx).data,
                "processed": ItemMakeSerializer(processed, context=ctx).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ===========================
#  Supplier (Client.type=suppliers) -> products -> receipt (оприходование)
# ===========================
class SupplierListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    GET /api/main/suppliers/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ClientSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["full_name", "phone", "llc", "inn"]
    ordering_fields = ["created_at", "full_name"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = Client.objects.filter(type=Client.StatusClient.SUPPLIERS)
        return self._filter_qs_company_branch(qs)


class SupplierProductsListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    GET /api/main/suppliers/<uuid:supplier_id>/products/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ProductListSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "barcode", "article", "code"]
    ordering_fields = ["created_at", "updated_at", "name", "price", "quantity"]
    ordering = ["-created_at"]

    def get_queryset(self):
        supplier_id = self.kwargs.get("supplier_id")
        sup_qs = self._filter_qs_company_branch(Client.objects.all())
        supplier = get_object_or_404(sup_qs, id=supplier_id, type=Client.StatusClient.SUPPLIERS)

        prod_qs = self._filter_qs_company_branch(Product.objects.all())
        return prod_qs.filter(Q(suppliers=supplier) | Q(client_id=supplier.id)).distinct()


class SupplierRecommendationsAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/suppliers/<uuid:supplier_id>/recommendations/
    Маркет — Рекомендуемый заказ

    ТЗ Н-30. Параметры: analysis_days (7|14|30, по умолчанию 30), coverage_days (целое >= 0, 7),
    safety_days (целое >= 0, 2), search, only_needed=true (синоним only_required: только строки с
    recommended_quantity > 0), critical=true (только critical и out_of_stock), stock_status
    (critical|ending|low|ok|no_sales|out_of_stock), ordering, page, page_size (<= 500).
    Ошибка параметра: 400 {code: "invalid_param", detail, field}.

    Правила stock_forecast.status (пороги зашиты в коде, от настроек компании не зависят;
    avg_per_day = продажи за analysis_days / analysis_days):
      no_sales     — продаж за период нет (avg_per_day = 0), days_remaining = null;
      out_of_stock — есть продажи, остаток <= 0;
      critical     — остатка хватит меньше чем на 2 дня (days_remaining < 2);
      ending       — хватит на 2..4 дня (days_remaining < 4);
      low          — остатка хватает на 4+ дней, но до целевого запаса
                     (avg_per_day * (coverage_days + safety_days)) не хватает (recommended_quantity > 0);
      ok           — остатка достаточно.
    Для совместимости critical_products_count и фильтры critical=true / stock_status=critical
    включают и out_of_stock.

    Количество к заказу: недостача до целевого запаса. Штучный товар округляется ВВЕРХ до целого, а при
    pack_size > 1 (кратность упаковки) — вверх до кратного pack_size. Весовой товар: вверх до 0.001
    (кратность упаковки не применяется).
    Порядок строк по умолчанию (и при ordering=urgency): out_of_stock, critical, ending, low, ok,
    no_sales; внутри — days_remaining по возрастанию, затем название и id (порядок стабильный).
    ordering: urgency, name, days_remaining, recommended_quantity, current_stock, estimated_cost;
    «-» перед именем — по убыванию. summary.estimated_total — всегда число (0, если нечего заказывать).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, supplier_id, *args, **kwargs):
        import math
        from datetime import timedelta

        qp = request.query_params

        # 1. Валидация параметров (ТЗ Н-30: единый код invalid_param)
        def _bad(field, detail, allowed=None):
            body = {"detail": detail, "code": "invalid_param", "field": field}  # Б-11: detail первым
            if allowed is not None:
                body["data"] = {"allowed": allowed}
            return Response(body, status=status.HTTP_400_BAD_REQUEST)

        def _int_param(field, default):
            raw = qp.get(field, default)
            raw = str(default) if raw is None or str(raw).strip() == "" else str(raw).strip()
            if not raw.lstrip("-").isdigit():
                return None, _bad(field, f"{field}: нужно целое число >= 0.")
            val = int(raw)
            if val < 0:
                return None, _bad(field, f"{field} не может быть отрицательным.")
            return val, None

        analysis_days, err = _int_param("analysis_days", "30")
        if err:
            return err
        if analysis_days not in (7, 14, 30):
            return _bad("analysis_days", "analysis_days должен быть 7, 14 или 30.", allowed=[7, 14, 30])
        coverage_days, err = _int_param("coverage_days", "7")
        if err:
            return err
        safety_days, err = _int_param("safety_days", "2")
        if err:
            return err

        raw_page_size = qp.get("page_size", "500")
        try:
            page_size = int(raw_page_size)
            if page_size <= 0:
                page_size = 500
            elif page_size > 500:
                return Response(
                    {"detail": "page_size не может быть больше 500."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except (ValueError, TypeError):
            return Response(
                {"detail": "Некорректное значение page_size."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_page = qp.get("page", "1")
        try:
            page = int(raw_page)
            if page <= 0:
                page = 1
        except (ValueError, TypeError):
            page = 1

        search = (qp.get("search") or "").strip()
        only_required = (
            (qp.get("only_required") or "").strip().lower() in ("true", "1")
            or (qp.get("only_needed") or "").strip().lower() in ("true", "1")
        )
        critical_only = (qp.get("critical") or "").strip().lower() in ("true", "1")
        ordering_raw = (qp.get("ordering") or "").strip() or "urgency"
        ordering_key = ordering_raw.lstrip("-")
        if ordering_key not in ("urgency", "name", "days_remaining", "recommended_quantity", "current_stock", "estimated_cost"):
            return _bad("ordering", "ordering: urgency, name, days_remaining, recommended_quantity, current_stock, estimated_cost (с «-» — по убыванию).")
        stock_status_filter = (qp.get("stock_status") or "").strip().lower()
        if stock_status_filter and stock_status_filter not in ("critical", "ending", "low", "ok", "no_sales", "out_of_stock"):
            return _bad("stock_status", "stock_status: critical, ending, low, ok, no_sales, out_of_stock.")

        # 2. Поставщик и компания
        sup_qs = self._filter_qs_company_branch(Client.objects.all())
        supplier = get_object_or_404(sup_qs, id=supplier_id, type=Client.StatusClient.SUPPLIERS)
        company = self._company()

        # 3. Товары поставщика
        prod_qs = self._filter_qs_company_branch(Product.objects.all())
        prod_qs = prod_qs.filter(
            Q(suppliers=supplier) | Q(client_id=supplier.id)
        ).exclude(kind=Product.Kind.SERVICE).distinct()

        if search:
            prod_qs = prod_qs.filter(
                Q(name__icontains=search)
                | Q(barcode__icontains=search)
                | Q(article__icontains=search)
                | Q(code__icontains=search)
                | Q(alternate_barcodes__barcode__icontains=search)
                | Q(alternate_barcodes__name__icontains=search)
            ).distinct()

        all_products = list(prod_qs)
        product_ids = [p.id for p in all_products]

        # 4. Временные окна (Asia/Bishkek, UTC+6)
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo("Asia/Bishkek")
        except Exception:
            import datetime as dt
            tz = dt.timezone(dt.timedelta(hours=6))

        now_local = datetime.now(tz)
        today_local = now_local.date()

        def get_window(days_count, offset_days=0):
            end_date = today_local - timedelta(days=offset_days)
            start_date = end_date - timedelta(days=days_count - 1)
            start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=tz)
            end_dt = datetime.combine(end_date, datetime.max.time(), tzinfo=tz)
            return start_dt, end_dt

        cur_7_start, cur_7_end = get_window(7, 0)
        cur_14_start, cur_14_end = get_window(14, 0)
        cur_30_start, cur_30_end = get_window(30, 0)

        prev_7_start, prev_7_end = get_window(7, 7)
        prev_14_start, prev_14_end = get_window(14, 14)
        prev_30_start, prev_30_end = get_window(30, 30)

        max_past_start = prev_30_start

        # 5. Агрегация продаж
        sales_agg = {}
        if product_ids:
            sale_items = SaleItem.objects.filter(
                product_id__in=product_ids,
                sale__status__in=[Sale.Status.PAID, Sale.Status.DEBT],
            )
            if company:
                sale_items = sale_items.filter(sale__company=company)

            branch_id_param = qp.get("branch_id")
            if branch_id_param:
                sale_items = sale_items.filter(sale__branch_id=branch_id_param)

            sale_items = sale_items.annotate(
                sale_time=Coalesce("sale__paid_at", "sale__created_at"),
                eff_qty=Case(
                    When(
                        sale_package__isnull=False,
                        sale_package__quantity_in_package__gt=0,
                        then=ExpressionWrapper(
                            F("quantity") / F("sale_package__quantity_in_package"),
                            output_field=DecimalField(max_digits=12, decimal_places=3),
                        ),
                    ),
                    default=F("quantity"),
                    output_field=DecimalField(max_digits=12, decimal_places=3),
                ),
            ).filter(sale_time__gte=max_past_start)

            agg_qs = sale_items.values("product_id").annotate(
                cur_7=Sum(Case(When(sale_time__gte=cur_7_start, sale_time__lte=cur_7_end, then=F("eff_qty")), default=V(0), output_field=DecimalField())),
                cur_14=Sum(Case(When(sale_time__gte=cur_14_start, sale_time__lte=cur_14_end, then=F("eff_qty")), default=V(0), output_field=DecimalField())),
                cur_30=Sum(Case(When(sale_time__gte=cur_30_start, sale_time__lte=cur_30_end, then=F("eff_qty")), default=V(0), output_field=DecimalField())),
                prev_7=Sum(Case(When(sale_time__gte=prev_7_start, sale_time__lte=prev_7_end, then=F("eff_qty")), default=V(0), output_field=DecimalField())),
                prev_14=Sum(Case(When(sale_time__gte=prev_14_start, sale_time__lte=prev_14_end, then=F("eff_qty")), default=V(0), output_field=DecimalField())),
                prev_30=Sum(Case(When(sale_time__gte=prev_30_start, sale_time__lte=prev_30_end, then=F("eff_qty")), default=V(0), output_field=DecimalField())),
            )

            for row in agg_qs:
                sales_agg[row["product_id"]] = {
                    "last_7_days": float(row["cur_7"] or 0),
                    "last_14_days": float(row["cur_14"] or 0),
                    "last_30_days": float(row["cur_30"] or 0),
                    "previous_7_days": float(row["prev_7"] or 0),
                    "previous_14_days": float(row["prev_14"] or 0),
                    "previous_30_days": float(row["prev_30"] or 0),
                }

        # 6. Упаковки
        pack_size_map = {}
        if product_ids:
            pkgs = ProductPackage.objects.filter(product_id__in=product_ids, quantity_in_package__gt=1)
            for pkg in pkgs:
                if pkg.product_id not in pack_size_map:
                    pack_size_map[pkg.product_id] = float(pkg.quantity_in_package)
            from apps.main.tz26.hw_units.receipts import default_receipt_pack_sizes

            pack_size_map.update(default_receipt_pack_sizes(product_ids))  # ТЗ 26.10 Б-53 п.5

        # 7. Последняя закупка
        last_purchase_map = {}
        if product_ids:
            rcpt_items = (
                SupplierReceiptItem.objects.filter(
                    receipt__supplier=supplier,
                    product_id__in=product_ids,
                )
                .select_related("receipt")
                .order_by("product_id", "-receipt__created_at")
            )
            if company:
                rcpt_items = rcpt_items.filter(receipt__company=company)

            for item in rcpt_items:
                if item.product_id not in last_purchase_map:
                    last_purchase_map[item.product_id] = {
                        "date": item.receipt.created_at.isoformat() if item.receipt and item.receipt.created_at else None,
                        "quantity": float(item.qty or 0),
                        "purchase_price": float(item.purchase_price) if item.purchase_price is not None else None,
                    }

        # 8. Формирование строк рекомендации
        items_result = []
        recommended_products_count = 0
        critical_products_count = 0
        estimated_total_sum = Decimal("0")
        has_estimated_total_price = False

        for prod in all_products:
            p_id = prod.id
            s_data = sales_agg.get(p_id, {
                "last_7_days": 0.0,
                "last_14_days": 0.0,
                "last_30_days": 0.0,
                "previous_7_days": 0.0,
                "previous_14_days": 0.0,
                "previous_30_days": 0.0,
            })

            cur_sales = s_data.get(f"last_{analysis_days}_days", 0.0)
            prev_sales = s_data.get(f"previous_{analysis_days}_days", 0.0)

            avg_per_day = (cur_sales / analysis_days) if analysis_days > 0 else 0.0

            if prev_sales == 0 and cur_sales == 0:
                trend_percent = 0.0
            elif prev_sales == 0 and cur_sales > 0:
                trend_percent = None
            else:
                trend_percent = round(((cur_sales - prev_sales) / prev_sales) * 100, 2)

            sales_payload = {
                "last_7_days": s_data["last_7_days"],
                "last_14_days": s_data["last_14_days"],
                "last_30_days": s_data["last_30_days"],
                "previous_7_days": s_data["previous_7_days"],
                "previous_14_days": s_data["previous_14_days"],
                "previous_30_days": s_data["previous_30_days"],
                "previous_period": prev_sales,
                "average_per_day": round(avg_per_day, 2),
                "trend_percent": trend_percent,
            }

            stock = float(max(Decimal("0"), prod.quantity or Decimal("0")))

            if avg_per_day <= 0:
                days_remaining = None
            else:
                days_remaining = round(stock / avg_per_day, 2)

            req_stock = avg_per_day * (coverage_days + safety_days)
            raw_rec = max(0.0, req_stock - stock)

            is_weight = bool(prod.is_weight)
            pack_size = pack_size_map.get(p_id)

            if is_weight:
                # ТЗ Н-30: весовой товар — вверх до 0.001 (упаковка не применяется)
                rec_qty = float(Decimal(str(round(raw_rec, 9))).quantize(Decimal("0.001"), rounding=ROUND_CEILING))
            else:
                raw_ceil = math.ceil(raw_rec)
                if pack_size and pack_size > 1:
                    rec_qty = math.ceil(raw_rec / pack_size) * pack_size
                else:
                    rec_qty = raw_ceil

            # ТЗ Н-30: no_sales / out_of_stock (правила — в докстринге)
            if avg_per_day <= 0:
                st_status = "no_sales"
            elif stock <= 0:
                st_status = "out_of_stock"
            elif days_remaining is not None and days_remaining < 2:
                st_status = "critical"
            elif days_remaining is not None and days_remaining < 4:
                st_status = "ending"
            elif rec_qty > 0:
                st_status = "low"
            else:
                st_status = "ok"

            if st_status in ("critical", "out_of_stock"):
                critical_products_count += 1

            p_price = float(prod.purchase_price) if prod.purchase_price is not None else None

            if rec_qty > 0:
                recommended_products_count += 1
                if p_price is not None and p_price > 0:
                    estimated_total_sum += Decimal(str(rec_qty)) * Decimal(str(p_price))
                    has_estimated_total_price = True

            last_pur = last_purchase_map.get(p_id)

            row_data = {
                "product_id": str(p_id),
                "product_name": prod.name,
                "barcode": prod.barcode or None,
                "article": prod.article or None,
                "code": prod.code or None,
                "unit": prod.unit or "шт",
                "is_weight": is_weight,
                "scale_type": "weight" if is_weight else "piece",
                "current_stock": stock,
                "purchase_price": p_price,
                "pack_size": pack_size,
                "sales": sales_payload,
                "stock_forecast": {
                    "days_remaining": days_remaining,
                    "status": st_status,
                },
                "last_purchase": last_pur,
                "recommended_quantity": rec_qty,
            }

            if only_required and rec_qty <= 0:
                continue
            if stock_status_filter == "critical":
                if st_status not in ("critical", "out_of_stock"):
                    continue
            elif stock_status_filter and st_status != stock_status_filter:
                continue
            if critical_only and st_status not in ("critical", "out_of_stock"):
                continue

            _cost = (Decimal(str(rec_qty)) * Decimal(str(p_price))) if (rec_qty > 0 and p_price) else Decimal("0")
            row_data["_sort"] = (None, None, _cost)
            items_result.append(row_data)

        # ТЗ Н-30: стабильный порядок. Базово — срочность, затем остаток дней, имя, id.
        _urgency_order = {"out_of_stock": 0, "critical": 1, "ending": 2, "low": 3, "ok": 4, "no_sales": 5}
        items_result.sort(key=lambda r: (str(r["product_name"] or "").lower(), r["product_id"]))
        _desc = ordering_raw.startswith("-")
        if ordering_key == "urgency":
            items_result.sort(key=lambda r: (
                _urgency_order[r["stock_forecast"]["status"]],
                r["stock_forecast"]["days_remaining"] if r["stock_forecast"]["days_remaining"] is not None else 10**9,
            ), reverse=_desc)
        elif ordering_key == "days_remaining":
            items_result.sort(key=lambda r: r["stock_forecast"]["days_remaining"] if r["stock_forecast"]["days_remaining"] is not None else 10**9, reverse=_desc)
        elif ordering_key == "name":
            items_result.sort(key=lambda r: str(r["product_name"] or "").lower(), reverse=_desc)
        elif ordering_key == "recommended_quantity":
            items_result.sort(key=lambda r: r["recommended_quantity"], reverse=_desc)
        elif ordering_key == "current_stock":
            items_result.sort(key=lambda r: r["current_stock"], reverse=_desc)
        elif ordering_key == "estimated_cost":
            items_result.sort(key=lambda r: r["_sort"][2], reverse=_desc)
        for r in items_result:
            r.pop("_sort", None)

        # 9. Пагинация
        total_count = len(items_result)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_results = items_result[start_idx:end_idx]

        base_url = request.build_absolute_uri(request.path)
        def make_page_url(p_num):
            params = request.query_params.copy()
            params["page"] = str(p_num)
            return f"{base_url}?{params.urlencode()}"

        next_url = make_page_url(page + 1) if end_idx < total_count else None
        prev_url = make_page_url(page - 1) if page > 1 and start_idx < total_count else None

        estimated_total = float(estimated_total_sum) if has_estimated_total_price else 0
        supplier_name = supplier.full_name or supplier.llc or str(supplier)

        response_data = {
            "supplier": {
                "id": str(supplier.id),
                "name": supplier_name,
            },
            "settings": {
                "analysis_days": analysis_days,
                "coverage_days": coverage_days,
                "safety_days": safety_days,
            },
            "summary": {
                "products_count": len(all_products),
                "recommended_products_count": recommended_products_count,
                "critical_products_count": critical_products_count,
                "estimated_total": estimated_total,
            },
            "count": total_count,
            "page": page,
            "page_size": page_size,
            "next": next_url,
            "previous": prev_url,
            "results": paginated_results,
        }

        return Response(response_data, status=status.HTTP_200_OK)


class SupplierPurchasesListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    GET /api/main/suppliers/<uuid:supplier_id>/purchases/
    История закупок у поставщика. Фильтры: date_from, date_to (включительно).
    summary — по всему периоду с учётом фильтров, не по странице.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = SupplierPurchaseReadSerializer
    pagination_class = LimitOffsetPagination

    def get_queryset(self):
        supplier_id = self.kwargs.get("supplier_id")
        sup_qs = self._filter_qs_company_branch(Client.objects.all())
        # Не поставщик или чужая компания → 404.
        supplier = get_object_or_404(sup_qs, id=supplier_id, type=Client.StatusClient.SUPPLIERS)

        qs = SupplierPurchase.objects.filter(company=supplier.company_id, supplier=supplier)

        qp = self.request.query_params
        df_raw = (qp.get("date_from") or "").strip()
        if df_raw:
            df = parse_date(df_raw)
            if df is None:
                raise ValidationError({"date_from": ["Некорректная дата."]})
            qs = qs.filter(purchased_at__date__gte=df)

        dt_raw = (qp.get("date_to") or "").strip()
        if dt_raw:
            dt = parse_date(dt_raw)
            if dt is None:
                raise ValidationError({"date_to": ["Некорректная дата."]})
            qs = qs.filter(purchased_at__date__lte=dt)

        return qs.order_by("-purchased_at", "-id")

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())

        money = DecimalField(max_digits=14, decimal_places=2)
        agg = qs.aggregate(
            total_amount=Coalesce(
                Sum("amount", output_field=money),
                V(Decimal("0.00"), output_field=money),
            ),
        )
        summary = {
            "total_amount": str(agg["total_amount"]),
            "total_count": qs.count(),
        }

        page = self.paginate_queryset(qs)
        if page is not None:
            response = self.get_paginated_response(self.get_serializer(page, many=True).data)
            response.data["summary"] = summary
            return response

        return Response({
            "count": summary["total_count"],
            "summary": summary,
            "results": self.get_serializer(qs, many=True).data,
        })


def _supplier_receipt_line_total_expr():
    return ExpressionWrapper(
        F("qty") * Coalesce(
            F("purchase_price"),
            V(Decimal("0"), output_field=DecimalField(max_digits=11, decimal_places=3)),
        ),
        output_field=DecimalField(max_digits=20, decimal_places=3),
    )


def _aggregate_supplier_receipts_total_amount(receipt_qs):
    total = (
        SupplierReceiptItem.objects.filter(receipt__in=receipt_qs.values("pk"))
        .aggregate(total=Sum(_supplier_receipt_line_total_expr()))["total"]
    )
    if total is None:
        return Decimal("0")
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class SupplierReceiptLimitPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "limit"
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        page_size = self.get_page_size(request)
        if not page_size:
            return None

        paginator = self.django_paginator_class(queryset, page_size)
        page_number = request.query_params.get(self.page_query_param, 1)
        try:
            page_number = int(page_number)
        except (TypeError, ValueError):
            page_number = 1
        if page_number < 1:
            page_number = 1

        try:
            self.page = paginator.page(page_number)
        except InvalidPage:
            if paginator.count and page_number > paginator.num_pages:
                # ТЗ ч.12, 2.10: за последней страницей — пустой список, а не повтор последней
                # (иначе клиент, читающий «пока есть строки», зацикливается).
                from django.core.paginator import Page
                self.page = Page([], page_number, paginator)
            else:
                self.page = paginator.page(1)

        self.request = request
        return list(self.page)

    def _beyond_last(self):
        return self.page.number > self.page.paginator.num_pages

    def get_next_link(self):
        # ТЗ Н-28: за последней страницей следующей нет (раньше Page([]) ронял ссылку в 500).
        if self._beyond_last():
            return None
        return super().get_next_link()

    def get_previous_link(self):
        # ТЗ Н-28: «предыдущая» для страницы за концом — последняя существующая.
        if self._beyond_last():
            from rest_framework.utils.urls import replace_query_param

            last = self.page.paginator.num_pages
            return replace_query_param(self.request.build_absolute_uri(), self.page_query_param, last)
        return super().get_previous_link()

    def get_paginated_response(self, data, *, total_amount):
        return Response(
            {
                "count": self.page.paginator.count,
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "results": data,
                "meta": {"total_amount": str(total_amount)},
            }
        )


def _receipt_discount_extras(data, items, by_id):
    """
    ТЗ Н-33: тело прихода с discount (сумма или «10%») / discount_percent и extra_costs[{name, amount}].
    Только если эти поля переданы. Корректировка = доп. расходы − скидка; распределяется по строкам
    пропорционально сумме строки (qty × закупка). → None или {landed: {id(item): цена за ед.}, receipt_fields}.
    """
    raw_disc = data.get("discount")
    raw_pct = data.get("discount_percent")
    raw_extra = data.get("extra_costs")
    if raw_disc in (None, "") and raw_pct in (None, "") and not raw_extra:
        return None

    def _dec(v, field):
        try:
            d = Decimal(str(v).strip())
        except Exception:
            raise ValidationError({field: "Ожидается число."})
        if d < 0:
            raise ValidationError({field: "Не может быть отрицательным."})
        return d

    base = {}
    subtotal = Decimal("0")
    for it in items:
        prod = by_id.get(it["product"].id)
        pp = it.get("purchase_price")
        if pp is None:
            pp = getattr(prod, "purchase_price", None)
        line = Decimal(str(pp or 0)) * Decimal(str(int(it["qty"])))
        base[id(it)] = line
        subtotal += line

    pct = None
    disc = Decimal("0")
    if isinstance(raw_disc, str) and raw_disc.strip().endswith("%"):
        pct = _dec(raw_disc.strip()[:-1], "discount")
    elif raw_pct not in (None, ""):
        pct = _dec(raw_pct, "discount_percent")
    elif raw_disc not in (None, ""):
        disc = _dec(raw_disc, "discount")
    if pct is not None:
        if pct > 100:
            raise ValidationError({"discount_percent": "Не больше 100."})
        disc = (subtotal * pct / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if disc > subtotal:
        raise ValidationError({"code": "discount_exceeds_total", "detail": "Скидка больше суммы прихода."})

    extras = []
    extra_total = Decimal("0")
    if raw_extra:
        if not isinstance(raw_extra, list):
            raise ValidationError({"extra_costs": "Ожидается список {name, amount}."})
        for row in raw_extra:
            if not isinstance(row, dict):
                raise ValidationError({"extra_costs": "Ожидается список {name, amount}."})
            amt = _dec(row.get("amount"), "extra_costs").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            extras.append({"name": str(row.get("name") or "").strip()[:100], "amount": f"{amt:.2f}"})
            extra_total += amt

    delta = extra_total - disc
    landed = {}
    for it in items:
        line = base[id(it)]
        qty = Decimal(str(int(it["qty"])))
        if subtotal > 0 and line > 0 and qty > 0:
            landed[id(it)] = (line + delta * line / subtotal) / qty
        else:
            landed[id(it)] = None
    return {
        "landed": landed,
        "receipt_fields": {
            "discount": disc,
            "discount_percent": pct,
            "extra_costs": extras,
        },
    }


@read_only_guard("can_edit_procurement")
class SupplierReceiptAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/suppliers/<uuid:supplier_id>/receipt/
    Body:
      {
        "items": [
          {"product": "<uuid>", "qty": 10},
          {"product": "<uuid>", "qty": 3}
        ]
      }

    ТЗ Н-10 (опционально, правило ОДНО): если в теле "create_deal": true — сервер в одной
    транзакции сам создаёт долг поставщику и принимает Idempotency-Key (заголовок или
    idempotency_key в теле; повтор → тот же ответ, replayed=true).
      payment_type: cash|transfer|debt|prepayment, paid_amount, first_due_date (YYYY-MM-DD,
      без неё — сегодня+30 дней), comment, shift/cashbox как в Н-03.
      debt → сделка kind=debt на всю сумму, ОДИН взнос на first_due_date, расхода нет;
      prepayment → один расход на paid_amount + сделка kind=debt на остаток.
      Связь сделка ↔ приход — метка [receipt:<id>] в note сделки (миграций нет).
    Ответ добавляет: payment_type, paid_amount, debt_amount, total_amount, deal_id,
    cashflow_id, supplier_debt_total, replayed. Ошибки: items_empty, payment_type_invalid,
    paid_amount_exceeds_total, paid_amount_invalid, first_due_date_invalid.
    БЕЗ "create_deal" запрос работает как раньше (сайт сам создаёт сделку).

    ТЗ Н-29: "order_id" (uuid заказа поставщику) — принятое количество прибавляется к received_qty
    строк заказа, статус → partial/received; приход хранит order_id. Ошибки: order_not_found,
    order_supplier_mismatch, order_closed (заказ cancelled/received).
    """
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _receipt_apply_variants(company, prod, it, qty):
        """ТЗ Н-27: variant / variants[] строки прихода → остаток варианта(ов); остаток товара = сумма активных."""
        from apps.main.models import ProductVariant
        from apps.main.variant_utils import notify_variants_in_stock, recalc_product_quantity

        parts = []
        if it.get("variants"):
            for row in it["variants"]:
                try:
                    vq = Decimal(str(row.get("qty")))
                except Exception:
                    raise ValidationError({"code": "variant_qty_mismatch", "detail": "Неверное количество в variants."})
                if vq <= 0 or not row.get("variant"):
                    raise ValidationError({"code": "variant_qty_mismatch", "detail": "variants: нужны variant и qty > 0."})
                parts.append((str(row["variant"]), vq))
            if sum((q for _, q in parts), Decimal("0")) != Decimal(qty):
                raise ValidationError({
                    "code": "variant_qty_mismatch",
                    "detail": "Сумма по размерам не равна количеству строки.",
                })
        elif it.get("variant"):
            parts.append((str(it["variant"]), Decimal(qty)))
        if not parts:
            return
        ids = [v for v, _ in parts]
        try:
            found = {str(v.id): v for v in ProductVariant.objects.select_for_update().filter(company=company, id__in=ids)}
        except Exception:
            found = {}
        for vid, vq in parts:
            v = found.get(vid)
            if v is None or v.product_id != prod.id:
                raise ValidationError({"code": "variant_mismatch", "detail": "Вариант не относится к товару."})
            ProductVariant.objects.filter(pk=v.pk).update(quantity=F("quantity") + vq)
        recalc_product_quantity(prod.id)
        notify_variants_in_stock([v.pk for v in found.values()])

    def post(self, request, supplier_id):
        # ТЗ Н-10: серверная сделка-долг и идемпотентность — только по явному "create_deal": true.
        # Без флага запрос работает как раньше (сайт сам создаёт сделку отдельным запросом).
        if _truthy(request.data.get("create_deal")):
            handler = idempotent(body_key=True, replay_flag=True)(type(self)._post_impl)
            return handler(self, request, supplier_id)
        return self._post_impl(request, supplier_id)

    @transaction.atomic
    def _post_impl(self, request, supplier_id):
        opt_in = _truthy(request.data.get("create_deal"))
        company = self._company()
        branch = self._auto_branch()

        sup_qs = self._filter_qs_company_branch(Client.objects.all())
        supplier = get_object_or_404(sup_qs, id=supplier_id, type=Client.StatusClient.SUPPLIERS)

        paid_amount_in = None
        if opt_in:
            if not request.data.get("items"):
                raise ValidationError({"code": "items_empty", "detail": "Нет строк прихода."})
            raw_pt = str(request.data.get("payment_type") or "cash").strip().lower()
            if raw_pt not in SupplierPurchase.PaymentType.values:
                raise ValidationError({"code": "payment_type_invalid", "detail": "payment_type: cash | transfer | debt | prepayment."})
            raw_paid = request.data.get("paid_amount")
            if raw_paid not in (None, ""):
                try:
                    paid_amount_in = Decimal(str(raw_paid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                except Exception:
                    paid_amount_in = None
                if paid_amount_in is None or paid_amount_in < 0:
                    raise ValidationError({"code": "paid_amount_invalid", "detail": "paid_amount — неотрицательная сумма."})
            first_due = None
            raw_due = request.data.get("first_due_date")
            if raw_due:
                first_due = parse_date(str(raw_due)) if not hasattr(raw_due, "year") else raw_due
                if first_due is None:
                    raise ValidationError({"code": "first_due_date_invalid", "detail": "first_due_date: YYYY-MM-DD."})

        # ТЗ Н-29: заказ поставщику, по которому идёт приход
        po_order = None
        raw_order_id = request.data.get("order_id")
        if raw_order_id not in (None, ""):
            from apps.main.models import PurchaseOrder
            from django.core.exceptions import ValidationError as DjangoValidationError
            try:
                po_order = self._filter_qs_company_branch(PurchaseOrder.objects.all()).select_for_update().filter(pk=raw_order_id).first()
            except (ValueError, DjangoValidationError):
                po_order = None
            if po_order is None:
                raise ValidationError({"code": "order_not_found", "detail": "Заказ поставщику не найден."})
            if po_order.supplier_id != supplier.id:
                raise ValidationError({"code": "order_supplier_mismatch", "detail": "Заказ оформлен на другого поставщика."})
            if po_order.status in (PurchaseOrder.Status.CANCELLED, PurchaseOrder.Status.RECEIVED):
                raise ValidationError({"code": "order_closed", "detail": "Заказ уже закрыт (отменён или принят полностью)."})

        ser = SupplierReceiptCreateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)

        items = ser.validated_data["items"]
        from apps.main.tz26.hw_units.receipts import convert_receipt_items

        convert_receipt_items(items)  # ТЗ 26.10 Б-53 п.5: строки в единицах закупки -> базовые
        product_ids = [it["product"].id for it in items]
        drawer_shift = _receipt_drawer_shift(request, company)

        prod_qs = self._filter_qs_company_branch(Product.objects.all()).select_for_update()
        products = list(prod_qs.filter(id__in=product_ids))
        by_id = {p.id: p for p in products}

        missing = [str(pid) for pid in product_ids if pid not in by_id]
        if missing:
            raise ValidationError({"items": [f"Товары не найдены/не доступны: {', '.join(missing)}"]})

        # Привязываем поставщика к товарам (suppliers M2M) и фиксируем primary client
        for p in products:
            try:
                p.suppliers.add(supplier)
            except Exception:
                pass
            if p.client_id != supplier.id:
                p.client = supplier
                p.save(update_fields=["client", "updated_at"])

        # ТЗ Н-33: скидка и доп. расходы прихода → в закупочную цену строк пропорционально.
        adj = _receipt_discount_extras(request.data, items, by_id)

        # лог оприходования
        receipt = SupplierReceipt.objects.create(
            company=company,
            branch=branch,
            supplier=supplier,
            created_by=getattr(request, "user", None),
            order=po_order,
            **(adj["receipt_fields"] if adj else {}),
        )
        payment_type = _parse_payment_type(request.data.get("payment_type"))

        # обновляем цены (если переданы) и увеличиваем остатки
        receipt_items = []
        if adj:
            for it in items:
                if id(it) in adj["landed"] and adj["landed"][id(it)] is not None:
                    it["purchase_price"] = adj["landed"][id(it)].quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        for it in items:
            pid = it["product"].id
            qty = int(it["qty"])
            prod = by_id[pid]
            upd = {"quantity": F("quantity") + qty}
            raw_pp = it.get("purchase_price")
            new_purchase_price = None
            if raw_pp is not None:
                new_purchase_price = Decimal(str(raw_pp))
                upd["purchase_price"] = new_purchase_price

            # Опциональная розничная цена (price / selling_price), переданная напрямую в строке прихода
            custom_price = it.get("price") if it.get("price") is not None else it.get("selling_price")
            if custom_price is not None:
                new_price = Decimal(str(custom_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                upd["price"] = new_price
                effective_pp = new_purchase_price if raw_pp is not None else getattr(prod, "purchase_price", None)
                if effective_pp is not None:
                    try:
                        eff_dec = Decimal(str(effective_pp))
                        if eff_dec > Decimal("0"):
                            upd["markup_percent"] = (
                                (new_price - eff_dec) / eff_dec * Decimal("100")
                            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    except Exception:
                        pass
            elif raw_pp is not None:
                # Оприходование меняет только закупочную цену и остаток.
                # Цена продажи (price) не трогается: раньше она перезаписывалась
                # закупочной и товар уходил с кассы по себестоимости.
                # Наценку пересчитываем от новой закупочной к текущей цене продажи,
                # чтобы тройка purchase_price / markup_percent / price осталась согласованной.
                current_price = Decimal(str(getattr(prod, "price", None) or 0))
                old_markup = Decimal(str(getattr(prod, "markup_percent", None) or 0))
                if it.get("recalc_price") and new_purchase_price > Decimal("0") and old_markup != Decimal("0"):
                    # ТЗ Н-04: пересчёт цены продажи только по явному флагу строки.
                    upd["price"] = (new_purchase_price * (Decimal("1") + old_markup / Decimal("100"))).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                elif new_purchase_price > Decimal("0") and current_price > Decimal("0"):
                    # 4 знака, как в поле: иначе следующий save() сдвигает цену на копейки.
                    upd["markup_percent"] = (
                        (current_price - new_purchase_price) / new_purchase_price * Decimal("100")
                    ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

            type(prod).objects.filter(id=pid).update(**upd)
            # Журнал товара (ТЗ п.8): смена цены продажи приходом — было → стало.
            if "price" in upd:
                try:
                    _old_price = Decimal(str(getattr(prod, "price", None) or 0))
                    if _old_price.quantize(Decimal("0.01")) != Decimal(upd["price"]).quantize(Decimal("0.01")):
                        admin_audit.log_request(
                            request, "price_change", "product", pid, prod.name,
                            before={"price": str(_old_price)}, after={"price": str(upd["price"])},
                            company=company,
                        )
                except Exception:
                    pass
            # ТЗ Н-27: приход по варианту — остаток варианта и товара в этой же транзакции.
            self._receipt_apply_variants(company, prod, it, qty)
            # The batch retains the shelf-life configuration at the receipt moment.
            from apps.main.services.product_expiry_batches import receive_stock
            receive_stock(prod, qty, source_kind="purchase", source_id=receipt.id, user=request.user)

            receipt_items.append(
                SupplierReceiptItem(
                    receipt=receipt,
                    product=prod,
                    qty=qty,
                    purchase_price=it.get("purchase_price"),
                    landed_price=((adj["landed"].get(id(it)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)) if adj and adj["landed"].get(id(it)) is not None else None),
                )
            )

            # История закупок поставщика: цена из строки, иначе текущая закупочная товара.
            record_supplier_purchase(
                company=company,
                branch=branch,
                supplier=supplier,
                product=prod,
                quantity=qty,
                unit=getattr(prod, "unit", "") or "",
                unit_price=(
                    it.get("purchase_price")
                    if it.get("purchase_price") is not None
                    else prod.purchase_price
                ),
                payment_type=payment_type,
                created_by=getattr(request, "user", None),
            )

        if receipt_items:
            SupplierReceiptItem.objects.bulk_create(receipt_items)
            # ТЗ 26.10 Б-61: batch_label строки прихода -> остаток партии (без метки ничего не делает)
            from apps.main.tz26.hw_batches.services import record_receipt_batches
            record_receipt_batches(company, request.data.get("items"), receipt_items)
        if po_order is not None:
            from apps.main.purchase_orders import apply_receipt_to_order
            apply_receipt_to_order(po_order, [(ri.product_id, ri.qty) for ri in receipt_items])

        # auto-cashflow для закупок за наличные
        receipt_cashflows = []
        total_receipt_amount = Decimal("0.00")
        for it in receipt_items:
            pp = it.purchase_price
            if pp is None:
                pp = it.product.purchase_price or Decimal("0.00")
            total_receipt_amount += Decimal(str(it.qty)) * Decimal(str(pp))
        total_receipt_amount = total_receipt_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # ТЗ Н-10: сколько заплачено сразу и сколько уходит в долг (только с create_deal).
        paid_now = total_receipt_amount if payment_type in ("cash", "transfer") else Decimal("0.00")
        if opt_in and payment_type == "prepayment":
            paid_now = paid_amount_in or Decimal("0.00")
        if opt_in and paid_now > total_receipt_amount:
            raise ValidationError({"code": "paid_amount_exceeds_total", "detail": "paid_amount больше суммы прихода."})
        debt_part = (total_receipt_amount - paid_now) if payment_type in ("debt", "prepayment") else Decimal("0.00")

        if payment_type == "cash" or (opt_in and payment_type == "prepayment"):
            expense_amount = paid_now if (opt_in and payment_type == "prepayment") else total_receipt_amount
            if expense_amount > Decimal("0"):
                from apps.construction.auto_cashflow import create_auto_cashflow, serialize_auto_cashflows
                from apps.construction.models import CashFlow
                # ТЗ Н-03: «оплачено из ящика смены» — один расход из кассы этой смены.
                cf = create_auto_cashflow(
                    company=company,
                    branch=branch,
                    cashbox=drawer_shift.cashbox if drawer_shift else None,
                    cashbox_id=request.data.get("cashbox_id") or request.data.get("cashbox"),
                    cashbox_role=request.data.get("cashbox_role") or "expense_variable",
                    user=request.user,
                    shift=drawer_shift,
                    type=CashFlow.Type.EXPENSE,
                    amount=expense_amount,
                    source_kind=CashFlow.SourceKind.PROCUREMENT_RECEIPT,
                    source_id=str(receipt.id),
                    name=f"Приход поставщика: {supplier.full_name or supplier.title or 'Поставщик'}",
                    source_business_operation_id="Закупки",
                    affects_shift_drawer=drawer_shift is not None,
                )
                if cf:
                    receipt_cashflows.append(cf)

        # Инвалидируем кэши товаров и аналитики
        from apps.main.cache_utils import invalidate_cache_pattern
        invalidate_cache_pattern(f"products:list:{company.id}:")
        invalidate_cache_pattern(f"analytics:market:{company.id}:")

        from apps.construction.auto_cashflow import serialize_auto_cashflows
        # вернём актуальные данные по товарам
        refreshed = list(self._filter_qs_company_branch(Product.objects.all()).filter(id__in=product_ids))
        resp_data = {
            "id": str(receipt.id),
            "receipt_id": str(receipt.id),
            "order_id": str(po_order.id) if po_order else None,
            "order_status": po_order.status if po_order else None,
            "supplier": str(supplier.id),
            "products": ProductListSerializer(refreshed, many=True, context={"request": request}).data,
            "cashflows": serialize_auto_cashflows(receipt_cashflows),
            "cashflow_id": str(receipt_cashflows[0].id) if receipt_cashflows else None,
            "cashbox": str(receipt_cashflows[0].cashbox_id) if receipt_cashflows and receipt_cashflows[0].cashbox_id else None,
            "shift": str(receipt_cashflows[0].shift_id) if receipt_cashflows and receipt_cashflows[0].shift_id else None,
        }
        if adj:  # ТЗ Н-33
            resp_data.update({
                "discount": f"{receipt.discount:.2f}",
                "discount_percent": f"{receipt.discount_percent:.2f}" if receipt.discount_percent is not None else None,
                "extra_costs": receipt.extra_costs,
            })
        if opt_in:
            # ТЗ Н-10: сделка-долг одним взносом на first_due_date, в той же транзакции.
            deal = None
            if debt_part > 0:
                from datetime import timedelta
                due = first_due or (timezone.localdate() + timedelta(days=30))
                name = supplier.full_name or supplier.title or "Поставщик"
                note = f"Приход поставщика {_receipt_marker(receipt.id)}"
                comment = str(request.data.get("comment") or "").strip()
                if comment:
                    note += f" {comment}"
                deal = ClientDeal(
                    company=company, branch=branch, client=supplier,
                    title=f"Долг {name}", kind=ClientDeal.Kind.DEBT,
                    amount=debt_part, prepayment=Decimal("0.00"),
                    debt_months=1, interval_months=1, first_due_date=due,
                    auto_schedule=True, note=note,
                )
                deal.save()
            resp_data.update({
                "payment_type": payment_type,
                "paid_amount": f"{paid_now:.2f}",
                "debt_amount": f"{debt_part:.2f}",
                "total_amount": f"{total_receipt_amount:.2f}",
                "deal_id": str(deal.id) if deal else None,
                "supplier_debt_total": f"{_supplier_debt_total(company, supplier.id):.2f}",
                "replayed": False,
            })
        return Response(resp_data, status=status.HTTP_200_OK)


class SupplierReceiptQuerysetMixin:
    def _supplier_receipts_base_queryset(self):
        line_total = ExpressionWrapper(
            F("items__qty")
            * Coalesce(
                F("items__purchase_price"),
                V(Decimal("0"), output_field=DecimalField(max_digits=11, decimal_places=3)),
            ),
            output_field=DecimalField(max_digits=20, decimal_places=3),
        )
        qs = (
            SupplierReceipt.objects.select_related("supplier", "company", "branch", "created_by")
            .prefetch_related("items", "items__product")
            .annotate(total_amount=Sum(line_total))
            # Meta.ordering is dropped by Django in GROUP BY queries (annotate+Sum),
            # so order explicitly: newest receipts first.
            .order_by("-created_at", "-id")
        )
        return self._filter_qs_company_branch(qs)

    def _supplier_receipts_filtered_queryset(self):
        qs = self._supplier_receipts_base_queryset()

        qp = self.request.query_params
        supplier_id = (qp.get("supplier_id") or "").strip()
        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)

        df_raw = (qp.get("date_from") or qp.get("created_from") or "").strip()
        dt_raw = (qp.get("date_to") or qp.get("created_to") or "").strip()
        df = parse_date(df_raw) if df_raw else None
        dt = parse_date(dt_raw) if dt_raw else None
        if df:
            qs = qs.filter(created_at__date__gte=df)
        if dt:
            qs = qs.filter(created_at__date__lte=dt)

        # ТЗ Н-10: фильтр по способу оплаты (строки журнала закупок, записанные в момент прихода)
        from datetime import timedelta
        pt = (qp.get("payment_type") or "").strip().lower()
        if pt:
            if pt not in SupplierPurchase.PaymentType.values:
                return qs.none()
            # строка закупки принадлежит последнему приходу поставщика, созданному до неё
            later_receipt = SupplierReceipt.objects.filter(
                supplier_id=OuterRef("supplier_id"),
                created_at__gt=OuterRef(OuterRef("created_at")),
                created_at__lte=OuterRef("purchased_at"),
            )
            qs = qs.filter(Exists(
                SupplierPurchase.objects.filter(
                    supplier_id=OuterRef("supplier_id"),
                    payment_type=pt,
                    purchased_at__gte=OuterRef("created_at"),
                    purchased_at__lte=OuterRef("created_at") + timedelta(seconds=60),
                ).filter(~Exists(later_receipt))
            ))

        # ТЗ Н-10: поиск — поставщик (имя/телефон/ООО), товар (название/штрихкод/код); Exists, чтобы не задвоить суммы
        search = (qp.get("search") or "").strip()
        if search:
            items_match = SupplierReceiptItem.objects.filter(receipt_id=OuterRef("pk")).filter(
                Q(product__name__icontains=search)
                | Q(product__barcode__icontains=search)
                | Q(product__code__icontains=search)
            )
            qs = qs.filter(
                Q(supplier__full_name__icontains=search)
                | Q(supplier__phone__icontains=search)
                | Q(supplier__llc__icontains=search)
                | Exists(items_match)
            )

        return qs


class SupplierReceiptListAPIView(
    SupplierReceiptQuerysetMixin,
    CompanyBranchRestrictedMixin,
    generics.ListAPIView,
):
    """
    GET /api/main/suppliers/receipts/?page=1&limit=20&supplier_id=<uuid>&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = SupplierReceiptReadSerializer
    pagination_class = SupplierReceiptLimitPagination

    def get_queryset(self):
        return self._supplier_receipts_filtered_queryset()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        total_amount = _aggregate_supplier_receipts_total_amount(queryset)

        page = self.paginate_queryset(queryset)
        info = _receipt_payment_info(page)  # ТЗ Н-10: один пакет запросов на страницу
        serializer = self.get_serializer(page, many=True, context={**self.get_serializer_context(), "receipt_info": info})
        return self.paginator.get_paginated_response(serializer.data, total_amount=total_amount)


class SupplierReceiptRetrieveAPIView(
    SupplierReceiptQuerysetMixin,
    CompanyBranchRestrictedMixin,
    generics.RetrieveAPIView,
):
    """
    GET /api/main/suppliers/receipts/<uuid:pk>/
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = SupplierReceiptReadSerializer
    lookup_url_kwarg = "pk"

    def get_queryset(self):
        return self._supplier_receipts_base_queryset()

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data
        items = data.get("items") or []
        data["lines"] = items
        return Response(data)


def _aggregate_purchase_batches_total_amount(item_qs):
    total = item_qs.aggregate(total=Sum(_supplier_receipt_line_total_expr()))["total"]
    if total is None:
        return Decimal("0")
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class ProductPurchaseBatchListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    GET /api/main/products/<uuid:pk>/purchase-batches/?page=1&limit=20

    История закупок (партий) конкретного товара: каждая партия — отдельная
    строка оприходования со своей ценой и количеством, новые сверху.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ProductPurchaseBatchSerializer
    pagination_class = SupplierReceiptLimitPagination
    lookup_url_kwarg = "pk"

    def _product(self):
        prod_qs = self._filter_qs_company_branch(Product.objects.all())
        return get_object_or_404(prod_qs, id=self.kwargs[self.lookup_url_kwarg])

    def get_queryset(self):
        product = self._product()
        return (
            SupplierReceiptItem.objects
            .filter(product_id=product.id)
            .select_related("receipt", "receipt__supplier", "receipt__created_by")
            .order_by("-receipt__created_at", "-id")
        )

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        total_amount = _aggregate_purchase_batches_total_amount(queryset)
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.paginator.get_paginated_response(
            serializer.data, total_amount=total_amount
        )


class ProductExpiryBatchListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ProductExpiryBatchSerializer
    pagination_class = SupplierReceiptLimitPagination

    def get_queryset(self):
        product = get_object_or_404(self._filter_qs_company_branch(Product.objects.all()), id=self.kwargs["pk"])
        return ProductExpiryBatch.objects.filter(product=product).order_by(F("expires_at").asc(nulls_last=True), "received_at")

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.paginator.get_paginated_response(self.get_serializer(page, many=True).data, total_amount=Decimal("0"))


# ===========================
#  Supplier Returns (Маркет: Возвраты поставщикам)
# ===========================
def _aggregate_supplier_returns_total_amount(qs):
    sub_qs = SupplierReturnItem.objects.filter(supplier_return__in=qs)
    expr = ExpressionWrapper(
        F("qty") * Coalesce(F("purchase_price"), V(Decimal("0"), output_field=DecimalField(max_digits=11, decimal_places=3))),
        output_field=DecimalField(max_digits=20, decimal_places=3),
    )
    total = sub_qs.aggregate(total=Sum(expr))["total"]
    if total is None:
        return Decimal("0.00")
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class SupplierReturnListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    GET /api/main/suppliers/returns/?page=1&limit=20&supplier_id=&date_from=&date_to=&reason=&search=&receipt_id=
    Alias: GET /api/main/supplier-returns/
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = SupplierReturnReadSerializer
    pagination_class = SupplierReceiptLimitPagination

    def get_queryset(self):
        qs = SupplierReturn.objects.select_related("supplier", "receipt", "created_by", "company", "branch").prefetch_related("items", "items__product")
        qs = self._filter_qs_company_branch(qs)

        qp = self.request.query_params
        supplier_id = (qp.get("supplier_id") or "").strip()
        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)

        receipt_id = (qp.get("receipt_id") or "").strip()
        if receipt_id:
            qs = qs.filter(receipt_id=receipt_id)

        reason = (qp.get("reason") or "").strip()
        if reason:
            qs = qs.filter(reason=reason)

        df_raw = (qp.get("date_from") or "").strip()
        dt_raw = (qp.get("date_to") or "").strip()
        df = parse_date(df_raw) if df_raw else None
        dt = parse_date(dt_raw) if dt_raw else None
        if df:
            qs = qs.filter(created_at__date__gte=df)
        if dt:
            qs = qs.filter(created_at__date__lte=dt)

        search = (qp.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(supplier__full_name__icontains=search)
                | Q(supplier__name__icontains=search)
                | Q(comment__icontains=search)
                | Q(items__product__name__icontains=search)
                | Q(items__product__code__icontains=search)
                | Q(items__product__barcode__icontains=search)
            ).distinct()

        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        total_amount = _aggregate_supplier_returns_total_amount(queryset)

        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.paginator.get_paginated_response(serializer.data, total_amount=total_amount)


class SupplierReturnRetrieveAPIView(CompanyBranchRestrictedMixin, generics.RetrieveAPIView):
    """
    GET /api/main/suppliers/returns/<uuid:pk>/
    Alias: GET /api/main/supplier-returns/<uuid:pk>/
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = SupplierReturnReadSerializer
    lookup_url_kwarg = "pk"

    def get_queryset(self):
        qs = SupplierReturn.objects.select_related("supplier", "receipt", "created_by", "company", "branch").prefetch_related("items", "items__product")
        return self._filter_qs_company_branch(qs)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data
        data["lines"] = data.get("items") or []
        return Response(data)


@read_only_guard("can_edit_procurement")
class SupplierReturnCreateAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/suppliers/<uuid:supplier_id>/returns/
    POST /api/main/suppliers/returns/
    """

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, supplier_id=None):
        company = self._company()
        branch = self._auto_branch()

        sup_id = supplier_id or request.data.get("supplier_id") or request.data.get("supplierId")
        if not sup_id:
            return Response({"detail": "Укажите поставщика."}, status=status.HTTP_400_BAD_REQUEST)

        sup_qs = self._filter_qs_company_branch(Client.objects.all())
        supplier = get_object_or_404(sup_qs, id=sup_id, type=Client.StatusClient.SUPPLIERS)

        ser = SupplierReturnCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        vdata = ser.validated_data
        receipt_id = vdata.get("receipt_id")
        receipt = None
        if receipt_id:
            rc_qs = self._filter_qs_company_branch(SupplierReceipt.objects.all())
            receipt = rc_qs.filter(id=receipt_id, supplier=supplier).first()
            if not receipt:
                return Response({"detail": "Оприходование не найдено или принадлежит другому поставщику."}, status=status.HTTP_400_BAD_REQUEST)

        items_data = vdata["items"]
        product_ids = [it["product_id"] for it in items_data]

        prod_qs = self._filter_qs_company_branch(Product.objects.all()).select_for_update()
        products = {p.id: p for p in prod_qs.filter(id__in=product_ids)}

        missing = [str(pid) for pid in product_ids if pid not in products]
        if missing:
            return Response({"detail": f"Товары не найдены: {', '.join(missing)}"}, status=status.HTTP_400_BAD_REQUEST)

        # Валидация позиций
        total_return_amount = Decimal("0.00")
        validated_lines = []

        for item in items_data:
            product = products[item["product_id"]]
            qty = Decimal(str(item["qty"]))
            if qty <= Decimal("0"):
                return Response({"detail": "Количество должно быть больше 0."}, status=status.HTTP_400_BAD_REQUEST)

            # Для штучных товаров проверяем целое число
            is_weight = getattr(product, "is_weight", False) or getattr(product, "scale_type", None) == "weight"
            if not is_weight and qty % Decimal("1") != Decimal("0"):
                return Response({"detail": f"Для штучного товара '{product.name}' количество должно быть целым числом."}, status=status.HTTP_400_BAD_REQUEST)

            # Проверка складского остатка
            current_stock = Decimal(str(product.quantity or 0))
            if qty > current_stock:
                return Response(
                    {"detail": f"Нельзя вернуть больше остатка по товару '{product.name}' (доступно: {current_stock}, запрошено: {qty})."},
                    status=status.HTTP_409_CONFLICT,
                )

            receipt_item = None
            r_item_id = item.get("receipt_item_id")
            if r_item_id:
                receipt_item = SupplierReceiptItem.objects.filter(id=r_item_id, receipt__supplier=supplier).first()
                if not receipt_item:
                    return Response({"detail": f"Строка оприходования '{r_item_id}' не найдена."}, status=status.HTTP_400_BAD_REQUEST)

            # Проверка лимита возврата по приходу
            if receipt_item:
                purchased_qty = Decimal(str(receipt_item.qty))
                already_ret = SupplierReturnItem.objects.filter(
                    receipt_item=receipt_item, supplier_return__status="posted"
                ).aggregate(s=Sum("qty"))["s"] or Decimal("0")
                returnable = max(Decimal("0"), purchased_qty - already_ret)
                if qty > returnable:
                    return Response(
                        {"detail": f"По товару '{product.name}' уже возвращено {already_ret} из {purchased_qty}. Максимум к возврату по приходу: {returnable}."},
                        status=status.HTTP_409_CONFLICT,
                    )

            price = item.get("purchase_price")
            if price is None:
                if receipt_item and receipt_item.purchase_price is not None:
                    price = receipt_item.purchase_price
                else:
                    price = product.purchase_price or Decimal("0.00")
            else:
                price = Decimal(str(price))

            line_sum = qty * price
            total_return_amount += line_sum

            validated_lines.append({
                "product": product,
                "receipt_item": receipt_item,
                "qty": qty,
                "purchase_price": price,
            })

        compensation = vdata["compensation"]
        cashbox = None

        if compensation == SupplierReturn.Compensation.CASH and total_return_amount > Decimal("0"):
            from apps.construction.auto_cashflow import resolve_cashbox
            cashbox_id = vdata.get("cashbox_id")
            cashbox = resolve_cashbox(
                company=company,
                context={
                    "cashbox_id": cashbox_id,
                    "cashbox_role": vdata.get("cashbox_role") or "expense_variable",
                    "branch_id": branch.id if branch else None,
                },
                source_kind="supplier_return",
                require_cashbox=False,
            )

        # Создаем документ возврата
        sup_return = SupplierReturn.objects.create(
            company=company,
            branch=branch,
            supplier=supplier,
            receipt=receipt,
            created_by=request.user,
            reason=vdata["reason"],
            comment=vdata.get("comment", ""),
            compensation=compensation,
            cashbox=cashbox,
            status=SupplierReturn.Status.POSTED,
        )

        # Создаем строки возврата и списываем остаток
        return_items = []
        for line in validated_lines:
            p = line["product"]
            q = line["qty"]
            Product.objects.filter(id=p.id).update(quantity=F("quantity") - q, updated_at=timezone.now())

            return_items.append(
                SupplierReturnItem(
                    supplier_return=sup_return,
                    product=p,
                    receipt_item=line["receipt_item"],
                    qty=q,
                    purchase_price=line["purchase_price"],
                )
            )

        SupplierReturnItem.objects.bulk_create(return_items)

        # Проводка денежных средств
        if compensation == SupplierReturn.Compensation.CASH and total_return_amount > Decimal("0"):
            from apps.construction.auto_cashflow import create_auto_cashflow
            from apps.construction.models import CashFlow
            create_auto_cashflow(
                company=company,
                branch=branch,
                cashbox=cashbox,
                cashbox_id=vdata.get("cashbox_id"),
                cashbox_role=vdata.get("cashbox_role") or "expense_variable",
                user=request.user,
                shift=None,
                type=CashFlow.Type.INCOME,
                amount=total_return_amount,
                source_kind=CashFlow.SourceKind.SUPPLIER_RETURN,
                source_id=str(sup_return.id),
                name=f"Возврат поставщику: {supplier.full_name or 'Поставщик'}",
                source_business_operation_id="Возврат поставщику",
                affects_shift_drawer=False,
            )
            try:
                TransactionRecord.objects.create(
                    company=company,
                    branch=branch,
                    cashbox=cashbox,
                    type=TransactionRecord.Type.INCOME if hasattr(TransactionRecord, "Type") else "income",
                    amount=total_return_amount,
                    note=f"Возврат поставщику: {supplier.full_name}",
                    created_by=request.user,
                )
            except Exception:
                pass
        elif compensation == SupplierReturn.Compensation.DEBT_OFFSET and total_return_amount > Decimal("0"):
            try:
                deal = ClientDeal.objects.filter(company=company, client=supplier, kind=ClientDeal.Kind.DEBT).order_by("-created_at").first()
                if deal:
                    new_pre = (deal.prepayment or Decimal("0")) + total_return_amount
                    deal.prepayment = min(deal.amount, new_pre)
                    deal.save(update_fields=["prepayment"])
            except Exception:
                pass

        resp_serializer = SupplierReturnReadSerializer(sup_return, context={"request": request})
        return Response(resp_serializer.data, status=status.HTTP_201_CREATED)


# ===========================
#  Product Form Layout (Маркет: Раскладка формы товара)
# ===========================
ALLOWED_PRODUCT_FORM_LAYOUT_FIELDS = {
    "code", "barcode", "alternateBarcodes", "article", "hotkey", "images",
    "category", "brand",
    "unit", "pieceSale", "weight", "adult", "packaging",
    "markup", "wholesale", "discount",
    "supplier", "debt", "minStock", "country", "expiry",
    "promotion", "characteristics", "description", "plu"
}


class ProductFormLayoutAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/products/form-layout/ — прочитать раскладку компании
    PATCH /api/main/products/form-layout/ — сохранить раскладку (owner/admin)
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = self._company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_400_BAD_REQUEST)

        layout = MarketProductFormLayout.objects.filter(company=company).first()
        hidden = layout.hidden if (layout and isinstance(layout.hidden, list)) else []
        updated_at = layout.updated_at.isoformat() if (layout and layout.updated_at) else None

        return Response({"hidden": hidden, "updated_at": updated_at}, status=status.HTTP_200_OK)

    def patch(self, request, *args, **kwargs):
        company = self._company()
        if not company:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_400_BAD_REQUEST)

        is_admin = getattr(request.user, "role", None) in ["owner", "admin"]
        if not is_admin:
            return Response({"detail": "Недостаточно прав. Настройка формы доступна только администраторам и владельцу."}, status=status.HTTP_403_FORBIDDEN)

        hidden_raw = request.data.get("hidden")
        if hidden_raw is None or not isinstance(hidden_raw, list):
            return Response({"hidden": ["Поле 'hidden' должно быть массивом строк."]}, status=status.HTTP_400_BAD_REQUEST)

        normalized_hidden = []
        seen = set()
        for item in hidden_raw:
            if isinstance(item, str):
                item_str = item.strip()
                if item_str in ALLOWED_PRODUCT_FORM_LAYOUT_FIELDS and item_str not in seen:
                    seen.add(item_str)
                    normalized_hidden.append(item_str)

        layout, _ = MarketProductFormLayout.objects.get_or_create(company=company)
        layout.hidden = normalized_hidden
        layout.updated_by = request.user
        layout.save()

        return Response({
            "hidden": layout.hidden,
            "updated_at": layout.updated_at.isoformat() if layout.updated_at else None,
        }, status=status.HTTP_200_OK)


# ===========================
#  Subreal / Acceptance / ReturnFromAgent
# ===========================
class ManufactureSubrealListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ManufactureSubrealSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["agent", "product", "status", "created_at", "external_ref"]
    # если нужен трек-номер, добавьте "agent__track_number"
    search_fields = ["product__name", "agent__username", "agent__first_name", "agent__last_name"]
    ordering_fields = ["created_at", "qty_transferred", "qty_accepted", "status"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = (
            ManufactureSubreal.objects
            .select_related("company", "user", "agent", "product")
            .all()
        )
        qs = self._filter_qs_company_branch(qs)
        # Агент видит только свои приёмки; владелец/админ — все приёмки компании.
        if not _is_owner_like(self.request.user):
            qs = qs.filter(agent=self.request.user)
        return qs

    def create(self, request, *args, **kwargs):
        # Пред-проверка остатка ВНЕ транзакции выдачи: при нехватке фиксируем
        # StockShortageEvent + уведомления и отклоняем (defense in depth).
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        company = self._company()
        branch = self._auto_branch()
        product = serializer.validated_data.get("product")
        agent = serializer.validated_data.get("agent")
        qty = int(serializer.validated_data.get("qty_transferred") or 0)
        if qty and product and company is not None:
            avail = type(product).objects.filter(pk=product.pk).values_list("quantity", flat=True).first()
            avail = avail if avail is not None else 0
            if avail < qty:
                report_stock_shortage(
                    company=company, branch=branch, product=product,
                    requested_qty=qty, available_qty=avail, agent=agent,
                    source=StockShortageEvent.Source.TRANSFER, created_by=request.user,
                )
                raise serializers.ValidationError({
                    "detail": f'Недостаточно товара «{product.name}». Запрошено {qty}, доступно {avail}.',
                    "shortage": {"product": str(product.pk), "requested_qty": qty, "available_qty": avail},
                })
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    @transaction.atomic
    def perform_create(self, serializer):
        company = self._company()
        branch = self._auto_branch()
        product = serializer.validated_data.get("product")
        agent = serializer.validated_data.get("agent")
        qty = int(serializer.validated_data.get("qty_transferred") or 0)
        is_sawmill = bool(serializer.validated_data.get("is_sawmill", False))

        # защита от подмены company
        if product and product.company_id != company.id:
            raise serializers.ValidationError({"product": "Товар другой компании."})
        if agent and getattr(agent, "company_id", None) != company.id:
            raise serializers.ValidationError({"agent": "Агент другой компании."})

        locked_qs = None
        if qty:
            if not product:
                raise serializers.ValidationError({"product": "Не выбран товар для списания количества."})
            locked_qs = type(product).objects.select_for_update().filter(pk=product.pk)
            current_qty = locked_qs.values_list("quantity", flat=True).first()
            if current_qty is None or current_qty < qty:
                raise serializers.ValidationError({
                    "qty_transferred": f"Недостаточно на складе: доступно {current_qty or 0}."
                })

        # создаём передачу
        obj = serializer.save(company=company, branch=branch, user=self.request.user)

        # минусуем склад (в той же транзакции)
        if qty and locked_qs is not None:
            qty_before = current_qty or Decimal("0")
            locked_qs.update(quantity=F("quantity") - qty)
            prod_model = type(product)
            prod_id = product.pk

            # Журнал: передача агенту
            record_stock_movement(
                company=company, branch=branch, type=StockMovement.Type.AGENT_TRANSFER,
                object_id=product.pk, product_name=product.name,
                warehouse=StockMovement.Warehouse.FINISHED_GOODS,
                qty_before=qty_before, change=-qty, qty_after=qty_before - qty,
                created_by=self.request.user, sender=self.request.user, receiver=agent,
                ref_type="subreal", ref_id=obj.id,
            )

            def _send_webhook():
                from apps.main.services.webhooks import send_product_webhook

                try:
                    p = prod_model.objects.get(pk=prod_id)
                    send_product_webhook(p, "product.updated")
                except Exception:
                    logging.getLogger("crm.webhooks").error(
                        "Failed to send product.updated webhook after subreal create. product_id=%s",
                        prod_id,
                        exc_info=True,
                    )

            try:
                transaction.on_commit(_send_webhook)
            except Exception:
                _send_webhook()

        # идемпотентный авто-приём, если is_sawmill=True
        if is_sawmill:
            obj.refresh_from_db(fields=["qty_transferred", "qty_accepted", "status"])
            to_accept = int((obj.qty_transferred or 0) - (obj.qty_accepted or 0))
            if to_accept > 0 and obj.status == ManufactureSubreal.Status.OPEN:
                Acceptance.objects.create(
                    company=company,
                    branch=branch,
                    subreal=obj,
                    accepted_by=self._user(),
                    qty=to_accept,
                    accepted_at=timezone.now(),
                )

        return obj


# ===========================
#  Agent: мои приемки (только свои)
# ===========================
class AgentMySubrealsListAPIView(ManufactureSubrealListCreateAPIView):
    """
    GET /api/main/agents/me/subreals/
    Возвращает только субреализации, где agent == авторизованный пользователь.
    """
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        return super().get_queryset().filter(agent=self.request.user)


# ===========================
#  Subreal: retrieve/update/destroy
# ===========================
class ManufactureSubrealRetrieveUpdateDestroyAPIView(
    CompanyBranchRestrictedMixin,
    generics.RetrieveUpdateDestroyAPIView
):
    """
    GET    /api/main/subreals/<uuid:pk>/
    PATCH  /api/main/subreals/<uuid:pk>/
    PUT    /api/main/subreals/<uuid:pk>/
    DELETE /api/main/subreals/<uuid:pk>/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ManufactureSubrealSerializer

    def get_queryset(self):
        qs = (
            ManufactureSubreal.objects
            .select_related("company", "user", "agent", "product")
            .all()
        )
        qs = self._filter_qs_company_branch(qs)
        # Агент работает только со своими приёмками; владелец/админ — со всеми.
        if not _is_owner_like(self.request.user):
            qs = qs.filter(agent=self.request.user)
        return qs

    def perform_update(self, serializer):
        company = self._company()
        branch = self._auto_branch()
        prod = serializer.validated_data.get("product")
        agent = serializer.validated_data.get("agent")

        if prod and prod.company_id != company.id:
            raise serializers.ValidationError({"product": "Товар другой компании."})
        if agent and getattr(agent, "company_id", None) != company.id:
            raise serializers.ValidationError({"agent": "Агент другой компании."})

        serializer.save(company=company, branch=branch)


# ===========================
#  Acceptance: list/create
# ===========================
class AcceptanceListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["subreal", "accepted_by", "accepted_at"]
    ordering_fields = ["accepted_at", "qty", "id"]
    ordering = ["-accepted_at"]

    def get_queryset(self):
        qs = (
            Acceptance.objects
            .select_related(
                "company",
                "subreal",
                "accepted_by",
                "subreal__agent",
                "subreal__product",
            )
            .all()
        )
        return self._filter_qs_company_branch(qs)

    def get_serializer_class(self):
        return (
            AcceptanceCreateSerializer
            if self.request.method == "POST"
            else AcceptanceReadSerializer
        )

    @transaction.atomic
    def perform_create(self, serializer):
        sub = serializer.validated_data["subreal"]
        locked = (
            ManufactureSubreal.objects
            .select_for_update()
            .get(pk=sub.pk)
        )

        if locked.status != ManufactureSubreal.Status.OPEN:
            raise serializers.ValidationError({"subreal": "Передача уже закрыта."})

        qty = serializer.validated_data["qty"]
        if qty > locked.qty_remaining:
            raise serializers.ValidationError({
                "qty": f"Можно принять максимум {locked.qty_remaining}."
            })

        serializer.save(company=self._company(), accepted_by=self._user())


# ===========================
#  Acceptance: retrieve/destroy
# ===========================
class AcceptanceRetrieveDestroyAPIView(CompanyBranchRestrictedMixin, generics.RetrieveDestroyAPIView):
    """
    GET    /api/main/acceptances/<uuid:pk>/
    DELETE /api/main/acceptances/<uuid:pk>/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AcceptanceReadSerializer

    def get_queryset(self):
        qs = (
            Acceptance.objects
            .select_related(
                "company",
                "subreal",
                "accepted_by",
                "subreal__agent",
                "subreal__product",
            )
            .all()
        )
        return self._filter_qs_company_branch(qs)


# ===========================
#  Return: list/create
# ===========================
class ReturnFromAgentListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    """
    GET  /api/main/returns/
    POST /api/main/returns/
    """
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["subreal", "returned_by", "returned_at", "status", "is_defect"]
    ordering_fields = ["returned_at", "qty", "id"]
    ordering = ["-returned_at"]

    def get_queryset(self):
        qs = (
            ReturnFromAgent.objects
            .select_related(
                "company",
                "subreal",
                "returned_by",
                "accepted_by",
                "subreal__agent",
                "subreal__product",
            )
            .all()
        )
        return self._filter_qs_company_branch(qs)

    def get_serializer_class(self):
        return ReturnCreateSerializer if self.request.method == "POST" else ReturnReadSerializer

    def create(self, request, *args, **kwargs):
        """
        POST /api/main/returns/
        Может создать несколько ReturnFromAgent, если qty покрывается несколькими subreal.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        created = serializer.save(company=self._company(), returned_by=self._user())

        if isinstance(created, list):
            out_ser = ReturnReadSerializer(created, many=True, context={"request": request})
            return Response(out_ser.data, status=status.HTTP_201_CREATED)

        out_ser = ReturnReadSerializer(created, context={"request": request})
        return Response(out_ser.data, status=status.HTTP_201_CREATED)

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.save(company=self._company(), returned_by=self._user())

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        # Сводка по тем же фильтрам, что и список (для агента: ?returned_by=me → только его возвраты)
        qs = self.filter_queryset(self.get_queryset())
        pending = qs.filter(status=ReturnFromAgent.Status.PENDING).aggregate(
            pending_count=Count("id"),
            pending_qty=Coalesce(Sum("qty"), 0),
        )
        # Брак (is_defect=True) и обычные возвраты (is_defect=False).
        defect = qs.filter(is_defect=True).aggregate(
            defect_count=Count("id"),
            defect_qty=Coalesce(Sum("qty"), 0),
            defect_amount=Coalesce(Sum("amount"), V(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))),
        )
        regular = qs.filter(is_defect=False).aggregate(
            regular_return_count=Count("id"),
            regular_return_qty=Coalesce(Sum("qty"), 0),
            regular_return_amount=Coalesce(Sum("amount"), V(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))),
        )
        summary = {
            "pending_count": pending["pending_count"] or 0,
            "pending_qty": int(pending["pending_qty"] or 0),
            "defect_count": defect["defect_count"] or 0,
            "defect_qty": float(defect["defect_qty"] or 0),
            "defect_amount": float(defect["defect_amount"] or Decimal("0.00")),
            "regular_return_count": regular["regular_return_count"] or 0,
            "regular_return_qty": float(regular["regular_return_qty"] or 0),
            "regular_return_amount": float(regular["regular_return_amount"] or Decimal("0.00")),
        }
        if isinstance(response.data, dict):
            response.data["returns_summary"] = summary
        return response


# ===========================
#  Agent: мои возвраты (только свои, со сводкой)
# ===========================
class AgentMyReturnsListCreateAPIView(ReturnFromAgentListCreateAPIView):
    """
    GET  /api/main/agents/me/returns/
    POST /api/main/agents/me/returns/
    Список и создание возвратов только текущего агента. В ответе GET — returns_summary
    (pending_count, pending_qty) по возвратам агента.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(returned_by=self.request.user)

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)

        # Доп.данные для UI возвратов: "на руках" по товарам (суммарно по всем партиям subreal).
        # Нужно, когда один и тот же товар был выдан в нескольких передачах: 30 + 40 -> показываем 70.
        try:
            company = self._company()
            branch = self._auto_branch()
            user = request.user

            subreals_qs = ManufactureSubreal.objects.filter(company=company, agent=user)
            if branch is not None:
                subreals_qs = subreals_qs.filter(branch=branch)
            else:
                subreals_qs = subreals_qs.filter(branch__isnull=True)

            base_rows = list(
                subreals_qs.values("product_id", "product__name")
                .annotate(
                    accepted=Coalesce(Sum("qty_accepted"), V(0)),
                    returned=Coalesce(Sum("qty_returned"), V(0)),
                )
            )

            sold_rows = list(
                AgentSaleAllocation.objects.filter(
                    company=company,
                    agent=user,
                    sale__status__in=[Sale.Status.PAID, Sale.Status.DEBT],
                )
                .values("product_id")
                .annotate(sold=Coalesce(Sum("qty"), V(0)))
            )
            sold_by_product = {r["product_id"]: int(r.get("sold") or 0) for r in sold_rows}

            pending_rows = list(
                ReturnFromAgent.objects.filter(
                    company=company,
                    status=ReturnFromAgent.Status.PENDING,
                    subreal__agent=user,
                )
                .values("subreal__product_id")
                .annotate(reserved=Coalesce(Sum("qty"), V(0)))
            )
            pending_by_product = {r["subreal__product_id"]: int(r.get("reserved") or 0) for r in pending_rows}

            on_hand = []
            for r in base_rows:
                pid = r.get("product_id")
                accepted = int(r.get("accepted") or 0)
                returned = int(r.get("returned") or 0)
                sold = int(sold_by_product.get(pid, 0) or 0)
                reserved = int(pending_by_product.get(pid, 0) or 0)
                qty_on_hand = max(accepted - returned - sold - reserved, 0)
                if qty_on_hand <= 0:
                    continue
                on_hand.append({
                    "product_id": str(pid) if pid else None,
                    "product_name": r.get("product__name") or "",
                    "qty_on_hand": qty_on_hand,
                })
            on_hand.sort(key=lambda x: x["qty_on_hand"], reverse=True)

            if isinstance(response.data, dict):
                response.data["on_hand_by_product"] = on_hand
        except Exception:
            # не ломаем endpoint из-за доп.раздела
            pass

        return response


# ===========================
#  Return: retrieve/destroy
# ===========================
class ReturnFromAgentRetrieveDestroyAPIView(
    CompanyBranchRestrictedMixin,
    generics.RetrieveDestroyAPIView
):
    """
    GET    /api/main/returns/<uuid:pk>/
    DELETE /api/main/returns/<uuid:pk>/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ReturnReadSerializer

    def get_queryset(self):
        qs = (
            ReturnFromAgent.objects
            .select_related(
                "company",
                "subreal",
                "returned_by",
                "accepted_by",
                "subreal__agent",
                "subreal__product",
            )
            .all()
        )
        return self._filter_qs_company_branch(qs)


# ===========================
#  Return: approve (идемпотентно)
# ===========================
class ReturnFromAgentApproveAPIView(APIView, CompanyBranchRestrictedMixin):
    """
    POST /api/main/returns/<uuid:pk>/approve/
    Подтверждение возврата (статус -> accepted, движение на склад).
    """
    permission_classes = [permissions.IsAuthenticated]

    def _relocate_return_subreal_if_needed(self, ret: ReturnFromAgent):
        """
        Если по текущей партии (subreal) уже нет остатка, пробуем найти другую партию
        того же товара у того же агента, где остатка хватает.
        """
        company_id = ret.company_id
        branch_id = ret.branch_id
        product_id = getattr(getattr(ret, "subreal", None), "product_id", None)
        agent_id = getattr(getattr(ret, "subreal", None), "agent_id", None)
        if not (company_id and product_id and agent_id):
            return

        current_subreal_id = ret.subreal_id
        try:
            current = ManufactureSubreal.objects.get(pk=current_subreal_id)
            on_hand_now = int(current.get_qty_on_hand_with_sales(company_id=company_id, exclude_pending_return_id=ret.pk) or 0)
        except Exception:
            on_hand_now = 0

        if on_hand_now >= int(ret.qty or 0):
            return

        candidates = ManufactureSubreal.objects.filter(
            company_id=company_id,
            agent_id=agent_id,
            product_id=product_id,
        )
        if branch_id is not None:
            candidates = candidates.filter(branch_id=branch_id)
        else:
            candidates = candidates.filter(branch__isnull=True)
        candidates = candidates.order_by("-created_at", "-id")

        need = int(ret.qty or 0)
        total = 0
        picked = None
        for s in candidates:
            on_hand = int(s.get_qty_on_hand_with_sales(company_id=company_id, exclude_pending_return_id=ret.pk) or 0)
            if on_hand <= 0:
                continue
            total += on_hand
            if picked is None and on_hand >= need:
                picked = s

        if picked is None:
            raise ValidationError({"qty": [f"Можно принять максимум {total}."]})

        if picked.pk != current_subreal_id:
            ret.subreal_id = picked.pk
            ret.save(update_fields=["subreal"])

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        try:
            ret = (
                ReturnFromAgent.objects
                .select_for_update()
                .select_related("subreal__product")
                .get(pk=pk, company_id=self._company().id)
            )
        except ReturnFromAgent.DoesNotExist:
            return Response({"detail": "Возврат не найден."}, status=status.HTTP_404_NOT_FOUND)

        # идемпотентность: если уже не pending — просто вернуть текущее состояние
        if ret.status != ReturnFromAgent.Status.PENDING:
            return Response(ReturnReadSerializer(ret).data, status=status.HTTP_200_OK)

        # Если партия, привязанная к возврату, уже "обнулилась", пробуем перепривязать к другой партии
        # того же товара у агента (остаток считаем в целом).
        self._relocate_return_subreal_if_needed(ret)

        ser = ReturnApproveSerializer(
            data=request.data,
            context={"request": request, "return_obj": ret},
        )
        ser.is_valid(raise_exception=True)
        ret = ser.save()
        return Response(ReturnReadSerializer(ret).data, status=status.HTTP_200_OK)


# ===========================
#  Return: bulk approve (owner/admin)
# ===========================
class ReturnFromAgentBulkApproveAPIView(APIView, CompanyBranchRestrictedMixin):
    """
    POST /api/main/returns/approve-bulk/
    Принимает сразу несколько возвратов одним запросом.

    Payload:
      - либо {"ids": ["uuid", ...]}
      - либо {"product_id": "<uuid>", "agent_id": "<uuid|optional>"} -> примет все PENDING по товару (и агенту, если задан)
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        if not _is_owner_like(request.user):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        company = self._company()
        data = request.data or {}
        ids = data.get("ids")
        product_id = data.get("product_id")
        agent_id = data.get("agent_id")

        def _as_uuid(value, field):
            try:
                return UUID(str(value))
            except (ValueError, TypeError, AttributeError):
                raise ValidationError({field: f"Неверный UUID: {value!r}."})

        if ids:
            if not isinstance(ids, (list, tuple)):
                raise ValidationError({"ids": "Ожидается список UUID."})
            ids = [_as_uuid(v, "ids") for v in ids]
            qs = ReturnFromAgent.objects.select_for_update().filter(company=company, status=ReturnFromAgent.Status.PENDING, id__in=ids)
        elif product_id:
            product_id = _as_uuid(product_id, "product_id")
            qs = ReturnFromAgent.objects.select_for_update().filter(
                company=company,
                status=ReturnFromAgent.Status.PENDING,
                subreal__product_id=product_id,
            )
            if agent_id:
                agent_id = _as_uuid(agent_id, "agent_id")
                qs = qs.filter(subreal__agent_id=agent_id)
        else:
            raise ValidationError({"detail": "Передай ids[] или product_id."})

        qs = qs.select_related("subreal__product")

        approved = []
        errors = []
        helper = ReturnFromAgentApproveAPIView()
        helper.request = request
        helper.args = ()
        helper.kwargs = {}

        for ret in qs.order_by("returned_at", "id"):
            try:
                helper._relocate_return_subreal_if_needed(ret)
                ser = ReturnApproveSerializer(data={}, context={"request": request, "return_obj": ret})
                ser.is_valid(raise_exception=True)
                approved_ret = ser.save()
                approved.append(approved_ret)
            except Exception as e:
                errors.append({"id": str(ret.id), "error": str(e)})

        return Response({
            "approved_count": len(approved),
            "errors_count": len(errors),
            "errors": errors,
            "items": ReturnReadSerializer(approved, many=True).data,
        }, status=status.HTTP_200_OK)


# ===========================
#  Return: reject (идемпотентно)
# ===========================
class ReturnFromAgentRejectAPIView(APIView, CompanyBranchRestrictedMixin):
    """
    POST /api/main/returns/<uuid:pk>/reject/
    Отклонение возврата (статус -> rejected).
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        try:
            ret = (
                ReturnFromAgent.objects
                .select_for_update()
                .select_related("subreal__product")
                .get(pk=pk, company_id=self._company().id)
            )
        except ReturnFromAgent.DoesNotExist:
            return Response({"detail": "Возврат не найден."}, status=status.HTTP_404_NOT_FOUND)

        # идемпотентность: если уже не pending — просто вернуть текущее состояние
        if ret.status != ReturnFromAgent.Status.PENDING:
            return Response(ReturnReadSerializer(ret).data, status=status.HTTP_200_OK)

        ser = ReturnRejectSerializer(
            data=request.data,
            context={"request": request, "return_obj": ret},
        )
        ser.is_valid(raise_exception=True)
        ret = ser.save()
        return Response(ReturnReadSerializer(ret).data, status=status.HTTP_200_OK)


# ===========================
#  Finished goods → raw material (частичное перемещение)
# ===========================
def _fmt_qty(value) -> str:
    """80.000 → '80', 80.500 → '80.5' (для человекочитаемых сообщений)."""
    d = _to_dec(value, default=Decimal("0"))
    d = d.normalize()
    # normalize() у целых даёт экспоненту (8E+1) — приводим обратно
    if d == d.to_integral():
        d = d.quantize(Decimal("1"))
    return format(d, "f")


class ProductMoveToRawAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/products/<uuid:product_id>/move-to-raw/

    Частично перемещает готовую продукцию в сырьё (ItemMake).
    Доступ: как у «Передать товар» — авторизованный сотрудник компании.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, product_id, *args, **kwargs):
        company = self._company()
        if company is None:
            return Response({"detail": "У вас не задана компания."}, status=status.HTTP_400_BAD_REQUEST)

        ser = FinishedToRawMoveInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        qty = ser.validated_data["quantity"]
        reason = ser.validated_data.get("reason") or ""

        try:
            product = (
                Product.objects.select_for_update()
                .get(pk=product_id, company=company)
            )
        except Product.DoesNotExist:
            return Response({"detail": "Товар не найден."}, status=status.HTTP_404_NOT_FOUND)

        available = product.quantity or Decimal("0")
        if qty > available:
            return Response(
                {"detail": f"Нельзя переместить больше, чем есть. Доступно: {_fmt_qty(available)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 1) списываем из готовой продукции
        Product.objects.filter(pk=product.pk).update(quantity=F("quantity") - qty, updated_at=timezone.now())

        # 2) находим/создаём запись сырья (мэппинг по company+branch+name+kind=raw)
        raw_item = (
            ItemMake.objects.select_for_update()
            .filter(
                company=company,
                branch=product.branch,
                name=product.name,
                kind=ItemMake.Kind.RAW,
            )
            .first()
        )
        if raw_item is None:
            raw_item = ItemMake.objects.create(
                company=company,
                branch=product.branch,
                name=product.name,
                unit=product.unit or "шт.",
                price=product.purchase_price or Decimal("0"),
                quantity=qty,
                kind=ItemMake.Kind.RAW,
            )
        else:
            ItemMake.objects.filter(pk=raw_item.pk).update(quantity=F("quantity") + qty)
        raw_item.refresh_from_db(fields=["quantity"])

        # 3) фиксируем перемещение
        transfer = FinishedToRawTransfer.objects.create(
            company=company,
            branch=product.branch,
            product=product,
            raw_item=raw_item,
            quantity=qty,
            reason=reason,
            status=FinishedToRawTransfer.Status.DONE,
            user=request.user,
        )

        product.refresh_from_db(fields=["quantity"])
        self._send_product_webhook(product.pk)

        # Журнал: уход из готовой продукции + приход в сырьё
        raw_before = (raw_item.quantity or Decimal("0")) - qty
        record_stock_movement(
            company=company, branch=product.branch, type=StockMovement.Type.TRANSFER,
            object_id=product.pk, product_name=product.name,
            warehouse=StockMovement.Warehouse.FINISHED_GOODS,
            qty_before=available, change=-qty, qty_after=product.quantity,
            created_by=request.user, comment=reason,
            ref_type="finished_to_raw_transfer", ref_id=transfer.id,
        )
        record_stock_movement(
            company=company, branch=product.branch, type=StockMovement.Type.TRANSFER,
            object_id=raw_item.pk, product_name=raw_item.name,
            warehouse=StockMovement.Warehouse.RAW_MATERIALS,
            qty_before=raw_before, change=qty, qty_after=raw_item.quantity,
            created_by=request.user, comment=reason,
            ref_type="finished_to_raw_transfer", ref_id=transfer.id,
        )

        data = FinishedToRawTransferSerializer(transfer).data
        data["product_remaining"] = _fmt_qty(product.quantity)
        data["raw_total"] = _fmt_qty(raw_item.quantity)
        return Response(data, status=status.HTTP_201_CREATED)

    @staticmethod
    def _send_product_webhook(product_id):
        def _send():
            try:
                from apps.main.services.webhooks import send_product_webhook
                prod = Product.objects.get(pk=product_id)
                send_product_webhook(prod, "product.updated")
            except Exception:
                logging.getLogger("crm.webhooks").error(
                    "Failed to send product.updated webhook after move-to-raw. product_id=%s",
                    product_id, exc_info=True,
                )
        try:
            transaction.on_commit(_send)
        except Exception:
            _send()


class FinishedToRawTransferListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    GET /api/main/finished-to-raw-transfers/
    Query (опц.): product, status, date_from, date_to, limit, offset.
    """
    serializer_class = FinishedToRawTransferSerializer
    queryset = FinishedToRawTransfer.objects.select_related("product", "user").all()
    pagination_class = LimitOffsetPagination

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params

        product_id = (params.get("product") or "").strip()
        if product_id:
            qs = qs.filter(product_id=product_id)

        status_q = (params.get("status") or "").strip()
        if status_q in (FinishedToRawTransfer.Status.DONE, FinishedToRawTransfer.Status.CANCELED):
            qs = qs.filter(status=status_q)

        df_raw = (params.get("date_from") or "").strip()
        if df_raw and parse_date(df_raw):
            qs = qs.filter(created_at__date__gte=parse_date(df_raw))
        dt_raw = (params.get("date_to") or "").strip()
        if dt_raw and parse_date(dt_raw):
            qs = qs.filter(created_at__date__lte=parse_date(dt_raw))

        return qs


class FinishedToRawTransferCancelAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/finished-to-raw-transfers/<uuid:pk>/cancel/
    Отмена перемещения: возврат количества в готовую продукцию и списание из сырья.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        company = self._company()
        if company is None:
            return Response({"detail": "У вас не задана компания."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            transfer = (
                FinishedToRawTransfer.objects.select_for_update()
                .select_related("product", "raw_item")
                .get(pk=pk, company=company)
            )
        except FinishedToRawTransfer.DoesNotExist:
            return Response({"detail": "Перемещение не найдено."}, status=status.HTTP_404_NOT_FOUND)

        if transfer.status == FinishedToRawTransfer.Status.CANCELED:
            return Response({"detail": "Перемещение уже отменено."}, status=status.HTTP_400_BAD_REQUEST)

        qty = transfer.quantity

        # списываем из сырья (если сырьё израсходовано — конфликт)
        raw_item = None
        if transfer.raw_item_id:
            raw_item = ItemMake.objects.select_for_update().get(pk=transfer.raw_item_id)
            raw_before = raw_item.quantity or Decimal("0")
            if raw_before < qty:
                return Response(
                    {"detail": "Невозможно отменить: сырьё уже израсходовано."},
                    status=status.HTTP_409_CONFLICT,
                )
            ItemMake.objects.filter(pk=raw_item.pk).update(quantity=F("quantity") - qty)

        # возвращаем количество в готовую продукцию
        product = Product.objects.select_for_update().get(pk=transfer.product_id)
        prod_before = product.quantity or Decimal("0")
        Product.objects.filter(pk=product.pk).update(quantity=F("quantity") + qty)

        transfer.status = FinishedToRawTransfer.Status.CANCELED
        transfer.canceled_at = timezone.now()
        transfer.canceled_by = request.user
        transfer.save(update_fields=["status", "canceled_at", "canceled_by"])

        ProductMoveToRawAPIView._send_product_webhook(transfer.product_id)

        # Журнал отмены: возврат в готовую продукцию + списание из сырья
        record_stock_movement(
            company=company, branch=product.branch, type=StockMovement.Type.TRANSFER,
            object_id=product.pk, product_name=product.name,
            warehouse=StockMovement.Warehouse.FINISHED_GOODS,
            qty_before=prod_before, change=qty, qty_after=prod_before + qty,
            created_by=request.user, comment="Отмена перемещения в сырьё",
            ref_type="finished_to_raw_transfer", ref_id=transfer.id,
        )
        if raw_item is not None:
            record_stock_movement(
                company=company, branch=product.branch, type=StockMovement.Type.TRANSFER,
                object_id=raw_item.pk, product_name=raw_item.name,
                warehouse=StockMovement.Warehouse.RAW_MATERIALS,
                qty_before=raw_before, change=-qty, qty_after=raw_before - qty,
                created_by=request.user, comment="Отмена перемещения в сырьё",
                ref_type="finished_to_raw_transfer", ref_id=transfer.id,
            )

        return Response(
            {"id": str(transfer.id), "status": transfer.status, "canceled_at": transfer.canceled_at},
            status=status.HTTP_200_OK,
        )


# ===========================
#  Инвентаризация (сверка остатков)
# ===========================
def _inventory_apply_adjustments(inventory, company, by_user=None):
    """Приводит учётные остатки к фактическим по позициям инвентаризации + журнал."""
    is_fg = inventory.warehouse == Inventory.Warehouse.FINISHED_GOODS
    model = Product if is_fg else ItemMake
    warehouse = (
        StockMovement.Warehouse.FINISHED_GOODS if is_fg else StockMovement.Warehouse.RAW_MATERIALS
    )
    author = by_user or inventory.user
    for item in inventory.items.all():
        model.objects.filter(pk=item.object_id, company=company).update(quantity=item.qty_fact)
        if item.diff:
            record_stock_movement(
                company=company, branch=inventory.branch, type=StockMovement.Type.INVENTORY,
                object_id=item.object_id, product_name=item.product_name,
                warehouse=warehouse,
                qty_before=item.qty_system, change=item.diff, qty_after=item.qty_fact,
                created_by=author, comment=inventory.comment,
                ref_type="inventory", ref_id=inventory.id,
            )


class InventoryListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    """
    GET  /api/main/inventories/  — история (фильтры warehouse/status/date_from/date_to)
    POST /api/main/inventories/  — создать (draft | confirmed)
    """
    permission_classes = [permissions.IsAuthenticated]
    queryset = Inventory.objects.select_related("company", "branch", "user").prefetch_related("items")
    pagination_class = LimitOffsetPagination

    def get_serializer_class(self):
        if self.request.method == "POST":
            return InventoryCreateSerializer
        return InventoryReadSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        qp = self.request.query_params

        warehouse = (qp.get("warehouse") or "").strip()
        if warehouse:
            qs = qs.filter(warehouse=warehouse)

        status_q = (qp.get("status") or "").strip()
        if status_q:
            qs = qs.filter(status=status_q)

        df_raw = (qp.get("date_from") or "").strip()
        if df_raw:
            df = parse_date(df_raw)
            if df is None:
                raise ValidationError({"date_from": ["Некорректная дата."]})
            qs = qs.filter(created_at__date__gte=df)
        dt_raw = (qp.get("date_to") or "").strip()
        if dt_raw:
            dt = parse_date(dt_raw)
            if dt is None:
                raise ValidationError({"date_to": ["Некорректная дата."]})
            qs = qs.filter(created_at__date__lte=dt)

        return qs

    @idempotent(body_key=True)  # ТЗ Н-08: повтор ключом — прежний ответ, до проверок
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        company = self._company()
        if company is None:
            return Response({"detail": "У вас не задана компания."}, status=status.HTTP_400_BAD_REQUEST)
        branch = self._auto_branch()

        ser = InventoryCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        warehouse = data["warehouse"]
        rows = data["items"]

        ref_model = Product if warehouse == Inventory.Warehouse.FINISHED_GOODS else ItemMake

        inventory = Inventory.objects.create(
            company=company,
            branch=branch,
            warehouse=warehouse,
            status=Inventory.Status.DRAFT,
            comment=data.get("comment") or "",
            user=request.user,
        )

        for row in rows:
            obj = ref_model.objects.filter(pk=row["product"], company=company).first()
            if obj is None:
                raise NotFound("Товар не найден.")
            qty_system = row["qty_system"]
            qty_fact = row["qty_fact"]
            InventoryItem.objects.create(
                inventory=inventory,
                object_id=obj.pk,
                product_name=getattr(obj, "name", "") or "",
                qty_system=qty_system,
                qty_fact=qty_fact,
                diff=(qty_fact - qty_system),
            )

        inventory.recompute_totals()

        # сразу подтверждаем, если запрошено
        if data.get("status") == Inventory.Status.CONFIRMED:
            _inventory_apply_adjustments(inventory, company)
            inventory.status = Inventory.Status.CONFIRMED
            inventory.confirmed_at = timezone.now()
            inventory.save(update_fields=["status", "confirmed_at"])

        inventory.refresh_from_db()
        out = InventoryReadSerializer(inventory, context=self.get_serializer_context())
        return Response(out.data, status=status.HTTP_201_CREATED)


class InventoryRetrieveAPIView(CompanyBranchRestrictedMixin, generics.RetrieveAPIView):
    """GET /api/main/inventories/<uuid:pk>/"""
    permission_classes = [permissions.IsAuthenticated]
    queryset = Inventory.objects.select_related("company", "branch", "user").prefetch_related("items")
    serializer_class = InventoryReadSerializer


class InventoryConfirmAPIView(CompanyBranchRestrictedMixin, APIView):
    """POST /api/main/inventories/<uuid:pk>/confirm/ — провести корректировку остатков."""
    permission_classes = [permissions.IsAuthenticated]

    @idempotent(body_key=True)  # ТЗ Н-08: повтор ключом — прежний ответ, до проверок
    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        company = self._company()
        if company is None:
            return Response({"detail": "У вас не задана компания."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            inventory = (
                Inventory.objects.select_for_update()
                .prefetch_related("items")
                .get(pk=pk, company=company)
            )
        except Inventory.DoesNotExist:
            return Response({"detail": "Инвентаризация не найдена."}, status=status.HTTP_404_NOT_FOUND)

        if inventory.status != Inventory.Status.DRAFT:
            return Response(
                {"detail": "Инвентаризация уже подтверждена."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        _inventory_apply_adjustments(inventory, company, by_user=request.user)
        inventory.status = Inventory.Status.CONFIRMED
        inventory.confirmed_at = timezone.now()
        inventory.save(update_fields=["status", "confirmed_at"])

        return Response(
            {"id": str(inventory.id), "status": inventory.status, "confirmed_at": inventory.confirmed_at},
            status=status.HTTP_200_OK,
        )


# ===========================
#  Нехватка готовой продукции (событие + уведомления)
# ===========================
def _shortage_recipients(company, branch=None):
    """Ответственные сотрудники: владелец(ы) компании + роли owner/admin."""
    qs = User.objects.filter(
        Q(owned_company=company) | Q(company=company, role__in=["owner", "admin"])
    ).distinct()
    return list(qs)


def report_stock_shortage(*, company, branch, product, requested_qty, available_qty,
                          agent=None, source=StockShortageEvent.Source.TRANSFER, created_by):
    """
    Фиксирует событие нехватки и рассылает уведомления ответственным.
    Возвращает (event, notified_count). Не бросает исключений по уведомлениям.
    """
    event = StockShortageEvent.objects.create(
        company=company,
        branch=branch,
        product=product,
        requested_qty=requested_qty,
        available_qty=available_qty,
        agent=agent,
        source=source,
        created_by=created_by,
    )

    product_name = getattr(product, "name", None) or "товар"
    agent_name = ""
    if agent is not None:
        agent_name = (
            f"{(agent.first_name or '').strip()} {(agent.last_name or '').strip()}".strip()
            or getattr(agent, "email", "") or ""
        )
    when = timezone.localtime(event.created_at).strftime("%d.%m.%Y %H:%M")
    who = f"Агент {agent_name} запросил" if agent_name else "Запрошен"
    message = (
        f"{who} «{product_name}»: {requested_qty} шт, "
        f"доступно {available_qty} шт. {when}"
    )

    from apps.main.realtime import create_and_publish_notification

    notified = 0
    shortage_data = {
        "product": str(product.pk),
        "requested_qty": str(requested_qty),
        "available_qty": str(available_qty),
        "agent": str(agent.pk) if agent is not None else None,
    }
    for recipient in _shortage_recipients(company, branch):
        try:
            create_and_publish_notification(
                company=company, branch=branch, user=recipient, message=message,
                type="stock_shortage", title="Нехватка товара на складе",
                level="high", actor=created_by, data=shortage_data,
            )
            notified += 1
        except Exception:
            logging.getLogger("crm.webhooks").error(
                "Failed to create stock-shortage notification for user=%s",
                getattr(recipient, "id", None), exc_info=True,
            )
    return event, notified


class StockShortageCreateAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/stock-shortages/
    Фиксирует нехватку (журнал) и уведомляет ответственных.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        company = self._company()
        if company is None:
            return Response({"detail": "У вас не задана компания."}, status=status.HTTP_400_BAD_REQUEST)
        branch = self._auto_branch()

        ser = StockShortageEventCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        product = Product.objects.filter(pk=data["product"], company=company).first()
        if product is None:
            return Response({"detail": "Товар не найден."}, status=status.HTTP_404_NOT_FOUND)

        agent = None
        agent_id = data.get("agent")
        if agent_id:
            agent = User.objects.filter(pk=agent_id, company=company).first()

        event, notified = report_stock_shortage(
            company=company,
            branch=branch,
            product=product,
            requested_qty=data["requested_qty"],
            available_qty=data["available_qty"],
            agent=agent,
            source=data.get("source") or StockShortageEvent.Source.TRANSFER,
            created_by=request.user,
        )

        out = StockShortageEventReadSerializer(event).data
        out["notified"] = notified
        return Response(out, status=status.HTTP_201_CREATED)


# ===========================
#  Журнал движения склада (read-only)
# ===========================
class StockMovementListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    GET /api/main/stock-movements/
    Фильтры: type, warehouse, date_from, date_to, search, product, employee, agent.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = StockMovementReadSerializer
    queryset = StockMovement.objects.select_related("company", "branch").all()
    pagination_class = LimitOffsetPagination

    def get_queryset(self):
        qs = super().get_queryset()
        qp = self.request.query_params

        type_q = (qp.get("type") or "").strip()
        if type_q:
            if type_q not in StockMovement.Type.values:
                raise ValidationError({"type": ["Недопустимый тип операции."]})
            qs = qs.filter(type=type_q)

        warehouse = (qp.get("warehouse") or "").strip()
        if warehouse:
            qs = qs.filter(warehouse=warehouse)

        df_raw = (qp.get("date_from") or "").strip()
        if df_raw:
            df = parse_date(df_raw)
            if df is None:
                raise ValidationError({"date_from": ["Некорректная дата."]})
            qs = qs.filter(created_at__date__gte=df)
        dt_raw = (qp.get("date_to") or "").strip()
        if dt_raw:
            dt = parse_date(dt_raw)
            if dt is None:
                raise ValidationError({"date_to": ["Некорректная дата."]})
            qs = qs.filter(created_at__date__lte=dt)

        product_id = (qp.get("product") or "").strip()
        if product_id:
            qs = qs.filter(object_id=product_id)

        employee_id = (qp.get("employee") or "").strip()
        if employee_id:
            qs = qs.filter(Q(sender_id=employee_id) | Q(receiver_id=employee_id) | Q(created_by_id=employee_id))

        agent_id = (qp.get("agent") or "").strip()
        if agent_id:
            qs = qs.filter(Q(sender_id=agent_id) | Q(receiver_id=agent_id))

        search = (qp.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(product_name__icontains=search)
                | Q(sender_name__icontains=search)
                | Q(receiver_name__icontains=search)
                | Q(source_name__icontains=search)
                | Q(target_name__icontains=search)
            )

        return qs


class StockMovementDetailAPIView(CompanyBranchRestrictedMixin, generics.RetrieveAPIView):
    """GET /api/main/stock-movements/<uuid:pk>/"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = StockMovementReadSerializer
    queryset = StockMovement.objects.select_related("company", "branch").all()


class ProductionReportListAPIView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    GET /api/main/production/report/
    Что произведено за период. Фильтры: date_from, date_to, shift (day|night), search.

    summary считается по всему периоду с учётом фильтров, а не по текущей странице.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ProductionRecordReadSerializer
    queryset = ProductionRecord.objects.select_related("company", "branch", "product").all()
    pagination_class = LimitOffsetPagination

    def get_queryset(self):
        qs = super().get_queryset()
        qp = self.request.query_params

        df_raw = (qp.get("date_from") or "").strip()
        if df_raw:
            df = parse_date(df_raw)
            if df is None:
                raise ValidationError({"date_from": ["Некорректная дата."]})
            qs = qs.filter(produced_at__date__gte=df)

        dt_raw = (qp.get("date_to") or "").strip()
        if dt_raw:
            dt = parse_date(dt_raw)
            if dt is None:
                raise ValidationError({"date_to": ["Некорректная дата."]})
            qs = qs.filter(produced_at__date__lte=dt)

        shift = (qp.get("shift") or "").strip().lower()
        if shift:
            if shift not in ProductionRecord.Shift.values:
                raise ValidationError({"shift": ["Допустимые значения: day, night."]})
            qs = qs.filter(shift=shift)

        search = (qp.get("search") or "").strip()
        if search:
            qs = qs.filter(product_name__icontains=search)

        return qs.order_by("-produced_at", "-id")

    def _summary(self, qs) -> dict:
        qty_field = DecimalField(max_digits=14, decimal_places=3)
        zero_qty = V(Decimal("0.000"), output_field=qty_field)
        money_field = DecimalField(max_digits=12, decimal_places=2)
        zero_money = V(Decimal("0.00"), output_field=money_field)

        agg = qs.aggregate(
            total_quantity=Coalesce(Sum("quantity", output_field=qty_field), zero_qty),
            day_quantity=Coalesce(
                Sum("quantity", filter=Q(shift=ProductionRecord.Shift.DAY), output_field=qty_field),
                zero_qty,
            ),
            night_quantity=Coalesce(
                Sum("quantity", filter=Q(shift=ProductionRecord.Shift.NIGHT), output_field=qty_field),
                zero_qty,
            ),
            total_cost=Coalesce(Sum("cost_total", output_field=money_field), zero_money),
        )
        return {
            "total_quantity": str(agg["total_quantity"]),
            "day_quantity": str(agg["day_quantity"]),
            "night_quantity": str(agg["night_quantity"]),
            "total_cost": str(agg["total_cost"]),
        }

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        summary = self._summary(qs)

        page = self.paginate_queryset(qs)
        if page is not None:
            data = self.get_serializer(page, many=True).data
            response = self.get_paginated_response(data)
            response.data["summary"] = summary
            return response

        data = self.get_serializer(qs, many=True).data
        return Response({"count": qs.count(), "summary": summary, "results": data})


# ===========================
#  Subreal: bulk create
# ===========================
class ManufactureSubrealBulkCreateAPIView(APIView, CompanyBranchRestrictedMixin):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        ser = BulkSubrealCreateSerializer(
            data=request.data,
            context={"request": request},
        )
        ser.is_valid(raise_exception=True)
        company = self._company()
        branch = self._auto_branch()
        # Защита остатка + фиксация нехватки ВНЕ транзакции выдачи,
        # чтобы StockShortageEvent сохранился даже при отклонении передачи.
        self._guard_bulk_stock(
            request, company, branch,
            ser.validated_data["agent"], ser.validated_data["items"],
        )
        return self._create_transfers(request)

    def _guard_bulk_stock(self, request, company, branch, agent, items):
        shortages = []
        for item in items:
            product = item["product"]
            qty = int(item["qty_transferred"] or 0)
            if not qty:
                continue
            avail = type(product).objects.filter(pk=product.pk).values_list("quantity", flat=True).first()
            avail = avail if avail is not None else 0
            if avail < qty:
                shortages.append((product, qty, avail))
        if not shortages:
            return
        for product, qty, avail in shortages:
            report_stock_shortage(
                company=company, branch=branch, product=product,
                requested_qty=qty, available_qty=avail, agent=agent,
                source=StockShortageEvent.Source.TRANSFER, created_by=request.user,
            )
        p, q, a = shortages[0]
        raise serializers.ValidationError({
            "detail": f'Недостаточно товара «{p.name}». Запрошено {q}, доступно {a}.',
            "shortage": {"product": str(p.pk), "requested_qty": q, "available_qty": a},
        })

    @transaction.atomic
    def _create_transfers(self, request):
        ser = BulkSubrealCreateSerializer(
            data=request.data,
            context={"request": request},
        )
        ser.is_valid(raise_exception=True)

        agent = ser.validated_data["agent"]
        items = ser.validated_data["items"]
        user = self._user()
        company = self._company()
        branch = self._auto_branch()
        transfer_ref = str(uuid4())

        created_objs = []

        for idx, item in enumerate(items):
            product = item["product"]
            qty = int(item["qty_transferred"])
            is_sawmill = bool(item.get("is_sawmill", False))

            locked_qs = type(product).objects.select_for_update().filter(pk=product.pk)
            current_qty = locked_qs.values_list("quantity", flat=True).first()
            if current_qty is None or current_qty < qty:
                raise serializers.ValidationError({
                    "items": f"Недостаточно на складе для {product.name}: доступно {current_qty or 0}."
                })

            # списываем со склада
            locked_qs.update(quantity=F("quantity") - qty)
            prod_model = type(product)
            prod_id = product.pk

            def _send_webhook():
                from apps.main.services.webhooks import send_product_webhook

                try:
                    p = prod_model.objects.get(pk=prod_id)
                    send_product_webhook(p, "product.updated")
                except Exception:
                    logging.getLogger("crm.webhooks").error(
                        "Failed to send product.updated webhook after subreal bulk create. product_id=%s",
                        prod_id,
                        exc_info=True,
                    )

            try:
                transaction.on_commit(_send_webhook)
            except Exception:
                _send_webhook()

            # Уникальный external_ref на строку: в БД uniq (company, external_ref) при non-null ref.
            sub = ManufactureSubreal.objects.create(
                company=company,
                branch=branch,
                user=user,
                agent=agent,
                product=product,
                external_ref=f"{transfer_ref}:{idx}",
                qty_transferred=qty,
                is_sawmill=is_sawmill,
            )
            created_objs.append(sub)

            # Журнал: передача агенту
            qty_before = current_qty or Decimal("0")
            record_stock_movement(
                company=company, branch=branch, type=StockMovement.Type.AGENT_TRANSFER,
                object_id=product.pk, product_name=product.name,
                warehouse=StockMovement.Warehouse.FINISHED_GOODS,
                qty_before=qty_before, change=-qty, qty_after=qty_before - qty,
                created_by=user, sender=user, receiver=agent,
                ref_type="subreal", ref_id=sub.id,
            )

            # авто-принятие для распила / пилорамы
            if is_sawmill:
                sub.refresh_from_db(fields=["qty_transferred", "qty_accepted", "status"])
                to_accept = int((sub.qty_transferred or 0) - (sub.qty_accepted or 0))
                if to_accept > 0 and sub.status == ManufactureSubreal.Status.OPEN:
                    Acceptance.objects.create(
                        company=company,
                        branch=branch,
                        subreal=sub,
                        accepted_by=user,
                        qty=to_accept,
                        accepted_at=timezone.now(),
                    )

        out = ManufactureSubrealSerializer(
            created_objs,
            many=True,
            context={"request": request},
        ).data
        return Response(out, status=status.HTTP_201_CREATED)


# ===========================
#  Agent: my products (GET/PATCH)
# ===========================
class AgentMyProductsListAPIView(APIView, CompanyBranchRestrictedMixin):
    """
    GET  /api/main/agents/me/products/
    PATCH /api/main/agents/me/products/
      — частичное обновление qty_accepted/qty_returned
    """
    permission_classes = [permissions.IsAuthenticated]

    # -------- Helpers --------
    def _build_queryset(self, request):
        accepted_returns_qs = ReturnFromAgent.objects.filter(
            status=ReturnFromAgent.Status.ACCEPTED
        )
        alloc_qs = AgentSaleAllocation.objects.only("id", "subreal_id", "qty")

        base = (
            ManufactureSubreal.objects
            .filter(agent_id=request.user.id)
            .select_related("product")
            .prefetch_related(
                "acceptances",
                Prefetch(
                    "returns",
                    queryset=accepted_returns_qs,
                    to_attr="accepted_returns",
                ),
                Prefetch(
                    "sale_allocations",
                    queryset=alloc_qs,
                    to_attr="prefetched_allocs",
                ),
            )
            .annotate(sold_qty=Coalesce(Sum("sale_allocations__qty"), V(0)))
            .order_by("product_id", "-created_at")
        )
        base = self._filter_qs_company_branch(base)

        term = (request.query_params.get("search") or "").strip()
        if term:
            q = (
                Q(product__name__icontains=term)
                | Q(product__barcode__icontains=term)
                | Q(product__article__icontains=term)
                | Q(product__code__icontains=term)
            )
            if term.isdigit():
                q |= Q(product__plu=int(term))
            base = base.filter(q)

        return base

    @staticmethod
    def _nz(v: Optional[int]) -> int:
        return int(v or 0)

    def _serialize_products(self, qs) -> List[Dict[str, Any]]:
        def _sold_for_sub(s):
            ann = self._nz(getattr(s, "sold_qty", 0))
            if ann:
                return ann
            return sum(self._nz(a.qty) for a in getattr(s, "prefetched_allocs", []))

        data = []

        for product_id, subreals_iter in groupby(qs, key=attrgetter("product_id")):
            subreals = list(subreals_iter)

            qty_on_hand = 0
            for s in subreals:
                accepted = self._nz(s.qty_accepted)
                returned = self._nz(s.qty_returned)
                sold = _sold_for_sub(s)
                qty_on_hand += max(accepted - returned - sold, 0)

            if qty_on_hand <= 0:
                continue

            movement_dates: List[datetime] = []
            for s in subreals:
                if s.created_at:
                    movement_dates.append(s.created_at)
                for acc in s.acceptances.all():
                    if getattr(acc, "accepted_at", None):
                        movement_dates.append(acc.accepted_at)
                for ret in getattr(s, "accepted_returns", []):
                    if getattr(ret, "accepted_at", None):
                        movement_dates.append(ret.accepted_at)

            last_movement_at = max(movement_dates) if movement_dates else None

            subreals_payload = []
            for s in subreals:
                accepted = self._nz(s.qty_accepted)
                returned = self._nz(s.qty_returned)
                sold = _sold_for_sub(s)
                subreals_payload.append({
                    "id": s.id,
                    "created_at": s.created_at,
                    "qty_transferred": self._nz(s.qty_transferred),
                    "qty_accepted": accepted,
                    "qty_returned": returned,
                    "qty_sold": sold,
                    "qty_on_hand": max(accepted - returned - sold, 0),
                })

            data.append({
                "product": product_id,
                "product_name": (
                    subreals[0].product.name
                    if (subreals and getattr(subreals[0], "product", None))
                    else ""
                ),
                "qty_on_hand": qty_on_hand,
                "last_movement_at": last_movement_at,
                "subreals": subreals_payload,
            })

        return data

    # -------- GET --------
    def get(self, request, *args, **kwargs):
        company_id = getattr(request.user, "company_id", None)
        if not company_id:
            return Response([], status=status.HTTP_200_OK)

        qs = self._build_queryset(request)
        data = self._serialize_products(qs)
        return Response(
            AgentProductOnHandSerializer(data, many=True).data,
            status=status.HTTP_200_OK,
        )

    # -------- PATCH --------
    def patch(self, request, *args, **kwargs):
        """
        Частичное обновление qty_accepted / qty_returned по конкретным передачам.
        Предпочтительно создавать Acceptance/Return события, а не трогать счётчики,
        но PATCH оставляем как "ручной корректор".
        """
        company_id = getattr(request.user, "company_id", None)
        if not company_id:
            return Response(
                {"detail": "No company bound."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = request.data or {}
        if not isinstance(payload, dict) or "subreals" not in payload:
            raise ValidationError({"subreals": "Required list of updates."})

        items = payload["subreals"]
        if not isinstance(items, list) or not items:
            raise ValidationError({"subreals": "Must be a non-empty list."})

        ids: List[str] = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                raise ValidationError({f"subreals[{i}]": "Must be an object."})
            if "id" not in it:
                raise ValidationError({f"subreals[{i}].id": "Required."})

            # validate UUID
            try:
                ids.append(str(UUID(str(it["id"]))))
            except Exception:
                raise ValidationError({f"subreals[{i}].id": "Must be UUID string."})

            allowed_keys = {"qty_accepted", "qty_returned"}
            unknown = set(it.keys()) - ({"id"} | allowed_keys)
            if unknown:
                raise ValidationError({
                    f"subreals[{i}]": f"Unknown fields: {', '.join(sorted(unknown))}"
                })

            for f in allowed_keys & set(it.keys()):
                v = it[f]
                if v is None:
                    continue
                if not isinstance(v, int):
                    raise ValidationError({f"subreals[{i}].{f}": "Must be integer."})
                if v < 0:
                    raise ValidationError({f"subreals[{i}].{f}": "Must be >= 0."})

        base_qs = ManufactureSubreal.objects.filter(
            id__in=ids,
            agent_id=request.user.id,
        )
        base_qs = self._filter_qs_company_branch(base_qs)

        subreal_map = {
            str(s.id): s
            for s in base_qs.select_for_update()
        }
        missing = [sid for sid in ids if sid not in subreal_map]
        if missing:
            raise ValidationError({
                "subreals": f"Not found or not allowed: {missing}"
            })

        to_update = []
        with transaction.atomic():
            for it in items:
                sid = str(UUID(str(it["id"])))
                s = subreal_map[sid]

                new_accepted = s.qty_accepted or 0
                new_returned = s.qty_returned or 0
                transferred = s.qty_transferred or 0

                if "qty_accepted" in it and it["qty_accepted"] is not None:
                    new_accepted = it["qty_accepted"]
                if "qty_returned" in it and it["qty_returned"] is not None:
                    new_returned = it["qty_returned"]

                if new_accepted > transferred:
                    raise ValidationError({
                        f"id={s.id}": (
                            f"qty_accepted ({new_accepted}) "
                            f"must be <= qty_transferred ({transferred})"
                        )
                    })
                if new_returned > new_accepted:
                    raise ValidationError({
                        f"id={s.id}": (
                            f"qty_returned ({new_returned}) "
                            f"must be <= qty_accepted ({new_accepted})"
                        )
                    })

                changed = False
                if new_accepted != (s.qty_accepted or 0):
                    s.qty_accepted = new_accepted
                    changed = True
                if new_returned != (s.qty_returned or 0):
                    s.qty_returned = new_returned
                    changed = True
                if changed:
                    to_update.append(s)

            if to_update:
                ManufactureSubreal.objects.bulk_update(
                    to_update,
                    ["qty_accepted", "qty_returned"],
                )

        qs = self._build_queryset(request)
        data = self._serialize_products(qs)
        return Response(
            AgentProductOnHandSerializer(data, many=True).data,
            status=status.HTTP_200_OK,
        )


# ===========================
#  Owner: agents products
# ===========================
class OwnerAgentsProductsListAPIView(APIView, CompanyBranchRestrictedMixin):
    """
    GET /api/main/owner/agents/products/

    Возвращает по КАЖДОМУ агенту список его товаров "на руках"
    (как /agents/me/products), плюс данные агента.
    """
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _nz(v: Optional[int]) -> int:
        return int(v or 0)

    def _build_queryset(self, request):
        accepted_returns_qs = ReturnFromAgent.objects.filter(
            status=ReturnFromAgent.Status.ACCEPTED
        )
        alloc_qs = AgentSaleAllocation.objects.only(
            "id", "subreal_id", "qty"
        )

        base = (
            ManufactureSubreal.objects
            .select_related("product", "agent")
            .prefetch_related(
                "acceptances",
                Prefetch(
                    "returns",
                    queryset=accepted_returns_qs,
                    to_attr="accepted_returns",
                ),
                Prefetch(
                    "sale_allocations",
                    queryset=alloc_qs,
                    to_attr="prefetched_allocs",
                ),
            )
            .annotate(sold_qty=Coalesce(Sum("sale_allocations__qty"), V(0)))
            .order_by("agent_id", "product_id", "-created_at")
        )
        base = self._filter_qs_company_branch(base)

        # date filters (optional): ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
        q = getattr(request, "query_params", getattr(request, "GET", {}))
        date_from_raw = (q.get("date_from") or "").strip()
        date_to_raw = (q.get("date_to") or "").strip()
        if date_from_raw or date_to_raw:
            try:
                df = _date.fromisoformat(date_from_raw) if date_from_raw else None
            except Exception:
                df = None
            try:
                dt = _date.fromisoformat(date_to_raw) if date_to_raw else None
            except Exception:
                dt = None

            if df or dt:
                today = timezone.localdate()
                df = df or dt or today
                dt = dt or df
                if df > dt:
                    df, dt = dt, df

                dt_from = timezone.make_aware(datetime.combine(df, datetime.min.time()))
                dt_to = timezone.make_aware(datetime.combine(dt, datetime.max.time()))
                base = base.filter(created_at__range=(dt_from, dt_to))

        term = (request.query_params.get("search") or "").strip()
        if term:
            q = (
                Q(product__name__icontains=term)
                | Q(product__barcode__icontains=term)
                | Q(product__article__icontains=term)
                | Q(product__code__icontains=term)
            )
            if term.isdigit():
                q |= Q(product__plu=int(term))
            base = base.filter(q)

        return base

    def _serialize_products_for_agent(self, subreals_qs) -> List[Dict[str, Any]]:
        def _sold_for_sub(s):
            ann = self._nz(getattr(s, "sold_qty", 0))
            if ann:
                return ann
            return sum(
                self._nz(a.qty)
                for a in getattr(s, "prefetched_allocs", [])
            )

        data: List[Dict[str, Any]] = []

        for product_id, subreals_iter in groupby(
            subreals_qs,
            key=attrgetter("product_id"),
        ):
            subreals = list(subreals_iter)

            qty_on_hand = 0
            for s in subreals:
                accepted = self._nz(s.qty_accepted)
                returned = self._nz(s.qty_returned)
                sold = _sold_for_sub(s)
                qty_on_hand += max(accepted - returned - sold, 0)

            if qty_on_hand <= 0:
                continue

            movement_dates: List[datetime] = []
            for s in subreals:
                if s.created_at:
                    movement_dates.append(s.created_at)
                for acc in s.acceptances.all():
                    if getattr(acc, "accepted_at", None):
                        movement_dates.append(acc.accepted_at)
                for ret in getattr(s, "accepted_returns", []):
                    if getattr(ret, "accepted_at", None):
                        movement_dates.append(ret.accepted_at)

            last_movement_at = max(movement_dates) if movement_dates else None

            subreals_payload = []
            for s in subreals:
                accepted = self._nz(s.qty_accepted)
                returned = self._nz(s.qty_returned)
                sold = _sold_for_sub(s)
                subreals_payload.append({
                    "id": s.id,
                    "created_at": s.created_at,
                    "qty_transferred": self._nz(s.qty_transferred),
                    "qty_accepted": accepted,
                    "qty_returned": returned,
                    "qty_sold": sold,
                    "qty_on_hand": max(accepted - returned - sold, 0),
                })

            data.append({
                "product": product_id,
                "product_name": (
                    subreals[0].product.name
                    if (subreals and getattr(subreals[0], "product", None))
                    else ""
                ),
                "qty_on_hand": qty_on_hand,
                "last_movement_at": last_movement_at,
                "subreals": subreals_payload,
            })

        return data

    def get(self, request, *args, **kwargs):
        # если надо, можно тут навесить owner-only check
        qs = self._build_queryset(request)

        out: List[Dict[str, Any]] = []
        for agent_id, agent_subreals_iter in groupby(
            qs,
            key=attrgetter("agent_id"),
        ):
            agent_subreals = list(agent_subreals_iter)
            if not agent_subreals:
                continue

            agent = agent_subreals[0].agent  # select_related("agent")

            products_payload = self._serialize_products_for_agent(agent_subreals)
            if not products_payload:
                continue

            out.append({
                "agent": {
                    "id": agent.id,
                    "first_name": getattr(agent, "first_name", "") or "",
                    "last_name": getattr(agent, "last_name", "") or "",
                    "track_number": getattr(agent, "track_number", None),
                },
                "products": products_payload,
            })

        return Response(
            AgentWithProductsSerializer(out, many=True).data,
            status=status.HTTP_200_OK,
        )


# ===========================
#  Product images
# ===========================
class ProductImageListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    """
    GET  /api/main/products/<uuid:product_id>/images/
    POST /api/main/products/<uuid:product_id>/images/
      form-data:
        image=<file>,
        alt="...",
        is_primary=true|false
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ProductImageSerializer

    def _base_qs(self):
        company = self._company()
        branch = self._auto_branch()

        qs = ProductImage.objects.select_related("product")

        if company is not None:
            qs = qs.filter(product__company=company)

        # новая логика: если branch задан → только этот филиал,
        # если branch None → не фильтруем по branch (вся компания)
        if branch is not None:
            qs = qs.filter(product__branch=branch)

        return qs

    def get_queryset(self):
        product_id = self.kwargs["product_id"]
        return self._base_qs().filter(product_id=product_id)

    @transaction.atomic
    def perform_create(self, serializer):
        # проверим, что продукт вообще доступен текущему пользователю
        pid = self.kwargs["product_id"]
        allowed = Product.objects.filter(id=pid)
        company = self._company()
        branch = self._auto_branch()

        if company is not None:
            allowed = allowed.filter(company=company)

        if branch is not None:
            allowed = allowed.filter(branch=branch)
        # branch is None → без ограничений по филиалу (вся компания)

        product = get_object_or_404(allowed)

        obj = serializer.save(product=product)


class ProductImageRetrieveUpdateDestroyAPIView(
    CompanyBranchRestrictedMixin,
    generics.RetrieveUpdateDestroyAPIView
):
    """
    PATCH /api/main/products/<uuid:product_id>/images/<uuid:image_id>/
      { "alt": "...", "is_primary": true/false }
    DELETE /api/main/products/<uuid:product_id>/images/<uuid:image_id>/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ProductImageSerializer
    lookup_url_kwarg = "image_id"

    def _base_qs(self):
        company = self._company()
        branch = self._auto_branch()
        qs = ProductImage.objects.select_related("product")

        if company is not None:
            qs = qs.filter(product__company=company)

        if branch is not None:
            qs = qs.filter(product__branch=branch)
        # branch None → все филиалы компании

        return qs

    def get_queryset(self):
        product_id = self.kwargs["product_id"]
        return self._base_qs().filter(product_id=product_id)

    @transaction.atomic
    def perform_update(self, serializer):
        obj = serializer.save()


# ===========================
#  Agent carts (корзины агента)
# ===========================
class AgentRequestCartListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AgentRequestCartSerializer

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["status", "client"]
    ordering_fields = ["created_at", "updated_at", "status"]
    ordering = ["-created_at"]

    def get_queryset(self):
        items_qs = (
            AgentRequestItem.objects
            .select_related("product")                    # продукт одной пачкой
            .prefetch_related("product__images")          # и все фотки товара
        )

        qs = (
            AgentRequestCart.objects
            .select_related(
                "company",
                "branch",
                "agent",
                "client",
                "approved_by",
            )
            .prefetch_related(
                Prefetch("items", queryset=items_qs)
            )
        )

        qs = self._filter_qs_company_branch(qs)

        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(agent=user)

        return qs

    def perform_create(self, serializer):
        """
        Сам сериализатор должен:
        - подставить agent = request.user
        - проставить company/branch от пользователя
        """
        serializer.save()


class AgentRequestCartRetrieveUpdateDestroyAPIView(
    CompanyBranchRestrictedMixin,
    generics.RetrieveUpdateDestroyAPIView
):
    """
    GET    /agent-carts/<uuid:pk>/
    PATCH  /agent-carts/<uuid:pk>/   (агент может менять client/note пока статус draft)
    DELETE /agent-carts/<uuid:pk>/   (удалить черновик)
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AgentRequestCartSerializer

    def get_queryset(self):
        items_qs = (
            AgentRequestItem.objects
            .select_related("product")                    # продукт одной пачкой
            .prefetch_related("product__images")          # и все фотки товара
        )

        qs = (
            AgentRequestCart.objects
            .select_related(
                "company",
                "branch",
                "agent",
                "client",
                "approved_by",
            )
            .prefetch_related(
                Prefetch("items", queryset=items_qs)
            )
        )

        qs = self._filter_qs_company_branch(qs)

        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(agent=user)

        return qs

    def perform_update(self, serializer):
        """
        В сериализаторе:
          - запрещаем апдейт, если статус != DRAFT
          - разрешаем менять только client / note
        """
        serializer.save()

    def perform_destroy(self, instance):
        """
        Удаляем корзину только если она DRAFT.
        Агент может удалять только свою корзину.
        Владелец может удалить любой черновик.
        """
        if instance.status != AgentRequestCart.Status.DRAFT:
            raise ValidationError("Удалять можно только корзину в статусе DRAFT.")

        user = self.request.user
        if not _is_owner_like(user) and instance.agent_id != user.id:
            raise ValidationError("Нельзя удалить чужую корзину.")

        instance.delete()


class AgentRequestCartSubmitAPIView(AgentCartLockMixin,
                                    CompanyBranchRestrictedMixin,
                                    APIView):
    """
    POST /agent-carts/<uuid:pk>/submit/
    Агент (или владелец вместо него) отправляет корзину.
    После этого корзина перестаёт редактироваться.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        # 1) лочим корзину безопасно (двухшагово, без FOR UPDATE с LEFT JOIN)
        cart = self._lock_cart_for_submit(request, pk)

        # 2) выполняем submit() через сериализатор
        ser = AgentRequestCartSubmitSerializer(
            data=request.data,
            context={"cart_obj": cart, "request": request},
        )
        ser.is_valid(raise_exception=True)
        cart = ser.save()  # cart.submit()

        # 3) возвращаем свежие данные
        out = AgentRequestCartSerializer(cart, context={"request": request}).data
        return Response(out, status=status.HTTP_200_OK)


class AgentRequestCartApproveAPIView(AgentCartLockMixin,
                                     CompanyBranchRestrictedMixin,
                                     APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        # только владелец/админ
        if not _is_owner_like(request.user):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        # аккуратно лочим корзину (без join'ов)
        cart = self._lock_cart_for_owner_action(request, pk)

        ser = AgentRequestCartApproveSerializer(
            data=request.data,
            context={"cart_obj": cart, "request": request},
        )
        ser.is_valid(raise_exception=True)
        cart = ser.save()  # внутри вызывает cart.approve(by_user=request.user)

        out = AgentRequestCartSerializer(cart, context={"request": request}).data
        return Response(out, status=status.HTTP_200_OK)


class AgentRequestCartRejectAPIView(AgentCartLockMixin,
                                    CompanyBranchRestrictedMixin,
                                    APIView):
    """
    POST /agent-carts/<uuid:pk>/reject/
    Только владелец/админ.
    cart.reject(by_user=request.user):
      - статус -> REJECTED
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        # 1) только владелец / админ
        if not _is_owner_like(request.user):
            raise PermissionDenied("Forbidden")

        # 2) лочим корзину
        cart = self._lock_cart_for_owner_action(request, pk)

        # 3) выполняем reject()
        ser = AgentRequestCartRejectSerializer(
            data=request.data,
            context={"cart_obj": cart, "request": request},
        )
        ser.is_valid(raise_exception=True)
        cart = ser.save()

        # 4) назад корзину в сериализованном виде
        out = AgentRequestCartSerializer(cart, context={"request": request}).data
        return Response(out, status=status.HTTP_200_OK)


# ===========================
#  Позиции корзины агента
# ===========================
class AgentRequestItemListCreateAPIView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    """
    GET  /agent-cart-items/?cart=<uuid>
    POST /agent-cart-items/
      { "cart": "<uuid>", "product": "<uuid>", "quantity_requested": 5 }

    - агент видит только свои черновики
    - владелец видит всё в своей компании/филиале
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AgentRequestItemSerializer

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["cart", "product"]
    ordering_fields = ["created_at", "updated_at", "quantity_requested"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = (
            AgentRequestItem.objects
            .select_related(
                "cart",
                "cart__agent",
                "cart__company",
                "cart__branch",
                "product",
                "subreal",
            )
            .all()
        )
        qs = self._filter_qs_company_branch(
            qs,
            company_field="cart__company",
            branch_field="cart__branch",
        )

        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(cart__agent=user)

        return qs

    def perform_create(self, serializer):
        """
        AgentRequestItemSerializer.create() должен проверять:
        - cart.status == DRAFT
        - cart принадлежит агенту (если юзер не владелец)
        - product принадлежит той же company/branch
        """
        serializer.save()


class AgentRequestItemRetrieveUpdateDestroyAPIView(
    CompanyBranchRestrictedMixin,
    generics.RetrieveUpdateDestroyAPIView
):
    """
    PATCH  /agent-cart-items/<uuid:pk>/
    DELETE /agent-cart-items/<uuid:pk>/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AgentRequestItemSerializer

    def get_queryset(self):
        qs = (
            AgentRequestItem.objects
            .select_related(
                "cart",
                "cart__agent",
                "cart__company",
                "cart__branch",
                "product",
                "subreal",
            )
            .all()
        )
        qs = self._filter_qs_company_branch(
            qs,
            company_field="cart__company",
            branch_field="cart__branch",
        )

        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(cart__agent=user)

        return qs

    def perform_update(self, serializer):
        """
        В сериализаторе:
          - запрещено менять, если cart.status != DRAFT
          - можно менять только product / quantity_requested
        """
        serializer.save()

    def perform_destroy(self, instance):
        """
        Удаляем позицию только если корзина DRAFT.
        Агент — только свою.
        Владелец — любую в своей компании/филиале.
        """
        cart = instance.cart
        if cart.status != AgentRequestCart.Status.DRAFT:
            raise ValidationError("Удалять позиции можно только в черновике.")

        user = self.request.user
        if not _is_owner_like(user) and cart.agent_id != user.id:
            raise ValidationError("Нельзя удалить позицию из чужой корзины.")

        instance.delete()

      
class AgentMyAnalyticsAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/agents/me/analytics/

    Квери:
      ?period=day|week|month|custom
      ?date_from=YYYY-MM-DD
      ?date_to=YYYY-MM-DD
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        # компания из миксина (company/branch уже используются во всей системе)
        company = self._company()
        branch = self._auto_branch()
        agent = request.user

        # fallback, если вдруг _company() вернул None
        if company is None:
            company = getattr(agent, "owned_company", None) or getattr(agent, "company", None)

        if company is None or getattr(agent, "company_id", None) != getattr(company, "id", None):
            return Response(
                {"detail": "Профиль агента не привязан к компании."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        period_params = _parse_period(request)

        data = build_agent_analytics_payload(
            company=company,
            branch=branch,
            agent=agent,
            **period_params,
        )
        return Response(data, status=status.HTTP_200_OK)


class ClientAgentAnalyticsAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/clients/<uuid:client_id>/agent-analytics/

    Аналитика по контрагенту (клиенту) глазами агента: продажи, брак, возвраты, долг.
    Считается по текущему пользователю-агенту (request.user).

    Квери: ?period=day|week|month|custom & ?date_from / ?date_to
    Период влияет на продажи/брак/возвраты. Долг — текущий (на момент запроса).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, client_id, *args, **kwargs):
        company = self._company()
        branch = self._auto_branch()
        agent = request.user

        if company is None:
            company = getattr(agent, "owned_company", None) or getattr(agent, "company", None)
        if company is None:
            return Response(
                {"detail": "Профиль не привязан к компании."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        client = get_object_or_404(Client, id=client_id, company=company)

        period_params = _parse_period(request)
        date_from = period_params["date_from"]
        date_to = period_params["date_to"]
        dt_from = timezone.make_aware(datetime.combine(date_from, datetime.min.time()))
        dt_to = timezone.make_aware(datetime.combine(date_to, datetime.max.time()))

        ZERO_MONEY = V(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))

        def _branch_filter(qs, field="branch"):
            if branch is not None:
                return qs.filter(Q(**{field: branch}) | Q(**{f"{field}__isnull": True}))
            return qs

        # ---------------- Продажи агента этому клиенту (оплаченные) ----------------
        sales_qs = Sale.objects.filter(
            company=company, user=agent, client=client,
            status=Sale.Status.PAID,
            created_at__range=(dt_from, dt_to),
        )
        sales_qs = _branch_filter(sales_qs)
        sales_agg = sales_qs.aggregate(
            count=Count("id"),
            amount=Coalesce(Sum("total"), ZERO_MONEY),
        )

        # ---------------- Брак / возвраты по этому клиенту ----------------
        returns_qs = ReturnFromAgent.objects.filter(
            company=company, returned_by=agent, client=client,
            status=ReturnFromAgent.Status.ACCEPTED,
            returned_at__range=(dt_from, dt_to),
        )
        returns_qs = _branch_filter(returns_qs)
        defects_agg = returns_qs.filter(is_defect=True).aggregate(
            count=Coalesce(Sum("qty"), V(0)),
            amount=Coalesce(Sum("amount"), ZERO_MONEY),
        )
        regular_agg = returns_qs.filter(is_defect=False).aggregate(
            count=Count("id"),
            amount=Coalesce(Sum("amount"), ZERO_MONEY),
        )

        # ---------------- Долг клиента (текущий) ----------------
        # 1) POS-продажи в долг
        pos_debt_qs = Sale.objects.filter(
            company=company, user=agent, client=client, status=Sale.Status.DEBT,
        )
        pos_debt_qs = _branch_filter(pos_debt_qs)
        pos_sales_debt = pos_debt_qs.aggregate(s=Coalesce(Sum("total"), ZERO_MONEY))["s"] or Decimal("0.00")

        # 2) Сделки-рассрочки (ClientDeal.DEBT): остаток = (amount - prepayment) - sum(paid)
        deals_qs = ClientDeal.objects.filter(
            company=company, client=client, kind=ClientDeal.Kind.DEBT,
        )
        deals_qs = _branch_filter(deals_qs)
        paid_subq = (
            DealInstallment.objects.filter(deal_id=OuterRef("pk"))
            .values("deal_id").annotate(s=Sum("paid_amount")).values("s")[:1]
        )
        client_deals_debt = (
            deals_qs
            .annotate(paid=Coalesce(Subquery(paid_subq), ZERO_MONEY))
            .annotate(remaining=(F("amount") - F("prepayment")) - F("paid"))
            .aggregate(t=Coalesce(Sum("remaining"), ZERO_MONEY))["t"]
            or Decimal("0.00")
        )

        total_debt = (pos_sales_debt or Decimal("0.00")) + (client_deals_debt or Decimal("0.00"))

        return Response({
            "client_id": str(client.id),
            "client_name": getattr(client, "full_name", "") or "",
            "period": {"type": period_params["period"], "date_from": date_from, "date_to": date_to},
            "sales": {
                "count": sales_agg["count"] or 0,
                "amount": float(sales_agg["amount"] or Decimal("0.00")),
            },
            "defects": {
                "count": int(defects_agg["count"] or 0),
                "amount": float(defects_agg["amount"] or Decimal("0.00")),
            },
            "returns": {
                "count": regular_agg["count"] or 0,
                "amount": float(regular_agg["amount"] or Decimal("0.00")),
            },
            "debt": {
                "pos_sales_debt": float(pos_sales_debt or Decimal("0.00")),
                "client_deals_debt": float(client_deals_debt or Decimal("0.00")),
                "total": float(total_debt),
            },
        }, status=status.HTTP_200_OK)


class OwnerAgentAnalyticsAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/owners/agents/<uuid:agent_id>/analytics/

    Доступ: только владелец/админ (_is_owner_like).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, agent_id, *args, **kwargs):
        user = request.user
        if not _is_owner_like(user):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        company = self._company()
        branch = self._auto_branch()

        # fallback, если _company() ничего не дал
        if company is None:
            company = getattr(user, "owned_company", None) or getattr(user, "company", None)

        if company is None:
            return Response(
                {"detail": "У вас не задана компания."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            agent = (
                User.objects
                .filter(company=company)
                .get(pk=agent_id)
            )
        except User.DoesNotExist:
            return Response({"detail": "Agent not found."}, status=status.HTTP_404_NOT_FOUND)

        period_params = _parse_period(request)

        data = build_agent_analytics_payload(
            company=company,
            branch=branch,
            agent=agent,
            **period_params,
        )
        return Response(data, status=status.HTTP_200_OK)
    
class OwnerOverallAnalyticsAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/owners/analytics/

    Доступ: только владелец/админ (_is_owner_like).
    Аналитика считается по всей компании (ветка/branch — как в твоём миксине).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        user = request.user
        if not _is_owner_like(user):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        company = self._company()
        branch = self._auto_branch()

        # fallback, если _company() ничего не дал
        if company is None:
            company = getattr(user, "owned_company", None) or getattr(user, "company", None)

        if company is None:
            return Response(
                {"detail": "У вас не задана компания."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        period_params = _parse_period(request)

        data = build_owner_analytics_payload(
            company=company,
            branch=branch,
            **period_params,
        )
        return Response(data, status=status.HTTP_200_OK)


class AnalyticsCardDetailsAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/analytics/cards/details/?card=<key>

    Деталка для модалки по клику на карточку в аналитике.
    Доступ: любой авторизованный пользователь (агенты тоже).

    Поддерживаемые card:
      - stock_purchase_value (alias: stock_value)
      - stock_retail_value
      - raw_material_value
      - defective_items
      - returns   (is_defect=False, accepted)
      - defects   (is_defect=True, accepted)
      - inventory_surplus   (diff > 0, confirmed)
      - inventory_shortage  (diff < 0, confirmed)
      - discounts_total
      - transfers_count
      - items_transferred
      - acceptances_count
      - sales_count
      - sales_amount
      - items_on_hand_qty
      - items_on_hand_amount
      - revenue
      - cost_of_goods_sold
      - gross_profit
      - gross_margin_percent
      - other_expenses (alias: other_expenses_total) — только владелец/админ
      - accounts_receivable
      - accounts_payable
      - total_debt
      - users_count
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        card = (request.query_params.get("card") or "").strip()
        if not card:
            raise ValidationError({"card": "Required. Example: card=stock_purchase_value"})

        agent_id = (request.query_params.get("agent_id") or "").strip()

        # простая пагинация под модалку (не DRF pagination, чтобы фронту было проще)
        limit = _parse_int_nonneg(
            request.query_params.get("limit"), "limit", default=200, maximum=1000
        )
        offset = _parse_int_nonneg(
            request.query_params.get("offset"), "offset", default=0, maximum=1000000
        )

        company = self._company()
        if company is None:
            return Response({"detail": "У вас не задана компания."}, status=status.HTTP_400_BAD_REQUEST)

        # branch фильтруем так же, как products/list и items-make (через миксин)
        branch = self._auto_branch()

        # ----- stock: products -----
        if card in ("stock_purchase_value", "stock_value", "stock_retail_value"):
            qs = Product.objects.all().exclude(kind=Product.Kind.SERVICE)
            qs = self._filter_qs_company_branch(qs)
            qs = qs.order_by("name", "id")

            total_count = qs.count()
            page = list(qs[offset: offset + limit].values(
                "id", "name", "quantity", "purchase_price", "price", "unit", "kind",
            ))

            items = []
            total_purchase = Decimal("0.00")
            total_retail = Decimal("0.00")
            for p in page:
                qty = _to_dec(p.get("quantity"), default=Decimal("0"))
                purchase_price = _to_dec(p.get("purchase_price"), default=Decimal("0"))
                retail_price = _to_dec(p.get("price"), default=Decimal("0"))
                purchase_sum = (qty * purchase_price).quantize(_Q2, rounding=ROUND_HALF_UP)
                retail_sum = (qty * retail_price).quantize(_Q2, rounding=ROUND_HALF_UP)
                total_purchase += purchase_sum
                total_retail += retail_sum
                items.append({
                    "id": str(p["id"]),
                    "name": p["name"],
                    "unit": p.get("unit"),
                    "kind": p.get("kind"),
                    "quantity": str(qty),
                    "purchase_price": str(purchase_price),
                    "retail_price": str(retail_price),
                    "purchase_sum": str(purchase_sum),
                    "retail_sum": str(retail_sum),
                })

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {
                    "purchase_sum": str(total_purchase.quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "retail_sum": str(total_retail.quantize(_Q2, rounding=ROUND_HALF_UP)),
                },
                "items": items,
            })

        # ----- raw materials: item make -----
        if card == "raw_material_value":
            qs = ItemMake.objects.select_related("supplier").all()
            qs = self._filter_qs_company_branch(qs)
            qs = qs.order_by("name", "id")

            total_count = qs.count()
            page = list(qs[offset: offset + limit].values(
                "id", "name", "quantity", "unit", "price",
                "supplier_id", "supplier__full_name",
            ))

            items = []
            total_sum = Decimal("0.00")
            for it in page:
                qty = _to_dec(it.get("quantity"), default=Decimal("0"))
                price = _to_dec(it.get("price"), default=Decimal("0"))
                s = (qty * price).quantize(_Q2, rounding=ROUND_HALF_UP)
                total_sum += s
                items.append({
                    "id": str(it["id"]),
                    "name": it["name"],
                    "unit": it.get("unit"),
                    "quantity": str(qty),
                    "price": str(price),
                    "sum": str(s),
                    "supplier": (
                        {"id": str(it["supplier_id"]), "full_name": it.get("supplier__full_name")}
                        if it.get("supplier_id") else None
                    ),
                })

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {
                    "sum": str(total_sum.quantize(_Q2, rounding=ROUND_HALF_UP)),
                },
                "items": items,
            })

        # ----- defective items: accepted returns from agents -----
        if card == "defective_items":
            qs = ReturnFromAgent.objects.filter(
                company=company,
                status=ReturnFromAgent.Status.ACCEPTED,
            ).select_related("subreal__product", "returned_by")

            # Агент видит только свои возвраты; владелец/админ — все
            if not _is_owner_like(request.user):
                qs = qs.filter(returned_by=request.user)
            elif agent_id:
                qs = qs.filter(returned_by_id=agent_id)

            if branch is not None:
                qs = qs.filter(branch=branch)
            else:
                qs = qs.filter(branch__isnull=True)

            # Клиент берётся из продажи (Sale.client) по этому subreal через последнюю аллокацию.
            alloc_client_id_subq = (
                AgentSaleAllocation.objects.filter(
                    company_id=company.id,
                    subreal_id=OuterRef("subreal_id"),
                )
                .exclude(sale__client_id__isnull=True)
                .order_by("-created_at")
                .values("sale__client_id")[:1]
            )
            alloc_client_name_subq = (
                AgentSaleAllocation.objects.filter(
                    company_id=company.id,
                    subreal_id=OuterRef("subreal_id"),
                )
                .exclude(sale__client_id__isnull=True)
                .order_by("-created_at")
                .values("sale__client__full_name")[:1]
            )

            grouped_qs = (
                qs.annotate(
                    client_id=Subquery(alloc_client_id_subq),
                    client_name=Subquery(alloc_client_name_subq),
                )
                .values(
                    "subreal__product_id",
                    "subreal__product__name",
                    "returned_by_id",
                    "returned_by__first_name",
                    "returned_by__last_name",
                    "client_id",
                    "client_name",
                )
                .annotate(
                    qty=Coalesce(Sum("qty"), V(0)),
                    returns_count=Count("id"),
                )
                .order_by("-qty", "subreal__product__name")
            )

            total_count = grouped_qs.count()
            page = list(grouped_qs[offset: offset + limit])

            items = []
            for r in page:
                agent_name = (
                    f"{(r.get('returned_by__first_name') or '').strip()} {(r.get('returned_by__last_name') or '').strip()}".strip()
                    or "Пользователь"
                )
                items.append({
                    "product_id": str(r["subreal__product_id"]) if r.get("subreal__product_id") else None,
                    "product_name": r.get("subreal__product__name") or "",
                    "qty": int(r.get("qty") or 0),
                    "returns_count": int(r.get("returns_count") or 0),
                    "agent": {
                        "id": str(r["returned_by_id"]) if r.get("returned_by_id") else None,
                        "name": agent_name,
                    },
                    "client": (
                        {
                            "id": str(r["client_id"]) if r.get("client_id") else None,
                            "name": r.get("client_name") or "",
                        }
                        if r.get("client_id") or r.get("client_name")
                        else None
                    ),
                })
            total_qty = int(
                (grouped_qs.aggregate(s=Coalesce(Sum("qty"), V(0)))["s"]) or 0
            )

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {
                    "qty": total_qty,
                },
                "items": items,
            })

        # ----- returns / defects: принятые возвраты от агентов, раздельно по is_defect -----
        # card=returns  → is_defect=False (качественный возврат, вернулся на склад)
        # card=defects  → is_defect=True  (брак, списан)
        if card in ("returns", "defects"):
            want_defect = (card == "defects")
            qs = ReturnFromAgent.objects.filter(
                company=company,
                status=ReturnFromAgent.Status.ACCEPTED,
                is_defect=want_defect,
            ).select_related("subreal__product", "returned_by", "client")

            # Агент видит только свои возвраты; владелец/админ — все
            if not _is_owner_like(request.user):
                qs = qs.filter(returned_by=request.user)
            elif agent_id:
                qs = qs.filter(returned_by_id=agent_id)

            if branch is not None:
                qs = qs.filter(branch=branch)
            else:
                qs = qs.filter(branch__isnull=True)

            qs = qs.order_by("-returned_at", "id")

            total_count = qs.count()
            agg = qs.aggregate(
                qty=Coalesce(Sum("qty"), V(0)),
                amount=Coalesce(
                    Sum("amount"),
                    V(Decimal("0.00"), output_field=DecimalField(max_digits=14, decimal_places=2)),
                ),
            )
            total_qty = int(agg["qty"] or 0)
            total_amount = agg["amount"] or Decimal("0.00")

            page = list(qs[offset: offset + limit].values(
                "subreal__product_id",
                "subreal__product__name",
                "returned_by__first_name",
                "returned_by__last_name",
                "client__full_name",
                "qty",
                "amount",
                "returned_at",
            ))

            items = []
            for r in page:
                agent_name = (
                    f"{(r.get('returned_by__first_name') or '').strip()} {(r.get('returned_by__last_name') or '').strip()}".strip()
                    or "Пользователь"
                )
                items.append({
                    "product_id": str(r["subreal__product_id"]) if r.get("subreal__product_id") else None,
                    "product_name": r.get("subreal__product__name") or "",
                    "agent_name": agent_name,
                    "client_name": r.get("client__full_name") or None,
                    "qty": int(r.get("qty") or 0),
                    "amount": str(_to_dec(r.get("amount"), default=Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "returned_at": r.get("returned_at"),
                })

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {
                    "qty": total_qty,
                    "amount": str(total_amount.quantize(_Q2, rounding=ROUND_HALF_UP)),
                },
                "items": items,
            })

        # ----- inventory surplus / shortage: строки подтверждённых инвентаризаций -----
        # card=inventory_surplus  → diff > 0 (излишек)
        # card=inventory_shortage → diff < 0 (недостача)
        if card in ("inventory_surplus", "inventory_shortage"):
            qs = InventoryItem.objects.filter(
                inventory__company=company,
                inventory__status=Inventory.Status.CONFIRMED,
            ).select_related("inventory", "inventory__user")

            if card == "inventory_surplus":
                qs = qs.filter(diff__gt=0)
            else:
                qs = qs.filter(diff__lt=0)

            if branch is not None:
                qs = qs.filter(Q(inventory__branch=branch) | Q(inventory__branch__isnull=True))
            else:
                qs = qs.filter(inventory__branch__isnull=True)

            qs = qs.order_by("-inventory__created_at", "id")

            total_count = qs.count()
            page = list(qs[offset: offset + limit].values(
                "product_name",
                "qty_system",
                "qty_fact",
                "diff",
                "inventory__comment",
                "inventory__created_at",
                "inventory__user__first_name",
                "inventory__user__last_name",
            ))

            items = []
            for r in page:
                user_name = (
                    f"{(r.get('inventory__user__first_name') or '').strip()} {(r.get('inventory__user__last_name') or '').strip()}".strip()
                    or "Пользователь"
                )
                items.append({
                    "product_name": r.get("product_name") or "",
                    "qty_system": float(r.get("qty_system") or 0),
                    "qty_fact": float(r.get("qty_fact") or 0),
                    "diff": float(r.get("diff") or 0),
                    "user_name": user_name,
                    "created_at": r.get("inventory__created_at"),
                    "comment": r.get("inventory__comment") or "",
                })

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {},
                "items": items,
            })

        # ----- transfers: ManufactureSubreal (transfers_count и items_transferred — один список) -----
        if card in ("transfers_count", "items_transferred"):
            p = _parse_period(request)
            date_from = p["date_from"]
            date_to = p["date_to"]

            if _is_owner_like(request.user):
                dt_from, dt_to_excl = _dt_range(date_from, date_to)
                qs = ManufactureSubreal.objects.filter(
                    company=company,
                    created_at__gte=dt_from,
                    created_at__lt=dt_to_excl,
                )
                agent_id = (request.query_params.get("agent_id") or "").strip()
                if agent_id:
                    qs = qs.filter(agent_id=agent_id)
            else:
                dt_from = timezone.make_aware(datetime.combine(date_from, datetime.min.time()))
                dt_to = timezone.make_aware(datetime.combine(date_to, datetime.max.time()))
                qs = ManufactureSubreal.objects.filter(
                    company=company,
                    agent=request.user,
                    created_at__gte=dt_from,
                    created_at__lte=dt_to,
                )

            if branch is not None:
                qs = qs.filter(branch=branch)
            else:
                qs = qs.filter(branch__isnull=True)

            qs = qs.select_related("agent", "product", "user").order_by("-created_at", "-id")
            total_count = qs.count()
            items_transferred_total = int(
                (qs.aggregate(s=Coalesce(Sum("qty_transferred"), V(0)))["s"]) or 0
            )
            page = qs[offset: offset + limit]

            items = []
            for tr in page:
                agent_u = tr.agent
                agent_name = (
                    f"{(getattr(agent_u, 'first_name', None) or '').strip()} {(getattr(agent_u, 'last_name', None) or '').strip()}".strip()
                    or getattr(agent_u, "username", None)
                    or "Пользователь"
                )
                prod = tr.product
                items.append({
                    "id": str(tr.id),
                    "created_at": timezone.localtime(tr.created_at).isoformat() if tr.created_at else None,
                    "status": tr.status,
                    "qty_transferred": int(tr.qty_transferred or 0),
                    "qty_accepted": int(tr.qty_accepted or 0),
                    "qty_returned": int(tr.qty_returned or 0),
                    "agent": {"id": str(tr.agent_id), "name": agent_name},
                    "product": {"id": str(tr.product_id), "name": getattr(prod, "name", None) or ""},
                })

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "period": {"type": p["period"], "date_from": date_from, "date_to": date_to},
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {"items_transferred": items_transferred_total},
                "items": items,
            })

        # ----- acceptances: Acceptance (как summary acceptances_count в аналитике) -----
        if card == "acceptances_count":
            p = _parse_period(request)
            date_from = p["date_from"]
            date_to = p["date_to"]

            qs = Acceptance.objects.filter(company=company)
            if not _is_owner_like(request.user):
                qs = qs.filter(subreal__agent=request.user)
            elif agent_id:
                qs = qs.filter(subreal__agent_id=agent_id)

            if _is_owner_like(request.user):
                dt_from, dt_to_excl = _dt_range(date_from, date_to)
                qs = qs.filter(accepted_at__gte=dt_from, accepted_at__lt=dt_to_excl)
            else:
                dt_from = timezone.make_aware(datetime.combine(date_from, datetime.min.time()))
                dt_to = timezone.make_aware(datetime.combine(date_to, datetime.max.time()))
                qs = qs.filter(accepted_at__gte=dt_from, accepted_at__lte=dt_to)

            if branch is not None:
                qs = qs.filter(subreal__branch=branch)
            else:
                qs = qs.filter(subreal__branch__isnull=True)

            qs = qs.select_related("subreal", "subreal__agent", "subreal__product", "accepted_by").order_by(
                "-accepted_at", "-id"
            )
            total_count = qs.count()
            qty_accepted_total = int((qs.aggregate(s=Coalesce(Sum("qty"), V(0)))["s"]) or 0)
            page = qs[offset: offset + limit]

            items = []
            for acc in page:
                sr = acc.subreal
                agent_u = sr.agent if sr else None
                agent_name = (
                    f"{(getattr(agent_u, 'first_name', None) or '').strip()} {(getattr(agent_u, 'last_name', None) or '').strip()}".strip()
                    or getattr(agent_u, "username", None)
                    or "Пользователь"
                ) if agent_u else "Пользователь"
                prod = sr.product if sr else None
                accepter = acc.accepted_by
                accepter_name = (
                    f"{(getattr(accepter, 'first_name', None) or '').strip()} {(getattr(accepter, 'last_name', None) or '').strip()}".strip()
                    or getattr(accepter, "username", None)
                    or "Пользователь"
                ) if accepter else "Пользователь"
                items.append({
                    "id": str(acc.id),
                    "accepted_at": timezone.localtime(acc.accepted_at).isoformat() if acc.accepted_at else None,
                    "qty": int(acc.qty or 0),
                    "accepted_by": {"id": str(accepter.id), "name": accepter_name} if accepter else None,
                    "agent": {"id": str(agent_u.id), "name": agent_name} if agent_u else None,
                    "subreal": {"id": str(sr.id), "status": sr.status} if sr else None,
                    "product": {"id": str(prod.id), "name": getattr(prod, "name", None) or ""} if prod else None,
                })

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "period": {"type": p["period"], "date_from": date_from, "date_to": date_to},
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {"qty_accepted": qty_accepted_total},
                "items": items,
            })

        # ----- sales: список оплаченных продаж (sales_count / sales_amount в summary) -----
        if card in ("sales_count", "sales_amount"):
            p = _parse_period(request)
            date_from = p["date_from"]
            date_to = p["date_to"]

            if _is_owner_like(request.user):
                dt_from, dt_to_excl = _dt_range(date_from, date_to)
                qs = Sale.objects.filter(
                    company=company,
                    status=Sale.Status.PAID,
                    created_at__gte=dt_from,
                    created_at__lt=dt_to_excl,
                )
                if agent_id:
                    qs = qs.filter(user_id=agent_id)
            else:
                dt_from = timezone.make_aware(datetime.combine(date_from, datetime.min.time()))
                dt_to = timezone.make_aware(datetime.combine(date_to, datetime.max.time()))
                qs = Sale.objects.filter(
                    company=company,
                    user=request.user,
                    status=Sale.Status.PAID,
                    created_at__gte=dt_from,
                    created_at__lte=dt_to,
                )

            if branch is not None:
                qs = qs.filter(branch=branch)
            else:
                qs = qs.filter(branch__isnull=True)

            money_field = DecimalField(max_digits=12, decimal_places=2)
            zero_money = V(Decimal("0.00"), output_field=money_field)
            sales_amount_total = qs.aggregate(s=Coalesce(Sum("total"), zero_money))["s"] or Decimal("0.00")

            qs = qs.select_related("user", "client").order_by("-created_at", "-id")
            total_count = qs.count()
            page = qs[offset: offset + limit]

            items = []
            for sale in page:
                u = sale.user
                user_name = (
                    f"{(getattr(u, 'first_name', None) or '').strip()} {(getattr(u, 'last_name', None) or '').strip()}".strip()
                    or getattr(u, "username", None)
                    or "Пользователь"
                ) if u else "Пользователь"
                cl = sale.client
                items.append({
                    "id": str(sale.id),
                    "created_at": timezone.localtime(sale.created_at).isoformat() if sale.created_at else None,
                    "total": str((sale.total or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "discount_total": str((sale.discount_total or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "user": {"id": str(sale.user_id), "name": user_name} if sale.user_id else None,
                    "client": (
                        {"id": str(cl.id), "name": getattr(cl, "full_name", None) or ""}
                        if cl else None
                    ),
                })

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "period": {"type": p["period"], "date_from": date_from, "date_to": date_to},
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {
                    "sales_amount": str(sales_amount_total.quantize(_Q2, rounding=ROUND_HALF_UP)),
                },
                "items": items,
            })

        # ----- прочие расходы кассы (карточки other_expenses_total / net_profit) -----
        if card in ("other_expenses", "other_expenses_total"):
            if not _is_owner_like(request.user):
                return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

            from apps.construction.models import CashFlow

            p = _parse_period(request)
            date_from = p["date_from"]
            date_to = p["date_to"]
            dt_from, dt_to_excl = _dt_range(date_from, date_to)

            # Фильтр обязан совпадать с cfqs в build_owner_analytics_payload,
            # иначе сумма в модалке не сойдётся с карточкой.
            qs = CashFlow.objects.filter(
                company=company,
                type=CashFlow.Type.EXPENSE,
                status=CashFlow.Status.APPROVED,
                created_at__gte=dt_from,
                created_at__lt=dt_to_excl,
            )
            if branch is not None:
                qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))

            money_field = DecimalField(max_digits=12, decimal_places=2)
            zero_money = V(Decimal("0.00"), output_field=money_field)
            expenses_total = qs.aggregate(s=Coalesce(Sum("amount"), zero_money))["s"] or Decimal("0.00")

            qs = qs.select_related("category", "cashier", "cashbox").order_by("-created_at", "-id")
            total_count = qs.count()
            page = qs[offset: offset + limit]

            items = []
            for flow in page:
                c = flow.cashier
                cashier_name = (
                    f"{(getattr(c, 'first_name', None) or '').strip()} {(getattr(c, 'last_name', None) or '').strip()}".strip()
                    or getattr(c, "email", None)
                    or ""
                ) if c else ""
                items.append({
                    "id": str(flow.id),
                    "created_at": timezone.localtime(flow.created_at).isoformat() if flow.created_at else None,
                    "name": flow.name or "Без названия",
                    "amount": str((flow.amount or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "category": (
                        {"id": str(flow.category_id), "title": flow.category.title}
                        if flow.category_id else None
                    ),
                    "cashbox": (
                        {"id": str(flow.cashbox_id), "name": getattr(flow.cashbox, "name", "") or ""}
                        if flow.cashbox_id else None
                    ),
                    "cashier": {"id": str(flow.cashier_id), "name": cashier_name} if flow.cashier_id else None,
                })

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "period": {"type": p["period"], "date_from": date_from, "date_to": date_to},
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {
                    "other_expenses_total": str(expenses_total.quantize(_Q2, rounding=ROUND_HALF_UP)),
                },
                "items": items,
            })

        # ----- остатки на руках у агента (как summary в analytics_agent) -----
        if card in ("items_on_hand_qty", "items_on_hand_amount"):
            if _is_owner_like(request.user):
                if not agent_id:
                    return Response({
                        "card": card,
                        "branch_id": str(getattr(branch, "id", "")) if branch else None,
                        "count": 0,
                        "offset": offset,
                        "limit": limit,
                        "totals": {
                            "qty_on_hand": 0,
                            "amount": "0.00",
                        },
                        "items": [],
                    })
                agent_obj = get_object_or_404(User.objects.filter(company=company), id=agent_id)
            else:
                agent_obj = request.user

            on_hand = _compute_agent_on_hand(company=company, branch=branch, agent=agent_obj)
            if card == "items_on_hand_qty":
                rows = list(on_hand["by_product_qty"])
            else:
                rows = list(on_hand["by_product_amount"])
            rows.sort(key=lambda r: ((r.get("product_name") or ""), str(r.get("product_id") or "")))
            total_count = len(rows)
            page = rows[offset: offset + limit]
            amt_total = Decimal(str(on_hand["total_amount"] or 0)).quantize(_Q2, rounding=ROUND_HALF_UP)
            items = []
            for r in page:
                row = {
                    "product_id": r.get("product_id"),
                    "product_name": r.get("product_name") or "",
                    "qty_on_hand": int(r.get("qty_on_hand") or 0),
                }
                if card == "items_on_hand_amount":
                    row["amount"] = str(
                        Decimal(str(r.get("amount") or 0)).quantize(_Q2, rounding=ROUND_HALF_UP)
                    )
                items.append(row)

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {
                    "qty_on_hand": int(on_hand["total_qty"] or 0),
                    "amount": str(amt_total),
                },
                "items": items,
            })

        # ----- discounts total: by employee and client -----
        if card == "discounts_total":
            # период берём из тех же query params, что и в аналитике
            p = _parse_period(request)
            date_from = p["date_from"]
            date_to = p["date_to"]
            dt_from = timezone.make_aware(datetime.combine(date_from, datetime.min.time()))
            dt_to = timezone.make_aware(datetime.combine(date_to, datetime.max.time()))

            money_field = DecimalField(max_digits=12, decimal_places=2)
            zero_money = V(Decimal("0.00"), output_field=money_field)

            qs = Sale.objects.filter(
                company=company,
                status=Sale.Status.PAID,
                created_at__range=(dt_from, dt_to),
            )

            # Агент видит только свои продажи; владелец/админ — все
            if not _is_owner_like(request.user):
                qs = qs.filter(user=request.user)
            elif agent_id:
                qs = qs.filter(user_id=agent_id)

            if branch is not None:
                qs = qs.filter(branch=branch)
            else:
                qs = qs.filter(branch__isnull=True)

            grouped_qs = (
                qs.filter(discount_total__gt=0)
                .values(
                    "user_id",
                    "user__first_name",
                    "user__last_name",
                    "client_id",
                    "client__full_name",
                )
                .annotate(
                    sales_count=Count("id"),
                    discounts_total=Coalesce(Sum("discount_total"), zero_money),
                )
                .order_by("-discounts_total")
            )

            total_count = grouped_qs.count()
            page = list(grouped_qs[offset: offset + limit])

            items = []
            for r in page:
                user_name = (
                    f"{(r.get('user__first_name') or '').strip()} {(r.get('user__last_name') or '').strip()}".strip()
                    or "Пользователь"
                )
                items.append({
                    "user": {"id": str(r["user_id"]) if r.get("user_id") else None, "name": user_name},
                    "client": {
                        "id": str(r["client_id"]) if r.get("client_id") else None,
                        "name": r.get("client__full_name") or "Без имени",
                    },
                    "sales_count": int(r.get("sales_count") or 0),
                    "discounts_total": str((r.get("discounts_total") or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)),
                })

            totals = grouped_qs.aggregate(s=Coalesce(Sum("discounts_total"), zero_money))["s"] or Decimal("0.00")

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "period": {"type": p["period"], "date_from": date_from, "date_to": date_to},
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {"discounts_total": str(totals.quantize(_Q2, rounding=ROUND_HALF_UP))},
                "items": items,
            })

        # ----- выручка / COGS / валовая / маржа (как summary в analytics_owner_production / analytics_agent) -----
        if card in (
            "revenue",
            "cost_of_goods_sold",
            "gross_profit",
            "gross_margin_percent",
        ):
            p = _parse_period(request)
            date_from = p["date_from"]
            date_to = p["date_to"]

            if _is_owner_like(request.user):
                dt_from, dt_to_excl = _dt_range(date_from, date_to)
                sales_qs = Sale.objects.filter(
                    company=company,
                    status=Sale.Status.PAID,
                    created_at__gte=dt_from,
                    created_at__lt=dt_to_excl,
                )
                if agent_id:
                    sales_qs = sales_qs.filter(user_id=agent_id)
            else:
                dt_from = timezone.make_aware(datetime.combine(date_from, datetime.min.time()))
                dt_to = timezone.make_aware(datetime.combine(date_to, datetime.max.time()))
                sales_qs = Sale.objects.filter(
                    company=company,
                    user=request.user,
                    status=Sale.Status.PAID,
                    created_at__gte=dt_from,
                    created_at__lte=dt_to,
                )

            if branch is not None:
                sales_qs = sales_qs.filter(branch=branch)
            else:
                sales_qs = sales_qs.filter(branch__isnull=True)

            money_field = DecimalField(max_digits=12, decimal_places=2)
            zero_money = V(Decimal("0.00"), output_field=money_field)
            revenue_expr = ExpressionWrapper(
                (F("quantity") * F("unit_price")) - Coalesce(F("line_discount"), zero_money),
                output_field=money_field,
            )
            _unit_purchase = Coalesce(
                F("purchase_price_snapshot"),
                F("product__purchase_price"),
                V(Decimal("0"), output_field=money_field),
            )

            items_qs = SaleItem.objects.filter(sale__in=sales_qs).select_related("product")

            # Выручка = продажи (Sale.total), не сумма строк.
            # Это соответствует "просто продажи − закупка".
            sales_total_agg = sales_qs.aggregate(
                revenue=Coalesce(Sum("total"), zero_money)
            )
            rev_t = sales_total_agg["revenue"] or Decimal("0.00")

            full_agg = items_qs.aggregate(
                cost_of_goods_sold=Coalesce(
                    Sum(F("quantity") * _unit_purchase, output_field=money_field),
                    zero_money,
                ),
            )
            cogs_t = full_agg["cost_of_goods_sold"] or Decimal("0.00")
            gp_t = rev_t - cogs_t
            margin_t = (
                (gp_t / rev_t * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if rev_t > 0
                else Decimal("0.00")
            )

            grouped_qs = (
                items_qs.values("product_id", "product__name")
                .annotate(
                    revenue=Coalesce(Sum(revenue_expr, output_field=money_field), zero_money),
                    cost_of_goods_sold=Coalesce(
                        Sum(F("quantity") * _unit_purchase, output_field=money_field),
                        zero_money,
                    ),
                )
                .annotate(
                    gross_profit=ExpressionWrapper(
                        F("revenue") - F("cost_of_goods_sold"),
                        output_field=money_field,
                    ),
                )
            )

            if card == "revenue":
                grouped_qs = grouped_qs.filter(revenue__gt=0).order_by("-revenue", "product_id")
            elif card == "cost_of_goods_sold":
                grouped_qs = grouped_qs.filter(cost_of_goods_sold__gt=0).order_by("-cost_of_goods_sold", "product_id")
            elif card == "gross_profit":
                grouped_qs = grouped_qs.filter(
                    Q(revenue__gt=0) | Q(cost_of_goods_sold__gt=0)
                ).order_by("-gross_profit", "product_id")
            else:
                grouped_qs = grouped_qs.filter(revenue__gt=0).order_by("-gross_profit", "product_id")

            total_count = grouped_qs.count()
            page = list(grouped_qs[offset: offset + limit])

            items = []
            for r in page:
                rev = r.get("revenue") or Decimal("0.00")
                cogs = r.get("cost_of_goods_sold") or Decimal("0.00")
                gp = r.get("gross_profit") if r.get("gross_profit") is not None else (rev - cogs)
                if not isinstance(gp, Decimal):
                    gp = Decimal(str(gp))
                m_pct = (
                    (gp / rev * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    if rev > 0
                    else Decimal("0.00")
                )
                items.append({
                    "product_id": str(r["product_id"]) if r.get("product_id") else None,
                    "product_name": r.get("product__name") or "",
                    "revenue": str(rev.quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "cost_of_goods_sold": str(cogs.quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "gross_profit": str(gp.quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "gross_margin_percent": str(m_pct),
                })

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "period": {"type": p["period"], "date_from": date_from, "date_to": date_to},
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {
                    "revenue": str(rev_t.quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "cost_of_goods_sold": str(cogs_t.quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "gross_profit": str(gp_t.quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "gross_margin_percent": str(margin_t),
                },
                "items": items,
            })

        # ----- total_debt: остаток по ClientDeal(kind=debt) -----
        if card == "total_debt":
            p_td = _parse_period(request)
            date_from_td = p_td["date_from"]
            date_to_td = p_td["date_to"]

            money_field_td = DecimalField(max_digits=12, decimal_places=2)
            zero_money_td = V(Decimal("0.00"), output_field=money_field_td)

            if _is_owner_like(request.user):
                deals_qs = ClientDeal.objects.filter(company=company, kind=ClientDeal.Kind.DEBT)
                if branch is not None:
                    deals_qs = deals_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    deals_qs = deals_qs.filter(branch__isnull=True)
            else:
                clients_scope = Client.objects.filter(company=company, salesperson=request.user)
                if branch is not None:
                    clients_scope = clients_scope.filter(branch=branch)
                else:
                    clients_scope = clients_scope.filter(branch__isnull=True)
                deals_qs = ClientDeal.objects.filter(
                    company=company,
                    kind=ClientDeal.Kind.DEBT,
                    client__in=clients_scope,
                )
                if branch is not None:
                    deals_qs = deals_qs.filter(branch=branch)
                else:
                    deals_qs = deals_qs.filter(branch__isnull=True)

            paid_subq = (
                DealInstallment.objects.filter(deal_id=OuterRef("pk"))
                .values("deal_id")
                .annotate(s=Sum("paid_amount"))
                .values("s")[:1]
            )
            deals_ann = (
                deals_qs.select_related("client")
                .annotate(paid=Coalesce(Subquery(paid_subq), V(Decimal("0.00"), output_field=money_field_td)))
                .annotate(remaining=(F("amount") - F("prepayment")) - F("paid"))
                .filter(remaining__gt=0)
                .order_by("-remaining", "-id")
            )
            total_count = deals_ann.count()
            total_remaining = (
                deals_ann.aggregate(t=Coalesce(Sum("remaining"), zero_money_td))["t"] or Decimal("0.00")
            )
            page = deals_ann[offset: offset + limit]

            items = []
            for d in page:
                cl = d.client
                items.append({
                    "id": str(d.id),
                    "title": d.title,
                    "client": (
                        {"id": str(cl.id), "name": getattr(cl, "full_name", None) or ""}
                        if cl else None
                    ),
                    "amount": str((d.amount or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "prepayment": str((d.prepayment or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "paid": str((d.paid or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "remaining": str((d.remaining or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)),
                })

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "period": {"type": p_td["period"], "date_from": date_from_td, "date_to": date_to_td},
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {"total_debt": str(total_remaining.quantize(_Q2, rounding=ROUND_HALF_UP))},
                "items": items,
            })

        # ----- accounts_receivable: сделки DEBT + продажи в долг -----
        if card == "accounts_receivable":
            p_ar = _parse_period(request)
            date_from_ar = p_ar["date_from"]
            date_to_ar = p_ar["date_to"]

            money_field_ar = DecimalField(max_digits=12, decimal_places=2)
            zero_money_ar = V(Decimal("0.00"), output_field=money_field_ar)

            if _is_owner_like(request.user):
                deals_qs = ClientDeal.objects.filter(company=company, kind=ClientDeal.Kind.DEBT)
                if branch is not None:
                    deals_qs = deals_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    deals_qs = deals_qs.filter(branch__isnull=True)
                sales_debt_qs = Sale.objects.filter(company=company, status=Sale.Status.DEBT)
                if branch is not None:
                    sales_debt_qs = sales_debt_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    sales_debt_qs = sales_debt_qs.filter(branch__isnull=True)
            else:
                clients_scope = Client.objects.filter(company=company, salesperson=request.user)
                if branch is not None:
                    clients_scope = clients_scope.filter(branch=branch)
                else:
                    clients_scope = clients_scope.filter(branch__isnull=True)
                deals_qs = ClientDeal.objects.filter(
                    company=company,
                    kind=ClientDeal.Kind.DEBT,
                    client__in=clients_scope,
                )
                if branch is not None:
                    deals_qs = deals_qs.filter(branch=branch)
                else:
                    deals_qs = deals_qs.filter(branch__isnull=True)
                sales_debt_qs = Sale.objects.filter(
                    company=company,
                    user=request.user,
                    status=Sale.Status.DEBT,
                )
                if branch is not None:
                    sales_debt_qs = sales_debt_qs.filter(branch=branch)
                else:
                    sales_debt_qs = sales_debt_qs.filter(branch__isnull=True)

            paid_subq_ar = (
                DealInstallment.objects.filter(deal_id=OuterRef("pk"))
                .values("deal_id")
                .annotate(s=Sum("paid_amount"))
                .values("s")[:1]
            )
            deals_ann_qs = (
                deals_qs.select_related("client")
                .annotate(paid=Coalesce(Subquery(paid_subq_ar), V(Decimal("0.00"), output_field=money_field_ar)))
                .annotate(remaining=(F("amount") - F("prepayment")) - F("paid"))
                .filter(remaining__gt=0)
            )
            deal_rows = list(deals_ann_qs.values("id", "title", "remaining", "client_id", "client__full_name"))

            # Чтобы не было двойного учёта "остаток по сделке" + "весь POS-чек в долг",
            # исключаем POS-долги по клиентам с активной рассрочкой (remaining > 0).
            active_client_ids = list(
                deals_ann_qs.exclude(client_id__isnull=True).values_list("client_id", flat=True).distinct()
            )
            sales_debt_filtered = sales_debt_qs
            if active_client_ids:
                sales_debt_filtered = sales_debt_qs.exclude(client_id__in=active_client_ids)
            sale_rows = list(
                sales_debt_filtered
                .select_related("client", "user")
                .order_by("-total", "-id")
                .values("id", "total", "created_at", "client_id", "client__full_name", "user_id")
            )

            merged = []
            for r in deal_rows:
                rem = r.get("remaining") or Decimal("0.00")
                merged.append({
                    "_sort": rem,
                    "kind": "client_deal",
                    "id": str(r["id"]),
                    "title": r.get("title") or "",
                    "amount": str(rem.quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "client": {
                        "id": str(r["client_id"]) if r.get("client_id") else None,
                        "name": r.get("client__full_name") or "",
                    },
                })
            for r in sale_rows:
                tot = r.get("total") or Decimal("0.00")
                merged.append({
                    "_sort": tot,
                    "kind": "sale_debt",
                    "id": str(r["id"]),
                    "total": str(tot.quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "created_at": timezone.localtime(r["created_at"]).isoformat() if r.get("created_at") else None,
                    "client": {
                        "id": str(r["client_id"]) if r.get("client_id") else None,
                        "name": r.get("client__full_name") or "Без имени",
                    },
                    "user_id": str(r["user_id"]) if r.get("user_id") else None,
                })
            merged.sort(key=lambda x: x["_sort"], reverse=True)
            for x in merged:
                x.pop("_sort", None)

            client_deals_total = sum((
                (Decimal(str(r["remaining"])) if r.get("remaining") is not None else Decimal("0"))
                for r in deal_rows
            ), Decimal("0.00"))
            pos_total = sum((
                (Decimal(str(r["total"])) if r.get("total") is not None else Decimal("0"))
                for r in sale_rows
            ), Decimal("0.00"))
            ar_total = (client_deals_total + pos_total).quantize(_Q2, rounding=ROUND_HALF_UP)

            total_count = len(merged)
            page = merged[offset: offset + limit]

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "period": {"type": p_ar["period"], "date_from": date_from_ar, "date_to": date_to_ar},
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {
                    "accounts_receivable": str(ar_total),
                    "accounts_receivable_client_deals": str(client_deals_total.quantize(_Q2, rounding=ROUND_HALF_UP)),
                    "accounts_receivable_pos_sales": str(pos_total.quantize(_Q2, rounding=ROUND_HALF_UP)),
                },
                "items": page,
            })

        # ----- accounts_payable (owner): по контрагентам-поставщикам склада -----
        if card == "accounts_payable":
            if not _is_owner_like(request.user):
                return Response({
                    "card": card,
                    "branch_id": str(getattr(branch, "id", "")) if branch else None,
                    "count": 0,
                    "offset": offset,
                    "limit": limit,
                    "totals": {"accounts_payable": "0.00"},
                    "items": [],
                })
            try:
                from apps.warehouse.models import Document as WDoc, MoneyDocument as WMoney, Counterparty as WCP
            except Exception:
                return Response({
                    "card": card,
                    "branch_id": str(getattr(branch, "id", "")) if branch else None,
                    "count": 0,
                    "offset": offset,
                    "limit": limit,
                    "totals": {"accounts_payable": "0.00"},
                    "items": [],
                })

            cp_qs = WCP.objects.filter(company=company).filter(
                Q(type=WCP.Type.SUPPLIER) | Q(type=WCP.Type.BOTH)
            )
            if branch is not None:
                cp_qs = cp_qs.filter(branch=branch)
            else:
                cp_qs = cp_qs.filter(branch__isnull=True)

            trade_doc_types = (
                WDoc.DocType.SALE,
                WDoc.DocType.PURCHASE,
                WDoc.DocType.SALE_RETURN,
                WDoc.DocType.PURCHASE_RETURN,
            )
            doc_debit_types = (WDoc.DocType.SALE, WDoc.DocType.PURCHASE_RETURN)
            doc_credit_types = (WDoc.DocType.PURCHASE, WDoc.DocType.SALE_RETURN)

            money_field_ap = DecimalField(max_digits=12, decimal_places=2)
            zero_money_ap = V(Decimal("0.00"), output_field=money_field_ap)

            rows_out = []
            ap_sum = Decimal("0.00")
            for cp in cp_qs.order_by("name", "id"):
                docs_qs = WDoc.objects.filter(
                    status=WDoc.Status.POSTED,
                    doc_type__in=trade_doc_types,
                    counterparty=cp,
                    warehouse_from__company=company,
                )
                if branch is not None:
                    docs_qs = docs_qs.filter(warehouse_from__branch=branch)
                else:
                    docs_qs = docs_qs.filter(warehouse_from__branch__isnull=True)

                docs_agg = docs_qs.aggregate(
                    doc_debit=Coalesce(Sum("total", filter=Q(doc_type__in=doc_debit_types)), zero_money_ap),
                    doc_credit=Coalesce(Sum("total", filter=Q(doc_type__in=doc_credit_types)), zero_money_ap),
                )
                doc_debit = docs_agg["doc_debit"] or Decimal("0.00")
                doc_credit = docs_agg["doc_credit"] or Decimal("0.00")

                money_qs = WMoney.objects.filter(
                    company=company,
                    status=WMoney.Status.POSTED,
                    counterparty=cp,
                    doc_type__in=(
                        WMoney.DocType.MONEY_RECEIPT,
                        WMoney.DocType.MONEY_EXPENSE,
                    ),
                )
                if branch is not None:
                    money_qs = money_qs.filter(branch=branch)
                else:
                    money_qs = money_qs.filter(branch__isnull=True)

                money_agg = money_qs.aggregate(
                    m_rec=Coalesce(Sum("amount", filter=Q(doc_type=WMoney.DocType.MONEY_RECEIPT)), zero_money_ap),
                    m_paid=Coalesce(Sum("amount", filter=Q(doc_type=WMoney.DocType.MONEY_EXPENSE)), zero_money_ap),
                )
                m_rec = money_agg["m_rec"] or Decimal("0.00")
                m_paid = money_agg["m_paid"] or Decimal("0.00")

                balance = (doc_debit + m_paid) - (doc_credit + m_rec)
                if balance >= 0:
                    continue
                payable = (-balance).quantize(_Q2, rounding=ROUND_HALF_UP)
                ap_sum += payable
                rows_out.append({
                    "counterparty_id": str(cp.id),
                    "name": cp.name,
                    "accounts_payable": str(payable),
                })

            # CRM-поставщики: Client.type=SUPPLIERS + сделки ClientDeal(kind=DEBT) → обязательства компании
            try:
                from apps.main.models import Client as ClientModel, ClientDeal as ClientDealModel, DealInstallment as DealInstallmentModel
            except Exception:
                ClientModel = None
                ClientDealModel = None
                DealInstallmentModel = None

            if ClientModel and ClientDealModel and DealInstallmentModel:
                supplier_clients_qs = ClientModel.objects.filter(company=company, type=ClientModel.StatusClient.SUPPLIERS)
                if branch is not None:
                    supplier_clients_qs = supplier_clients_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    supplier_clients_qs = supplier_clients_qs.filter(branch__isnull=True)

                supplier_deals_qs = ClientDealModel.objects.filter(
                    company=company,
                    kind=ClientDealModel.Kind.DEBT,
                    client__in=supplier_clients_qs,
                )
                if branch is not None:
                    supplier_deals_qs = supplier_deals_qs.filter(Q(branch=branch) | Q(branch__isnull=True))
                else:
                    supplier_deals_qs = supplier_deals_qs.filter(branch__isnull=True)

                supplier_paid_subq = (
                    DealInstallmentModel.objects.filter(deal_id=OuterRef("pk"))
                    .values("deal_id")
                    .annotate(s=Sum("paid_amount"))
                    .values("s")[:1]
                )
                supplier_rows = (
                    supplier_deals_qs
                    .annotate(paid=Coalesce(Subquery(supplier_paid_subq), V(Decimal("0.00"), output_field=money_field_ap)))
                    .annotate(remaining=(F("amount") - F("prepayment")) - F("paid"))
                    .values("client_id", "client__full_name")
                    .annotate(remaining_total=Coalesce(Sum("remaining"), zero_money_ap))
                )
                for r in supplier_rows:
                    rem = (r.get("remaining_total") or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)
                    if rem <= 0:
                        continue
                    ap_sum += rem
                    rows_out.append({
                        "counterparty_id": str(r["client_id"]),
                        "name": r.get("client__full_name") or "Поставщик",
                        "accounts_payable": str(rem),
                    })

            rows_out.sort(key=lambda x: Decimal(x["accounts_payable"]), reverse=True)
            total_count = len(rows_out)
            page = rows_out[offset: offset + limit]

            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {"accounts_payable": str(ap_sum.quantize(_Q2, rounding=ROUND_HALF_UP))},
                "items": page,
            })

        if card == "users_count":
            qs = User.objects.filter(company=company).order_by("last_name", "first_name", "email", "id")
            total_count = qs.count()
            page = list(
                qs[offset: offset + limit].values(
                    "id", "email", "first_name", "last_name", "phone_number", "role", "is_active"
                )
            )
            items = [
                {
                    "id": str(u["id"]),
                    "email": u.get("email") or "",
                    "first_name": u.get("first_name") or "",
                    "last_name": u.get("last_name") or "",
                    "phone_number": u.get("phone_number"),
                    "role": u.get("role"),
                    "is_active": bool(u.get("is_active")),
                }
                for u in page
            ]
            return Response({
                "card": card,
                "branch_id": str(getattr(branch, "id", "")) if branch else None,
                "count": total_count,
                "offset": offset,
                "limit": limit,
                "totals": {},
                "items": items,
            })

        raise ValidationError({
            "card": f"Unsupported card: {card}",
            "supported": [
                "stock_purchase_value",
                "stock_retail_value",
                "raw_material_value",
                "stock_value",
                "defective_items",
                "returns",
                "defects",
                "inventory_surplus",
                "inventory_shortage",
                "discounts_total",
                "transfers_count",
                "items_transferred",
                "acceptances_count",
                "sales_count",
                "sales_amount",
                "items_on_hand_qty",
                "items_on_hand_amount",
                "revenue",
                "cost_of_goods_sold",
                "gross_profit",
                "gross_margin_percent",
                "accounts_receivable",
                "accounts_payable",
                "total_debt",
                "users_count",
            ],
        })
