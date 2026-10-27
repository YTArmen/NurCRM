from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .profiles import all_profiles


class SphereProfilesAPIView(APIView):
    """Б-54: GET api/public/sphere-profiles/ — каталог профилей магазина (публично)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response(all_profiles())
