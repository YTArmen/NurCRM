"""Б-80: карточка клиента для чата, заказ из диалога."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from core.exceptions import ApiError

from . import access, actions
from .access import InboxPermission
from .views_conv import _conv_or_error


class ChatCardView(APIView):
    """GET api/main/inbox/clients/{id}/chat-card/."""
    permission_classes = [InboxPermission]

    def get(self, request, client_id):
        company = access.require(request.user, "can_view_inbox")
        if not access.can_see_phone(request.user):
            raise ApiError("permission_denied", "Нет доступа: Клиенты", status_code=403, top={"required": "can_view_clients"})
        from apps.main.models import Client
        client = Client.objects.filter(company=company, pk=client_id).first()
        if not client:
            raise ApiError("not_found", "Клиент не найден.", status_code=404)
        return Response(actions.chat_card(company, client))


class ConversationOrderView(APIView):
    """POST api/main/inbox/conversations/{id}/order/ {items:[{product, variant?, qty}], customer?, delivery?, comment?}."""
    permission_classes = [InboxPermission]

    @idempotent(replay_flag=True)
    def post(self, request, pk):
        _, conv = _conv_or_error(request, pk, "can_reply_inbox")
        data = request.data if isinstance(request.data, dict) else {}
        order = actions.create_order_from_conversation(conv, request.user, data)
        return Response({"order": {"id": str(order.id), "number": order.number, "status": order.status,
                                   "total": str(order.total), "source": order.source},
                         "conversation_id": conv.id}, status=status.HTTP_201_CREATED)
