"""Б-71: отложенный чек на сервере с передачей между устройствами и кассирами."""
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from apps.main.tz26.pos_print.common import (
    branch_ok, clean_device_key, company_of, iso, paginate, require_idem_key,
)
from apps.main.tz26.pos_print.models import HELD_CART_TTL, HeldCart
from apps.main.tz26.pos_print.views import not_found
from apps.users.permissions_flags import PosAccess
from apps.utils import _is_owner_like
from core.exceptions import ApiError

MAX_ITEMS = 1000


def _user_ref(u):
    if not u:
        return None
    return {"id": str(u.pk), "name": (u.get_full_name() or u.email or "")}


def serialize_held(h):
    return {
        "id": str(h.pk), "number": h.number, "label": h.label, "status": h.status,
        "cashbox": str(h.cashbox_id), "branch": str(h.branch_id) if h.branch_id else None,
        "items": h.items, "order_discount": str(h.order_discount) if h.order_discount is not None else None,
        "client": str(h.client_id) if h.client_id else None,
        "from_device_key": h.from_device_key or None,
        "created_by": _user_ref(h.created_by) if h.created_by_id else None,
        "created_at": iso(h.created_at), "expires_at": iso(h.expires_at),
        "taken_at": iso(h.taken_at), "taken_by": _user_ref(h.taken_by) if h.taken_by_id else None,
        "taken_by_device": h.taken_by_device or None,
        "sale": str(h.sale_id) if h.sale_id else None, "closed_at": iso(h.closed_at),
    }


