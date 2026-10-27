from django.urls import path

from apps.main.tz26.ai_stock.views import AiActionsView, AiSettingsView

urlpatterns = [
    path("ai-actions/settings/", AiSettingsView.as_view(), name="ai-actions-settings"),
    path("ai-actions/", AiActionsView.as_view(), name="ai-actions"),
]
