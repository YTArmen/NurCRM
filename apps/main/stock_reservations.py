"""19.11.2026 К-8 (k458): резервы товара — мягкие, остаток (quantity) не трогают.

  GET    reservations/?product=&source=&source_id=&status=active|released|consumed|expired|all
  POST   reservations/ {product_id, variant_id?, qty, source: site_order|held_receipt|manual, source_id?,
                        expires_at?, comment?, allow_over?}  + Idempotency-Key (повтор -> replayed: true)
         qty больше доступного -> 409 insufficient_available {available}; allow_over=true -> 201 + warnings
  DELETE reservations/<id>/ {status?: released|consumed}  — снять резерв (по умолчанию released)

В товаре (products/list, products/<id>/): reserved, available = quantity − активные резервы (истёкшие не считаются).
Быстрый чек кассы (pos/checkout/): продажа сверх available -> warnings[reserved_stock];
настройка settings/stock/ reserved_sale_policy = warn (по умолчанию) | block (409 reserved_stock).
Свои резервы чека не мешают: pos/checkout/ {reservation_ids: [...]} — они не считаются в «чужих».
Чек из очереди — всегда только предупреждение.
"""
import logging
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models_batches import StockReservation

log = logging.getLogger("nurcrm.stock_reservations")
SOURCES = ("site_order", "held_receipt", "manual")
POLICIES = ("warn", "block")
_Q3 = Decimal("0.001")


def _active_q(now=None):
    now = now or timezone.now()
    return Q(status="active") & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))


def reserved_map(company_id, product_ids=None, exclude_ids=None):
    """{product_id(str): Decimal} — активные резервы компании."""
    qs = StockReservation.objects.filter(_active_q(), company_id=company_id)
    if product_ids is not None:
        qs = qs.filter(product_id__in=list(product_ids))
    if exclude_ids:
        qs = qs.exclude(id__in=list(exclude_ids))
    out = {str(r["product_id"]): r["s"] for r in qs.values("product_id").annotate(s=Sum("qty"))}
    # co2011: удержание заказов покупателей (customer-orders) тоже уменьшает available
    try:
        from apps.main.tz26.cust_orders.models import CustomerOrder, CustomerOrderLine
        from apps.main.tz26.cust_orders.services import factor_of

        lq = (CustomerOrderLine.objects.filter(order__company_id=company_id, reserved_qty__gt=0, product__isnull=False,
                                               order__status__in=CustomerOrder.ACTIVE_STATUSES)
              .exclude(status__in=("canceled", "delivered")).select_related("sale_unit"))
        if product_ids is not None:
            lq = lq.filter(product_id__in=list(product_ids))
        for ln in lq:
            k = str(ln.product_id)
            out[k] = (out.get(k) or Decimal("0")) + (ln.reserved_qty * factor_of(ln)).quantize(_Q3)
    except Exception:  # noqa: BLE001
        log.exception("customer order holds failed")
    return out


def policy_of(company) -> str:
    try:
        from apps.main.stock_batches import _settings

        v = _settings(company).get("reserved_sale_policy") or "warn"
    except Exception:  # noqa: BLE001
        v = "warn"
    return v if v in POLICIES else "warn"


def _status(r, now):
    if r.status == "active" and r.expires_at and r.expires_at <= now:
        return "expired"
    return r.status


def _payload(r, now=None):
    now = now or timezone.now()
    return {"id": str(r.id), "product_id": str(r.product_id), "variant_id": str(r.variant_id) if r.variant_id else None,
            "qty": format(Decimal(r.qty).quantize(_Q3), "f"), "source": r.source, "source_id": r.source_id or None,
            "status": _status(r, now), "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "comment": r.comment, "created_by": str(r.created_by_id) if r.created_by_id else None,
            "created_at": r.created_at.isoformat(), "closed_at": r.closed_at.isoformat() if r.closed_at else None}


def _err(code, detail, status=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=status)


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _invalidate(company_id):
    try:
        from apps.main.cache_utils import invalidate_cache_pattern

        invalidate_cache_pattern(f"products:list:{company_id}:")
    except Exception:  # noqa: BLE001
        pass


