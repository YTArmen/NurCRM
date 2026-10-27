"""
Единое ТЗ 26.10.2026, Б-02: отчёт «до пересчёта остатков по вариантам» (только чтение).

GET api/main/products/variant-stock-diff/ — владелец и админ:
    [{product, name, quantity_now, quantity_after, diff}]
Товары с вариантами, у которых остаток товара не равен сумме остатков активных вариантов
(так его пересчитает сервер, см. variant_utils.recalc_product_quantity). Сам пересчёт здесь не делается.
"""
from decimal import Decimal

from django.db.models import Sum
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Product, ProductVariant
from apps.utils import _is_owner_like
from core.exceptions import ApiError


class VariantStockDiffAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        if company is None:
            return Response([])
        if not (_is_owner_like(user) or getattr(user, "owned_company_id", None)):
            raise ApiError("permission_denied", "Отчёт доступен владельцу и администратору.", status_code=403)
        with_variants = set(
            ProductVariant.objects.filter(product__company=company).values_list("product_id", flat=True).distinct()
        )
        after = {
            r["product_id"]: r["s"] or Decimal("0")
            for r in ProductVariant.objects.filter(product__company=company, is_active=True)
            .values("product_id").annotate(s=Sum("quantity"))
        }
        rows = []
        for p in Product.objects.filter(company=company, id__in=with_variants).only("id", "name", "quantity").order_by("name"):
            now = p.quantity or Decimal("0")
            new = after.get(p.id, Decimal("0"))
            if now != new:
                rows.append({
                    "product": str(p.id), "name": p.name,
                    "quantity_now": str(now), "quantity_after": str(new), "diff": str(new - now),
                })
        return Response(rows)
