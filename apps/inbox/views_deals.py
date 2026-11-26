"""Б-82: воронка магазина (can_view_clients, как воронка в программе владельца)."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from core.exceptions import ApiError

from . import access, deals as dl
from .access import InboxPermission
from .models import DealExt
from .views_conv import _conv_or_error


def _company(request):
    company = access.company_of(request.user)
    if not company or not access.can_see_phone(request.user):
        raise ApiError("permission_denied", "Нет доступа: Клиенты", status_code=403, top={"required": "can_view_clients"})
    return company


def _ext(company, pk):
    ext = DealExt.objects.filter(company=company, deal_id=pk).select_related("deal", "deal__contact", "client").first()
    if not ext:
        raise ApiError("not_found", "Сделка не найдена.", status_code=404)
    return ext


class DealListView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request):
        company = _company(request)
        qs = DealExt.objects.filter(company=company).select_related("deal", "deal__contact", "client")
        q = request.query_params
        if q.get("source"):
            qs = qs.filter(source=q["source"])
        if q.get("stage"):
            qs = qs.filter(deal__stage=q["stage"])
        if q.get("client"):
            qs = qs.filter(client_id=q["client"])
        if q.get("conversation") and q["conversation"].isdigit():
            qs = qs.filter(conversation_id=int(q["conversation"]))
        if q.get("q"):
            qs = qs.filter(deal__title__icontains=q["q"])
        rows = list(qs.order_by("-deal__created_at")[:500])
        return Response({"count": len(rows), "stages": dl.STAGES, "results": [dl.out(e) for e in rows]})

    @idempotent(replay_flag=True)
    def post(self, request):
        company = _company(request)
        ext = dl.create(company, request.user, request.data if isinstance(request.data, dict) else {})
        return Response(dl.out(ext), status=status.HTTP_201_CREATED)


class DealDetailView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request, pk):
        return Response(dl.out(_ext(_company(request), pk)))

    def patch(self, request, pk):
        ext = _ext(_company(request), pk)
        dl.update(ext, request.data if isinstance(request.data, dict) else {})
        return Response(dl.out(ext))

    def delete(self, request, pk):
        ext = _ext(_company(request), pk)
        ext.deal.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DealMoveView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        ext = _ext(_company(request), pk)
        dl.update(ext, {"stage": (request.data or {}).get("stage")})
        return Response(dl.out(ext))


class DealImportView(APIView):
    """POST deals/import/ — перенос funnel.json ([{id,name,phone,amount,note,stage,source,instagram,telegram_username,chat_id,created_at,updated_at}])."""
    permission_classes = [InboxPermission]

    def post(self, request):
        company = _company(request)
        data = request.data
        cards = data.get("cards") if isinstance(data, dict) else data
        return Response(dl.import_cards(company, request.user, cards), status=status.HTTP_200_OK)


class ConversationDealView(APIView):
    """POST conversations/{id}/deal/ — сделка из диалога; в диалоге ссылка deal."""
    permission_classes = [InboxPermission]

    @idempotent(replay_flag=True)
    def post(self, request, pk):
        company, conv = _conv_or_error(request, pk, "can_reply_inbox")
        if not access.can_see_phone(request.user):
            raise ApiError("permission_denied", "Нет доступа: Клиенты", status_code=403, top={"required": "can_view_clients"})
        if conv.deal_id and DealExt.objects.filter(deal_id=conv.deal_id).exists():
            raise ApiError("deal_exists", "У диалога уже есть сделка.", status_code=409, data={"deal": str(conv.deal_id)})
        d = dict(request.data) if isinstance(request.data, dict) else {}
        d.setdefault("name", conv.contact.display_name or conv.contact.username or "Покупатель")
        d.setdefault("phone", conv.contact.phone)
        d.setdefault("source", dl.KIND_SOURCE.get(conv.channel.kind, "other"))
        if conv.contact.username:
            d.setdefault("telegram_username" if conv.channel.kind.startswith("telegram") else "instagram", conv.contact.username)
        if conv.channel.kind.startswith("telegram"):
            d.setdefault("chat_id", conv.contact.external_id)
        ext = dl.create(company, request.user, d, conversation=conv)
        return Response(dl.out(ext), status=status.HTTP_201_CREATED)
