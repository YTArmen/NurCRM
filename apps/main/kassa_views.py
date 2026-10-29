"""
Эндпоинты для кассы NurMarket (BACKEND_API.md):

    POST /api/main/pos/checkout/                 — продажа одним запросом (Idempotency-Key)
    GET  /api/main/pos/returns/                  — список возвратов
    GET  /api/main/clients/debtors/              — сводка по должникам
    POST /api/main/clients/{id}/pay-debt/        — погашение долга клиента одной суммой
    POST /api/main/clients/{id}/bonus/           — начислить/списать бонусы
    GET  /api/main/clients/{id}/bonus/history/   — история бонусов
"""
import uuid
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import DecimalField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from django.core.exceptions import ValidationError as DjangoValidationError
from apps.construction.models import CashFlow, CashShift, Cashbox
from apps.users.models import Branch
from apps.main.models import (
    Cart,
    CartItem,
    Client,
    ClientBonusTransaction,
    ClientDeal,
    DealInstallment,
    DealPayment,
    Product,
    ProductPackage,
    ProductVariant,
    Sale,
    SalePayment,
    SaleReturn,
)
from apps.main.pos_serializers import MoneyField, QtyField, _is_owner_like
from apps.main.pos_utils import money, qty3
from apps.main.tz26.pos_sync.services import return_origin_fields  # Б-67
from apps.main.views import CompanyBranchRestrictedMixin, _filter_clients_visible_for_user
from core.api_compat import is_new_client, is_strict, parse_client
import logging

logger = logging.getLogger(__name__)
from core.exceptions import ApiError
from apps.main.tz26.hw_prices.services import (
    attach_debtor_objects as hw_attach_debtor_objects,
    check_credit_limit as hw_check_credit_limit,
    default_due_date as hw_default_due_date,
    link_direct_debt as hw_link_direct_debt,
    reprice_for_request as hw_reprice_for_request,
)

Q2 = Decimal("0.01")
ZERO = Decimal("0.00")


class PriceChangedError(APIException):
    """
    Б-03 (27.10.2026): цена строки чека не совпадает с текущей ценой каталога, а клиент
    прислал заголовок X-NurMarket-Strict-Price: 1. Чек не проводится, касса получает
    актуальные цены и пересчитывает чек.
    """

    status_code = status.HTTP_409_CONFLICT
    default_code = "price_changed"

    def __init__(self, mismatch):
        super().__init__({
            "code": "price_changed",
            "detail": "Цены изменились. Обновите каталог и пересчитайте чек.",
            "price_mismatch": mismatch,
        })


def _strict_price(request) -> bool:
    headers = getattr(request, "headers", None) or {}
    raw = headers.get("X-NurMarket-Strict-Price")
    if raw is None:
        # pos/checkout/batch/ подставляет обычный dict: ключи там в том регистре, в каком пришли.
        for k, v in dict(headers).items():
            if str(k).lower() == "x-nurmarket-strict-price":
                raw = v
                break
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _idempotency_key(request, required=True):
    key = (request.headers.get("Idempotency-Key") or request.data.get("idempotency_key") or "").strip()
    if not key and required:
        raise ValidationError({"Idempotency-Key": "Заголовок Idempotency-Key обязателен."})
    if len(key) > 128:
        raise ValidationError({"Idempotency-Key": "Не длиннее 128 символов."})
    return key or None


def _company(request):
    company = getattr(request.user, "owned_company", None) or getattr(request.user, "company", None)
    if company is None:
        raise ValidationError({"detail": "Пользователь не привязан к компании."})
    return company


# ======================================================================
# Продажа одним запросом
# ======================================================================

MAX_CHECKOUT_ITEMS = 1000  # ТЗ Н-15: больше позиций в одном чеке не принимаем
TOTAL_TOLERANCE = Decimal("0.01")


def _line_error(code, idx, detail, *, http=400, **extra):
    """
    ТЗ Н-15: отказ по строке чека. Ключи `items` ({индекс: текст}) и `fields` остаются как раньше,
    добавлены `code` и `errors[]` с `index` (с 0).
    """
    entry = {"index": idx, "code": code, "detail": detail, **extra}
    top = {"errors": [entry]}
    fields = None
    if idx is not None:
        top["items"] = {idx: detail}
        fields = {f"items[{idx}]": [detail]}
    return ApiError(code, detail, status_code=http, top=top, fields=fields)

class QuickCheckoutItemSerializer(serializers.Serializer):
    custom = serializers.BooleanField(required=False, default=False)
    product = serializers.UUIDField(required=False, allow_null=True)
    sale_package = serializers.UUIDField(required=False, allow_null=True)
    # Продажа упаковкой: qty — число упаковок, цена — цена упаковки (обрабатывает apps.possync.packs)
    sale_package_id = serializers.UUIDField(required=False, allow_null=True)
    variant = serializers.UUIDField(required=False, allow_null=True)
    # ТЗ 26.10 Б-53: единица продажи (id); qty — в этих единицах, price — цена единицы
    sale_unit = serializers.IntegerField(required=False, allow_null=True)
    sale_unit_id = serializers.IntegerField(required=False, allow_null=True)
    performer = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    qty = QtyField()
    price = MoneyField(required=False, allow_null=True)
    discount = MoneyField(required=False, allow_null=True, default=ZERO)
    is_wholesale = serializers.BooleanField(required=False, default=False)
    # Б-03 (27.10.2026): кассир сознательно продал не по цене каталога — не считать расхождением.
    price_override = serializers.BooleanField(required=False, default=False)

    # ТЗ 11.5: принимается псевдоним quantity.
    def to_internal_value(self, data):
        if isinstance(data, dict) and data.get("qty") in (None, "") and data.get("quantity") not in (None, ""):
            data = {**data, "qty": data["quantity"]}
        return super().to_internal_value(data)

    # ТЗ 11.5: принимается псевдоним quantity.
    def to_internal_value(self, data):
        if isinstance(data, dict) and data.get("qty") in (None, "") and data.get("quantity") not in (None, ""):
            data = {**data, "qty": data["quantity"]}
        return super().to_internal_value(data)

    def validate(self, attrs):
        if attrs["qty"] <= 0:
            raise serializers.ValidationError({"qty": "Количество должно быть больше нуля."})
        if (attrs.get("discount") or ZERO) < 0:
            raise serializers.ValidationError({"discount": "Скидка не может быть отрицательной."})
        if attrs.get("custom"):
            if not (attrs.get("name") or "").strip():
                raise serializers.ValidationError({"name": "Укажите название позиции."})
            if attrs.get("price") is None or attrs["price"] < 0:
                raise serializers.ValidationError({"price": "Укажите цену позиции."})
        elif not attrs.get("product"):
            raise serializers.ValidationError({"product": "Укажите товар."})
        return attrs


class CheckoutItemSerializer(QuickCheckoutItemSerializer):
    """Строка чека POS checkout: qty/quantity обязательны (раньше молча ставилась 1 шт.)."""

    def to_internal_value(self, data):
        if isinstance(data, dict) and all(data.get(k) in (None, "") for k in ("qty", "quantity")):
            raise serializers.ValidationError({"qty": "Укажите количество (qty или quantity)."})
        return super().to_internal_value(data)


class QuickCheckoutPaymentSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=Sale.PaymentMethod.choices, default=Sale.PaymentMethod.CASH)
    received = MoneyField(required=False, allow_null=True)
    cash_amount = MoneyField(required=False, allow_null=True)
    card_amount = MoneyField(required=False, allow_null=True)
    card_method = serializers.CharField(required=False, allow_blank=True)
    prepayment_method = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    payments = serializers.ListField(child=serializers.DictField(), required=False)
    # ТЗ Н-15: срок долга одной датой (без отдельного PATCH сделки)
    debt_due_date = serializers.DateField(required=False, allow_null=True)

    def to_internal_value(self, data):
        # ТЗ 12.9: старое имя cash_received принимается как received.
        if isinstance(data, dict) and data.get("received") in (None, "") and data.get("cash_received") not in (None, ""):
            data = {**data, "received": data["cash_received"]}
        return super().to_internal_value(data)

    def to_internal_value(self, data):
        # ТЗ 12.9: старое имя cash_received принимается как received.
        if isinstance(data, dict) and data.get("received") in (None, "") and data.get("cash_received") not in (None, ""):
            data = {**data, "received": data["cash_received"]}
        return super().to_internal_value(data)



class SoftMoneyField(serializers.Field):
    """Горячая правка 29.10: сверочная сумма (expected_total) — подсказка, а не условие продажи.
    Касса шлёт double с хвостом (1297.5400000000001) — DRF DecimalField отказывал «не более 12 цифр».
    Округляем до копеек, мусор — None (сверка просто не выполняется)."""

    def to_internal_value(self, data):
        if data in (None, ""):
            return None
        try:
            return money(Decimal(str(data)).quantize(Decimal("0.01")))
        except Exception:  # noqa: BLE001
            return None

    def to_representation(self, value):
        return None if value is None else str(value)


