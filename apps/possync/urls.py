"""
Маршруты ТЗ-28. Подключаются в core/urls.py ПЕРЕД apps.main.urls и apps.construction.urls,
чтобы перекрыть pos/checkout/ и clients/<id>/debts/.
"""
from django.urls import path

from .views_reports import ProductHistoryAPIView
from .views import (
    BootstrapAPIView,
    ClientDebtsAPIView,
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
    path("clients/<uuid:client_id>/debts/", ClientDebtsAPIView.as_view(), name="possync-client-debts"),
    path("pos/checkout/", PosQuickCheckoutGuardedAPIView.as_view(), name="possync-pos-quick-checkout"),
    path("products/<uuid:product_id>/history/", ProductHistoryAPIView.as_view(), name="possync-product-history"),
    path("products/barcode/<str:barcode>/", ProductByBarcodeWithVariantsAPIView.as_view(), name="possync-product-by-barcode"),
]

# префикс api/construction/
construction_urlpatterns = [
    path("shifts/current/", ShiftCurrentAPIView.as_view(), name="possync-shift-current"),
]

urlpatterns = main_urlpatterns
