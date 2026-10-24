import secrets

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from .events import EVENTS, validate_public_url
from .models import ApiKey, WebhookEndpoint, hash_api_key


def _owner_company(request):
    """Ключами и вебхуками управляет только владелец/админ, и только с обычным входом (не по API-ключу)."""
    from apps.users.views import _get_company, _is_owner_like

    if isinstance(request.auth, ApiKey):
        raise PermissionDenied("Управление ключами по API-ключу недоступно.")
    user = request.user
    company = _get_company(user)
    if company is None or not _is_owner_like(user):
        raise PermissionDenied("Доступно только владельцу компании.")
    return company


# ---------- API-ключи ----------

class ApiKeySerializer(serializers.ModelSerializer):
    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=ApiKey.Scope.choices),
        required=False,
        allow_empty=False,
    )

    class Meta:
        model = ApiKey
        fields = ("id", "name", "prefix", "scopes", "rate_limit_per_minute", "created_at", "last_used_at", "revoked_at")
        read_only_fields = ("id", "prefix", "created_at", "last_used_at", "revoked_at")


class ApiKeyListCreateAPIView(generics.ListCreateAPIView):
    """
    GET  /api/users/api-keys/  — список ключей (без самих ключей)
    POST /api/users/api-keys/  {"name": "...", "scopes": ["read"]} → ключ показывается один раз
    """

    serializer_class = ApiKeySerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        return ApiKey.objects.filter(company=_owner_company(self.request), revoked_at__isnull=True)

    def create(self, request, *args, **kwargs):
        company = _owner_company(request)
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        raw = ApiKey.generate()
        key = ApiKey.objects.create(
            company=company,
            name=ser.validated_data["name"],
            scopes=ser.validated_data.get("scopes") or [ApiKey.Scope.READ],
            rate_limit_per_minute=ser.validated_data.get("rate_limit_per_minute") or 120,
            prefix=raw[:16],
            key_hash=hash_api_key(raw),
            created_by=request.user,
        )
        data = ApiKeySerializer(key).data
        data["key"] = raw
        return Response(data, status=status.HTTP_201_CREATED)


class ApiKeyRevokeAPIView(generics.DestroyAPIView):
    """DELETE /api/users/api-keys/{id}/ — отозвать ключ."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk, *args, **kwargs):
        key = get_object_or_404(ApiKey, pk=pk, company=_owner_company(request))
        if key.revoked_at is None:
            key.revoked_at = timezone.now()
            key.save(update_fields=["revoked_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ApiKeyCallLogAPIView(generics.GenericAPIView):
    """GET /api/users/api-keys/{id}/calls/ — последние 200 вызовов по ключу (ТЗ Н-24)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        from .models import ApiKeyCallLog

        key = get_object_or_404(ApiKey, pk=pk, company=_owner_company(request))
        rows = ApiKeyCallLog.objects.filter(key=key)[:200]
        return Response([
            {"created_at": r.created_at, "method": r.method, "path": r.path, "scope": r.scope,
             "allowed": r.allowed, "reason": r.reason}
            for r in rows
        ])


# ---------- Вебхуки ----------

class WebhookEndpointSerializer(serializers.ModelSerializer):
    events = serializers.ListField(child=serializers.ChoiceField(choices=EVENTS), allow_empty=False)
    secret = serializers.CharField(write_only=True, required=False, min_length=16, max_length=128)

    class Meta:
        model = WebhookEndpoint
        fields = (
            "id", "url", "events", "secret", "is_active",
            "created_at", "updated_at", "last_delivery_at", "last_status", "last_error",
        )
        read_only_fields = ("id", "created_at", "updated_at", "last_delivery_at", "last_status", "last_error")

    def validate_url(self, value):
        try:
            validate_public_url(value)
        except ValueError as e:
            raise serializers.ValidationError(str(e))
        return value

    def validate_events(self, value):
        return list(dict.fromkeys(value))


class WebhookListCreateAPIView(generics.ListCreateAPIView):
    """
    GET  /api/users/webhooks/
    POST /api/users/webhooks/ {"url": "...", "events": ["sale.paid", ...], "secret": "..."}
    Без secret сервер создаст его сам и вернёт один раз.
    """

    serializer_class = WebhookEndpointSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        return WebhookEndpoint.objects.filter(company=_owner_company(self.request))

    def create(self, request, *args, **kwargs):
        company = _owner_company(request)
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        secret = ser.validated_data.pop("secret", None)
        generated = secret is None
        ep = ser.save(company=company, created_by=request.user, secret=secret or secrets.token_hex(32))
        data = WebhookEndpointSerializer(ep).data
        if generated:
            data["secret"] = ep.secret
        return Response(data, status=status.HTTP_201_CREATED)


class WebhookDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /api/users/webhooks/{id}/"""

    serializer_class = WebhookEndpointSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return WebhookEndpoint.objects.filter(company=_owner_company(self.request))


class WebhookEventsAPIView(generics.GenericAPIView):
    """GET /api/users/webhooks/events/ — список событий, на которые можно подписаться."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        return Response({"events": list(EVENTS)})