class QuickCheckoutSerializer(serializers.Serializer):
    shift = serializers.UUIDField(required=False, allow_null=True)
    client = serializers.UUIDField(required=False, allow_null=True)
    items = CheckoutItemSerializer(many=True, allow_empty=False)
    order_discount_total = MoneyField(required=False, allow_null=True)
    order_discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True, min_value=0, max_value=100
    )
    bonus_redeemed = MoneyField(required=False, allow_null=True)
    # ТЗ Н-15: итог, который видит кассир; расхождение с серверным > 0.01 → 409 total_mismatch
    expected_total = SoftMoneyField(required=False, allow_null=True)
    payment = QuickCheckoutPaymentSerializer()
    prepayment_method = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    consultant_id = serializers.UUIDField(required=False, allow_null=True)
    consultant_commission_enabled = serializers.BooleanField(required=False, default=False)
    consultant_commission_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True
    )
    print_receipt = serializers.BooleanField(required=False, default=False)
    allow_minus = serializers.BooleanField(required=False, default=False)
    is_wholesale = serializers.BooleanField(required=False, default=False)
    # Б-03: все цены чека заданы сознательно (не сверять с каталогом).
    price_override = serializers.BooleanField(required=False, default=False)
    # BE2-10: продажа, сделанная без связи и досланная позже.
    offline = serializers.BooleanField(required=False, default=False)
    offline_created_at = serializers.DateTimeField(required=False, allow_null=True)
    # ТЗ ч.12, 2.4: время продажи на кассе. Если смена к моменту отправки закрыта или неизвестна
    # серверу — продажа идёт в смену этой кассы, открытую в это время, иначе в техническую смену дня.
    sold_at = serializers.DateTimeField(required=False, allow_null=True)
    cashbox = serializers.UUIDField(required=False, allow_null=True)
    # Удалённые строки корзины (веб-касса) — пишутся в CartItemDeletionLog (apps.possync.deletions)
    deleted_items = serializers.ListField(child=serializers.DictField(), required=False, allow_empty=True, max_length=500)
    # График долга (веб-касса): создаётся вместе со сделкой (apps.possync.schedule)
    debt_schedule = serializers.JSONField(required=False, allow_null=True)
    schedule_version = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    def to_internal_value(self, data):
        items = data.get("items")
        if items is not None and not items:
            raise serializers.ValidationError({"code": "empty_sale", "detail": "В чеке нет позиций"})
        return super().to_internal_value(data)

    def validate(self, attrs):
        items = attrs.get("items")
        if not items:
            raise serializers.ValidationError({"code": "empty_sale", "detail": "В чеке нет позиций"})
        if attrs.get("sold_at") and not attrs.get("offline_created_at"):
            attrs["offline_created_at"] = attrs["sold_at"]
        if attrs.get("sold_at") and attrs["sold_at"] < timezone.now() - OFFLINE_CLOCK_SKEW:
            attrs["offline"] = True
        if attrs.get("offline"):
            at = attrs.get("offline_created_at")
            if at is None:
                raise serializers.ValidationError({"offline_created_at": "Для продажи без связи нужно время продажи."})
            now = timezone.now()
            if at > now + OFFLINE_CLOCK_SKEW:
                raise serializers.ValidationError({"offline_created_at": "Время продажи в будущем."})
            if at < now - OFFLINE_MAX_AGE:
                raise serializers.ValidationError({"offline_created_at": "Продажа старше 7 суток — оформите её вручную."})
        return attrs


def _quick_result(sale: Sale) -> dict:
    if not sale.doc_number:
        # ТЗ Н-15: номер есть в любом ответе продажи, в том числе у старых чеков без номера
        from apps.main.utils_numbers import ensure_sale_doc_number

        ensure_sale_doc_number(sale)
    return {
        "id": str(sale.id),
        "number": sale.doc_number,
        "status": sale.status,
        "subtotal": str(money(sale.subtotal or ZERO)),
        "discount_total": str(money(sale.discount_total or ZERO)),
        "bonus_redeemed": str(money(sale.bonus_redeemed or ZERO)),
        "total": str(money(sale.total or ZERO)),
        "payment_method": sale.payment_method,
        "cash_received": str(money(sale.cash_received or ZERO)),
        "change": str(money(sale.change or ZERO)),
        "paid_now": str(money(getattr(sale, "paid_now", ZERO) or ZERO)),
        "paid_cash": str(money(getattr(sale, "cash_amount", ZERO) or ZERO)),
        "paid_card": str(money(getattr(sale, "card_amount", ZERO) or ZERO)),
        "cash_amount": str(money(getattr(sale, "cash_amount", ZERO) or ZERO)),
        "card_amount": str(money(getattr(sale, "card_amount", ZERO) or ZERO)),
        "debt_initial": str(money(getattr(sale, "debt_initial", ZERO) or ZERO)),
        "debt_remaining": str(money(getattr(sale, "debt_remaining", ZERO) or ZERO)),
        "debt_amount": str(money(getattr(sale, "debt_remaining", ZERO) or ZERO)),
        "shift": str(sale.shift_id) if sale.shift_id else None,
        "client": str(sale.client_id) if sale.client_id else None,
        "source": sale.source, "device_key": sale.device_key, "device_name": sale.device_name,  # Б-67
        "items": [_quick_item(it) for it in sale.items.all().order_by("id")],
    }


def _quick_item(it) -> dict:
    """Расшифровка цены строки (BE2-07): касса сверяет итог со своим."""
    base = money((it.unit_price or ZERO) * (it.quantity or ZERO))
    discount = money(it.line_discount or ZERO)
    return {
        "id": str(it.id),
        "product": str(it.product_id) if it.product_id else None,
        "is_custom": getattr(it, "is_custom", False) or (it.product_id is None),
        "variant": str(it.variant_id) if it.variant_id else None,
        "name": it.name_snapshot,
        "qty": str(it.quantity),
        "quantity": str(it.quantity),
        "price": str(money(it.unit_price or ZERO)),
        "discount": str(discount),
        "discount_source": it.discount_source,
        "promotion_id": str(it.promotion_id) if it.promotion_id else None,
        "manual_discount": str(money(it.manual_discount or ZERO)),
        "manual_discount_ignored": it.discount_source == "promotion" and (it.manual_discount or ZERO) > 0,
        "amount": str(money(base - discount)),
        "cost_price": str(money(getattr(it, "purchase_price_snapshot", ZERO) or ZERO)),
        "total": str(money(base - discount)),
        "line_total": str(money(base - discount)),  # ТЗ 26.10 Б-52: имя из приёмки
        **_sale_unit_fields(it),
    }


def _sale_unit_fields(it) -> dict:
    """ТЗ 26.10 Б-53: sale_unit {id,name,factor} и base_quantity в строке ответа (null у обычных строк)."""
    from apps.main.tz26.hw_units.units import sale_unit_line_fields

    su, bq = sale_unit_line_fields(it)
    return {"sale_unit": su, "base_quantity": bq}


def resolve_performers(company_id, ids):
    """{uuid: User} активных сотрудников компании; чужой/несуществующий id — 400."""
    from django.contrib.auth import get_user_model

    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    users = {
        u.id: u
        for u in get_user_model().objects.filter(id__in=wanted, company_id=company_id, is_active=True)
    }
    missing = wanted - set(users)
    if missing:
        raise ValidationError({"performer": "Мастер не найден в компании."})
    return users


def _is_admin_for_discounts(user) -> bool:
    # как в остальных проверках max_discount_percent
    return getattr(user, "role", None) in ("owner", "admin")


OFFLINE_MAX_AGE = timedelta(days=7)
OFFLINE_CLOCK_SKEW = timedelta(minutes=5)


def _resolve_offline_shift(*, company, shift_id, at):
    """Смена, в которой касса сделала продажу без связи; она могла уже закрыться."""
    shift = CashShift.objects.select_related("cashbox", "cashier").filter(id=shift_id, company=company).first()
    if shift is None:
        raise ValidationError({"shift": "Смена не найдена."})
    if shift.opened_at and at < shift.opened_at - OFFLINE_CLOCK_SKEW:
        raise ValidationError({"offline_created_at": "Время продажи раньше открытия смены."})
    return shift


TECHNICAL_SHIFT_REASON = "technical_offline"


def _find_offline_shift(*, company, cashier, at, shift_id=None, cashbox_id=None):
    """
    Смена для продажи без связи (ТЗ ч.12, 2.4):
    1) переданная кассой смена, если сервер её знает и время продажи в её пределах;
    2) смена этого кассира (на этой кассе, если она указана), открытая в момент продажи — даже закрытая;
    3) техническая смена дня этого кассира на этой кассе (создаётся закрытой).
    """
    from apps.construction.models import Cashbox

    def _covers(sh):
        start = (sh.opened_at or at) - OFFLINE_CLOCK_SKEW
        return start <= at and (sh.closed_at is None or at <= sh.closed_at + OFFLINE_CLOCK_SKEW)

    if shift_id:
        sh = CashShift.objects.select_related("cashbox").filter(id=shift_id, company=company).first()
        if sh is not None and _covers(sh):
            return sh
        if sh is not None and not cashbox_id:
            cashbox_id = sh.cashbox_id

    qs = CashShift.objects.select_related("cashbox").filter(
        company=company, cashier=cashier, opened_at__lte=at + OFFLINE_CLOCK_SKEW
    )
    if cashbox_id:
        qs = qs.filter(cashbox_id=cashbox_id)
    for sh in qs.order_by("-opened_at")[:10]:
        if _covers(sh):
            return sh

    cashbox = None
    if cashbox_id:
        cashbox = Cashbox.objects.filter(id=cashbox_id, company=company).first()
        if cashbox is None:
            raise ValidationError({"cashbox": "Касса не найдена."})
    if cashbox is None:
        last = CashShift.objects.filter(company=company, cashier=cashier).order_by("-opened_at").first()
        cashbox = last.cashbox if last else None
    if cashbox is None:
        cashbox = (
            Cashbox.objects.filter(company=company, is_active=True)
            .order_by("created_at")
            .first()
        )
    if cashbox is None:
        raise ValidationError({"detail": "Нет кассы для продажи без связи.", "code": "no_cashbox"})

    local = timezone.localtime(at)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)
    sh = (
        CashShift.objects.select_for_update()
        .filter(cashbox=cashbox, cashier=cashier, close_reason=TECHNICAL_SHIFT_REASON, opened_at=day_start)
        .first()
    )
    if sh is None:
        sh = CashShift.objects.create(
            company=company,
            cashbox=cashbox,
            cashier=cashier,
            status=CashShift.Status.CLOSED,
            closed_at=day_end,
            close_reason=TECHNICAL_SHIFT_REASON,
            opening_cash=ZERO,
            closing_cash=None,
        )
        CashShift.objects.filter(pk=sh.pk).update(opened_at=day_start)
        sh.opened_at = day_start
    return sh


