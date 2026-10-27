"""Б-84: GET api/main/inbox/analytics/."""
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access, analytics
from .access import InboxPermission


class AnalyticsView(APIView):
    """Владелец и админ — по компании; сотрудник — только свои цифры."""
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.require(request.user, "can_view_inbox")
        return Response(analytics.build(request, company))
