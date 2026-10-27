# Адреса относительно api/main/inbox/ (ТЗ 26.10 Б-73…Б-77)
from django.urls import path

from . import views_channels as vc
from . import views_actions as vact
from . import views_ai as vai
from . import views_campaigns as vcp
from . import views_analytics as van
from . import views_conv as vv
from . import views_deals as vd
from . import views_meta as vm
from . import views_templates as vt
from . import views_widget as vw

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
    path("clients/<uuid:client_id>/chat-card/", vact.ChatCardView.as_view(), name="inbox-chat-card"),
    path("conversations/<int:pk>/order/", vact.ConversationOrderView.as_view(), name="inbox-conv-order"),
    path("conversations/", vv.ConversationListView.as_view(), name="inbox-conversations"),
    path("conversations/<int:pk>/", vv.ConversationDetailView.as_view(), name="inbox-conversation"),
    path("conversations/<int:pk>/messages/", vv.MessagesView.as_view(), name="inbox-messages"),
    path("conversations/<int:pk>/assign/", vv.AssignView.as_view(), name="inbox-assign"),
    path("conversations/<int:pk>/take/", vv.TakeView.as_view(), name="inbox-take"),
    path("conversations/<int:pk>/status/", vv.StatusView.as_view(), name="inbox-status"),
    path("conversations/<int:pk>/labels/", vv.LabelsActionView.as_view(), name="inbox-conv-labels"),
    path("conversations/<int:pk>/read/", vv.ReadView.as_view(), name="inbox-read"),
    path("conversations/<int:pk>/transfer/", vv.TransferView.as_view(), name="inbox-transfer"),
    path("conversations/<int:pk>/ai-draft/", vai.AiDraftView.as_view(), name="inbox-ai-draft"),
    path("ai-runs/", vai.AiRunsView.as_view(), name="inbox-ai-runs"),
    path("widget/settings/", vw.WidgetSettingsView.as_view(), name="inbox-widget-settings"),
    path("quick-replies/", vt.QuickReplyListView.as_view(), name="inbox-quick-replies"),
    path("quick-replies/<int:pk>/", vt.QuickReplyDetailView.as_view(), name="inbox-quick-reply"),
    path("channels/<uuid:pk>/templates/", vt.ChannelTemplatesView.as_view(), name="inbox-templates"),
    path("channels/<uuid:pk>/templates/sync/", vt.ChannelTemplatesSyncView.as_view(), name="inbox-templates-sync"),
    path("channels/<uuid:pk>/templates/standard/", vt.ChannelTemplatesStandardView.as_view(), name="inbox-templates-standard"),
    path("channels/<uuid:pk>/pricing/", vt.ChannelPricingView.as_view(), name="inbox-pricing"),
    path("channels/whatsapp/embedded-signup/start/", vm.WaStartView.as_view(), name="inbox-wa-start"),
    path("channels/whatsapp/embedded-signup/callback/", vm.WaCallbackView.as_view(), name="inbox-wa-callback"),
    path("channels/instagram/oauth/start/", vm.IgStartView.as_view(), name="inbox-ig-start"),
    path("channels/instagram/oauth/callback/", vm.IgCallbackView.as_view(), name="inbox-ig-callback"),
    path("campaigns/", vcp.CampaignListView.as_view(), name="inbox-campaigns"),
    path("campaigns/preview/", vcp.CampaignPreviewDraftView.as_view(), name="inbox-campaign-preview-draft"),
    path("campaigns/<int:pk>/", vcp.CampaignDetailView.as_view(), name="inbox-campaign"),
    path("campaigns/<int:pk>/preview/", vcp.CampaignPreviewView.as_view(), name="inbox-campaign-preview"),
    path("campaigns/<int:pk>/start/", vcp.CampaignStartView.as_view(), name="inbox-campaign-start"),
    path("campaigns/<int:pk>/cancel/", vcp.CampaignCancelView.as_view(), name="inbox-campaign-cancel"),
    path("campaigns/<int:pk>/recipients/", vcp.CampaignRecipientsView.as_view(), name="inbox-campaign-recipients"),
    path("deals/", vd.DealListView.as_view(), name="inbox-deals"),
    path("deals/import/", vd.DealImportView.as_view(), name="inbox-deals-import"),
    path("deals/<uuid:pk>/", vd.DealDetailView.as_view(), name="inbox-deal"),
    path("deals/<uuid:pk>/move/", vd.DealMoveView.as_view(), name="inbox-deal-move"),
    path("conversations/<int:pk>/deal/", vd.ConversationDealView.as_view(), name="inbox-conv-deal"),
    path("analytics/", van.AnalyticsView.as_view(), name="inbox-analytics"),
    path("labels/", vv.LabelListCreateView.as_view(), name="inbox-labels"),
    path("labels/<int:pk>/", vv.LabelDetailView.as_view(), name="inbox-label"),
    path("unread-count/", vv.UnreadCountView.as_view(), name="inbox-unread-count"),
    path("webhooks/health/", vv.WebhookHealthView.as_view(), name="inbox-webhook-health"),
]
