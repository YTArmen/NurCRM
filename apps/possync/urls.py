"""
Маршруты ТЗ-28. Подключаются в core/urls.py ПЕРЕД apps.main.urls и apps.construction.urls,
чтобы перекрыть pos/checkout/ и clients/<id>/debts/.
"""
from django.urls import path

from .sales_views import SaleItemsListAPIView, SaleListWithDatesAPIView
from .views import (
    BootstrapAPIView,
    ClientDebtsAPIView,
    ClientDetailWithDebtLimitAPIView,
    PosQuickCheckoutGuardedAPIView,
    ProductByBarcodeWithVariantsAPIView,
    ProductsStockAPIView,
    ShiftCurrentAPIView,
    StockMovementsAPIView,
)

# префикс api/main/
main_urlpatterns = [
    path("bootstrap/", BootstrapAPIView.as_view(), name="possync-bootstrap"),
    path("stock/movements/", StockMovementsAPIView.as_view(), name="possync-stock-movements"),
    path("products/stock/", ProductsStockAPIView.as_view(), name="possync-products-stock"),
    path("clients/<uuid:pk>/", ClientDetailWithDebtLimitAPIView.as_view(), name="possync-client-detail"),
    path("clients/<uuid:client_id>/debts/", ClientDebtsAPIView.as_view(), name="possync-client-debts"),
    path("pos/sales/", SaleListWithDatesAPIView.as_view(), name="possync-pos-sale-list"),
    path("pos/sale-items/", SaleItemsListAPIView.as_view(), name="possync-pos-sale-items"),
    path("pos/checkout/", PosQuickCheckoutGuardedAPIView.as_view(), name="possync-pos-quick-checkout"),
    path("products/barcode/<str:barcode>/", ProductByBarcodeWithVariantsAPIView.as_view(), name="possync-product-by-barcode"),
]

# префикс api/construction/
construction_urlpatterns = [
    path("shifts/current/", ShiftCurrentAPIView.as_view(), name="possync-shift-current"),
]

urlpatterns = main_urlpatterns
