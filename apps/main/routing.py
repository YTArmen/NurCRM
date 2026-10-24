from django.urls import re_path

from .consumers import CompanyChangesConsumer, NotificationsConsumer

websocket_urlpatterns = [
    re_path(r"^ws/notifications/$", NotificationsConsumer.as_asgi(), name="ws-notifications"),
    # ТЗ Н-31: токен первым сообщением (JWT-middleware этот путь пропускает)
    re_path(r"^ws/company/$", CompanyChangesConsumer.as_asgi(), name="ws-company-changes"),
]
