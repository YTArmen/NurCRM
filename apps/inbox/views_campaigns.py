"""Б-81: рассылки (can_broadcast)."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from core.exceptions import ApiError

from . import access, campaigns as cp
from .access import InboxPermission
from .models import Campaign


def _get(request, pk):
    company = access.require(request.user, "can_broadcast")
    c = Campaign.objects.filter(company=company, pk=pk).select_related("channel", "template", "company").first()
    if not c:
        raise ApiError("not_found", "Рассылка не найдена.", status_code=404)
    return c


class CampaignListView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.require(request.user, "can_broadcast")
        qs = Campaign.objects.filter(company=company).select_related("channel", "template")
        q = request.query_params
        if q.get("status"):
            qs = qs.filter(status=q["status"])
        if q.get("kind"):
            qs = qs.filter(kind=q["kind"])
        rows = list(qs[:100])
        return Response({"count": len(rows), "results": [cp.campaign_out(c) for c in rows]})

    def post(self, request):
        company = access.require(request.user, "can_broadcast")
        data = request.data if isinstance(request.data, dict) else {}
        vals = cp.validate(company, data)
        c = Campaign.objects.create(company=company, created_by=request.user, **vals)
        return Response(cp.campaign_out(c), status=status.HTTP_201_CREATED)


class CampaignPreviewDraftView(APIView):
    """POST campaigns/preview/ — оценка по телу запроса, без сохранения."""
    permission_classes = [InboxPermission]

    def post(self, request):
        company = access.require(request.user, "can_broadcast")
        data = request.data if isinstance(request.data, dict) else {}
        vals = cp.validate(company, data)
        c = Campaign(company=company, **vals)
        return Response(cp.preview(c))


class CampaignDetailView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request, pk):
        return Response(cp.campaign_out(_get(request, pk)))

    def patch(self, request, pk):
        c = _get(request, pk)
        if c.status != "draft":
            raise ApiError("invalid_status", "Менять можно только черновик.", status_code=409, data={"status": c.status})
        vals = cp.validate(c.company, request.data if isinstance(request.data, dict) else {}, current=c)
        for k, v in vals.items():
            setattr(c, k, v)
        c.save()
        return Response(cp.campaign_out(c))

    def delete(self, request, pk):
        c = _get(request, pk)
        if c.status not in ("draft", "canceled"):
            raise ApiError("invalid_status", "Удалить можно черновик или отменённую.", status_code=409)
        c.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CampaignPreviewView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        return Response(cp.preview(_get(request, pk)))


class CampaignStartView(APIView):
    """POST campaigns/{id}/start/ — с Idempotency-Key: повтор тем же ключом — тот же результат."""
    permission_classes = [InboxPermission]

    @idempotent(replay_flag=True)
    def post(self, request, pk):
        c = _get(request, pk)
        cp.start(c, request.user)
        return Response(cp.campaign_out(c))


class CampaignCancelView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        c = _get(request, pk)
        cp.cancel(c)
        return Response(cp.campaign_out(c))


class CampaignRecipientsView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request, pk):
        c = _get(request, pk)
        rows = c.recipients.select_related("contact", "message").order_by("id")[:500]
        return Response({"results": [{"contact": r.contact_id, "name": r.contact.display_name, "status": r.status,
                                      "skip_reason": r.skip_reason or None,
                                      "message_status": r.message.status if r.message_id else None} for r in rows]})
