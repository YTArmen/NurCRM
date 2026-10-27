from django.urls import path

from apps.main.tz26.ai_stock.views import AiActionsView, AiSettingsView
from apps.main.tz26.ai_stock.stock_health import StockHealthView

urlpatterns = [
    path("analytics/stock-health/", StockHealthView.as_view(), name="analytics-stock-health"),
    path("ai-actions/settings/", AiSettingsView.as_view(), name="ai-actions-settings"),
    path("ai-actions/", AiActionsView.as_view(), name="ai-actions"),
]
