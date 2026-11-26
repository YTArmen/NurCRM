"""ТЗ 26.10 Б-55/Б-56: адреса уровней цен, цен товара, объектов клиента."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Client, Product
from apps.main.views import _filter_clients_visible_for_user
from core.api_compat import is_hardware_strict
from core.exceptions import ApiError

from . import services as svc
from .models import ClientObject, PriceLevel


def _company(request):
    company = svc.company_of(request.user)
    if company is None:
        raise ValidationError({"detail": "Пользователь не привязан к компании."})
    return company


class PriceLevelListCreateAPIView(APIView):
    """GET/POST api/main/price-levels/ — справочник уровней компании (retail и wholesale создаются при первом GET)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = _company(request)
        svc.ensure_system_levels(company)
        return Response([svc.level_payload(lv) for lv in PriceLevel.objects.filter(company=company)])

    def post(self, request, *args, **kwargs):
        if not svc.can_edit_prices(request.user):
            raise svc.deny("can_view_products", "Товары")
        with transaction.atomic():
            lv = svc.create_level(_company(request), request.data)
        return Response(svc.level_payload(lv), status=status.HTTP_201_CREATED)


class PriceLevelDetailAPIView(APIView):
    """PATCH/DELETE api/main/price-levels/{id}/ — переименовать/упорядочить; удалить можно только свой неиспользуемый."""

    permission_classes = [permissions.IsAuthenticated]

    def _level(self, request, pk):
        return get_object_or_404(PriceLevel, pk=pk, company=_company(request))

    def get(self, request, pk, *args, **kwargs):
        return Response(svc.level_payload(self._level(request, pk)))

    def patch(self, request, pk, *args, **kwargs):
        if not svc.can_edit_prices(request.user):
            raise svc.deny("can_view_products", "Товары")
        lv = svc.update_level(self._level(request, pk), request.data)
        return Response(svc.level_payload(lv))

    def delete(self, request, pk, *args, **kwargs):
        if not svc.can_edit_prices(request.user):
            raise svc.deny("can_view_products", "Товары")
        lv = self._level(request, pk)
        if lv.is_system:
            raise ApiError("price_level_system", "Системный уровень удалить нельзя.", status_code=400)
        if lv.clients.exists():
            raise ApiError("price_level_in_use", "Уровень назначен клиентам.", status_code=409)
        lv.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PriceQuoteAPIView(APIView):
    """
    POST api/main/price-levels/quote/ {client?, items:[{product, qty, price?}]} -> цена каждой строки по правилам Б-55
    (уровень клиента, объём, ручная цена). `applied` — будет ли сервер так считать в продаже (флаг + новая касса).
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        company = _company(request)
        items = request.data.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError({"items": "Передайте непустой список."})
        client = None
        if request.data.get("client"):
            client = get_object_or_404(Client.objects.select_related("price_level"), id=request.data["client"], company=company)
        lines = svc.quote_lines(company, client, items)
        return Response({"applied": is_hardware_strict(request, company), "items": lines})


class ProductPricesAPIView(APIView):
    """GET/PUT/PATCH api/main/products/{id}/prices/ -> {prices:[{level,price}], volume_prices:[{min_qty,price,level}]}."""

    permission_classes = [permissions.IsAuthenticated]

    def _product(self, request, pk):
        return get_object_or_404(Product.objects.select_related("company"), pk=pk, company=_company(request))

    def get(self, request, pk, *args, **kwargs):
        return Response(svc.product_prices_payload(self._product(request, pk)))

    def put(self, request, pk, *args, **kwargs):
        if not svc.can_edit_prices(request.user):
            raise svc.deny("can_view_products", "Товары")
        product = self._product(request, pk)
        data = request.data
        svc.save_product_prices(
            product,
            data.get("prices") if "prices" in data else None,
            data.get("volume_prices") if "volume_prices" in data else None,
        )
        product.refresh_from_db()
        return Response(svc.product_prices_payload(product))

    patch = put


class _ClientObjectsBase(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _client(self, request, client_id):
        if not svc.can_edit_clients(request.user):
            raise svc.deny("can_view_clients", "Клиенты")
        qs = _filter_clients_visible_for_user(Client.objects.filter(company=_company(request)), request.user)
        return get_object_or_404(qs, id=client_id)


class ClientObjectsAPIView(_ClientObjectsBase):
    """GET/POST api/main/clients/{id}/objects/ -> [{id, name, address, is_active}] (?active=1 — только действующие)."""

    def get(self, request, client_id, *args, **kwargs):
        client = self._client(request, client_id)
        qs = ClientObject.objects.filter(client=client)
        if request.query_params.get("active") in ("1", "true", "True"):
            qs = qs.filter(is_active=True)
        return Response([svc.object_payload(o) for o in qs])

    def post(self, request, client_id, *args, **kwargs):
        client = self._client(request, client_id)
        obj = svc.save_object(ClientObject(client=client), request.data)
        return Response(svc.object_payload(obj), status=status.HTTP_201_CREATED)


class ClientObjectDetailAPIView(_ClientObjectsBase):
    """PATCH api/main/clients/{id}/objects/{object_id}/. Удаления нет: объект уходит в архив (is_active=false)."""

    def get(self, request, client_id, object_id, *args, **kwargs):
        client = self._client(request, client_id)
        return Response(svc.object_payload(get_object_or_404(ClientObject, pk=object_id, client=client)))

    def patch(self, request, client_id, object_id, *args, **kwargs):
        client = self._client(request, client_id)
        obj = get_object_or_404(ClientObject, pk=object_id, client=client)
        return Response(svc.object_payload(svc.save_object(obj, request.data)))
