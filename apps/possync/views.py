"""
Эндпоинты ТЗ-28 (скорость и пробелы кассы/телефона) и ТЗ-27 §4 (идемпотентность).

GET  api/main/bootstrap/?cashbox_id=&device_id=     — старт приложения одним запросом (§1)
POST api/main/stock/movements/                        — остаток «на величину», атомарно, с ключом (§3)
GET  api/main/stock/movements/?from=&to=&kind=&product_id=
GET  api/main/products/stock/?updated_since=<ISO>     — только остатки, лёгкий ответ (§2.2)
GET  api/construction/shifts/current/?cashbox=<id>    — текущая смена кассира с итогами (§6)
GET  api/main/clients/{id}/debts/                     — долги клиента одним запросом (§7)
GET  api/main/products/barcode/{code}/                — плюс штрихкоды вариантов (§2.6, ТЗ-27 §8)
POST api/main/pos/checkout/                           — тот же checkout + 409 idempotency_conflict (§8.4)
Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import F, Max, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.construction.models import Cashbox, CashShift
from apps.main.kassa_stage3 import ProductVariantSerializer
from apps.main.kassa_views import ClientDirectDebtAPIView, PosQuickCheckoutAPIView, _company, _open_debts
from apps.main.models import Client, ClientDeal, DealInstallment, Product, ProductDeletion, ProductVariant, Sale, SaleItem
from apps.main.pos_utils import money, qty3
from apps.main.price_check import catalog_version
from apps.main.pos_serializers import _is_owner_like
from apps.main.views import (
    ClientRetrieveUpdateDestroyAPIView,
    CompanyBranchRestrictedMixin,
    ProductByBarcodeAPIView,
    _filter_products_company_only,
)
from apps.users.models import Branch
from apps.users.serializers import CompanySerializer, UserSerializer, company_addons_payload, company_feature_codes
from core.exceptions import ApiError

from apps.integrations.idempotency import _request_key, idempotent

from .debt_limits import check_company_debt_limits, check_debt_limits, client_debt_payload
from .deletions import write_deletions
from .schedule import apply_debt_schedule, schedule_payload
from .packs import annotate_items, convert_pack_items, save_pack_lines
from .models import ClientDebtLimit, StockAdjustment, StockAdjustmentLine

ZERO = Decimal("0.00")
Q3 = Decimal("0.001")


def _d3(v) -> str:
    return str(Decimal(v or 0).quantize(Q3))


def _parse_since(raw: str, field: str):
    """ISO 8601 или целое — миллисекунды с эпохи (server_time из прошлого ответа)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=dt_timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ApiError("validation_error", "Некорректное время.", fields={field: ["Ожидается ISO 8601 или мс."]})
    dt = parse_datetime(raw.replace(" ", "+"))
    if dt is None:
        raise ApiError("validation_error", "Ожидается дата-время ISO 8601.", fields={field: ["Ожидается ISO 8601."]})
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


# ----------------------------------------------------------------------
# Смена: текущая, с итогами (§6)
# ----------------------------------------------------------------------

def shift_payload(shift: CashShift) -> dict:
    totals = shift.calc_live_totals()
    try:
        breakdown = shift.calc_payment_breakdown()
    except Exception:  # noqa: BLE001 — старые смены без оплат
        breakdown = {}
    try:
        returns = shift.returns_summary()
    except Exception:  # noqa: BLE001
        returns = {}
    cashier = shift.cashier
    cashbox = shift.cashbox
    return {
        "id": str(shift.id),
        "status": shift.status,
        "opened_at": shift.opened_at.isoformat() if shift.opened_at else None,
        "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
        "branch_id": str(shift.branch_id) if shift.branch_id else None,
        "cashier": {
            "id": str(cashier.id),
            "name": f"{cashier.first_name} {cashier.last_name}".strip() or cashier.email,
        } if cashier else None,
        "cashbox": {"id": str(cashbox.id), "name": cashbox.name} if cashbox else None,
        "opening_cash": str(money(shift.opening_cash or ZERO)),
        "expected_cash": str(money(totals.get("expected_cash") or ZERO)),
        "drawer_expected_cash": str(money(totals.get("drawer_expected_cash") or ZERO)),
        "sales_count": int(totals.get("sales_count") or 0),
        "sales_total": str(money(totals.get("sales_total") or ZERO)),
        "cash_sales_total": str(money(totals.get("cash_sales_total") or ZERO)),
        "noncash_sales_total": str(money(totals.get("noncash_sales_total") or ZERO)),
        "income_total": str(money(totals.get("income_total") or ZERO)),
        "expense_total": str(money(totals.get("expense_total") or ZERO)),
        "debt_prepayments_cash": str(money(totals.get("debt_prepayments_cash") or ZERO)),
        "debt_prepayments_noncash": str(money(totals.get("debt_prepayments_noncash") or ZERO)),
        "by_method": breakdown if isinstance(breakdown, dict) else {},
        "returns_count": int(returns.get("returns_count") or 0) if isinstance(returns, dict) else 0,
        "returns_sum": str(returns.get("returns_total") or "0.00") if isinstance(returns, dict) else "0.00",
        "returns_cash": str(returns.get("returns_cash") or "0.00") if isinstance(returns, dict) else "0.00",
        "returns_noncash": str(returns.get("returns_noncash") or "0.00") if isinstance(returns, dict) else "0.00",
        "warning": shift.cashbox_shared_warning() if hasattr(shift, "cashbox_shared_warning") else None,
    }