def _num(v, field, idx, *, positive=False):
    try:
        d = Decimal(str(v).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError("validation_error", f"items[{idx}].{field}: не число.",
                       fields={f"items[{idx}].{field}": ["Не число."]})
    if positive and d <= 0:
        raise ApiError("validation_error", f"items[{idx}].{field}: должно быть больше 0.",
                       fields={f"items[{idx}].{field}": ["Больше 0."]})
    return d


def _clean_items(raw):
    """Только форма: цены и остатки сервер не проверяет и не пересчитывает (касса проверит сама)."""
    if not isinstance(raw, list) or not raw:
        raise ApiError("validation_error", "items: непустой список.", fields={"items": ["Непустой список."]})
    if len(raw) > MAX_ITEMS:
        raise ApiError("validation_error", f"Не больше {MAX_ITEMS} позиций.", fields={"items": ["Слишком много."]})
    out = []
    for i, it in enumerate(raw):
        if not isinstance(it, dict):
            raise ApiError("validation_error", f"items[{i}]: объект.", fields={f"items[{i}]": ["Ожидался объект."]})
        if not it.get("product") and not (it.get("custom") and str(it.get("name") or "").strip()):
            raise ApiError("validation_error", f"items[{i}]: нужен product или custom с name.",
                           fields={f"items[{i}].product": ["Обязательное поле."]})
        row = dict(it)
        row["qty"] = str(_num(it.get("qty"), "qty", i, positive=True))
        if it.get("price") is not None:
            row["price"] = str(_num(it["price"], "price", i))
        if it.get("discount") is not None:
            row["discount"] = str(_num(it["discount"], "discount", i))
        out.append(row)
    return out


class HeldCartListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def get(self, request):
        company = company_of(request)
        qs = HeldCart.objects.filter(company=company).select_related("created_by", "taken_by")
        if request.query_params.get("cashbox"):
            qs = qs.filter(cashbox_id=request.query_params["cashbox"])
        st = request.query_params.get("status")
        if st == "open":
            qs = qs.filter(status=HeldCart.Status.OPEN, expires_at__gt=timezone.now())
        elif st:
            qs = qs.filter(status=st)
        return paginate(request, qs.order_by("created_at", "id"), serialize_held)

    @idempotent(body_key=True)
    def _create(self, request):
        from apps.construction.models import Cashbox
        from apps.main.models import Client

        company = company_of(request)
        d = request.data
        cashbox = Cashbox.objects.filter(pk=d.get("cashbox") or None, company=company).first() if d.get("cashbox") else None
        if cashbox is None:
            raise ApiError("validation_error", "Касса не найдена.", fields={"cashbox": ["Обязательное поле."]})
        items = _clean_items(d.get("items"))
        client = None
        if d.get("client"):
            client = Client.objects.filter(pk=d["client"], company=company).first()
            if client is None:
                raise ApiError("validation_error", "Клиент не найден.", fields={"client": ["Не найден."]})
        od = None
        if d.get("order_discount") not in (None, ""):
            od = _num(d["order_discount"], "order_discount", 0).quantize(Decimal("0.01"))
        device = clean_device_key(d.get("from_device_key"), field="from_device_key")
        for _ in range(5):
            try:
                with transaction.atomic():
                    last = HeldCart.objects.filter(company=company).aggregate(m=Max("number"))["m"] or 0
                    h = HeldCart.objects.create(
                        company=company, branch_id=cashbox.branch_id, cashbox=cashbox, number=last + 1,
                        label=str(d.get("label") or "")[:120], items=items, order_discount=od, client=client,
                        from_device_key=device, created_by=request.user,
                        expires_at=timezone.now() + HELD_CART_TTL,
                    )
                break
            except IntegrityError:
                continue
        else:
            raise ApiError("conflict", "Не удалось выдать номер, повторите.", status_code=409)
        return Response({"id": str(h.pk), "number": h.number, "expires_at": iso(h.expires_at)},
                        status=status.HTTP_201_CREATED)

    def post(self, request):
        require_idem_key(request)
        return self._create(request)


def _get_held(request, pk):
    company = company_of(request)
    h = HeldCart.objects.filter(pk=pk, company=company).select_related("created_by", "taken_by").first()
    if h is None:
        raise not_found("Отложенный чек")
    return company, h


class HeldCartDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def get(self, request, pk):
        return Response(serialize_held(_get_held(request, pk)[1]))


class HeldCartTakeAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def post(self, request, pk):
        company, h = _get_held(request, pk)
        device = clean_device_key(request.data.get("device_key"), required=True)
        if not branch_ok(request.user, h.branch_id):
            raise ApiError("permission_denied", "Чек другого филиала.", status_code=403)
        now = timezone.now()
        n = HeldCart.objects.filter(pk=h.pk, company=company, status=HeldCart.Status.OPEN, expires_at__gt=now).update(
            status=HeldCart.Status.TAKEN, taken_at=now, taken_by=request.user, taken_by_device=device)
        h.refresh_from_db()
        if n == 1:
            return Response(serialize_held(h))
        if h.status == HeldCart.Status.OPEN:  # не открыт по сроку
            raise ApiError("expired", "Срок отложенного чека истёк.", status_code=409)
        raise ApiError("already_taken", "Отложенный чек уже забран.", status_code=409,
                       data={"status": h.status, "taken_by_device": h.taken_by_device or None})


class HeldCartCloseAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def post(self, request, pk):
        from apps.main.models import Sale

        company, h = _get_held(request, pk)
        sale = None
        if request.data.get("sale"):
            sale = Sale.objects.filter(pk=request.data["sale"], company=company).first()
            if sale is None:
                raise not_found("Продажа")
        if h.status == HeldCart.Status.CLOSED:
            return Response(serialize_held(h))  # повтор close — 200
        if h.status != HeldCart.Status.TAKEN:
            raise ApiError("not_taken", "Сначала заберите чек (take).", status_code=409, data={"status": h.status})
        if h.taken_by_id != request.user.pk and not _is_owner_like(request.user):
            raise ApiError("permission_denied", "Чек забрал другой сотрудник.", status_code=403)
        HeldCart.objects.filter(pk=h.pk, status=HeldCart.Status.TAKEN).update(
            status=HeldCart.Status.CLOSED, sale=sale, closed_at=timezone.now())
        h.refresh_from_db()
        return Response(serialize_held(h))
