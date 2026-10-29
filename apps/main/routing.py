from django.urls import re_path

from .consumers import CompanyChangesConsumer, MainEventsConsumer, NotificationsConsumer

websocket_urlpatterns = [
    re_path(r"^ws/notifications/$", NotificationsConsumer.as_asgi(), name="ws-notifications"),
    # ТЗ Н-31: токен первым сообщением (JWT-middleware этот путь пропускает)
    re_path(r"^ws/company/$", CompanyChangesConsumer.as_asgi(), name="ws-company-changes"),
    # ТЗ 29.10 Б-102: поток событий (токен первым сообщением или ?token=)
    re_path(r"^ws/main/events/$", MainEventsConsumer.as_asgi(), name="ws-main-events"),
]
