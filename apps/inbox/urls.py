# Адреса относительно api/main/inbox/ (ТЗ 26.10 Б-73…Б-77)
from django.urls import path

from . import views_channels as vc
from . import views_conv as vv

urlpatterns = [
    path("capabilities/", vc.CapabilitiesView.as_view(), name="inbox-capabilities"),
    path("me/", vc.MyAccessView.as_view(), name="inbox-me"),
    path("access/<uuid:user_id>/", vc.UserAccessView.as_view(), name="inbox-access"),
    path("channels/", vc.ChannelListCreateView.as_view(), name="inbox-channels"),
    path("channels/<uuid:pk>/", vc.ChannelDetailView.as_view(), name="inbox-channel"),
    path("channels/<uuid:pk>/check/", vc.ChannelCheckView.as_view(), name="inbox-channel-check"),
    path("contacts/<int:pk>/", vv.ContactDetailView.as_view(), name="inbox-contact"),
    path("contacts/<int:pk>/link/", vv.ContactLinkView.as_view(), name="inbox-contact-link"),
    path("contacts/<int:pk>/unlink/", vv.ContactUnlinkView.as_view(), name="inbox-contact-unlink"),
    path("contacts/<int:pk>/consents/", vv.ContactConsentsView.as_view(), name="inbox-contact-consents"),
    path("contacts/<int:pk>/data/", vv.ContactDataEraseView.as_view(), name="inbox-contact-data"),
    path("clients/<uuid:client_id>/channels/", vv.ClientChannelsView.as_view(), name="inbox-client-channels"),
    path("conversations/", vv.ConversationListView.as_view(), name="inbox-conversations"),
    path("conversations/<int:pk>/", vv.ConversationDetailView.as_view(), name="inbox-conversation"),
    path("conversations/<int:pk>/messages/", vv.MessagesView.as_view(), name="inbox-messages"),
    path("conversations/<int:pk>/assign/", vv.AssignView.as_view(), name="inbox-assign"),
    path("conversations/<int:pk>/take/", vv.TakeView.as_view(), name="inbox-take"),
    path("conversations/<int:pk>/status/", vv.StatusView.as_view(), name="inbox-status"),
    path("conversations/<int:pk>/labels/", vv.LabelsActionView.as_view(), name="inbox-conv-labels"),
    path("conversations/<int:pk>/read/", vv.ReadView.as_view(), name="inbox-read"),
    path("conversations/<int:pk>/transfer/", vv.TransferView.as_view(), name="inbox-transfer"),
    path("labels/", vv.LabelListCreateView.as_view(), name="inbox-labels"),
    path("labels/<int:pk>/", vv.LabelDetailView.as_view(), name="inbox-label"),
    path("unread-count/", vv.UnreadCountView.as_view(), name="inbox-unread-count"),
    path("webhooks/health/", vv.WebhookHealthView.as_view(), name="inbox-webhook-health"),
]
