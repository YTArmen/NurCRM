# apps/integrations/views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from django.shortcuts import get_object_or_404
from .models import WhatsAppSession
from .serializers import WhatsAppSessionSerializer
from .auth import CompanyTokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView
from django.conf import settings
from rest_framework.permissions import IsAuthenticated

class TokenView(TokenObtainPairView):
    serializer_class = CompanyTokenObtainPairSerializer

class IsCompanyMember(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        company_id = str(view.kwargs.get("company_id"))
        return user.is_authenticated and str(user.company_id) == company_id and \
               getattr(user.company, "can_view_whatsapp", False)

class WhatsAppSessionGetView(APIView):
    permission_classes = [IsCompanyMember]
    def get(self, request, company_id):
        session, _ = WhatsAppSession.objects.get_or_create(company_id=company_id)
        return Response(WhatsAppSessionSerializer(session).data)

# Node будет дергать без JWT, но с тех. токеном (для простоты локала)
class WhatsAppSessionUpsertView(APIView):
    permission_classes = []  # защищаем по заголовку
    authentication_classes = []
    def post(self, request, company_id):
        token = request.headers.get("X-WA-TOKEN") or ""
        expected = getattr(settings, "WHATSAPP_NODE_TOKEN", "") or ""
        # Аудит 12.11: при пустом токене на сервере пустой заголовок проходил (подмена QR любой компании).
        import hmac as _hmac
        if not expected or not token or not _hmac.compare_digest(str(token), str(expected)):
            return Response({"detail": "forbidden"}, status=403)
        session, _ = WhatsAppSession.objects.get_or_create(company_id=company_id)
        for f in ("status", "last_qr_data_url", "phone_hint"):
            if f in request.data:
                setattr(session, f, request.data[f])
        session.save()
        return Response(WhatsAppSessionSerializer(session).data, status=200)


class MeView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        u = request.user
        return Response({
            "user_id": str(u.id),
            "email": u.email,
            "company_id": str(u.company_id) if u.company_id else None,
            "company_name": u.company.name if u.company_id else None,
            "can_view_whatsapp": bool(getattr(u.company, "can_view_whatsapp", False)),
        })