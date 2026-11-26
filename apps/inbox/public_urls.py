# Адреса относительно api/inbox/ (вебхуки, без входа; подлинность — по подписи)
from django.urls import path

from . import views_meta as vm
from . import views_widget as vw
from .webhooks import WebhookView

urlpatterns = [
    path("webhooks/<uuid:channel_uuid>/", WebhookView.as_view(), name="inbox-webhook"),
    # общие вебхуки приложений Meta (Б-85, Б-86): подпись — секрет приложения, канал ищется по номеру/аккаунту
    path("meta/whatsapp/", vm.WaAppWebhookView.as_view(), name="inbox-meta-wa-webhook"),
    path("meta/instagram/", vm.IgAppWebhookView.as_view(), name="inbox-meta-ig-webhook"),
]

# чат и помощник витрины (Б-83, Б-88): api/inbox/public/companies/{slug}/chat/... и то же под api/main/ (см. showcase_urls.py)
chat_patterns = [
    path("public/companies/<slug:slug>/chat/config/", vw.PublicConfigView.as_view(), name="widget-config"),
    path("public/companies/<slug:slug>/chat/sessions/", vw.PublicSessionCreateView.as_view(), name="widget-sessions"),
    path("public/companies/<slug:slug>/chat/sessions/<uuid:sid>/messages/", vw.PublicMessagesView.as_view(), name="widget-messages"),
    path("public/companies/<slug:slug>/chat/sessions/<uuid:sid>/contact/", vw.PublicContactView.as_view(), name="widget-contact"),
    path("public/companies/<slug:slug>/chat/sessions/<uuid:sid>/telegram-link/", vw.PublicTelegramLinkView.as_view(), name="widget-tg"),
    path("public/companies/<slug:slug>/chat/sessions/<uuid:sid>/", vw.PublicSessionView.as_view(), name="widget-session"),
    path("public/companies/<slug:slug>/chat/sessions/<uuid:sid>/consent/", vw.PublicConsentView.as_view(), name="widget-consent"),
    path("public/companies/<slug:slug>/chat/sessions/<uuid:sid>/assistant/", vw.PublicAssistantView.as_view(), name="widget-assistant"),
]

urlpatterns += chat_patterns