def find_current_shift(company, user, cashbox_id=None):
    qs = CashShift.objects.filter(company=company, status=CashShift.Status.OPEN).select_related(
        "cashier", "cashbox", "cashbox__branch"
    )
    if cashbox_id:
        qs = qs.filter(cashbox_id=cashbox_id)
    own = qs.filter(cashier=user).order_by("-opened_at").first()
    if own is not None:
        return own
    if cashbox_id:
        # На кассе открыта чужая смена — вернём её, касса покажет предупреждение.
        return qs.order_by("-opened_at").first()
    return None


class ShiftCurrentAPIView(APIView):
    """GET /api/construction/shifts/current/?cashbox=<id> → смена с итогами или 404 shift_not_open."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = _company(request)
        cashbox_id = (request.query_params.get("cashbox") or request.query_params.get("cashbox_id") or "").strip() or None
        shift = find_current_shift(company, request.user, cashbox_id)
        if shift is None:
            raise ApiError("shift_not_open", "Смена не открыта.", status_code=status.HTTP_404_NOT_FOUND,
                           data={"cashbox_id": cashbox_id})
        data = shift_payload(shift)
        data["is_own"] = shift.cashier_id == request.user.id
        return Response(data)


# ----------------------------------------------------------------------
# Старт одним запросом (§1)
# ----------------------------------------------------------------------

class BootstrapAPIView(CompanyBranchRestrictedMixin, APIView):
    """GET /api/main/bootstrap/?cashbox_id=&device_id= — всё для старта кассы/телефона одним ответом."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = _company(request)
        user = request.user
        cashbox_id = (request.query_params.get("cashbox_id") or request.query_params.get("cashbox") or "").strip() or None
        device_id = (request.query_params.get("device_id") or "").strip() or None
        server_time = timezone.now()

        u = UserSerializer(user, context={"request": request}).data
        c = CompanySerializer(company, context={"request": request}).data

        cashboxes = [
            {"id": str(cb.id), "name": cb.name, "branch_id": str(cb.branch_id) if cb.branch_id else None,
             "role": getattr(cb, "role", None)}
            for cb in Cashbox.objects.filter(company=company).order_by("name")
        ]
        shift = find_current_shift(company, user, cashbox_id)

        products_qs = _filter_products_company_only(self, Product.objects.all())
        cv = catalog_version(products_qs)

        settings_payload = {
            "scale_barcode_mode": c.get("scale_barcode_mode"),
            "scale_barcode_layout": c.get("scale_barcode_layout"),
            "scale_barcode_amount_unit": c.get("scale_barcode_amount_unit"),
            "max_discount_percent": c.get("max_discount_percent"),
            "debt_schedule_version": getattr(company, "debt_schedule_version", None),
            "pos_quick_slots": getattr(company, "pos_quick_slots", None),
            "appointment_work_start": c.get("appointment_work_start"),
            "appointment_work_end": c.get("appointment_work_end"),
            "cashflow_requests_enabled": getattr(company, "cashflow_requests_enabled", None),
        }
        legacy_flags = {k: getattr(user, k) for k in dir(type(user)) if k.startswith("can_view_") and not callable(getattr(user, k, None))}

        return Response({
            "server_time": server_time.isoformat(timespec="milliseconds"),
            "device_id": device_id,
            "user": {
                "id": u.get("id"),
                "email": u.get("email"),
                "first_name": u.get("first_name"),
                "last_name": u.get("last_name"),
                "full_name": f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip() or u.get("email"),
                "role": u.get("role"),
                "role_display": u.get("role_display"),
                "is_owner": getattr(user, "owned_company", None) is not None or (user.role == "owner"),
                "branch_id": u.get("primary_branch_id"),
                "branch_ids": u.get("branch_ids") or [],
            },
            "company": {
                "id": c.get("id"),
                "name": c.get("name"),
                "slug": c.get("slug"),
                "market_sphere": c.get("market_sphere"),
                "market_spheres": getattr(company, "market_spheres", None) or [],
                "tariff": c.get("subscription_plan"),
                "tariff_name": getattr(getattr(company, "subscription_plan", None), "name", None),
                "end_date": c.get("end_date"),
                "inn": c.get("inn"),
                "address": c.get("address"),
                "phone": c.get("phone"),
                "currency": "KGS",
            },
            # ТЗ-19 (права по функциям) ещё не реализовано: пока флаги пользователя «как есть».
            "permissions": [],
            "legacy_flags": legacy_flags,
            "features": {
                "codes": company_feature_codes(company),
                "addons": company_addons_payload(company),
            },
            "cashboxes": cashboxes,
            "shift": shift_payload(shift) if shift else None,
            "catalog_version": cv.get("version"),
            "catalog_updated_at": cv.get("updated_at"),
            "products_total": cv.get("products_total"),
            # Курсор для products/list/?updated_since= и products/stock/?updated_since=
            "catalog_cursor": server_time.isoformat(timespec="milliseconds"),
            "settings": settings_payload,
        })