def _backdate_offline_sale(sale, at):
    """
    Продажа без связи попадает в отчёты своего дня и своей смены: переносим время
    продажи, её оплат и движений денег; итоги уже закрытой смены пересчитываем.
    """
    now = timezone.now()
    Sale.objects.filter(pk=sale.pk).update(created_at=at, paid_at=at, is_offline=True, received_at=now)
    SalePayment.objects.filter(sale_id=sale.pk).update(created_at=at)
    CashFlow.objects.filter(company_id=sale.company_id, source_id=str(sale.pk)).update(created_at=at)
    shift = sale.shift
    if shift is not None and shift.status == CashShift.Status.CLOSED:
        shift.recalc_totals_for_close()
        shift.save(update_fields=[
            "income_total", "expense_total", "sales_count", "sales_total",
            "cash_sales_total", "noncash_sales_total",
        ])


from apps.users.permissions_flags import PosAccess  # noqa: E402  ТЗ Н-16


class PosQuickCheckoutAPIView(APIView):
    """
    POST /api/main/pos/checkout/
    Idempotency-Key: <uuid кассы>

    Корзина, позиции, скидки и оплата — одним запросом. Повтор с тем же ключом
    возвращает уже созданную продажу (200), дубль не создаётся.
    """

    permission_classes = [permissions.IsAuthenticated, PosAccess]  # ТЗ Н-16

    @staticmethod
    def _shift_not_owned(request, company):
        """ТЗ 29.10 Б-13: чужая смена — code shift_not_owned (новый клиент: 403, старый — прежний 400)."""
        msg = "Это не ваша смена."
        return ApiError(
            "shift_not_owned", msg,
            status_code=403 if is_strict(request, company) else 400,
            top={"shift": msg},
        )

    def post(self, request, *args, **kwargs):
        from apps.main.pos_views import (
            SaleCheckoutAPIView,
            _discount_limit_error,
            _find_open_shift_for_cashier,
            _resolve_requested_open_shift,
        )

        key = _idempotency_key(request)
        company = _company(request)

        existing = Sale.objects.filter(company=company, idempotency_key=key).first()
        if existing:
            return Response({**_quick_result(existing), "replayed": True}, status=status.HTTP_200_OK)

        self._neg_discount = ZERO
        from apps.main.tz26.pos_sync.services import resolve_origin  # Б-67
        self._origin = resolve_origin(request)
        payload = self._precheck_items(request.data, strict=is_strict(request, company))
        ser = QuickCheckoutSerializer(data=payload)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        user = request.user
        # ТЗ Н-15: офлайн-досылка (offline + sold_at): цены кассы, остаток в минус, вариант не обязателен
        self._offline = bool(data.get("offline") and data.get("offline_created_at"))
        self._warnings = []
        self._variant_missing = set()
        self._index_by_identity = {id(it): i for i, it in enumerate(data["items"])}
        self._index_by_key = {}
        for i, it in enumerate(data["items"]):
            self._index_by_key.setdefault((it.get("product"), it.get("variant")), i)
        self._index_by_product = {}
        for i, it in reversed(list(enumerate(data["items"]))):
            self._index_by_product[it.get("product")] = i

        try:
            with transaction.atomic():
                offline_at = data.get("offline_created_at") if data.get("offline") else None
                if offline_at and data.get("sold_at"):
                    shift = _find_offline_shift(
                        company=company, cashier=user, at=offline_at,
                        shift_id=data.get("shift"), cashbox_id=data.get("cashbox"),
                    )
                    if shift.cashier_id != user.id and not _is_owner_like(user):
                        raise self._shift_not_owned(request, company)
                elif data.get("shift") and offline_at:
                    shift = _resolve_offline_shift(company=company, shift_id=data["shift"], at=offline_at)
                    if shift.cashier_id != user.id and not _is_owner_like(user):
                        raise self._shift_not_owned(request, company)
                elif data.get("shift"):
                    shift = _resolve_requested_open_shift(company=company, cashier=user, shift_id=data["shift"])
                    if shift.cashier_id != user.id and not _is_owner_like(user):
                        raise self._shift_not_owned(request, company)
                else:
                    shift = _find_open_shift_for_cashier(company=company, cashier=user)
                if shift is None:
                    raise ValidationError(
                        {"detail": "Смена не открыта. Сначала откройте смену на кассе, затем завершите продажу.",
                         "code": "shift_closed"}
                    )

                cart = Cart.objects.create(
                    company=company,
                    user=user,
                    status=Cart.Status.ACTIVE,
                    branch=shift.branch,
                    shift=shift,
                    is_default=False,
                    is_wholesale=bool(data.get("is_wholesale")),
                )
                max_dp = getattr(company, "max_discount_percent", None)
                # В офлайн-досылке продажа уже состоялась по ценам кассы: лимит скидок не применяем
                limit_discounts = max_dp is not None and not _is_admin_for_discounts(user) and not self._offline
                self._price_mismatch = []
                self._price_override_all = bool(data.get("price_override"))
                self._add_items(cart, data["items"], limit_discounts, max_dp)
                hw_reprice_for_request(request, cart, data.get("client"))  # ТЗ 26.10 Б-55: цена по уровню клиента (под флагом)
                if self._price_mismatch and _strict_price(request):
                    # Б-03: строгий режим (новая касса, онлайн): чек не проводим, отдаём актуальные цены.
                    raise PriceChangedError(self._price_mismatch)

                bonus = money(data.get("bonus_redeemed") or ZERO)
                self._apply_order_discount(cart, data, bonus, limit_discounts, max_dp)
                self._check_expected_total(cart, data)

                client = None
                if data.get("client"):
                    client = get_object_or_404(Client, id=data["client"], company=company)
                if bonus > 0:
                    if client is None:
                        raise ValidationError({"bonus_redeemed": "Списать бонусы можно только у клиента."})
                    if bonus > (client.bonus_balance or ZERO):
                        raise ValidationError({"bonus_redeemed": "Недостаточно бонусов у клиента."})

                resp = SaleCheckoutAPIView().checkout(
                    request, cart.id, self._checkout_data(data, cart),
                    force_negative_stock=self._offline, lenient_variants=self._offline,
                )
                if resp.status_code >= 400:
                    transaction.set_rollback(True)
                    self._fill_error_indexes(resp)
                    return resp

                sale = Sale.objects.select_for_update().get(pk=resp.data["sale_id"])
                if bonus > 0:
                    # Бонусы — отдельное поле: total = subtotal - discount_total - bonus_redeemed
                    sale.discount_total = money((sale.discount_total or ZERO) - bonus)
                    sale.bonus_redeemed = bonus
                    change_bonus(
                        client=client,
                        delta=-bonus,
                        reason=ClientBonusTransaction.Reason.REDEEM,
                        sale=sale,
                        user=user,
                    )
                # Хук для проверок после создания чека (лимиты долга и т.п.): исключение откатывает всю транзакцию.
                self.after_sale_created(request, sale, client, data)
                self._apply_debt_due_date(sale, data)
                sale.idempotency_key = key
                Sale.objects.filter(pk=sale.pk).update(
                    idempotency_key=key,
                    bonus_redeemed=sale.bonus_redeemed,
                    discount_total=sale.discount_total,
                    **self._origin,  # Б-67: source, device_key, device_name
                )
                if request.data.get("print_on"):  # ТЗ 26.10 Б-69: задание печати в той же транзакции
                    from apps.main.tz26.pos_print.services import create_print_job_from_checkout
                    create_print_job_from_checkout(sale, request.data.get("print_on"), request)
                if offline_at:
                    _backdate_offline_sale(sale, offline_at)
        except IntegrityError:
            existing = Sale.objects.filter(company=company, idempotency_key=key).first()
            if existing:
                return Response({**_quick_result(existing), "replayed": True}, status=status.HTTP_200_OK)
            raise
        except ValidationError as e:
            if isinstance(e.detail, dict) and e.detail.get("_discount_limit"):
                body = _discount_limit_error(max_dp)
                # ТЗ Н-15: прежний code «discount_limit» остаётся; в errors[] — новый код и номер строки
                body["errors"] = [{
                    "index": getattr(e, "index", None), "code": "discount_limit_exceeded",
                    "detail": body["detail"], "max_discount_percent": str(max_dp),
                }]
                return Response(body, status=status.HTTP_400_BAD_REQUEST)
            if isinstance(e.detail, dict) and e.detail.get("code") and "errors" not in e.detail:
                code = e.detail["code"]
                code = code[0] if isinstance(code, (list, tuple)) else code
                e.detail["errors"] = [{"index": None, "code": str(code), "detail": str(e.detail.get("detail") or "")}]
            raise

        sale.refresh_from_db()
        body = _quick_result(sale)
        for k in ("cashflows", "receipt_print_path", "ekassa", "payments"):
            if k in resp.data:
                body[k] = resp.data[k]
        if self._offline:
            # ТЗ Н-15: что допущено при досылке — остаток в минус, вариант не выбран
            for it in body["items"]:
                if it["product"] in {str(x) for x in self._variant_missing} and not it["variant"]:
                    it["variant_missing"] = True
            if self._warnings:
                body["warnings"] = self._warnings
        # Б-03: расхождения цен строк с каталогом — в ответ и в журнал продажи.
        mismatch = list(getattr(self, "_price_mismatch", None) or [])
        body["price_mismatch"] = mismatch
        if mismatch:
            try:
                from apps.construction import audit as admin_audit

                admin_audit.log_request(
                    request, "price_mismatch", "sale", sale.pk, str(sale.doc_number or ""),
                    after={"items": mismatch}, company=company,
                )
            except Exception:
                pass
        return Response(body, status=status.HTTP_201_CREATED)

    # --- helpers ---

    def after_sale_created(self, request, sale, client, data):
        """Переопределяется в apps.possync: проверки внутри транзакции продажи."""

    # --- ТЗ Н-15 ---

    def _precheck_items(self, raw, strict=True):
        """
        Ограничение на число позиций и общее правило для custom-строк с отрицательной ценой.
        Б-02: старый клиент (флаг api_strict_2026_10 выключен) — «Расход» с минусом проводим
        как скидку на чек на эту сумму; строка в чек не попадает. Возвращает данные для сериализатора.
        """
        items = raw.get("items") if hasattr(raw, "get") else None
        if not isinstance(items, list):
            return raw
        if len(items) > MAX_CHECKOUT_ITEMS:
            raise ApiError(
                "too_many_items", f"В чеке не больше {MAX_CHECKOUT_ITEMS} позиций.",
                top={"limit": MAX_CHECKOUT_ITEMS, "count": len(items),
                     "errors": [{"index": None, "code": "too_many_items", "detail": f"Не больше {MAX_CHECKOUT_ITEMS} позиций."}]},
            )
        kept, neg_sum, had_neg = [], ZERO, False
        for idx, it in enumerate(items):
            if isinstance(it, dict) and it.get("custom"):
                try:
                    price = Decimal(str(it.get("price")))
                    neg = price < 0
                except Exception:  # noqa: BLE001 — остальное проверит сериализатор
                    neg = False
                if neg:
                    if strict:
                        raise _line_error(
                            "negative_price", idx,
                            "Цена своей позиции не может быть отрицательной. Для уменьшения суммы используйте скидку.",
                        )
                    try:
                        qty = Decimal(str(it.get("qty") or it.get("quantity") or "1"))
                    except Exception:  # noqa: BLE001
                        qty = Decimal("1")
                    neg_sum += money(-price * qty)
                    had_neg = True
                    continue
            kept.append(it)
        if not had_neg:
            return raw
        self._neg_discount = neg_sum
        payload = raw.copy() if hasattr(raw, "copy") else dict(raw)
        payload["items"] = kept
        return payload

    def _orig_index(self, pos, item):
        """Индекс строки в теле запроса (после слияния упаковок позиции в списке могут сдвинуться)."""
        by_id = getattr(self, "_index_by_identity", None)
        if by_id is None:
            return pos
        if id(item) in by_id:
            return by_id[id(item)]
        return self._index_by_key.get((item.get("product"), item.get("variant")), pos)

    def _fill_error_indexes(self, resp):
        """Проставляет index в errors[] отказа по остатку: ищем первую строку с этим товаром/вариантом."""
        data = getattr(resp, "data", None)
        if not isinstance(data, dict):
            return
        for e in data.get("errors") or []:
            if e.get("index") is not None or not e.get("product_id"):
                continue
            pid = uuid.UUID(e["product_id"])
            vid = uuid.UUID(e["variant_id"]) if e.get("variant_id") else None
            idx = self._index_by_key.get((pid, vid)) if vid else None
            if idx is None:
                idx = self._index_by_product.get(pid)
            e["index"] = idx

    def _check_expected_total(self, cart, data):
        expected = data.get("expected_total")
        if expected is None:
            return
        # Б-02: мобилка < 1.18.5 сверку итога не ждёт — не применяем
        req = getattr(self, "request", None)
        if req is not None and parse_client(req)[0] == "mobile" and not is_new_client(req):
            return
        cart.recalc()
        server_total = money(cart.total or ZERO)
        if abs(server_total - money(expected)) <= TOTAL_TOLERANCE:
            return
        # Горячая правка 29.10: до 29.10 сверки не было — старым кассам и сайту чек не отказываем,
        # только пишем расхождение в журнал. 409 total_mismatch — для новых клиентов.
        if req is not None and not is_strict(req):
            logger.warning("total_mismatch (не strict) cart=%s server=%s expected=%s", cart.pk, server_total, expected)
            return
        lines = []
        for i, ci in enumerate(CartItem.objects.filter(cart=cart)):
            base = money((ci.unit_price or ZERO) * (ci.quantity or ZERO))
            lines.append({
                "index": i,
                "product": str(ci.product_id) if ci.product_id else None,
                "variant": str(ci.variant_id) if ci.variant_id else None,
                "name": ci.custom_name or None,
                "qty": str(ci.quantity),
                "price": str(money(ci.unit_price or ZERO)),
                "discount": str(money(ci.line_discount or ZERO)),
                "amount": str(money(base - money(ci.line_discount or ZERO))),
            })
        raise ApiError(
            "total_mismatch", "Итог чека на сервере отличается от итога кассы.",
            status_code=status.HTTP_409_CONFLICT,
            top={
                "server_total": str(server_total), "expected_total": str(money(expected)), "lines": lines,
                "errors": [{"index": None, "code": "total_mismatch", "server_total": str(server_total),
                            "expected_total": str(money(expected))}],
            },
        )

    def _apply_debt_due_date(self, sale, data):
        """payment.debt_due_date → срок долговой сделки (через общий график v1/v2, один взнос на эту дату)."""
        due = (data.get("payment") or {}).get("debt_due_date")
        if due is None or data.get("debt_schedule") not in (None, "", [], {}):
            return  # явный график важнее
        from apps.possync.schedule import apply_debt_schedule

        deal = ClientDeal.objects.filter(sale=sale, kind=ClientDeal.Kind.DEBT).order_by("created_at").first()
        if deal is None:
            return
        if due < timezone.localdate() and not self._offline:
            raise ApiError(
                "invalid_debt_schedule", "Срок долга не может быть в прошлом.",
                fields={"payment.debt_due_date": ["Дата в прошлом."]},
            )
        apply_debt_schedule(sale, {
            "debt_schedule": [{"due_date": due.isoformat(), "amount": str(deal.debt_amount)}],
            "schedule_version": data.get("schedule_version"),
        })

    def _add_items(self, cart, items, limit_discounts, max_dp):
        """
        Строки чека одним пакетом: товары, упаковки и варианты читаются тремя запросами на весь чек,
        CartItem пишутся bulk_create, итоги корзины считаются один раз (ТЗ Н-15: чек до 1000 позиций).
        """
        from apps.main.pos_utils import line_qty_consume_units
        from apps.main.pos_views import _q2, default_unit_price_for_package

        offline = bool(getattr(self, "_offline", False))
        cid = cart.company_id
        product_ids = {it["product"] for it in items if not it.get("custom")}
        products = {p.id: p for p in Product.objects.filter(company_id=cid, id__in=product_ids)}
        pkg_ids = {it["sale_package"] for it in items if it.get("sale_package")}
        packages = {p.id: p for p in ProductPackage.objects.filter(company_id=cid, id__in=pkg_ids)}
        var_ids = {it["variant"] for it in items if it.get("variant")}
        variants = {
            v.id: v for v in ProductVariant.objects.filter(company_id=cid, id__in=var_ids).select_related("product")
        }
        with_variants = set(
            ProductVariant.objects.filter(product_id__in=product_ids, is_active=True).values_list("product_id", flat=True)
        )
        performers = resolve_performers(cid, [it.get("performer") for it in items])
        demand_p, demand_v = defaultdict(lambda: Decimal("0")), defaultdict(lambda: Decimal("0"))
        from apps.main.tz26.hw_units.units import effective_unit_price, load_line_units, pick_line_unit

        unit_map, unit_seen = load_line_units(cid, items), {}  # ТЗ 26.10 Б-53

        if not hasattr(self, "_price_mismatch"):
            self._price_mismatch = []
        rows = []
        for pos, it in enumerate(items):
            idx = self._orig_index(pos, it)
            qty = qty3(it["qty"])
            performer = performers.get(it.get("performer"))
            discount = money(it.get("discount") or ZERO)
            if it.get("custom"):
                price = _q2(it["price"])
                if price < 0:
                    raise _line_error("negative_price", idx, "Цена своей позиции не может быть отрицательной.")
                if discount > price * qty:
                    raise ValidationError({"items": {idx: "Скидка больше суммы позиции."}})
                rows.append(CartItem(
                    company_id=cid, branch_id=cart.branch_id, cart=cart, product=None,
                    custom_name=it["name"].strip(), unit_price=price, quantity=qty,
                    line_discount=discount, manual_discount=discount, performer=performer,
                ))
                continue

            product = products.get(it["product"])
            if product is None:
                raise _line_error("product_not_found", idx, "Товар не найден.")
            pkg = None
            if it.get("sale_package"):
                pkg = packages.get(it["sale_package"])
                if pkg is None or pkg.product_id != product.id:
                    raise _line_error("package_not_found", idx, "Упаковка не найдена.")
            variant = None
            if it.get("variant"):
                variant = variants.get(it["variant"])
                if variant is not None and variant.product_id != product.id:
                    variant = None
                if variant is None:
                    if not offline:
                        raise _line_error("variant_not_found", idx, "Вариант не найден.")
                    # ТЗ Н-15: офлайн — списываем с товара, помечаем variant_missing
                    self._variant_missing.add(product.id)
                    self._warnings.append({"code": "variant_missing", "index": idx})
            elif product.id in with_variants:
                if not offline:
                    raise _line_error(
                        "variant_required", idx,
                        f"Товар «{product.name}» имеет размеры/цвета. Выберите размер/цвет.",
                    )
                self._variant_missing.add(product.id)
                self._warnings.append({"code": "variant_missing", "index": idx})
            unit = pick_line_unit(it, product, pkg, unit_map, unit_seen, idx)  # ТЗ 26.10 Б-53
            if pkg is None and not offline:  # ТЗ 26.10 Б-52: шаг и минимум (под флагом hardware_2026_11)
                from apps.main.tz26.hw_units.services import check_line_quantity

                check_line_quantity(getattr(self, "request", None), cart.company, product, qty, sale_unit=unit, index=idx)
            default_price = _q2(variant.effective_price) if variant else _q2(default_unit_price_for_package(product, pkg))
            if unit is not None:  # ТЗ 26.10 Б-53: цена единицы продажи
                default_price = effective_unit_price(product, unit, wholesale=bool(getattr(cart, "is_wholesale", False)))
            price = _q2(it["price"]) if it.get("price") is not None else default_price
            wholesale_price = _q2(Decimal(str(getattr(product, "wholesale_price", None) or 0)))
            is_wholesale = bool(
                getattr(cart, "is_wholesale", False)
                or it.get("is_wholesale")
                or (wholesale_price > 0 and price == wholesale_price)
            )
            base = money(price * qty)
            if discount > base:
                raise ValidationError({"items": {idx: "Скидка больше суммы позиции."}})
            if limit_discounts and not (is_wholesale and wholesale_price > 0 and price == wholesale_price) and discount > money(base * Decimal(str(max_dp)) / Decimal("100")):
                exc = ValidationError({"_discount_limit": True})
                exc.index = idx
                raise exc
            if not offline and discount <= 0 and not (is_wholesale and wholesale_price > 0 and price == wholesale_price):
                min_price = _q2(Decimal(str(getattr(product, "purchase_price", None) or 0)))
                if pkg:
                    ipp = Decimal(str(pkg.quantity_in_package or 0))
                    min_price = _q2(min_price / ipp) if ipp > 0 else min_price
                if unit is not None:
                    min_price = _q2(min_price * unit.factor)
                if price < min_price:
                    raise _line_error(
                        "price_below_cost", idx,
                        f"Цена продажи не может быть ниже закупочной ({min_price}).",
                        min_price=str(min_price), price=str(price),
                    )
            if offline and product.kind != Product.Kind.SERVICE:
                try:
                    units = line_qty_consume_units(qty, pkg)
                except ValueError:
                    units = qty
                if unit is not None:
                    units = qty3(qty * unit.factor)
                have_p = Decimal(str(product.quantity or 0))
                demand_p[product.id] += units
                over = demand_p[product.id] > have_p
                if variant is not None:
                    demand_v[variant.id] += units
                    over = over or demand_v[variant.id] > Decimal(str(variant.quantity or 0))
                if over:
                    self._warnings.append({"code": "negative_stock", "index": idx})
            # Б-03 (27.10.2026): цена строки не совпадает с текущей ценой каталога и клиент не
            # сказал, что это сознательно (price_override / скидка / оптовая цена) — запоминаем
            # расхождение. Продажа проходит (оффлайн-досылка со старым каталогом), в ответе
            # будет price_mismatch; со строгим заголовком X-NurMarket-Strict-Price — 409.
            if (
                it.get("price") is not None
                and price != default_price
                and discount <= 0
                and not it.get("price_override")
                and not getattr(self, "_price_override_all", False)
                and not (is_wholesale and wholesale_price > 0 and price == wholesale_price)
            ):
                self._price_mismatch.append({
                    "index": idx,
                    "product_id": str(product.id),
                    "name": product.name,
                    "sent": str(price),
                    "current": str(default_price),
                })
            rows.append(CartItem(
                company_id=cid, branch_id=cart.branch_id, cart=cart, product=product,
                sale_package=pkg, variant=variant, performer=performer, quantity=qty,
                unit_price=price, line_discount=discount, manual_discount=discount,
                price_manually_edited=price != default_price, is_wholesale=is_wholesale,
                sale_unit=unit, base_quantity=(qty3(qty * unit.factor) if unit is not None else None),
            ))
        CartItem.objects.bulk_create(rows)
        cart.recalc()

    def _apply_order_discount(self, cart, data, bonus, limit_discounts, max_dp):
        from apps.main.pos_views import _cart_line_discounts_total, _order_discount_over_limit

        pct = data.get("order_discount_percent")
        total = money(data.get("order_discount_total") or ZERO)
        if pct is not None and pct > 0:
            if limit_discounts and Decimal(str(pct)) > Decimal(str(max_dp)):
                raise ValidationError({"_discount_limit": True})
            base = (cart.subtotal or ZERO) - _cart_line_discounts_total(cart)
            total = money(max(base, ZERO) * Decimal(str(pct)) / Decimal("100"))
        elif total > 0 and limit_discounts and _order_discount_over_limit(cart, total, max_dp):
            raise ValidationError({"_discount_limit": True})

        combined = money(total + bonus + (getattr(self, "_neg_discount", None) or ZERO))
        if combined > 0:
            cart.order_discount_percent = None
            cart.order_discount_total = combined
            cart.save(update_fields=["order_discount_total", "order_discount_percent", "updated_at"])
            cart.recalc()
            applied = (cart.discount_total or ZERO) - _cart_line_discounts_total(cart)
            if money(applied) < combined:
                raise ValidationError({"order_discount_total": "Скидка и бонусы больше суммы чека."})

    def _checkout_data(self, data, cart):
        pay = data["payment"]
        out = {
            "print_receipt": data.get("print_receipt", False),
            "client_id": str(data["client"]) if data.get("client") else None,
            "allow_minus": data.get("allow_minus", False),
            "consultant_id": str(data["consultant_id"]) if data.get("consultant_id") else None,
            "consultant_commission_enabled": data.get("consultant_commission_enabled", False),
            "consultant_commission_percent": data.get("consultant_commission_percent"),
        }
        if pay.get("payments"):
            out["payments"] = pay["payments"]
            if pay.get("received") is not None:
                out["cash_received"] = pay["received"]
            return out
        out["payment_method"] = pay["method"]
        if pay["method"] == Sale.PaymentMethod.MIXED:
            out["cash_amount"] = pay.get("cash_amount")
            out["card_amount"] = pay.get("card_amount")
            if pay.get("card_method"):
                out["card_method"] = pay["card_method"]
            if pay.get("received") is not None:
                out["cash_received"] = pay["received"]
        elif pay["method"] == Sale.PaymentMethod.CASH:
            cart.recalc()
            out["cash_received"] = pay["received"] if pay.get("received") is not None else cart.total
        elif pay["method"] == Sale.PaymentMethod.DEBT:
            out["cash_amount"] = pay.get("cash_amount")
            out["card_amount"] = pay.get("card_amount")
            prepay_m = pay.get("prepayment_method") or data.get("prepayment_method")
            if prepay_m:
                out["prepayment_method"] = prepay_m
            if pay.get("received") is not None:
                out["cash_received"] = pay["received"]
            if out.get("cash_amount") is None and out.get("cash_received") is not None:
                out["cash_amount"] = out["cash_received"]
        elif pay.get("received") is not None:
            # для долга received — предоплата наличными
            out["cash_received"] = pay["received"]
        return out


