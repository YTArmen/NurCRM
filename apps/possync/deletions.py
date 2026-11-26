"""
Журнал удалённых строк корзины для веб-кассы (ТЗ веб-кассы, п. 12.7).

Веб-касса оформляет чек одним запросом и не вызывает DELETE по строкам корзины, поэтому
удалённые кассиром строки присылаются вместе с чеком (`deleted_items`) либо отдельно
(`POST main/pos/cart-item-deletions/`). Пишем в штатный CartItemDeletionLog.

Строка: {"product": "<uuid>" | null, "name": "…", "qty": "2", "cart_item_id": "<uuid>" | null}
Нужно хотя бы product или name; qty > 0.
"""
from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from rest_framework import permissions
from rest_framework.response import Response

from apps.main.models import CartItemDeletionLog, Product
from apps.main.pos_serializers import CartItemDeletionLogSerializer
from apps.main.pos_utils import qty3
from apps.main.pos_views import CartItemDeletionLogListAPIView
from core.exceptions import ApiError


def _uuid_or_none(raw, field, idx):
    if raw in (None, ""):
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise ApiError("validation_error", f"deleted_items[{idx}].{field}: некорректный UUID.",
                       fields={f"deleted_items[{idx}].{field}": ["Некорректный UUID."]})


def write_deletions(*, company, branch, user, items, cart=None) -> list:
    rows = []
    if not items:
        return rows
    parsed = []
    for idx, raw in enumerate(items):
        pid = _uuid_or_none(raw.get("product") or raw.get("product_id"), "product", idx)
        name = str(raw.get("name") or "").strip()[:255]
        try:
            qty = qty3(Decimal(str(raw.get("qty", raw.get("quantity", "1")))))
        except (InvalidOperation, ValueError):
            qty = Decimal("0")
        if qty <= 0:
            raise ApiError("validation_error", f"deleted_items[{idx}].qty: количество должно быть больше нуля.",
                           fields={f"deleted_items[{idx}].qty": ["Больше нуля."]})
        if not pid and not name:
            raise ApiError("validation_error", f"deleted_items[{idx}]: укажите product или name.",
                           fields={f"deleted_items[{idx}]": ["Укажите product или name."]})
        parsed.append((pid, name, qty, _uuid_or_none(raw.get("cart_item_id"), "cart_item_id", idx)))
    products = {p.id: p for p in Product.objects.filter(
        company_id=company.id, id__in=[p[0] for p in parsed if p[0]]).only("id", "name")}
    for pid, name, qty, cart_item_id in parsed:
        product = products.get(pid) if pid else None
        rows.append(CartItemDeletionLog.objects.create(
            company=company, branch=branch, cart=cart, cart_item_id=cart_item_id, product=product,
            product_name=(name or (product.name if product else "Товар"))[:255],
            quantity=qty, deleted_by=user if getattr(user, "is_authenticated", False) else None,
        ))
    return rows


class CartItemDeletionsAPIView(CartItemDeletionLogListAPIView):
    """
    GET  /api/main/pos/cart-item-deletions/ — журнал, как раньше.
    POST /api/main/pos/cart-item-deletions/ {"items": [<строка deleted_items>, …], "branch": "<uuid>"?}
         → 201 {"created": N, "results": [...]}
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        company = self._company()
        items = request.data.get("items") if hasattr(request.data, "get") else None
        if not isinstance(items, list) or not items:
            raise ApiError("validation_error", "Передайте непустой список items.", fields={"items": ["Обязательное поле."]})
        if len(items) > 500:
            raise ApiError("validation_error", "Не больше 500 строк за раз.", fields={"items": ["Не больше 500."]})
        branch = self._auto_branch()
        rows = write_deletions(company=company, branch=branch, user=request.user, items=items)
        return Response(
            {"created": len(rows), "results": CartItemDeletionLogSerializer(rows, many=True).data}, status=201
        )
