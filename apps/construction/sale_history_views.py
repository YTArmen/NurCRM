# apps/construction/views/shift_sales.py
import uuid as uuid_lib
from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from django.db.models import Q, Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination

from apps.construction.models import CashShift
from apps.main.models import Sale, SaleItem
from apps.construction.sale_history_serializer import SaleHistorySerializer

from apps.construction.utils import (
    get_company_from_user as _get_company,
    is_owner_like as _is_owner_like,
)


def assert_shift_access(user, shift: CashShift):
    if _is_owner_like(user):
        return
    if shift.cashier_id != getattr(user, "id", None):
        raise PermissionDenied("Нельзя смотреть продажи чужой смены.")


def _parse_dt_or_date(s: str, *, end_of_day: bool = False):
    if not s:
        return None

    s = s.strip()
    try:
        if len(s) == 10:
            d = datetime.fromisoformat(s).date()
            dt = datetime.combine(d, time.max if end_of_day else time.min)
        else:
            dt = datetime.fromisoformat(s)
    except ValueError:
        return None

    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _parse_decimal(s: str):
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _choice_map_from_textchoices(textchoices):
    # {"paid","PAID"} -> value
    out = {}
    for k, _ in getattr(textchoices, "choices", []):
        out[str(k).lower()] = k
        out[str(k)] = k
    return out


STATUS_MAP = _choice_map_from_textchoices(Sale.Status)
PM_MAP = _choice_map_from_textchoices(Sale.PaymentMethod)

ALLOWED_ORDERING = {
    "created_at", "-created_at",
    "total", "-total",
    "paid_at", "-paid_at",
    "doc_number", "-doc_number",
}


class ShiftSalesPagination(PageNumberPagination):
    """
    Глобальный PageNumberPagination не даёт клиенту менять размер страницы, из-за
    чего фронт видел только первые 100 чеков смены и строил фильтр «Кассир» по
    неполному списку (L1 в shift-sales-cashier-filter-backend.md).
    """
    page_size_query_param = "page_size"
    max_page_size = 500


def _parse_cashier_ids(raw: str):
    """`?cashier=<uuid>` или несколько через запятую. Невалидный uuid → 400."""
    ids = []
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(uuid_lib.UUID(chunk))
        except (ValueError, AttributeError, TypeError):
            raise ValidationError({"cashier": "Ожидается UUID сотрудника (можно несколько через запятую)."})
    return ids


# ─────────────────────────────────────────────────────────────
# view
# ─────────────────────────────────────────────────────────────
class CashShiftSalesListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = SaleHistorySerializer
    pagination_class = ShiftSalesPagination

    def get_queryset(self):
        user = self.request.user
        company = _get_company(user)
        if not company:
            return Sale.objects.none()

        shift = get_object_or_404(
            CashShift.objects.select_related("company", "cashier", "cashbox"),
            id=self.kwargs["pk"],
            company=company,
        )
        assert_shift_access(user, shift)

        # base qs
        qs = (
            Sale.objects
            .filter(company=company, shift=shift)
            .select_related("cashbox", "shift", "client", "user")
            .only(
                "id", "company_id", "branch_id", "shift_id", "cashbox_id",
                "status", "doc_number",
                "payment_method", "cash_received",
                "subtotal", "discount_total", "tax_total", "total",
                "created_at", "paid_at",
                "client_id", "user_id",
            )
        )

        # items prefetch (оптимально)
        item_qs = (
            SaleItem.objects
            .select_related("product")
            .only(
                "id", "sale_id", "product_id",
                "name_snapshot", "barcode_snapshot",
                "unit_price", "quantity", "line_discount",
                "product__name",
            )
            .order_by("id")
        )
        qs = qs.prefetch_related(Prefetch("items", queryset=item_qs))

        p = self.request.query_params

        # ---- status ----
        raw_status = (p.get("status") or "").strip()
        if raw_status:
            val = STATUS_MAP.get(raw_status.lower())
            if val is None:
                raise ValidationError({"status": "Допустимо: new, paid, canceled"})
            qs = qs.filter(status=val)

        # ---- payment_method ----
        raw_pm = (p.get("payment_method") or "").strip()
        if raw_pm:
            val = PM_MAP.get(raw_pm.lower())
            if val is None:
                raise ValidationError({"payment_method": "Допустимо: cash, transfer"})
            if val == Sale.PaymentMethod.TRANSFER:
                # ТЗ 12.11: безнал разных банков тоже считается переводом.
                qs = qs.filter(payment_method__in=[
                    Sale.PaymentMethod.TRANSFER, Sale.PaymentMethod.MBANK, Sale.PaymentMethod.OPTIMA,
                    Sale.PaymentMethod.OBANK, Sale.PaymentMethod.BAKAI,
                ])
            else:
                qs = qs.filter(payment_method=val)

        # ---- cashier ----
        # Фильтруем по продавцу конкретного чека (Sale.user — источник
        # cashier_display), а НЕ по владельцу смены: в общей смене на одну кассу
        # чеки пробивают несколько сотрудников.
        raw_cashier = (p.get("cashier") or "").strip()
        if raw_cashier:
            cashier_ids = _parse_cashier_ids(raw_cashier)
            if cashier_ids:
                qs = qs.filter(user_id__in=cashier_ids)

        # ---- q search ----
        q = (p.get("q") or "").strip()
        if q:
            cond = Q(id__icontains=q)

            if q.isdigit():
                # doc_number
                try:
                    cond |= Q(doc_number=int(q))
                except Exception:
                    pass

            # client name (если есть поле)
            if hasattr(Sale, "client") and hasattr(Sale._meta.get_field("client").related_model, "name"):
                cond |= Q(client__name__icontains=q)

            qs = qs.filter(cond)

        # ---- date range ----
        raw_from = (p.get("date_from") or "").strip()
        raw_to = (p.get("date_to") or "").strip()

        date_from = _parse_dt_or_date(raw_from, end_of_day=False) if raw_from else None
        date_to = _parse_dt_or_date(raw_to, end_of_day=(len(raw_to) == 10)) if raw_to else None

        if raw_from and date_from is None:
            raise ValidationError({"date_from": "Формат: 2025-12-16 или 2025-12-16T10:30:00"})
        if raw_to and date_to is None:
            raise ValidationError({"date_to": "Формат: 2025-12-16 или 2025-12-16T10:30:00"})

        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)

        # ---- totals range ----
        min_total = _parse_decimal(p.get("min_total"))
        max_total = _parse_decimal(p.get("max_total"))

        if p.get("min_total") is not None and min_total is None:
            raise ValidationError({"min_total": "Неверное число"})
        if p.get("max_total") is not None and max_total is None:
            raise ValidationError({"max_total": "Неверное число"})

        if min_total is not None:
            qs = qs.filter(total__gte=min_total)
        if max_total is not None:
            qs = qs.filter(total__lte=max_total)

        # ---- ordering ----
        ordering = (p.get("ordering") or "-created_at").strip()
        if ordering not in ALLOWED_ORDERING:
            raise ValidationError({"ordering": f"Допустимо: {', '.join(sorted(ALLOWED_ORDERING))}"})

        return qs.order_by(ordering)
