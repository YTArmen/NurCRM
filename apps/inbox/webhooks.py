"""Единый вебхук POST/GET api/inbox/webhooks/{channel_uuid}/ (Б-76).

Подпись проверяется ДО разбора тела; неверная → 401 и событие в журнал (signature_ok=false).
Тело кладём в WebhookEvent, обработка — воркером (Celery); отвечаем сразу.
"""
import hashlib
import hmac
import json
import logging

from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from rest_framework import permissions
from rest_framework.views import APIView

from . import crypto, inbound
from .models import Channel, WebhookEvent

logger = logging.getLogger("inbox.webhook")


def _verify_telegram(channel, request, raw) -> bool:
    sent = request.META.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
    secret = channel.webhook_secret
    return bool(secret) and hmac.compare_digest(sent.encode(), secret.encode())


def _verify_meta(channel, request, raw) -> bool:
    header = request.META.get("HTTP_X_HUB_SIGNATURE_256", "")
    app_secret = crypto.get_secrets(channel).get("app_secret", "")
    if not app_secret:  # каналы Embedded Signup / Instagram Login подписываются секретом приложения Meta
        from . import meta_channels
        app_secret = (meta_channels.instagram_app_secret() if channel.kind == "instagram"
                      else meta_channels.meta_app_secret())
    if not (app_secret and header.startswith("sha256=")):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


def _verifier(channel):
    if channel.kind.startswith("telegram"):
        return _verify_telegram
    if channel.provider == Channel.Provider.META:
        return _verify_meta
    return None  # Wazzup/360dialog — их схема подписи будет на этапе WhatsApp


class WebhookView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = []

    def get(self, request, channel_uuid):
        channel = Channel.objects.filter(pk=channel_uuid).first()
        if not channel:
            return JsonResponse({"detail": "Канал не найден.", "code": "not_found"}, status=404)
        # верификация Meta: hub.mode=subscribe & hub.verify_token == секрет канала
        if request.GET.get("hub.mode") == "subscribe":
            token = request.GET.get("hub.verify_token", "")
            if channel.webhook_secret and hmac.compare_digest(token.encode(), channel.webhook_secret.encode()):
                return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
            return JsonResponse({"detail": "Неверный verify_token.", "code": "signature_invalid"}, status=403)
        return JsonResponse({"ok": True, "kind": channel.kind})

    def post(self, request, channel_uuid):
        channel = Channel.objects.filter(pk=channel_uuid).select_related("bot_settings").first()
        if not channel:
            return JsonResponse({"detail": "Канал не найден.", "code": "not_found"}, status=404)
        raw = request.body  # до разбора тела
        verify = _verifier(channel)
        if verify is None:
            return JsonResponse({"detail": "Приём вебхуков этого провайдера пока не поддержан.",
                                 "code": "provider_not_supported"}, status=501)
        if not verify(channel, request, raw):
            WebhookEvent.objects.create(channel=channel, signature_ok=False, payload={},
                                        error="Неверная подпись вебхука")
            return JsonResponse({"detail": "Неверная подпись.", "code": "signature_invalid"}, status=401)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            WebhookEvent.objects.create(channel=channel, signature_ok=True, payload={}, error="Тело не JSON",
                                        processed_at=timezone.now())
            return JsonResponse({"detail": "Тело запроса не JSON.", "code": "invalid"}, status=400)
        event_id = inbound.telegram_event_id(payload) if channel.kind.startswith("telegram") else ""
        try:
            with transaction.atomic():
                event = WebhookEvent.objects.create(channel=channel, provider_event_id=event_id, payload=payload)
        except IntegrityError:
            return JsonResponse({"ok": True, "duplicate": True})
        try:
            from .tasks import process_webhook_event
            process_webhook_event.delay(event.id)
        except Exception:
            logger.exception("inbox webhook: очередь недоступна, событие %s подберёт sweep", event.id)
        return JsonResponse({"ok": True})
