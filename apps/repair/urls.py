"""П2-1 (15.11.2026): подключается из apps/main/urls.py → адреса api/main/…"""
from django.urls import path, re_path

from apps.repair import views_repair as r
from apps.repair import views_tradein as t
from apps.repair import views_ext as x  # rps2611

urlpatterns = [
    path("repair-orders/", r.RepairOrdersAPIView.as_view(), name="repair-orders"),
    re_path(r"^repair-orders/(?P<external_id>[0-9a-fA-F]{32})/$", x.RepairExternalAPIView.as_view(), name="repair-order-external"),
    path("public/companies/<slug:slug>/repair-status/", x.PublicRepairStatusAPIView.as_view(), name="public-repair-status"),
    path("repair-orders/import/", r.RepairImportAPIView.as_view(), name="repair-orders-import"),
    path("repair-orders/by-serial/", r.RepairBySerialAPIView.as_view(), name="repair-orders-by-serial"),
    path("repair-orders/<uuid:pk>/", r.RepairOrderDetailAPIView.as_view(), name="repair-order-detail"),
    path("repair-orders/<uuid:pk>/status/", r.RepairStatusAPIView.as_view(), name="repair-order-status"),
    path("repair-orders/<uuid:pk>/prepay/", r.RepairPrepayAPIView.as_view(), name="repair-order-prepay"),
    path("repair-orders/<uuid:pk>/refund/", r.RepairRefundAPIView.as_view(), name="repair-order-refund"),
    path("repair-orders/<uuid:pk>/issue/", r.RepairIssueAPIView.as_view(), name="repair-order-issue"),
    path("repair-orders/<uuid:pk>/notify/", r.RepairNotifyAPIView.as_view(), name="repair-order-notify"),
    path("repair/settings/", r.RepairSettingsAPIView.as_view(), name="repair-settings"),
    path("repair/permissions/<uuid:user_id>/", r.RepairStaffPermsAPIView.as_view(), name="repair-permissions"),
    path("trade-in/", t.TradeInsAPIView.as_view(), name="trade-in-list"),
    path("trade-in/import/", t.TradeInImportAPIView.as_view(), name="trade-in-import"),
    path("trade-in/<uuid:pk>/", t.TradeInDetailAPIView.as_view(), name="trade-in-detail"),
    path("trade-in/<uuid:pk>/payout/", t.TradeInPayoutAPIView.as_view(), name="trade-in-payout"),
    path("trade-in/<uuid:pk>/cancel/", t.TradeInCancelAPIView.as_view(), name="trade-in-cancel"),
    path("trade-in/<uuid:pk>/stock/", t.TradeInStockAPIView.as_view(), name="trade-in-stock"),
    path("trade-in/<uuid:pk>/photos/", t.TradeInPhotosAPIView.as_view(), name="trade-in-photos"),
    path("trade-in/<uuid:pk>/photos/<int:photo_id>/", t.TradeInPhotosAPIView.as_view(), name="trade-in-photo"),
]
