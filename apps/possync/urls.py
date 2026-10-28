"""
Маршруты ТЗ-28. Подключаются в core/urls.py ПЕРЕД apps.main.urls и apps.construction.urls,
чтобы перекрыть pos/checkout/ и clients/<id>/debts/.
"""
from django.urls import path

from .deal_reversal import ClientDealDestroyWithCashflowAPIView, ClientDealRefundWithCashflowAPIView
from .deletions import CartItemDeletionsAPIView
from .sales_views import SaleItemsListAPIView, SaleListWithDatesAPIView
from .views_reports import AnalyticsHoursSourcesAPIView, ProductHistoryAPIView, ProductsSalesVelocityAPIView
from .views import (
    BootstrapAPIView,
    ClientDebtsAPIView,
    ClientDetailWithDebtLimitAPIView,
    DebtLimitsAPIView,
    DebtLimitsUsageAPIView,
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
    path("debt-limits/", DebtLimitsAPIView.as_view(), name="possync-debt-limits"),
    path("debt-limits/usage/", DebtLimitsUsageAPIView.as_view(), name="possync-debt-limits-usage"),
    path("pos/sales/", SaleListWithDatesAPIView.as_view(), name="possync-pos-sale-list"),
    path("pos/sale-items/", SaleItemsListAPIView.as_view(), name="possync-pos-sale-items"),
    path("pos/cart-item-deletions/", CartItemDeletionsAPIView.as_view(), name="possync-cart-item-deletions"),
    path("clients/<uuid:client_id>/deals/<uuid:pk>/refund/", ClientDealRefundWithCashflowAPIView.as_view(), name="possync-deal-refund"),
    path("clients/<uuid:client_id>/deals/<uuid:pk>/unpay/", ClientDealRefundWithCashflowAPIView.as_view(), name="possync-deal-unpay"),
    path("clients/<uuid:client_id>/deals/<uuid:pk>/", ClientDealDestroyWithCashflowAPIView.as_view(), name="possync-client-deal-detail"),
    path("clientdeals/<uuid:pk>/refund/", ClientDealRefundWithCashflowAPIView.as_view(), name="possync-clientdeal-refund"),
    path("clientdeals/<uuid:pk>/unpay/", ClientDealRefundWithCashflowAPIView.as_view(), name="possync-clientdeal-unpay"),
    path("deals/<uuid:pk>/unpay/", ClientDealRefundWithCashflowAPIView.as_view(), name="possync-deal-unpay-legacy"),
    path("clientdeals/<uuid:pk>/", ClientDealDestroyWithCashflowAPIView.as_view(), name="possync-clientdeal-detail"),
    path("pos/checkout/", PosQuickCheckoutGuardedAPIView.as_view(), name="possync-pos-quick-checkout"),
    path("products/sales-velocity/", ProductsSalesVelocityAPIView.as_view(), name="possync-sales-velocity"),
    path("products/<uuid:product_id>/history/", ProductHistoryAPIView.as_view(), name="possync-product-history"),
    path("analytics/market/hours-sources/", AnalyticsHoursSourcesAPIView.as_view(), name="possync-hours-sources"),
    path("products/barcode/<str:barcode>/", ProductByBarcodeWithVariantsAPIView.as_view(), name="possync-product-by-barcode"),
]

# префикс api/construction/
construction_urlpatterns = [
    path("shifts/current/", ShiftCurrentAPIView.as_view(), name="possync-shift-current"),
]

urlpatterns = main_urlpatterns
