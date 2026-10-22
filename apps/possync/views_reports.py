"""
Отчётные read-only эндпоинты (ТЗ отчёты, октябрь 2026).

GET api/main/products/{id}/history/            — история движения одного товара
GET api/main/analytics/market/hours-sources/   — продажи по часам и по источникам
GET api/main/products/sales-velocity/          — скорость продаж и «на сколько дней хватит»

Схему БД не меняем: всё считается агрегатами из существующих таблиц.
Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import connection
from django.db.models import Case, Count, DecimalField, Exists, F, OuterRef, Q, Sum, Value, When
from django.db.models.functions import Coalesce, ExtractHour
from django.utils import timezone
from rest_framework import permissions
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.analytics_market import _get_active_branch, _get_company, _get_period, _parse_bounds
from apps.main.models import (
    BranchTransferItem,
    Product,
    ProductInventoryItem,
    Sale,
    SaleItem,
    SaleReturn,
    ShowcaseOrder,
    SupplierReceiptItem,
    SupplierReturnItem,
)
from apps.main.views import CompanyBranchRestrictedMixin

from .models import StockAdjustment, StockAdjustmentLine

BISHKEK = ZoneInfo("Asia/Bishkek")
D0 = Decimal("0")
SOLD_STATUSES = (Sale.Status.PAID, Sale.Status.DEBT, Sale.Status.PARTIALLY_RETURNED)


def _s(v):
    return None if v is None else str(v)


def _user_name(first, last, email=None) -> str | None:
    full = f"{first or ''} {last or ''}".strip()
    return full or email or None


def _int_param(qp, name, default, lo, hi):
    raw = qp.get(name)
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: "Нужно целое число."})
    return max(lo, min(v, hi))


# ======================================================================
# 1. История товара
# ======================================================================

ADJ_TYPE = {
    StockAdjustment.Kind.RECEIVE: "receive",
    StockAdjustment.Kind.WRITE_OFF: "write_off",
    StockAdjustment.Kind.COUNT_FIX: "count_fix",
    StockAdjustment.Kind.RETURN_TO_SUPPLIER: "supplier_return",
}


class ProductHistoryAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/products/{id}/history/?date_from&date_to&branch&page&page_size
    Каждый источник отдаёт верхние `offset+page_size` строк по убыванию времени,
    затем строки сливаются и режутся страницей: ни N+1, ни полной выгрузки.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, product_id, *args, **kwargs):
        company = _get_company(request.user)
        if company is None:
            raise NotFound()
        product = Product.objects.filter(pk=product_id, company=company).first()
        if product is None:
            raise NotFound("Товар не найден.")
        branch = self._auto_branch()
        if branch is not None and product.branch_id not in (None, branch.id):
            raise NotFound("Товар не найден.")

        qp = request.query_params
        start, end = _parse_bounds(qp.get("date_from"), qp.get("date_to"), param_hint="date")
        page = _int_param(qp, "page", 1, 1, 10**6)
        page_size = _int_param(qp, "page_size", 50, 1, 200)
        cap = page * page_size

        sources = [
            self._sales(company, product, branch, start, end),
            self._returns(company, product, branch, start, end),
            self._receipts(company, product, branch, start, end),
            self._supplier_returns(company, product, branch, start, end),
            self._adjustments(company, product, branch, start, end),
            self._inventory(company, product, branch, start, end),
            self._transfers_out(company, product, branch, start, end),
            self._transfers_in(company, product, branch, start, end),
        ]
        total = 0
        rows = []
        for qs, build in sources:
            total += qs.count()
            rows.extend(build(r) for r in qs.order_by("-at")[:cap])
        rows.sort(key=lambda r: r["_at"], reverse=True)
        chunk = rows[(page - 1) * page_size: page * page_size]
        for r in chunk:
            r["at"] = r.pop("_at").isoformat()
        return Response({
            "product": {"id": str(product.id), "name": product.name, "quantity": _s(product.quantity)},
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": chunk,
        })

    # ---- helpers ----
    @staticmethod
    def _period(qs, field, start, end):
        if start:
            qs = qs.filter(**{f"{field}__gte": start})
        if end:
            qs = qs.filter(**{f"{field}__lt": end})
        return qs

    @staticmethod
    def _row(at, typ, delta, price, doc_id, doc_number, first, last, email, branch_id, branch_name,
             before=None, after=None, **extra):
        return {
            "_at": at,
            "type": typ,
            "qty_delta": _s(delta),
            "price": _s(price),
            "doc_id": _s(doc_id),
            "doc_number": _s(doc_number),
            "user": _user_name(first, last, email),
            "branch": {"id": str(branch_id), "name": branch_name} if branch_id else None,
            "before": _s(before),
            "after": _s(after),
            **extra,
        }

    # ---- источники ----
    def _sales(self, company, product, branch, start, end):
        returned = SaleReturn.objects.filter(sale=OuterRef("sale_id"))
        qs = SaleItem.objects.filter(company=company, product=product).filter(
            Q(sale__status__in=SOLD_STATUSES)
            | (Q(sale__status=Sale.Status.CANCELED) & Exists(returned))
        )
        if branch is not None:
            qs = qs.filter(sale__branch=branch)
        qs = qs.annotate(at=Coalesce("sale__paid_at", "sale__created_at"))
        qs = self._period(qs, "at", start, end)
        qs = qs.values(
            "at", "quantity", "unit_price", "sale_id", "id", "sale__doc_number", "sale__status",
            "sale__user__first_name", "sale__user__last_name", "sale__user__email",
            "sale__branch_id", "sale__branch__name",
        )

        # Частичный возврат уменьшает quantity строки: восстанавливаем проданное количество.
        def build(r):
            return self._row(
                r["at"], "sale", -(r["quantity"] + self._partial_back.get(str(r["id"]), D0)),
                r["unit_price"], r["sale_id"], r["sale__doc_number"],
                r["sale__user__first_name"], r["sale__user__last_name"], r["sale__user__email"],
                r["sale__branch_id"], r["sale__branch__name"],
            )

        self._partial_back = self._partial_returns_map(company, product)
        return qs, build

    @staticmethod
    def _return_lines(ret, product_id):
        for ln in ret.returned_items or []:
            if not isinstance(ln, dict):
                continue
            if str(ln.get("product") or ln.get("product_id") or "") == str(product_id):
                yield ln

    def _partial_returns_map(self, company, product):
        """sale_item_id -> суммарно возвращено частичными возвратами (строка чека уменьшена)."""
        out: dict[str, Decimal] = {}
        for ret in self._returns_qs(company, product).filter(is_full=False):
            for ln in self._return_lines(ret, product.id):
                try:
                    q = Decimal(str(ln.get("qty") or ln.get("quantity") or 0))
                except Exception:
                    continue
                key = str(ln.get("sale_item") or "")
                out[key] = out.get(key, D0) + q
        return out

    @staticmethod
    def _returns_qs(company, product):
        cond = Q(sale__items__product=product)
        if connection.vendor == "postgresql":
            # частично возвращённая строка могла быть удалена из чека целиком
            cond |= Q(returned_items__contains=[{"product": str(product.id)}])
        return SaleReturn.objects.filter(company=company).filter(cond).distinct()

    def _returns(self, company, product, branch, start, end):
        qs = self._returns_qs(company, product)
        if branch is not None:
            qs = qs.filter(sale__branch=branch)
        qs = qs.annotate(at=F("created_at"))
        qs = self._period(qs, "at", start, end)
        qs = qs.select_related("sale", "sale__branch", "user")

        def build(ret):
            lines = list(self._return_lines(ret, product.id))
            qty = D0
            amount = D0
            restocked = False
            for ln in lines:
                try:
                    q = Decimal(str(ln.get("qty") or ln.get("quantity") or 0))
                except Exception:
                    q = D0
                if ln.get("restock", True):
                    qty += q
                    restocked = True
                amount += q
            price = None
            if lines and lines[0].get("price") is not None:
                price = lines[0]["price"]
            sale = ret.sale
            u = ret.user
            return self._row(
                ret.at, "return", qty, price, ret.sale_id, sale.doc_number if sale else None,
                getattr(u, "first_name", None), getattr(u, "last_name", None), getattr(u, "email", None),
                sale.branch_id if sale else None, sale.branch.name if sale and sale.branch_id else None,
                restocked=restocked, is_defect=ret.is_defect, reason=ret.reason or None,
            )

        return qs, build

    def _receipts(self, company, product, branch, start, end):
        qs = SupplierReceiptItem.objects.filter(product=product, receipt__company=company)
        if branch is not None:
            qs = qs.filter(receipt__branch=branch)
        qs = qs.annotate(at=F("receipt__created_at"))
        qs = self._period(qs, "at", start, end).values(
            "at", "qty", "purchase_price", "receipt_id",
            "receipt__created_by__first_name", "receipt__created_by__last_name", "receipt__created_by__email",
            "receipt__branch_id", "receipt__branch__name",
        )
        return qs, lambda r: self._row(
            r["at"], "receipt", r["qty"], r["purchase_price"], r["receipt_id"], None,
            r["receipt__created_by__first_name"], r["receipt__created_by__last_name"],
            r["receipt__created_by__email"], r["receipt__branch_id"], r["receipt__branch__name"],
        )

    def _supplier_returns(self, company, product, branch, start, end):
        qs = SupplierReturnItem.objects.filter(
            product=product, supplier_return__company=company, supplier_return__status="posted",
        )
        if branch is not None:
            qs = qs.filter(supplier_return__branch=branch)
        qs = qs.annotate(at=F("supplier_return__created_at"))
        qs = self._period(qs, "at", start, end).values(
            "at", "qty", "purchase_price", "supplier_return_id",
            "supplier_return__created_by__first_name", "supplier_return__created_by__last_name",
            "supplier_return__created_by__email", "supplier_return__branch_id", "supplier_return__branch__name",
        )
        return qs, lambda r: self._row(
            r["at"], "supplier_return", -r["qty"], r["purchase_price"], r["supplier_return_id"], None,
            r["supplier_return__created_by__first_name"], r["supplier_return__created_by__last_name"],
            r["supplier_return__created_by__email"], r["supplier_return__branch_id"],
            r["supplier_return__branch__name"],
        )

    def _adjustments(self, company, product, branch, start, end):
        qs = StockAdjustmentLine.objects.filter(product=product, adjustment__company=company)
        if branch is not None:
            qs = qs.filter(adjustment__branch=branch)
        qs = qs.annotate(at=F("adjustment__created_at"))
        qs = self._period(qs, "at", start, end).values(
            "at", "delta", "before", "after", "purchase_price", "adjustment_id", "adjustment__kind",
            "adjustment__seq", "adjustment__reason", "adjustment__created_by__first_name",
            "adjustment__created_by__last_name", "adjustment__created_by__email",
            "adjustment__branch_id", "adjustment__branch__name",
        )

        def build(r):
            kind = r["adjustment__kind"]
            prefix = StockAdjustment.PREFIX.get(kind, "ДК")
            return self._row(
                r["at"], ADJ_TYPE.get(kind, "other"), r["delta"], r["purchase_price"], r["adjustment_id"],
                f"{prefix}-{r['adjustment__seq']:06d}",
                r["adjustment__created_by__first_name"], r["adjustment__created_by__last_name"],
                r["adjustment__created_by__email"], r["adjustment__branch_id"], r["adjustment__branch__name"],
                before=r["before"], after=r["after"], reason=r["adjustment__reason"] or None,
            )

        return qs, build

    def _inventory(self, company, product, branch, start, end):
        qs = ProductInventoryItem.objects.filter(
            product=product, session__company=company, session__status="applied",
        )
        if branch is not None:
            qs = qs.filter(session__branch=branch)
        qs = qs.annotate(at=Coalesce("session__applied_at", "session__created_at"))
        qs = self._period(qs, "at", start, end).values(
            "at", "quantity_before", "quantity_fact", "session_id",
            "session__created_by__first_name", "session__created_by__last_name", "session__created_by__email",
            "session__branch_id", "session__branch__name",
        )
        return qs, lambda r: self._row(
            r["at"], "inventory", r["quantity_fact"] - r["quantity_before"], None, r["session_id"], None,
            r["session__created_by__first_name"], r["session__created_by__last_name"],
            r["session__created_by__email"], r["session__branch_id"], r["session__branch__name"],
            before=r["quantity_before"], after=r["quantity_fact"],
        )

    def _transfers(self, company, branch, start, end, filt, branch_field, typ, sign):
        qs = BranchTransferItem.objects.filter(filt, transfer__company=company, transfer__status="completed")
        if branch is not None:
            qs = qs.filter(**{f"transfer__{branch_field}": branch})
        qs = qs.annotate(at=F("transfer__created_at"))
        qs = self._period(qs, "at", start, end).values(
            "at", "quantity", "price", "transfer_id", "transfer__number",
            "transfer__created_by__first_name", "transfer__created_by__last_name", "transfer__created_by__email",
            f"transfer__{branch_field}_id", f"transfer__{branch_field}__name",
        )
        return qs, lambda r: self._row(
            r["at"], typ, sign * r["quantity"], r["price"], r["transfer_id"], r["transfer__number"],
            r["transfer__created_by__first_name"], r["transfer__created_by__last_name"],
            r["transfer__created_by__email"], r[f"transfer__{branch_field}_id"],
            r[f"transfer__{branch_field}__name"],
        )

    def _transfers_out(self, company, product, branch, start, end):
        return self._transfers(company, branch, start, end, Q(product=product), "from_branch", "transfer_out", -1)

    def _transfers_in(self, company, product, branch, start, end):
        return self._transfers(company, branch, start, end, Q(dest_product=product), "to_branch", "transfer_in", 1)