# ----------------------------------------------------------------------
# Остаток на величину (§3)
# ----------------------------------------------------------------------

class StockLineSerializer(serializers.Serializer):
    product_id = serializers.UUIDField()
    variant_id = serializers.UUIDField(required=False, allow_null=True)
    delta = serializers.DecimalField(max_digits=14, decimal_places=3, required=False)
    set_to = serializers.DecimalField(max_digits=14, decimal_places=3, required=False, allow_null=True)
    purchase_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)

    def validate(self, attrs):
        if attrs.get("delta") is None and attrs.get("set_to") is None:
            raise serializers.ValidationError({"delta": "Укажите delta (на сколько) или set_to (точное количество)."})
        if attrs.get("delta") is not None and attrs["delta"] == 0 and attrs.get("set_to") is None:
            raise serializers.ValidationError({"delta": "Величина не может быть 0."})
        return attrs


class StockMovementSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=StockAdjustment.Kind.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    branch_id = serializers.UUIDField(required=False, allow_null=True)
    source = serializers.ChoiceField(choices=StockAdjustment.Source.choices, required=False, default="kassa")
    device_id = serializers.CharField(required=False, allow_blank=True, default="", max_length=64)
    allow_negative = serializers.BooleanField(required=False, default=False)
    lines = StockLineSerializer(many=True)

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("Добавьте хотя бы одну строку.")
        return lines


def _movement_payload(adj: StockAdjustment, lines, cv: dict | None = None) -> dict:
    return {
        "id": str(adj.id),
        "number": adj.number,
        "kind": adj.kind,
        "reason": adj.reason,
        "source": adj.source,
        "branch_id": str(adj.branch_id) if adj.branch_id else None,
        "created_by": str(adj.created_by_id) if adj.created_by_id else None,
        "created_at": adj.created_at.isoformat() if adj.created_at else None,
        "lines": [
            {
                "product_id": str(ln.product_id),
                "variant_id": str(ln.variant_id) if ln.variant_id else None,
                "delta": _d3(ln.delta),
                "before": _d3(ln.before),
                "after": _d3(ln.after),
                "purchase_price": str(money(ln.purchase_price)) if ln.purchase_price is not None else None,
            }
            for ln in lines
        ],
        **({"catalog_version": cv.get("version"), "catalog_updated_at": cv.get("updated_at")} if cv else {}),
    }


class StockMovementsAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    POST /api/main/stock/movements/  (Idempotency-Key)
    { "kind": "receive|write_off|count_fix|return_to_supplier", "reason": "...", "branch_id": "...",
      "source": "kassa|phone|web",
      "lines": [{"product_id": "...", "variant_id": null, "delta": "24.000", "purchase_price": null}] }
    → { "id", "number": "ПР-000123", "lines": [{"product_id", "before", "after"}], "catalog_version" }

    Остаток меняется одним UPDATE … SET quantity = quantity + delta под блокировкой строки:
    две кассы, принимающие один товар одновременно, не затирают друг друга.
    """

    permission_classes = [permissions.IsAuthenticated]

    @idempotent(body_key=True)
    def post(self, request, *args, **kwargs):
        company = _company(request)
        ser = StockMovementSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        key = _request_key(request, True) or None

        branch = None
        if data.get("branch_id"):
            branch = get_object_or_404(Branch, pk=data["branch_id"], company=company)
        else:
            branch = self._auto_branch()

        product_ids = {ln["product_id"] for ln in data["lines"]}

        with transaction.atomic():
            # Блокируем строки товаров в детерминированном порядке — без взаимных блокировок.
            products = {
                p.id: p
                for p in Product.objects.select_for_update().filter(company=company, id__in=product_ids).order_by("id")
            }
            missing = [str(pid) for pid in product_ids if pid not in products]
            if missing:
                raise ApiError("not_found", "Товар не найден.", status_code=404, data={"product_ids": missing})

            variant_ids = {ln["variant_id"] for ln in data["lines"] if ln.get("variant_id")}
            variants = {
                v.id: v
                for v in ProductVariant.objects.select_for_update().filter(company=company, id__in=variant_ids).order_by("id")
            } if variant_ids else {}

            adj = self._create_adjustment(company, branch, data, key, request)

            now = timezone.now()
            out_lines = []
            # Один товар может встретиться в нескольких строках: ведём текущий остаток локально.
            running = {pid: Decimal(p.quantity or 0) for pid, p in products.items()}
            running_v = {vid: Decimal(v.quantity or 0) for vid, v in variants.items()}

            for idx, ln in enumerate(data["lines"]):
                product = products[ln["product_id"]]
                if product.kind == Product.Kind.SERVICE:
                    raise ApiError("validation_error", f"«{product.name}» — услуга, у неё нет остатка.",
                                   fields={f"lines[{idx}].product_id": ["Услуга без остатка."]})
                variant = None
                if ln.get("variant_id"):
                    variant = variants.get(ln["variant_id"])
                    if variant is None or variant.product_id != product.id:
                        raise ApiError("not_found", "Вариант не найден.", status_code=404,
                                       data={"variant_id": str(ln["variant_id"])})

                target_before = running_v[variant.id] if variant else running[product.id]
                if ln.get("set_to") is not None:
                    delta = qty3(Decimal(ln["set_to"])) - qty3(target_before)
                else:
                    delta = qty3(Decimal(ln["delta"]))
                after = qty3(target_before + delta)
                if after < 0 and not data.get("allow_negative"):
                    raise ApiError(
                        "insufficient_stock",
                        f"Товара «{product.name}» осталось {_d3(target_before)}.",
                        status_code=status.HTTP_409_CONFLICT,
                        data={"product_id": str(product.id), "variant_id": str(variant.id) if variant else None,
                              "available": _d3(target_before), "requested": _d3(-delta)},
                    )

                if variant:
                    ProductVariant.objects.filter(pk=variant.pk).update(quantity=F("quantity") + delta, updated_at=now)
                    running_v[variant.id] = after
                # Остаток товара — всегда (у товара с вариантами это сумма по вариантам).
                Product.objects.filter(pk=product.pk).update(quantity=F("quantity") + delta, updated_at=now)
                before_p = running[product.id]
                running[product.id] = qty3(before_p + delta)

                line = StockAdjustmentLine.objects.create(
                    adjustment=adj,
                    product=product,
                    variant=variant,
                    delta=delta,
                    before=target_before,
                    after=after,
                    purchase_price=ln.get("purchase_price"),
                )
                out_lines.append(line)

        cv = catalog_version(_filter_products_company_only(self, Product.objects.all()))
        return Response(_movement_payload(adj, out_lines, cv), status=status.HTTP_201_CREATED)

    @staticmethod
    def _create_adjustment(company, branch, data, key, request) -> StockAdjustment:
        for _attempt in range(5):
            seq = (StockAdjustment.objects.filter(company=company).aggregate(m=Max("seq"))["m"] or 0) + 1
            try:
                with transaction.atomic():
                    return StockAdjustment.objects.create(
                        company=company,
                        branch=branch,
                        seq=seq,
                        kind=data["kind"],
                        reason=data.get("reason") or "",
                        source=data.get("source") or StockAdjustment.Source.KASSA,
                        idempotency_key=key,
                        created_by=request.user,
                        device_id=data.get("device_id") or "",
                    )
            except IntegrityError:
                if key and StockAdjustment.objects.filter(company=company, idempotency_key=key).exists():
                    raise ApiError("idempotency_in_progress", "Документ с этим ключом уже создаётся.",
                                   status_code=status.HTTP_409_CONFLICT)
                continue
        raise ApiError("conflict", "Не удалось выдать номер документа, повторите.", status_code=409)

    def get(self, request, *args, **kwargs):
        company = _company(request)
        qs = StockAdjustment.objects.filter(company=company).prefetch_related("lines")
        qp = request.query_params
        kind = (qp.get("kind") or "").strip()
        if kind:
            qs = qs.filter(kind=kind)
        pid = (qp.get("product_id") or "").strip()
        if pid:
            qs = qs.filter(lines__product_id=pid).distinct()
        dt_from = _parse_since(qp.get("from"), "from")
        dt_to = _parse_since(qp.get("to"), "to")
        if dt_from:
            qs = qs.filter(created_at__gte=dt_from)
        if dt_to:
            qs = qs.filter(created_at__lte=dt_to)
        try:
            limit = max(1, min(int(qp.get("limit") or 100), 500))
        except ValueError:
            limit = 100
        rows = list(qs.order_by("-created_at", "-seq")[:limit])
        return Response({
            "count": qs.count(),
            "results": [_movement_payload(a, list(a.lines.all())) for a in rows],
        })


# ----------------------------------------------------------------------
# Только остатки (§2.2)
# ----------------------------------------------------------------------

class ProductsStockAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/main/products/stock/?updated_since=<ISO | мс>
    → {"server_time", "items": [{"id", "q", "v": [{"id", "q"}]}], "deleted": [...]}
    Около 60 байт на товар вместо 1–2 КБ полной карточки. Без параметра — все остатки.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        server_time = timezone.now()
        qp = request.query_params
        since = _parse_since(qp.get("updated_since") or qp.get("updated_after"), "updated_since")
        qs = _filter_products_company_only(self, Product.objects.all())
        if since is not None:
            qs = qs.filter(updated_at__gte=since)
        rows = list(qs.values_list("id", "quantity").order_by("id"))
        ids = [r[0] for r in rows]
        variants = {}
        if ids:
            for vid, pid, q in ProductVariant.objects.filter(product_id__in=ids, is_active=True).values_list("id", "product_id", "quantity"):
                variants.setdefault(pid, []).append({"id": str(vid), "q": _d3(q)})
        items = [{"id": str(pid), "q": _d3(q), **({"v": variants[pid]} if pid in variants else {})} for pid, q in rows]
        deleted = []
        if since is not None:
            company = self._company()
            deleted = [
                str(x) for x in ProductDeletion.objects.filter(company=company, deleted_at__gte=since)
                .values_list("product_id", flat=True).distinct()
            ]
        resp = Response({
            "server_time": server_time.isoformat(timespec="milliseconds"),
            "count": len(items),
            "items": items,
            "deleted": deleted,
        })
        resp["Cache-Control"] = "private, no-cache"
        return resp


# ----------------------------------------------------------------------
# Долги клиента одним запросом (§7)
# ----------------------------------------------------------------------

class ClientDebtsAPIView(ClientDirectDebtAPIView):
    """
    GET  /api/main/clients/{id}/debts/ → {"total", "items": [...], "history": [...]}
    POST — как раньше (прямой долг без продажи, AN-03).
    """

    def get(self, request, client_id, *args, **kwargs):
        company = _company(request)
        client = get_object_or_404(Client.objects.filter(company=company), id=client_id)

        open_rows = _open_debts([company.id], Client.objects.filter(pk=client.pk).values("id"))
        sale_ids = [sid for _c, sid, _r, _d in open_rows if sid]

        deals = {
            d.sale_id: d
            for d in ClientDeal.objects.filter(company=company, client=client, kind=ClientDeal.Kind.DEBT, sale_id__in=sale_ids)
        }
        # Долги без продажи (прямые) — тоже сделки, но sale_id = None
        direct_deals = list(
            ClientDeal.objects.filter(company=company, client=client, kind=ClientDeal.Kind.DEBT, sale__isnull=True)
        )
        sales = {s.id: s for s in Sale.objects.filter(company=company, id__in=sale_ids).only("id", "doc_number", "created_at", "total", "cash_received")}
        first_items = {}
        for it in SaleItem.objects.filter(sale_id__in=sale_ids).order_by("sale_id", "id").only("sale_id", "name_snapshot", "product_id").select_related("product"):
            first_items.setdefault(it.sale_id, (it.name_snapshot or "").strip() or getattr(it.product, "name", None))

        inst_rows = DealInstallment.objects.filter(
            deal__in=[d.id for d in list(deals.values()) + direct_deals]
        ).values("deal_id", "due_date", "amount", "paid_amount", "paid_on")
        inst_by_deal = {}
        for r in inst_rows:
            inst_by_deal.setdefault(r["deal_id"], []).append(r)

        def _inst_info(deal_id):
            rows = inst_by_deal.get(deal_id, [])
            unpaid = [r for r in rows if (r["paid_amount"] or ZERO) < (r["amount"] or ZERO)]
            next_due = min((r["due_date"] for r in unpaid), default=None)
            return len(unpaid), next_due.isoformat() if next_due else None

        items = []
        total = ZERO
        seen_direct = set()
        for _cid, sale_id, remaining, created_at in open_rows:
            total += remaining
            deal = deals.get(sale_id) if sale_id else None
            sale = sales.get(sale_id) if sale_id else None
            if sale_id is None:
                # прямой долг: найти сделку по сумме/дате среди direct_deals (одна строка = одна сделка)
                deal = next((d for d in direct_deals if d.id not in seen_direct and money(d.remaining_debt) == remaining), None)
                if deal:
                    seen_direct.add(deal.id)
            left, next_due = _inst_info(deal.id) if deal else (0, None)
            original = money(deal.amount) if deal else money(getattr(sale, "total", ZERO) or ZERO)
            prepayment = money(deal.prepayment) if deal else money(getattr(sale, "cash_received", ZERO) or ZERO)
            items.append({
                "sale_id": str(sale_id) if sale_id else None,
                "deal_id": str(deal.id) if deal else None,
                "number": getattr(sale, "doc_number", None),
                "created_at": created_at.isoformat() if created_at else None,
                "first_item_name": first_items.get(sale_id) if sale_id else (deal.title if deal else None),
                "original": str(original),
                "prepayment": str(prepayment),
                "paid": str(money(original - prepayment - remaining)),
                "remaining": str(money(remaining)),
                "installments_left": left,
                "next_due": next_due,
            })
        items.sort(key=lambda x: x["created_at"] or "")

        since = timezone.now() - timedelta(days=90)
        history = []
        closed = (
            ClientDeal.objects.filter(company=company, client=client, kind=ClientDeal.Kind.DEBT, updated_at__gte=since)
            .select_related("sale")
            .order_by("-updated_at")[:200]
        )
        for d in closed:
            if money(d.remaining_debt) > 0 and not (d.sale_id and d.sale and d.sale.status == Sale.Status.PAID):
                continue
            history.append({
                "sale_id": str(d.sale_id) if d.sale_id else None,
                "deal_id": str(d.id),
                "number": getattr(d.sale, "doc_number", None) if d.sale_id else None,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "closed_at": d.updated_at.isoformat() if d.updated_at else None,
                "amount": str(money(d.amount)),
            })

        return Response({
            "client_id": str(client.id),
            "client_name": client.full_name,
            "total": str(money(total)),
            "items": items,
            "history": history,
        })


# ----------------------------------------------------------------------
# checkout: 409 при том же ключе и другом теле (§8.4); клиент с ключом (§12)
# ----------------------------------------------------------------------

class PosQuickCheckoutGuardedAPIView(PosQuickCheckoutAPIView):
    """
    POST /api/main/pos/checkout/ — то же, что раньше, плюс общий декоратор BE2-11:
    тот же Idempotency-Key с другим составом → 409 idempotency_conflict (ТЗ-28 §8.4),
    повтор с тем же составом → тот же ответ и "replayed": true.
    """

    @idempotent(body_key=True)
    def _guarded_post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        response = self._guarded_post(request, *args, **kwargs)
        if response.status_code == status.HTTP_409_CONFLICT and isinstance(getattr(response, "data", None), dict) \
                and response.data.get("code") in ("idempotency_conflict", "in_progress"):
            # Касса повторяет чек с тем же ключом, но чуть другим телом; раньше получала ту же продажу.
            # Отдаём по-старому: базовый checkout находит продажу по Sale.idempotency_key.
            response = super().post(request, *args, **kwargs)
        if isinstance(getattr(response, "data", None), dict) and isinstance(response.data.get("items"), list):
            annotate_items(response.data.get("id"), response.data["items"])
        if response.get("Idempotent-Replayed") == "true":
            # Как и раньше у pos/checkout/: повтор — 200 и "replayed": true (касса на это смотрит).
            response.status_code = status.HTTP_200_OK
            if isinstance(getattr(response, "data", None), dict):
                response.data["replayed"] = True
        elif response.status_code == status.HTTP_201_CREATED and isinstance(getattr(response, "data", None), dict):
            self._enrich(request, response.data)
        return response

    def _add_items(self, cart, items, limit_discounts, max_dp):
        # Упаковки (sale_package_id) переводим в базовые единицы, остальное — как раньше
        items, adjustments, self._pack_info = convert_pack_items(cart.company_id, items)
        super()._add_items(cart, items, limit_discounts, max_dp)
        if adjustments:
            from apps.main.models import CartItem

            for (pid, vid), adj in adjustments.items():
                CartItem.objects.filter(cart=cart, product_id=pid, variant_id=vid).update(
                    line_discount=F("line_discount") + adj
                )
            cart.recalc()

    def after_sale_created(self, request, sale, client, data) -> None:
        save_pack_lines(sale, getattr(self, "_pack_info", []))
        # Удалённые строки корзины веб-кассы → CartItemDeletionLog
        write_deletions(company=sale.company, branch=sale.branch, user=request.user, items=data.get("deleted_items") or [])
        # График долга: создаётся вместе со сделкой, а не вторым PATCH
        apply_debt_schedule(sale, data)
        # Лимиты долга: клиент и кассир (внутри транзакции продажи, под блокировкой)
        check_debt_limits(request, sale, client, data)
        # Общий лимит компании (Б-14): офлайн-досылка не отклоняется — уходит в warnings
        self._warnings.extend(check_company_debt_limits(request, sale, client, offline=self._offline))

    def _enrich(self, request, data: dict) -> None:
        """ТЗ-28 §8.5: итоги смены, новые остатки и версия каталога прямо в ответе продажи."""
        try:
            sale = Sale.objects.select_related("shift", "shift__cashier", "shift__cashbox").filter(pk=data.get("id")).first()
            if sale is None:
                return
            if sale.shift_id and "shift_totals" not in data:
                data["shift_totals"] = shift_payload(sale.shift)
            product_ids = [it.get("product") for it in data.get("items") or [] if it.get("product")]
            if product_ids:
                data["stock"] = [
                    {"product_id": str(pid), "quantity": _d3(q)}
                    for pid, q in Product.objects.filter(id__in=product_ids).values_list("id", "quantity")
                ]
                variant_ids = [it.get("variant") for it in data.get("items") or [] if it.get("variant")]
                if variant_ids:
                    data["stock_variants"] = [
                        {"variant_id": str(vid), "quantity": _d3(q)}
                        for vid, q in ProductVariant.objects.filter(id__in=variant_ids).values_list("id", "quantity")
                    ]
            from apps.main.tz26.hw_prices.services import enrich_sale_response

            enrich_sale_response(sale, data)  # ТЗ 26.10 Б-55, Б-56: object, price_source/base_price строк
            if sale.debt_initial and sale.debt_initial > 0:
                data.update(schedule_payload(sale))
            company = _company(request)
            data["catalog_version"] = catalog_version(Product.objects.filter(company=company).exclude(status=Product.Status.ARCHIVED))["version"]
            data["server_time"] = timezone.now().isoformat(timespec="milliseconds")
        except Exception:  # noqa: BLE001 — дополнение не должно ломать продажу
            return


class ProductByBarcodeWithVariantsAPIView(ProductByBarcodeAPIView):
    """
    GET /api/main/products/barcode/{code}/ — как раньше, плюс штрихкоды вариантов (ТЗ-28 §2.6):
    если код не найден у товара и доп. штрихкодов, ищем ProductVariant.barcode и отдаём
    товар с полем "variant" и "matched_by": "variant_barcode".
    """

    def get(self, request, *args, **kwargs):
        try:
            return super().get(request, *args, **kwargs)
        except (Http404, NotFound):
            code = (self.kwargs.get("barcode") or "").replace(" ", "").strip()
            if not code:
                raise
            variant = (
                ProductVariant.objects.select_related("product")
                .filter(company=_company(request), barcode=code, is_active=True)
                .exclude(product__status=Product.Status.ARCHIVED)
                .first()
            )
            if variant is None:
                raise
            data = self.get_serializer(variant.product).data
            data["variant"] = ProductVariantSerializer(variant).data
            data["matched_by"] = "variant_barcode"
            return Response(data)


class ClientDetailWithDebtLimitAPIView(ClientRetrieveUpdateDestroyAPIView):
    """
    GET/PATCH /api/main/clients/{id}/ — как раньше, плюс `debt_limit` и `debt_available`.
    PATCH {"debt_limit": 5000} / {"debt_limit": null} (без лимита) — только владелец/администратор.
    """

    def _with_limit(self, response):
        if response.status_code == 200 and isinstance(getattr(response, "data", None), dict):
            response.data.update(client_debt_payload(self.get_object()))
        return response

    def retrieve(self, request, *args, **kwargs):
        return self._with_limit(super().retrieve(request, *args, **kwargs))

    def update(self, request, *args, **kwargs):
        has_limit = hasattr(request.data, "get") and "debt_limit" in request.data
        new_limit = None
        if has_limit:
            if not _is_owner_like(request.user):
                raise ApiError("forbidden", "Менять лимит долга может владелец или администратор.", status_code=403)
            raw = request.data.get("debt_limit")
            if raw in (None, ""):
                new_limit = None
            else:
                try:
                    new_limit = money(Decimal(str(raw)))
                except (InvalidOperation, ValueError):
                    raise ApiError("validation_error", "Лимит долга — число.", fields={"debt_limit": ["Ожидается число."]})
                if new_limit < 0:
                    raise ApiError("validation_error", "Лимит долга не может быть отрицательным.",
                                   fields={"debt_limit": ["Не меньше нуля."]})
            data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
            data.pop("debt_limit", None)
            request._full_data = data
        with transaction.atomic():
            response = super().update(request, *args, **kwargs)
            if has_limit and response.status_code == 200:
                client = self.get_object()
                ClientDebtLimit.objects.update_or_create(
                    client=client,
                    defaults={"company_id": client.company_id, "limit": new_limit, "updated_by": request.user},
                )
        return self._with_limit(response)


# ---- Общий лимит долга компании (ТЗ 26.10, Б-14 п. 3) ----

def _limits_payload(lim) -> dict:
    return {
        "max_sales_per_day": lim.max_sales_per_day if lim else None,
        "max_amount_per_day": str(money(lim.max_amount_per_day)) if lim and lim.max_amount_per_day is not None else None,
        "max_debt_per_client": str(money(lim.max_debt_per_client)) if lim and lim.max_debt_per_client is not None else None,
        "applies_to": lim.applies_to if lim else "all",
    }


class DebtLimitsAPIView(APIView):
    """GET — любой сотрудник (кассе нужно знать лимит); PATCH — только владелец и администратор."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .debt_limits import company_limits

        return Response(_limits_payload(company_limits(_company(request).pk)))

    def patch(self, request):
        from .models import CompanyDebtLimits

        if not _is_owner_like(request.user):
            raise ApiError("permission_denied", "Нет доступа: лимиты долга меняет владелец.", status_code=403)
        company = _company(request)
        data = request.data
        fields, errors = {}, {}
        if "max_sales_per_day" in data:
            v = data["max_sales_per_day"]
            if v in (None, "", 0, "0"):
                fields["max_sales_per_day"] = None
            else:
                try:
                    n = int(v)
                    if n < 0:
                        raise ValueError
                    fields["max_sales_per_day"] = n
                except (TypeError, ValueError):
                    errors["max_sales_per_day"] = ["Нужно целое число не меньше 0."]
        for name in ("max_amount_per_day", "max_debt_per_client"):
            if name in data:
                v = data[name]
                if v in (None, ""):
                    fields[name] = None
                    continue
                try:
                    d = money(Decimal(str(v)))
                    if d < 0:
                        raise InvalidOperation
                    fields[name] = d if d > 0 else None
                except (InvalidOperation, ValueError):
                    errors[name] = ["Нужна сумма не меньше 0."]
        if "applies_to" in data:
            if data["applies_to"] not in ("all", "cashiers"):
                errors["applies_to"] = ["Допустимо: all, cashiers."]
            else:
                fields["applies_to"] = data["applies_to"]
        if errors:
            raise ApiError("validation_error", "Неверные значения лимитов.", fields=errors)
        lim, _ = CompanyDebtLimits.objects.get_or_create(company=company)
        for k, v in fields.items():
            setattr(lim, k, v)
        lim.updated_by = request.user
        lim.save()
        return Response(_limits_payload(lim))


class DebtLimitsUsageAPIView(APIView):
    """GET debt-limits/usage/?date=YYYY-MM-DD — сколько долговых чеков и долга выдано за день."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .debt_limits import company_limits, day_usage

        company = _company(request)
        day = None
        raw = request.query_params.get("date")
        if raw:
            try:
                day = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                raise ApiError("invalid_param", "date: формат ГГГГ-ММ-ДД.", data={"param": "date"})
        lim = company_limits(company.pk)
        cashiers_only = bool(lim and lim.applies_to == "cashiers")
        count, total = day_usage(company.pk, day, cashiers_only=cashiers_only)
        return Response({
            "date": str(day) if day else None,
            "sales_count": count,
            "amount": str(total),
            "limits": _limits_payload(lim),
            "sales_left": max(0, lim.max_sales_per_day - count) if lim and lim.max_sales_per_day else None,
            "amount_left": str(max(Decimal("0.00"), money(lim.max_amount_per_day - total)))
            if lim and lim.max_amount_per_day is not None else None,
        })