# ======================================================================
# Бонусы
# ======================================================================

def change_bonus(*, client, delta, reason, sale=None, user=None, note="", idempotency_key=None, branch=None, cashbox=None):
    """Меняет баланс бонусов клиента под блокировкой строки. Баланс не уходит в минус."""
    delta = money(Decimal(str(delta)))
    with transaction.atomic():
        locked = Client.objects.select_for_update().get(pk=client.pk)
        new_balance = money((locked.bonus_balance or ZERO) + delta)
        if new_balance < 0:
            raise ValidationError({"delta": "Недостаточно бонусов у клиента."})
        Client.objects.filter(pk=locked.pk).update(bonus_balance=new_balance)
        client.bonus_balance = new_balance
        return ClientBonusTransaction.objects.create(
            company_id=locked.company_id,
            client=locked,
            sale=sale,
            delta=delta,
            balance_after=new_balance,
            reason=reason,
            note=note[:255],
            idempotency_key=idempotency_key,
            user=user if getattr(user, "is_authenticated", False) else None,
            # ТЗ 29.10 Б-99: филиал/касса — явно, иначе из чека
            branch_id=getattr(branch, "pk", branch) or (sale.branch_id if sale is not None else None),
            cashbox_id=getattr(cashbox, "pk", cashbox) or (sale.cashbox_id if sale is not None else None),
        )


