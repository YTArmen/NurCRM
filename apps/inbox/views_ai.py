"""Б-79: черновик ИИ и журнал вызовов."""
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ApiError

from . import access, ai, services
from .access import InboxPermission
from .models import AiRun, Message
from .views_conv import _conv_or_error


def run_out(r) -> dict:
    return {
        "id": r.id, "conversation": r.conversation_id, "question": r.question,
        "tools": r.tools or [], "answer": r.answer, "mode": r.mode,
        "sent_by": r.sent_by or None, "outcome": r.outcome, "handoff_reason": r.handoff_reason or None,
        "model": r.model, "tokens": r.tokens, "cost": str(r.cost), "created_at": r.created_at.isoformat(),
    }


class AiDraftView(APIView):
    """POST api/main/inbox/conversations/{id}/ai-draft/ → {text, run_id}. Ничего не отправляет."""
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        company, conv = _conv_or_error(request, pk, "can_reply_inbox")
        access.require(request.user, "can_use_ai_inbox")
        data = request.data if isinstance(request.data, dict) else {}
        question = str(data.get("question") or "").strip()
        if not question:
            last = (Message.objects.filter(conversation=conv, direction="in", sender="contact")
                    .exclude(text="").order_by("-id").first())
            question = last.text if last else ""
        if not question:
            raise ApiError("invalid", "Нет вопроса покупателя для ИИ.", fields={"question": ["Обязательное поле"]})
        if conv.channel.kind == "instagram":
            ai.window_check_for_ai(conv)
        run = ai.generate(conv, question, mode="draft")
        if run.outcome == "error":
            raise ApiError("ai_unavailable", "ИИ сейчас недоступен (нет ключа или модель не отвечает).", status_code=503)
        return Response({"text": run.answer, "run_id": run.id, "outcome": run.outcome,
                         "needs_human": conv.needs_human})


class AiRunsView(APIView):
    """GET api/main/inbox/ai-runs/?conversation= — журнал."""
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.require(request.user, "can_view_inbox")
        qs = AiRun.objects.filter(company=company)
        cid = request.query_params.get("conversation")
        if cid:
            if not str(cid).isdigit():
                raise ApiError("invalid", "conversation: число.")
            if not services.visible_conversations(request.user, company).filter(pk=int(cid)).exists():
                raise ApiError("not_found", "Диалог не найден.", status_code=404)
            qs = qs.filter(conversation_id=int(cid))
        else:
            access.require(request.user, "can_view_all_conversations")
        rows = list(qs.order_by("-id")[:100])
        return Response({"count": len(rows), "results": [run_out(r) for r in rows]})
