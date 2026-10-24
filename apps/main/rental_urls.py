from django.urls import path

from apps.main.rentals import (
    RentalDetailAPIView,
    RentalExtendAPIView,
    RentalListCreateAPIView,
    RentalReturnAPIView,
)

urlpatterns = [
    path("", RentalListCreateAPIView.as_view(), name="rentals"),
    path("<uuid:pk>/", RentalDetailAPIView.as_view(), name="rental-detail"),
    path("<uuid:pk>/extend/", RentalExtendAPIView.as_view(), name="rental-extend"),
    path("<uuid:pk>/return/", RentalReturnAPIView.as_view(), name="rental-return"),
]