def _bonus_scope(model, raw, company_id):
    """ТЗ 29.10 Б-99: необязательные branch/cashbox в POST …/bonus/ — только своей компании, иначе 400."""
    if not raw:
        return None
    try:
        obj = model.objects.filter(pk=raw).first()
    except (ValueError, DjangoValidationError):
        obj = None
    cid = getattr(obj, "company_id", None) or getattr(getattr(obj, "branch", None), "company_id", None)
    if obj is None or (cid is not None and cid != company_id):
        raise ValidationError({model.__name__.lower(): "Не найдено."})
    return obj


class BonusChangeSerializer(serializers.Serializer):
    delta = MoneyField()
    reason = serializers.ChoiceField(choices=ClientBonusTransaction.Reason.choices)
    sale = serializers.UUIDField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        delta = attrs["delta"]
        if delta == 0:
            raise serializers.ValidationError({"delta": "Изменение не может быть нулевым."})
        if attrs["reason"] == ClientBonusTransaction.Reason.EARN and delta < 0:
            raise serializers.ValidationError({"delta": "Начисление — положительное число."})
        if attrs["reason"] == ClientBonusTransaction.Reason.REDEEM and delta > 0:
            raise serializers.ValidationError({"delta": "Списание — отрицательное число."})
        return attrs


