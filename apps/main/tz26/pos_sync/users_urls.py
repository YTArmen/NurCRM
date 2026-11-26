# Адреса относительно api/users/ (подключено ПЕРЕД apps.users.urls)
from django.urls import path

from .views import (
    CompanyDeviceDetailAPIView,
    CompanyDeviceHeartbeatAPIView,
    CompanyDeviceListAPIView,
    DevicePushTokenAPIView,
)

urlpatterns = [
    path("company/devices/heartbeat/", CompanyDeviceHeartbeatAPIView.as_view(), name="company-device-heartbeat"),
    path("company/devices/", CompanyDeviceListAPIView.as_view(), name="company-device-list"),
    path("company/devices/<str:device_key>/", CompanyDeviceDetailAPIView.as_view(), name="company-device-detail"),
    path("devices/push-token/", DevicePushTokenAPIView.as_view(), name="device-push-token"),
]
