from django.urls import path

from .views import (
    AttributeKeyDefaultsAPIView,
    AttributeKeyDetailAPIView,
    AttributeKeyListCreateAPIView,
    CategoryRelationDetailAPIView,
    CategoryRelationListCreateAPIView,
)

urlpatterns = [
    path("attribute-keys/", AttributeKeyListCreateAPIView.as_view(), name="tz26-attribute-keys"),
    path("attribute-keys/defaults/", AttributeKeyDefaultsAPIView.as_view(), name="tz26-attribute-keys-defaults"),
    path("attribute-keys/<str:key>/", AttributeKeyDetailAPIView.as_view(), name="tz26-attribute-key-detail"),
    path("category-relations/", CategoryRelationListCreateAPIView.as_view(), name="tz26-category-relations"),
    path("category-relations/<uuid:pk>/", CategoryRelationDetailAPIView.as_view(), name="tz26-category-relation-detail"),
]