def _bonus_tx_payload(tx):
    return {
        "id": str(tx.id),
        "delta": str(tx.delta),
        "balance_after": str(tx.balance_after),
        "reason": tx.reason,
        "sale": str(tx.sale_id) if tx.sale_id else None,
        "note": tx.note,
        "user": str(tx.user_id) if tx.user_id else None,
        "branch": str(tx.branch_id) if tx.branch_id else None,  # ТЗ 29.10 Б-99
        "cashbox": str(tx.cashbox_id) if tx.cashbox_id else None,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
    }


class _ClientScopedMixin(CompanyBranchRestrictedMixin):
    def _client(self, request, pk, *, for_update=False):
        qs = self._filter_qs_company_branch(Client.objects.all())
        qs = _filter_clients_visible_for_user(qs, request.user)
        if for_update:
            qs = qs.select_for_update()
        return get_object_or_404(qs, pk=pk)


class ClientBonusAPIView(_ClientScopedMixin, APIView):
    """POST /api/main/clients/{id}/bonus/ {"delta": "-100.00", "sale": "…", "reason": "redeem"}"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        client = self._client(request, pk)
        ser = BonusChangeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if data["reason"] == ClientBonusTransaction.Reason.MANUAL and not _is_owner_like(request.user):
            return Response({"detail": "Ручная корректировка — только владелец."}, status=status.HTTP_403_FORBIDDEN)
        sale = None
        if data.get("sale"):
            sale = get_object_or_404(Sale, pk=data["sale"], company_id=client.company_id)
        key = _idempotency_key(request, required=False)
        if key:
            prior = ClientBonusTransaction.objects.filter(company_id=client.company_id, idempotency_key=key).first()
            if prior:
                return Response({"bonus_balance": str(prior.balance_after), "transaction": _bonus_tx_payload(prior)})
        try:
            tx = change_bonus(
                client=client,
                delta=data["delta"],
                reason=data["reason"],
                sale=sale,
                user=request.user,
                note=data.get("note") or "",
                idempotency_key=key,
                branch=_bonus_scope(Branch, request.data.get("branch"), client.company_id),
                cashbox=_bonus_scope(Cashbox, request.data.get("cashbox"), client.company_id),
            )
        except IntegrityError:
            prior = ClientBonusTransaction.objects.get(company_id=client.company_id, idempotency_key=key)
            return Response({"bonus_balance": str(prior.balance_after), "transaction": _bonus_tx_payload(prior)})
        return Response(
            {"bonus_balance": str(tx.balance_after), "transaction": _bonus_tx_payload(tx)},
            status=status.HTTP_201_CREATED,
        )


class ClientBonusHistoryAPIView(_ClientScopedMixin, APIView):
    """GET /api/main/clients/{id}/bonus/history/"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        client = self._client(request, pk)
        txs = ClientBonusTransaction.objects.filter(client=client)
        # ТЗ 29.10 Б-99: фильтры reason, date_from/date_to (дни по Бишкеку)
        from apps.main.tz1029_bonus import apply_bonus_filters
        txs = apply_bonus_filters(txs, request.query_params).order_by("-created_at")[:500]
        return Response(
            {"bonus_balance": str(client.bonus_balance), "results": [_bonus_tx_payload(t) for t in txs]}
        )


# ======================================================================
# Должники и погашение долга
# ======================================================================

def _open_debts(company_ids, clients_qs):
    """
    Непогашенные долги: (client_id, sale_id|None, remaining, created_at).

    Источник — долговые сделки (ClientDeal kind=debt) с остатком; сделка по чеку,
    оплаченному целиком через pos/sales/{id}/pay-debt/, не считается. Долговые
    чеки без сделки считаются как total - cash_received.
    """
    money_field = DecimalField(max_digits=14, decimal_places=2)
    paid_sq = (
        DealInstallment.objects.filter(deal=OuterRef("pk"))
        .values("deal")
        .annotate(s=Sum("paid_amount"))
        .values("s")
    )
    deals = (
        ClientDeal.objects.filter(
            company_id__in=company_ids, kind=ClientDeal.Kind.DEBT, client__in=clients_qs
        )
        .exclude(sale__status=Sale.Status.PAID)
        .annotate(paid=Coalesce(Subquery(paid_sq, output_field=money_field), Value(ZERO), output_field=money_field))
        .values("client_id", "sale_id", "amount", "prepayment", "paid", "created_at")
    )
    rows = []
    for d in deals:
        remaining = money((d["amount"] or ZERO) - (d["prepayment"] or ZERO) - (d["paid"] or ZERO))
        if remaining > 0:
            rows.append((d["client_id"], d["sale_id"], remaining, d["created_at"]))

    orphan_sales = (
        Sale.objects.filter(
            company_id__in=company_ids, status=Sale.Status.DEBT, client__in=clients_qs, deals__isnull=True
        )
        .values("client_id", "id", "total", "cash_received", "created_at")
    )
    for s in orphan_sales:
        remaining = money((s["total"] or ZERO) - (s["cash_received"] or ZERO))
        if remaining > 0:
            rows.append((s["client_id"], s["id"], remaining, s["created_at"]))
    return rows


class ClientDebtorsAPIView(_ClientScopedMixin, APIView):
    """
    GET /api/main/clients/debtors/?min_amount=0&ordering=-debt_total
    → [{client, full_name, phone, debt_total, sales_count, oldest_debt_at,
        manual_debts_total, overdue_amount, last_payment_at}]

    ТЗ Н-14:
    - ?include=sales — в каждую строку `sales:[{sale, number, created_at, total, debt_remaining,
      due_date, deal_id}]` (долговые чеки, старые первыми); ручные долги без чека (clients/{id}/debts/)
      в `sales` не входят, их сумма — `manual_debts_total`, а в `debt_total` они уже учтены;
    - ?page= / ?page_size= — ответ `{count, next, previous, results}`; без них голый массив;
    - поставщики по умолчанию не выводятся (?include_suppliers=1 вернёт их).
    """

    permission_classes = [permissions.IsAuthenticated]
    ORDERINGS = {"debt_total", "-debt_total", "oldest_debt_at", "-oldest_debt_at", "full_name", "-full_name"}

    def get(self, request, *args, **kwargs):
        try:
            min_amount = Decimal(str(request.query_params.get("min_amount") or "0"))
        except Exception:
            raise ValidationError({"min_amount": "Число."})
        ordering = request.query_params.get("ordering") or "-debt_total"
        if ordering not in self.ORDERINGS:
            raise ValidationError({"ordering": f"Допустимо: {', '.join(sorted(self.ORDERINGS))}."})

        clients_qs = _filter_clients_visible_for_user(
            self._filter_qs_company_branch(Client.objects.all()), request.user
        )
        company = _company(request)
        # ТЗ Н-14: долг поставщику — это наш долг, а не должник магазина
        if request.query_params.get("include_suppliers") not in ("1", "true", "True"):
            clients_qs = clients_qs.exclude(type=Client.StatusClient.SUPPLIERS)
        agg = defaultdict(lambda: {"debt_total": ZERO, "sales_count": 0, "oldest": None, "manual": ZERO})
        for client_id, _sale_id, remaining, created_at in _open_debts([company.id], clients_qs.values("id")):
            a = agg[client_id]
            a["debt_total"] += remaining
            a["sales_count"] += 1
            if _sale_id is None:
                a["manual"] += remaining
            if a["oldest"] is None or created_at < a["oldest"]:
                a["oldest"] = created_at

        clients = {c.id: c for c in Client.objects.filter(id__in=list(agg)).only("id", "full_name", "phone", "telegram_chat_id")}
        extra = _debtor_extras(list(agg))
        out = []
        for cid, a in agg.items():
            if a["debt_total"] < min_amount or cid not in clients:
                continue
            c = clients[cid]
            out.append({
                "client": str(cid),
                "full_name": c.full_name,
                "phone": c.phone,
                "telegram_chat_id": c.telegram_chat_id,
                "debt_total": str(money(a["debt_total"])),
                "sales_count": a["sales_count"],
                "oldest_debt_at": a["oldest"].date().isoformat() if a["oldest"] else None,
                "manual_debts_total": str(money(a["manual"])),
                "overdue_amount": str(money(extra["overdue"].get(cid, ZERO))),
                "last_payment_at": extra["last_payment"].get(cid),
            })
        field = ordering.lstrip("-")
        keyfn = {
            "debt_total": lambda r: Decimal(r["debt_total"]),
            "oldest_debt_at": lambda r: r["oldest_debt_at"] or "",
            "full_name": lambda r: (r["full_name"] or "").lower(),
        }[field]
        out.sort(key=lambda r: (keyfn(r), r["client"]), reverse=ordering.startswith("-"))

        qp = request.query_params
        include_sales = "sales" in [x.strip() for x in (qp.get("include") or "").split(",")]
        paged = "page" in qp or "page_size" in qp
        page_meta = None
        if paged:
            out, page_meta = _paginate_list(request, out)
        if include_sales:
            by_client = _debtor_sales([uuid.UUID(r["client"]) for r in out], company.id)
            for r in out:
                r["sales"] = by_client.get(uuid.UUID(r["client"]), [])
        if "objects" in [x.strip() for x in (qp.get("include") or "").split(",")]:
            hw_attach_debtor_objects(out, company.id)  # ТЗ 26.10 Б-56: долг по объектам
        if page_meta is not None:
            return Response({**page_meta, "results": out})
        return Response(out)


