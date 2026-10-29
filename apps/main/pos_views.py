from apps.main.services_debt import debt_detached_client as _debt_detached_client  # ТЗ 29.10 Б-13
from apps.users.pin import pin_guard
from apps.integrations.idempotency import idempotent
from rest_framework import generics, status, permissions, filters
from rest_framework.views import APIView
from rest_framework.response import Response
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime, parse_date
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.pagination import _positive_int

from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from django.http import FileResponse, Http404
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from django.core.exceptions import ValidationError as DjangoValidationError

from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_HALF_UP,
    InvalidOperation,
)
from datetime import timedelta, datetime, date, time as dtime
import io, os, uuid, logging
from django.db import IntegrityError

from django.db.models import Q, F, Value as V, Sum, Prefetch, Count, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils.timezone import is_aware, make_aware, get_current_timezone

from typing import List, Optional, Dict
from dataclasses import dataclass

from django.core.cache import cache
from django.conf import settings
import requests
import qrcode

from apps.users.models import (
    Roles,
    User,
    Company,
    SCALE_BARCODE_MODE_AUTO,
    SCALE_BARCODE_MODE_WEIGHT,
    SCALE_BARCODE_MODE_AMOUNT,
    SCALE_BARCODE_LAYOUT_PLU,
    SCALE_BARCODE_LAYOUT_CODE,
    SCALE_BARCODE_AMOUNT_UNIT_TIYIN,
    SCALE_BARCODE_AMOUNT_UNIT_SOM,
)
from apps.main.models import (
    Cart,
    CartItem,
    CartItemDeletionLog,
    Sale,
    SaleItem,
    SalePayment,
    Product,
    ProductPackage,
    ProductPromotionTier,
    ProductAlternateBarcode,
    MobileScannerToken,
    Client,
    ProductImage,
    ClientDeal,
    DealInstallment,
    Debt,
    PosPrinterSetting,
    MarketCustomerDisplaySettings,
)
from apps.main.models import ManufactureSubreal, AgentSaleAllocation, ReturnFromAgent
from apps.main.models import cart_line_base, scale_amount_step
from apps.main.models import ProductVariant
from apps.main.cache_utils import invalidate_cache_pattern
from apps.main.services import checkout_cart, NotEnoughStock
from apps.main.cart_service import abandon_cart
from apps.main.tz26.hw_prices.services import (
    after_checkout as hw_after_checkout,
    reprice_for_request as hw_reprice_for_request,
)
from apps.ekassa.runtime import schedule_after_commit
from apps.ekassa.shift_bridge import sync_ekassa_after_local_shift_open_by_id


def _ekassa_checkout_hint(company):
    """Если у компании включена eKassa — клиенту можно показать, что чек уходит в ОФД."""
    from apps.ekassa.services import get_integration

    cfg = get_integration(company)
    if cfg and cfg.is_ready():
        return {"queued": True}
    return None
from apps.main.services_agent_pos import checkout_agent_cart, AgentNotEnoughStock
from apps.main.notifications_market import notify_sale_created
from apps.main.utils_numbers import ensure_sale_doc_number
from apps.main.views import CompanyBranchRestrictedMixin, SupplierReceiptLimitPagination
from apps.construction.models import Cashbox, CashShift
from .pos_utils import (
    money,
    qty3,
    q2,
    fmt_money,
    fmt,
    to_decimal,
    as_decimal,
    _q2,
    Q2,
    Q3,
    line_qty_consume_units,
    default_unit_price_for_package,
    total_cart_consume_packs_for_product,
    log_cart_item_deletion,
)

from .pos_serializers import (
    SaleCartSerializer,
    SaleItemSerializer,
    ScanRequestSerializer,
    AddItemSerializer,
    CartItemPatchSerializer,
    CheckoutSerializer,
    PayDebtSerializer,
    MobileScannerTokenSerializer,
    SaleListSerializer,
    SaleDetailSerializer,
    StartCartOptionsSerializer,
    CustomCartItemCreateSerializer,
    SaleStatusUpdateSerializer,
    ReceiptSerializer,
    AgentCheckoutSerializer,
    _is_owner_like,
    CartItemDeletionLogSerializer,
)

try:
    from apps.main.models import ClientDeal, DealInstallment
except Exception:
    ClientDeal = None
    DealInstallment = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(BASE_DIR, "fonts")

