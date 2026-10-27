# Адреса относительно api/main/inbox/ (ТЗ 26.10 Б-73…Б-77)
from django.urls import path

from . import views_channels as vc

urlpatterns = [
    path("capabilities/", vc.CapabilitiesView.as_view(), name="inbox-capabilities"),
    path("me/", vc.MyAccessView.as_view(), name="inbox-me"),
    path("access/<uuid:user_id>/", vc.UserAccessView.as_view(), name="inbox-access"),
    path("channels/", vc.ChannelListCreateView.as_view(), name="inbox-channels"),
    path("channels/<uuid:pk>/", vc.ChannelDetailView.as_view(), name="inbox-channel"),
    path("channels/<uuid:pk>/check/", vc.ChannelCheckView.as_view(), name="inbox-channel-check"),
]