def _paginate_list(request, rows, default_size=100, max_size=1000):
    """ТЗ Н-14: страница из готового списка → (строки, {count, next, previous})."""
    from rest_framework.utils.urls import replace_query_param

    qp = request.query_params

    def _pos_int(name, default, cap=None):
        raw = qp.get(name)
        if raw in (None, ""):
            return default
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise ValidationError({name: "Целое число."})
        if v < 1:
            raise ValidationError({name: "Должно быть больше нуля."})
        return min(v, cap) if cap else v

    size = _pos_int("page_size", default_size, max_size)
    page = _pos_int("page", 1)
    count = len(rows)
    url = request.build_absolute_uri()
    meta = {
        "count": count,
        "next": replace_query_param(url, "page", page + 1) if page * size < count else None,
        "previous": replace_query_param(url, "page", page - 1) if page > 1 else None,
    }
    return rows[(page - 1) * size: page * size], meta


def _debtor_extras(client_ids):
    """ТЗ Н-14: {overdue: {client: сумма просрочки}, last_payment: {client: ISO-время последней оплаты}}."""
    from django.db.models import F, Max

    today = timezone.localdate()
    overdue = {}
    rows = (
        DealInstallment.objects.filter(
            deal__client_id__in=client_ids, deal__kind=ClientDeal.Kind.DEBT,
            due_date__lt=today, paid_amount__lt=F("amount"),
        )
        .exclude(deal__sale__status=Sale.Status.PAID)
        .values("deal__client_id")
        .annotate(s=Sum(F("amount") - F("paid_amount")))
    )
    for r in rows:
        overdue[r["deal__client_id"]] = money(r["s"] or ZERO)
    last_payment = {}
    for r in (
        DealPayment.objects.filter(
            deal__client_id__in=client_ids, deal__kind=ClientDeal.Kind.DEBT, kind=DealPayment.Kind.PAY
        )
        .values("deal__client_id")
        .annotate(m=Max("created_at"))
    ):
        last_payment[r["deal__client_id"]] = r["m"].isoformat() if r["m"] else None
    return {"overdue": overdue, "last_payment": last_payment}


def _debtor_sales(client_ids, company_id):
    """
    ТЗ Н-14: открытые долговые чеки по клиентам, старые первыми:
    {client: [{sale, number, created_at, total, debt_remaining, due_date, deal_id}]}.
    Правила остатка те же, что в _open_debts; ручные долги (без чека) не включаются.
    """
    from django.db.models import F, Min

    money_field = DecimalField(max_digits=14, decimal_places=2)
    paid_sq = (
        DealInstallment.objects.filter(deal=OuterRef("pk"))
        .values("deal").annotate(s=Sum("paid_amount")).values("s")
    )
    due_sq = (
        DealInstallment.objects.filter(deal=OuterRef("pk"), paid_amount__lt=F("amount"))
        .values("deal").annotate(m=Min("due_date")).values("m")
    )
    deals = (
        ClientDeal.objects.filter(
            company_id=company_id, kind=ClientDeal.Kind.DEBT, client_id__in=client_ids, sale__isnull=False
        )
        .exclude(sale__status=Sale.Status.PAID)
        .select_related("sale")
        .annotate(
            paid=Coalesce(Subquery(paid_sq, output_field=money_field), Value(ZERO), output_field=money_field),
            next_due=Subquery(due_sq),
        )
    )
    out = defaultdict(list)
    for d in deals:
        remaining = money((d.amount or ZERO) - (d.prepayment or ZERO) - (d.paid or ZERO))
        if remaining <= 0:
            continue
        sale = d.sale
        out[d.client_id].append((sale.created_at, {
            "sale": str(sale.id),
            "number": sale.doc_number,
            "created_at": sale.created_at.isoformat() if sale.created_at else None,
            "total": str(money(sale.total or ZERO)),
            "debt_remaining": str(remaining),
            "due_date": d.next_due.isoformat() if d.next_due else None,
            "deal_id": str(d.id),
        }))
    orphans = Sale.objects.filter(
        company_id=company_id, status=Sale.Status.DEBT, client_id__in=client_ids, deals__isnull=True
    )
    for sale in orphans:
        remaining = money((sale.total or ZERO) - (sale.cash_received or ZERO))
        if remaining <= 0:
            continue
        out[sale.client_id].append((sale.created_at, {
            "sale": str(sale.id),
            "number": sale.doc_number,
            "created_at": sale.created_at.isoformat() if sale.created_at else None,
            "total": str(money(sale.total or ZERO)),
            "debt_remaining": str(remaining),
            "due_date": None,
            "deal_id": None,
        }))
    return {cid: [row for _, row in sorted(rows, key=lambda x: (x[0], x[1]["sale"]))] for cid, rows in out.items()}


class ClientPayDebtSerializer(serializers.Serializer):
    amount = MoneyField()
    method = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    # ТЗ Н-13: то же поле под именем из pos/sales/{id}/pay-debt/
    payment_method = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    shift = serializers.UUIDField(required=False, allow_null=True)
    cashbox = serializers.UUIDField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True)


