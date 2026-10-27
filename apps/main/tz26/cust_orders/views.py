"""Б-60: api/main/customer-orders/ — заказ покупателя."""
import functools

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent as _idempotent
from apps.main.models import Client
from apps.main.views import CompanyBranchRestrictedMixin
from apps.users.permissions_flags import PosAccess, has_any_flag, is_privileged
from core.exceptions import ApiError

from . import services as svc
from .models import CustomerOrder
from .serializers import OrderInSerializer, order_payload

MAX_PAGE_SIZE = 500


def idempotent(body_key=True, replay_flag=True):
    """Ключ повтора (О3): повтор успешной операции отдаёт 200 и replayed:true, даже если первый ответ был 201."""
    inner = _idempotent(body_key=body_key, replay_flag=replay_flag)

    def deco(handler):
        wrapped = inner(handler)

        @functools.wraps(handler)
        def run(view, request, *a, **kw):
            resp = wrapped(view, request, *a, **kw)
            if resp.get("Idempotent-Replayed") and resp.status_code == 201:
                resp.status_code = 200
            return resp
        return run
    return deco


def _page_params(request):
    qp = request.query_params
    try:
        page = max(1, int(qp.get("page") or 1))
        size = int(qp.get("page_size") or qp.get("limit") or 50)
    except ValueError:
        raise ApiError("invalid", "page и page_size — целые числа.")
    return page, max(1, min(size, MAX_PAGE_SIZE))


class OrderBase(CompanyBranchRestrictedMixin):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def _orders(self):
        return self._filter_qs_company_branch(
            CustomerOrder.objects.select_related("client", "created_by", "company")
            .prefetch_related("lines__product", "lines__sale_unit", "lines__supply_links", "payments")
        )

    def _get(self, pk, lock=False):
        qs = self._orders()
        if lock:
            qs = qs.select_for_update(of=("self",))
        obj = qs.filter(pk=pk).first()
        if obj is None:
            raise ApiError("not_found", "Заказ покупателя не найден.", status_code=404)
        return obj

    def _fresh(self, pk):
        return order_payload(self._get(pk))

    def _check_refs(self, d):
        company = self._company()
        cl = d.get("client")
        if cl is not None and cl.company_id != company.id:
            raise ApiError("client_not_found", "Клиент не найден.", fields={"client": ["Клиент не найден."]})
        ob = d.get("object")
        if ob is not None and (ob.client.company_id != company.id or (cl is not None and ob.client_id != cl.id)):
            raise ApiError("object_not_found", "Объект не найден.", fields={"object": ["Объект не найден."]})
        for i, it in enumerate(d.get("items") or []):
            if it["product"].company_id != company.id:
                raise ApiError("product_not_found", "Товар не найден.", fields={f"items[{i}].product": ["Товар не найден."]})


class OrderListCreateAPIView(OrderBase, APIView):
    """
    GET  /api/main/customer-orders/?status=&client=&source=&search=&date_from=&date_to=&page=&page_size=
    POST /api/main/customer-orders/  (Idempotency-Key)
    """

    def get(self, request):
        qs = self._orders()
        qp = request.query_params
        st = (qp.get("status") or "").strip()
        if st:
            bad = [s for s in st.split(",") if s not in CustomerOrder.Status.values]
            if bad:
                raise ApiError("invalid_param", "status: " + "|".join(CustomerOrder.Status.values), data={"field": "status"})
            qs = qs.filter(status__in=st.split(","))
        if qp.get("client"):
            qs = qs.filter(client_id=qp["client"])
        if qp.get("source"):
            qs = qs.filter(source=qp["source"])
        for key, lookup in (("date_from", "created_at__date__gte"), ("date_to", "created_at__date__lte")):
            raw = (qp.get(key) or "").strip()
            if raw:
                d = parse_date(raw)
                if d is None:
                    raise ApiError("invalid_param", f"{key}: YYYY-MM-DD.", data={"field": key})
                qs = qs.filter(**{lookup: d})
        search = (qp.get("search") or "").strip()
        if search:
            q = (Q(customer_name__icontains=search) | Q(customer_phone__icontains=search)
                 | Q(client__full_name__icontains=search) | Q(note__icontains=search))
            if search.lstrip("#").isdigit():
                q |= Q(number=int(search.lstrip("#")))
            qs = qs.filter(q)
        qs = qs.order_by("-created_at", "-number", "id")
        page, size = _page_params(request)
        count = qs.count()
        rows = list(qs[(page - 1) * size: page * size])
        base = request.build_absolute_uri(request.path)
        return Response({
            "count": count,
            "next": f"{base}?page={page + 1}&page_size={size}" if page * size < count else None,
            "previous": f"{base}?page={page - 1}&page_size={size}" if page > 1 else None,
            "results": [order_payload(o) for o in rows],
        })

    @idempotent()
    def post(self, request):
        ser = OrderInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        if d.get("status") not in (None, "draft", "confirmed"):
            raise ApiError("status_transition_invalid", "Новый заказ можно создать только как draft или confirmed.")
        self._check_refs(d)
        company, branch = self._company(), self._auto_branch()
        with transaction.atomic():
            order = svc.create_order(company=company, branch=branch, user=request.user, data=d, items=d.get("items") or [])
            pre = d.get("prepayment")
            if pre:
                svc.add_prepayment(order, request.user, pre["amount"], pre.get("method"), pre.get("shift"))
        return Response(self._fresh(order.pk), status=status.HTTP_201_CREATED)