try:
    pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(FONTS_DIR, "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(FONTS_DIR, "DejaVuSans-Bold.ttf")))
except Exception:
    # если шрифтов нет в окружении — PDF всё равно сгенерится на Helvetica
    pass


def register_fonts_if_needed():
    try:
        pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", "DejaVuSans-Bold.ttf"))
    except Exception:
        pass


register_fonts_if_needed()

class MarketCashierOnlyMixin:
    """
    Ограничение POS/кассирского функционала:
    - проверка доступа выполняется на фронте
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        return


def _set_font(p, name: str, size: int, fallback: str = "Helvetica"):
    """
    Безопасно ставит шрифт. Если кастомного нет — падаем на fallback,
    но PDF продолжаем генерировать.
    """
    try:
        p.setFont(name, size)
    except Exception:
        p.setFont(fallback, size)


# Алиасы для обратной совместимости (используются в коде)
_to_decimal = to_decimal
_as_decimal = as_decimal


def _is_market_company(company: Company) -> bool:
    try:
        return bool(company and getattr(company, "is_market", None) and company.is_market())
    except Exception:
        return False


def _company_can_use_cashier(company: Company) -> bool:
    try:
        return bool(company and getattr(company, "can_use_cashier", None) and company.can_use_cashier())
    except Exception:
        return False


def _build_physical_receipt_text(sale, *, payment_method=None, cash_received=None, change=None, include_shift=True) -> str:
    from apps.main.receipt_header import receipt_vendor_header

    ensure_sale_doc_number(sale)

    created_at = timezone.localtime(sale.created_at) if getattr(sale, "created_at", None) else timezone.localtime()
    vh = receipt_vendor_header(sale)
    company_name = vh.get("brand") or "Компания"
    from apps.main.printers import _user_display_name
    cashier_name = _user_display_name(getattr(sale, "user", None)) or ""

    payment_method_value = payment_method or getattr(sale, "payment_method", None)
    payment_label = None
    try:
        payment_label = sale.get_payment_method_display()
    except Exception:
        payment_label = payment_method_value or ""

    cash_received_value = getattr(sale, "cash_received", None)
    if cash_received_value in (None, ""):
        cash_received_value = cash_received if cash_received is not None else Decimal("0.00")

    change_value = getattr(sale, "change", None)
    if change_value in (None, ""):
        change_value = change if change is not None else Decimal("0.00")

    lines = [company_name]
    if vh.get("inn"):
        lines.append(f"ИНН {vh['inn']}")
    if vh.get("address"):
        lines.append(vh["address"])
    lines.extend(
        [
            f"Чек № {getattr(sale, 'doc_no', None) or sale.id}",
            created_at.strftime("%d.%m.%Y %H:%M"),
        ]
    )
    if cashier_name:
        lines.append(f"Кассир: {cashier_name}")
    if getattr(sale, "consultant", None):
        cons_name = (
            getattr(sale.consultant, "get_full_name", lambda: "")()
            or getattr(sale.consultant, "full_name", None)
            or getattr(sale.consultant, "username", None)
            or ""
        )
        if cons_name:
            lines.append(f"Консультант: {cons_name}")
    if include_shift and getattr(sale, "shift_id", None):
        lines.append(f"Смена: {sale.shift_id}")
    if getattr(sale, "cashbox_id", None):
        lines.append(f"Касса: {sale.cashbox_id}")
    if getattr(sale, "client", None):
        client_name = (
            getattr(sale.client, "full_name", None)
            or getattr(sale.client, "llc", None)
            or getattr(sale.client, "enterprise", None)
            or ""
        )
        if client_name:
            lines.append(f"Клиент: {client_name}")

    lines.extend([
        "-" * 32,
        "Товары",
    ])

    for idx, it in enumerate(sale.items.all(), start=1):
        item_name = (getattr(it, "name_snapshot", None) or getattr(it, "custom_name", None) or "Товар").strip()
        qty = fmt(getattr(it, "quantity", 0))
        unit_price = fmt_money(getattr(it, "unit_price", 0))
        line_base = (getattr(it, "unit_price", 0) or 0) * (getattr(it, "quantity", 0) or 0)
        line_disc = getattr(it, "line_discount", None) or 0
        line_total = fmt_money(line_base - line_disc)
        lines.append(f"{idx}. {item_name}")
        if Decimal(str(line_disc or 0)) > 0:
            lines.append(f"   Скидка: {fmt_money(line_disc)}")
        lines.append(f"   {qty} x {unit_price} = {line_total}")

    lines.extend([
        "-" * 32,
        f"Сумма: {fmt_money(sale.subtotal)}",
    ])
    if sale.discount_total and sale.discount_total > 0:
        lines.append(f"Скидка: {fmt_money(sale.discount_total)}")
    if sale.tax_total and sale.tax_total > 0:
        lines.append(f"Налог: {fmt_money(sale.tax_total)}")
    lines.append(f"Итого: {fmt_money(sale.total)}")

    payment_lines = sale.payment_lines() if hasattr(sale, "payment_lines") else []
    if payment_lines:
        if len(payment_lines) > 1:
            lines.append("Оплата:")
            for line in payment_lines:
                try:
                    label = line.get_method_display()
                except Exception:
                    label = line.method
                lines.append(f"  {label}: {fmt_money(line.amount)}")
        else:
            line = payment_lines[0]
            try:
                payment_label = line.get_method_display()
            except Exception:
                payment_label = line.method
            if line.method == Sale.PaymentMethod.CASH:
                lines.append("Оплата: Наличные")
            elif line.method == Sale.PaymentMethod.DEBT:
                lines.append("Оплата: В долг")
            else:
                lines.append(f"Оплата: {payment_label}")

        cash_portion = sale.cash_payment_amount() if hasattr(sale, "cash_payment_amount") else Decimal("0.00")
        if cash_portion > 0:
            cash_received_value = getattr(sale, "cash_received", None)
            if cash_received_value in (None, ""):
                cash_received_value = cash_received if cash_received is not None else cash_portion
            change_value = getattr(sale, "change", None)
            if change_value in (None, ""):
                change_value = change if change is not None else Decimal("0.00")
            lines.append(f"Получено наличными: {fmt_money(cash_received_value)}")
            if Decimal(str(change_value or 0)) > 0:
                lines.append(f"Сдача: {fmt_money(change_value)}")
    elif payment_method_value == Sale.PaymentMethod.CASH:
        lines.append("Оплата: Наличные")
        lines.append(f"Получено: {fmt_money(cash_received_value)}")
        lines.append(f"Сдача: {fmt_money(change_value)}")
    elif payment_method_value == Sale.PaymentMethod.DEBT:
        lines.append("Оплата: В долг")
    elif payment_label:
        lines.append(f"Оплата: {payment_label}")

    lines.append("-" * 32)
    lines.append("Спасибо за покупку")

    # eKassa (если фискализация уже выполнена и данные сохранены в Sale.ekassa_fiscal)
    try:
        meta = getattr(sale, "ekassa_fiscal", None) or {}
    except Exception:
        meta = {}
    if isinstance(meta, dict) and meta:
        lines.append("-" * 32)
        lines.append("eKassa")
        status = meta.get("status")
        if status is not None:
            lines.append(f"status: {status}")
        # Явные реквизиты (как в «Дубликат»)
        kkm_reg = meta.get("kkm_reg_number")
        fm_number = meta.get("fm_number")
        fd_number = meta.get("fd_number") if meta.get("fd_number") is not None else meta.get("fields", {}).get("1040")
        fpd = meta.get("fpd")
        if kkm_reg:
            lines.append(f"РН ККМ\t{kkm_reg}")
        if fm_number:
            lines.append(f"ФМ\t{fm_number}")
        if fd_number is not None:
            lines.append(f"ФД\t{fd_number}")
        if fpd:
            lines.append(f"ФПД\t{fpd}")
        if meta.get("ekassa_receipt_id") is not None:
            lines.append(f"receipt_id: {meta.get('ekassa_receipt_id')}")
        # Данные для QR (ссылка из ответа eKassa)
        link = meta.get("link")
        if link:
            lines.append(f"QR\t{link}")
        fields = meta.get("fields")
        if isinstance(fields, dict) and fields:
            lines.append("fields:")
            for k in sorted(fields.keys(), key=lambda x: str(x)):
                v = fields.get(k)
                if v is None or v == "":
                    continue
                lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def _truthy_query_param(val) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


def _cart_queryset_for_response():
    image_qs = ProductImage.objects.only("id", "product_id", "image", "alt", "is_primary").order_by("id")
    promo_tier_qs = ProductPromotionTier.objects.only(
        "id", "product_id", "position", "min_amount", "discount_percent", "promo_quantity"
    ).order_by("position", "id")
    item_qs = (
        CartItem.objects.select_related("product", "variant", "sale_unit")
        .only(
            "id",
            "cart_id",
            "product_id",
            "custom_name",
            "quantity",
            "unit_price",
            "line_discount",
            "sale_package_id",
            "performer_id",
            "variant_id",
            "variant__id",
            "variant__size",
            "variant__color",
            "sale_unit_id", "base_quantity",  # ТЗ 26.10 Б-53
            "sale_unit__id", "sale_unit__name", "sale_unit__factor",
            "product__id",
            "product__name",
            "product__barcode",
            "product__stock",
            "product__is_weight",
        )
        .prefetch_related(
            Prefetch("product__images", queryset=image_qs),
            Prefetch("product__promotion_tiers", queryset=promo_tier_qs),
        )
        .order_by("id")
    )
    return Cart.objects.select_related("shift").only(
        "id",
        "company_id",
        "status",
        "is_wholesale",
        "is_default",
        "shift_id",
        "subtotal",
        "discount_total",
        "order_discount_total",
        "order_discount_percent",
        "tax_total",
        "total",
    ).prefetch_related(
        Prefetch("items", queryset=item_qs)
    )


MAX_OPEN_CARTS_PER_SHIFT = 10


def _normalize_pos_request_data(data):
    """Фронт может прислать camelCase (isNew, saleId)."""
    if not hasattr(data, "get"):
        return data
    out = data.copy() if hasattr(data, "copy") else dict(data)
    if "isNew" in out and "is_new" not in out:
        out["is_new"] = out["isNew"]
    if "saleId" in out and "sale_id" not in out:
        out["sale_id"] = out["saleId"]
    if "isWholesale" in out and "is_wholesale" not in out:
        out["is_wholesale"] = out["isWholesale"]
    if "orderDiscountTotal" in out and "order_discount_total" not in out:
        out["order_discount_total"] = out["orderDiscountTotal"]
    if "orderDiscountPercent" in out and "order_discount_percent" not in out:
        out["order_discount_percent"] = out["orderDiscountPercent"]
    return out


def _pos_body_has_explicit_field(request, *field_names):
    """Поле реально передано в теле запроса (не дефолт сериализатора)."""
    raw = _normalize_pos_request_data(getattr(request, "data", {}) or {})
    if not hasattr(raw, "get"):
        return False
    for name in field_names:
        if name in raw and raw.get(name) not in (None, "", "null"):
            return True
    return False


def _shift_carts_base_qs(company, user, shift):
    qs = Cart.objects.filter(
        company=company,
        shift=shift,
        status=Cart.Status.ACTIVE,
    )
    if user:
        qs = qs.filter(Q(user=user) | Q(shift__cashier=user))
    return qs


def _shift_active_carts_qs(company, user, shift):
    """Список вкладок корзин (с items_count). Без select_for_update — несовместимо с GROUP BY в PostgreSQL."""
    return (
        _shift_carts_base_qs(company, user, shift)
        .annotate(items_count=Count("items"))
        .order_by("created_at")
    )



def _pick_shift_cart_id(*, company, user, shift, sale_id=None):
    base = _shift_carts_base_qs(company, user, shift)
    if sale_id:
        return sale_id if base.filter(id=sale_id).exists() else None
    cart_id = (
        base.filter(is_default=True)
        .order_by("-updated_at")
        .values_list("id", flat=True)
        .first()
    )
    if cart_id:
        return cart_id
    return base.order_by("-updated_at").values_list("id", flat=True).first()


def _lock_single_shift_cart(*, company, user, shift, cart_id):
    if not cart_id:
        return None
    return (
        _shift_carts_base_qs(company, user, shift)
        .select_for_update(of=("self",))
        .filter(id=cart_id)
        .first()
    )


def _find_locked_shift_cart(*, company, user, shift, sale_id=None):
    """FOR UPDATE только одной корзины — не блокируем всю смену."""
    cart_id = _pick_shift_cart_id(
        company=company,
        user=user,
        shift=shift,
        sale_id=sale_id,
    )
    return _lock_single_shift_cart(
        company=company,
        user=user,
        shift=shift,
        cart_id=cart_id,
    )


def _get_pos_open_cart_for_cashier(*, company, user, cart_id):
    """Open-корзина кассира по id (GET вкладки / переключение)."""
    return (
        Cart.objects.filter(
            id=cart_id,
            company=company,
            status=Cart.Status.ACTIVE,
            shift__status=CashShift.Status.OPEN,
        )
        .filter(Q(user=user) | Q(shift__cashier=user))
        .select_related("shift")
        .first()
    )


def _abandon_pos_open_cart(*, company, user, cart):
    """Закрыть open-корзину (вкладку кассы) — status=abandoned, не DELETE Sale."""
    shift = cart.shift
    with transaction.atomic():
        cart = (
            Cart.objects.select_for_update(of=("self",))
            .select_related("shift")
            .get(id=cart.id, company=company, status=Cart.Status.ACTIVE)
        )
        was_default = bool(cart.is_default)
        abandon_cart(cart=cart)
        if was_default:
            new_default_id = (
                _shift_carts_base_qs(company, user, shift)
                .order_by("created_at")
                .values_list("id", flat=True)
                .first()
            )
            if new_default_id:
                Cart.objects.filter(id=new_default_id).update(
                    is_default=True,
                    updated_at=timezone.now(),
                )
    return shift


def _pos_cart_tab_label(cart, ordered_carts):
    if getattr(cart, "is_default", False):
        return "Основная"
    for idx, c in enumerate(ordered_carts):
        if c.id == cart.id:
            return f"Корзина {idx + 1}"
    return "Корзина"


def _serialize_pos_cart_tab(cart, ordered_carts):
    return {
        "id": str(cart.id),
        "is_default": bool(getattr(cart, "is_default", False)),
        "label": _pos_cart_tab_label(cart, ordered_carts),
        "items_count": int(getattr(cart, "items_count", 0) or 0),
        "total": fmt_money(cart.total),
        "status": "open",
    }


def _serialize_pos_sale(request, cart):
    data = SaleCartSerializer(cart, context={"request": request}).data
    if data.get("id") is not None:
        data["id"] = str(data["id"])
    for field in (
        "subtotal",
        "discount_total",
        "order_discount_total",
        "tax_total",
        "total",
    ):
        if field in data and data[field] is not None:
            data[field] = fmt_money(data[field])
    if data.get("order_discount_percent") is not None:
        data["order_discount_percent"] = f"{money(data['order_discount_percent']):.2f}"
    if data.get("shift"):
        data["shift"] = str(data["shift"])
    return data


def _line_discount_from_request(base_price, quantity, discount_total=None, discount_percent=None) -> Decimal:
    if discount_percent is not None:
        line_base = Decimal(str(base_price or 0)) * Decimal(str(quantity or 0))
        return _q2(line_base * Decimal(str(discount_percent)) / Decimal("100"))
    if discount_total is not None:
        return _q2(Decimal(str(discount_total)))
    return Decimal("0.00")


def _pos_multi_cart_response(request, active_cart, *, status_code=status.HTTP_200_OK):
    shift = active_cart.shift
    user = active_cart.user or request.user
    company = active_cart.company
    carts_qs = _shift_active_carts_qs(company, user, shift)
    ordered = list(carts_qs)
    active_id = str(active_cart.id)
    return Response(
        {
            "sale": _serialize_pos_sale(request, active_cart),
            "active_sale_id": active_id,
            # Старый фронт читал id корзины из корня ответа start/scan/GET.
            "id": active_id,
            "carts": [_serialize_pos_cart_tab(c, ordered) for c in ordered],
        },
        status=status_code,
    )


def _resolve_pos_target_cart(*, company, user, shift, sale_id=None, for_update=False):
    if for_update:
        return _lock_pos_target_cart(company=company, user=user, shift=shift, sale_id=sale_id)

    qs = _shift_active_carts_qs(company, user, shift)

    if sale_id:
        cart = qs.filter(id=sale_id).first()
        if not cart:
            raise ValidationError({"sale_id": "Открытая корзина не найдена в этой смене."})
        return cart

    target = qs.filter(is_default=True).first() or qs.first()
    if not target:
        raise ValidationError({"detail": "Нет открытых корзин в смене."})
    return target


def _cart_response(request, cart_id, *, status_code=status.HTTP_200_OK, multi_cart=False):
    cart = get_object_or_404(
        _cart_queryset_for_response(),
        id=cart_id,
        company=request.user.company,
    )
    if multi_cart and cart.shift_id:
        return _pos_multi_cart_response(request, cart, status_code=status_code)
    return Response(
        SaleCartSerializer(cart, context={"request": request}).data,
        status=status_code,
    )


def _upsert_scanned_cart_item(cart, product, quantity, variant=None):
    scanned_qty = qty3(quantity)
    item = (
        CartItem.objects.select_for_update()
        .filter(cart=cart, product=product, sale_package__isnull=True, variant=variant)
        .first()
    )
    if item:
        item.quantity = qty3(item.quantity + scanned_qty)
        item.save(update_fields=["quantity"], skip_full_clean=True)
        return item

    item = CartItem(
        cart=cart,
        company=cart.company,
        branch=getattr(cart, "branch", None),
        product=product,
        variant=variant,
        quantity=scanned_qty,
        unit_price=(
            variant.effective_price
            if variant is not None
            else (product.wholesale_price or product.price)
            if getattr(cart, "is_wholesale", False)
            else product.price
        ),
    )
    item.save(skip_full_clean=True)
    return item


def _reprice_cart_items_for_mode(cart: Cart) -> None:
    items = list(cart.items.select_related("product", "sale_package"))
    if not items:
        return
    changed = []
    for it in items:
        if not it.product_id or not it.product:
            continue
        p = it.product
        if it.sale_unit_id:  # ТЗ 26.10 Б-53: цена единицы продажи
            from apps.main.tz26.hw_units.units import effective_unit_price

            it.unit_price = effective_unit_price(p, it.sale_unit, wholesale=bool(getattr(cart, "is_wholesale", False)))
            changed.append(it)
            continue
        if getattr(cart, "is_wholesale", False):
            raw_wholesale = getattr(p, "wholesale_price", None)
            raw_retail = getattr(p, "price", None)
            pack_price = Decimal(str(raw_wholesale)) if raw_wholesale not in (None, 0, "0") else Decimal(str(raw_retail or 0))
            if it.sale_package_id:
                ipp = Decimal(str(it.sale_package.quantity_in_package or 0))
                it.unit_price = _q2(pack_price / ipp) if ipp > 0 else _q2(pack_price)
            else:
                it.unit_price = _q2(pack_price)
        else:
            if it.sale_package_id:
                it.unit_price = _q2(default_unit_price_for_package(p, it.sale_package))
            else:
                it.unit_price = _q2(Decimal(str(getattr(p, "price", None) or 0)))
        changed.append(it)
    if changed:
        CartItem.objects.bulk_update(changed, ["unit_price"])
        cart.recalc()


# ТЗ 12.11: все безналичные способы оплаты, попадающие под фильтр payment_method=transfer.
SALE_TRANSFER_METHODS = (
    Sale.PaymentMethod.TRANSFER,
    Sale.PaymentMethod.MBANK,
    Sale.PaymentMethod.OPTIMA,
    Sale.PaymentMethod.OBANK,
    Sale.PaymentMethod.BAKAI,
)


def _aware(dt_or_date, end=False):
    tz = get_current_timezone()
    if isinstance(dt_or_date, datetime):
        return dt_or_date if is_aware(dt_or_date) else make_aware(dt_or_date, tz)
    if isinstance(dt_or_date, date):
        t = dtime(23, 59, 59, 999999) if end else dtime(0, 0, 0)
        return make_aware(datetime.combine(dt_or_date, t), tz)
    return None


def _parse_range_dt(v: str, *, end: bool) -> Optional[datetime]:
    """
    start/end из query_params.
    Поддерживает YYYY-MM-DD и ISO datetime.
    Всегда возвращает aware datetime или None.
    """
    if not v:
        return None
    v = v.strip()
    # Голая дата YYYY-MM-DD: на Python 3.11+ parse_datetime() принимает её как 00:00,
    # поэтому дату разбираем первой (конец диапазона = конец дня).
    d = parse_date(v) if "T" not in v and " " not in v else None
    if d:
        return _aware(d, end=end)
    dt = parse_datetime(v)
    if dt:
        return _aware(dt, end=end)
    return None


def _safe(v) -> str:
    return v if (v is not None and str(v).strip()) else "—"


# qty3 импортирован из pos_utils


@dataclass
class Entry:
    date: datetime
    desc: str
    debit: Decimal
    credit: Decimal


def _party_lines(
    title,
    name,
    inn=None,
    okpo=None,
    score=None,
    bik=None,
    addr=None,
    phone=None,
    email=None,
):
    return [
        title,
        name,
        f"ИНН: {_safe(inn)}   ОКПО: {_safe(okpo)}",
        f"Р/с: {_safe(score)}   БИК: {_safe(bik)}",
        f"Адрес: {_safe(addr)}",
        f"Тел.: {_safe(phone)}",
    ]


# Префиксы EAN-13 весов ШТРИХ-ПРИНТ (см. «Префикс штрих-кода» в драйвере весов):
#   весовой товар  -> в поле EEEEE зашит ВЕС (граммы): 00214 = 0.214 кг
#   итоговый       -> в поле EEEEE зашита СТОИМОСТЬ в сомах: 00044 = 44 сом
# По умолчанию весовой = 20, итоговый = 25 (заводская настройка ШТРИХ).
# Если на весах префиксы перенастроены — поправьте эти множества.
SCALE_WEIGHT_PREFIXES = {"20"}
SCALE_AMOUNT_PREFIXES = {"25"}


def _company_scale_barcode_settings(company_id):
    """(mode, layout, amount_unit) чтения штрихкода весов для компании."""
    row = (
        Company.objects.filter(id=company_id)
        .values("scale_barcode_mode", "scale_barcode_layout", "scale_barcode_amount_unit")
        .first()
    ) or {}
    mode = row.get("scale_barcode_mode") or SCALE_BARCODE_MODE_AUTO
    layout = row.get("scale_barcode_layout") or SCALE_BARCODE_LAYOUT_PLU
    amount_unit = row.get("scale_barcode_amount_unit") or SCALE_BARCODE_AMOUNT_UNIT_TIYIN
    return mode, layout, amount_unit


def _company_scale_barcode_mode(company_id) -> str:
    """Режим чтения штрихкода весов (обратная совместимость)."""
    return _company_scale_barcode_settings(company_id)[0]


def _parse_scale_barcode(barcode: str, mode: str = SCALE_BARCODE_MODE_AUTO,
                         layout: str = SCALE_BARCODE_LAYOUT_PLU,
                         amount_unit: str = SCALE_BARCODE_AMOUNT_UNIT_TIYIN):
    """
    EAN-13 штрихкод весов. Раскладка полей задаётся `layout`:

    - "plu"  (по умолчанию): FF PPPPP EEEEE C
        FF (20–29) префикс, PPPPP PLU (5 цифр), EEEEE вес/сумма (5 цифр), C — чек.
        Товар ищется по Product.plu.
    - "code" : FF PPPPPP EEEE C
        FF префикс, PPPPPP PLU (6 цифр — в экспорте «Код»=PLU), EEEE вес/сумма
        (4 цифры), C — чек. Товар ищется по Product.plu.

    Трактовка поля значения задаётся `mode` (Company.scale_barcode_mode):
    weight | amount | auto (по префиксу). Единица суммы — `amount_unit`
    (Company.scale_barcode_amount_unit): tiyin (÷100) | som (целые сомы).
    """
    if not barcode or len(barcode) != 13 or not barcode.isdigit():
        return None

    prefix = barcode[0:2]
    try:
        prefix_num = int(prefix)
    except ValueError:
        return None
    if not (20 <= prefix_num <= 29):
        return None

    check_digit = barcode[12]

    # Границы полей: «по коду» — PLU(6)+значение(4), «по PLU» — PLU(5)+значение(5).
    if layout == SCALE_BARCODE_LAYOUT_CODE:
        raw_code, value_raw = barcode[2:8], barcode[8:12]
    else:
        raw_code, value_raw = barcode[2:7], barcode[7:12]

    try:
        plu = int(raw_code)
    except ValueError:
        return None

    if mode == SCALE_BARCODE_MODE_WEIGHT:
        as_weight = True
    elif mode == SCALE_BARCODE_MODE_AMOUNT:
        as_weight = False
    else:  # auto — по префиксу
        as_weight = prefix in SCALE_WEIGHT_PREFIXES

    # Весовой штрихкод: в поле зашит ВЕС (граммы), а не сумма.
    if as_weight:
        try:
            weight_raw = int(value_raw)
        except ValueError:
            return None
        return {
            "prefix": prefix,
            "plu": plu,
            # Раскладка «по коду» исторически отдавала код без ведущих нулей.
            "raw_code": str(plu) if layout == SCALE_BARCODE_LAYOUT_CODE else raw_code,
            "weight_raw": weight_raw,
            "weight_kg": Decimal(weight_raw) / Decimal(1000),
            "check_digit": check_digit,
            "mode": "weight",
        }

    # Итоговый/прочие префиксы: в поле зашита СТОИМОСТЬ. Единица зависит от весов:
    #   tiyin — сумма в тыйынах: на этикетке 38.00 сом → поле «03800» (делим на 100);
    #   som   — сумма целыми сомами: на этикетке 36 сом → поле «00036» (как есть).
    divisor = Decimal(1) if amount_unit == SCALE_BARCODE_AMOUNT_UNIT_SOM else Decimal(100)
    amount = (Decimal(value_raw) / divisor).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    return {
        "prefix": prefix,
        "plu": plu,
        "raw_code": str(plu) if layout == SCALE_BARCODE_LAYOUT_CODE else raw_code,
        "amount_raw": value_raw,
        "amount": amount,
        "amount_unit": amount_unit,
        "check_digit": check_digit,
        "mode": "amount_plain",
    }


def _scale_barcode_variants(barcode: str, mode: str, layout: str,
                            amount_unit: str = SCALE_BARCODE_AMOUNT_UNIT_TIYIN) -> list[dict]:
    """
    Разборы весового штрихкода: сначала раскладка из настроек компании, затем запасная.

    Раскладки дают РАЗНЫЙ PLU для одного и того же кода:
        2 00453 00054 9  →  layout=plu → PLU 453 (вес 54 г)
                            layout=code → PLU 4530 (вес 54 г)
    Если в `Company.scale_barcode_layout` указана не та раскладка, которую реально
    печатают весы, товар не находится («Товар с PLU 4530 … не найден»). Поэтому после
    промаха по основной раскладке пробуем альтернативную.

    Обе раскладки трактуют поле значения одинаково (вес или сумма — по `mode`),
    поэтому запасной разбор безопасен и для суммового штрихкода.
    """
    primary = _parse_scale_barcode(barcode, mode, layout, amount_unit)
    if not primary:
        return []

    variants = [primary]
    alt_layout = (
        SCALE_BARCODE_LAYOUT_PLU
        if layout == SCALE_BARCODE_LAYOUT_CODE
        else SCALE_BARCODE_LAYOUT_CODE
    )

    alt = _parse_scale_barcode(barcode, mode, alt_layout, amount_unit)
    if alt and alt.get("plu") != primary.get("plu"):
        variants.append(alt)
    return variants


# Шаг дискретности торговых весов (поверочное деление e). У весов 6/15 кг это
# обычно 2/5 г; берём 5 г — веса с шагом 10 г и крупнее тоже лежат на этой сетке.
SCALE_WEIGHT_STEP_KG = Decimal("0.005")


def _weight_from_amount(amount: Decimal, price: Decimal, amount_unit: str) -> Decimal:
    """
    Вес из суммы, напечатанной на этикетке весов.

    Весы ОТБРАСЫВАЮТ дробную часть суммы (проверено по этикетке: 0.170 кг × 540 сом/кг
    = 91.80 → на этикетке «91»). Поэтому простое `amount / price` систематически
    занижает вес: 91/540 = 0.16852 → касса показывала 0.169 вместо 0.170.

    Восстанавливаем так: напечатанная сумма A допускает интервал весов
    [A/цена, (A+шаг суммы)/цена). Если ровно один вес с сетки весов (кратный
    SCALE_WEIGHT_STEP_KG) попадает в этот интервал — его весы и взвесили, берём его.
    Если подходящих весов несколько (дешёвый товар: при цене ниже ~200 сом/кг шаг
    в 1 сом грубее шага весов) или ни одного (точная сумма в тыйынах) — не гадаем
    и возвращаем обычное деление, округлённое до грамма.
    """
    exact = amount / price

    # Шаг суммы на этикетке: целые сомы либо тыйыны (2 знака).
    unit = Decimal(1) if amount_unit == SCALE_BARCODE_AMOUNT_UNIT_SOM else Decimal("0.01")

    # Интервал возможных весов — полуоткрытый [exact, upper): сумма отброшена вниз.
    step = SCALE_WEIGHT_STEP_KG
    upper = (amount + unit) / price
    n_min = (exact / step).to_integral_value(rounding=ROUND_CEILING)
    # Наибольшее n, при котором n*step строго меньше upper.
    n_max = (upper / step).to_integral_value(rounding=ROUND_CEILING) - 1

    if n_min == n_max:
        snapped = (n_min * step).quantize(Decimal("0.001"))
        pos_scan_logger.info(
            "weight from amount=%s price=%s unit=%s: %s -> %s (сетка %s кг)",
            amount, price, amount_unit, exact.quantize(Decimal("0.00001")), snapped, step,
        )
        return snapped

    # Однозначно восстановить нельзя — отдаём деление как есть (прежнее поведение).
    plain = exact.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    pos_scan_logger.info(
        "weight from amount=%s price=%s unit=%s: %s -> %s (подходящих весов на сетке: %s)",
        amount, price, amount_unit, exact.quantize(Decimal("0.00001")), plain,
        max(n_max - n_min + 1, 0),
    )
    return plain


def _finalize_scale_data_for_product(product, scale_data: dict) -> Optional[str]:
    """
    Для mode=amount_plain дополняет scale_data полем quantity_kg.
    Возвращает текст ошибки или None.
    """
    if not scale_data or scale_data.get("mode") != "amount_plain":
        return None

    price_raw = getattr(product, "price", None)
    if price_raw is None or price_raw == "":
        return "Невозможно рассчитать вес: у товара не указана цена"

    price = Decimal(str(price_raw))
    if price <= 0:
        return "Невозможно рассчитать вес: у товара не указана цена"

    amount = scale_data.get("amount")
    if amount is None:
        return "Невозможно рассчитать вес: в штрихкоде не указана сумма"

    quantity_kg = _weight_from_amount(
        Decimal(str(amount)),
        price,
        scale_data.get("amount_unit") or SCALE_BARCODE_AMOUNT_UNIT_TIYIN,
    )
    scale_data["quantity_kg"] = quantity_kg
    scale_data["plu"] = scale_data.get("plu")
    scale_data["amount"] = Decimal(str(amount))
    scale_data["mode"] = "amount_plain"
    return None


def _effective_qty_from_scale_data(scale_data, qty) -> Decimal:
    if scale_data:
        if "quantity_kg" in scale_data:
            return Decimal(str(scale_data["quantity_kg"]))
        if "weight_kg" in scale_data:
            return Decimal(str(scale_data["weight_kg"]))
    return Decimal(str(qty))


def _parse_scale_barcode_loose(barcode: str):
    """
    Как на складе (warehouse): любой 13-значный EAN → PLU + вес.
    Используется только если прямой поиск по штрихкоду не дал результат.
    """
    if not barcode or len(barcode) != 13 or not barcode.isdigit():
        return None

    raw_code = barcode[2:7]
    weight_digits = barcode[7:12]

    try:
        plu = int(raw_code)
        weight_raw = int(weight_digits)
    except ValueError:
        return None

    return {
        "prefix": barcode[0:2],
        "plu": plu,
        "raw_code": raw_code,
        "weight_raw": weight_raw,
        "weight_kg": weight_raw / 1000.0,
    }


def _pos_barcode_lookup_candidates(barcode: str):
    """Варианты штрихкода для поиска (ведущие нули, EAN-13 padding)."""
    raw = (barcode or "").strip()
    if not raw:
        return []

    candidates = []

    def _add(value):
        value = (value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    _add(raw)
    if raw.isdigit():
        _add(raw.lstrip("0") or "0")
        if len(raw) < 13:
            _add(raw.zfill(13))
        if len(raw) == 13 and raw.startswith("0"):
            _add(raw[1:])
        if len(raw) == 14 and raw.startswith("0"):
            _add(raw[1:])
    return candidates


POS_SCAN_PRODUCT_FIELDS = (
    "id",
    "company_id",
    "price",
    "barcode",
    "plu",
    "code",
    "is_weight",
    "is_adult",
    "kind",
)


def _product_pk_from_cache(value):
    """Поддержка legacy-кэша: раньше хранили ORM-объект, теперь — UUID строкой."""
    if value is None:
        return None
    if isinstance(value, Product):
        return value.pk
    return value


pos_scan_logger = logging.getLogger("crm.pos.scan")


class AmbiguousBarcode(Exception):
    """Штрихкод (с учётом нормализации/альт-кодов) найден у нескольких разных товаров.

    Раньше в таких случаях брался произвольный `.first()` — в чек мог уйти не тот
    товар. Теперь бэкенд не угадывает: касса получает 409 и просит выбрать вручную.
    """

    def __init__(self, barcode: str, matches):
        self.barcode = barcode
        self.matches = matches  # [(id_str, name, is_adult), ...]
        super().__init__(f"Штрихкод {barcode} найден у нескольких товаров.")


def _ambiguous_barcode_response(exc: "AmbiguousBarcode") -> Response:
    formatted_matches = []
    for item in exc.matches:
        if len(item) == 3:
            mid, name, is_adult = item
        else:
            mid, name = item[0], item[1]
            is_adult = False
        formatted_matches.append({"id": mid, "name": name, "is_adult": is_adult})

    return Response(
        {
            "ambiguous": True,
            "message": (
                f"Штрихкод {exc.barcode} найден у нескольких товаров — "
                f"выберите нужный вручную."
            ),
            "matches": formatted_matches,
        },
        status=status.HTTP_409_CONFLICT,
    )


def _product_matches_candidates(product, candidates, company_id) -> bool:
    """Проверка, что найденный (в т.ч. из кэша) товар действительно содержит один
    из кандидатов-штрихкодов — защита от протухшего/чужого кэша."""
    if (getattr(product, "barcode", None) or "") in candidates:
        return True
    return ProductAlternateBarcode.objects.filter(
        product_id=product.pk, company_id=company_id, barcode__in=candidates
    ).exists()


def _resolve_product_by_barcode_for_pos(company_id, barcode: str, *, only_fields):
    """Товар по основному или дополнительному штрихкоду. В кэше хранится только UUID.

    Приоритет — точное совпадение основного `barcode` (это и есть напечатанный код),
    затем кэш (с проверкой), затем нормализованные кандидаты и альт-коды. Если после
    нормализации/альтов под скан подходят РАЗНЫЕ товары — поднимаем AmbiguousBarcode,
    а не берём произвольный.
    """
    candidates = _pos_barcode_lookup_candidates(barcode)
    if not candidates:
        return None
    raw = (barcode or "").strip()

    # 1) Полный поиск по кандидатам (основной + альтернативные).
    matches = list(
        Product.objects.only(*only_fields)
        .filter(company_id=company_id)
        .filter(Q(barcode__in=candidates) | Q(alternate_barcodes__barcode__in=candidates))
        .distinct()
        .order_by("created_at", "id")
    )
    if not matches:
        return None
    if len(matches) > 1:
        pairs = [(str(m.pk), getattr(m, "name", ""), bool(getattr(m, "is_adult", False))) for m in matches]
        pos_scan_logger.warning("scan barcode=%s company=%s AMBIGUOUS -> %s", raw, company_id, pairs)
        raise AmbiguousBarcode(raw, pairs)

    product = matches[0]
    for candidate in candidates:
        cache.set(f"product_barcode:{company_id}:{candidate}", str(product.pk), 300)
    pos_scan_logger.info("scan barcode=%s company=%s -> product=%s", raw, company_id, product.pk)
    return product


def _resolve_product_by_plu_or_code_for_pos(company_id, scale_data: dict, *, only_fields):
    """Весовой штрих: сначала PLU (как на складе), затем legacy-поиск по code."""
    plu = scale_data.get("plu")
    if plu is not None:
        product = Product.objects.only(*only_fields).filter(company_id=company_id, plu=plu).first()
        if product:
            return product

    raw_code = scale_data.get("raw_code") or ""
    try:
        normalized_code = str(int(raw_code))
    except Exception:
        normalized_code = raw_code
    padded_code = normalized_code.zfill(4) if normalized_code.isdigit() else normalized_code

    for code_value in (normalized_code, raw_code, padded_code):
        if not code_value:
            continue
        cache_key = f"product_code:{company_id}:{code_value}"
        cached_id = _product_pk_from_cache(cache.get(cache_key))
        if cached_id:
            try:
                return Product.objects.only(*only_fields).get(pk=cached_id, company_id=company_id)
            except (Product.DoesNotExist, TypeError, ValueError):
                cache.delete(cache_key)
        try:
            product = Product.objects.only(*only_fields).get(
                company_id=company_id,
                code=code_value,
            )
            cache.set(cache_key, str(product.pk), 300)
            return product
        except Product.DoesNotExist:
            continue
    return None


def _lookup_product_for_pos_scan(company_id, barcode: str, *, only_fields=POS_SCAN_PRODUCT_FIELDS):
    """
    Единый поиск товара для POS-скана.
    Возвращает (product, scale_data|None, error_message|None).
    Удалённый (архивный) товар не продаётся.
    """
    product, scale_data, error = _lookup_product_for_pos_scan_any(company_id, barcode, only_fields=only_fields)
    if product is not None and getattr(product, "status", None) == Product.Status.ARCHIVED:
        return None, scale_data, "Товар удалён"
    return product, scale_data, error


def _lookup_product_for_pos_scan_any(company_id, barcode: str, *, only_fields=POS_SCAN_PRODUCT_FIELDS):
    """Поиск товара для POS-скана без учёта статуса (см. _lookup_product_for_pos_scan)."""
    barcode = (barcode or "").strip()
    if not barcode:
        return None, None, "Пустой штрихкод"

    product = _resolve_product_by_barcode_for_pos(company_id, barcode, only_fields=only_fields)
    if product:
        return product, None, None

    # Прямой PLU (короткий числовой код с этикетки/весов)
    if barcode.isdigit() and len(barcode) <= 7:
        try:
            plu_value = int(barcode)
            product = Product.objects.only(*only_fields).filter(company_id=company_id, plu=plu_value).first()
            if product:
                return product, None, None
        except ValueError:
            pass

    scale_mode, scale_layout, scale_amount_unit = _company_scale_barcode_settings(company_id)
    scale_variants = _scale_barcode_variants(barcode, scale_mode, scale_layout, scale_amount_unit)
    if scale_variants:
        for idx, scale_data in enumerate(scale_variants):
            product = _resolve_product_by_plu_or_code_for_pos(
                company_id, scale_data, only_fields=only_fields
            )
            if not product:
                continue
            if idx > 0:
                pos_scan_logger.warning(
                    "scan barcode=%s company=%s: товар найден по ЗАПАСНОЙ раскладке весов "
                    "(plu=%s вместо %s) — проверьте Company.scale_barcode_layout",
                    barcode, company_id, scale_data.get("plu"), scale_variants[0].get("plu"),
                )
            finalize_error = _finalize_scale_data_for_product(product, scale_data)
            if finalize_error:
                return None, scale_data, finalize_error
            return product, scale_data, None

        primary = scale_variants[0]
        plu = primary.get("plu")
        raw_code = primary.get("raw_code")
        message = f"Товар с PLU {plu} / кодом {raw_code} не найден"
        if len(scale_variants) > 1:
            tried = " / ".join(str(v.get("plu")) for v in scale_variants)
            message = f"Товар не найден: проверены PLU {tried} (штрихкод {barcode})"
        return None, primary, message

    # Fallback как на складе: 13 цифр → PLU из середины штрихкода
    scale_loose = _parse_scale_barcode_loose(barcode)
    if scale_loose:
        product = Product.objects.only(*only_fields).filter(
            company_id=company_id,
            plu=scale_loose["plu"],
        ).first()
        if product:
            return product, scale_loose, None
        plu = scale_loose.get("plu")
        raw_code = scale_loose.get("raw_code")
        return None, scale_loose, f"Товар с PLU {plu} / кодом {raw_code} не найден"

    # Внутренний код товара (code), если штрихкод числовой
    if barcode.isdigit():
        code_candidates = [barcode, str(int(barcode)), barcode.zfill(4)]
        for code_value in code_candidates:
            product = Product.objects.only(*only_fields).filter(
                company_id=company_id,
                code=code_value,
            ).first()
            if product:
                return product, None, None

    return None, None, "Товар не найден"


def _lock_pos_target_cart(*, company, user, shift, sale_id=None):
    """Блокировка одной open-корзины без annotate (PostgreSQL: FOR UPDATE + GROUP BY запрещён)."""
    cart = _find_locked_shift_cart(company=company, user=user, shift=shift, sale_id=sale_id)
    if not cart:
        if sale_id:
            raise ValidationError({"sale_id": "Открытая корзина не найдена в этой смене."})
        raise ValidationError({"detail": "Нет открытых корзин в смене."})
    return cart


def _resolve_pos_cashbox(company, branch, cashbox_id=None):
    """
    Правило:
    - если cashbox_id передали -> берём её (только этой компании/филиала)
    - иначе -> берём последнюю созданную кассу этого филиала
    - если в филиале нет кассы -> пробуем глобальную (branch NULL)
    """
    qs = Cashbox.objects.filter(company=company)

    if branch is None:
        qs_branch = qs.filter(branch__isnull=True)
    else:
        qs_branch = qs.filter(branch=branch)

    if cashbox_id:
        cb = qs_branch.filter(id=cashbox_id).first()
        if not cb:
            raise ValidationError({"cashbox_id": "Касса не найдена или не принадлежит этому филиалу."})
        return cb

    cb = qs_branch.order_by("-created_at").first()
    if cb:
        return cb

    cb = qs.filter(branch__isnull=True).order_by("-created_at").first()
    return cb


def _find_open_shift_for_cashier(*, company, cashier, cashbox=None, branch=None, for_update=False):
    qs = CashShift.objects.filter(
        company=company,
        cashier=cashier,
        status=CashShift.Status.OPEN,
    )
    if for_update:
        qs = qs.select_for_update()
    if cashbox is not None:
        qs = qs.filter(cashbox=cashbox)
    elif branch is not None:
        qs = qs.filter(branch=branch)
    return qs.order_by("-opened_at").first()


def _resolve_requested_open_shift(*, company, cashier, shift_id, cashbox_id=None):
    try:
        parsed_shift_id = uuid.UUID(str(shift_id))
    except (TypeError, ValueError, AttributeError):
        raise ValidationError({"shift": "Некорректный ID смены."})

    shift = (
        CashShift.objects.select_related("cashbox", "cashier")
        .filter(id=parsed_shift_id, company=company)
        .first()
    )
    if not shift or shift.status != CashShift.Status.OPEN:
        # ТЗ Н-09: машинный код отказа (текст и статус 400 прежние)
        raise ValidationError(
            {
                "detail": "Смена не открыта. Сначала откройте смену на кассе, затем начните продажу.",
                "code": "shift_not_found" if not shift else "shift_closed",
            }
        )
    if cashbox_id and str(shift.cashbox_id) != str(cashbox_id):
        raise ValidationError({"cashbox_id": "Переданная смена относится к другой кассе."})
    return shift


def _ensure_open_shift(*, company, branch, cashier, cashbox, opening_cash=None):
    """
    Возвращает открытую смену ТОЛЬКО этого cashier в этой cashbox.
    Если нет — открывает новую.
    Чужую смену НЕ возвращает никогда.
    """
    if not cashier or not getattr(cashier, "is_authenticated", False):
        raise ValidationError({"cashier": "Нужен кассир."})
    if not cashbox:
        raise ValidationError({"cashbox": "Нужна касса."})

    if opening_cash in (None, "", "null", "None"):
        opening_cash = None
    if opening_cash is not None:
        try:
            opening_cash = Decimal(str(opening_cash))
        except Exception:
            opening_cash = Decimal("0.00")
    else:
        opening_cash = Decimal("0.00")

    qs = (
        CashShift.objects.select_for_update()
        .filter(
            company=company,
            cashbox=cashbox,
            status=CashShift.Status.OPEN,
            cashier=cashier,
        )
        .order_by("-opened_at")
    )
    shift = qs.first()
    if shift:
        return shift

    shift = CashShift.objects.create(
        company=company,
        branch=branch,
        cashbox=cashbox,
        cashier=cashier,
        status=CashShift.Status.OPEN,
        opened_at=timezone.now(),
        opening_cash=opening_cash,
    )
    schedule_after_commit(sync_ekassa_after_local_shift_open_by_id, shift.id)
    return shift


def _supplier_recon_rows(company, client, start_dt, end_dt, source="both"):
    """
    ТЗ Н-12: акт сверки с поставщиком — направление «мы должны».
    Знак как в акте клиента (плюс = контрагент должен нам), поэтому наш долг поставщику —
    отрицательное сальдо: debt.debtor = наша компания, amount = |closing| = kpis.debt поставщика.
    Строки: сделки-долги (кредит поставщика), предоплата/зачёт возврата по сделке и оплаты (дебет);
    приходы и возвраты поставщику — справочные строки (сумма в `doc_amount`, на сальдо не влияют:
    долг приходов уже в сделках, возврат деньгами — движение денег, зачёт долга — в предоплате сделки).
    Сделки kind=sale/amount у поставщика (оплаченные закупки сайта) в долг не входят.
    Возвращает (opening, rows); rows: dict(date, title, a_debit, a_credit, b_debit, b_credit, ref_type, ref_id, extra).
    """
    from apps.main.models import DealPayment, SupplierReceipt, SupplierReceiptItem, SupplierReturn, SupplierReturnItem

    Z = Decimal("0.00")
    owed_before = Z
    rows = []

    def _row(dt, title, a_debit, a_credit, ref_type, ref_id, extra=None):
        rows.append(dict(
            date=dt, title=title, a_debit=a_debit, a_credit=a_credit,
            b_debit=a_credit, b_credit=a_debit, ref_type=ref_type, ref_id=ref_id, extra=extra or {},
        ))

    if source in ("both", "deals"):
        debt_deals = ClientDeal.objects.filter(company=company, client=client, kind=ClientDeal.Kind.DEBT)
        agg = debt_deals.filter(created_at__lt=start_dt).aggregate(a=Sum("amount"), p=Sum("prepayment"))
        owed_before += (agg["a"] or Z) - (agg["p"] or Z)
        pay_before = DealPayment.objects.filter(
            company=company, deal__client=client, deal__kind=ClientDeal.Kind.DEBT, paid_date__lt=start_dt.date()
        )
        for kind, sign in ((DealPayment.Kind.PAY, 1), (DealPayment.Kind.REFUND, -1)):
            owed_before -= sign * (pay_before.filter(kind=kind).aggregate(s=Sum("amount"))["s"] or Z)

        for d in debt_deals.filter(created_at__gte=start_dt, created_at__lte=end_dt).order_by("created_at"):
            amt = q2(d.amount)
            if amt > 0:
                _row(d.created_at, f"Долг поставщику: {d.title}", Z, amt, "deal", d.id)
            pp = q2(d.prepayment or Z)
            if pp > 0:
                _row(d.created_at, f"Предоплата / зачёт по сделке: {d.title}", pp, Z, "deal_prepayment", d.id)
        pays = (
            DealPayment.objects.filter(
                company=company, deal__client=client, deal__kind=ClientDeal.Kind.DEBT,
                paid_date__gte=start_dt.date(), paid_date__lte=end_dt.date(),
            ).select_related("deal", "installment").order_by("paid_date", "created_at")
        )
        for pay in pays:
            amt = q2(pay.amount)
            sign_debit = amt if pay.kind == DealPayment.Kind.PAY else Z
            sign_credit = amt if pay.kind == DealPayment.Kind.REFUND else Z
            label = "Оплата" if pay.kind == DealPayment.Kind.PAY else "Возврат оплаты"
            _row(_aware(pay.paid_date, end=False), f"{label} по сделке: {pay.deal.title} (взнос №{pay.installment.number})",
                 sign_debit, sign_credit, "installment_payment", pay.id)
        # сальдо «до начала» уже включает оплаты; знак: наш долг = отрицательное сальдо
    opening = q2(-owed_before)

    if source == "both":
        rcpts = SupplierReceipt.objects.filter(
            company=company, supplier=client, created_at__gte=start_dt, created_at__lte=end_dt
        ).order_by("created_at")
        for r in rcpts:
            tot = sum(
                (Decimal(str(i.qty)) * Decimal(str(i.purchase_price or 0)) for i in SupplierReceiptItem.objects.filter(receipt=r)),
                Z,
            )
            _row(r.created_at, f"Приход поставщика {r.id}", Z, Z, "receipt", r.id, {"doc_amount": fmt(q2(tot))})
        rets = SupplierReturn.objects.filter(
            company=company, supplier=client, status=SupplierReturn.Status.POSTED,
            created_at__gte=start_dt, created_at__lte=end_dt,
        ).order_by("created_at")
        for rt in rets:
            tot = sum(
                (Decimal(str(i.qty)) * Decimal(str(i.purchase_price or 0)) for i in SupplierReturnItem.objects.filter(supplier_return=rt)),
                Z,
            )
            _row(rt.created_at, f"Возврат поставщику {rt.id} ({rt.get_compensation_display()})", Z, Z,
                 "supplier_return", rt.id, {"doc_amount": fmt(q2(tot)), "compensation": rt.compensation})
    rows.sort(key=lambda x: x["date"])
    return opening, rows


class ClientReconciliationJSONAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, client_id, *args, **kwargs):
        company = request.user.company
        client = get_object_or_404(Client, id=client_id, company=company)

        source = (request.query_params.get("source") or "both").lower()
        currency = request.query_params.get("currency") or "KGS"

        s = request.query_params.get("start")
        e = request.query_params.get("end")
        if not s or not e:
            return Response(
                {"detail": "Укажите параметры start и end (YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_raw = parse_datetime(s) or parse_date(s)
        end_raw = parse_datetime(e) or parse_date(e)
        if not start_raw or not end_raw:
            return Response(
                {"detail": "Неверный формат дат. Используйте YYYY-MM-DD или ISO datetime."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_dt = _aware(start_raw, end=False)
        end_dt = _aware(end_raw, end=True)

        if start_dt > end_dt:
            return Response(
                {"detail": "start не может быть больше end."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ТЗ Н-12: поставщик — свой расчёт (направление «мы должны»)
        is_supplier = client.type == Client.StatusClient.SUPPLIERS

        # ---------- opening ----------
        debit_before = Decimal("0.00")
        credit_before = Decimal("0.00")

        if not is_supplier and source in ("both", "sales"):
            # Отменённые продажи (полный возврат) не создают задолженности —
            # исключаем их, чтобы возврат корректно уменьшал долг в акте сверки.
            sales_before = (
                Sale.objects.filter(company=company, client=client, created_at__lt=start_dt)
                .exclude(status=Sale.Status.CANCELED)
                .aggregate(s=Sum("total"))
                .get("s")
                or Decimal("0")
            )
            debit_before += sales_before

        if not is_supplier and ClientDeal and source in ("both", "deals"):
            deals_before = (
                ClientDeal.objects.filter(
                    company=company,
                    client=client,
                    created_at__lt=start_dt,
                    kind__in=[ClientDeal.Kind.SALE, ClientDeal.Kind.AMOUNT, ClientDeal.Kind.DEBT],
                )
                .aggregate(s=Sum("amount"))
                .get("s")
                or Decimal("0")
            )
            debit_before += deals_before

            pre_before = (
                ClientDeal.objects.filter(company=company, client=client, created_at__lt=start_dt)
                .aggregate(s=Sum("prepayment"))
                .get("s")
                or Decimal("0")
            )
            credit_before += pre_before

        if not is_supplier and DealInstallment and source in ("both", "deals"):
            inst_before = (
                DealInstallment.objects.filter(
                    deal__company=company,
                    deal__client=client,
                    paid_on__isnull=False,
                    paid_on__lt=start_dt.date(),
                )
                .aggregate(s=Sum("amount"))
                .get("s")
                or Decimal("0")
            )
            credit_before += inst_before

        opening = q2(debit_before - credit_before)
        sup_rows = []
        if is_supplier:
            opening, sup_rows = _supplier_recon_rows(company, client, start_dt, end_dt, source)

        # ---------- entries ----------
        entries = []

        def _push(dt, title, a_debit, a_credit, b_debit, b_credit, ref_type=None, ref_id=None, extra=None):
            # отдаём ISO в локальном времени, чтобы фронту было проще
            dt_local = timezone.localtime(dt) if hasattr(timezone, "localtime") else dt
            entries.append(
                {
                    "date": dt_local.isoformat(),
                    "title": title,
                    "a_debit": fmt(a_debit),
                    "a_credit": fmt(a_credit),
                    "b_debit": fmt(b_debit),
                    "b_credit": fmt(b_credit),
                    "ref_type": ref_type,
                    "ref_id": str(ref_id) if ref_id else None,
                    **(extra or {}),
                }
            )

        for r in sup_rows:
            _push(r["date"], r["title"], r["a_debit"], r["a_credit"], r["b_debit"], r["b_credit"],
                  ref_type=r["ref_type"], ref_id=r["ref_id"], extra=r["extra"])

        if not is_supplier and source in ("both", "sales"):
            qs = (
                Sale.objects.filter(
                    company=company,
                    client=client,
                    created_at__gte=start_dt,
                    created_at__lte=end_dt,
                )
                .exclude(status=Sale.Status.CANCELED)
                .order_by("created_at")
            )
            for srow in qs:
                amt = q2(srow.total)
                if amt > 0:
                    _push(
                        srow.created_at,
                        f"Продажа {srow.id}",
                        a_debit=amt,
                        a_credit=Decimal("0.00"),
                        b_debit=Decimal("0.00"),
                        b_credit=amt,
                        ref_type="sale",
                        ref_id=srow.id,
                    )

        if not is_supplier and ClientDeal and source in ("both", "deals"):
            deals_qs = (
                ClientDeal.objects.filter(
                    company=company,
                    client=client,
                    created_at__gte=start_dt,
                    created_at__lte=end_dt,
                    kind__in=[ClientDeal.Kind.SALE, ClientDeal.Kind.AMOUNT, ClientDeal.Kind.DEBT],
                )
                .order_by("created_at")
            )
            for d in deals_qs:
                amt = q2(d.amount)
                if amt > 0:
                    _push(
                        d.created_at,
                        f"Сделка: {d.title} ({d.get_kind_display()})",
                        a_debit=amt,
                        a_credit=Decimal("0.00"),
                        b_debit=Decimal("0.00"),
                        b_credit=amt,
                        ref_type="deal",
                        ref_id=d.id,
                    )

            pre_qs = (
                ClientDeal.objects.filter(
                    company=company,
                    client=client,
                    prepayment__gt=0,
                    created_at__gte=start_dt,
                    created_at__lte=end_dt,
                )
                .order_by("created_at")
            )
            for d in pre_qs:
                pp = q2(d.prepayment)
                _push(
                    d.created_at,
                    f"Предоплата (сделка: {d.title})",
                    a_debit=Decimal("0.00"),
                    a_credit=pp,
                    b_debit=pp,
                    b_credit=Decimal("0.00"),
                    ref_type="deal_prepayment",
                    ref_id=d.id,
                )

        if not is_supplier and DealInstallment and source in ("both", "deals"):
            inst_qs = (
                DealInstallment.objects.filter(
                    deal__company=company,
                    deal__client=client,
                    paid_on__isnull=False,
                    paid_on__gte=start_dt.date(),
                    paid_on__lte=end_dt.date(),
                )
                .select_related("deal")
                .order_by("paid_on", "number")
            )
            for inst in inst_qs:
                amt = q2(inst.amount)
                dt = _aware(inst.paid_on, end=False)
                _push(
                    dt,
                    f"Оплата по рассрочке №{inst.number} (сделка: {inst.deal.title})",
                    a_debit=Decimal("0.00"),
                    a_credit=amt,
                    b_debit=amt,
                    b_credit=Decimal("0.00"),
                    ref_type="installment_payment",
                    ref_id=inst.id,
                )

        # сортировка по дате (ISO строка уже есть, но сортируем по реальному dt)
        # поэтому собираем ещё раз через парсинг ISO не надо — проще сортировать до преобразования
        # тут уже entries готовые => сортируем по date-строке, она ISO и корректно сортируется
        entries.sort(key=lambda x: x["date"])

        # ---------- totals & closing ----------
        def _sum_field(field: str) -> Decimal:
            s = Decimal("0")
            for r in entries:
                try:
                    s += Decimal(str(r[field]).replace(",", "."))
                except Exception:
                    pass
            return q2(s)

        totals = {
            "a_debit": fmt(_sum_field("a_debit")),
            "a_credit": fmt(_sum_field("a_credit")),
            "b_debit": fmt(_sum_field("b_debit")),
            "b_credit": fmt(_sum_field("b_credit")),
        }

        # closing = opening + обороты (как в PDF)
        closing = q2(opening + _sum_field("a_debit") - _sum_field("a_credit"))
        as_of_date = (end_dt + timedelta(days=1)).date()

        company_name = getattr(company, "llc", None) or getattr(company, "name", str(company))
        client_name = client.llc or client.enterprise or client.full_name

        debtor = None
        creditor = None
        amount = abs(closing)

        if closing > 0:
            debtor = client_name
            creditor = company_name
        elif closing < 0:
            debtor = company_name
            creditor = client_name

        # ---------- running balance after each entry (для фронта) ----------
        # running = opening; for each row: running += a_debit - a_credit
        running = opening
        for r in entries:
            a_deb = _as_decimal(r["a_debit"], default=Decimal("0"))
            a_cre = _as_decimal(r["a_credit"], default=Decimal("0"))
            running = q2(running + a_deb - a_cre)
            r["running_balance_after"] = fmt(running)

        payload = {
            "company": {
                "id": str(company.id),
                "name": company_name,
                "inn": _safe(getattr(company, "inn", None)),
                "okpo": _safe(getattr(company, "okpo", None)),
                "score": _safe(getattr(company, "score", None)),
                "bik": _safe(getattr(company, "bik", None)),
                "address": _safe(getattr(company, "address", None)),
                "phone": _safe(getattr(company, "phone", None)),
                "email": _safe(getattr(company, "email", None)),
            },
            "client": {
                "id": str(client.id),
                "name": client_name,
                "inn": _safe(getattr(client, "inn", None)),
                "okpo": _safe(getattr(client, "okpo", None)),
                "score": _safe(getattr(client, "score", None)),
                "bik": _safe(getattr(client, "bik", None)),
                "address": _safe(getattr(client, "address", None)),
                "phone": _safe(getattr(client, "phone", None)),
                "email": _safe(getattr(client, "email", None)),
            },
            "period": {
                "start": start_dt.date().isoformat(),
                "end": end_dt.date().isoformat(),
                "source": source,
                "currency": currency,
            },
            "opening_balance": fmt(opening),
            "entries": entries,
            "totals": totals,
            "closing_balance": fmt(closing),
            "as_of_date": as_of_date.isoformat(),
            "direction": ("we_owe" if closing < 0 else "they_owe") if closing != 0 else None,
            "debt": {
                "debtor": debtor,
                "creditor": creditor,
                "amount": fmt(amount),
                "currency": currency,
            },
        }
        return Response(payload, status=200)

class ClientReconciliationClassicAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, client_id, *args, **kwargs):
        company = request.user.company
        client = get_object_or_404(Client, id=client_id, company=company)

        source = (request.query_params.get("source") or "both").lower()
        currency = request.query_params.get("currency") or "KGS"

        s = request.query_params.get("start")
        e = request.query_params.get("end")
        if not s or not e:
            return self._error_pdf("Укажите параметры start и end (YYYY-MM-DD).")

        start_dt = parse_datetime(s) or parse_date(s)
        end_dt = parse_datetime(e) or parse_date(e)
        if not start_dt or not end_dt:
            return self._error_pdf("Неверный формат дат. Используйте YYYY-MM-DD или ISO datetime.")

        start_dt = _aware(start_dt, end=False)
        end_dt = _aware(end_dt, end=True)

        is_supplier = client.type == Client.StatusClient.SUPPLIERS  # ТЗ Н-12
        debit_before = Decimal("0.00")
        credit_before = Decimal("0.00")

        if not is_supplier and source in ("both", "sales"):
            # Отменённые продажи (полный возврат) не создают задолженности —
            # исключаем их, чтобы возврат корректно уменьшал долг в акте сверки.
            sales_before = (
                Sale.objects.filter(company=company, client=client, created_at__lt=start_dt)
                .exclude(status=Sale.Status.CANCELED)
                .aggregate(s=Sum("total"))["s"]
                or Decimal("0")
            )
            debit_before += sales_before

        if not is_supplier and ClientDeal and source in ("both", "deals"):
            deals_before = (
                ClientDeal.objects.filter(
                    company=company,
                    client=client,
                    created_at__lt=start_dt,
                    kind__in=[ClientDeal.Kind.SALE, ClientDeal.Kind.AMOUNT, ClientDeal.Kind.DEBT],
                ).aggregate(s=Sum("amount"))["s"]
                or Decimal("0")
            )
            debit_before += deals_before

            pre_before = (
                ClientDeal.objects.filter(company=company, client=client, created_at__lt=start_dt).aggregate(
                    s=Sum("prepayment")
                )["s"]
                or Decimal("0")
            )
            credit_before += pre_before

        if not is_supplier and DealInstallment and source in ("both", "deals"):
            inst_before = (
                DealInstallment.objects.filter(
                    deal__company=company,
                    deal__client=client,
                    paid_on__isnull=False,
                    paid_on__lt=start_dt.date(),
                ).aggregate(s=Sum("amount"))["s"]
                or Decimal("0")
            )
            credit_before += inst_before

        opening = q2(debit_before - credit_before)
        if is_supplier:
            opening, _sup_rows = _supplier_recon_rows(company, client, start_dt, end_dt, source)

        entries: List[Dict] = []
        if is_supplier:
            for r in _sup_rows:
                entries.append({k: r[k] for k in ("date", "title", "a_debit", "a_credit", "b_debit", "b_credit")})

        if not is_supplier and source in ("both", "sales"):
            for s in (
                Sale.objects.filter(
                    company=company,
                    client=client,
                    created_at__gte=start_dt,
                    created_at__lte=end_dt,
                )
                .exclude(status=Sale.Status.CANCELED)
                .order_by("created_at")
            ):
                if q2(s.total) > 0:
                    entries.append(
                        dict(
                            date=s.created_at,
                            title=f"Продажа {s.id}",
                            a_debit=q2(s.total),
                            a_credit=Decimal("0.00"),
                            b_debit=Decimal("0.00"),
                            b_credit=q2(s.total),
                        )
                    )

        if not is_supplier and ClientDeal and source in ("both", "deals"):
            for d in (
                ClientDeal.objects.filter(
                    company=company,
                    client=client,
                    created_at__gte=start_dt,
                    created_at__lte=end_dt,
                    kind__in=[ClientDeal.Kind.SALE, ClientDeal.Kind.AMOUNT, ClientDeal.Kind.DEBT],
                ).order_by("created_at")
            ):
                amt = q2(d.amount)
                if amt > 0:
                    entries.append(
                        dict(
                            date=d.created_at,
                            title=f"Сделка: {d.title} ({d.get_kind_display()})",
                            a_debit=amt,
                            a_credit=Decimal("0.00"),
                            b_debit=Decimal("0.00"),
                            b_credit=amt,
                        )
                    )

            for d in (
                ClientDeal.objects.filter(
                    company=company,
                    client=client,
                    prepayment__gt=0,
                    created_at__gte=start_dt,
                    created_at__lte=end_dt,
                ).order_by("created_at")
            ):
                pp = q2(d.prepayment)
                entries.append(
                    dict(
                        date=d.created_at,
                        title=f"Предоплата (сделка: {d.title})",
                        a_debit=Decimal("0.00"),
                        a_credit=pp,
                        b_debit=pp,
                        b_credit=Decimal("0.00"),
                    )
                )

        if not is_supplier and DealInstallment and source in ("both", "deals"):
            for inst in (
                DealInstallment.objects.filter(
                    deal__company=company,
                    deal__client=client,
                    paid_on__isnull=False,
                    paid_on__gte=start_dt.date(),
                    paid_on__lte=end_dt.date(),
                )
                .select_related("deal")
                .order_by("paid_on", "number")
            ):
                amt = q2(inst.amount)
                dt = _aware(inst.paid_on, end=False)
                entries.append(
                    dict(
                        date=dt,
                        title=f"Оплата по рассрочке №{inst.number} (сделка: {inst.deal.title})",
                        a_debit=Decimal("0.00"),
                        a_credit=amt,
                        b_debit=amt,
                        b_credit=Decimal("0.00"),
                    )
                )

        entries.sort(key=lambda x: x["date"])

        totals = dict(
            a_debit=q2(sum(x["a_debit"] for x in entries) if entries else 0),
            a_credit=q2(sum(x["a_credit"] for x in entries) if entries else 0),
            b_debit=q2(sum(x["b_debit"] for x in entries) if entries else 0),
            b_credit=q2(sum(x["b_credit"] for x in entries) if entries else 0),
        )

        closing = q2(opening + totals["a_debit"] - totals["a_credit"])
        on_date = (end_dt + timedelta(days=1)).date()

        buf = io.BytesIO()
        p = canvas.Canvas(buf, pagesize=A4)
        W, H = A4

        try:
            FONT, BFONT = "DejaVu", "DejaVu-Bold"
            p.setFont(BFONT, 14)
        except Exception:
            FONT, BFONT = "Helvetica", "Helvetica-Bold"
            p.setFont(BFONT, 14)

        p.drawCentredString(W / 2, H - 20 * mm, "АКТ СВЕРКИ ВЗАИМНЫХ РАСЧЁТОВ")
        p.setFont(FONT, 11)
        p.drawCentredString(
            W / 2,
            H - 27 * mm,
            f"Период: {start_dt.strftime('%d.%m.%Y')} — {end_dt.strftime('%d.%m.%Y')}   валюта сверки {currency}",
        )

        company_name = getattr(company, "llc", None) or getattr(company, "name", str(company))
        client_name = client.llc or client.enterprise or client.full_name

        p.setFont(BFONT, 10)
        p.drawString(20 * mm, H - 38 * mm, "КОМПАНИЯ")
        p.drawString(110 * mm, H - 38 * mm, "КЛИЕНТ")
        p.setFont(FONT, 11)
        p.drawString(20 * mm, H - 44 * mm, _safe(company_name))
        p.drawString(110 * mm, H - 44 * mm, _safe(client_name))
        p.setFont(FONT, 9)
        p.drawString(
            20 * mm,
            H - 50 * mm,
            f"ИНН: {_safe(getattr(company,'inn',None))}    ОКПО: {_safe(getattr(company,'okpo',None))}",
        )
        p.drawString(
            110 * mm,
            H - 50 * mm,
            f"ИНН: {_safe(client.inn)}    ОКПО: {_safe(client.okpo)}",
        )
        p.drawString(
            20 * mm,
            H - 56 * mm,
            f"Р/с: {_safe(getattr(company,'score',None))}    БИК: {_safe(getattr(company,'bik',None))}",
        )
        p.drawString(
            110 * mm,
            H - 56 * mm,
            f"Р/с: {_safe(client.score)}    БИК: {_safe(client.bik)}",
        )
        p.drawString(20 * mm, H - 62 * mm, f"Адрес: {_safe(getattr(company,'address',None))}")
        p.drawString(110 * mm, H - 62 * mm, f"Адрес: {_safe(client.address)}")
        p.drawString(
            20 * mm,
            H - 68 * mm,
            f"Тел.: {_safe(getattr(company,'phone',None))}    E-mail: {_safe(getattr(company,'email',None))}",
        )
        p.drawString(
            110 * mm,
            H - 68 * mm,
            f"Тел.: {_safe(client.phone)}    E-mail: {_safe(client.email)}",
        )

        y = H - 78 * mm
        p.setFont(BFONT, 9)
        p.drawString(20 * mm, y, "№")
        p.drawString(28 * mm, y, "Содержание записи")
        p.drawString(100 * mm, y, _safe(company_name))
        p.drawString(148 * mm, y, _safe(client_name))

        y -= 5 * mm
        p.setFont(BFONT, 9)
        p.drawString(100 * mm, y, "Дт")
        p.drawString(118 * mm, y, "Кт")
        p.drawString(148 * mm, y, "Дт")
        p.drawString(166 * mm, y, "Кт")
        p.line(20 * mm, y - 1 * mm, 190 * mm, y - 1 * mm)
        y -= 6 * mm

        p.setFont(FONT, 9)
        a_dt = opening if opening > 0 else Decimal("0.00")
        a_kt = -opening if opening < 0 else Decimal("0.00")
        b_dt = a_kt
        b_kt = a_dt

        p.drawString(28 * mm, y, "Сальдо начальное")
        p.drawRightString(115 * mm, y, fmt(a_dt))
        p.drawRightString(133 * mm, y, fmt(a_kt))
        p.drawRightString(163 * mm, y, fmt(b_dt))
        p.drawRightString(181 * mm, y, fmt(b_kt))
        y -= 7 * mm

        num = 0
        p.setFont(FONT, 9)

        def ensure_page_space(current_y: float) -> float:
            if current_y < 40 * mm:
                p.showPage()
                try:
                    p.setFont(BFONT, 10)
                except Exception:
                    p.setFont("Helvetica-Bold", 10)
                p.drawString(20 * mm, H - 20 * mm, "Продолжение акта сверки")
                yy = H - 30 * mm
                p.setFont(BFONT, 9)
                p.drawString(20 * mm, yy, "№")
                p.drawString(28 * mm, yy, "Содержание записи")
                p.drawString(100 * mm, yy, _safe(company_name))
                p.drawString(148 * mm, yy, _safe(client_name))
                yy -= 5 * mm
                p.drawString(100 * mm, yy, "Дт")
                p.drawString(118 * mm, yy, "Кт")
                p.drawString(148 * mm, yy, "Дт")
                p.drawString(166 * mm, yy, "Кт")
                p.line(20 * mm, yy - 1 * mm, 190 * mm, yy - 1 * mm)
                return yy - 6 * mm
            return current_y

        for row in entries:
            y = ensure_page_space(y)
            num += 1
            p.drawString(20 * mm, y, str(num))
            desc = row["title"]
            line1 = desc[:52]
            line2 = desc[52:104] if len(desc) > 52 else ""
            p.drawString(28 * mm, y, line1)

            p.drawRightString(115 * mm, y, fmt(row["a_debit"]))
            p.drawRightString(133 * mm, y, fmt(row["a_credit"]))
            p.drawRightString(163 * mm, y, fmt(row["b_debit"]))
            p.drawRightString(181 * mm, y, fmt(row["b_credit"]))
            y -= 6 * mm
            if line2:
                y = ensure_page_space(y)
                p.drawString(28 * mm, y, line2)
                y -= 6 * mm

        y -= 4 * mm
        p.line(20 * mm, y, 190 * mm, y)
        y -= 7 * mm
        p.setFont(BFONT, 10)
        p.drawString(28 * mm, y, "Итого обороты:")
        p.drawRightString(115 * mm, y, fmt(totals["a_debit"]))
        p.drawRightString(133 * mm, y, fmt(totals["a_credit"]))
        p.drawRightString(163 * mm, y, fmt(totals["b_debit"]))
        p.drawRightString(181 * mm, y, fmt(totals["b_credit"]))
        y -= 10 * mm

        debtor, creditor, amount = None, None, abs(closing)
        if closing > 0:
            debtor = client_name
            creditor = company_name
        elif closing < 0:
            debtor = company_name
            creditor = client_name

        p.setFont(FONT, 10)
        if amount == 0:
            phrase = f"Задолженность отсутствует на {on_date.strftime('%d.%m.%Y')}."
        else:
            phrase = (
                f"Задолженность {debtor} перед {creditor} на {on_date.strftime('%d.%m.%Y')} "
                f"составляет {fmt(amount)} {currency}"
            )
        p.drawString(20 * mm, y, phrase)
        y -= 8 * mm
        if amount == 0:
            p.drawString(20 * mm, y, "(Ноль сом 00 тыйын)")
        y -= 16 * mm

        p.setFont(BFONT, 10)
        p.drawString(20 * mm, y, _safe(company_name))
        p.drawString(110 * mm, y, _safe(client_name))
        y -= 8 * mm
        p.setFont(FONT, 10)
        p.drawString(20 * mm, y, "Главный бухгалтер: __________________")
        p.drawString(110 * mm, y, "Главный бухгалтер: __________________")

        p.showPage()
        p.save()
        buf.seek(0)
        filename = f"reconciliation_classic_{client.id}_{start_dt.date()}_{end_dt.date()}.pdf"
        return FileResponse(buf, as_attachment=True, filename=filename)

    def _error_pdf(self, message: str):
        buf = io.BytesIO()
        p = canvas.Canvas(buf, pagesize=A4)
        p.setFont("Helvetica-Bold", 14)
        p.drawString(30 * mm, 260 * mm, "Невозможно сформировать акт сверки")
        p.setFont("Helvetica", 11)
        p.drawString(30 * mm, 248 * mm, message)
        p.showPage()
        p.save()
        buf.seek(0)
        return FileResponse(buf, as_attachment=False, filename="reconciliation_error.pdf", status=400)


class SaleInvoiceDownloadAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        sale = get_object_or_404(
            Sale.objects.select_related("company", "user", "client").prefetch_related("items__product"),
            id=pk,
            company=request.user.company,
        )

        doc_no = ensure_sale_doc_number(sale)

        buffer = io.BytesIO()
        p = canvas.Canvas(buffer, pagesize=(210 * mm, 297 * mm))

        # ✅ безопасно: если DejaVu не зарегистрирован — упадём на Helvetica
        _set_font(p, "DejaVu-Bold", 14, fallback="Helvetica-Bold")
        p.drawCentredString(105 * mm, 280 * mm, f"НАКЛАДНАЯ № {doc_no}")
        _set_font(p, "DejaVu", 10, fallback="Helvetica")
        p.drawCentredString(105 * mm, 273 * mm, f"от {sale.created_at.strftime('%d.%m.%Y %H:%M')}")

        company = sale.company
        client = sale.client

        left = _party_lines(
            "КОМПАНИЯ",
            getattr(company, "llc", None) or getattr(company, "name", "—"),
            inn=getattr(company, "inn", None),
            okpo=getattr(company, "okpo", None),
            score=getattr(company, "score", None),
            bik=getattr(company, "bik", None),
            addr=getattr(company, "address", None),
            phone=getattr(company, "phone", None),
        )
        if client:
            right = _party_lines(
                "ПОКУПАТЕЛЬ",
                client.llc or client.enterprise or client.full_name,
                inn=client.inn,
                okpo=client.okpo,
                score=client.score,
                bik=client.bik,
                addr=client.address,
                phone=client.phone,
            )
        else:
            right = _party_lines("ПОКУПАТЕЛЬ", "—")

        y = 260 * mm
        x_left, x_right = 20 * mm, 110 * mm
        _set_font(p, "DejaVu-Bold", 10, fallback="Helvetica-Bold")
        p.drawString(x_left, y, left[0])
        p.drawString(x_right, y, right[0])
        y -= 6 * mm
        _set_font(p, "DejaVu", 10, fallback="Helvetica")
        for i in range(1, len(left)):
            p.drawString(x_left, y, left[i])
            p.drawString(x_right, y, right[i])
            y -= 6 * mm

        y -= 6 * mm
        _set_font(p, "DejaVu-Bold", 10, fallback="Helvetica-Bold")
        p.drawString(20 * mm, y, "Товар")
        p.drawRightString(140 * mm, y, "Кол-во")
        p.drawRightString(160 * mm, y, "Цена")
        p.drawRightString(190 * mm, y, "Сумма")

        y -= 5
        p.line(20 * mm, y, 190 * mm, y)
        y -= 10

        _set_font(p, "DejaVu", 10, fallback="Helvetica")
        amount_step = scale_amount_step(sale.company)
        for it in sale.items.select_related("product"):
            p.drawString(20 * mm, y, (it.name_snapshot or "")[:60])
            p.drawRightString(140 * mm, y, str(it.quantity))
            p.drawRightString(160 * mm, y, fmt_money(it.unit_price))
            p.drawRightString(
                190 * mm,
                y,
                fmt_money(
                    cart_line_base(it, amount_step) - (getattr(it, "line_discount", None) or 0)
                ),
            )
            y -= 7 * mm
            if y < 60 * mm:
                p.showPage()
                y = 270 * mm
                _set_font(p, "DejaVu", 10, fallback="Helvetica")

        y -= 10
        _set_font(p, "DejaVu-Bold", 11, fallback="Helvetica-Bold")
        p.drawRightString(190 * mm, y, f"СУММА (без скидок): {fmt_money(sale.subtotal)}")
        y -= 6 * mm
        if sale.discount_total and sale.discount_total > 0:
            p.drawRightString(190 * mm, y, f"СКИДКА: {fmt_money(sale.discount_total)}")
            y -= 6 * mm
        if sale.tax_total and sale.tax_total > 0:
            p.drawRightString(190 * mm, y, f"НАЛОГ: {fmt_money(sale.tax_total)}")
            y -= 6 * mm
        p.drawRightString(190 * mm, y, f"ИТОГО К ОПЛАТЕ: {fmt_money(sale.total)}")

        y -= 20
        _set_font(p, "DejaVu", 10, fallback="Helvetica")
        p.drawString(20 * mm, y, "Продавец: _____________")
        p.drawString(120 * mm, y, "Покупатель: _____________")

        p.showPage()
        p.save()

        buffer.seek(0)
        return FileResponse(buffer, as_attachment=True, filename=f"invoice_{doc_no}.pdf")


def _serialize_sale_payments(sale):
    lines = sale.payment_lines()
    out = []
    for line in lines:
        try:
            method_display = line.get_method_display()
        except Exception:
            method_display = line.method
        out.append(
            {
                "method": line.method,
                "method_display": method_display,
                "amount": fmt_money(line.amount),
            }
        )
    return out


class SaleReceiptDataAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        sale = get_object_or_404(
            Sale.objects.select_related("company", "user").prefetch_related("items", "payments"),
            id=pk,
            company=request.user.company,
        )
        cashier_override = (request.query_params.get("cashier_name") or "").strip()
        cashier_name = cashier_override if cashier_override else None
        from apps.main.printers import build_receipt_payload
        from apps.ekassa.sale_bridge import wait_for_pos_sale_ekassa

        if _truthy_query_param(request.query_params.get("wait_ekassa")):
            wait_for_pos_sale_ekassa(sale.pk)
            sale.refresh_from_db()

        payload = build_receipt_payload(sale, cashier_name=cashier_name, ensure_number=True)

        if _truthy_query_param(request.query_params.get("receipt_text")):
            payload["receipt_text"] = _build_physical_receipt_text(
                sale,
                payment_method=sale.payment_method,
                cash_received=sale.cash_received,
                change=sale.change,
                include_shift=bool(getattr(sale, "shift_id", None)),
            )

        return Response(payload, status=200)


class SaleStartAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @idempotent(body_key=True)
    def post(self, request, *args, **kwargs):
        user = request.user
        company = self._company() or user.company
        branch = self._auto_branch()

        cashbox_id = request.data.get("cashbox_id")
        requested_shift_id = request.data.get("shift_id") or request.data.get("shift")
        # opening_cash НЕ используется тут намеренно:
        # смена должна быть открыта отдельным действием, а start не открывает её автоматически.

        if requested_shift_id:
            shift = _resolve_requested_open_shift(
                company=company,
                cashier=user,
                shift_id=requested_shift_id,
                cashbox_id=cashbox_id,
            )
            cashbox = shift.cashbox
            branch = shift.branch
        else:
            cashbox = _resolve_pos_cashbox(company, branch, cashbox_id=cashbox_id)
            if not cashbox and not cashbox_id:
                shift = _find_open_shift_for_cashier(company=company, cashier=user, branch=branch)
                if shift:
                    cashbox = shift.cashbox
                    branch = shift.branch
                else:
                    raise ValidationError({"detail": "Нет кассы для этого филиала. Создай Cashbox."})
            elif not cashbox:
                raise ValidationError({"detail": "Нет кассы для этого филиала. Создай Cashbox."})

            shift = _find_open_shift_for_cashier(company=company, cashier=user, cashbox=cashbox)
            if not shift and not cashbox_id:
                shift = _find_open_shift_for_cashier(company=company, cashier=user, branch=branch)
                if shift:
                    cashbox = shift.cashbox
                    branch = shift.branch

        if not shift:
            raise ValidationError(
                {"detail": "Смена не открыта. Сначала откройте смену на кассе, затем начните продажу.", "code": "shift_closed"}
            )

        if not cashbox:
            cashbox = shift.cashbox
            if shift:
                branch = shift.branch

        opts = StartCartOptionsSerializer(data=_normalize_pos_request_data(request.data))
        opts.is_valid(raise_exception=True)
        is_wholesale_req = (
            bool(opts.validated_data.get("is_wholesale"))
            if "is_wholesale" in opts.validated_data
            else None
        )
        is_new = bool(opts.validated_data.get("is_new"))
        requested_sale_id = opts.validated_data.get("sale_id")

        with transaction.atomic():
            cart = None
            created = False

            if requested_sale_id:
                cart = _find_locked_shift_cart(
                    company=company,
                    user=user,
                    shift=shift,
                    sale_id=requested_sale_id,
                )
                if not cart:
                    raise ValidationError({"sale_id": "Открытая корзина не найдена в этой смене."})
            elif is_new:
                open_count = _shift_carts_base_qs(company, user, shift).count()
                if open_count >= MAX_OPEN_CARTS_PER_SHIFT:
                    raise ValidationError(
                        {"detail": f"Достигнут лимит открытых корзин ({MAX_OPEN_CARTS_PER_SHIFT})."}
                    )
                cart = Cart.objects.create(
                    company=company,
                    user=user,
                    status=Cart.Status.ACTIVE,
                    branch=branch or shift.branch,
                    shift=shift,
                    is_default=False,
                    is_wholesale=bool(is_wholesale_req) if is_wholesale_req is not None else False,
                )
                created = True
            else:
                cart = _find_locked_shift_cart(company=company, user=user, shift=shift)
                if cart is None:
                    try:
                        with transaction.atomic():
                            cart = Cart.objects.create(
                                company=company,
                                user=user,
                                status=Cart.Status.ACTIVE,
                                branch=branch or shift.branch,
                                shift=shift,
                                is_default=True,
                                is_wholesale=bool(is_wholesale_req) if is_wholesale_req is not None else False,
                            )
                            created = True
                    except IntegrityError:
                        # Race condition fallback
                        cart = _find_locked_shift_cart(company=company, user=user, shift=shift)
                        if cart is None:
                            raise ValidationError({"detail": "Ошибка при создании корзины. Попробуйте еще раз."})
                elif (branch or shift.branch) and cart.branch_id != getattr(shift.branch, "id", None):
                    cart.branch = branch or shift.branch
                    cart.save(update_fields=["branch"])
                elif not _shift_carts_base_qs(company, user, shift).filter(is_default=True).exists():
                    cart.is_default = True
                    cart.save(update_fields=["is_default", "updated_at"])

            # recalc() делаем один раз — в конце (после применения скидок/опта).
            # Раньше здесь был лишний recalc() на каждый start.
            update_f = []
            wholesale_changed = False

            if _pos_body_has_explicit_field(
                request,
                "order_discount_total",
                "orderDiscountTotal",
                "order_discount_percent",
                "orderDiscountPercent",
            ):
                order_disc_total = opts.validated_data.get("order_discount_total")
                order_disc_percent = opts.validated_data.get("order_discount_percent")
                
                is_admin = getattr(request.user, "role", None) in ["owner", "admin"]
                max_dp = request.user.company.max_discount_percent
                if max_dp is not None and not is_admin:
                    if order_disc_percent is not None and Decimal(str(order_disc_percent)) > max_dp:
                        return Response(_discount_limit_error(max_dp), status=400)
                    if order_disc_total is not None and getattr(cart, "total_price", 0) > 0: # Approximation before recalc or skip
                        pass # Validated on recalc or here if subtotal available, but CartStart creates cart, maybe no subtotal yet.
                        # Actually CartStart sets discount on empty cart, so total is 0. Skip total check here.
                
                if order_disc_percent is not None:
                    cart.order_discount_percent = _q2(Decimal(str(order_disc_percent)))
                    cart.order_discount_total = Decimal("0.00")
                else:
                    cart.order_discount_percent = None
                    cart.order_discount_total = _q2(order_disc_total or Decimal("0.00"))
                update_f.extend(["order_discount_total", "order_discount_percent"])

            if _pos_body_has_explicit_field(request, "is_wholesale", "isWholesale"):
                is_wholesale = bool(opts.validated_data.get("is_wholesale"))
                if getattr(cart, "is_wholesale", False) != is_wholesale:
                    cart.is_wholesale = is_wholesale
                    update_f.append("is_wholesale")
                    wholesale_changed = True

            if update_f:
                update_f.append("updated_at")
                cart.save(update_fields=update_f)
            if created or wholesale_changed:
                _reprice_cart_items_for_mode(cart)

            cart.recalc()
            cart_id = cart.id

        cart = get_object_or_404(_cart_queryset_for_response(), id=cart_id, company=company)
        return _pos_multi_cart_response(request, cart, status_code=status.HTTP_201_CREATED)


def _cart_line_discounts_total(cart) -> Decimal:
    return cart.items.aggregate(s=Sum("line_discount"))["s"] or Decimal("0.00")


def _order_discount_over_limit(cart, order_discount_total, max_dp) -> bool:
    """Скидка на чек суммой больше max_dp% от суммы после скидок по строкам."""
    if max_dp is None:
        return False
    cart.recalc()
    base = (cart.subtotal or Decimal("0.00")) - _cart_line_discounts_total(cart)
    limit = money(max(base, Decimal("0.00")) * Decimal(str(max_dp)) / Decimal("100"))
    return Decimal(str(order_discount_total)) > limit


def _discount_limit_error(max_dp) -> dict:
    """Единый ответ 400 на скидку сверх лимита кассира: касса узнаёт его по code."""
    pct = format(Decimal(str(max_dp)).normalize(), "f")
    return {
        "detail": f"Скидка больше разрешённой ({pct}%), нужно разрешение владельца",
        "code": "discount_limit",
        "max_discount_percent": str(max_dp),
    }


def _line_discount_over_limit(unit_price, quantity, line_discount, max_dp) -> bool:
    """Скидка на строку больше max_dp% от суммы строки (цена × количество)."""
    if max_dp is None:
        return False
    base = Decimal(str(unit_price or 0)) * Decimal(str(quantity or 0))
    limit = money(max(base, Decimal("0.00")) * Decimal(str(max_dp)) / Decimal("100"))
    return Decimal(str(line_discount or 0)) > limit


class CartDetailAPIView(MarketCashierOnlyMixin, generics.RetrieveAPIView):
    serializer_class = SaleCartSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return _cart_queryset_for_response().filter(company=self.request.user.company)

    def patch(self, request, *args, **kwargs):
        """Обновить скидку на чек: order_discount_total или order_discount_percent."""
        cart = self.get_object()
        if cart.status != Cart.Status.ACTIVE:
            return Response({"detail": "Корзина не активна."}, status=400)
        opts = StartCartOptionsSerializer(data=request.data, partial=True)
        opts.is_valid(raise_exception=True)
        order_disc_total = opts.validated_data.get("order_discount_total")
        order_disc_percent = opts.validated_data.get("order_discount_percent")
        
        is_admin = getattr(request.user, "role", None) in ["owner", "admin"]
        max_dp = request.user.company.max_discount_percent
        if max_dp is not None and not is_admin:
            if order_disc_percent is not None and Decimal(str(order_disc_percent)) > max_dp:
                return Response(_discount_limit_error(max_dp), status=400)
            if order_disc_total is not None and _order_discount_over_limit(cart, order_disc_total, max_dp):
                return Response(_discount_limit_error(max_dp), status=400)
                    
        if order_disc_percent is not None:
            cart.order_discount_percent = _q2(Decimal(str(order_disc_percent)))
            cart.order_discount_total = Decimal("0.00")
        elif order_disc_total is not None:
            cart.order_discount_percent = None
            cart.order_discount_total = _q2(order_disc_total)
        if order_disc_total is not None or order_disc_percent is not None:
            cart.save(update_fields=["order_discount_total", "order_discount_percent", "updated_at"])
        cart.recalc()
        return _cart_response(request, cart.id, status_code=200)


class SaleScanAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        url_cart = get_object_or_404(
            Cart.objects.select_related("shift", "user"),
            id=pk,
            company=request.user.company,
            status=Cart.Status.ACTIVE,
        )
        if not url_cart.shift_id:
            raise ValidationError({"detail": "Корзина не привязана к смене."})

        ser = ScanRequestSerializer(data=_normalize_pos_request_data(request.data))
        ser.is_valid(raise_exception=True)

        sale_id = ser.validated_data.get("sale_id")
        barcode = ser.validated_data["barcode"].strip()
        qty = ser.validated_data["quantity"]

        from apps.main.tz26.hw_units.units import find_sale_unit_by_barcode, scan_sale_unit_response

        _unit = find_sale_unit_by_barcode(url_cart.company_id, barcode)  # ТЗ 26.10 Б-53: скан коробки/мешка
        if _unit is not None:
            return scan_sale_unit_response(request, url_cart, _unit, qty)

        variant = (
            ProductVariant.objects.select_related("product")
            .filter(company_id=url_cart.company_id, barcode=barcode, is_active=True)
            .first()
        )
        if variant is not None:
            product, scale_data = variant.product, None
        else:
            try:
                product, scale_data, lookup_error = _lookup_product_for_pos_scan(url_cart.company_id, barcode)
            except AmbiguousBarcode as exc:
                return _ambiguous_barcode_response(exc)
            if not product:
                return Response({"not_found": True, "message": lookup_error or "Товар не найден"}, status=404)

        effective_qty = _effective_qty_from_scale_data(scale_data, qty)
        if variant is None:  # ТЗ 26.10 Б-52: шаг и минимум (под флагом hardware_2026_11)
            from apps.main.tz26.hw_units.services import check_line_quantity

            check_line_quantity(request, url_cart.company, product, effective_qty)

        with transaction.atomic():
            cart = _lock_pos_target_cart(
                company=request.user.company,
                user=request.user,
                shift=url_cart.shift,
                sale_id=sale_id,
            )
            _upsert_scanned_cart_item(cart, product, effective_qty, variant=variant)
            cart.recalc()
            cart_id = cart.id

        cart = get_object_or_404(_cart_queryset_for_response(), id=cart_id, company=request.user.company)
        return _pos_multi_cart_response(request, cart, status_code=status.HTTP_201_CREATED)


class SaleAddItemAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        cart = get_object_or_404(
            Cart.objects.select_related("company", "branch", "user", "shift"),
            id=pk,
            company=request.user.company,
            status=Cart.Status.ACTIVE,
        )
        ser = AddItemSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        product = get_object_or_404(
            Product,
            id=ser.validated_data["product_id"],
            company_id=cart.company_id,
        )
        qty = ser.validated_data["quantity"]
        allow_minus = bool(ser.validated_data.get("allow_minus"))
        can_minus = allow_minus and _is_owner_like(request.user)
        if ser.validated_data.get("sale_unit_id"):  # ТЗ 26.10 Б-53 п.4: строка в единице продажи
            from apps.main.tz26.hw_units.units import add_sale_unit_item_response

            return add_sale_unit_item_response(request, cart, product, ser.validated_data, can_minus=can_minus)
        variant = None
        if ser.validated_data.get("variant_id"):
            variant = get_object_or_404(
                ProductVariant, id=ser.validated_data["variant_id"], product_id=product.id, company_id=cart.company_id
            )

        unit_price = ser.validated_data.get("unit_price")
        if unit_price is None and variant is not None and variant.price is not None:
            unit_price = variant.price
        line_discount = ser.validated_data.get("discount_total")
        discount_percent = ser.validated_data.get("discount_percent")
        sale_package_id = ser.validated_data.get("sale_package_id")
        pkg = None
        if sale_package_id:
            pkg = get_object_or_404(
                ProductPackage.objects.filter(
                    id=sale_package_id,
                    product_id=product.id,
                    company_id=cart.company_id,
                )
            )

        # unit_price — база, line_discount — скидка на строку (хранятся отдельно)
        if unit_price is not None:
            base_price = _q2(unit_price)
        else:
            if getattr(cart, "is_wholesale", False):
                raw_wholesale = getattr(product, "wholesale_price", None)
                raw_retail = getattr(product, "price", None)
                pack_price = Decimal(str(raw_wholesale)) if raw_wholesale not in (None, 0, "0") else Decimal(str(raw_retail or 0))
                if pkg:
                    ipp = Decimal(str(pkg.quantity_in_package or 0))
                    base_price = _q2(pack_price / ipp) if ipp > 0 else _q2(pack_price)
                else:
                    base_price = _q2(pack_price)
            else:
                base_price = _q2(default_unit_price_for_package(product, pkg))
        disc_total = _line_discount_from_request(base_price, qty, line_discount, discount_percent)

        is_admin = getattr(request.user, "role", None) in ["owner", "admin"]
        max_dp = request.user.company.max_discount_percent
        if not is_admin and _line_discount_over_limit(base_price, qty, disc_total, max_dp):
            return Response(_discount_limit_error(max_dp), status=status.HTTP_400_BAD_REQUEST)

        # Цена продажи не ниже закупочной, кроме случая со скидкой (со скидкой можно ниже)
        if disc_total <= 0:
            min_price = _q2(Decimal(str(getattr(product, "purchase_price", None) or 0)))
            if pkg:
                ipp = Decimal(str(pkg.quantity_in_package or 0))
                min_price = _q2(min_price / ipp) if ipp > 0 else min_price
            qty_dec = Decimal(str(qty))
            effective_unit = base_price - (disc_total / qty_dec) if qty_dec else base_price
            if effective_unit < min_price:
                return Response(
                    {"unit_price": f"Цена продажи не может быть ниже закупочной ({min_price})."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Блокируем корзину для предотвращения race conditions
        cart = Cart.objects.select_for_update().get(id=cart.id)

        other = total_cart_consume_packs_for_product(cart.id, product.id)
        target = (
            CartItem.objects.select_for_update()
            .filter(cart=cart, product=product, sale_package=pkg, variant=variant)
            .first()
        )
        if target:
            other = qty3(other - line_qty_consume_units(target.quantity, pkg))
            combined_consume = line_qty_consume_units(target.quantity + qty, pkg)
        else:
            combined_consume = line_qty_consume_units(qty, pkg)
        if pkg is None:  # ТЗ 26.10 Б-52: шаг и минимум (под флагом hardware_2026_11)
            from apps.main.tz26.hw_units.services import check_line_quantity

            check_line_quantity(request, cart.company, product, (target.quantity + qty) if target else qty)
        have = Decimal(str(product.quantity or 0))
        if product.kind != Product.Kind.SERVICE and (not can_minus) and qty3(other + combined_consume) > have:
            return Response(
                {
                    "detail": (
                        "Недостаточно остатка (учёт в пачках). "
                        f"Доступно не более {qty3(max(Decimal('0'), have - other))} условных пачек с учётом корзины."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if target:
            item = target
            item.quantity = qty3(item.quantity + qty)
            if unit_price is not None:
                item.unit_price = base_price
            if line_discount is not None or discount_percent is not None:
                item.manual_discount = item.manual_discount_base() + disc_total
                item.line_discount = item.manual_discount
            update_f = ["quantity"]
            if unit_price is not None:
                update_f.append("unit_price")
            if line_discount is not None or discount_percent is not None:
                update_f += ["line_discount", "manual_discount"]
            item.save(update_fields=update_f, skip_full_clean=True)
        else:
            item = CartItem(
                cart=cart,
                company=cart.company,
                branch=getattr(cart, "branch", None),
                product=product,
                sale_package=pkg,
                variant=variant,
                quantity=qty3(qty),
                unit_price=base_price,
                line_discount=disc_total,
                manual_discount=disc_total,
            )
            item.save(skip_full_clean=True)

        cart.recalc()
        resp = _cart_response(request, cart.id, status_code=status.HTTP_201_CREATED)
        # Возвращаем UUID конкретной добавленной/обновлённой строки, чтобы фронт
        # PATCH-ил именно её (штучную/упаковочную), а не падал в поиск по product_id.
        if isinstance(resp.data, dict):
            resp.data["added_item_id"] = str(item.id)
            resp.data["added_item"] = {
                "id": str(item.id),
                "product": str(product.id),
                "sale_package": str(pkg.id) if pkg else None,
            }
        return resp


def _checkout_value_error_body(e, cart, cash_received=None) -> dict:
    """ТЗ Н-15: машинный код к ValueError из checkout_cart/mark_paid; `detail` остаётся как был."""
    msg = str(e)
    body = {"detail": msg}
    code = None
    if msg.startswith("Сумма оплат"):
        code = "mixed_sum_mismatch"
        body["total"] = fmt_money(cart.total)
    elif msg.startswith("Сумма, полученная наличными"):
        code = "cash_less_than_total"
        body["total"] = fmt_money(cart.total)
        body["cash_received"] = fmt_money(cash_received or Decimal("0.00"))
    elif "имеет размеры/цвета" in msg:
        code = "variant_required"
    elif msg == "Вариант позиции не найден.":
        code = "variant_not_found"
    elif msg == "Товар позиции не найден.":
        code = "product_not_found"
    if code:
        body["code"] = code
        body["errors"] = [{"index": None, "code": code, "detail": msg}]
    return body


def _raise_checkout_serializer_error(errors, cart, data):
    """ТЗ Н-15: ошибки оплаты получают коды mixed_sum_mismatch / cash_less_than_total, ключи полей — прежние."""
    from core.exceptions import ApiError, _flatten_fields

    def _texts(key):
        v = errors.get(key)
        if isinstance(v, (list, tuple)):
            return " ".join(str(x) for x in v)
        return str(v or "")

    code = None
    extra = {}
    if "должна равняться сумме чека" in _texts("payments"):
        code = "mixed_sum_mismatch"
        extra = {"total": fmt_money(cart.total)}
    elif "меньше наличной части" in _texts("cash_received"):
        code = "cash_less_than_total"
        raw = data.get("cash_received") if hasattr(data, "get") else None
        try:
            received = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            received = Decimal("0.00")
        extra = {"total": fmt_money(cart.total), "cash_received": fmt_money(received)}
    if code is None:
        raise ValidationError(errors)
    detail = _texts("payments") or _texts("cash_received")
    top = dict(errors)
    top.update(extra)
    top["errors"] = [{"index": None, "code": code, "detail": detail, **extra}]
    raise ApiError(code, detail, fields=_flatten_fields(dict(errors)), top=top)


class SaleCheckoutAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @idempotent(body_key=True)
    def post(self, request, pk, *args, **kwargs):
        return self.checkout(request, pk, request.data)

    def checkout(self, request, pk, data, *, allow_offset=False, force_negative_stock=False, lenient_variants=False):
        # ТЗ Н-15: force_negative_stock/lenient_variants — только для офлайн-досылки (pos/checkout/ с offline=true).
        with transaction.atomic():
            cart = get_object_or_404(
                Cart.objects.select_related("company", "branch", "user", "shift"),
                id=pk,
                company=request.user.company,
                status=Cart.Status.ACTIVE,
            )

            ser = CheckoutSerializer(
                data=data, context={"request": request, "cart": cart, "allow_offset": allow_offset}
            )
            if not ser.is_valid():
                _raise_checkout_serializer_error(ser.errors, cart, data)

            print_receipt = ser.validated_data["print_receipt"]
            allow_minus = bool(ser.validated_data.get("allow_minus"))
            can_minus = (allow_minus and _is_owner_like(request.user)) or bool(force_negative_stock)
            client_id = ser.validated_data.get("client_id")
            payment_method = ser.validated_data.get("payment_method") or Sale.PaymentMethod.CASH
            cash_received = ser.validated_data.get("cash_received") or Decimal("0.00")
            payments = ser.validated_data.get("payments") or None
            cashbox_id = ser.validated_data.get("cashbox_id")

            if not cart.shift_id:
                company = cart.company
                branch = getattr(cart, "branch", None)
                cashbox = _resolve_pos_cashbox(company, branch, cashbox_id=cashbox_id)
                if not cashbox and not cashbox_id:
                    shift = _find_open_shift_for_cashier(company=company, cashier=request.user, branch=branch)
                    if shift:
                        cashbox = shift.cashbox
                        branch = shift.branch
                    else:
                        raise ValidationError({"detail": "Нет кассы для этого филиала. Создай Cashbox."})
                elif not cashbox:
                    raise ValidationError({"detail": "Нет кассы для этого филиала. Создай Cashbox."})

                shift = _find_open_shift_for_cashier(company=company, cashier=request.user, cashbox=cashbox)
                if not shift and not cashbox_id:
                    shift = _find_open_shift_for_cashier(company=company, cashier=request.user, branch=branch)
                    if shift:
                        cashbox = shift.cashbox
                        branch = shift.branch

                if not shift:
                    raise ValidationError(
                        {"detail": "Смена не открыта. Сначала откройте смену на кассе, затем завершите продажу.", "code": "shift_closed"}
                    )
                cart.shift = shift
                if (branch or shift.branch) and cart.branch_id != getattr(shift.branch, "id", None):
                    cart.branch = branch or shift.branch
                    cart.save(update_fields=["shift", "branch"])
                else:
                    cart.save(update_fields=["shift"])

            hw_reprice_for_request(request, cart, client_id)  # ТЗ 26.10 Б-55: цена по уровню клиента (под флагом)
            cart.recalc()
            if not payments and payment_method == Sale.PaymentMethod.CASH and cash_received < cart.total:
                # ТЗ Н-15: код и суммы; старый ключ detail сохранён
                transaction.set_rollback(True)
                return Response(
                    {
                        "detail": "Сумма, полученная наличными, меньше суммы продажи.",
                        "code": "cash_less_than_total",
                        "total": fmt_money(cart.total),
                        "cash_received": fmt_money(cash_received),
                        "errors": [{"index": None, "code": "cash_less_than_total",
                                    "total": fmt_money(cart.total), "cash_received": fmt_money(cash_received)}],
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            client_obj = None
            if client_id:
                client_obj = get_object_or_404(Client, id=client_id, company=request.user.company)

            consultant_obj = ser.validated_data.get("consultant_obj")
            consultant_comm_enabled = ser.validated_data.get("consultant_commission_enabled", False)
            consultant_comm_pct = ser.validated_data.get("consultant_commission_percent")
            cash_amount = ser.validated_data.get("cash_amount")
            card_amount = ser.validated_data.get("card_amount")
            card_method = ser.validated_data.get("card_method")
            prepayment_method = ser.validated_data.get("prepayment_method")

            # ТЗ 26.10 Б-61: партии и отрезной товар (None — нет ни партий, ни отрезов)
            from apps.main.tz26.hw_batches.services import commit_checkout, plan_checkout
            batches_plan = plan_checkout(cart, getattr(request, "data", None))

            try:
                sale = checkout_cart(
                    cart,
                    allow_negative_stock=can_minus,
                    payments=payments,
                    payment_method=None if payments else payment_method,
                    cash_received=cash_received,
                    cash_amount=cash_amount,
                    card_amount=card_amount,
                    card_method=card_method,
                    prepayment_method=prepayment_method,
                    client=client_obj,
                    consultant=consultant_obj,
                    consultant_commission_enabled=consultant_comm_enabled,
                    consultant_commission_percent=consultant_comm_pct,
                    lenient_variants=lenient_variants,
                )
            except NotEnoughStock as e:
                transaction.set_rollback(True)
                errs = [
                    {"index": None, "code": "insufficient_stock", "detail": str(e),
                     "product_id": str(sh["product_id"]),
                     "variant_id": str(sh["variant_id"]) if sh.get("variant_id") else None,
                     "available": str(sh["available"]), "requested": str(sh["requested"])}
                    for sh in getattr(e, "shortages", [])
                ] or [{"index": None, "code": "insufficient_stock", "detail": str(e)}]
                return Response(
                    {"detail": str(e), "code": "insufficient_stock", "errors": errs},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            except ValueError as e:
                transaction.set_rollback(True)
                return Response(_checkout_value_error_body(e, cart, cash_received), status=status.HTTP_400_BAD_REQUEST)

            commit_checkout(batches_plan, sale, request.user)  # ТЗ 26.10 Б-61
            hw_after_checkout(request, cart, sale, client_obj)  # ТЗ 26.10 Б-55, Б-56: цены строк, объект, лимит долга

            cons_name = None
            if sale.consultant:
                cons_name = (
                    getattr(sale.consultant, "get_full_name", lambda: "")()
                    or getattr(sale.consultant, "full_name", None)
                    or getattr(sale.consultant, "username", None)
                )

            payload = {
                "sale_id": str(sale.id),
                "number": sale.doc_number,  # ТЗ Н-15: постоянный номер чека во всех ответах
                "status": sale.status,
                "subtotal": fmt_money(sale.subtotal),
                "discount_total": fmt_money(sale.discount_total),
                "tax_total": fmt_money(sale.tax_total),
                "total": fmt_money(sale.total),
                "user": str(sale.user_id) if sale.user_id else None,
                "user_display": (
                    getattr(sale.user, "get_full_name", lambda: "")()
                    or getattr(sale.user, "email", None)
                    or getattr(sale.user, "username", None)
                ) if sale.user else None,
                "consultant": str(sale.consultant_id) if sale.consultant_id else None,
                "consultant_display": cons_name,
                "consultant_commission_enabled": sale.consultant_commission_enabled,
                "consultant_commission_percent": fmt_money(sale.consultant_commission_percent) if sale.consultant_commission_percent is not None else None,
                "consultant_commission_amount": fmt_money(sale.consultant_commission_amount or Decimal("0.00")),
                "client": str(sale.client_id) if sale.client_id else None,
                "client_name": getattr(sale.client, "full_name", None) if sale.client else None,
                "payment_method": sale.payment_method,
                "cash_received": fmt_money(sale.cash_received),
                "change": fmt_money(sale.change),
                "paid_now": fmt_money(sale.paid_now),
                "paid_cash": fmt_money(sale.cash_amount),
                "paid_card": fmt_money(sale.card_amount),
                "cash_amount": fmt_money(sale.cash_amount),
                "card_amount": fmt_money(sale.card_amount),
                "debt_initial": fmt_money(sale.debt_initial),
                "debt_remaining": fmt_money(sale.debt_remaining),
                "debt_amount": fmt_money(sale.debt_remaining),
                "payments": _serialize_sale_payments(sale),
                "shift_id": str(sale.shift_id) if sale.shift_id else None,
                "cashbox_id": str(sale.cashbox_id) if sale.cashbox_id else None,
            }

            from apps.construction.auto_cashflow import create_auto_cashflow, serialize_auto_cashflows
            from apps.construction.models import CashFlow

            auto_flows = []
            if payments:
                for p_idx, p_part in enumerate(payments):
                    p_amt = Decimal(str(p_part.get("amount") or 0))
                    p_method = p_part.get("method") or "cash"
                    # Долговая часть — это не деньги в кассе (придут при погашении),
                    # зачёт уже проведён раньше (возврат при обмене, залог, предоплата).
                    if p_method in (Sale.PaymentMethod.DEBT, Sale.PaymentMethod.OFFSET):
                        continue
                    if p_amt > 0:
                        p_title = dict(Sale.PaymentMethod.choices).get(p_method, p_method)
                        cf = create_auto_cashflow(
                            company=sale.company,
                            branch=sale.branch,
                            cashbox=sale.cashbox,
                            cashbox_id=cashbox_id,
                            user=request.user,
                            shift=sale.shift,
                            type=CashFlow.Type.INCOME,
                            amount=p_amt,
                            source_kind=CashFlow.SourceKind.POS_SALE,
                            source_id=str(sale.id),
                            # Ключ на каждую строку: иначе 500 нал + 500 Мбанк склеивались в одно движение
                            idempotency_key=f"sale:{sale.id}:pay:{p_method}:{p_idx}",
                            name=f"Продажа ({p_title})",
                            source_business_operation_id="Продажа",
                            affects_shift_drawer=(p_method == Sale.PaymentMethod.CASH),
                            payment_method=p_method,
                        )
                        if cf:
                            auto_flows.append(cf)
            elif sale.payment_method == Sale.PaymentMethod.DEBT:
                cash_prepay = sale.cash_amount if sale.cash_amount > 0 else (sale.cash_received or Decimal("0.00"))
                if cash_prepay > 0:
                    cf = create_auto_cashflow(
                        company=sale.company,
                        branch=sale.branch,
                        cashbox=sale.cashbox,
                        cashbox_id=cashbox_id,
                        user=request.user,
                        shift=sale.shift,
                        type=CashFlow.Type.INCOME,
                        amount=cash_prepay,
                        source_kind=CashFlow.SourceKind.POS_PREPAYMENT,
                        source_id=str(sale.id),
                        idempotency_key=f"sale:{sale.id}:prepay:cash",
                        name="Предоплата (долг)",
                        source_business_operation_id="Продажа",
                        affects_shift_drawer=True,
                        payment_method="cash",
                    )
                    if cf:
                        auto_flows.append(cf)
                if (sale.card_amount or Decimal("0.00")) > 0:
                    noncash_m = prepayment_method or card_method or "transfer"
                    p_title = dict(Sale.PaymentMethod.choices).get(noncash_m, noncash_m)
                    cf = create_auto_cashflow(
                        company=sale.company,
                        branch=sale.branch,
                        cashbox=sale.cashbox,
                        cashbox_id=cashbox_id,
                        user=request.user,
                        shift=sale.shift,
                        type=CashFlow.Type.INCOME,
                        amount=sale.card_amount,
                        source_kind=CashFlow.SourceKind.POS_PREPAYMENT,
                        source_id=str(sale.id),
                        idempotency_key=f"sale:{sale.id}:prepay:{noncash_m}",
                        name=f"Предоплата ({p_title})",
                        source_business_operation_id="Продажа",
                        affects_shift_drawer=False,
                        payment_method=noncash_m,
                    )
                    if cf:
                        auto_flows.append(cf)
            else:
                if (sale.total or 0) > 0 and sale.payment_method != Sale.PaymentMethod.OFFSET:
                    method_title = sale.get_payment_method_display() if hasattr(sale, "get_payment_method_display") else sale.payment_method
                    cf_name = f"Продажа ({method_title})" if sale.payment_method != Sale.PaymentMethod.CASH else "Продажа"
                    cf = create_auto_cashflow(
                        company=sale.company,
                        branch=sale.branch,
                        cashbox=sale.cashbox,
                        cashbox_id=cashbox_id,
                        user=request.user,
                        shift=sale.shift,
                        type=CashFlow.Type.INCOME,
                        amount=sale.total,
                        source_kind=CashFlow.SourceKind.POS_SALE,
                        source_id=str(sale.id),
                        idempotency_key=f"sale:{sale.id}:pay:{sale.payment_method}",
                        name=cf_name,
                        source_business_operation_id="Продажа",
                        payment_method=sale.payment_method,
                    )
                    if cf:
                        auto_flows.append(cf)

            payload["cashflows"] = serialize_auto_cashflows(auto_flows)
            cb_resolved_id = str(sale.cashbox_id) if sale.cashbox_id else None
            cb_resolved_name = getattr(sale.cashbox, "name", None) or ("Касса филиала" if getattr(sale.cashbox, "branch_id", None) else "Касса") if sale.cashbox else "Касса"
            payload["resolved_cashbox_id"] = cb_resolved_id
            payload["resolved_cashbox_name"] = cb_resolved_name

            if print_receipt:
                payload["receipt_print_path"] = (
                    f"/api/main/pos/sales/{sale.id}/receipt/?wait_ekassa=1&receipt_text=1"
                )

            if cart.shift_id:
                remaining_qs = _shift_active_carts_qs(cart.company, request.user, cart.shift)
                ordered = list(remaining_qs)
                if ordered:
                    active_open = next((c for c in ordered if c.is_default), ordered[0])
                    active_open = get_object_or_404(
                        _cart_queryset_for_response(),
                        id=active_open.id,
                        company=request.user.company,
                    )
                    payload["active_sale_id"] = str(active_open.id)
                    payload["sale"] = _serialize_pos_sale(request, active_open)
                    payload["carts"] = [_serialize_pos_cart_tab(c, ordered) for c in ordered]

        notify_sale_created(sale, actor=request.user)

        hint = _ekassa_checkout_hint(sale.company)
        if hint:
            payload["ekassa"] = hint

        return Response(payload, status=status.HTTP_201_CREATED)


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


class SalePayDebtAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    """
    Погасить долг конкретной продажи одним запросом (ТЗ ч.12, 2.3).
    POST /api/main/pos/sales/<sale_id>/pay-debt/
    Idempotency-Key: <строка>  (или поле idempotency_key; повтор с тем же ключом ничего не создаёт)
    Body: {
      "amount": "40.00",            # необязательно; по умолчанию — весь остаток
      "payment_method": "cash",     # обязательно (ТЗ Н-13: можно и под именем "method"; разные → 400 field_conflict)
      "shift": "<id смены>",        # необязательно; по умолчанию — открытая смена кассира
      "cashbox_id": "<id кассы>"    # необязательно
    }
    200 → {"paid": "40.00", "left": "0.00", "sale": {...}, "cashflows": [...],
           "method", "payment_method", "received_by": {"id", "name"}, "received_at", "shift", "cashbox"}
    400 → {"detail": "Долг по продаже уже погашен", "code": "no_debt"}

    Без `shift` платёж привязывается к открытой смене кассира (как и раньше); «вне смены»
    не делаем, чтобы не менять expected_cash у действующих касс (ТЗ Н-13, см. `shift` в ответе).
    Деньги гасят взносы сделки продажи по порядку; в кассу пишется один приход на всю сумму,
    наличные попадают в смену один раз (debt_payments_cash / expected_cash).
    """

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        from uuid import NAMESPACE_URL, uuid5
        from apps.construction.auto_cashflow import create_auto_cashflow, serialize_auto_cashflows
        from apps.construction.models import CashFlow
        from apps.main.models import ClientDeal, DealInstallment, DealPayment
        from apps.main.services_debt import (
            VALID_DEBT_PAYMENT_METHODS, normalize_debt_payment_method, pick_debt_method,
        )

        qs = self._filter_qs_company_branch(Sale.objects.select_for_update(of=("self",)))
        sale = get_object_or_404(qs, id=pk)

        data = request.data if isinstance(request.data, dict) else {}
        errors = {}
        allowed_pm = set(VALID_DEBT_PAYMENT_METHODS)
        # ТЗ Н-13: имя поля method или payment_method; card — синоним transfer
        raw_pm = pick_debt_method(data) or ""
        if raw_pm == "card":
            raw_pm = "transfer"
        error_code = None
        if not raw_pm:
            errors["payment_method"] = ["Укажите способ оплаты."]
            error_code = "method_required"
        elif raw_pm not in allowed_pm:
            errors["payment_method"] = [f"Неизвестный способ оплаты. Допустимо: {', '.join(sorted(allowed_pm))}."]
            error_code = "method_not_allowed"
        amount = None
        if data.get("amount") not in (None, ""):
            try:
                amount = Decimal(str(data.get("amount"))).quantize(Decimal("0.01"))
                if amount <= 0:
                    errors["amount"] = ["Сумма должна быть больше 0."]
                    error_code = error_code or "amount_invalid"
            except Exception:
                errors["amount"] = ["Некорректная сумма."]
                error_code = error_code or "amount_invalid"
        if errors:
            body = dict(errors)
            body["code"] = error_code
            if error_code == "method_not_allowed":
                body["data"] = {"allowed": sorted(allowed_pm)}
            return Response(body, status=status.HTTP_400_BAD_REQUEST)

        is_debt_sale = sale.status == Sale.Status.DEBT or sale.payment_method == Sale.PaymentMethod.DEBT
        if not is_debt_sale or sale.status not in (Sale.Status.DEBT, Sale.Status.PAID):
            return Response({"detail": "У продажи нет долга.", "code": "no_debt"}, status=status.HTTP_400_BAD_REQUEST)

        deal = (
            ClientDeal.objects.select_for_update()
            .filter(sale=sale, kind=ClientDeal.Kind.DEBT)
            .order_by("created_at")
            .first()
        )
        if deal is None:
            if sale.status == Sale.Status.PAID:
                return Response({"detail": "Долг по продаже уже погашен", "code": "no_debt"}, status=status.HTTP_400_BAD_REQUEST)
            if not sale.client_id:
                return Response(
                    {"detail": "У продажи в долг нет клиента — погасить её нельзя.", "code": "no_client"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            debt_left = sale.debt_remaining if (sale.debt_remaining or 0) > 0 else (sale.total or Decimal("0"))
            deal = ClientDeal.objects.create(
                company=sale.company,
                branch=sale.branch,
                client_id=sale.client_id,
                sale=sale,
                title=f"Продажа в долг №{sale.doc_number or sale.id}",
                kind=ClientDeal.Kind.DEBT,
                amount=sale.total,
                prepayment=max(Decimal("0.00"), (sale.total or Decimal("0")) - debt_left),
                debt_days=1,
                first_due_date=timezone.localdate(),
            )

        installments = list(
            DealInstallment.objects.select_for_update().filter(deal=deal).order_by("number")
        )

        raw_key = (request.headers.get("Idempotency-Key") or str(data.get("idempotency_key") or "")).strip()
        base_key = uuid5(NAMESPACE_URL, f"sale-pay-debt:{sale.id}:{raw_key}") if raw_key else uuid.uuid4()
        inst_keys = {inst.id: uuid5(base_key, str(inst.id)) for inst in installments}

        if raw_key:
            replay = DealPayment.objects.filter(deal=deal, idempotency_key__in=list(inst_keys.values()))
            if replay.exists():
                paid = replay.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
                from apps.construction.models import CashFlow as _CF
                prior_cf = list(_CF.objects.filter(
                    company=sale.company, idempotency_key=f"sale-pay-debt:{base_key}"
                ))
                return self._response(
                    request, sale, deal, paid, prior_cf, replayed=True,
                    payments=list(replay.select_related("created_by").order_by("created_at")),
                    pm=normalize_debt_payment_method(raw_pm),
                )

        remaining = deal.remaining_debt
        if remaining <= 0:
            return Response({"detail": "Долг по продаже уже погашен", "code": "no_debt"}, status=status.HTTP_400_BAD_REQUEST)
        pay_total = remaining if amount is None else amount
        if pay_total > remaining:
            return Response(
                {
                    "amount": [f"Сумма больше остатка долга. Максимум: {remaining}."],
                    "code": "amount_exceeds_debt",
                    "data": {"max": str(money(remaining))},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        pm = normalize_debt_payment_method(raw_pm)

        shift = None
        detached = False
        shift_id = data.get("shift") or data.get("shift_id")
        cashbox_id = data.get("cashbox_id") or data.get("cashbox")
        if cashbox_id and not (_is_uuid(cashbox_id) and Cashbox.objects.filter(id=cashbox_id, company=sale.company).exists()):
            return Response(
                {"detail": "Касса не найдена или не принадлежит компании.", "code": "cashbox_not_found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if shift_id:
            _req = CashShift.objects.filter(id=str(shift_id), company=sale.company).first() if _is_uuid(shift_id) else None
            if _req is not None:
                if _req.status != CashShift.Status.OPEN:
                    return Response(
                        {"detail": "Смена закрыта.", "code": "shift_closed"}, status=status.HTTP_400_BAD_REQUEST
                    )
                if _req.cashier_id != request.user.id and not _is_owner_like(request.user):
                    return Response(
                        {"detail": "Эта смена открыта другим кассиром.", "code": "shift_not_owned"},
                        status=status.HTTP_403_FORBIDDEN,
                    )
            shift = _resolve_requested_open_shift(
                company=sale.company, cashier=request.user, shift_id=shift_id, cashbox_id=cashbox_id
            )
        else:
            # ТЗ 29.10 Б-13: владелец без shift принимает «в руки» — вне смены; кассир — в свою открытую
            detached = _is_owner_like(request.user) and _debt_detached_client(request, sale.company)
            shift = None if detached else _find_open_shift_for_cashier(company=sale.company, cashier=request.user)
        if shift is None and pm == "cash" and not detached:
            from core.api_compat import is_strict as _is_strict
            return Response(
                {"detail": "Смена не открыта. Откройте смену на кассе, чтобы принять наличные.",
                 "code": "shift_required" if _is_strict(request, sale.company) else "shift_not_open"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        today = timezone.localdate()
        left = pay_total
        for inst in installments:
            if left <= 0:
                break
            inst_left = ((inst.amount or Decimal("0")) - (inst.paid_amount or Decimal("0"))).quantize(Decimal("0.01"))
            if inst_left <= 0:
                continue
            part = min(left, inst_left)
            DealPayment.objects.create(
                company=deal.company,
                branch=deal.branch,
                deal=deal,
                installment=inst,
                kind=DealPayment.Kind.PAY,
                amount=part,
                paid_date=today,
                idempotency_key=inst_keys[inst.id],
                created_by=request.user,
                note=f"Погашение долга по продаже №{sale.doc_number or sale.id}",
                payment_method=pm,
            )
            inst.paid_amount = ((inst.paid_amount or Decimal("0")) + part).quantize(Decimal("0.01"))
            inst.paid_on = today if inst.paid_amount >= inst.amount else None
            inst.save(update_fields=["paid_amount", "paid_on"])
            left = (left - part).quantize(Decimal("0.01"))

        cf = create_auto_cashflow(
            company=sale.company,
            branch=sale.branch,
            cashbox=shift.cashbox if shift is not None else None,
            cashbox_id=None if shift is not None else cashbox_id,
            user=request.user,
            shift=shift,
            type=CashFlow.Type.INCOME,
            amount=pay_total,
            source_kind=CashFlow.SourceKind.DEBT_REPAYMENT,
            source_id=str(deal.id),
            idempotency_key=f"sale-pay-debt:{base_key}",
            name=f"Оплата долга по продаже №{sale.doc_number or sale.id}",
            source_business_operation_id="Оплата долга",
            payment_method=pm,
            affects_shift_drawer=(pm == "cash"),
            detached=detached,
        )

        from apps.main.models import sync_sale_status_from_deal
        sync_sale_status_from_deal(deal.id)  # статус и остаток продажи — сразу, не дожидаясь сигнала
        made = list(DealPayment.objects.filter(deal=deal, idempotency_key__in=list(inst_keys.values()))
                    .select_related("created_by").order_by("created_at")) if raw_key else []
        return self._response(request, sale, deal, pay_total, [cf] if cf else [], payments=made, pm=pm)

    def _response(self, request, sale, deal, paid, cashflows, replayed=False, payments=None, pm=None):
        from apps.main.services_debt import debt_receipt_info

        from apps.construction.auto_cashflow import serialize_auto_cashflows

        deal.refresh_from_db()
        left = max(Decimal("0.00"), deal.remaining_debt)
        sale.refresh_from_db()
        return Response(
            {
                "paid": str(money(paid)),
                "left": str(money(left)),
                "replayed": replayed,
                "sale": SaleDetailSerializer(sale, context={"request": request}).data,
                "cashflows": serialize_auto_cashflows(cashflows),
                # ТЗ 29.10 Б-13 п.3: что и сколько погашено по продаже
                "applied": [{
                    "sale": str(sale.id), "sale_number": sale.doc_number,
                    "amount": str(money(paid)), "sale_debt_left": str(money(left)),
                }],
                # ТЗ Н-13: оба имени поля способа оплаты, кто принял, смена, касса
                "method": pm,
                "payment_method": pm,
                **debt_receipt_info(request.user, cashflows, payments),
            },
            status=status.HTTP_200_OK,
        )


def _parse_partial_return_items(data) -> Optional[List[tuple]]:
    """
    None — полный возврат чека (как без тела запроса).
    Список — частичный возврат: пары (sale_item_id UUID, quantity).
    """
    if not isinstance(data, dict):
        return None
    raw = data.get("items", None)
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValidationError({"items": 'Ожидается список объектов с полями "sale_item_id" и "quantity".'})
    if len(raw) == 0:
        return None
    merged: Dict[uuid.UUID, Decimal] = {}
    for row in raw:
        if not isinstance(row, dict):
            raise ValidationError({"items": "Каждая позиция должна быть объектом."})
        sid = row.get("sale_item_id") or row.get("id")
        if sid in (None, ""):
            raise ValidationError({"items": "Укажите sale_item_id (или id) для каждой строки."})
        try:
            uid = uuid.UUID(str(sid))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError({"items": f"Некорректный sale_item_id: {sid!r}."})
        try:
            q = qty3(Decimal(str(row.get("quantity"))))
        except Exception:
            raise ValidationError({"items": "Некорректное quantity."})
        if q <= 0:
            raise ValidationError({"items": "quantity должно быть > 0."})
        merged[uid] = merged.get(uid, Decimal("0")) + q
    return [(k, merged[k]) for k in sorted(merged.keys())]


def _restock_product_for_sale_item_return(item: SaleItem, return_qty: Decimal) -> None:
    if not item.product_id:
        return
    if item.product and item.product.kind == Product.Kind.SERVICE:
        return
    stock_delta = line_qty_consume_units(return_qty, getattr(item, "sale_package", None))
    if getattr(item, "sale_unit_id", None):  # ТЗ 26.10 Б-53 п.6: возврат в единицах продажи -> базовые
        from apps.main.tz26.hw_units.units import unit_factor

        stock_delta = qty3(stock_delta * unit_factor(item))
    if stock_delta <= 0:
        return
    Product.objects.filter(pk=item.product_id).exclude(kind=Product.Kind.SERVICE).update(
        quantity=F("quantity") + stock_delta, updated_at=timezone.now()
    )
    if getattr(item, "variant_id", None):
        ProductVariant.objects.filter(pk=item.variant_id).update(quantity=F("quantity") + stock_delta, updated_at=timezone.now())


def _release_agent_allocations_for_qty(sale_item: SaleItem, return_qty_int: int) -> List[tuple]:
    """
    Снимает агентские привязки (AgentSaleAllocation) по строке чека на указанное
    количество. Возвращает разбивку [(subreal_id, agent_id, take), ...] — нужна,
    чтобы зафиксировать возврат/брак по каждой партии (subreal).
    """
    if return_qty_int <= 0:
        return []
    qs = (
        AgentSaleAllocation.objects.select_for_update()
        .filter(sale_item=sale_item)
        .order_by("-id")
    )
    if not qs.exists():
        return []
    remaining = return_qty_int
    breakdown: List[tuple] = []
    for alloc in qs:
        if remaining <= 0:
            break
        take = min(int(alloc.qty), remaining)
        if take <= 0:
            continue
        new_q = int(alloc.qty) - take
        if new_q <= 0:
            AgentSaleAllocation.objects.filter(pk=alloc.pk).delete()
        else:
            AgentSaleAllocation.objects.filter(pk=alloc.pk).update(qty=new_q)
        breakdown.append((alloc.subreal_id, alloc.agent_id, take))
        remaining -= take
    if remaining > 0:
        raise ValidationError(
            {"items": "Недостаточно привязок по строке чека для агентского возврата (рассинхронизация данных)."}
        )
    return breakdown


def _sale_item_unit_net(item: SaleItem) -> Decimal:
    """Чистая цена за единицу строки чека: (сумма строки - line_discount) / qty.

    Сумма строки берётся через cart_line_base — для весовых позиций она округлена
    вниз как на весах, поэтому возврат считается от той же суммы, что и продажа.
    """
    q = Decimal(str(item.quantity or 0))
    if q <= 0:
        return Decimal("0.00")
    base = cart_line_base(item, scale_amount_step(item.company))
    net = base - Decimal(str(item.line_discount or 0))
    return net / q


def _record_agent_sale_return(*, sale: Sale, breakdown: List[tuple], unit_net: Decimal,
                              is_defect: bool, user) -> None:
    """
    Фиксирует возврат/брак агентской продажи записями ReturnFromAgent (status=accepted)
    по разбивке снятых привязок.

    - is_defect=False (обычный возврат): товар уже вернулся агенту (привязка снята,
      «на руках» вырос). Только создаём запись для аналитики.
    - is_defect=True (брак): товар списывается — дополнительно увеличиваем
      subreal.qty_returned (уходит с рук агента) и на склад НЕ возвращаем.
    """
    now = timezone.now()
    for subreal_id, agent_id, take in breakdown:
        if take <= 0:
            continue
        amount = (unit_net * Decimal(take)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if is_defect:
            ManufactureSubreal.objects.filter(pk=subreal_id).update(
                qty_returned=F("qty_returned") + take
            )
        ReturnFromAgent.objects.create(
            subreal_id=subreal_id,
            returned_by_id=agent_id or getattr(user, "id", None),
            qty=int(take),
            is_defect=is_defect,
            amount=amount,
            client_id=sale.client_id,
            status=ReturnFromAgent.Status.ACCEPTED,
            accepted_by=user,
            accepted_at=now,
        )


def _rescale_sale_payments_after_partial_return(sale: Sale, old_total: Decimal, new_total: Decimal) -> None:
    """
    После частичного возврата приводим строки оплаты (SalePayment) в соответствие
    с новой суммой чека.

    Зачем: «живой» expected_cash смены считается по сумме SalePayment.amount
    оплаченных продаж (см. CashShift.calc_live_totals). Если строки оплаты не
    уменьшать, наличная касса смены после возврата остаётся завышенной.

    Поведение:
    - наличная строка уменьшается пропорционально → expected_cash смены падает;
    - безналичная строка тоже уменьшается, но на наличную кассу не влияет;
    - инвариант: sum(SalePayment.amount) == sale.total.
    Долговые чеки строк оплаты не имеют — тогда функция ничего не делает.
    """
    if not old_total or old_total <= 0 or new_total < 0:
        return

    payments = list(sale.payments.order_by("created_at", "id"))
    if not payments:
        # legacy-чек без строк оплаты: наличные считаются по sale.total, он уже пересчитан.
        return

    scale = Decimal(new_total) / Decimal(old_total)
    residual = money(new_total)
    survivors: List[SalePayment] = []
    for p in payments:
        scaled = money(Decimal(str(p.amount or 0)) * scale)
        if scaled <= 0:
            p.delete()
            continue
        p.amount = scaled
        survivors.append(p)
        residual = money(residual - scaled)

    # Копеечную погрешность округления вешаем на самую крупную строку,
    # чтобы сумма оплат точно совпала с новой суммой чека.
    if survivors and residual != 0:
        biggest = max(survivors, key=lambda x: x.amount)
        adjusted = money(biggest.amount + residual)
        if adjusted > 0:
            biggest.amount = adjusted

    for p in survivors:
        p.save(update_fields=["amount"])


def _recalc_sale_headers_from_items(sale: Sale, total_override: Optional[Decimal] = None) -> None:
    """Пересчитать суммы шапки чека по оставшимся строкам.

    total_override — точный остаток чека после возврата (ТЗ Н-02): скидка на чек
    сохраняется в discount_total, а не теряется при пересчёте по строкам.
    """
    rows = list(SaleItem.objects.filter(sale=sale).order_by("id"))
    if not rows:
        return
    d0 = Decimal("0")
    new_subtotal = money(sum((it.unit_price or d0) * Decimal(str(it.quantity or 0)) for it in rows))
    new_line_disc = money(sum(Decimal(str(it.line_discount or 0)) for it in rows))
    old_sub = sale.subtotal or d0
    old_disc = sale.discount_total or d0
    old_tax = sale.tax_total or d0
    old_total = sale.total or d0
    old_taxable = old_sub - old_disc
    new_taxable = new_subtotal - new_line_disc
    if old_taxable > 0:
        new_tax = money(old_tax * (new_taxable / old_taxable))
    elif old_taxable == 0 and new_taxable == 0:
        new_tax = old_tax
    else:
        new_tax = d0
    new_total = money(new_taxable + new_tax)
    if total_override is not None:
        new_total = money(total_override)
        new_line_disc = money(new_subtotal + new_tax - new_total)
    sale.subtotal = new_subtotal
    sale.discount_total = new_line_disc
    sale.tax_total = new_tax
    sale.total = new_total
    if old_total and old_total > 0:
        cr = sale.cash_received or d0
        sale.cash_received = money(cr * (new_total / old_total))
    if sale.consultant_id and getattr(sale, "consultant_commission_enabled", False) and getattr(sale, "consultant_commission_percent", None):
        comm_pct = Decimal(str(sale.consultant_commission_percent))
        if comm_pct > 0:
            sale.consultant_commission_amount = money(new_total * comm_pct / Decimal("100"))
        else:
            sale.consultant_commission_amount = Decimal("0.00")
    else:
        sale.consultant_commission_amount = Decimal("0.00")
    sale.save(update_fields=["subtotal", "discount_total", "tax_total", "total", "cash_received", "consultant_commission_amount"])

    # Синхронизируем строки оплаты с новой суммой чека, чтобы «живой» расчёт смены
    # (expected_cash) уменьшался симметрично частичному возврату.
    _rescale_sale_payments_after_partial_return(sale, old_total, new_total)


def _parse_is_defect(data) -> bool:
    """Флаг брака на уровне всего запроса возврата."""
    if not isinstance(data, dict):
        return False
    val = data.get("is_defect", False)
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "y", "on", "да")
    return False


def adjust_sale_debt_on_return(
    sale: Sale,
    returned_money: Decimal,
    *,
    is_full_return: bool = False,
) -> Optional[Dict]:
    """
    Корректировка / аннулирование долга по продаже (ClientDeal или legacy Debt).
    Выполняется внутри общей транзакции @transaction.atomic.
    """
    if returned_money is None or Decimal(str(returned_money)) <= 0:
        return None

    returned_money = money(Decimal(str(returned_money)))

    # 1. Проверяем связанную ClientDeal
    deal = (
        ClientDeal.objects.select_for_update()
        .filter(sale=sale)
        .first()
    )

    if deal:
        remaining_before = deal.remaining_debt
        debt_reduce = min(remaining_before, returned_money)
        cash_refund_due = max(Decimal("0.00"), returned_money - debt_reduce)
        insts_updated = 0
        today = timezone.localdate()

        if debt_reduce > 0:
            deal.amount = max(Decimal("0.00"), (deal.amount or Decimal("0.00")) - debt_reduce)

            # Корректировка неоплаченных взносов (алгоритм from_end: с конца)
            unpaid_insts = list(
                deal.installments.select_for_update()
                .filter(paid_on__isnull=True)
                .order_by("-number")
            )
            rem = debt_reduce

            for inst in unpaid_insts:
                if rem <= 0:
                    break
                unpaid_part = (inst.amount or Decimal("0.00")) - (inst.paid_amount or Decimal("0.00"))
                if unpaid_part <= 0:
                    continue
                insts_updated += 1
                if unpaid_part <= rem:
                    rem -= unpaid_part
                    inst.amount = inst.paid_amount or Decimal("0.00")
                    inst.paid_on = today
                    inst.save(update_fields=["amount", "paid_on"])
                else:
                    inst.amount = inst.amount - rem
                    rem = Decimal("0.00")
                    inst.save(update_fields=["amount"])

            if is_full_return or deal.remaining_debt <= 0:
                deal.installments.filter(paid_on__isnull=True).update(
                    paid_on=today,
                    amount=F("paid_amount"),
                )

            # Пересчитываем balance_after у всех взносов
            running_balance = deal.debt_amount
            for inst in deal.installments.order_by("number"):
                running_balance = max(Decimal("0.00"), running_balance - (inst.amount or Decimal("0.00")))
                inst.balance_after = running_balance
                inst.save(update_fields=["balance_after"])

            deal.auto_schedule = False
            deal._is_return_adjustment = True
            if is_full_return or (sale and sale.status == Sale.Status.CANCELED):
                deal.kind = ClientDeal.Kind.CANCELED
                deal.save(update_fields=["amount", "auto_schedule", "kind", "updated_at"])
            else:
                deal.save(update_fields=["amount", "auto_schedule", "updated_at"])

        remaining_after = deal.remaining_debt
        if (is_full_return and deal.paid_total == 0) or deal.kind == ClientDeal.Kind.CANCELED:
            deal_status = "canceled"
        elif remaining_after <= 0:
            deal_status = "closed"
        else:
            deal_status = "open"

        return {
            "deal_id": str(deal.id),
            "legacy_debt_id": None,
            "reduced_by": str(debt_reduce),
            "remaining_debt_before": str(remaining_before),
            "remaining_debt": str(remaining_after),
            "deal_status": deal_status,
            "installments_updated": insts_updated if debt_reduce > 0 else 0,
            "cash_refund_due": str(cash_refund_due),
            "reason": "sale_return",
        }

    # 2. Проверяем legacy Debt
    legacy_debt = (
        Debt.objects.select_for_update()
        .filter(sale=sale)
        .first()
    )
    if legacy_debt:
        remaining_before = legacy_debt.balance
        debt_reduce = min(remaining_before, returned_money)
        cash_refund_due = max(Decimal("0.00"), returned_money - debt_reduce)

        if debt_reduce > 0:
            legacy_debt.amount = max(Decimal("0.00"), (legacy_debt.amount or Decimal("0.00")) - debt_reduce)
            legacy_debt.save(update_fields=["amount", "updated_at"])

        remaining_after = legacy_debt.balance
        deal_status = "closed" if remaining_after <= 0 else "open"

        return {
            "deal_id": None,
            "legacy_debt_id": str(legacy_debt.id),
            "reduced_by": str(debt_reduce),
            "remaining_debt_before": str(remaining_before),
            "remaining_debt": str(remaining_after),
            "deal_status": deal_status,
            "installments_updated": 0,
            "cash_refund_due": str(cash_refund_due),
            "reason": "sale_return",
        }

    return None


def _resolve_return_cashbox(sale: Sale, payload: Optional[Dict] = None):
    from apps.construction.models import Cashbox, CashFlow
    from apps.construction.auto_cashflow import resolve_cashbox

    payload = payload or {}

    # 0. Явно переданная касса в payload
    cb_id = payload.get("cashbox_id")
    if cb_id:
        cb = Cashbox.objects.filter(id=cb_id, company=sale.company).first()
        if cb:
            return cb

    # 1. Если у чека зафиксирована касса — возвращаем её!
    if sale.cashbox:
        return sale.cashbox

    # 2. Если у чека зафиксирована смена с кассой — возвращаем кассу этой смены
    if sale.shift and sale.shift.cashbox:
        return sale.shift.cashbox

    # 3. Попытка разрешить по контексту из payload (§6)
    b_id = payload.get("branch_id") or (str(sale.branch_id) if sale.branch_id else None)
    cb_role = payload.get("cashbox_role")
    ctx = {}
    if b_id:
        ctx["branch_id"] = b_id
    if cb_role:
        ctx["cashbox_role"] = cb_role
    if ctx:
        cb = resolve_cashbox(
            company=sale.company,
            context=ctx,
            source_kind=CashFlow.SourceKind.POS_SALE_RETURN,
            require_cashbox=False,
        )
        if cb:
            return cb

    # 4. Fallback: POS_MAIN компании, затем POS_BRANCH, затем любая доступная касса
    cb = Cashbox.objects.filter(company=sale.company, role=Cashbox.CashboxRole.POS_MAIN).first()
    if not cb and sale.branch_id:
        cb = Cashbox.objects.filter(company=sale.company, branch_id=sale.branch_id, role=Cashbox.CashboxRole.POS_BRANCH).first()
    if not cb:
        # ТЗ Н-33: сейф и расчётный счёт — не ящик кассы, выбирать их запасным вариантом нельзя
        cb = Cashbox.objects.filter(company=sale.company).exclude(
            role__in=(Cashbox.CashboxRole.SAFE, Cashbox.CashboxRole.BANK)
        ).first()
    return cb


def _resolve_return_shift(sale: Sale, user, cashbox, payload: Optional[Dict] = None):
    from apps.construction.models import CashShift
    payload = payload or {}
    shift_id = payload.get("shift_id")
    if shift_id:
        sh = CashShift.objects.filter(id=shift_id, company=sale.company, status=CashShift.Status.OPEN).first()
        if sh and (not cashbox or sh.cashbox_id == cashbox.id):
            return sh

    # Если смена чека ещё открыта и относится к той же кассе (§6)
    if sale.shift and sale.shift.status == CashShift.Status.OPEN:
        if not cashbox or sale.shift.cashbox_id == cashbox.id:
            return sale.shift

    # Если у текущего кассира открыта смена на этой кассе (§6)
    if user and getattr(user, "is_authenticated", False) and cashbox:
        sh = CashShift.objects.filter(cashbox=cashbox, cashier=user, status=CashShift.Status.OPEN).first()
        if sh:
            return sh

    # Иначе смена null (§6)
    return None


def _create_refund_cash_kept_flow(*, sale, cashbox, user, shift, amount, method, idempotency_key):
    """
    Чек был оплачен наличными, а деньги вернули безналом — наличные физически
    остались в ящике. Строки оплат при возврате уже «унесли» их из расчёта смены,
    поэтому возвращаем сумму обратно отдельным приходом, иначе при закрытии
    смены появится ложная недостача.
    """
    from apps.construction.models import CashFlow
    from apps.construction.auto_cashflow import create_auto_cashflow

    return create_auto_cashflow(
        company=sale.company,
        branch=sale.branch,
        cashbox=cashbox,
        user=user,
        shift=shift,
        type=CashFlow.Type.INCOME,
        amount=amount,
        source_kind=CashFlow.SourceKind.POS_SALE_RETURN,
        source_id=str(sale.id),
        idempotency_key=f"{idempotency_key}:{method}:cash-kept",
        affects_shift_drawer=True,
        name=f"Наличные остались в кассе: возврат по чеку №{sale.doc_number or sale.id} отдан безналом",
        source_business_operation_id="pos_sale_return",
    )


def _refund_drawer_effect(original_method, refund_method, has_shift):
    """
    Как возврат влияет на наличный ящик смены, с учётом способа, которым деньги
    фактически отдали (`refund_method` в теле запроса).

    Ящик считается по строкам оплат живых чеков, поэтому обычный возврат он
    отражает сам: полный уводит чек из paid, частичный уменьшает строки оплат.
    Трогать ящик нужно только когда способ возврата разошёлся с исходной оплатой:

      * чек оплачен безналом, деньги отдали наличными → наличные ушли из ящика,
        но по строкам оплат там их и не было → нужен расход (expense_affects);
      * чек оплачен наличными, деньги вернули переводом → строки оплат уже
        «унесли» наличные из ящика, хотя физически они остались → нужен
        компенсирующий приход (income_adjust).

    Возвращает (expense_affects_drawer, needs_income_adjustment).
    """
    orig_cash = str(original_method or "cash").strip().lower() == "cash"
    rm = str(refund_method or "").strip().lower() or None
    if rm in (None, "original", "same"):
        refund_cash = orig_cash
    else:
        refund_cash = (rm == "cash")

    if not has_shift:
        return False, False
    return (refund_cash and not orig_cash), (orig_cash and not refund_cash)


def _broadcast_return_realtime(sale: Sale, created_flows, shift=None):
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        company_group = f"notif_company_{sale.company_id}"
        for cf in created_flows:
            async_to_sync(channel_layer.group_send)(
                company_group,
                {
                    "type": "market.notification",
                    "event": "market.cashflow.created",
                    "data": {
                        "cashflow_id": str(cf.id),
                        "type": cf.type,
                        "amount": str(cf.amount),
                        "cashbox_id": str(cf.cashbox_id) if cf.cashbox_id else None,
                        "source_kind": cf.source_kind or "",
                        "source_id": str(cf.source_id or ""),
                    },
                },
            )
        # Раньше условием был affects_shift_drawer у компенсирующего движения. Теперь
        # возврат ящик через движение не уменьшает (сумма уже учтена через статус
        # чека и пересчитанные строки оплат), но drawer_expected_cash при этом всё
        # равно меняется — значит экран смены надо обновлять при любом возврате.
        if shift and created_flows:
            totals = shift.calc_live_totals()
            async_to_sync(channel_layer.group_send)(
                company_group,
                {
                    "type": "market.notification",
                    "event": "market.shift.updated",
                    "data": {
                        "shift_id": str(shift.id),
                        "drawer_expected_cash": str(totals.get("drawer_expected_cash", "0.00")),
                        "cash_sales_total": str(totals.get("cash_sales_total", "0.00")),
                    },
                },
            )
    except Exception as exc:
        logging.getLogger("crm.pos.return").warning("WebSocket return notification error: %s", exc)


def _return_line(item, qty, discount, *, reason, restock) -> dict:
    """Строка возврата (BE2-01). amount — старое имя total, оставлено для совместимости."""
    price = money(item.unit_price or Decimal("0.00"))
    total = money(price * qty - discount)
    return {
        "sale_item": str(item.id),
        "product": str(item.product_id) if item.product_id else None,
        "variant": str(item.variant_id) if item.variant_id else None,
        "name": item.name_snapshot,
        "qty": str(qty3(qty)),
        "price": str(price),
        "discount": str(money(discount)),
        "total": str(total),
        "amount": str(total),
        "reason": reason,
        "restock": bool(restock and item.product_id),
    }


def _effective_refund_method(sale, refund_method) -> str:
    """Чем вернули деньги: явный способ кассы, иначе способ оплаты чека."""
    if refund_method and refund_method != "original":
        return str(refund_method)[:16]
    if sale.payment_method == Sale.PaymentMethod.DEBT:
        prepay_line = sale.payments.exclude(method=Sale.PaymentMethod.DEBT).first()
        if prepay_line and prepay_line.method:
            return str(prepay_line.method)[:16]
    return str(sale.payment_method or "cash")[:16]


def _refund_exceeds_paid_error(request, sale):
    """ТЗ Б-04 / Б-02: повтор возврата по полностью возвращённому чеку.
    Новый клиент или флаг компании — 409, старая касса — 400 (она не уходит в бесконечный повтор).
    data: {paid, refunded, requested, refundable}."""
    from core.api_compat import is_strict
    from core.exceptions import ApiError
    from apps.main.pos_serializers import _sale_refunded_total

    refunded = Decimal(_sale_refunded_total(sale))
    return ApiError(
        "refund_exceeds_paid",
        "Продажа уже отменена или полностью возвращена.",
        status_code=409 if is_strict(request, sale.company) else 400,
        data={
            "paid": str(money(refunded)),
            "refunded": str(money(refunded)),
            "requested": str(money(Decimal(str(sale.total or 0)))),
            "refundable": "0.00",
        },
    )


def _refund_summary(sale, debt_adj) -> Optional[Dict]:
    """ТЗ Н-02: разбивка последнего возврата — {id, amount, cash, noncash, debt_reduced, method}."""
    from apps.main.models import SaleReturn

    ret = SaleReturn.objects.filter(sale=sale).order_by("-created_at").first()
    if ret is None:
        return None
    return {
        "id": str(ret.id),
        "amount": str(money(ret.returned_amount or 0)),
        "cash": str(money(ret.refund_cash or 0)),
        "noncash": str(money(ret.refund_noncash or 0)),
        "debt_reduced": str(money(Decimal(str((debt_adj or {}).get("reduced_by") or 0)))),
        "method": ret.refund_method,
    }


def _execute_sale_return(
    sale: Sale,
    partial_items: Optional[List[tuple]],
    *,
    is_defect: bool = False,
    user=None,
    payload: Optional[Dict] = None,
) -> Optional[Dict]:
    """
    Выполняет возврат продажи (полный или частичный).
    Создаёт компенсирующие движения расхода CashFlow (§3, §9, §10).
    Гарантирует идемпотентность по idempotency_key (§5).
    """
    is_agent_sale = sale.agent_allocations.exists()
    payload = payload or {}
    idempotency_key = payload.get("idempotency_key")
    # Чем деньги фактически отдали: "cash" | "original" (по умолчанию) | способ безнала.
    refund_method = payload.get("refund_method")
    line_reason = "defect" if is_defect else (str(payload.get("reason") or "")[:255] or None)

    returned_lines = []
    if not partial_items:
        returned_money = sale.total or Decimal("0.00")
        for item in sale.items.all():
            returned_lines.append(_return_line(
                item, qty3(Decimal(str(item.quantity or 0))), money(item.line_discount or 0),
                reason=line_reason, restock=not is_defect,
            ))
        if is_agent_sale:
            for item in sale.items.select_related("product", "sale_package"):
                rq = qty3(Decimal(str(item.quantity or 0)))
                if rq <= 0:
                    continue
                breakdown = _release_agent_allocations_for_qty(item, int(rq))
                _record_agent_sale_return(
                    sale=sale, breakdown=breakdown,
                    unit_net=_sale_item_unit_net(item),
                    is_defect=is_defect, user=user,
                )
        else:
            if not is_defect:
                for item in sale.items.filter(product_id__isnull=False).select_related("sale_package"):
                    rq = qty3(Decimal(str(item.quantity or 0)))
                    if rq <= 0:
                        continue
                    _restock_product_for_sale_item_return(item, rq)
        sale.status = Sale.Status.CANCELED
        sale.save(update_fields=["status"])
        debt_adj = adjust_sale_debt_on_return(sale, returned_money, is_full_return=True)
        is_full = True
    else:
        item_ids = [uid for uid, _ in partial_items]
        found = set(SaleItem.objects.filter(sale=sale, id__in=item_ids).values_list("id", flat=True))
        missing = set(item_ids) - found
        if missing:
            raise ValidationError({"items": "Есть позиции не из этого чека или несуществующие sale_item_id."})

        total_returned_money = Decimal("0.00")
        # ТЗ Н-02: доля скидки на чек. Строка возвращается по цене после скидки
        # на позицию × (итог чека / сумма строк); последний возврат — ровно остаток.
        old_total = money(sale.total or Decimal("0.00"))
        lines_net = money(sum(
            (Decimal(str(it.unit_price or 0)) * Decimal(str(it.quantity or 0)) - Decimal(str(it.line_discount or 0)))
            for it in sale.items.all()
        ))
        check_ratio = (old_total / lines_net) if lines_net > 0 else Decimal("1")

        for sid, rq in partial_items:
            item = (
                SaleItem.objects.select_for_update(of=("self",))
                .select_related("product", "sale_package")
                .get(pk=sid, sale=sale)
            )
            old_q = qty3(Decimal(str(item.quantity or 0)))
            rq = qty3(rq)
            if rq > old_q or rq <= 0:
                raise ValidationError({"items": f"Некорректное количество возврата для позиции {sid}."})

            unit_price = item.unit_price or Decimal("0.00")
            old_disc = Decimal(str(item.line_discount or 0))
            item_disc = money(old_disc * (rq / old_q)) if old_q > 0 else Decimal("0.00")
            item_net = money((unit_price * rq) - item_disc)
            item_returned_money = money(item_net * check_ratio)
            total_returned_money += item_returned_money
            # В строке возврата — цена со скидкой (и на позицию, и на чек), чтобы сверка сходилась.
            returned_lines.append(_return_line(
                item, rq, money(item_disc + item_net - item_returned_money), reason=line_reason, restock=not is_defect,
            ))

            if is_agent_sale:
                if rq != rq.to_integral_value():
                    raise ValidationError({"items": "Для агентского чека количество возврата должно быть целым."})
                unit_net = _sale_item_unit_net(item)
                breakdown = _release_agent_allocations_for_qty(item, int(rq))
                _record_agent_sale_return(
                    sale=sale, breakdown=breakdown, unit_net=unit_net,
                    is_defect=is_defect, user=user,
                )
            else:
                if not is_defect:
                    _restock_product_for_sale_item_return(item, rq)

            new_q = qty3(old_q - rq)
            new_disc = money(old_disc - item_disc) if old_q > 0 else old_disc

            if new_q <= 0:
                item.delete()
            else:
                item.quantity = new_q
                item.line_discount = new_disc
                item.save(update_fields=["quantity", "line_discount"])

        is_full = not SaleItem.objects.filter(sale=sale).exists()
        if is_full:
            # Последний возврат — ровно остаток чека (копейки округления — в последнюю строку).
            diff = old_total - total_returned_money
            total_returned_money = old_total
            if diff and returned_lines:
                last = returned_lines[-1]
                last["discount"] = str(money(Decimal(last["discount"]) - diff))
                last["total"] = last["amount"] = str(money(Decimal(last["total"]) + diff))
            sale.status = Sale.Status.CANCELED
        else:
            total_returned_money = min(total_returned_money, old_total)
            sale.status = getattr(Sale.Status, "PARTIALLY_RETURNED", "partially_returned")
        sale.save(update_fields=["status"])

        if not is_full:
            _recalc_sale_headers_from_items(sale, total_override=old_total - total_returned_money)

        returned_money = total_returned_money
        debt_adj = adjust_sale_debt_on_return(sale, total_returned_money, is_full_return=is_full)

    # --- Создание компенсирующего движения CashFlow (§3, §9, §10) ---
    if sale.payment_method == Sale.PaymentMethod.DEBT:
        cash_to_refund = Decimal(str(debt_adj.get("cash_refund_due") or "0.00")) if debt_adj else Decimal("0.00")
    else:
        cash_to_refund = returned_money

    created_flows = []
    actual_shift = None
    # Без ключа от кассы — служебный: иначе два возврата по 200 по одному чеку
    # склеивались в одно движение (идемпотентность по сумме).
    flow_key = str(idempotency_key) if idempotency_key else f"return-auto:{uuid.uuid4().hex}"
    # Обмен: возвращённая сумма идёт в зачёт нового чека, денег не выдают —
    # расход на возврат не создаём (иначе обмен даёт двойной доход). Наличную
    # часть ящику смены всё равно возвращаем поправкой «наличные остались».
    is_offset_refund = str(refund_method or "").strip().lower() == "offset"
    if cash_to_refund > Decimal("0.00"):
        from apps.construction.models import CashFlow
        from apps.construction.auto_cashflow import create_auto_cashflow

        cashbox = _resolve_return_cashbox(sale, payload)
        actual_shift = _resolve_return_shift(sale, user, cashbox, payload)

        payments = list(sale.payments.filter(amount__gt=0).exclude(method=Sale.PaymentMethod.DEBT))
        if payments:
            if is_full:
                for p in payments:
                    p_amt = p.amount
                    p_m = str(p.method or "cash").lower()
                    # Способ, которым деньги фактически отдали, может отличаться от
                    # исходной оплаты — тогда ящик надо поправить (см. _refund_drawer_effect).
                    affects_drawer, _needs_income = _refund_drawer_effect(
                        p_m, refund_method, actual_shift is not None
                    )
                    ik = f"{flow_key}:{p_m}"
                    cf = None if is_offset_refund else create_auto_cashflow(
                        company=sale.company,
                        branch=sale.branch,
                        cashbox=cashbox,
                        user=user,
                        shift=actual_shift,
                        type=CashFlow.Type.EXPENSE,
                        amount=p_amt,
                        source_kind=CashFlow.SourceKind.POS_SALE_RETURN,
                        source_id=str(sale.id),
                        idempotency_key=ik,
                        affects_shift_drawer=affects_drawer,
                        payment_method=p_m,
                        name=f"Возврат по чеку №{sale.doc_number or sale.id} ({p.get_method_display() if hasattr(p, 'get_method_display') else p_m})",
                        source_business_operation_id="pos_sale_return",
                    )
                    if cf:
                        created_flows.append(cf)
                    if _needs_income:
                        adj = _create_refund_cash_kept_flow(
                            sale=sale, cashbox=cashbox, user=user, shift=actual_shift,
                            amount=p_amt, method=p_m,
                            idempotency_key=flow_key,
                        )
                        if adj:
                            created_flows.append(adj)
            else:
                total_orig = sum(p.amount for p in payments) or sale.total
                ratio = (cash_to_refund / total_orig) if total_orig > 0 else Decimal("0")
                for p in payments:
                    part_amt = money(p.amount * ratio)
                    if part_amt > Decimal("0.00"):
                        p_m = str(p.method or "cash").lower()
                        # Способ, которым деньги фактически отдали, может отличаться от
                        # исходной оплаты — тогда ящик надо поправить (см. _refund_drawer_effect).
                        affects_drawer, _needs_income = _refund_drawer_effect(
                            p_m, refund_method, actual_shift is not None
                        )
                        ik = f"{flow_key}:{p_m}"
                        cf = None if is_offset_refund else create_auto_cashflow(
                            company=sale.company,
                            branch=sale.branch,
                            cashbox=cashbox,
                            user=user,
                            shift=actual_shift,
                            type=CashFlow.Type.EXPENSE,
                            amount=part_amt,
                            source_kind=CashFlow.SourceKind.POS_SALE_RETURN,
                            source_id=str(sale.id),
                            idempotency_key=ik,
                            affects_shift_drawer=affects_drawer,
                            payment_method=p_m,
                            name=f"Возврат по чеку №{sale.doc_number or sale.id} ({p.get_method_display() if hasattr(p, 'get_method_display') else p_m})",
                            source_business_operation_id="pos_sale_return",
                        )
                        if cf:
                            created_flows.append(cf)
                        if _needs_income:
                            adj = _create_refund_cash_kept_flow(
                                sale=sale, cashbox=cashbox, user=user, shift=actual_shift,
                                amount=part_amt, method=p_m,
                                idempotency_key=flow_key,
                            )
                            if adj:
                                created_flows.append(adj)
        else:
            p_m = str(sale.payment_method or "cash").lower()
            if p_m == "debt":
                p_m = "cash"
            # Способ, которым деньги фактически отдали, может отличаться от
            # исходной оплаты — тогда ящик надо поправить (см. _refund_drawer_effect).
            affects_drawer, _needs_income = _refund_drawer_effect(
                p_m, refund_method, actual_shift is not None
            )
            cf = None if is_offset_refund else create_auto_cashflow(
                company=sale.company,
                branch=sale.branch,
                cashbox=cashbox,
                user=user,
                shift=actual_shift,
                type=CashFlow.Type.EXPENSE,
                amount=cash_to_refund,
                source_kind=CashFlow.SourceKind.POS_SALE_RETURN,
                source_id=str(sale.id),
                idempotency_key=f"{flow_key}:{p_m}" if not idempotency_key else str(idempotency_key),
                affects_shift_drawer=affects_drawer,
                payment_method=p_m,
                name=f"Возврат по чеку №{sale.doc_number or sale.id}",
                source_business_operation_id="pos_sale_return",
            )
            if cf:
                created_flows.append(cf)
            if _needs_income:
                adj = _create_refund_cash_kept_flow(
                    sale=sale, cashbox=cashbox, user=user, shift=actual_shift,
                    amount=cash_to_refund, method=p_m,
                    idempotency_key=flow_key,
                )
                if adj:
                    created_flows.append(adj)

    from apps.main.models import SaleReturn
    import json
    from django.core.serializers.json import DjangoJSONEncoder
    items_payload_safe = json.loads(json.dumps(partial_items, cls=DjangoJSONEncoder)) if partial_items else None
    # Денег не выдавали (например, чек в долг) — смена возврата = открытая смена кассира.
    if actual_shift is None and user is not None and getattr(user, "is_authenticated", False):
        actual_shift = _find_open_shift_for_cashier(company=sale.company, cashier=user)

    # Запись о возврате ведём всегда (список возвратов, отчёт смены);
    # без ключа от кассы — служебный уникальный ключ.
    from apps.main.models import sale_refund_split

    # Сколько денег отдали и каким способом — для итогов смены (returns_cash / returns_noncash).
    refund_cash, refund_noncash = sale_refund_split(sale, refund_method, cash_to_refund)
    ret_key = str(idempotency_key) if idempotency_key else f"auto:{uuid.uuid4()}"
    sale_return, _ = SaleReturn.objects.get_or_create(
        company=sale.company,
        idempotency_key=ret_key,
        defaults={
            "sale": sale,
            "user": user if (user and getattr(user, "is_authenticated", False)) else None,
            "shift": actual_shift,
            "returned_amount": returned_money,
            "is_defect": is_defect,
            "is_full": is_full,
            "items_payload": items_payload_safe,
            "returned_items": returned_lines,
            "reason": str(payload.get("reason") or "")[:255],
            "refund_method": _effective_refund_method(sale, refund_method),
            "refund_cash": refund_cash,
            "refund_noncash": refund_noncash,
        }
    )

    from apps.integrations.events import emit_event

    emit_event(
        sale.company_id,
        "sale.returned",
        {
            "return": sale_return.pk,
            "sale": sale.pk,
            "sale_number": sale.doc_number,
            "amount": returned_money,
            "is_full": is_full,
            "items": returned_lines,
            "shift": getattr(actual_shift, "pk", None),
            "cashier": getattr(user, "pk", None),
        },
    )

    _broadcast_return_realtime(sale, created_flows, actual_shift)
    return debt_adj


@pin_guard("POST")
class SaleReturnAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    """
    Возврат продажи (владелец и кассир).
    POST /api/main/pos/sales/<pk>/return/
    """

    permission_classes = [permissions.IsAuthenticated]

    @idempotent(body_key=True)
    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        payload = request.data or {}
        idempotency_key = payload.get("idempotency_key")
        if idempotency_key:
            from apps.main.models import SaleReturn
            existing_ret = SaleReturn.objects.filter(
                sale_id=pk, idempotency_key=str(idempotency_key)
            ).first()
            if existing_ret and existing_ret.response_data:
                return Response({**existing_ret.response_data, "replayed": True}, status=status.HTTP_200_OK)

        locked_qs = Sale.objects.select_for_update()
        locked_qs = self._filter_qs_company_branch(locked_qs)
        sale = get_object_or_404(locked_qs, id=pk)

        # Закрытие периода (ТЗ «Бухгалтер», п. 2): возврат проводится сегодняшним числом.
        from apps.warehouse.services import ensure_period_open_api
        ensure_period_open_api(company=sale.company, dates=[timezone.now()], user=request.user)

        if sale.status == Sale.Status.CANCELED:
            # ТЗ Н-02: всё уже возвращено (или чек отменён) — больше вернуть нельзя.
            raise _refund_exceeds_paid_error(request, sale)
        valid_return_statuses = (
            Sale.Status.PAID,
            Sale.Status.DEBT,
            getattr(Sale.Status, "PARTIALLY_RETURNED", "partially_returned")
        )
        if sale.status not in valid_return_statuses:
            return Response(
                {"detail": "Возврат возможен только для оплаченных или долговых продаж."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            partial = _parse_partial_return_items(request.data)
            is_defect = _parse_is_defect(request.data)
            debt_adj = _execute_sale_return(
                sale, partial, is_defect=is_defect, user=request.user, payload=request.data
            )
        except (ValidationError, DjangoValidationError) as e:
            detail = getattr(e, "detail", None) or getattr(e, "message_dict", None) or getattr(e, "messages", None) or str(e)
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)

        invalidate_cache_pattern(f"analytics:market:{sale.company_id}:")
        invalidate_cache_pattern(f"products:list:{sale.company_id}:")

        sale.refresh_from_db()
        from apps.main.tz26.pos_sync.services import record_return_origin
        record_return_origin(request, sale)  # Б-67: с какого устройства возврат
        data = SaleDetailSerializer(sale, context={"request": request}).data
        data["debt_adjustment"] = debt_adj
        data["refund"] = _refund_summary(sale, debt_adj)

        if idempotency_key:
            from apps.main.models import SaleReturn
            import json
            from django.core.serializers.json import DjangoJSONEncoder
            ret_obj = SaleReturn.objects.filter(
                sale=sale, idempotency_key=str(idempotency_key)
            ).first()
            if ret_obj:
                ret_obj.response_data = json.loads(json.dumps(data, cls=DjangoJSONEncoder))
                ret_obj.save(update_fields=["response_data"])

        return Response(
            data,
            status=status.HTTP_200_OK,
        )


class AgentSaleReturnAPIView(SaleReturnAPIView):
    """
    Возврат продажи агента — только свои.
    POST /api/main/agents/me/sales/<pk>/return/
    """

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        payload = request.data or {}
        idempotency_key = payload.get("idempotency_key")
        if idempotency_key:
            from apps.main.models import SaleReturn
            existing_ret = SaleReturn.objects.filter(
                sale_id=pk, idempotency_key=str(idempotency_key)
            ).first()
            if existing_ret and existing_ret.response_data:
                return Response({**existing_ret.response_data, "replayed": True}, status=status.HTTP_200_OK)

        allowed_qs = (
            Sale.objects.filter(
                Q(agent_allocations__agent=request.user) | Q(user=request.user)
            )
            .filter(id=pk)
            .distinct()
        )
        allowed_qs = self._filter_qs_company_branch(allowed_qs)
        if not allowed_qs.exists():
            raise Http404("Продажа не найдена или не принадлежит агенту.")

        sale = Sale.objects.select_for_update().get(id=pk)

        if sale.status == Sale.Status.CANCELED:
            # ТЗ Н-02: всё уже возвращено (или чек отменён) — больше вернуть нельзя.
            raise _refund_exceeds_paid_error(request, sale)
        valid_return_statuses = (
            Sale.Status.PAID,
            Sale.Status.DEBT,
            getattr(Sale.Status, "PARTIALLY_RETURNED", "partially_returned")
        )
        if sale.status not in valid_return_statuses:
            return Response(
                {"detail": "Возврат возможен только для оплаченных или долговых продаж."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            partial = _parse_partial_return_items(request.data)
            is_defect = _parse_is_defect(request.data)
            debt_adj = _execute_sale_return(
                sale, partial, is_defect=is_defect, user=request.user, payload=request.data
            )
        except (ValidationError, DjangoValidationError) as e:
            detail = getattr(e, "detail", None) or getattr(e, "message_dict", None) or getattr(e, "messages", None) or str(e)
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)

        invalidate_cache_pattern(f"analytics:market:{sale.company_id}:")
        invalidate_cache_pattern(f"products:list:{sale.company_id}:")
        sale.refresh_from_db()
        data = SaleDetailSerializer(sale, context={"request": request}).data
        data["debt_adjustment"] = debt_adj
        data["refund"] = _refund_summary(sale, debt_adj)

        if idempotency_key:
            from apps.main.models import SaleReturn
            import json
            from django.core.serializers.json import DjangoJSONEncoder
            ret_obj = SaleReturn.objects.filter(
                sale=sale, idempotency_key=str(idempotency_key)
            ).first()
            if ret_obj:
                ret_obj.response_data = json.loads(json.dumps(data, cls=DjangoJSONEncoder))
                ret_obj.save(update_fields=["response_data"])

        return Response(
            data,
            status=status.HTTP_200_OK,
        )


class SaleMobileScannerTokenAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        cart = get_object_or_404(
            Cart.objects.select_related("company", "branch", "user", "shift"),
            id=pk,
            company=request.user.company,
            status=Cart.Status.ACTIVE,
        )
        token = MobileScannerToken.issue(cart, ttl_minutes=10)
        return Response(MobileScannerTokenSerializer(token).data, status=201)


class ProductFindByBarcodeAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        barcode = request.query_params.get("barcode", "").strip()
        if not barcode:
            return Response([], status=200)

        from apps.main.tz26.hw_units.units import find_sale_unit_by_barcode, sale_unit_scan_payload

        unit = find_sale_unit_by_barcode(request.user.company_id, barcode)  # ТЗ 26.10 Б-53 п.3
        if unit is not None:
            return Response([{
                "id": str(unit.product_id), "name": unit.product.name, "barcode": barcode,
                "price": str(unit.price if unit.price is not None else unit.product.price),
                **sale_unit_scan_payload(unit),
            }], status=200)
        try:
            product = _resolve_product_by_barcode_for_pos(
                request.user.company_id,
                barcode,
                only_fields=("id", "name", "barcode", "price"),
            )
        except AmbiguousBarcode as exc:
            return _ambiguous_barcode_response(exc)

        if not product:
            return Response([], status=200)
        
        return Response(
            [{"id": str(product.id), "name": product.name, "barcode": product.barcode, "price": str(product.price)}],
            status=200,
        )


class MobileScannerIngestAPIView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, token, *args, **kwargs):
        barcode = request.data.get("barcode", "").strip()
        raw_qty = request.data.get("quantity", "1.000")
        qty = _to_decimal(raw_qty, default=Decimal("1.000"))
        qty = qty3(qty)
        if qty <= 0:
            return Response({"detail": "quantity must be > 0"}, status=400)

        mt = MobileScannerToken.objects.select_related("cart", "cart__company").filter(token=token).first()
        if not mt:
            return Response({"detail": "invalid token"}, status=404)
        if not mt.is_valid():
            return Response({"detail": "token expired"}, status=410)

        cart = mt.cart
        # POS доступен сферам Маркет и Услуги
        if not _company_can_use_cashier(getattr(cart, "company", None)):
            return Response({"detail": "invalid token"}, status=404)

        # ✅ защита: нельзя сканить в уже закрытую/неактивную корзину
        if cart.status != Cart.Status.ACTIVE:
            return Response({"detail": "cart is not active"}, status=409)

        try:
            product, scale_data, lookup_error = _lookup_product_for_pos_scan(
                cart.company_id,
                barcode,
                only_fields=("id", "company_id", "price", "barcode"),
            )
        except AmbiguousBarcode as exc:
            return _ambiguous_barcode_response(exc)
        if not product:
            return Response({"not_found": True, "message": lookup_error or "Товар не найден"}, status=404)

        effective_qty = _effective_qty_from_scale_data(scale_data, qty)

        with transaction.atomic():
            cart = Cart.objects.select_for_update().get(id=cart.id)
            _upsert_scanned_cart_item(cart, product, effective_qty)
        return Response({"ok": True}, status=201)


class PosSalesLimitPagination(SupplierReceiptLimitPagination):
    page_size = 100
    max_page_size = 5000

    def get_page_size(self, request):
        # Размер страницы можно задать как `limit` (историческое имя),
        # так и `page_size` (используется фильтром аналитики).
        for param in (self.page_size_query_param, "page_size"):
            raw = request.query_params.get(param)
            if raw:
                try:
                    return _positive_int(raw, strict=True, cutoff=self.max_page_size)
                except (KeyError, ValueError):
                    pass
        return self.page_size


def _parse_decimal_param(raw):
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError):
        return None


def _apply_sale_date_filters(qs, request):
    start_raw = (
        (request.query_params.get("date_from") or "").strip()
        or (request.query_params.get("start") or "").strip()
    )
    end_raw = (
        (request.query_params.get("date_to") or "").strip()
        or (request.query_params.get("end") or "").strip()
    )
    start_dt = _parse_range_dt(start_raw, end=False) if start_raw else None
    end_dt = _parse_range_dt(end_raw, end=True) if end_raw else None
    if start_dt:
        qs = qs.filter(created_at__gte=start_dt)
    if end_dt:
        qs = qs.filter(created_at__lte=end_dt)
    return qs


def _aggregate_pos_sales_total_amount(qs, *, status_filter: str):
    if status_filter != Sale.Status.CANCELED:
        qs = qs.exclude(status=Sale.Status.CANCELED)
    total = qs.aggregate(total=Coalesce(Sum("total"), Decimal("0.00")))["total"]
    if total is None:
        return Decimal("0.00")
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class SaleConsultantsAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    """
    Список возможных консультантов для кассы (активные сотрудники компании).
    GET /api/main/pos/sale-consultants/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = self._company() or request.user.company
        branch = self._auto_branch()

        from django.contrib.auth import get_user_model
        User = get_user_model()
        users = User.objects.filter(company=company, is_active=True).order_by("first_name", "last_name", "email")

        from apps.main.models import MarketSaleEmployeePayProfile
        profiles = MarketSaleEmployeePayProfile.objects.filter(company=company)
        if branch is not None:
            profiles = profiles.filter(Q(branch=branch) | Q(branch__isnull=True))
        else:
            profiles = profiles.filter(branch__isnull=True)

        prof_map = {}
        for p in profiles.order_by("user_id", "-branch_id"):
            if p.user_id not in prof_map or p.branch_id is not None:
                prof_map[p.user_id] = p

        result = []
        for u in users:
            prof = prof_map.get(u.id)
            pct_str = "0.00"
            if prof and prof.pay_scheme in (MarketSaleEmployeePayProfile.PayScheme.PERCENT, MarketSaleEmployeePayProfile.PayScheme.SALARY_PLUS_PERCENT):
                if prof.sales_percent is not None:
                    pct_str = str(prof.sales_percent)

            fn = (getattr(u, "get_full_name", lambda: "")() or getattr(u, "full_name", None) or getattr(u, "email", "") or str(u.id)).strip()
            result.append({
                "id": str(u.id),
                "full_name": fn,
                "first_name": getattr(u, "first_name", "") or "",
                "last_name": getattr(u, "last_name", "") or "",
                "default_commission_percent": pct_str,
            })

        return Response(result, status=status.HTTP_200_OK)


class SaleListOrderingFilter(filters.OrderingFilter):
    """
    `ordering=-paid_at` сортирует по времени продажи: Coalesce(paid_at, created_at).
    Иначе чеки без paid_at (продажа в долг) при DESC в PostgreSQL шли бы первыми (NULLS FIRST).
    К явной сортировке добавляется `-id`, чтобы страницы не пересекались при равных значениях.
    """

    def get_ordering(self, request, queryset, view):
        terms = super().get_ordering(request, queryset, view)
        if not terms or not request.query_params.get(self.ordering_param):
            return terms
        mapped = [("-" if t.startswith("-") else "") + "sold_at" if t.lstrip("-") == "paid_at" else t for t in terms]
        if not any(t.lstrip("-") == "id" for t in mapped):
            mapped.append("-id" if mapped[-1].startswith("-") else "id")  # Б-68: id в ту же сторону
        return mapped


class SaleListAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, generics.ListAPIView):
    serializer_class = SaleListSerializer
    queryset = (
        Sale.objects.select_related("user", "consultant", "client", "cashbox", "cashbox__branch", "branch")
        .prefetch_related("items__product", "payments", "deals", "deals__installments", "returns")
        .all()
    )
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, SaleListOrderingFilter]
    filterset_fields = ("user", "consultant")
    search_fields = ("id",)
    ordering_fields = ("created_at", "paid_at", "sold_at", "total", "status", "doc_number")
    # id — стабильный порядок при равном created_at, иначе страницы пересекаются.
    ordering = ("-created_at", "-id")
    pagination_class = PosSalesLimitPagination

    def get_queryset(self):
        qs = super().get_queryset().annotate(sold_at=Coalesce("paid_at", "created_at"))
        # ТЗ 29.10 Б-99: начисленные бонусы по чеку одним подзапросом (без N+1)
        from django.db.models import DecimalField as _DF, OuterRef as _OR, Subquery as _SQ, Sum as _Sum
        from apps.main.models import ClientBonusTransaction as _CBT
        qs = qs.annotate(bonus_earned_sum=Coalesce(_SQ(
            _CBT.objects.filter(sale=_OR("pk"), reason="earn").values("sale").annotate(s=_Sum("delta")).values("s"),
            output_field=_DF(max_digits=14, decimal_places=2),
        ), Decimal("0.00"), output_field=_DF(max_digits=14, decimal_places=2)))
        qs = _apply_sale_date_filters(qs, self.request)

        # ТЗ-28 §5.1: фильтры по смене, кассе, кассиру и точному времени — чтобы телефон
        # («Мои чеки» за смену) и касса («История чеков») не грузили чеки всей компании.
        qp = self.request.query_params
        shift_param = (qp.get("shift") or qp.get("shift_id") or "").strip()
        if shift_param:
            qs = qs.filter(shift_id=shift_param)
        cashbox_param = (qp.get("cashbox") or qp.get("cashbox_id") or "").strip()
        if cashbox_param:
            qs = qs.filter(cashbox_id=cashbox_param)
        cashier_param = (qp.get("cashier") or qp.get("cashier_id") or "").strip()
        if cashier_param:
            qs = qs.filter(user_id=cashier_param)
        for param, lookup in (("created_after", "created_at__gte"), ("created_before", "created_at__lte")):
            raw = (qp.get(param) or "").strip()
            if raw:
                dt = parse_datetime(raw.replace(" ", "+"))
                if dt is None:
                    raise ValidationError({param: "Ожидается дата-время ISO 8601."})
                if timezone.is_naive(dt):
                    dt = timezone.make_aware(dt)
                qs = qs.filter(**{lookup: dt})

        from apps.main.tz26.pos_sync.sales_api import apply_sync_filters
        qs = apply_sync_filters(qs, self.request)  # Б-67/Б-68: source, device_key, created_by, updated_after

        number = (self.request.query_params.get("number") or "").strip()
        if number:
            if not number.isdigit():
                raise ValidationError({"number": "Номер чека — целое число."})
            qs = qs.filter(doc_number=int(number))

        paid_only = self.request.query_params.get("paid")
        if paid_only in ("1", "true", "True"):
            qs = qs.filter(status=Sale.Status.PAID)

        # Поддержка фильтра статуса (одиночного или нескольких через запятую)
        status_param = (self.request.query_params.get("status") or "").strip()
        statuses_param = (self.request.query_params.get("statuses") or "").strip()
        raw_statuses = statuses_param or status_param
        if raw_statuses:
            if "," in raw_statuses:
                status_list = [s.strip() for s in raw_statuses.split(",") if s.strip()]
                if status_list:
                    qs = qs.filter(status__in=status_list)
            else:
                qs = qs.filter(status=raw_statuses)

        # Без статусов из списка (например `exclude_status=new` — открытые корзины)
        # или `only_finished=true` — то же самое одним флагом.
        exclude_param = (self.request.query_params.get("exclude_status") or "").strip()
        if exclude_param:
            excluded = [x.strip() for x in exclude_param.split(",") if x.strip()]
            if excluded:
                qs = qs.exclude(status__in=excluded)
        if (self.request.query_params.get("only_finished") or "").strip().lower() in ("1", "true", "yes"):
            qs = qs.exclude(status=Sale.Status.NEW)

        users_param = self.request.query_params.get("users")
        if users_param:
            user_ids = [x.strip() for x in users_param.split(",") if x.strip()]
            if user_ids:
                qs = qs.filter(user_id__in=user_ids)

        client_param = (self.request.query_params.get("client") or "").strip()
        if client_param:
            qs = qs.filter(client_id=client_param)

        consultant_param = (self.request.query_params.get("consultant") or "").strip()
        if consultant_param:
            qs = qs.filter(consultant_id=consultant_param)

        # Аналитический фильтр (страница /crm/market/analytics → «Транзакции»)
        payment_method = (self.request.query_params.get("payment_method") or "").strip()
        if payment_method:
            if payment_method == Sale.PaymentMethod.TRANSFER:
                # ТЗ 12.11: безнал разных банков тоже считается переводом.
                qs = qs.filter(payment_method__in=SALE_TRANSFER_METHODS)
            else:
                qs = qs.filter(payment_method=payment_method)

        cashbox_param = (self.request.query_params.get("cashbox") or "").strip()
        if cashbox_param:
            qs = qs.filter(cashbox_id=cashbox_param)

        cashier_param = (self.request.query_params.get("cashier") or "").strip()
        if cashier_param:
            qs = qs.filter(user_id=cashier_param)

        min_total = _parse_decimal_param(self.request.query_params.get("min_total"))
        if min_total is not None:
            qs = qs.filter(total__gte=min_total)

        max_total = _parse_decimal_param(self.request.query_params.get("max_total"))
        if max_total is not None:
            qs = qs.filter(total__lte=max_total)

        barcode = (self.request.query_params.get("barcode") or "").strip()
        if barcode:
            matched_items = SaleItem.objects.filter(sale_id=OuterRef("pk")).filter(
                Q(barcode_snapshot=barcode)
                | Q(product__barcode=barcode)
                | Q(product__alternate_barcodes__barcode=barcode)
            ).order_by("id")
            qs = qs.filter(
                Q(items__barcode_snapshot=barcode)
                | Q(items__product__barcode=barcode)
                | Q(items__product__alternate_barcodes__barcode=barcode)
            ).annotate(matched_item_name=Subquery(matched_items.values("name_snapshot")[:1])).distinct()

        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        from apps.main.tz26.pos_sync.sales_api import etag_matches, sales_etag
        etag = sales_etag(request, queryset)  # Б-68: ETag + If-None-Match -> 304
        if etag_matches(request, etag):
            return Response(status=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        status_param = (request.query_params.get("status") or "").strip()
        total_amount = _aggregate_pos_sales_total_amount(queryset, status_filter=status_param)

        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        response = self.paginator.get_paginated_response(serializer.data, total_amount=total_amount)
        if etag:
            response["ETag"] = etag
        return response


def _audit_sale_deleted(request, infos):
    from apps.construction.audit import log_request

    for company, pk, before in infos[:500]:
        log_request(request, "delete", "sale", pk, f"Чек {pk[:8]}", before=before, company=company)


def _audit_sale_deleted(request, infos):
    from apps.construction.audit import log_request

    for company, pk, before in infos[:500]:
        log_request(request, "delete", "sale", pk, f"Чек {pk[:8]}", before=before, company=company)


class SaleRetrieveAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "id"
    lookup_url_kwarg = "pk"

    queryset = (
        Sale.objects.select_related("user")
        .prefetch_related("items__product", "returns")
        .all()
    )

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return SaleStatusUpdateSerializer
        return SaleDetailSerializer

    def retrieve(self, request, *args, **kwargs):
        pk = kwargs.get(self.lookup_url_kwarg or self.lookup_field)
        user = request.user
        company = self._company() or user.company

        cart = _get_pos_open_cart_for_cashier(company=company, user=user, cart_id=pk)
        if cart:
            cart = get_object_or_404(_cart_queryset_for_response(), id=cart.id, company=company)
            return _pos_multi_cart_response(request, cart)

        return super().retrieve(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        pk = kwargs.get(self.lookup_url_kwarg or self.lookup_field)
        user = request.user
        company = self._company() or user.company

        cart = _get_pos_open_cart_for_cashier(company=company, user=user, cart_id=pk)
        if cart:
            if not _is_cart_deletion_authorized(request, company=company):
                return Response({"detail": "Требуется код удаления или время проверки истекло"}, status=status.HTTP_403_FORBIDDEN)
                    
            shift = _abandon_pos_open_cart(company=company, user=user, cart=cart)
            ordered = list(_shift_active_carts_qs(company, user, shift))
            if not ordered:
                return Response(
                    {
                        "sale": None,
                        "active_sale_id": None,
                        "id": None,
                        "carts": [],
                    },
                    status=status.HTTP_200_OK,
                )
            active = next((c for c in ordered if c.is_default), ordered[0])
            active = get_object_or_404(_cart_queryset_for_response(), id=active.id, company=company)
            return _pos_multi_cart_response(request, active)

        # журнал (ТЗ п.8): удаление чека
        _sale = self.get_object()
        _info = (_sale.company, str(_sale.pk), {"total": str(_sale.total), "status": str(_sale.status)})
        response = super().destroy(request, *args, **kwargs)
        _audit_sale_deleted(request, [_info])
        return response


class SaleBulkDeleteAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    """
    Массовое удаление продаж.
    Поддерживает DELETE и POST (многие клиенты/прокси не передают body с DELETE).
    Body: {"ids": ["uuid", ...], "allow_paid": false}
    """
    permission_classes = [permissions.IsAuthenticated]

    def _perform_bulk_delete(self, request):
        data = request.data or {}
        ids = data.get("ids")
        allow_paid = bool(data.get("allow_paid", False))
        if not isinstance(ids, list) or not ids:
            return Response({"detail": "Укажите непустой список 'ids'."}, status=400)

        valid_ids, invalid_ids = [], []
        for x in ids:
            try:
                valid_ids.append(uuid.UUID(str(x)))
            except Exception:
                invalid_ids.append(str(x))

        if not valid_ids and invalid_ids:
            return Response({"detail": "Нет валидных UUID.", "invalid_ids": invalid_ids}, status=400)

        base_qs = Sale.objects.filter(id__in=valid_ids)
        base_qs = self._filter_qs_company_branch(base_qs)

        paid_ids = list(base_qs.filter(status=Sale.Status.PAID).values_list("id", flat=True))
        if paid_ids and not allow_paid:
            return Response(
                {
                    "code": "paid_sales_need_confirmation",
                    "detail": "Среди выбранных чеков есть оплаченные. "
                              "Удалить их можно только после подтверждения.",
                    "paid_ids": [str(x) for x in paid_ids],
                },
                status=400,
            )

        if allow_paid:
            deletable_qs = base_qs
            not_allowed_ids = []
        else:
            deletable_qs = base_qs.exclude(status=Sale.Status.PAID)
            not_allowed_ids = []

        found_ids = set(str(sid) for sid in base_qs.values_list("id", flat=True))
        not_found_ids = [str(x) for x in valid_ids if str(x) not in found_ids]

        _infos = [(x.company, str(x.pk), {"total": str(x.total), "status": str(x.status)}) for x in deletable_qs.select_related("company")]
        deleted_count, _ = deletable_qs.delete()
        _audit_sale_deleted(request, _infos)

        return Response(
            {
                "deleted": deleted_count,
                "not_found": not_found_ids + invalid_ids,
                "not_allowed": [str(x) for x in not_allowed_ids],
            },
            status=200,
        )

    @transaction.atomic
    def delete(self, request, *args, **kwargs):
        return self._perform_bulk_delete(request)

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        return self._perform_bulk_delete(request)


class AgentMySalesListAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    История продаж агента — только свои продажи.
    GET /api/main/agents/me/sales/

    Показывает продажи, где агент имеет AgentSaleAllocation или является кассиром (user).
    """
    serializer_class = SaleListSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ("status", "user")
    search_fields = ("id",)
    ordering_fields = ("created_at", "total", "status")
    ordering = ("-created_at",)

    def get_queryset(self):
        user = self.request.user
        qs = (
            Sale.objects.filter(
                Q(agent_allocations__agent=user) | Q(user=user)
            )
            .select_related("user")
            .prefetch_related("items__product")
            .distinct()
        )
        qs = self._filter_qs_company_branch(qs)

        start_dt = _parse_range_dt(self.request.query_params.get("start"), end=False)
        end_dt = _parse_range_dt(self.request.query_params.get("end"), end=True)
        if start_dt:
            qs = qs.filter(created_at__gte=start_dt)
        if end_dt:
            qs = qs.filter(created_at__lte=end_dt)

        paid_only = self.request.query_params.get("paid")
        if paid_only in ("1", "true", "True"):
            qs = qs.filter(status=Sale.Status.PAID)

        return qs.order_by("-created_at")


class AgentMySaleRetrieveAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, generics.RetrieveAPIView):
    """
    Детали продажи агента — только свои.
    GET /api/main/agents/me/sales/<pk>/
    """
    serializer_class = SaleDetailSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "id"
    lookup_url_kwarg = "pk"

    def get_queryset(self):
        user = self.request.user
        qs = (
            Sale.objects.filter(
                Q(agent_allocations__agent=user) | Q(user=user)
            )
            .select_related("user")
            .prefetch_related("items__product")
            .distinct()
        )
        return self._filter_qs_company_branch(qs)




# ========================
# Cashier Settings
# ========================
class MarketCashierSettingsAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = request.user.company
        is_admin = getattr(request.user, "role", None) in ["owner", "admin"]

        debt_ver = getattr(company, "debt_schedule_version", None) or "v1"
        if debt_ver not in ("v1", "v2"):
            debt_ver = "v1"

        data = {
            "delete_item_code_required": bool(company.cashier_password),
            "max_discount_percent": str(company.max_discount_percent) if company.max_discount_percent is not None else None,
            "debt_schedule_version": debt_ver,
            "deferred_schedule_version": debt_ver,
            "cashflow_requests_enabled": bool(getattr(company, "cashflow_requests_enabled", False)),
        }
        
        if is_admin:
            data["delete_item_code"] = company.cashier_password
            
        return Response(data, status=status.HTTP_200_OK)

    def patch(self, request, *args, **kwargs):
        company = request.user.company
        is_admin = getattr(request.user, "role", None) in ["owner", "admin"]
        if not is_admin:
            return Response({"detail": "Недостаточно прав"}, status=status.HTTP_403_FORBIDDEN)
            
        data = request.data
        updated_fields = []

        if "cashflow_requests_enabled" in data:
            cf_val = data["cashflow_requests_enabled"]
            if isinstance(cf_val, bool):
                company.cashflow_requests_enabled = cf_val
                updated_fields.append("cashflow_requests_enabled")
            elif str(cf_val).strip().lower() in ("true", "1"):
                company.cashflow_requests_enabled = True
                updated_fields.append("cashflow_requests_enabled")
            elif str(cf_val).strip().lower() in ("false", "0"):
                company.cashflow_requests_enabled = False
                updated_fields.append("cashflow_requests_enabled")
            else:
                return Response(
                    {"cashflow_requests_enabled": ["Значение должно быть булевым (true/false)"]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if "delete_item_code" in data:
            code = data["delete_item_code"]
            if code in (None, ""):
                company.cashier_password = None
            else:
                code_str = str(code).strip()
                if not code_str.isdigit() or not (4 <= len(code_str) <= 8):
                    return Response({"delete_item_code": ["Код должен состоять из 4–8 цифр"]}, status=status.HTTP_400_BAD_REQUEST)
                company.cashier_password = code_str
            updated_fields.append("cashier_password")

        if "max_discount_percent" in data:
            mdp = data["max_discount_percent"]
            if mdp in (None, ""):
                company.max_discount_percent = None
            else:
                try:
                    from decimal import Decimal
                    val = Decimal(str(mdp))
                    if not (0 <= val <= 100):
                        raise ValueError
                    company.max_discount_percent = val
                except (ValueError, TypeError, ArithmeticError):
                    return Response({"max_discount_percent": ["Значение должно быть от 0 до 100"]}, status=status.HTTP_400_BAD_REQUEST)
            updated_fields.append("max_discount_percent")

        ver_val = data.get("debt_schedule_version") or data.get("deferred_schedule_version")
        if ver_val is not None:
            raw_ver = str(ver_val).strip().lower()
            if raw_ver in ("v2", "2"):
                company.debt_schedule_version = "v2"
            else:
                company.debt_schedule_version = "v1"
            updated_fields.append("debt_schedule_version")

        if updated_fields:
            company.save(update_fields=updated_fields)

        debt_ver = getattr(company, "debt_schedule_version", None) or "v1"
        if debt_ver not in ("v1", "v2"):
            debt_ver = "v1"

        resp = {
            "delete_item_code_required": bool(company.cashier_password),
            "max_discount_percent": str(company.max_discount_percent) if company.max_discount_percent is not None else None,
            "delete_item_code": company.cashier_password,
            "debt_schedule_version": debt_ver,
            "deferred_schedule_version": debt_ver,
            "cashflow_requests_enabled": bool(getattr(company, "cashflow_requests_enabled", False)),
        }
        return Response(resp, status=status.HTTP_200_OK)


class VerifyDeleteCodeAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        company = request.user.company
        
        if not company.cashier_password:
            return Response({"valid": True}, status=status.HTTP_200_OK)
            
        code = request.data.get("code", "")
        
        from django.core.cache import cache
        throttle_key = f"verify_delete_throttle_{request.user.id}"
        attempts = cache.get(throttle_key, 0)
        if attempts >= 10:
            return Response({"detail": "Слишком много попыток. Попробуйте позже."}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        
        cache.set(throttle_key, attempts + 1, timeout=60)
        
        import secrets
        is_valid = secrets.compare_digest(str(code).strip(), company.cashier_password)
        
        # Аудит попытки ввода кода (сам код НЕ логируется согласно §3.5)
        import logging
        audit_logger = logging.getLogger("crm.pos.cashier_settings")
        audit_logger.info(
            "Verify delete code attempt: user_id=%s company_id=%s valid=%s ip=%s",
            request.user.id,
            company.id if company else None,
            is_valid,
            request.META.get("REMOTE_ADDR"),
        )

        if is_valid:
            cache.set(f"delete_verified_{request.user.id}", True, timeout=120)
            return Response({"valid": True}, status=status.HTTP_200_OK)
            
        return Response({"valid": False}, status=status.HTTP_200_OK)


class MarketCustomerDisplaySettingsAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /main/pos/customer-display-settings/
    PATCH /main/pos/customer-display-settings/

    Настройки второго экрана покупателя (per-company singleton).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = self._company()
        if not company:
            return Response({"detail": "Компания не найдена"}, status=status.HTTP_400_BAD_REQUEST)

        from apps.main.serializers import MarketCustomerDisplaySettingsSerializer

        settings_obj = MarketCustomerDisplaySettings.objects.filter(company=company).first()
        if not settings_obj:
            settings_obj, _ = MarketCustomerDisplaySettings.objects.get_or_create(company=company)

        serializer = MarketCustomerDisplaySettingsSerializer(settings_obj)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request, *args, **kwargs):
        company = self._company()
        if not company:
            return Response({"detail": "Компания не найдена"}, status=status.HTTP_400_BAD_REQUEST)

        is_admin = getattr(request.user, "role", None) in ["owner", "admin"] or getattr(request.user, "is_superuser", False)
        if not is_admin:
            return Response({"detail": "Недостаточно прав"}, status=status.HTTP_403_FORBIDDEN)

        from apps.main.serializers import MarketCustomerDisplaySettingsSerializer

        settings_obj = MarketCustomerDisplaySettings.objects.filter(company=company).first()
        if not settings_obj:
            settings_obj, _ = MarketCustomerDisplaySettings.objects.get_or_create(company=company)

        serializer = MarketCustomerDisplaySettingsSerializer(settings_obj, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save(updated_by=request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def put(self, request, *args, **kwargs):
        return self.patch(request, *args, **kwargs)


def _is_cart_deletion_authorized(request, company=None) -> bool:
    """
    Проверка авторизации на удаление позиции или корзины:
    - owner / admin -> всегда разрешено
    - код не задан на компании -> разрешено
    - передан заголовок X-Delete-Code с верным кодом -> разрешено
    - есть активный токен верификации (cache delete_verified_{user_id}, 120 сек) -> разрешено
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return False
    is_admin = getattr(user, "role", None) in ["owner", "admin"]
    if is_admin:
        return True
    comp = company or getattr(user, "company", None)
    code_req = getattr(comp, "cashier_password", None) if comp else None
    if not code_req:
        return True

    header_code = request.headers.get("X-Delete-Code") or request.META.get("HTTP_X_DELETE_CODE")
    if header_code:
        import secrets
        if secrets.compare_digest(str(header_code).strip(), str(code_req)):
            return True

    from django.core.cache import cache
    return bool(cache.get(f"delete_verified_{user.id}"))

class CartItemUpdateDestroyAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get_active_cart(self, request, cart_id):
        return get_object_or_404(
            Cart,
            id=cart_id,
            company=request.user.company,
            status=Cart.Status.ACTIVE,
        )

    def _get_item_in_cart(self, cart, item_or_product_id):
        item = CartItem.objects.filter(cart=cart, id=item_or_product_id).select_related("product").first()
        if item:
            return item

        item = CartItem.objects.filter(cart=cart, product_id=item_or_product_id).select_related("product").first()
        if item:
            return item

        raise Http404("CartItem not found in this cart.")

    def _apply_min_price(self, item, unit_price):
        """Цена продажи не ниже закупочной. Со скидкой (line_discount > 0) можно ниже. Для услуг ограничение отменено."""
        if not item.product_id:
            return unit_price
        if getattr(getattr(item, "product", None), "kind", None) == Product.Kind.SERVICE:
            return unit_price
        if Decimal(str(getattr(item, "line_discount", None) or 0)) > 0:
            return unit_price
        min_price = _q2(Decimal(str(getattr(item.product, "purchase_price", None) or 0)))
        if item.sale_unit_id:  # ТЗ 26.10 Б-53: закупочная цена единицы = базовая x factor
            from apps.main.tz26.hw_units.units import unit_factor

            min_price = _q2(min_price * unit_factor(item))
        if unit_price < min_price:
            return min_price
        return unit_price

    @transaction.atomic
    def patch(self, request, cart_id, item_id, *args, **kwargs):
        cart = self._get_active_cart(request, cart_id)
        item = self._get_item_in_cart(cart, item_id)

        ser = CartItemPatchSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        qty = data.get("quantity")
        
        is_admin = getattr(request.user, "role", None) in ["owner", "admin"]
        
        if qty is not None:
            qty = qty3(qty)
            if qty < 0:
                return Response({"quantity": "Количество не может быть отрицательным."}, status=400)
            if qty == 0:
                if not _is_cart_deletion_authorized(request):
                    return Response({"detail": "Требуется код удаления или время проверки истекло"}, status=status.HTTP_403_FORBIDDEN)
                
                log_cart_item_deletion(item=item, deleted_by=request.user)
                item.delete()
                cart.recalc()
                return Response(SaleCartSerializer(cart).data, status=200)
            if item.product_id and not item.sale_package_id:  # ТЗ 26.10 Б-52 (под флагом hardware_2026_11)
                from apps.main.tz26.hw_units.services import check_line_quantity

                check_line_quantity(request, cart.company, item.product, qty, sale_unit=item.sale_unit)
            item.quantity = qty
            if item.sale_unit_id:
                item.base_quantity = qty3(qty * item.sale_unit.factor)

        unit_price = data.get("unit_price")
        line_discount = data.get("discount_total")
        discount_percent = data.get("discount_percent")

        # Цена и скидка меняются независимо. Со скидкой можно продавать ниже закупочной.
        if unit_price is not None:
            item.unit_price = self._apply_min_price(item, _q2(unit_price))
            item.price_manually_edited = True
        if line_discount is not None or discount_percent is not None:
            current_qty = qty if qty is not None else item.quantity
            current_price = (
                item.unit_price
                if unit_price is None
                else self._apply_min_price(item, _q2(unit_price))
            )
            new_discount = _line_discount_from_request(
                current_price,
                current_qty,
                line_discount,
                discount_percent,
            )
            max_dp = request.user.company.max_discount_percent
            if max_dp is not None and not is_admin:
                if _line_discount_over_limit(current_price, current_qty, new_discount, max_dp):
                    return Response(_discount_limit_error(max_dp), status=status.HTTP_400_BAD_REQUEST)
            item.line_discount = new_discount
            item.manual_discount = new_discount

        update_fields = []
        if qty is not None:
            update_fields.append("quantity")
            update_fields.append("base_quantity")  # ТЗ 26.10 Б-53
        if unit_price is not None:
            update_fields.append("unit_price")
            update_fields.append("price_manually_edited")
        if line_discount is not None or discount_percent is not None:
            update_fields += ["line_discount", "manual_discount"]
        if "performer" in data:
            from apps.main.kassa_views import resolve_performers

            item.performer = resolve_performers(cart.company_id, [data["performer"]]).get(data["performer"])
            update_fields.append("performer")
        if update_fields:
            item.save(update_fields=update_fields)
        cart.recalc()
        if item.pk:
            item.refresh_from_db()
        return Response(SaleCartSerializer(cart).data, status=200)

    @transaction.atomic
    def delete(self, request, cart_id, item_id, *args, **kwargs):
        cart = self._get_active_cart(request, cart_id)
        item = self._get_item_in_cart(cart, item_id)
        if not _is_cart_deletion_authorized(request):
            return Response({"detail": "Требуется код удаления или время проверки истекло"}, status=status.HTTP_403_FORBIDDEN)

        log_cart_item_deletion(item=item, deleted_by=request.user)
        item.delete()
        cart.recalc()
        return Response(SaleCartSerializer(cart).data, status=200)


class CartItemDeletionLogListAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    GET /api/main/pos/cart-item-deletions/
    Журнал удалений позиций из корзины (товар, количество, кто удалил, время).
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = CartItemDeletionLogSerializer
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = CartItemDeletionLog.objects.select_related("deleted_by", "product", "cart").all()
        return self._filter_qs_company_branch(qs)


class SaleAddCustomItemAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        cart = get_object_or_404(
            Cart.objects.select_related("company", "branch", "user", "shift")
            .prefetch_related("items"),
            id=pk,
            company=request.user.company,
            status=Cart.Status.ACTIVE,
        )
        ser = CustomCartItemCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        name = ser.validated_data["name"].strip()
        if not name:
            return Response({"name": "Название не может быть пустым."}, status=400)

        price = _q2(ser.validated_data["price"])
        negative_expense = price < 0
        if negative_expense:
            # ТЗ Н-15 / Б-02: новый клиент (или флаг api_strict_2026_10) — 400; старая касса — «Расход»
            # проводим как скидку на чек на эту сумму (сумма на сервере = сумме в кассе).
            from core.api_compat import is_strict
            from core.exceptions import ApiError

            if is_strict(request, cart.company):
                msg = "Цена своей позиции не может быть отрицательной. Для уменьшения суммы используйте скидку."
                raise ApiError("negative_price", msg, fields={"price": [msg]}, top={"price": msg})
        qty = _to_decimal(ser.validated_data.get("quantity", "1.000"), default=Decimal("1.000"))
        qty = qty3(qty)

        if negative_expense:
            extra = _q2(-price * qty)
            cart.order_discount_percent = None
            cart.order_discount_total = _q2((cart.order_discount_total or Decimal("0")) + extra)
            cart.save(update_fields=["order_discount_total", "order_discount_percent", "updated_at"])
            cart.recalc()
            return Response(SaleCartSerializer(cart).data, status=status.HTTP_201_CREATED)

        item = CartItem.objects.filter(
            cart=cart,
            product__isnull=True,
            custom_name=name,
            unit_price=price,
        ).first()

        if item:
            CartItem.objects.filter(pk=item.pk).update(quantity=F("quantity") + qty)
            item.refresh_from_db(fields=["quantity"])
        else:
            CartItem.objects.create(
                company=cart.company,
                branch=getattr(cart, "branch", None),
                cart=cart,
                product=None,
                custom_name=name,
                unit_price=price,
                quantity=qty,
            )

        cart.recalc()
        return Response(SaleCartSerializer(cart).data, status=status.HTTP_201_CREATED)


OWNER_ROLES = {Roles.OWNER}


def _is_owner(user) -> bool:
    if getattr(user, "role", None) in OWNER_ROLES:
        company = getattr(user, "company", None)
        if company and getattr(company, "owner_id", None):
            return company.owner_id == user.id
        return True
    return False


def _agent_available_qty(user, company, product_id) -> int:
    sub = ManufactureSubreal.objects.filter(company=company, agent_id=user.id, product_id=product_id)
    accepted = sub.aggregate(s=Sum("qty_accepted"))["s"] or 0
    returned = sub.aggregate(s=Sum("qty_returned"))["s"] or 0
    sold = (
        AgentSaleAllocation.objects.filter(
            company=company,
            agent=user,
            product_id=product_id,
        ).aggregate(s=Sum("qty"))["s"]
        or 0
    )
    return int(accepted) - int(returned) - int(sold)


def _resolve_acting_agent(request, cart, *, allow_owner_override=True):
    user = request.user
    company = user.company

    agent_id = request.data.get("agent") or request.query_params.get("agent")
    if allow_owner_override and agent_id:
        if not _is_owner(user):
            raise ValidationError({"agent": "Только владелец может продавать за агента."})
        agent = get_object_or_404(User, id=agent_id, company=company)
        cache.set(f"cart_agent:{cart.id}", str(agent.id), timeout=60 * 60)
        return agent

    cached_id = cache.get(f"cart_agent:{cart.id}")
    if cached_id:
        try:
            return User.objects.get(id=cached_id, company=company)
        except User.DoesNotExist:
            cache.delete(f"cart_agent:{cart.id}")

    return user


def _should_use_main_stock_in_agent_sale(*, user, acting_agent) -> bool:
    # If the operator is the owner, agent selection only affects attribution,
    # while product availability must still come from the main stock.
    return _is_owner(user)


@transaction.atomic
def _allocate_agent_sale(*, company, agent, sale: Sale):
    items = sale.items.select_related("product").all()

    for item in items:
        if not item.product_id:
            continue

        qty_to_allocate = int(item.quantity or 0)
        if qty_to_allocate <= 0:
            continue

        locked_subreals = list(
            ManufactureSubreal.objects.select_for_update()
            .filter(
                company=company,
                agent_id=agent.id,
                product_id=item.product_id,
            )
            .order_by("created_at", "id")
        )

        if not locked_subreals:
            raise ValidationError(
                {
                    "detail": f"У агента нет передач по товару "
                    f"{getattr(item.product, 'name', item.product_id)}."
                }
            )

        sub_ids = [s.id for s in locked_subreals]
        sold_map = {
            row["subreal_id"]: (row["s"] or 0)
            for row in AgentSaleAllocation.objects.filter(
                company=company,
                subreal_id__in=sub_ids,
            )
            .values("subreal_id")
            .annotate(s=Sum("qty"))
        }

        total_available = 0
        avail_rows = []
        for s in locked_subreals:
            sold = int(sold_map.get(s.id, 0))
            acc = int(s.qty_accepted or 0)
            ret = int(s.qty_returned or 0)
            avail = max(acc - ret - sold, 0)
            total_available += avail
            avail_rows.append((s, avail))

        if qty_to_allocate > total_available:
            name = getattr(item.product, "name", item.product_id)
            raise ValidationError(
                {
                    "detail": f"Недостаточно на руках у агента для товара {name}. "
                    f"Нужно {qty_to_allocate}, доступно {total_available}."
                }
            )

        for s, avail in avail_rows:
            if qty_to_allocate <= 0 or avail <= 0:
                continue
            take = min(avail, qty_to_allocate)

            AgentSaleAllocation.objects.create(
                company=company,
                agent=agent,
                subreal=s,
                sale=sale,
                sale_item=item,
                product=item.product,
                qty=take,
            )
            qty_to_allocate -= take

        if qty_to_allocate > 0:
            raise ValidationError({"detail": "Внутренняя ошибка распределения остатков по передачам."})


class AgentCartStartAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        user = request.user

        company = self._company() or user.company
        branch = self._auto_branch()

        qs = Cart.objects.filter(company=company, user=user, status=Cart.Status.ACTIVE)

        if hasattr(Cart, "branch"):
            if branch is not None:
                qs = qs.filter(branch=branch)
            else:
                qs = qs.filter(branch__isnull=True)

        qs = qs.order_by("-created_at")
        cart = qs.first()
        created = False

        if cart is None:
            create_kwargs = dict(company=company, user=user, status=Cart.Status.ACTIVE)
            if hasattr(Cart, "branch"):
                create_kwargs["branch"] = branch
            if hasattr(Cart, "is_wholesale"):
                opts0 = StartCartOptionsSerializer(data=request.data)
                if opts0.is_valid():
                    is_wholesale0 = (
                        bool(opts0.validated_data.get("is_wholesale"))
                        if "is_wholesale" in opts0.validated_data
                        else False
                    )
                    create_kwargs["is_wholesale"] = is_wholesale0
            cart = Cart.objects.create(**create_kwargs)
            created = True
        else:
            extra_ids = list(qs.values_list("id", flat=True)[1:])
            if extra_ids:
                Cart.objects.filter(id__in=extra_ids).update(
                    status=Cart.Status.CHECKED_OUT,
                )

        _ = _resolve_acting_agent(request, cart, allow_owner_override=True)

        opts = StartCartOptionsSerializer(data=request.data)
        if opts.is_valid():
            order_disc_total = opts.validated_data.get("order_discount_total")
            order_disc_percent = opts.validated_data.get("order_discount_percent")
            is_wholesale_req = (
                bool(opts.validated_data.get("is_wholesale"))
                if "is_wholesale" in opts.validated_data
                else None
            )

            if order_disc_percent is not None:
                cart.order_discount_percent = money(Decimal(str(order_disc_percent)))
                cart.order_discount_total = Decimal("0.00")
            elif order_disc_total is not None:
                cart.order_discount_percent = None
                cart.order_discount_total = money(order_disc_total)
            update_fields = []
            if order_disc_total is not None or order_disc_percent is not None:
                update_fields.extend(["order_discount_total", "order_discount_percent"])
            wholesale_changed = False
            if is_wholesale_req is not None and getattr(cart, "is_wholesale", False) != bool(is_wholesale_req):
                cart.is_wholesale = bool(is_wholesale_req)
                update_fields.append("is_wholesale")
                wholesale_changed = True
            if update_fields:
                cart.save(update_fields=update_fields)
            if created or wholesale_changed:
                _reprice_cart_items_for_mode(cart)

        cart.recalc()
        return Response(SaleCartSerializer(cart).data, status=status.HTTP_201_CREATED)


class AgentSaleScanAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        cart = get_object_or_404(
            Cart.objects.select_for_update().select_related("company", "branch", "user", "shift"),
            id=pk,
            company=request.user.company,
            status=Cart.Status.ACTIVE,
        )
        ser = ScanRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        barcode = ser.validated_data["barcode"].strip()
        qty = ser.validated_data["quantity"]

        try:
            product, scale_data, lookup_error = _lookup_product_for_pos_scan(
                cart.company_id,
                barcode,
                only_fields=("id", "company_id", "price", "quantity", "barcode", "plu", "code", "is_weight"),
            )
        except AmbiguousBarcode as exc:
            return _ambiguous_barcode_response(exc)
        if not product:
            return Response({"not_found": True, "message": lookup_error or "Товар не найден"}, status=404)

        effective_qty = _effective_qty_from_scale_data(scale_data, qty)

        acting_agent = _resolve_acting_agent(request, cart, allow_owner_override=True)
        use_main_stock = _should_use_main_stock_in_agent_sale(user=request.user, acting_agent=acting_agent)

        # ✅ типобезопасно: int/Decimal не смешиваем
        available = (
            Decimal(str(getattr(product, "quantity", 0) or 0))
            if use_main_stock
            else Decimal(_agent_available_qty(acting_agent, cart.company, product.id))
        )
        in_cart = _as_decimal(
            CartItem.objects.filter(cart=cart, product=product).aggregate(s=Sum("quantity"))["s"] or 0,
            default=Decimal("0"),
        )
        req = _as_decimal(effective_qty, default=Decimal("0"))

        if req + in_cart > available:
            remaining = max(Decimal("0"), available - in_cart)
            return Response(
                {
                    "detail": (
                        f"Недостаточно на основном складе. Доступно: {qty3(remaining)}."
                        if use_main_stock
                        else f"Недостаточно у агента. Доступно: {qty3(remaining)}."
                    )
                },
                status=400,
            )

        _upsert_scanned_cart_item(cart, product, effective_qty)
        cart.recalc()
        return _cart_response(request, cart.id, status_code=status.HTTP_201_CREATED)


class AgentSaleAddItemAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        cart = get_object_or_404(
            Cart,
            id=pk,
            company=request.user.company,
            status=Cart.Status.ACTIVE,
        )
        ser = AddItemSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        product = get_object_or_404(
            Product,
            id=ser.validated_data["product_id"],
            company=cart.company,
        )
        qty = ser.validated_data["quantity"]
        allow_minus = bool(ser.validated_data.get("allow_minus"))
        can_minus = allow_minus and _is_owner_like(request.user)

        acting_agent = _resolve_acting_agent(request, cart, allow_owner_override=True)
        use_main_stock = _should_use_main_stock_in_agent_sale(user=request.user, acting_agent=acting_agent)

        # Поштучная продажа из пачки — только обычная касса / checkout_cart.
        if ser.validated_data.get("sale_package_id"):
            return Response(
                {
                    "sale_package_id": (
                        "Поштучная продажа из упаковки оформляется только через обычную кассу со сменой, "
                        "не через агентскую корзину."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        unit_price = ser.validated_data.get("unit_price")
        line_discount = ser.validated_data.get("discount_total")
        discount_percent = ser.validated_data.get("discount_percent")

        base_price = (
            money(unit_price)
            if unit_price is not None
            else money(getattr(product, "price", None) or Decimal("0"))
        )
        disc_total = _line_discount_from_request(base_price, qty, line_discount, discount_percent)

        if disc_total <= 0:
            min_price = money(Decimal(str(getattr(product, "purchase_price", None) or 0)))
            qty_dec = Decimal(str(qty))
            effective_unit = base_price - (disc_total / qty_dec) if qty_dec else base_price
            if effective_unit < min_price:
                return Response(
                    {"unit_price": f"Цена продажи не может быть ниже закупочной ({min_price})."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        cart = Cart.objects.select_for_update().get(id=cart.id)

        if use_main_stock:
            available = Decimal(str(getattr(product, "quantity", 0) or 0))
            in_cart = _as_decimal(
                CartItem.objects.filter(
                    cart=cart, product=product, sale_package__isnull=True
                ).aggregate(s=Sum("quantity"))["s"]
                or 0,
                default=Decimal("0"),
            )
            req = _as_decimal(qty, default=Decimal("0"))
            if (not can_minus) and req + in_cart > available:
                return Response(
                    {
                        "detail": (
                            f"Недостаточно на основном складе. "
                            f"Доступно: {qty3(max(Decimal('0'), available - in_cart))}."
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            available = Decimal(str(_agent_available_qty(acting_agent, cart.company, product.id)))
            in_cart = _as_decimal(
                CartItem.objects.filter(
                    cart=cart, product=product, sale_package__isnull=True
                ).aggregate(s=Sum("quantity"))["s"]
                or 0,
                default=Decimal("0"),
            )
            req = _as_decimal(qty, default=Decimal("0"))
            if (not can_minus) and req + in_cart > available:
                remaining = max(Decimal("0"), available - in_cart)
                return Response(
                    {"detail": f"Недостаточно у агента. Доступно: {qty3(remaining)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        item = (
            CartItem.objects.select_for_update()
            .filter(cart=cart, product=product, sale_package__isnull=True)
            .first()
        )
        if item:
            item.quantity = qty3(item.quantity + qty)
            if unit_price is not None:
                item.unit_price = base_price
            if line_discount is not None or discount_percent is not None:
                item.manual_discount = item.manual_discount_base() + disc_total
                item.line_discount = item.manual_discount
            update_f = ["quantity"]
            if unit_price is not None:
                update_f.append("unit_price")
            if line_discount is not None or discount_percent is not None:
                update_f.append("line_discount")
            item.save(update_fields=update_f, skip_full_clean=True)
        else:
            item = CartItem(
                cart=cart,
                company=cart.company,
                branch=getattr(cart, "branch", None),
                product=product,
                sale_package=None,
                quantity=qty3(qty),
                unit_price=base_price,
                line_discount=disc_total,
                manual_discount=disc_total,
            )
            item.save(skip_full_clean=True)

        cart.recalc()
        return Response(SaleCartSerializer(cart).data, status=status.HTTP_201_CREATED)


class AgentSaleAddCustomItemAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        cart = get_object_or_404(
            Cart.objects.select_related("company", "branch", "user", "shift")
            .prefetch_related("items"),
            id=pk,
            company=request.user.company,
            status=Cart.Status.ACTIVE,
        )
        ser = CustomCartItemCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        name = ser.validated_data["name"].strip()
        if not name:
            return Response({"name": "Название не может быть пустым."}, status=400)

        price = money(ser.validated_data["price"])
        qty = ser.validated_data.get("quantity", 1)

        item = CartItem.objects.filter(
            cart=cart,
            product__isnull=True,
            custom_name=name,
            unit_price=price,
        ).first()

        if item:
            CartItem.objects.filter(pk=item.pk).update(quantity=F("quantity") + qty)
            item.refresh_from_db(fields=["quantity"])
        else:
            CartItem.objects.create(
                company=cart.company,
                branch=getattr(cart, "branch", None),
                cart=cart,
                product=None,
                custom_name=name,
                unit_price=price,
                quantity=qty,
            )

        cart.recalc()
        return Response(SaleCartSerializer(cart).data, status=status.HTTP_201_CREATED)


class AgentSaleCheckoutAPIView(MarketCashierOnlyMixin, CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @idempotent(body_key=True)
    def post(self, request, pk, *args, **kwargs):
        with transaction.atomic():
            company = self._company() or request.user.company
            branch = self._auto_branch()

            cart_qs = Cart.objects.filter(id=pk, company=company, status=Cart.Status.ACTIVE)
            if hasattr(Cart, "branch"):
                cart_qs = cart_qs.filter(branch=branch) if branch is not None else cart_qs.filter(branch__isnull=True)
            cart = get_object_or_404(cart_qs)

            ser = AgentCheckoutSerializer(data=request.data)
            ser.is_valid(raise_exception=True)

            print_receipt = ser.validated_data["print_receipt"]
            allow_minus = bool(ser.validated_data.get("allow_minus"))
            can_minus = allow_minus and _is_owner_like(request.user)
            client_id = ser.validated_data.get("client_id")
            payment_method = ser.validated_data.get("payment_method") or Sale.PaymentMethod.CASH
            cash_received = ser.validated_data.get("cash_received") or Decimal("0.00")
            cashbox_id = ser.validated_data.get("cashbox_id")  # опционально

            resolved_client = None
            if client_id:
                resolved_client = get_object_or_404(Client, id=client_id, company=company)
                if hasattr(cart, "client_id"):
                    cart.client = resolved_client
                    cart.save(update_fields=["client"])

            acting_agent = _resolve_acting_agent(request, cart, allow_owner_override=True)
            use_main_stock = _should_use_main_stock_in_agent_sale(user=request.user, acting_agent=acting_agent)

            cart.recalc()
            if payment_method == Sale.PaymentMethod.CASH and cash_received < cart.total:
                raise ValidationError({"detail": "Сумма, полученная наличными, меньше суммы продажи."})

            consultant_obj = ser.validated_data.get("consultant_obj")
            consultant_comm_enabled = ser.validated_data.get("consultant_commission_enabled", False)
            consultant_comm_pct = ser.validated_data.get("consultant_commission_percent")

            # ✅ ВАЖНО: checkout_agent_cart должен НЕ требовать shift
            try:
                sale = checkout_agent_cart(
                    cart,
                    agent=acting_agent,
                    use_main_stock=use_main_stock,
                    allow_negative_stock=bool(can_minus and use_main_stock),
                    cashbox_id=cashbox_id,  # можно сохранить кассу в Sale, но без смен
                    client=resolved_client,
                    consultant=consultant_obj,
                    consultant_commission_enabled=consultant_comm_enabled,
                    consultant_commission_percent=consultant_comm_pct,
                )
            except Exception as e:
                raise ValidationError({"detail": str(e)})

            # ✅ гарантируем, что смены нет
            if getattr(sale, "shift_id", None):
                sale.shift = None
                sale.save(update_fields=["shift"])

            pm = payment_method or Sale.PaymentMethod.CASH
            cr = cash_received
            prepay_req = request.data.get("prepayment") or request.data.get("prepayment_amount")
            if prepay_req is not None and str(prepay_req).strip() != "":
                prepay_val = Decimal(str(prepay_req))
            else:
                prepay_val = cr if (cr is not None and cr > Decimal("0.00")) else Decimal("0.00")

            if pm == Sale.PaymentMethod.CASH:
                if cr is None:
                    cr = sale.total
            elif pm == Sale.PaymentMethod.DEBT:
                cr = prepay_val
            else:
                cr = Decimal("0.00")

            if hasattr(sale, "mark_paid") and callable(sale.mark_paid):
                sale.mark_paid(payment_method=pm, cash_received=cr)
            else:
                updates = []
                if hasattr(sale, "payment_method"):
                    sale.payment_method = pm
                    updates.append("payment_method")
                if hasattr(sale, "cash_received"):
                    sale.cash_received = cr
                    updates.append("cash_received")
                if hasattr(sale, "paid_at") and not sale.paid_at:
                    sale.paid_at = timezone.now()
                    updates.append("paid_at")
                if hasattr(sale, "status") and sale.status != Sale.Status.PAID:
                    sale.status = Sale.Status.PAID
                    updates.append("status")
                if updates:
                    sale.save(update_fields=updates)

            if sale.client_id and pm == Sale.PaymentMethod.DEBT:
                if not ClientDeal.objects.filter(sale=sale).exists():
                    from datetime import timedelta
                    debt_amt = sale.total or Decimal("0.00")
                    unlinked_deal = (
                        ClientDeal.objects.filter(
                            company=sale.company,
                            client=sale.client,
                            kind=ClientDeal.Kind.DEBT,
                            sale__isnull=True,
                            created_at__gte=timezone.now() - timedelta(seconds=60),
                        )
                        .order_by("-created_at")
                        .first()
                    )

                    v2_sch_ver = request.data.get("schedule_version")
                    debt_sched = request.data.get("debt_schedule") if isinstance(request.data.get("debt_schedule"), dict) else {}
                    is_v1_req = str(v2_sch_ver).strip().lower() in ("v1", "1") or str(debt_sched.get("schedule_version")).strip().lower() in ("v1", "1")
                    sch_version = "v1" if is_v1_req else "v2"

                    if unlinked_deal and (abs((unlinked_deal.amount or Decimal("0.00")) - debt_amt) <= Decimal("0.01") or unlinked_deal.amount == Decimal("0.00")):
                        unlinked_deal.sale = sale
                        if not unlinked_deal.amount or unlinked_deal.amount == Decimal("0.00"):
                            unlinked_deal.amount = debt_amt
                        if prepay_val > Decimal("0.00") and unlinked_deal.prepayment == Decimal("0.00"):
                            unlinked_deal.prepayment = prepay_val
                        unlinked_deal.save(update_fields=["sale", "amount", "prepayment", "updated_at"])
                    else:
                        d_days = request.data.get("debt_days") or debt_sched.get("count")
                        d_months = request.data.get("debt_months")
                        first_due = request.data.get("first_due_date") or debt_sched.get("first_due_date")
                        int_days = request.data.get("interval_days") or debt_sched.get("interval_days") or 1
                        int_months = request.data.get("interval_months") or debt_sched.get("interval_months") or 1
                        custom_inst = request.data.get("installments") or debt_sched.get("installments")
                        if not d_days and not d_months and not custom_inst and sch_version == "v2":
                            # Без графика — один взнос через 30 дней, а не 30 ежедневных (ТЗ ч.12, 2.2).
                            d_days = 1
                            first_due = first_due or (timezone.localdate() + timedelta(days=30))

                        new_deal = ClientDeal(
                            company=sale.company,
                            branch=sale.branch,
                            client=sale.client,
                            sale=sale,
                            title=f"Продажа в долг №{sale.id}",
                            kind=ClientDeal.Kind.DEBT,
                            amount=debt_amt,
                            prepayment=prepay_val,
                            schedule_version=sch_version,
                            debt_days=int(d_days) if d_days else (None if d_months else 30),
                            debt_months=int(d_months) if d_months else None,
                            interval_days=int(int_days),
                            interval_months=int(int_months),
                            first_due_date=first_due,
                        )
                        if custom_inst and isinstance(custom_inst, (list, tuple)):
                            new_deal._custom_installments = custom_inst
                        new_deal.save()


            try:
                invalidate_cache_pattern(f"analytics:market:{sale.company_id}:")
                invalidate_cache_pattern(f"products:list:{sale.company_id}:")
            except Exception:
                pass

            payload = {
                "sale_id": str(sale.id),
                "status": sale.status,
                "subtotal": f"{sale.subtotal:.2f}",
                "discount_total": f"{sale.discount_total:.2f}",
                "tax_total": f"{sale.tax_total:.2f}",
                "total": f"{sale.total:.2f}",
                "client": str(sale.client_id) if sale.client_id else None,
                "client_name": getattr(sale.client, "full_name", None) if sale.client else None,
                "payment_method": getattr(sale, "payment_method", payment_method),
                "cash_received": f"{getattr(sale, 'cash_received', cash_received):.2f}",
                "shift_id": None,  # ✅ нет смен у агента
                "cashbox_id": str(getattr(sale, "cashbox_id", None)) if getattr(sale, "cashbox_id", None) else None,
            }

            from apps.construction.auto_cashflow import create_auto_cashflow, serialize_auto_cashflows
            from apps.construction.models import CashFlow

            auto_flows = []
            pm_val = getattr(sale, "payment_method", payment_method)
            if pm_val == Sale.PaymentMethod.DEBT:
                prepay_amt = getattr(sale, "cash_received", None) or Decimal("0.00")
                if prepay_amt > 0:
                    cf = create_auto_cashflow(
                        company=sale.company,
                        branch=sale.branch,
                        cashbox=getattr(sale, "cashbox", None),
                        cashbox_id=cashbox_id,
                        user=request.user,
                        type=CashFlow.Type.INCOME,
                        amount=prepay_amt,
                        source_kind=CashFlow.SourceKind.POS_PREPAYMENT,
                        source_id=str(sale.id),
                        name="Предоплата (долг)",
                        source_business_operation_id="Продажа",
                    )
                    if cf:
                        auto_flows.append(cf)
            else:
                if (sale.total or 0) > 0:
                    method_title = sale.get_payment_method_display() if hasattr(sale, "get_payment_method_display") else pm_val
                    cf_name = f"Продажа ({method_title})" if pm_val != Sale.PaymentMethod.CASH else "Продажа"
                    cf = create_auto_cashflow(
                        company=sale.company,
                        branch=sale.branch,
                        cashbox=getattr(sale, "cashbox", None),
                        cashbox_id=cashbox_id,
                        user=request.user,
                        type=CashFlow.Type.INCOME,
                        amount=sale.total,
                        source_kind=CashFlow.SourceKind.POS_SALE,
                        source_id=str(sale.id),
                        name=cf_name,
                        source_business_operation_id="Продажа",
                    )
                    if cf:
                        auto_flows.append(cf)

            payload["cashflows"] = serialize_auto_cashflows(auto_flows)
            cb_obj = getattr(sale, "cashbox", None)
            cb_resolved_id = str(cb_obj.id) if cb_obj else None
            cb_resolved_name = getattr(cb_obj, "name", None) or ("Касса филиала" if getattr(cb_obj, "branch_id", None) else "Касса") if cb_obj else "Касса"
            payload["resolved_cashbox_id"] = cb_resolved_id
            payload["resolved_cashbox_name"] = cb_resolved_name

            if print_receipt:
                payload["receipt_print_path"] = (
                    f"/api/main/pos/sales/{sale.id}/receipt/?wait_ekassa=1&receipt_text=1"
                )

        notify_sale_created(sale, actor=request.user)

        hint = _ekassa_checkout_hint(sale.company)
        if hint:
            payload["ekassa"] = hint

        return Response(payload, status=status.HTTP_201_CREATED)


class AgentCartItemUpdateDestroyAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get_active_cart(self, request, cart_id):
        return get_object_or_404(
            Cart,
            id=cart_id,
            company=request.user.company,
            user=request.user,
            status=Cart.Status.ACTIVE,
        )

    def _get_item_in_cart(self, cart, item_or_product_id):
        item = CartItem.objects.filter(cart=cart, id=item_or_product_id).select_related("product").first()
        if item:
            return item
        item = CartItem.objects.filter(cart=cart, product_id=item_or_product_id).select_related("product").first()
        if item:
            return item
        raise Http404("CartItem not found in this cart.")

    @transaction.atomic
    def patch(self, request, cart_id, item_id, *args, **kwargs):
        cart = self._get_active_cart(request, cart_id)
        item = self._get_item_in_cart(cart, item_id)

        ser = CartItemPatchSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        qty = data.get("quantity")
        
        is_admin = getattr(request.user, "role", None) in ["owner", "admin"]
        
        if qty is not None:
            qty = qty3(qty)
            if qty < 0:
                return Response({"quantity": "Количество не может быть отрицательным."}, status=400)
            if qty == 0:
                if not _is_cart_deletion_authorized(request):
                    return Response({"detail": "Требуется код удаления или время проверки истекло"}, status=status.HTTP_403_FORBIDDEN)
                
                log_cart_item_deletion(item=item, deleted_by=request.user)
                item.delete()
                cart.recalc()
                return Response(SaleCartSerializer(cart).data, status=200)
            if item.product_id and not item.sale_package_id:  # ТЗ 26.10 Б-52 (под флагом hardware_2026_11)
                from apps.main.tz26.hw_units.services import check_line_quantity

                check_line_quantity(request, cart.company, item.product, qty, sale_unit=item.sale_unit)
            item.quantity = qty
            if item.sale_unit_id:
                item.base_quantity = qty3(qty * item.sale_unit.factor)

        unit_price = data.get("unit_price")
        line_discount = data.get("discount_total")
        discount_percent = data.get("discount_percent")

        # Цена и скидка меняются независимо.
        if unit_price is not None:
            item.unit_price = _q2(unit_price)
            item.price_manually_edited = True
        if line_discount is not None or discount_percent is not None:
            current_qty = qty if qty is not None else item.quantity
            current_price = item.unit_price if unit_price is None else _q2(unit_price)
            new_discount = _line_discount_from_request(
                current_price,
                current_qty,
                line_discount,
                discount_percent,
            )
            max_dp = request.user.company.max_discount_percent
            if max_dp is not None and not is_admin:
                if _line_discount_over_limit(current_price, current_qty, new_discount, max_dp):
                    return Response(_discount_limit_error(max_dp), status=status.HTTP_400_BAD_REQUEST)
            item.line_discount = new_discount
            item.manual_discount = new_discount

        update_fields = []
        if qty is not None:
            update_fields.append("quantity")
            update_fields.append("base_quantity")  # ТЗ 26.10 Б-53
        if unit_price is not None:
            update_fields.append("unit_price")
            update_fields.append("price_manually_edited")
        if line_discount is not None or discount_percent is not None:
            update_fields += ["line_discount", "manual_discount"]
        if update_fields:
            item.save(update_fields=update_fields, skip_full_clean=True)
        cart.recalc()
        if item.pk:
            item.refresh_from_db()
        return Response(SaleCartSerializer(cart).data, status=200)

    @transaction.atomic
    def delete(self, request, cart_id, item_id, *args, **kwargs):
        cart = self._get_active_cart(request, cart_id)
        item = self._get_item_in_cart(cart, item_id)
        if not _is_cart_deletion_authorized(request):
            return Response({"detail": "Требуется код удаления или время проверки истекло"}, status=status.HTTP_403_FORBIDDEN)
        log_cart_item_deletion(item=item, deleted_by=request.user)
        item.delete()
        cart.recalc()
        return Response(SaleCartSerializer(cart).data, status=200)


class PosPrinterSettingAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/pos/printer-settings/?device_key=<device_key>
    PUT / PATCH / POST /api/main/pos/printer-settings/
    DELETE /api/main/pos/printer-settings/?device_key=<device_key>

    Синхронизация ESC/POS-конфигурации принтера чеков per-device (или per-branch workstation).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        from apps.main.serializers import PosPrinterSettingSerializer

        company = self._company()
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")

        device_key = (request.query_params.get("device_key") or "").strip()
        if device_key:
            try:
                setting = PosPrinterSetting.objects.select_related("branch", "cashbox").get(
                    company=company, device_key=device_key
                )
                return Response(PosPrinterSettingSerializer(setting, context={"request": request}).data, status=status.HTTP_200_OK)
            except PosPrinterSetting.DoesNotExist:
                raise NotFound({"detail": "Конфигурация принтера для данного устройства не найдена."})

        qs = self._filter_qs_company_branch(
            PosPrinterSetting.objects.select_related("branch", "cashbox").all()
        )
        serializer = PosPrinterSettingSerializer(qs, many=True, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def _save_setting(self, request):
        from apps.main.serializers import PosPrinterSettingSerializer
        from apps.users.models import Branch
        from apps.construction.models import Cashbox

        company = self._company()
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")

        data = request.data or {}
        device_key = (data.get("device_key") or request.query_params.get("device_key") or "").strip()
        if not device_key:
            raise ValidationError({"device_key": ["Поле device_key обязательно."]})

        settings_dict = data.get("settings")
        if settings_dict is None:
            settings_dict = {}
        elif not isinstance(settings_dict, dict):
            raise ValidationError({"settings": ["Поле settings должно быть JSON-объектом (словарем)."]})

        branch = self._auto_branch()
        branch_id = data.get("branch")
        if branch_id:
            try:
                branch = Branch.objects.get(id=branch_id, company=company)
            except (Branch.DoesNotExist, ValueError):
                raise ValidationError({"branch": ["Некорректный филиал."]})

        cashbox = None
        cashbox_id = data.get("cashbox")
        if cashbox_id:
            try:
                cashbox = Cashbox.objects.get(id=cashbox_id, company=company)
            except (Cashbox.DoesNotExist, ValueError):
                raise ValidationError({"cashbox": ["Некорректная касса."]})

        setting, created = PosPrinterSetting.objects.update_or_create(
            company=company,
            device_key=device_key,
            defaults={
                "branch": branch,
                "cashbox": cashbox,
                "settings": settings_dict,
            },
        )
        serializer = PosPrinterSettingSerializer(setting, context={"request": request})
        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        return self._save_setting(request)

    def put(self, request, *args, **kwargs):
        return self._save_setting(request)

    def patch(self, request, *args, **kwargs):
        return self._save_setting(request)

    def delete(self, request, *args, **kwargs):
        company = self._company()
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")

        device_key = (request.query_params.get("device_key") or (request.data or {}).get("device_key") or "").strip()
        if not device_key:
            raise ValidationError({"device_key": ["Поле device_key обязательно для удаления."]})

        deleted_count, _ = PosPrinterSetting.objects.filter(company=company, device_key=device_key).delete()
        if deleted_count == 0:
            raise NotFound({"detail": "Конфигурация принтера для данного устройства не найдена."})

        return Response(status=status.HTTP_204_NO_CONTENT)
