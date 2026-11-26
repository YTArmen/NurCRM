"""POST main/products/<id>/convert-to-packages/ (логика — apps/main/product_pack_convert.py)."""
from rest_framework import permissions
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from apps.main.product_pack_convert import convert_stock_to_packages
from apps.main.views import CompanyBranchRestrictedMixin
from apps.users.permissions import read_only_guard


@read_only_guard("can_edit_products")
class ProductConvertToPackagesAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        return idempotent(body_key=True, replay_flag=True)(convert_stock_to_packages)(self, request, pk)
