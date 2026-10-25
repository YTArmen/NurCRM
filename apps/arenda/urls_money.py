"""Этап 2 «Деньги»: оплаты, чеки, взыскания, карточка арендатора, акты сверки."""
from django.urls import path

from . import views_money as v

urlpatterns = [
    path("payments/", v.PaymentsView.as_view()),
    path("payments/<uuid:pk>/storno/", v.PaymentStornoView.as_view()),
    path("receipts/", v.ReceiptListView.as_view()),
    path("receipts/<uuid:pk>/", v.ReceiptDetailView.as_view()),
    path("receipts/<uuid:pk>/text/", v.ReceiptTextView.as_view()),
    path("receipts/<uuid:pk>/print/", v.ReceiptPrintView.as_view()),
    path("tenants/<uuid:pk>/card/", v.TenantCardView.as_view()),
    path("tenants/<uuid:pk>/remarks/", v.RemarksView.as_view()),
    path("tenants/<uuid:pk>/remarks/<uuid:rid>/", v.RemarkDetailView.as_view()),
    path("tenants/<uuid:pk>/acts/", v.TenantActsView.as_view()),
    path("acts/", v.ActListView.as_view()),
    path("acts/<uuid:pk>/", v.ActDetailView.as_view()),
    path("acts/<uuid:pk>/print/", v.ActPrintView.as_view()),
]
