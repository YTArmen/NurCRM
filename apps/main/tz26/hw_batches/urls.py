from django.urls import path

from . import views

urlpatterns = [
    path("products/<uuid:pk>/batches/", views.ProductBatchesAPIView.as_view(), name="tz26-product-batches"),
    path("products/<uuid:pk>/cut/", views.ProductCutAPIView.as_view(), name="tz26-product-cut"),
    path("products/<uuid:pk>/rolls/", views.ProductRollsAPIView.as_view(), name="tz26-product-rolls"),
    path("pos/carts/<uuid:cart_id>/items/<uuid:item_id>/batch/", views.CartItemBatchAPIView.as_view(),
         name="tz26-cart-item-batch"),
    path("pos/cart/quote/", views.CartQuoteAPIView.as_view(), name="tz26-cart-quote"),
]
