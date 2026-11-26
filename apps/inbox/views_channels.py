"""Б-73: каналы, capabilities, права ящика."""
from django.db import transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from core.exceptions import ApiError

from . import access, channels as ch_svc
from .access import InboxPermission
from .models import Channel, InboxAccess


def _get_channel(request, pk, flag="can_view_inbox"):
    company = access.require(request.user, flag)
    ch = Channel.objects.filter(company=company, pk=pk).select_related("bot_settings", "risk_accepted_by").first()
    if not ch:
        raise ApiError("not_found", "Канал не найден.", status_code=404)
    return company, ch


class CapabilitiesView(APIView):
    """GET api/main/inbox/capabilities/ — по нему касса включает разделы."""
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.company_of(request.user)
        kinds = ch_svc.allowed_kinds(company) if company else []
        ai = bool(company) and access.has(request.user, "can_use_ai_inbox")
        return Response({
            "inbox": bool(company) and access.has(request.user, "can_view_inbox"),
            "kinds": kinds,
            "implemented_kinds": [k for k in ch_svc.IMPLEMENTED_KINDS],
            "ai": True,
            "whatsapp_embedded": _meta_on(),
            "instagram_oauth": _ig_on(),
            "ai_allowed": ai,
            "templates": True,
            "widget": True,
            "permissions": access.perms_of(request.user),
        })


class MyAccessView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request):
        return Response(access.perms_of(request.user))


class UserAccessView(APIView):
    """GET/PUT api/main/inbox/access/{user_id}/ — владелец задаёт права ящика сотруднику."""
    permission_classes = [InboxPermission]

    def _target(self, request, user_id):
        company = access.company_of(request.user)
        if not company or not access.is_owner_like(request.user):
            raise ApiError("permission_denied", "Только владелец или админ.", status_code=403)
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.filter(pk=user_id).filter(company=company).first()
        if not u:
            raise ApiError("not_found", "Сотрудник не найден.", status_code=404)
        return u

    def get(self, request, user_id):
        return Response(access.perms_of(self._target(request, user_id)))

    def put(self, request, user_id):
        u = self._target(request, user_id)
        row, _ = InboxAccess.objects.get_or_create(user=u)
        for f in access.FLAGS:
            if f in request.data:
                setattr(row, f, bool(request.data[f]))
        row.save()
        if hasattr(u, "_inbox_access_cache"):
            del u._inbox_access_cache
        return Response(access.perms_of(u))


class ChannelListCreateView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.require(request.user, "can_view_inbox")
        ch_svc.ensure_server_bot_channel(company)
        qs = Channel.objects.filter(company=company).select_related("bot_settings", "risk_accepted_by").order_by("created_at", "id")
        return Response({"count": qs.count(), "next": None, "previous": None,
                         "results": [ch_svc.serialize(c) for c in qs]})

    @idempotent(replay_flag=True)
    def post(self, request):
        company = access.require(request.user, "can_manage_channels")
        data = request.data if isinstance(request.data, dict) else {}
        c = Channel(company=company)
        with transaction.atomic():
            ch_svc.apply_fields(c, data, request.user, creating=True)
            c.save()
        return Response(ch_svc.serialize(c), status=status.HTTP_201_CREATED)


class ChannelDetailView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request, pk):
        _, c = _get_channel(request, pk)
        return Response(ch_svc.serialize(c))

    def patch(self, request, pk):
        _, c = _get_channel(request, pk, "can_manage_channels")
        data = request.data if isinstance(request.data, dict) else {}
        ch_svc.apply_fields(c, data, request.user)
        c.save()
        return Response(ch_svc.serialize(c))

    def delete(self, request, pk):
        _, c = _get_channel(request, pk, "can_manage_channels")
        if c.bot_settings_id:
            raise ApiError("channel_managed_by_bot",
                           "Серверный бот — часть настроек Telegram-бота. Выключите его через PATCH status=disabled.")
        if c.conversations.exists():
            raise ApiError("channel_has_data", "В канале есть переписка. Выключите его (status=disabled) вместо удаления.",
                           status_code=409)
        c.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChannelCheckView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        _, c = _get_channel(request, pk, "can_manage_channels")
        if request.data.get("install_webhook") and not c.bot_settings_id:
            res = ch_svc.install_webhook(c)
            if not res["ok"]:
                return Response({"ok": False, "status": c.status, "detail": res["detail"]})
        return Response(ch_svc.check(c))


def _meta_on():
    from . import meta_channels
    return meta_channels.meta_enabled()


def _ig_on():
    from . import meta_channels
    return meta_channels.instagram_enabled()
