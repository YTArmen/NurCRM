from django.urls import path, include

from apps.main.mass_incoming_views import ProductMassIncomingAPIView
from .views import *
from .purchase_orders import PurchaseOrderListCreateAPIView, PurchaseOrderDetailAPIView
from .tz1029_invest import (
    InvestorDetailView, InvestorListCreateView, PayoutDetailView, PayoutListCreateView, StartupPaybackAPIView,
)
from .tz1029_bonus import ClientBonusSummaryAPIView
from .tz1029_views import AppTestChecksView, GlobalProductExportView
from .price_check import ProductCatalogVersionView, ProductPriceCheckView
from .change_views import ChangesFeedView, EventsStreamView  # ТЗ Н-31
from core.capabilities import CapabilitiesAPIView, ClientVersionsReportAPIView  # единое ТЗ Б-01
from apps.main.purchases import PurchaseActivateAPIView, PurchasesListAPIView
from .variant_stock_diff import VariantStockDiffAPIView  # Б-02
from .pos_requests import (PosRequestAcceptView, PosRequestCancelView, PosRequestDeclineView,
                           PosRequestListCreateView)
from .product_changes import ProductChangesView
from .pos_views import *
from apps.construction.views_buhgalter import AuditLogView
from apps.construction.views_admin import TrashListView, TrashRestoreView
from .kassa_stage3 import (
    AppointmentDetailAPIView,
    AppointmentListCreateAPIView,
    AppointmentToSaleAPIView,
    ProductStocksAPIView,
    ProductVariantDetailAPIView,
    ProductVariantListCreateAPIView,
    SaleExchangeAPIView,
    StockTransferListCreateAPIView,
    WorkOrderDetailAPIView,
    WorkOrderIssueAPIView,
    WorkOrderListCreateAPIView,
)
from .kassa_views import (
    ClientBonusAPIView,
    ClientBonusHistoryAPIView,
    ClientDebtorsAPIView,
    ClientPayDebtAPIView,
    ClientDirectDebtAPIView,
    PosQuickCheckoutAPIView,
    SaleReturnListAPIView,
)
from .analytics_market import *
from .analytics_dashboard import *
from .document import SaleReceiptAPIView, SaleInvoiceAPIView
from apps.main.showcase.size_charts import SizeChartListCreateAPIView, SizeChartDetailAPIView
from apps.main.supplier_receipt_pdf import SupplierReceiptPdfAPIView
from apps.main.media_receipts import MainMediaListCreateAPIView, MainMediaDetailAPIView, SalePublicLinkAPIView
from apps.main.showcase.views_public import PublicCompanyShowcaseFacetsAPIView, PublicCompanyProductDetailAPIView, PublicCompanyAPIView, PublicCompanyShowcaseAPIView
from apps.main.recommendations import (
    RecommendationEventsAPIView,
    RecommendationLinkSaleAPIView,
    RecommendationStatsAPIView,
    RecommendationPairsAPIView,
    SalesTargetAPIView,
)
from apps.main.showcase.views_design import (
    ShowcaseDesignAPIView,
    ShowcaseStockRequestListAPIView,
    ShowcaseDraftUpdateAPIView,
    ShowcaseDraftProductOrderAPIView,
    ShowcasePreviewLinkAPIView,
    ShowcasePublishAPIView,
    ShowcaseDiscardAPIView,
    ShowcaseVersionsListAPIView,
    ShowcaseVersionRestoreAPIView,
    ShowcaseMediaListCreateAPIView,
    ShowcaseMediaDetailAPIView,
    ShowcaseBannerListCreateAPIView,
    ShowcaseBannerDetailAPIView,
    ShowcaseBannerReorderAPIView,
    ShowcasePromoBlockListCreateAPIView,
    ShowcasePromoBlockDetailAPIView,
    ShowcasePromoBlockReorderAPIView,
    ShowcaseOrderListAPIView,
    ShowcaseOrderDetailAPIView,
    ShowcaseStatsAPIView,
    PublicCompanyShowcaseDesignAPIView,
    PublicCompanyShowcaseOrderCreateAPIView,
    PublicCompanyShowcaseTrackAPIView,
    ShowcaseEditorOptionsAPIView,
    ShowcaseDraftResetAPIView,
    ShowcaseDraftApplyPresetAPIView,
    ShowcaseProductsListAPIView,
    ShowcaseProductDetailAPIView,
    ShowcaseProductsOrderAPIView,
    ShowcaseProductsBulkAPIView,
    ShowcaseCategoriesListAPIView,
    ShowcaseCategoryDetailAPIView,
    ShowcaseCategoriesOrderAPIView,
    ShowcasePageListCreateAPIView,
    ShowcasePageDetailAPIView,
    ShowcaseSlugCheckAPIView,
)
from apps.main.showcase.views_public_design import (
    PublicCompanyShowcaseEventsAPIView,
    PublicCompanyShowcasePageAPIView,
    PublicCompanyShowcaseCategoriesAPIView,
    PublicCompanyShowcaseCartQuoteAPIView,
    PublicCompanyShowcaseNotifyAPIView,
)
from apps.main.inventory_views import (
    ProductInventorySessionListCreateAPIView,
    ProductInventorySessionRetrieveAPIView,
    ProductInventorySessionApplyAPIView,
    ProductInventorySessionCancelAPIView,
)
from apps.main.branch_transfers_views import (
    BranchTransferListCreateAPIView,
    BranchTransferDetailAPIView,
    BranchTransferCancelAPIView,
)
from apps.main.production_salary_views import (
    ProductionRateListAPIView,
    ProductionRateDetailAPIView,
    ProductionPieceRateListAPIView,
    ProductionPieceRateDetailAPIView,
    ProductionWorkSessionListCreateAPIView,
    ProductionWorkSessionDetailAPIView,
    ProductionAccrualListAPIView,
    ProductionSalarySummaryAPIView,
    ProductionPayoutListCreateAPIView,
)
from apps.main.market_salary_views import MarketSalaryPayoutDetailAPIView, MarketSalaryPayoutListCreateAPIView
from apps.construction.views import CashFlowListCreateView
from apps.users.app_settings_views import CompanyAppSettingsAPIView
from apps.main.part12_views import (
    AnalyticsFinanceAPIView,
    AnalyticsMarketSummaryAPIView,
    BarcodeDuplicatesAPIView,
    PosCheckoutBatchAPIView,
)


