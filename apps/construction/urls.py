from django.urls import path
from apps.construction.views import (
    CashShiftReportView,
    CashboxListCreateView,
    CashboxDetailView,
    CashboxBulkDeleteView,
    CashFlowListCreateView,
    CashFlowDetailView,
    CashFlowCategoryListCreateView,
    CashFlowCategoryDetailView,
    CashboxOwnerDetailView,
    CashboxOwnerDetailSingleView,
    CashShiftListView,
    CashShiftDetailView,
    CashShiftOpenView,
    CashShiftCloseView,
    CashFlowBulkStatusUpdateView,
    CashFlowEditRequestView,
    CashFlowCancelRequestView,
    CashboxReportView,
    CashboxReportAnalyticsView,
)
from apps.construction.sale_history_views import CashShiftSalesListView
from apps.construction.views_buhgalter import AuditLogView, CashboxBalancesView, CashFlowCheckView

urlpatterns = [
    path("audit-log/", AuditLogView.as_view(), name="construction-audit-log"),
    path("cashboxes/balances/", CashboxBalancesView.as_view(), name="cashbox-balances"),
    path("cashboxes/", CashboxListCreateView.as_view(), name="cashbox-list-create"),
    path("cashboxes/<uuid:pk>/", CashboxDetailView.as_view(), name="cashbox-detail"),
    path("cashboxes/bulk-delete/", CashboxBulkDeleteView.as_view(), name="cashbox-bulk-delete"),

    path("cashboxes/<uuid:cashbox_id>/report/", CashboxReportView.as_view(), name="cashbox-report"),
    path("cashbox-reports/analytics/", CashboxReportAnalyticsView.as_view(), name="cashbox-reports-analytics"),

    path("cashboxes/<uuid:cashbox_id>/report/", CashboxReportView.as_view(), name="cashbox-report"),
    path("cashbox-reports/analytics/", CashboxReportAnalyticsView.as_view(), name="cashbox-reports-analytics"),

    path("cashflows/", CashFlowListCreateView.as_view(), name="cashflow-list-create"),
    path("cashflows/<uuid:pk>/", CashFlowDetailView.as_view(), name="cashflow-detail"),
    path("cashflows/<uuid:pk>/edit-request/", CashFlowEditRequestView.as_view(), name="cashflow-edit-request"),
    path("cashflows/<uuid:pk>/cancel-request/", CashFlowCancelRequestView.as_view(), name="cashflow-cancel-request"),
    path("cashflows/<uuid:pk>/check/", CashFlowCheckView.as_view(), name="cashflow-check"),
    path("cashflows/bulk/status/", CashFlowBulkStatusUpdateView.as_view(), name="cashflow-bulk-status"),

    path("cashflow-categories/", CashFlowCategoryListCreateView.as_view(), name="cashflow-category-list-create"),
    path("cashflow-categories/<uuid:pk>/", CashFlowCategoryDetailView.as_view(), name="cashflow-category-detail"),

    path("cashboxes/detail/owner/", CashboxOwnerDetailView.as_view(), name="owner-cashboxes"),
    path("cashboxes/<uuid:pk>/detail/owner/", CashboxOwnerDetailSingleView.as_view(), name="owner-cashbox-detail"),

    path("shifts/", CashShiftListView.as_view(), name="cashshift-list"),
    path("shifts/open/", CashShiftOpenView.as_view(), name="cashshift-open"),
    path("shifts/<uuid:pk>/", CashShiftDetailView.as_view(), name="cashshift-detail"),
    path("shifts/<uuid:pk>/close/", CashShiftCloseView.as_view(), name="cashshift-close"),
    path("shifts/<uuid:pk>/report/", CashShiftReportView.as_view(), name="cash-shift-report"),
    path("cash/shifts/<uuid:pk>/sales/", CashShiftSalesListView.as_view(), name="cash-shift-sales"),
]

