"""Б-61: адреса партий, отрезного товара и предупреждений."""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Cart, CartItem, Product
from apps.main.views import CompanyBranchRestrictedMixin
from apps.users.permissions_flags import PosAccess, flag_guard
from core.exceptions import ApiError

from . import services
from .models import ProductBatch, ProductCutSettings, ProductRemnant, ProductRoll


def _s(v):
    return None if v is None else str(v)


def _dec(data, key, *, required=False, default=None, minimum=Decimal("0")):
    raw = data.get(key)
    if raw in (None, ""):
        if required:
            raise ApiError("validation_error", f"Поле {key} обязательно.", fields={f"cut.{key}": ["Обязательное поле."]})
        return default
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ApiError("validation_error", f"Поле {key}: ожидается число.", fields={f"cut.{key}": ["Ожидается число."]})
    if d < minimum:
        raise ApiError("validation_error", f"Поле {key} не может быть меньше {minimum}.",
                       fields={f"cut.{key}": ["Слишком мало."]})
    return d


def batch_dict(b):
    return {"id": str(b.pk), "label": b.label, "remaining": _s(b.remaining),
            "received_total": _s(b.received_total), "created_at": b.created_at.isoformat()}


def cut_dict(c):
    return {"roll_length": _s(c.roll_length), "min_cut": _s(c.min_cut),
            "remnant_threshold": _s(c.remnant_threshold),
            "remnant_discount_percent": _s(c.remnant_discount_percent)}


class _ProductMixin(CompanyBranchRestrictedMixin):
    def _product(self, pk):
        return get_object_or_404(self._filter_qs_company_branch(Product.objects.all()), pk=pk)


class ProductBatchesAPIView(_ProductMixin, APIView):
    """GET api/main/products/{id}/batches/ — остаток по партиям."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        product = self._product(pk)
        rows = list(ProductBatch.objects.filter(product=product).order_by("created_at"))
        in_batches = sum((b.remaining for b in rows), Decimal("0"))
        return Response({
            "product": str(product.pk), "count": len(rows),
            "results": [batch_dict(b) for b in rows],
            "total_in_batches": _s(in_batches),
            "unbatched_quantity": _s(Decimal(str(product.quantity or 0)) - in_batches),
        })


@flag_guard("can_view_products")
class ProductCutAPIView(_ProductMixin, APIView):
    """GET/PUT/DELETE api/main/products/{id}/cut/ — настройка отрезного товара."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        product = self._product(pk)
        c = ProductCutSettings.objects.filter(product=product).first()
        return Response({"product": str(product.pk), "cut": cut_dict(c) if c else None})

    @transaction.atomic
    def put(self, request, pk):
        product = self._product(pk)
        data = request.data.get("cut", request.data)
        roll = _dec(data, "roll_length", required=True)
        if roll <= 0:
            raise ApiError("validation_error", "roll_length должен быть больше 0.",
                           fields={"cut.roll_length": ["Больше нуля."]})
        c, _ = ProductCutSettings.objects.get_or_create(
            product=product, defaults={"company": product.company, "roll_length": roll})
        c.roll_length = roll
        c.min_cut = _dec(data, "min_cut", default=Decimal("0"))
        c.remnant_threshold = _dec(data, "remnant_threshold", default=Decimal("0"))
        pct = _dec(data, "remnant_discount_percent", default=Decimal("0"))
        if pct > 100:
            raise ApiError("validation_error", "Скидка не больше 100%.",
                           fields={"cut.remnant_discount_percent": ["Не больше 100."]})
        c.remnant_discount_percent = pct
        c.save()
        return Response({"product": str(product.pk), "cut": cut_dict(c)})

    @transaction.atomic
    def delete(self, request, pk):
        ProductCutSettings.objects.filter(product=self._product(pk)).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProductRollsAPIView(_ProductMixin, APIView):
    """GET api/main/products/{id}/rolls/ — бухты и обрезки отрезного товара."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        product = self._product(pk)
        rolls = ProductRoll.objects.filter(product=product).order_by("-opened_at")[:200]
        rem = ProductRemnant.objects.filter(remnant_of=product).select_related("product")[:200]
        active = ProductRoll.objects.filter(product=product, status=ProductRoll.Status.ACTIVE)
        return Response({
            "product": str(product.pk),
            "active_remaining": _s(active.aggregate(s=Sum("remaining"))["s"] or Decimal("0")),
            "rolls": [{"id": str(r.pk), "length": _s(r.length), "remaining": _s(r.remaining), "status": r.status,
                       "opened_at": r.opened_at.isoformat()} for r in rolls],
            "remnants": [{"product": str(x.product_id), "name": x.product.name, "length": _s(x.length),
                          "discount_percent": _s(x.discount_percent), "price": _s(x.product.price),
                          "quantity": _s(x.product.quantity)} for x in rem],
        })


class CartItemBatchAPIView(APIView):
    """PUT {"batch": id|метка} / DELETE api/main/pos/carts/{cart}/items/{item}/batch/ — партия строки корзины."""
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def _item(self, request, cart_id, item_id):
        cart = get_object_or_404(Cart, pk=cart_id, company=request.user.company, status=Cart.Status.ACTIVE)
        return get_object_or_404(CartItem, pk=item_id, cart=cart)

    @transaction.atomic
    def put(self, request, cart_id, item_id):
        item = self._item(request, cart_id, item_id)
        ref = request.data.get("batch")
        if ref in (None, ""):
            raise ApiError("validation_error", "Нужно поле batch.", fields={"batch": ["Обязательное поле."]})
        b = services.set_cart_item_batch(item, ref)
        return Response({"cart_item": str(item.pk), "batch": batch_dict(b)})

    @transaction.atomic
    def delete(self, request, cart_id, item_id):
        services.set_cart_item_batch(self._item(request, cart_id, item_id), None)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CartQuoteAPIView(APIView):
    """POST api/main/pos/cart/quote/ — предупреждения по партиям до оплаты.

    Тело: {"cart": "<uuid>"} или {"items": [{"product", "qty", "batch"?}]}.
    Ответ: {"warnings": [{"code": "mixed_batches", ...}], "lines": [...]}.
    """
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def post(self, request):
        company = request.user.owned_company or request.user.company
        lines = []
        if request.data.get("cart"):
            cart = get_object_or_404(Cart, pk=request.data["cart"], company=company)
            lines = services.cart_lines(cart)
        else:
            for idx, raw in enumerate(request.data.get("items") or []):
                if not isinstance(raw, dict) or not raw.get("product"):
                    continue
                bid = blabel = None
                if raw.get("batch") not in (None, ""):
                    b = services.resolve_batch(company.pk, raw["product"], raw["batch"], index=idx)
                    bid, blabel = b.pk, b.label
                lines.append({"product": raw["product"], "variant": raw.get("variant"),
                              "quantity": raw.get("qty"), "batch_id": bid, "batch_label": blabel})
        return Response({
            "warnings": services.mixed_batches_warnings(lines),
            "lines": [{"product": str(l["product"]), "quantity": _s(l.get("quantity")),
                       "batch": _s(l.get("batch_id")), "batch_label": l.get("batch_label")} for l in lines],
        })