from apps.main.views_payables import SupplierPayablesAPIView
from apps.main.telegram_bot.views import BotNotifyOwnerView
from apps.main.ai_agent_api import (
    ProductVoiceSearchAPIView, OrderDraftAPIView, OrderDraftStatusAPIView, VoiceCallAPIView,
)
from apps.main.views_idempotency import IdempotencyLookupAPIView

from apps.main.client_documents import (
    ClientDocumentDetailAPIView,
    ClientDocumentListCreateAPIView,
    ClientDocumentPayAPIView,
)

from apps.main.pos_bulk import CartClearAPIView, SaleItemsBulkAPIView  # ТЗ Н-15

urlpatterns = [
    path("", include("apps.main.tz26.urls")),  # ТЗ 26.10 Б-52…Б-72: новые адреса (выше старых, чтобы tasks/summary/ и т.п. не съел старый маршрут)
    # ТЗ «Админка»: журнал действий (тот же, что construction/audit-log/) и корзина удалённого
    path("audit-log/", AuditLogView.as_view(), name="main-audit-log"),
    path("trash/", TrashListView.as_view(), name="main-trash"),
    path("trash/<str:type>/<str:object_id>/restore/", TrashRestoreView.as_view(), name="main-trash-restore"),
    # Приложение клиентов: поиск по телефону, QR, перенос бонусов, магазин в приложении, приглашения
    path("", include("apps.clientapp.staff_urls")),

    # 🔹 Контакты
    path('socialapplications/<uuid:pk>/', SocialApplicationsRetrieveUpdateDestroyAPIView.as_view(), name='socialapplications-detail'),
    path('socialapplications/', SocialApplicationsListCreateAPIView.as_view(), name='socialapplications-list-create'),
    
    path('applications/<uuid:pk>/', BidRetrieveUpdateDestroyAPIView.as_view(), name='applications-detail'),
    path('applications/', BidListCreateAPIView.as_view(), name='applications-list-create'),

    path('contacts/', ContactListCreateAPIView.as_view(), name='contact-list-create'),
    path('contacts/<uuid:pk>/', ContactRetrieveUpdateDestroyAPIView.as_view(), name='contact-detail'),

    # 🔹 Воронки
    path('pipelines/', PipelineListCreateAPIView.as_view(), name='pipeline-list-create'),
    path('pipelines/<uuid:pk>/', PipelineRetrieveUpdateDestroyAPIView.as_view(), name='pipeline-detail'),

    # 🔹 Сделки
    path('deals/', DealListCreateAPIView.as_view(), name='deal-list-create'),
    path('deals/<uuid:pk>/', DealRetrieveUpdateDestroyAPIView.as_view(), name='deal-detail'),

    # 🔹 Задачи
    path('tasks/', TaskListCreateAPIView.as_view(), name='task-list-create'),
    path('tasks/<uuid:pk>/', TaskRetrieveUpdateDestroyAPIView.as_view(), name='task-detail'),

    # 🔹 Заказы и позиции заказов
    path('orders/', OrderListCreateAPIView.as_view(), name='order-list-create'),
    path('orders/<uuid:pk>/', OrderRetrieveUpdateDestroyAPIView.as_view(), name='order-detail'),
    # (если нужна работа с отдельными позициями заказа — реализуй OrderItemAPIView)
    # path('order-items/<uuid:pk>/', OrderItemAPIView.as_view(), name='order-item-detail'),

    # 🔹 Продукты, категории, бренды
    path('products/list/', ProductListView.as_view(), name='product-list'),
    path('products/compact-list/', ProductCompactListView.as_view(), name='product-compact-list'),
    path('products/price-check/', ProductPriceCheckView.as_view(), name='product-price-check'),
    path('pos/requests/', PosRequestListCreateView.as_view(), name='pos-requests'),  # ТЗ Н-31
    path('pos/requests/<uuid:pk>/accept/', PosRequestAcceptView.as_view(), name='pos-request-accept'),
    path('pos/requests/<uuid:pk>/decline/', PosRequestDeclineView.as_view(), name='pos-request-decline'),
    path('pos/requests/<uuid:pk>/cancel/', PosRequestCancelView.as_view(), name='pos-request-cancel'),
    path('events/stream/', EventsStreamView.as_view(), name='events-stream'),  # ТЗ 29.10 Б-102
    path('changes/', ChangesFeedView.as_view(), name='changes-feed'),  # ТЗ Н-31
    path('capabilities/', CapabilitiesAPIView.as_view(), name='api-capabilities'),  # Б-01
    path('purchases/', PurchasesListAPIView.as_view(), name='purchases'),  # ТЗ 29.10 Б-96
    path('purchases/activate/', PurchaseActivateAPIView.as_view(), name='purchases-activate'),
    path('client-versions/', ClientVersionsReportAPIView.as_view(), name='api-client-versions'),  # Б-01
    path('products/variant-stock-diff/', VariantStockDiffAPIView.as_view(), name='variant-stock-diff'),  # Б-02
    path('products/catalog-version/', ProductCatalogVersionView.as_view(), name='product-catalog-version'),
    path('products/changes/', ProductChangesView.as_view(), name='product-changes'),
    path('products/scale-export/', WeightProductsScaleExportAPIView.as_view(), name='product-scale-export'),
    path('products/create-by-barcode/', ProductCreateByBarcodeAPIView.as_view(), name='product-create-by-barcode'),
    path("products/barcode-duplicates/", BarcodeDuplicatesAPIView.as_view(), name="product-barcode-duplicates"),
    # синоним для «Калькуляции» (calculator-after-stress-test/03)
    path("products/duplicate-barcodes/", BarcodeDuplicatesAPIView.as_view(), name="product-duplicate-barcodes"),
    path("app-settings/<str:app>/", CompanyAppSettingsAPIView.as_view(), name="company-app-settings"),
    path('products/create-manual/', ProductCreateManualAPIView.as_view(), name='product-create-manual'),
    path('products/mass-incoming/', ProductMassIncomingAPIView.as_view(), name='product-mass-incoming'),
    path('products/bulk-update/', ProductBulkUpdateAPIView.as_view(), name='product-bulk-update'),
    path('products/<uuid:pk>/', ProductRetrieveUpdateDestroyAPIView.as_view(), name='product-detail'),
    path('products/<uuid:pk>/purchase-batches/', ProductPurchaseBatchListAPIView.as_view(), name='product-purchase-batches'),
    path('products/<uuid:pk>/expiry-batches/', ProductExpiryBatchListAPIView.as_view(), name='product-expiry-batches'),
    path("products/<uuid:product_id>/favorite/", ProductFavoriteAPIView.as_view(), name="product-favorite"),
    path("products/bulk-update/", ProductBulkUpdateAPIView.as_view(), name="product-bulk-update"),
    path("products/bulk-delete/", ProductBulkDeleteAPIView.as_view(), name="product-bulk-delete"),
    path("products/<uuid:product_id>/move-to-raw/", ProductMoveToRawAPIView.as_view(), name="product-move-to-raw"),

    # 🔹 Перемещение готовой продукции в сырьё (история / отмена)
    path("finished-to-raw-transfers/", FinishedToRawTransferListAPIView.as_view(), name="finished-to-raw-list"),
    path("finished-to-raw-transfers/<uuid:pk>/cancel/", FinishedToRawTransferCancelAPIView.as_view(), name="finished-to-raw-cancel"),

    # 🔹 Инвентаризация (сверка остатков)
    path("inventories/", InventoryListCreateAPIView.as_view(), name="inventory-list-create"),
    path("inventories/<uuid:pk>/", InventoryRetrieveAPIView.as_view(), name="inventory-detail"),
    path("inventories/<uuid:pk>/confirm/", InventoryConfirmAPIView.as_view(), name="inventory-confirm"),

    # 🔹 Нехватка готовой продукции (событие + уведомления)
    path("stock-shortages/", StockShortageCreateAPIView.as_view(), name="stock-shortage-create"),

    # 🔹 Журнал движения склада (read-only)
    path("stock-movements/", StockMovementListAPIView.as_view(), name="stock-movement-list"),
    path("stock-movements/<uuid:pk>/", StockMovementDetailAPIView.as_view(), name="stock-movement-detail"),
    path("production/report/", ProductionReportListAPIView.as_view(), name="production-report"),

    # Зарплата в производстве
    path("production/salary/rates/", ProductionRateListAPIView.as_view(), name="production-salary-rates"),
    path("production/salary/rates/<uuid:employee_id>/", ProductionRateDetailAPIView.as_view(), name="production-salary-rate-detail"),
    path("production/salary/piece-rates/", ProductionPieceRateListAPIView.as_view(), name="production-salary-piece-rates"),
    path("production/salary/piece-rates/<uuid:product_id>/", ProductionPieceRateDetailAPIView.as_view(), name="production-salary-piece-rate-detail"),
    path("production/salary/work-sessions/", ProductionWorkSessionListCreateAPIView.as_view(), name="production-salary-work-sessions"),
    path("production/salary/work-sessions/<uuid:pk>/", ProductionWorkSessionDetailAPIView.as_view(), name="production-salary-work-session-detail"),
    path("production/salary/accruals/", ProductionAccrualListAPIView.as_view(), name="production-salary-accruals"),
    path("production/salary/summary/", ProductionSalarySummaryAPIView.as_view(), name="production-salary-summary"),
    path("production/salary/payouts/", ProductionPayoutListCreateAPIView.as_view(), name="production-salary-payouts"),
    path("market-salary/payouts/", MarketSalaryPayoutListCreateAPIView.as_view(), name="market-salary-payouts"),
    path("market-salary/payouts/<uuid:pk>/", MarketSalaryPayoutDetailAPIView.as_view(), name="market-salary-payout-detail"),

    # Инвентаризация товаров (остаток Product.quantity)
    path("inventory/sessions/", ProductInventorySessionListCreateAPIView.as_view(), name="product-inventory-session-list"),
    path("inventory/sessions/<uuid:pk>/", ProductInventorySessionRetrieveAPIView.as_view(), name="product-inventory-session-detail"),
    path("inventory/sessions/<uuid:pk>/apply/", ProductInventorySessionApplyAPIView.as_view(), name="product-inventory-session-apply"),
    path("inventory/sessions/<uuid:pk>/cancel/", ProductInventorySessionCancelAPIView.as_view(), name="product-inventory-session-cancel"),
    path('products/barcode/<str:barcode>/', ProductByBarcodeAPIView.as_view(), name='product-by-barcode'),
    path('products/warehouse-barcode/<str:barcode>/', ProductWarehouseBarcodeAPIView.as_view(), name='product-warehouse-barcode'),
    path('products/global-barcode/<str:barcode>/', ProductByGlobalBarcodeAPIView.as_view(), name='product-by-barcode'),
    path('products/form-layout/', ProductFormLayoutAPIView.as_view(), name='product-form-layout'),
    
    #photo
    path("products/<uuid:product_id>/images/",
         ProductImageListCreateAPIView.as_view(), name="productimage-list-create"),
    path("products/<uuid:product_id>/images/<uuid:image_id>/",
         ProductImageRetrieveUpdateDestroyAPIView.as_view(), name="productimage-rud"),


    path('categories/', ProductCategoryListCreateAPIView.as_view(), name='category-list'),
    path('categories/<uuid:pk>/', ProductCategoryRetrieveUpdateDestroyAPIView.as_view(), name='category-detail'),

    path('brands/', ProductBrandListCreateAPIView.as_view(), name='brand-list'),
    path('brands/<uuid:pk>/', ProductBrandRetrieveUpdateDestroyAPIView.as_view(), name='brand-detail'),

    # 🔹 Склад и складские события
    path('warehouses/', WarehouseListCreateAPIView.as_view(), name='warehouse-list-create'),
    path('warehouses/<uuid:pk>/', WarehouseRetrieveUpdateDestroyAPIView.as_view(), name='warehouse-retrieve-update-destroy'),

    path('warehouse-events/', WarehouseEventListCreateAPIView.as_view(), name='warehouse-event-list-create'),
    path('warehouse-events/<uuid:pk>/', WarehouseEventRetrieveUpdateDestroyAPIView.as_view(), name='warehouse-event-retrieve-update-destroy'),

    # 🔹 Интеграции и аналитика
    path('integrations/', IntegrationListCreateAPIView.as_view(), name='integration-list-create'),
    path('integrations/<uuid:pk>/', IntegrationRetrieveUpdateDestroyAPIView.as_view(), name='integration-detail'),

    path('analytics/', AnalyticsListAPIView.as_view(), name='analytics-list'),
    path('analytics/dashboard/', OwnerDashboardAnalyticsAPIView.as_view(), name='analytics-dashboard'),
    path('analytics/dashboard/my/', MyDashboardAnalyticsAPIView.as_view(), name='analytics-dashboard-my'),
    path("analytics/cards/details/", AnalyticsCardDetailsAPIView.as_view(), name="analytics-card-details"),

    # 🔹 Отзывы
    path('reviews/', ReviewListCreateAPIView.as_view(), name='review-list-create'),
    path('reviews/<uuid:pk>/', ReviewRetrieveUpdateDestroyAPIView.as_view(), name='review-detail'),

    # 🔹 Уведомления
    path('notifications/', NotificationListView.as_view(), name='notification-list'),
    path('notifications/unread-count/', NotificationUnreadCountView.as_view(), name='notification-unread-count'),
    path('notifications/mark-all-read/', MarkAllNotificationsReadView.as_view(), name='mark-all-notifications-read'),
    # алиас: часть клиентов зовёт read-all/ (см. realtime-notifications-backend.md)
    path('notifications/read-all/', MarkAllNotificationsReadView.as_view(), name='notifications-read-all'),
    path('notifications/<uuid:pk>/read/', MarkNotificationReadView.as_view(), name='notification-read'),
    path('notifications/<uuid:pk>/', NotificationDetailView.as_view(), name='notification-detail'),
    path('pos/quick-slots/', POSQuickSlotsAPIView.as_view(), name='pos-quick-slots'),

    # 🔹 События
    path('events/', EventListCreateAPIView.as_view(), name='event-list-create'),
    path('events/<uuid:pk>/', EventRetrieveUpdateDestroyAPIView.as_view(), name='event-detail'),
    path('orders/analytics/', OrderAnalyticsView.as_view(), name='order-analytics'),
    
    # ТЗ Н-22: клиентские документы (КП, счёт, накладная)
    path("client-documents/", ClientDocumentListCreateAPIView.as_view(), name="client-documents"),
    path("client-documents/<uuid:pk>/", ClientDocumentDetailAPIView.as_view(), name="client-document-detail"),
    path("client-documents/<uuid:pk>/pay/", ClientDocumentPayAPIView.as_view(), name="client-document-pay"),
    path("clients/", ClientListCreateAPIView.as_view(), name="client-list"),
    path("clients/<uuid:pk>/", ClientRetrieveUpdateDestroyAPIView.as_view(), name="client-detail"),
    path(
        "clients/<uuid:client_id>/agent-analytics/",
        ClientAgentAnalyticsAPIView.as_view(),
        name="client-agent-analytics",
    ),
    path(
        "clients/<uuid:client_id>/kpis/",
        ClientKPIsAPIView.as_view(),
        name="client-kpis",
    ),
    path(
        "clients/<uuid:client_id>/deals/",
        ClientDealListCreateAPIView.as_view(),
        name="client-deals-list-create",
    ),
    path(
        "clients/<uuid:client_id>/deals/<uuid:pk>/",
        ClientDealRetrieveUpdateDestroyAPIView.as_view(),
        name="client-deals-detail",
    ),
    path(
        "clients/<uuid:client_id>/deals/pay-any/",
        ClientDealsPayAnyAPIView.as_view(),
        name="client-deals-pay-any",
    ),

    # installments reschedule / promised date
    path(
        "clients/<uuid:client_id>/deals/<uuid:deal_id>/installments/<uuid:installment_id>/",
        ClientDealInstallmentUpdateAPIView.as_view(),
        name="client-deal-installment-update",
    ),
    path(
        "clients/<uuid:client_id>/deals/<uuid:pk>/installments/<uuid:installment_id>/",
        ClientDealInstallmentUpdateAPIView.as_view(),
        name="client-deal-installment-update-pk",
    ),

    # pay/refund (nested)
    path(
        "clients/<uuid:client_id>/deals/<uuid:pk>/pay/",
        ClientDealPayAPIView.as_view(),
        name="client-deal-pay",
    ),
    path(
        "clients/<uuid:client_id>/deals/<uuid:pk>/refund/",
        ClientDealRefundAPIView.as_view(),
        name="client-deal-refund",
    ),

    # ✅ legacy alias: unpay -> refund (вернёт всё по последнему/указанному взносу)
    path(
        "clients/<uuid:client_id>/deals/<uuid:pk>/unpay/",
        ClientDealRefundAPIView.as_view(),
        name="client-deal-unpay",
    ),

    # ===== flat =====
    path("clientdeals/", ClientDealListCreateAPIView.as_view(), name="deal-list-create"),
    path("clientdeals/<uuid:pk>/", ClientDealRetrieveUpdateDestroyAPIView.as_view(), name="deal-detail"),

    path("clientdeals/<uuid:deal_id>/installments/<uuid:installment_id>/", ClientDealInstallmentUpdateAPIView.as_view(), name="clientdeal-installment-update"),
    path("deals/<uuid:deal_id>/installments/<uuid:installment_id>/", ClientDealInstallmentUpdateAPIView.as_view(), name="deal-installment-update"),

    path("clientdeals/<uuid:pk>/pay/", ClientDealPayAPIView.as_view(), name="deal-pay"),
    path("clientdeals/<uuid:pk>/refund/", ClientDealRefundAPIView.as_view(), name="deal-refund"),

    # ✅ legacy alias: unpay -> refund
    path("clientdeals/<uuid:pk>/unpay/", ClientDealRefundAPIView.as_view(), name="deal-unpay"),

    # ✅ если у тебя где-то уже был такой путь — оставь как алиас тоже
    path("deals/<uuid:pk>/unpay/", ClientDealRefundAPIView.as_view(), name="deal-unpay-legacy"),

    # ===== extra =====
    path("clients/with-debts/", ClientWithDebtsListAPIView.as_view(), name="clients-with-debts"),
    path("clients/debtors/", ClientDebtorsAPIView.as_view(), name="clients-debtors"),
    path("clients/<uuid:pk>/pay-debt/", ClientPayDebtAPIView.as_view(), name="client-pay-debt"),
    path("clients/<uuid:client_id>/pay-supplier-debt/", SupplierDebtPayAPIView.as_view(), name="client-pay-supplier-debt"),
    path("clients/<uuid:client_id>/debts/", ClientDirectDebtAPIView.as_view(), name="client-direct-debt"),
    path("clients/bonus/summary/", ClientBonusSummaryAPIView.as_view(), name="client-bonus-summary"),  # ТЗ 29.10 Б-99
    path("clients/<uuid:pk>/bonus/", ClientBonusAPIView.as_view(), name="client-bonus"),
    path("clients/<uuid:pk>/bonus/history/", ClientBonusHistoryAPIView.as_view(), name="client-bonus-history"),
    path(
        "clients/<uuid:client_id>/reconciliation/",
        ClientReconciliationClassicAPIView.as_view(),
        name="client-reconciliation",
    ),
    path("clients/<uuid:client_id>/reconciliation/json/", ClientReconciliationJSONAPIView.as_view()),
    path(
        "clients/<uuid:client_id>/subscription-schedule/",
        ClientSubscriptionScheduleAPIView.as_view(),
        name="client-subscription-schedule",
    ),
    
    path("pos/sales/", SaleListAPIView.as_view(), name="pos-sale-list"),
    path("pos/checkout/batch/", PosCheckoutBatchAPIView.as_view(), name="pos-checkout-batch"),
    path("pos/checkout/", PosQuickCheckoutAPIView.as_view(), name="pos-quick-checkout"),
    path("pos/returns/", SaleReturnListAPIView.as_view(), name="pos-return-list"),
    path("pos/sales/<uuid:pk>/public-link/", SalePublicLinkAPIView.as_view(), name="pos-sale-public-link"),  # ТЗ Н-32
    path("media/", MainMediaListCreateAPIView.as_view(), name="main-media"),  # ТЗ Н-32
    path("media/<uuid:pk>/", MainMediaDetailAPIView.as_view(), name="main-media-detail"),
    path("pos/sales/<uuid:pk>/exchange/", SaleExchangeAPIView.as_view(), name="pos-sale-exchange"),
    path("products/<uuid:pk>/variants/", ProductVariantListCreateAPIView.as_view(), name="product-variants"),
    path("products/<uuid:pk>/variants/<uuid:vid>/", ProductVariantDetailAPIView.as_view(), name="product-variant-detail"),
    path("products/<uuid:pk>/stocks/", ProductStocksAPIView.as_view(), name="product-stocks"),
    path("stock-transfers/", StockTransferListCreateAPIView.as_view(), name="stock-transfers"),
    path("branch-transfers/", BranchTransferListCreateAPIView.as_view(), name="branch-transfers-list-create"),
    path("branch-transfers/<uuid:pk>/", BranchTransferDetailAPIView.as_view(), name="branch-transfers-detail"),
    path("branch-transfers/<uuid:pk>/cancel/", BranchTransferCancelAPIView.as_view(), name="branch-transfers-cancel"),
    path("appointments/", AppointmentListCreateAPIView.as_view(), name="market-appointments"),
    path("appointments/<uuid:pk>/", AppointmentDetailAPIView.as_view(), name="market-appointment-detail"),
    path("appointments/<uuid:pk>/to-sale/", AppointmentToSaleAPIView.as_view(), name="market-appointment-to-sale"),
    path("work-orders/", WorkOrderListCreateAPIView.as_view(), name="work-orders"),
    path("work-orders/<uuid:pk>/", WorkOrderDetailAPIView.as_view(), name="work-order-detail"),
    path("work-orders/<uuid:pk>/issue/", WorkOrderIssueAPIView.as_view(), name="work-order-issue"),
    path("pos/sale-consultants/", SaleConsultantsAPIView.as_view(), name="pos-sale-consultants"),
    path("pos/cashier-settings/", MarketCashierSettingsAPIView.as_view(), name="pos-cashier-settings"),
    path("pos/cashier-settings/verify-delete-code/", VerifyDeleteCodeAPIView.as_view(), name="pos-verify-delete-code"),
    path("pos/customer-display-settings/", MarketCustomerDisplaySettingsAPIView.as_view(), name="pos-customer-display-settings"),
    path("pos/sales/start/", SaleStartAPIView.as_view(), name="pos-sale-start"),
    path("pos/carts/<uuid:pk>/custom-item/", SaleAddCustomItemAPIView.as_view(), name="pos-cart-add-custom-item"),
    # ТЗ Н-15: быстрые операции над корзиной
    path("pos/sales/<uuid:pk>/items/bulk/", SaleItemsBulkAPIView.as_view(), name="pos-sale-items-bulk"),
    path("pos/carts/<uuid:pk>/clear/", CartClearAPIView.as_view(), name="pos-cart-clear"),
    path("pos/carts/<uuid:pk>/", CartDetailAPIView.as_view(), name="pos-cart-detail"),
    path("sales/bulk-delete/", SaleBulkDeleteAPIView.as_view(), name="sale-bulk-delete"),
    path("pos/sales/<uuid:pk>/", SaleRetrieveAPIView.as_view(), name="pos-sale-detail"),
    path("pos/sales/<uuid:pk>/scan/", SaleScanAPIView.as_view(), name="pos-sale-scan"),
    path("pos/sales/<uuid:pk>/add-item/", SaleAddItemAPIView.as_view(), name="pos-sale-add-item"),
    path("pos/sales/<uuid:pk>/checkout/", SaleCheckoutAPIView.as_view(), name="pos-sale-checkout"),
    path("pos/sales/<uuid:pk>/pay-debt/", SalePayDebtAPIView.as_view(), name="pos-sale-pay-debt"),
    path("pos/sales/<uuid:pk>/return/", SaleReturnAPIView.as_view(), name="pos-sale-return"),
    path("pos/sales/<uuid:pk>/mobile-scanner/", SaleMobileScannerTokenAPIView.as_view(), name="pos-sale-mobile-scanner"),
    path(
        "pos/carts/<uuid:cart_id>/items/<uuid:item_id>/",
        CartItemUpdateDestroyAPIView.as_view(),
        name="pos-cart-item-update-destroy",
    ),
    path(
        "pos/cart-item-deletions/",
        CartItemDeletionLogListAPIView.as_view(),
        name="pos-cart-item-deletions",
    ),
    path("pos/printer-settings/", PosPrinterSettingAPIView.as_view(), name="pos-printer-settings"),
    path("pos/sales/<uuid:pk>/receipt/", SaleReceiptDataAPIView.as_view(), name="sale-receipt-download"),
    path("sales/<uuid:pk>/invoice/", SaleInvoiceDownloadAPIView.as_view()),
    
    path("sales/json/<uuid:pk>/receipt/", SaleReceiptAPIView.as_view(), name="sale-receipt"),
    path("sales/json/<uuid:pk>/invoice/", SaleInvoiceAPIView.as_view(), name="sale-invoice"),
    
    path("transactions/", TransactionRecordListCreateView.as_view(), name="transaction-list"),
    path("transactions/<uuid:pk>/", TransactionRecordRetrieveUpdateDestroyView.as_view(), name="transaction-detail"),
    
    path("contractor-works/", ContractorWorkListCreateAPIView.as_view(), name="contractorwork-list"),
    path("contractor-works/<uuid:pk>/", ContractorWorkRetrieveUpdateDestroyAPIView.as_view(), name="contractorwork-detail"),

    # nested по отделу (если нужно)
    path("departments/<uuid:department_id>/contractor-works/", ContractorWorkListCreateAPIView.as_view(), name="department-contractorwork-list"),
    path("departments/<uuid:department_id>/contractor-works/<uuid:pk>/", ContractorWorkRetrieveUpdateDestroyAPIView.as_view(), name="department-contractorwork-detail"),
    
    path("debts/", DebtListCreateAPIView.as_view(), name="debt-list"),
    path("debts/<uuid:pk>/", DebtRetrieveUpdateDestroyAPIView.as_view(), name="debt-detail"),
    path("debts/<uuid:pk>/pay/", DebtPayAPIView.as_view(), name="debt-pay"),
    path("debts/<uuid:pk>/payments/", DebtPaymentListAPIView.as_view(), name="debt-payments"),
    path("cashflows/", CashFlowListCreateView.as_view(), name="main-cashflows-alias"),
    
    path("object-items/", ObjectItemListCreateAPIView.as_view()),
    path("object-items/<uuid:pk>/", ObjectItemRetrieveUpdateDestroyAPIView.as_view()),

    path("object-sales/", ObjectSaleListCreateAPIView.as_view()),
    path("object-sales/<uuid:pk>/", ObjectSaleRetrieveUpdateDestroyAPIView.as_view()),
    path("object-sales/<uuid:sale_id>/items/", ObjectSaleAddItemAPIView.as_view()),
    
    path("items-make/", ItemListCreateAPIView.as_view(), name="item-make-list-create"),
    path("items-make/<uuid:pk>/process/", ItemMakeProcessAPIView.as_view(), name="item-make-process"),
    path("items-make/<uuid:pk>/purchase/", ItemMakePurchaseAPIView.as_view(), name="item-make-purchase"),
    path("items-make/<uuid:pk>/", ItemRetrieveUpdateDestroyAPIView.as_view(), name="item-make-detail"),

    # -------------------------
    # Поставщики -> товары -> оприходование
    # -------------------------
    path("suppliers/payables/", SupplierPayablesAPIView.as_view(), name="supplier-payables"),
    # ТЗ Н-08: исход операции по ключу повтора
    path("idempotency/<str:key>/", IdempotencyLookupAPIView.as_view(), name="idempotency-lookup"),
    path("purchase-orders/", PurchaseOrderListCreateAPIView.as_view(), name="purchase-orders"),  # ТЗ Н-29
    path("purchase-orders/<uuid:pk>/", PurchaseOrderDetailAPIView.as_view(), name="purchase-order-detail"),
    path("suppliers/", SupplierListAPIView.as_view(), name="supplier-list"),
    path("suppliers/receipts/", SupplierReceiptListAPIView.as_view(), name="supplier-receipts"),
    path("suppliers/receipts/<uuid:pk>/pdf/", SupplierReceiptPdfAPIView.as_view(), name="supplier-receipt-pdf"),  # ТЗ Н-33
    path("suppliers/receipts/<uuid:pk>/", SupplierReceiptRetrieveAPIView.as_view(), name="supplier-receipt-detail"),
    path("suppliers/returns/", SupplierReturnListAPIView.as_view(), name="supplier-returns-list"),
    path("suppliers/returns/<uuid:pk>/", SupplierReturnRetrieveAPIView.as_view(), name="supplier-returns-detail"),
    path("suppliers/<uuid:supplier_id>/returns/", SupplierReturnCreateAPIView.as_view(), name="supplier-returns-create"),
    path("supplier-returns/", SupplierReturnListAPIView.as_view(), name="supplier-returns-list-alias"),
    path("supplier-returns/<uuid:pk>/", SupplierReturnRetrieveAPIView.as_view(), name="supplier-returns-detail-alias"),
    path("suppliers/<uuid:supplier_id>/products/", SupplierProductsListAPIView.as_view(), name="supplier-products"),
    path("suppliers/<uuid:supplier_id>/purchases/", SupplierPurchasesListAPIView.as_view(), name="supplier-purchases"),
    path("suppliers/<uuid:supplier_id>/receipt/", SupplierReceiptAPIView.as_view(), name="supplier-receipt"),
    path("suppliers/<uuid:supplier_id>/recommendations/", SupplierRecommendationsAPIView.as_view(), name="supplier-recommendations"),
    
    path("subreals/", ManufactureSubrealListCreateAPIView.as_view(), name="subreal-list-create"),
    path("subreals/<uuid:pk>/", ManufactureSubrealRetrieveUpdateDestroyAPIView.as_view(), name="subreal-detail"),
    path("subreals/bulk/", ManufactureSubrealBulkCreateAPIView.as_view(), name="subreal-bulk-create"),

    # -------------------------
    # Приёмы
    # -------------------------
    path("acceptances/", AcceptanceListCreateAPIView.as_view(), name="acceptance-list-create"),
    path("acceptances/<uuid:pk>/", AcceptanceRetrieveDestroyAPIView.as_view(), name="acceptance-detail"),

    # -------------------------
    # Возвраты
    # -------------------------
    path("returns/", ReturnFromAgentListCreateAPIView.as_view(), name="return-list-create"),
    path("returns/<uuid:pk>/", ReturnFromAgentRetrieveDestroyAPIView.as_view(), name="return-detail"),
    path("returns/<uuid:pk>/approve/", ReturnFromAgentApproveAPIView.as_view(), name="return-approve"),
    path("returns/approve-bulk/", ReturnFromAgentBulkApproveAPIView.as_view(), name="return-approve-bulk"),
    path("returns/<uuid:pk>/reject/", ReturnFromAgentRejectAPIView.as_view(), name="return-reject"),

    # -------------------------
    # Агент: свои товары и возвраты
    # -------------------------
    path("agents/me/products/", AgentMyProductsListAPIView.as_view(), name="agent-my-products"),
    path("agents/me/returns/", AgentMyReturnsListCreateAPIView.as_view(), name="agent-my-returns"),
    path("agents/me/subreals/", AgentMySubrealsListAPIView.as_view(), name="agent-my-subreals"),
    path(
        "owners/agents/products/",
        OwnerAgentsProductsListAPIView.as_view(),
        name="owner-agents-products-list",
    ),
    path("agents/me/cart/start/", AgentCartStartAPIView.as_view()),
    path("agents/me/carts/<uuid:pk>/", CartDetailAPIView.as_view(), name="agent-cart-detail"),
    path("agents/me/carts/<uuid:pk>/scan/", AgentSaleScanAPIView.as_view()),
    path("agents/me/carts/<uuid:pk>/add-item/", AgentSaleAddItemAPIView.as_view()),
    path("agents/me/carts/<uuid:pk>/custom-item/", AgentSaleAddCustomItemAPIView.as_view()),
    path("agents/me/carts/<uuid:pk>/checkout/", AgentSaleCheckoutAPIView.as_view()),
    path("agents/me/sales/", AgentMySalesListAPIView.as_view(), name="agent-my-sales-list"),
    path("agents/me/sales/<uuid:pk>/", AgentMySaleRetrieveAPIView.as_view(), name="agent-my-sale-detail"),
    path("agents/me/sales/<uuid:pk>/return/", AgentSaleReturnAPIView.as_view(), name="agent-sale-return"),
    path("agents/me/carts/<uuid:cart_id>/items/<uuid:item_id>/", AgentCartItemUpdateDestroyAPIView.as_view(),name="agent-cart-item-update-destroy", ),
    
    
    path("agent-carts/", AgentRequestCartListCreateAPIView.as_view()),
    path("agent-carts/<uuid:pk>/", AgentRequestCartRetrieveUpdateDestroyAPIView.as_view()),
    path("agent-carts/<uuid:pk>/submit/", AgentRequestCartSubmitAPIView.as_view()),
    path("agent-carts/<uuid:pk>/approve/", AgentRequestCartApproveAPIView.as_view()),
    path("agent-carts/<uuid:pk>/reject/", AgentRequestCartRejectAPIView.as_view()),

    path("agent-cart-items/", AgentRequestItemListCreateAPIView.as_view()),
    path("agent-cart-items/<uuid:pk>/", AgentRequestItemRetrieveUpdateDestroyAPIView.as_view()),
    
    path("agents/me/products/", AgentMyProductsListAPIView.as_view(), name="agent-my-products"),

    path("agents/me/analytics/", AgentMyAnalyticsAPIView.as_view(), name="agent-my-analytics"),
    path("owners/agents/<uuid:agent_id>/analytics/", OwnerAgentAnalyticsAPIView.as_view(), name="owner-agent-analytics",),
    path("owners/analytics/", OwnerOverallAnalyticsAPIView.as_view(), name="owner-overall-analytics"),
    
    
    path("analytics/market/summary/", AnalyticsMarketSummaryAPIView.as_view(), name="analytics-market-summary"),
    path("analytics/market/", AnalyticsView.as_view(), name="analytics"),
    path("analytics/reconcile/", AnalyticsReconcileAPIView.as_view(), name="analytics-reconcile"),
    path("analytics/cashflow/", AnalyticsCashFlowAPIView.as_view(), name="analytics-cashflow"),
    path("analytics/loss-sales/", AnalyticsLossSalesAPIView.as_view(), name="analytics-loss-sales"),
    path("analytics/finance/", AnalyticsFinanceAPIView.as_view(), name="analytics-finance"),
    path("analytics/pnl/", AnalyticsPnLAPIView.as_view(), name="analytics-pnl"),
    path("analytics/stock/", AnalyticsStockAPIView.as_view(), name="analytics-stock"),
    path("analytics/market/reconcile/", AnalyticsReconcileAPIView.as_view(), name="analytics-market-reconcile"),
    path("analytics/market/cashflow/", AnalyticsCashFlowAPIView.as_view(), name="analytics-market-cashflow"),
    path("analytics/market/pnl/", AnalyticsPnLAPIView.as_view(), name="analytics-market-pnl"),
    path("analytics/market/stock/", AnalyticsStockAPIView.as_view(), name="analytics-market-stock"),
    path("reconcile/", AnalyticsReconcileAPIView.as_view(), name="direct-reconcile"),
    path("cashflow/", AnalyticsCashFlowAPIView.as_view(), name="direct-cashflow"),
    path("pnl/", AnalyticsPnLAPIView.as_view(), name="direct-pnl"),
    path("stock/", AnalyticsStockAPIView.as_view(), name="direct-stock"),
    path(
        "market-sale-employee-pay-profiles/",
        MarketSaleEmployeePayProfileListCreateAPIView.as_view(),
        name="market-sale-employee-pay-profile-list",
    ),
    path(
        "market-sale-employee-pay-profiles/<uuid:pk>/",
        MarketSaleEmployeePayProfileRetrieveUpdateDestroyAPIView.as_view(),
        name="market-sale-employee-pay-profile-detail",
    ),

    path("public/companies/<slug:slug>/", PublicCompanyAPIView.as_view()),
    path("public/companies/<slug:slug>/showcase/", PublicCompanyShowcaseAPIView.as_view()),
    path("public/companies/<slug:slug>/showcase/<uuid:product_id>/notify/", PublicCompanyShowcaseNotifyAPIView.as_view(), name="public-company-showcase-notify"),
    path("public/companies/<slug:slug>/showcase/facets/", PublicCompanyShowcaseFacetsAPIView.as_view(), name="public-company-showcase-facets"),
    path("public/companies/<slug:slug>/showcase/<uuid:product_id>/", PublicCompanyProductDetailAPIView.as_view()),
    path("public/companies/<slug:slug>/showcase/design/", PublicCompanyShowcaseDesignAPIView.as_view(), name="public-company-showcase-design"),
    path("public/companies/<slug:slug>/orders/", PublicCompanyShowcaseOrderCreateAPIView.as_view(), name="public-company-showcase-orders"),
    path("public/companies/<slug:slug>/showcase/track/", PublicCompanyShowcaseTrackAPIView.as_view(), name="public-company-showcase-track"),
    path("public/companies/<slug:slug>/showcase/events/", PublicCompanyShowcaseEventsAPIView.as_view(), name="public-company-showcase-events"),
    path("public/companies/<slug:slug>/showcase/categories/", PublicCompanyShowcaseCategoriesAPIView.as_view(), name="public-company-showcase-categories"),
    path("public/companies/<slug:slug>/showcase/pages/<slug:page_slug>/", PublicCompanyShowcasePageAPIView.as_view(), name="public-company-showcase-page"),
    path("public/companies/<slug:slug>/showcase/cart/quote/", PublicCompanyShowcaseCartQuoteAPIView.as_view(), name="public-company-showcase-cart-quote"),
    
    path("public/knowledge-base/", PublicKnowledgeBaseCourseListCreateAPIView.as_view()),
    path("public/knowledge-base/<uuid:course_id>/", PublicKnowledgeBaseCourseRetrieveAPIView.as_view()),

    # ==========================
    # Showcase Visual Editor & Management (SC-01 - SC-11)
    # ==========================
    path("showcase/editor/options/", ShowcaseEditorOptionsAPIView.as_view(), name="showcase-editor-options"),
    path("showcase/design/", ShowcaseDesignAPIView.as_view(), name="showcase-design"),
    path("showcase/design/draft/", ShowcaseDraftUpdateAPIView.as_view(), name="showcase-design-draft"),
    path("showcase/design/draft/reset/", ShowcaseDraftResetAPIView.as_view(), name="showcase-design-draft-reset"),
    path("showcase/design/draft/apply-preset/", ShowcaseDraftApplyPresetAPIView.as_view(), name="showcase-design-draft-apply-preset"),
    path("showcase/design/draft/product-order/", ShowcaseDraftProductOrderAPIView.as_view(), name="showcase-design-product-order"),
    path("showcase/design/preview-link/", ShowcasePreviewLinkAPIView.as_view(), name="showcase-design-preview-link"),
    path("showcase/design/publish/", ShowcasePublishAPIView.as_view(), name="showcase-design-publish"),
    path("showcase/design/discard/", ShowcaseDiscardAPIView.as_view(), name="showcase-design-discard"),
    path("showcase/design/versions/", ShowcaseVersionsListAPIView.as_view(), name="showcase-design-versions"),
    path("showcase/design/versions/<int:version>/restore/", ShowcaseVersionRestoreAPIView.as_view(), name="showcase-design-restore"),

    path("showcase/size-charts/", SizeChartListCreateAPIView.as_view(), name="showcase-size-charts"),  # ТЗ Н-33
    path("showcase/size-charts/<uuid:pk>/", SizeChartDetailAPIView.as_view(), name="showcase-size-chart-detail"),
    path("showcase/media/", ShowcaseMediaListCreateAPIView.as_view(), name="showcase-media-list-create"),
    path("showcase/media/<uuid:pk>/", ShowcaseMediaDetailAPIView.as_view(), name="showcase-media-detail"),

    path("showcase/banners/", ShowcaseBannerListCreateAPIView.as_view(), name="showcase-banners-list-create"),
    path("showcase/banners/reorder/", ShowcaseBannerReorderAPIView.as_view(), name="showcase-banners-reorder"),
    path("showcase/banners/<uuid:pk>/", ShowcaseBannerDetailAPIView.as_view(), name="showcase-banners-detail"),

    path("showcase/promo-blocks/", ShowcasePromoBlockListCreateAPIView.as_view(), name="showcase-promo-blocks-list-create"),
    path("showcase/promo-blocks/reorder/", ShowcasePromoBlockReorderAPIView.as_view(), name="showcase-promo-blocks-reorder"),
    path("showcase/promo-blocks/<uuid:pk>/", ShowcasePromoBlockDetailAPIView.as_view(), name="showcase-promo-blocks-detail"),

    path("showcase/products/", ShowcaseProductsListAPIView.as_view(), name="showcase-products-list"),
    path("showcase/products/order/", ShowcaseProductsOrderAPIView.as_view(), name="showcase-products-order"),
    path("showcase/products/bulk/", ShowcaseProductsBulkAPIView.as_view(), name="showcase-products-bulk"),
    path("showcase/products/<uuid:product_id>/", ShowcaseProductDetailAPIView.as_view(), name="showcase-products-detail"),

    path("showcase/categories/", ShowcaseCategoriesListAPIView.as_view(), name="showcase-categories-list"),
    path("showcase/categories/order/", ShowcaseCategoriesOrderAPIView.as_view(), name="showcase-categories-order"),
    path("showcase/categories/<uuid:category_id>/", ShowcaseCategoryDetailAPIView.as_view(), name="showcase-categories-detail"),

    path("showcase/pages/", ShowcasePageListCreateAPIView.as_view(), name="showcase-pages-list-create"),
    path("showcase/pages/<uuid:pk>/", ShowcasePageDetailAPIView.as_view(), name="showcase-pages-detail"),

    path("showcase/slug-check/", ShowcaseSlugCheckAPIView.as_view(), name="showcase-slug-check"),

    path("showcase/stock-requests/", ShowcaseStockRequestListAPIView.as_view(), name="showcase-stock-requests"),
    path("showcase/orders/", ShowcaseOrderListAPIView.as_view(), name="showcase-orders-list"),
    path("showcase/orders/<uuid:pk>/", ShowcaseOrderDetailAPIView.as_view(), name="showcase-orders-detail"),

    path("showcase/stats/", ShowcaseStatsAPIView.as_view(), name="showcase-stats"),

    # ==========================
    # Recommendations / Upsell & Sales Targets (TZ-BE-2026-07 / Часть 7)
    # ==========================
    path("recommendations/events/", RecommendationEventsAPIView.as_view(), name="recommendation-events"),
    path("recommendations/events/link-sale/", RecommendationLinkSaleAPIView.as_view(), name="recommendation-link-sale"),
    path("recommendations/stats/", RecommendationStatsAPIView.as_view(), name="recommendation-stats"),
    path("recommendations/pairs/", RecommendationPairsAPIView.as_view(), name="recommendation-pairs"),
    path("sales-targets/", SalesTargetAPIView.as_view(), name="sales-targets"),

    # ==========================
    # Telegram Bot & AI (TZ-BE-2026-06 / Часть 5)
    # ==========================
    # ТЗ Н-24: голосовой агент и ИИ
    path("products/search/", ProductVoiceSearchAPIView.as_view(), name="product-voice-search"),
    path("orders/draft/", OrderDraftAPIView.as_view(), name="order-draft"),
    path("orders/draft/status/", OrderDraftStatusAPIView.as_view(), name="order-draft-status"),
    path("voice/calls/", VoiceCallAPIView.as_view(), name="voice-calls"),
    path("bot/notify-owner/", BotNotifyOwnerView.as_view(), name="bot-notify-owner"),  # ТЗ Н-23
    path("analytics/startup-payback/", StartupPaybackAPIView.as_view(), name="analytics-startup-payback"),  # ТЗ 29.10 Б-100
    path("investors/", InvestorListCreateView.as_view(), name="investors"),
    path("investors/<uuid:pk>/", InvestorDetailView.as_view(), name="investor-detail"),
    path("investor-payouts/", PayoutListCreateView.as_view(), name="investor-payouts"),
    path("investor-payouts/<uuid:pk>/", PayoutDetailView.as_view(), name="investor-payout-detail"),
    path("global-products/export/", GlobalProductExportView.as_view(), name="global-products-export"),  # ТЗ 29.10 Б-97
    path("app-tests/checks/", AppTestChecksView.as_view(), name="app-tests-checks"),  # ТЗ 29.10 Б-98
    path("telegram-bot/", include("apps.main.telegram_bot.urls")),
    path("telegram/", include("apps.main.telegram_bot.urls")),
]
