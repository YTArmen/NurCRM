"""Б-78: быстрые ответы и шаблоны WhatsApp."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from core.exceptions import ApiError

from . import access, quick, templates as tpl
from .access import InboxPermission
from .models import Channel, QuickReply, WaTemplate


class QuickReplyListView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.require(request.user, "can_reply_inbox")
        qs = quick.visible(request.user, company)
        if request.query_params.get("q"):
            qs = qs.filter(shortcut__icontains=request.query_params["q"].lstrip("/"))
        return Response({"results": [quick.out(q) for q in qs]})

    def post(self, request):
        company = access.require(request.user, "can_reply_inbox")
        data = request.data if isinstance(request.data, dict) else {}
        vals = quick.validate(data)
        shared = bool(data.get("shared"))
        if shared and not access.is_owner_like(request.user):
            raise ApiError("permission_denied", "Общие ответы создаёт владелец или админ.", status_code=403)
        owner = None if shared else request.user
        if QuickReply.objects.filter(company=company, owner=owner, shortcut=vals["shortcut"]).exists():
            raise ApiError("shortcut_exists", "Такой shortcut уже есть.", status_code=409)
        q = QuickReply.objects.create(company=company, owner=owner, **vals)
        return Response(quick.out(q), status=status.HTTP_201_CREATED)


class QuickReplyDetailView(APIView):
    permission_classes = [InboxPermission]

    def _get(self, request, pk, edit=False):
        company = access.require(request.user, "can_reply_inbox")
        q = quick.visible(request.user, company).filter(pk=pk).first()
        if not q:
            raise ApiError("not_found", "Быстрый ответ не найден.", status_code=404)
        if edit and not quick.can_edit(request.user, q):
            raise ApiError("permission_denied", "Менять общий ответ может владелец или админ.", status_code=403)
        return q

    def get(self, request, pk):
        return Response(quick.out(self._get(request, pk)))

    def patch(self, request, pk):
        q = self._get(request, pk, edit=True)
        vals = quick.validate(request.data if isinstance(request.data, dict) else {}, partial=True)
        for k, v in vals.items():
            setattr(q, k, v)
        q.save()
        return Response(quick.out(q))

    def delete(self, request, pk):
        self._get(request, pk, edit=True).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _channel(request, pk, flag="can_manage_channels"):
    company = access.require(request.user, flag)
    ch = Channel.objects.filter(company=company, pk=pk).first()
    if not ch:
        raise ApiError("not_found", "Канал не найден.", status_code=404)
    return company, ch


class ChannelTemplatesView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request, pk):
        _, ch = _channel(request, pk)
        if ch.kind not in tpl.WA_KINDS:
            raise ApiError("kind_not_supported", "Шаблоны есть только у каналов WhatsApp.")
        qs = WaTemplate.objects.filter(channel=ch)
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        return Response({"count": qs.count(), "results": [tpl.template_out(t) for t in qs]})

    @idempotent(replay_flag=True)
    def post(self, request, pk):
        _, ch = _channel(request, pk)
        name, lang, cat, comps = tpl.validate_new(request.data if isinstance(request.data, dict) else {})
        t = tpl.create(ch, name, lang, cat, comps)
        return Response(tpl.template_out(t), status=status.HTTP_201_CREATED)


class ChannelTemplatesSyncView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        _, ch = _channel(request, pk)
        n = tpl.sync(ch)
        return Response({"synced": n})


class ChannelTemplatesStandardView(APIView):
    permission_classes = [InboxPermission]

    @idempotent(replay_flag=True)
    def post(self, request, pk):
        _, ch = _channel(request, pk)
        langs = request.data.get("languages") if isinstance(request.data, dict) else None
        res = tpl.create_standard(ch, tuple(langs) if isinstance(langs, list) and langs else ("ru", "ky", "uz"))
        return Response(res, status=status.HTTP_201_CREATED)


class ChannelPricingView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request, pk):
        company, ch = _channel(request, pk, "can_view_inbox")
        return Response(tpl.pricing(company, request.query_params.get("country")))