class OrderDetailAPIView(OrderBase, APIView):
    def get(self, request, pk):
        return Response(order_payload(self._get(pk)))

    def patch(self, request, pk):
        ser = OrderInSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        if "prepayment" in d:
            raise ApiError("prepayment_via_action", "Предоплата вносится через POST customer-orders/{id}/prepay/.")
        self._check_refs(d)
        with transaction.atomic():
            order = self._get(pk, lock=True)
            if_match = (request.headers.get("If-Match") or "").strip().strip('"')
            if if_match and if_match != str(order.version):
                raise ApiError("version_conflict", "Заказ изменили в другом месте. Обновите и повторите.",
                               status_code=409, data={"version": order.version})
            if order.status in svc.INACTIVE and not (order.status == "delivered" and d.get("status") == "closed" and len(d) == 1):
                raise ApiError("order_closed", "Заказ закрыт: менять нельзя.", status_code=409)
            new_status = d.get("status")
            if new_status and new_status != order.status:
                allowed = {("draft", "confirmed"), ("confirmed", "draft"), ("delivered", "closed")}
                if (order.status, new_status) not in allowed:
                    hint = " Отмена — POST …/cancel/." if new_status == "canceled" else ""
                    raise ApiError("status_transition_invalid",
                                   f"Переход {order.status} → {new_status} недоступен.{hint}", status_code=400)
                order.status = new_status
                if new_status == "closed":
                    order.lines.update(status="delivered")
            if "items" in d:
                if order.status not in ("draft", "confirmed") or order.lines.filter(reserved_qty__gt=0).exists():
                    raise ApiError("order_reserved", "Строки нельзя менять: заказ в резерве. Отмените его или создайте новый.",
                                   status_code=409)
                svc.replace_lines(order, d["items"])
            if "client" in d:
                order.client = d["client"]
            if "object" in d:
                order.client_object = d["object"]
            for f in ("customer_name", "customer_phone", "due_date", "note", "needs_quote", "source"):
                if f in d:
                    setattr(order, f, d[f])
            if "delivery" in d:
                svc.apply_delivery(order, d["delivery"])
            order.version += 1
            order.save()
        return Response(self._fresh(pk))


class OrderReserveAPIView(OrderBase, APIView):
    """POST …/reserve/ {create_supplier_order?: bool, supplier?: id} — резерв того, что есть."""

    @idempotent()
    def post(self, request, pk):
        want_po = bool(request.data.get("create_supplier_order"))
        supplier = None
        if request.data.get("supplier"):
            supplier = self._filter_qs_company_branch(Client.objects.all()).filter(pk=request.data["supplier"]).first()
            if supplier is None:
                raise ApiError("supplier_not_found", "Поставщик не найден.", status_code=400)
        with transaction.atomic():
            order = self._get(pk, lock=True)
            order, po = svc.reserve_order(order, request.user, create_supplier_order=want_po, supplier=supplier)
        body = self._fresh(pk)
        body["supplier_order"] = str(po.pk) if po else None
        return Response(body)


class OrderPrepayAPIView(OrderBase, APIView):
    """POST …/prepay/ {amount, method, shift?} — предоплата: движение в смене, не выручка."""

    @idempotent()
    def post(self, request, pk):
        from .serializers import PrepaymentInSerializer

        ser = PrepaymentInSerializer(data=request.data.get("prepayment") or request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        with transaction.atomic():
            order = self._get(pk, lock=True)
            svc.add_prepayment(order, request.user, d["amount"], d.get("method"), d.get("shift"))
        return Response(self._fresh(pk), status=status.HTTP_201_CREATED)


class OrderDeliverAPIView(OrderBase, APIView):
    """POST …/deliver/ {payment:{method, received}} — выдача: продажа с зачётом предоплаты."""

    @idempotent()
    def post(self, request, pk):
        replay = False
        resp = None
        with transaction.atomic():
            order = self._get(pk, lock=True)
            resp = svc.deliver_order(order, request, request.data.get("payment"))
            replay = resp is None
        if resp is not None and resp.status_code >= 400:
            return resp
        body = self._fresh(pk)
        if replay:
            body["replayed"] = True
            return Response(body)
        body["change"] = resp.data.get("change")
        body["sale_id"] = str(resp.data.get("sale_id"))
        return Response(body, status=status.HTTP_201_CREATED)


class OrderCancelAPIView(OrderBase, APIView):
    """POST …/cancel/ — отмена, предоплата возвращается из кассы текущей смены."""

    @idempotent()
    def post(self, request, pk):
        user = request.user
        may_refund = is_privileged(user) or has_any_flag(user, ["can_view_cashier"])
        with transaction.atomic():
            order = self._get(pk, lock=True)
            order, replay = svc.cancel_order(order, user, may_refund=may_refund)
        body = self._fresh(pk)
        if replay:
            body["replayed"] = True
        return Response(body)
