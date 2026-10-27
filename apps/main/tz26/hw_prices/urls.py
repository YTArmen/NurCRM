"""Адреса относительно api/main/."""
from django.urls import path

from . import views

urlpatterns = [
    path("price-levels/", views.PriceLevelListCreateAPIView.as_view(), name="price-levels"),
    path("price-levels/quote/", views.PriceQuoteAPIView.as_view(), name="price-levels-quote"),
    path("price-levels/<int:pk>/", views.PriceLevelDetailAPIView.as_view(), name="price-level-detail"),
    path("products/<uuid:pk>/prices/", views.ProductPricesAPIView.as_view(), name="product-prices"),
    path("clients/<uuid:client_id>/objects/", views.ClientObjectsAPIView.as_view(), name="client-objects"),
    path("clients/<uuid:client_id>/objects/<int:object_id>/", views.ClientObjectDetailAPIView.as_view(), name="client-object-detail"),
]
