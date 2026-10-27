from django.urls import path

from . import views

urlpatterns = [
    path("customer-orders/", views.OrderListCreateAPIView.as_view(), name="customer-orders"),
    path("customer-orders/<uuid:pk>/", views.OrderDetailAPIView.as_view(), name="customer-order-detail"),
    path("customer-orders/<uuid:pk>/reserve/", views.OrderReserveAPIView.as_view(), name="customer-order-reserve"),
    path("customer-orders/<uuid:pk>/prepay/", views.OrderPrepayAPIView.as_view(), name="customer-order-prepay"),
    path("customer-orders/<uuid:pk>/deliver/", views.OrderDeliverAPIView.as_view(), name="customer-order-deliver"),
    path("customer-orders/<uuid:pk>/cancel/", views.OrderCancelAPIView.as_view(), name="customer-order-cancel"),
]