def _emit(company_id, product_id, available, reserved):
    try:
        from apps.main.change_feed import _emit as emit

        emit(company_id, "stock_changed", branch_id=None, entity="product", entity_id=product_id,
             data={"product": str(product_id), "product_id": str(product_id), "reserved": format(reserved, "f"),
                   "available": format(available, "f"), "reason": "reservation"})
    except Exception:  # noqa: BLE001
        log.exception("reservation event failed")


class ReservationListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request.user)
        qp = request.query_params
        now = timezone.now()
        qs = StockReservation.objects.filter(company_id=company.id).order_by("-created_at")
        st = (qp.get("status") or "active").strip()
        if st == "active":
            qs = qs.filter(_active_q(now))
        elif st == "expired":
            qs = qs.filter(status="active", expires_at__lte=now)
        elif st in ("released", "consumed"):
            qs = qs.filter(status=st)
        for f in ("product", "source", "source_id"):
            if qp.get(f):
                try:
                    qs = qs.filter(**{("product_id" if f == "product" else f): qp[f]})
                except Exception:  # noqa: BLE001
                    return _err("validation_error", f"{f}: неверное значение.")
        try:
            page = max(1, int(qp.get("page") or 1))
            size = min(500, max(1, int(qp.get("page_size") or 100)))
        except ValueError:
            return _err("validation_error", "page, page_size — целые числа.")
        total = qs.count()
        return Response({"count": total, "page": page, "page_size": size,
                         "results": [_payload(r, now) for r in qs[(page - 1) * size: page * size]]})

    def post(self, request):
        from apps.integrations.idempotency import idempotent

        return idempotent(body_key=True, replay_flag=True)(ReservationListCreateAPIView._create)(self, request)

    def _create(self, request):
        from apps.main.models import Product, ProductVariant

        company = _company(request.user)
        data = request.data if hasattr(request.data, "get") else {}
        fields = {}
        pid = data.get("product_id") or data.get("product")
        product = None
        try:
            product = Product.objects.filter(pk=pid, company=company).only("id", "quantity", "name").first() if pid else None
        except Exception:  # noqa: BLE001
            product = None
        if product is None:
            fields["product_id"] = ["Товар не найден."]
        vid = data.get("variant_id") or data.get("variant")
        if vid and product is not None:
            try:
                if not ProductVariant.objects.filter(pk=vid, product_id=product.id).exists():
                    fields["variant_id"] = ["Вариант не найден."]
            except Exception:  # noqa: BLE001
                fields["variant_id"] = ["Вариант не найден."]
        try:
            qty = Decimal(str(data.get("qty")).replace(",", ".")).quantize(_Q3)
            if qty <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError):
            qty = None
            fields["qty"] = ["Количество больше нуля."]
        source = str(data.get("source") or "manual").strip()
        if source not in SOURCES:
            fields["source"] = ["site_order, held_receipt или manual."]
        exp = None
        if data.get("expires_at"):
            exp = parse_datetime(str(data["expires_at"]))
            if exp is None:
                fields["expires_at"] = ["ISO-8601 с часовым поясом."]
            elif timezone.is_naive(exp):
                exp = timezone.make_aware(exp)
        if fields:
            return _err("validation_error", "Проверьте резерв.", fields=fields)
        with transaction.atomic():
            Product.objects.select_for_update().filter(pk=product.id).first()  # резервы одного товара — по очереди
            product.refresh_from_db(fields=["quantity"])
            reserved = reserved_map(company.id, [product.id]).get(str(product.id), Decimal("0"))
            available = Decimal(str(product.quantity or 0)) - reserved
            warnings = []
            if qty > available:
                if str(data.get("allow_over")).lower() not in ("1", "true", "yes"):
                    return _err("insufficient_available", f"Доступно {available.quantize(_Q3)}, резерв {qty}.", 409,
                                available=format(available.quantize(_Q3), "f"), reserved=format(reserved, "f"),
                                quantity=format(Decimal(str(product.quantity or 0)), "f"))
                warnings.append({"code": "insufficient_available", "available": format(available.quantize(_Q3), "f")})
            r = StockReservation.objects.create(
                id=uuid.uuid4(), created_at=timezone.now(), company_id=company.id, product_id=product.id, variant_id=vid or None, qty=qty, source=source,
                source_id=str(data.get("source_id") or "")[:64], expires_at=exp,
                comment=str(data.get("comment") or "")[:255], created_by_id=request.user.pk)
        _invalidate(company.id)
        _emit(company.id, product.id, available - qty, reserved + qty)
        return Response({**_payload(r), "product_reserved": format(reserved + qty, "f"),
                         "product_available": format(available - qty, "f"), "warnings": warnings}, status=201)


class ReservationDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        company = _company(request.user)
        r = StockReservation.objects.filter(pk=pk, company_id=company.id).first()
        return Response(_payload(r)) if r else _err("not_found", "Резерв не найден.", 404)

    def delete(self, request, pk):
        company = _company(request.user)
        data = request.data if hasattr(request.data, "get") else {}
        st = str(data.get("status") or request.query_params.get("status") or "released").strip()
        if st not in ("released", "consumed"):
            return _err("validation_error", "status: released или consumed.")
        with transaction.atomic():
            r = StockReservation.objects.select_for_update().filter(pk=pk, company_id=company.id).first()
            if r is None:
                return _err("not_found", "Резерв не найден.", 404)
            if r.status != "active":
                return Response({**_payload(r), "replayed": True})
            r.status, r.closed_at = st, timezone.now()
            r.save(update_fields=["status", "closed_at"])
        _invalidate(company.id)
        try:
            from apps.main.models import Product

            q = Decimal(str(Product.objects.filter(pk=r.product_id).values_list("quantity", flat=True).first() or 0))
            res = reserved_map(company.id, [r.product_id]).get(str(r.product_id), Decimal("0"))
            _emit(company.id, r.product_id, q - res, res)
        except Exception:  # noqa: BLE001
            pass
        return Response(_payload(r))


# ------------------------------------------------------------------ товар: reserved / available

def wrap_product_serializer(cls):
    orig = cls.to_representation

    def to_representation(self, obj):
        data = orig(self, obj)
        try:
            if not isinstance(data, dict) or not getattr(obj, "company_id", None):
                return data
            ctx = self.context if isinstance(getattr(self, "context", None), dict) else {}
            key = ("_k8", obj.company_id)
            m = ctx.get(key)
            if m is None:
                m = reserved_map(obj.company_id)  # активных резервов у компании мало — одним запросом
                if isinstance(ctx, dict):
                    ctx[key] = m
            res = m.get(str(obj.pk), Decimal("0"))
            q = Decimal(str(getattr(obj, "quantity", None) or 0))
            data["reserved"] = format(Decimal(res).quantize(_Q3), "f")
            data["available"] = format((q - res).quantize(_Q3), "f")
        except Exception:  # noqa: BLE001
            log.exception("reserved field failed")
        return data

    cls.to_representation = to_representation


# ------------------------------------------------------------------ касса

def check_reserved(view, company, items, reservation_ids=None):
    """Быстрый чек: продажа сверх available (с учётом чужих резервов)."""
    try:
        demand = defaultdict(Decimal)
        idx = {}
        for i, it in enumerate(items or []):
            pid = it.get("product") if isinstance(it, dict) else None
            if pid:
                demand[str(pid)] += Decimal(str(it.get("qty") or 0))
                idx.setdefault(str(pid), i)
        if not demand:
            return
        res = reserved_map(company.id, list(demand), exclude_ids=[str(x) for x in (reservation_ids or [])])
        if not res:
            return
        from apps.main.models import Product

        qty = {str(p): Decimal(str(q or 0)) for p, q in Product.objects.filter(pk__in=list(res)).values_list("id", "quantity")}
        rows = []
        for pid, r in res.items():
            avail = qty.get(pid, Decimal("0")) - r
            if demand[pid] > avail:
                rows.append({"product_id": pid, "index": idx.get(pid), "requested": format(demand[pid], "f"),
                             "available": format(avail.quantize(_Q3), "f"), "reserved": format(r, "f")})
    except Exception:  # noqa: BLE001
        log.exception("check_reserved failed")
        return
    if not rows:
        return
    if policy_of(company) == "block" and not getattr(view, "_force_minus_queue", False):
        from core.exceptions import ApiError

        raise ApiError("reserved_stock", "Товар зарезервирован — продажа сверх доступного запрещена настройкой.",
                       status_code=409, data={"lines": rows})
    for r in rows:
        view._warnings.append({"code": "reserved_stock", **r})