class ClientPayDebtAPIView(APIView):
    """
    POST /api/main/clients/{id}/pay-debt/
    Idempotency-Key: …
    {"amount": "5000.00", "method": "cash", "shift": "…"}
    → {"paid", "left", "applied": [{"sale", "deal", "amount"}], "method", "payment_method",
       "received_by": {"id", "name"}, "received_at", "shift", "cashbox"}

    ТЗ Н-13: способ оплаты — `method` или `payment_method` (разные значения → 400 field_conflict).
    ТЗ 29.10 Б-13: без `shift` владелец/админ принимает деньги «в руки» — движение type:income,
    source_kind:debt_repayment, shift:null, касса по умолчанию «Основная касса компании», в смену не попадает.
    Кассир без `shift` — в свою открытую смену как раньше (новый клиент без открытой смены: 400 shift_required).
    Коды: no_debt, amount_exceeds_debt (data.max), amount_invalid, shift_required, shift_closed,
    shift_not_found, shift_not_owned (403), method_not_allowed (data.allowed, новый клиент), cashbox_not_found.

    Гасит от старых долгов к новым (по сроку взноса), как clients/{id}/deals/pay-any/.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        from apps.main.views import ClientDealsPayAnyAPIView

        key = _idempotency_key(request)
        ser = ClientPayDebtSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        try:
            idem = uuid.UUID(key)
        except ValueError:
            idem = uuid.uuid5(uuid.NAMESPACE_URL, f"nurcrm-pay-debt:{pk}:{key}")

        from apps.main.services_debt import (
            VALID_DEBT_PAYMENT_METHODS, normalize_debt_payment_method, pick_debt_method,
        )

        raw_pm = pick_debt_method(data)
        if raw_pm == "card":
            raw_pm = "transfer"
        if raw_pm and raw_pm not in VALID_DEBT_PAYMENT_METHODS and is_strict(request, None):
            # ТЗ 29.10 Б-13: новый клиент получает код вместо тихой замены на transfer
            raise ApiError(
                "method_not_allowed", "Неизвестный способ оплаты.",
                data={"allowed": sorted(VALID_DEBT_PAYMENT_METHODS)},
                top={"payment_method": [f"Допустимо: {', '.join(sorted(VALID_DEBT_PAYMENT_METHODS))}."]},
            )
        pm = normalize_debt_payment_method(raw_pm)
        payload = {
            "amount": str(data["amount"]),
            "idempotency_key": str(idem),
            "payment_method": pm,
            "note": data.get("note") or "",
        }
        if data.get("shift"):
            payload["shift_id"] = str(data["shift"])
        if data.get("cashbox"):
            payload["cashbox_id"] = str(data["cashbox"])

        view = ClientDealsPayAnyAPIView()
        view.request = request
        view.args, view.kwargs = (), {"client_id": pk}
        view.format_kwarg = None
        with transaction.atomic():
            resp = view.pay(request, pk, payload)
            if resp.status_code >= 400:
                transaction.set_rollback(True)
                return resp
            pairs = list(getattr(view, "applied", []))
            if not pairs and getattr(view, "replayed", False):
                # ТЗ 29.10 Б-13 п.6: повтор показывает то, что было применено в первый раз
                merged = {}
                for p_ in getattr(view, "replay_payments", []):
                    d_ = merged.setdefault(p_.deal_id, [p_.deal, Decimal("0.00")])
                    d_[1] += p_.amount
                pairs = [(d, a) for d, a in merged.values()]
            applied = []
            for deal, amount in pairs:
                # остаток долга по этой продаже после платежа и номер чека (ТЗ 29.10 Б-13 п.3)
                fresh = ClientDeal.objects.select_related("sale").prefetch_related("installments").get(pk=deal.pk)
                applied.append({
                    "sale": str(deal.sale_id) if deal.sale_id else None,
                    "deal": str(deal.id),
                    "amount": str(money(amount)),
                    "sale_number": fresh.sale.doc_number if fresh.sale_id else None,
                    "sale_debt_left": str(money(max(ZERO, fresh.remaining_debt))),
                })
        client = get_object_or_404(Client, pk=pk)
        left = sum(
            (r[2] for r in _open_debts([client.company_id], Client.objects.filter(pk=pk).values("id"))),
            ZERO,
        )
        # ТЗ Н-13: кто принял, когда, в какой смене и кассе
        from apps.main.services_debt import debt_receipt_info

        info = debt_receipt_info(
            request.user, getattr(view, "cashflows", []),
            getattr(view, "payments_made", None) or getattr(view, "replay_payments", []),
        )
        return Response(
            {
                "paid": resp.data.get("paid_total"),
                "left": str(money(left)),
                "applied": applied,
                "replayed": bool(getattr(view, "replayed", False)),
                "method": pm,
                "payment_method": pm,
                **info,
            },
            status=status.HTTP_200_OK,
        )


# ======================================================================
# Возвраты
# ======================================================================

def _return_item(line: dict, ret) -> dict:
    """Строка возврата (AN-08)."""
    total = line.get("amount") or line.get("total")
    qty = line.get("quantity") or line.get("qty") or "1"
    price = line.get("price")

    if total is None and price is not None and qty:
        try:
            total = str((Decimal(str(price)) * Decimal(str(qty))).quantize(Decimal("0.01")))
        except Exception:
            pass

    if total is None and ret and getattr(ret, "returned_amount", None):
        items_list = getattr(ret, "returned_items", None) or []
        n = len(items_list)
        if n == 1:
            total = str(ret.returned_amount)
        elif n > 1:
            try:
                per_item = (ret.returned_amount / Decimal(n)).quantize(Decimal("0.01"))
                if line == items_list[-1]:
                    total = str(ret.returned_amount - (per_item * Decimal(n - 1)))
                else:
                    total = str(per_item)
            except Exception:
                pass

    if price is None and total is not None and qty:
        try:
            qd = Decimal(str(qty))
            if qd > 0:
                price = str((Decimal(str(total)) / qd).quantize(Decimal("0.01")))
        except Exception:
            price = None
    prod_id = line.get("product") or line.get("product_id")
    return {
        "return_item_id": line.get("return_item_id") or line.get("id"),
        "return_id": str(ret.id),
        "sale_item": line.get("sale_item"),
        "product": prod_id,
        "product_id": prod_id,
        "variant": line.get("variant"),
        "name": line.get("name"),
        "qty": qty,
        "quantity": qty,
        "price": price,
        "discount": line.get("discount"),
        "total": total,
        "amount": total,
        "reason": line.get("reason", "defect" if ret.is_defect else (ret.reason or None)),
        "restock": line.get("restock", not ret.is_defect),
    }


class SaleReturnListAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/pos/returns/?date_from=2026-09-01&date_to=2026-09-28&shift=…&sale=…
    AN-08: каждый возврат содержит items с позициями, original_sale_id, employee_id, customer_id, warehouse_id.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        from django.utils.dateparse import parse_date

        company = _company(request)
        qs = (
            SaleReturn.objects.filter(company=company)
            .select_related("sale", "user", "sale__client", "origin")
            .prefetch_related("sale__items__product")
        )
        branch = self._auto_branch()
        if branch is not None:
            qs = qs.filter(sale__branch=branch)
        qp = request.query_params
        for param, lookup in (("date_from", "created_at__date__gte"), ("date_to", "created_at__date__lte")):
            if qp.get(param):
                d = parse_date(qp[param])
                if d is None:
                    raise ValidationError({param: "Формат YYYY-MM-DD."})
                qs = qs.filter(**{lookup: d})
        for param, field in (("shift", "shift_id"), ("sale", "sale_id"), ("cashier", "user_id")):
            if qp.get(param):
                try:
                    qs = qs.filter(**{field: uuid.UUID(qp[param])})
                except ValueError:
                    raise ValidationError({param: "Некорректный UUID."})
        # id — стабильный порядок при равном created_at (страницы не пересекаются)
        qs = qs.order_by("-created_at", "-id")
        # ТЗ Н-07: страницы только если клиент передал page/page_size; иначе — голый массив
        # (кассы в поле разбирают массив, их ломать нельзя).
        paged = "page" in qp or "page_size" in qp
        page_meta = None
        if paged:
            from rest_framework.utils.urls import replace_query_param

            def _pos_int(name, default, cap=None):
                raw = qp.get(name)
                if raw in (None, ""):
                    return default
                try:
                    v = int(raw)
                except (TypeError, ValueError):
                    raise ValidationError({name: "Целое число."})
                if v < 1:
                    raise ValidationError({name: "Должно быть больше нуля."})
                return min(v, cap) if cap else v

            page_size = _pos_int("page_size", 100, 1000)
            page = _pos_int("page", 1)
            count = qs.count()
            qs = qs[(page - 1) * page_size: page * page_size]
            url = request.build_absolute_uri()
            page_meta = {
                "count": count,
                "next": replace_query_param(url, "page", page + 1) if page * page_size < count else None,
                "previous": replace_query_param(url, "page", page - 1) if page > 1 else None,
            }
        else:
            qs = qs[:1000]

        out = []
        for r in qs:
            items = r.returned_items or []
            if not items and r.sale:
                # AN-08: для старых возвратов позиции восстанавливаются из исходной продажи
                items = [
                    {
                        "return_item_id": str(it.id),
                        "sale_item": str(it.id),
                        "product": str(it.product_id) if it.product_id else None,
                        "product_id": str(it.product_id) if it.product_id else None,
                        "name": it.name_snapshot or (it.product.name if it.product else "Товар"),
                        "quantity": str(it.quantity),
                        "qty": str(it.quantity),
                        "price": str(it.unit_price),
                        "amount": str(it.amount),
                        "total": str(it.amount),
                    }
                    for it in r.sale.items.all()
                ]
            out.append({
                "id": str(r.id),
                "original_sale_id": str(r.sale_id),
                "sale": str(r.sale_id),
                "sale_number": r.sale.doc_number if r.sale else None,
                "warehouse_id": str(r.sale.branch_id) if (r.sale and r.sale.branch_id) else None,
                "employee_id": str(r.user_id) if r.user_id else None,
                "customer_id": str(r.sale.client_id) if (r.sale and r.sale.client_id) else None,
                "amount": str(money(r.returned_amount or ZERO)),
                "total": str(money(r.returned_amount or ZERO)),
                "refund_method": r.refund_method or (r.sale.payment_method if r.sale else None),
                "is_full": r.is_full,
                "is_defect": r.is_defect,
                "items": [_return_item(i, r) for i in items],
                "reason": r.reason,
                "cashier": str(r.user_id) if r.user_id else None,
                "shift": str(r.shift_id) if r.shift_id else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                **return_origin_fields(r),  # Б-67
            })
        if page_meta is not None:
            return Response({**page_meta, "results": out})
        return Response(out)


class ClientDirectDebtAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/clients/{id}/debts/
    { "amount": "7000.00", "comment": "...", "due_date": "2026-10-30" }
    AN-03: создание прямого документа «долг» клиента без оформления фиктивной продажи.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, client_id, *args, **kwargs):
        from apps.main.models import Client, ClientDeal, DealInstallment
        from django.utils.dateparse import parse_date

        company = _company(request)
        branch = self._auto_branch()

        client_qs = Client.objects.filter(company=company)
        client = get_object_or_404(client_qs, id=client_id)

        raw_amount = request.data.get("amount")
        if raw_amount is None:
            raise ValidationError({"amount": "Укажите сумму долга."})
        try:
            amount = money(Decimal(str(raw_amount)))
        except Exception:
            raise ValidationError({"amount": "Некорректная сумма долга."})
        if amount <= 0:
            raise ValidationError({"amount": "Сумма долга должна быть больше 0."})

        comment = (request.data.get("comment") or "").strip()
        due_date_raw = request.data.get("due_date")
        due_date = parse_date(str(due_date_raw)) if due_date_raw else hw_default_due_date(request, client)
        if due_date is None:
            raise ValidationError({"due_date": "Ожидается дата YYYY-MM-DD."})

        with transaction.atomic():
            hw_check_credit_limit(request, client, amount)  # ТЗ 26.10 Б-56: 409 credit_limit_exceeded (под флагом)
            # ТЗ ч.12, 3.2: долг без продажи — сделка с одним взносом, без выручки
            deal = ClientDeal.objects.create(
                company=company,
                branch=branch or client.branch,
                client=client,
                kind=ClientDeal.Kind.DEBT,
                amount=amount,
                prepayment=Decimal("0.00"),
                note=comment,
                title=(comment or f"Долг без продажи: {amount}")[:255],
                debt_days=1,
                first_due_date=due_date,
            )
            hw_link_direct_debt(request, client, deal)  # ТЗ 26.10 Б-56: object

        return Response(
            {
                "id": str(deal.id),
                "client": str(client.id),
                "client_name": client.full_name,
                "amount": str(amount),
                "prepayment": "0.00",
                "debt_remaining": str(amount),
                "comment": comment,
                "due_date": due_date.isoformat() if due_date else None,
                "created_at": deal.created_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )
