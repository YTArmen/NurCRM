from django.urls import path

from .views import SphereProfilesAPIView

urlpatterns = [
    path("sphere-profiles/", SphereProfilesAPIView.as_view(), name="tz26-sphere-profiles"),
]
