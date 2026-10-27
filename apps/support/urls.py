from django.urls import path
from apps.support.forgot import PasswordResetRequestAPIView
from apps.support.views import (
    ErrorReportListCreateAPIView,
    ErrorReportAttachmentAPIView,
    SupportBotWebhookAPIView,
)

urlpatterns = [
    path("error-reports/", ErrorReportListCreateAPIView.as_view(), name="support-error-reports"),
    path("error-reports/attachment/", ErrorReportAttachmentAPIView.as_view(), name="support-error-reports-attachment"),
    path("password-reset-requests/", PasswordResetRequestAPIView.as_view(), name="support-password-reset-requests"),
    path("bot/webhook/", SupportBotWebhookAPIView.as_view(), name="support-bot-webhook"),
]
