"""Б-70 устройства компании, Б-77 п.3 токен push."""
from datetime import timedelta

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.tz26.pos_sync.models import Device, DevicePushToken
from apps.main.tz26.pos_sync.services import DEVICE_KEY_RE, user_brief
from core.exceptions import ApiError

ONLINE_WINDOW = timedelta(seconds=120)


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _is_owner(user) -> bool:
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "role", None) == "owner"
        or getattr(user, "owned_company", None) is not None
    )


def _is_owner_or_admin(user) -> bool:
    return _is_owner(user) or getattr(user, "role", None) == "admin" or bool(getattr(user, "is_staff", False))


def _require_company(request):
    company = _company(request.user)
    if company is None:
        raise ApiError("no_company", "У пользователя нет компании.", status_code=status.HTTP_403_FORBIDDEN)
    return company


def _device_key_field():
    return serializers.RegexField(
        DEVICE_KEY_RE.pattern, max_length=64,
        error_messages={"invalid": "8–64 символа: латиница, цифры, _ и -."},
    )


class HeartbeatSerializer(serializers.Serializer):
    device_key = _device_key_field()
    kind = serializers.ChoiceField(choices=Device.Kind.values)
    name = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    app_version = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    cashbox = serializers.UUIDField(required=False, allow_null=True)
    can_print = serializers.BooleanField(required=False, default=False)


def device_payload(d: Device, now=None) -> dict:
    now = now or timezone.now()
    cb = d.cashbox
    return {
        "device_key": d.device_key,
        "kind": d.kind,
        "name": d.name,
        "app_version": d.app_version,
        "cashbox": str(d.cashbox_id) if d.cashbox_id else None,
        "cashbox_name": (cb.name if cb else None),
        "can_print": d.can_print,
        "user": user_brief(d.user),
        "last_seen_at": d.last_seen_at.isoformat(),
        "online": (now - d.last_seen_at) <= ONLINE_WINDOW,
    }


class CompanyDeviceHeartbeatAPIView(APIView):
    """POST api/users/company/devices/heartbeat/ — раз в 60 с, любой сотрудник за своё устройство."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from apps.construction.models import Cashbox

        company = _require_company(request)
        ser = HeartbeatSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        cashbox = None
        if v.get("cashbox"):
            cashbox = Cashbox.objects.filter(id=v["cashbox"], company=company).first()
            if cashbox is None:
                raise ApiError("cashbox_not_found", "Касса не найдена.", status_code=400,
                               fields={"cashbox": ["Касса не найдена."]})
        now = timezone.now()
        device, _ = Device.objects.update_or_create(
            company=company, device_key=v["device_key"],
            defaults={
                "kind": v["kind"], "name": v["name"], "app_version": v["app_version"],
                "cashbox": cashbox, "can_print": v["can_print"], "user": request.user, "last_seen_at": now,
            },
        )
        return Response({"device_id": str(device.id), "server_time": now.isoformat()})


class CompanyDeviceListAPIView(APIView):
    """GET api/users/company/devices/ — владелец и администратор: все; кассир: устройства своей кассы с can_print."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.construction.models import CashShift

        company = _require_company(request)
        user = request.user
        qs = Device.objects.filter(company=company).select_related("cashbox", "user")
        if not _is_owner_or_admin(user):
            cashbox_ids = set(
                CashShift.objects.filter(cashier=user, status=CashShift.Status.OPEN, cashbox__company=company)
                .values_list("cashbox_id", flat=True)
            )
            cashbox_ids |= set(
                Device.objects.filter(company=company, user=user, cashbox__isnull=False)
                .values_list("cashbox_id", flat=True)
            )
            qs = qs.filter(Q(cashbox_id__in=cashbox_ids) & Q(can_print=True)) if cashbox_ids else qs.none()
        now = timezone.now()
        return Response([device_payload(d, now) for d in qs.order_by("-last_seen_at", "id")])


class CompanyDeviceDetailAPIView(APIView):
    """DELETE api/users/company/devices/{device_key}/ — забыть устройство (владелец)."""
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, device_key):
        company = _require_company(request)
        if not _is_owner(request.user):
            raise ApiError("permission_denied", "Забыть устройство может владелец.",
                           status_code=status.HTTP_403_FORBIDDEN)
        device = get_object_or_404(Device, company=company, device_key=device_key)
        device.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PushTokenSerializer(serializers.Serializer):
    device_key = _device_key_field()
    platform = serializers.ChoiceField(choices=DevicePushToken.Platform.values)
    token = serializers.CharField(max_length=1024)


class DevicePushTokenAPIView(APIView):
    """POST api/users/devices/push-token/ — только хранение токена (отправки push здесь нет)."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _require_company(request)
        ser = PushTokenSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        token = v["token"].strip()
        if not token:
            raise ApiError("invalid", "Пустой токен.", fields={"token": ["Пустой токен."]})
        # Один токен - одно устройство: старые привязки этого токена снимаем
        DevicePushToken.objects.filter(token=token).exclude(user=request.user, device_key=v["device_key"]).delete()
        obj, created = DevicePushToken.objects.update_or_create(
            user=request.user, device_key=v["device_key"],
            defaults={"company": company, "platform": v["platform"], "token": token},
        )
        return Response(
            {"device_key": obj.device_key, "platform": obj.platform, "registered": True},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
