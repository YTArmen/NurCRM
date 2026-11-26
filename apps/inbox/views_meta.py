"""Б-85/Б-86: подключение WhatsApp (Embedded Signup) и Instagram (OAuth); общий вебхук приложения Meta."""
import hashlib
import hmac
import json
import logging

from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access, channels as ch_svc, meta_channels as mc
from .access import InboxPermission
from .models import WebhookEvent

logger = logging.getLogger("inbox.webhook")


class WaStartView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request):
        company = access.require(request.user, "can_manage_channels")
        return Response(mc.whatsapp_start(request.user, company))


class WaCallbackView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request):
        company = access.require(request.user, "can_manage_channels")
        d = request.data if isinstance(request.data, dict) else {}
        ch = mc.whatsapp_callback(request.user, company, d.get("code"), d.get("state"),
                                  str(d.get("waba_id") or ""), str(d.get("phone_number_id") or ""))
        return Response(ch_svc.serialize(ch), status=status.HTTP_201_CREATED)


class IgStartView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request):
        company = access.require(request.user, "can_manage_channels")
        return Response(mc.instagram_start(request.user, company))

    get = post


class IgCallbackView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request):
        company = access.require(request.user, "can_manage_channels")
        d = request.data if isinstance(request.data, dict) else {}
        ch = mc.instagram_callback(request.user, company, d.get("code"), d.get("state"))
        return Response(ch_svc.serialize(ch), status=status.HTTP_201_CREATED)


def _app_webhook(kind, secret_fn, verify_env):
    class View(APIView):
        permission_classes = [permissions.AllowAny]
        authentication_classes = []
        throttle_classes = []

        def get(self, request):
            token = mc._env(verify_env)
            if (token and request.GET.get("hub.mode") == "subscribe"
                    and hmac.compare_digest(request.GET.get("hub.verify_token", "").encode(), token.encode())):
                return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
            return JsonResponse({"detail": "Неверный verify_token.", "code": "signature_invalid"}, status=403)

        def post(self, request):
            secret = secret_fn()
            raw = request.body
            header = request.META.get("HTTP_X_HUB_SIGNATURE_256", "")
            expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest() if secret else ""
            if not secret or not hmac.compare_digest(header, expected):
                return JsonResponse({"detail": "Неверная подпись.", "code": "signature_invalid"}, status=401)
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return JsonResponse({"detail": "Тело запроса не JSON.", "code": "invalid"}, status=400)
            from .tasks import process_webhook_event
            for ch, part in mc.route_channel(payload, kind):
                eid = hashlib.sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()[:40]
                try:
                    ev = WebhookEvent.objects.create(channel=ch, provider_event_id=eid, payload=part)
                except IntegrityError:
                    continue
                try:
                    process_webhook_event.delay(ev.id)
                except Exception:
                    logger.exception("meta webhook: очередь недоступна, подберёт sweep")
            return JsonResponse({"ok": True})
    return View


WaAppWebhookView = _app_webhook("whatsapp", mc.meta_app_secret, "META_VERIFY_TOKEN")
IgAppWebhookView = _app_webhook("instagram", mc.instagram_app_secret, "INSTAGRAM_VERIFY_TOKEN")
