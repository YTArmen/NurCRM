# Адреса относительно api/inbox/ (вебхуки, без входа; подлинность — по подписи)
from django.urls import path

from .webhooks import WebhookView

urlpatterns = [
    path("webhooks/<uuid:channel_uuid>/", WebhookView.as_view(), name="inbox-webhook"),
]
