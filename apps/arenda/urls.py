from django.urls import include, path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register(r"branches", views.BranchViewSet, basename="arenda-branch")
router.register(r"unit-types", views.UnitTypeViewSet, basename="arenda-unit-type")
router.register(r"units", views.UnitViewSet, basename="arenda-unit")
router.register(r"tenants", views.TenantViewSet, basename="arenda-tenant")
router.register(r"leases", views.LeaseViewSet, basename="arenda-lease")

urlpatterns = [
    path("", include("apps.arenda.urls_money")),
    path("me/", views.MeView.as_view()),
    path("chess/", views.ChessView.as_view()),
    path("settings/", views.SettingsView.as_view()),
    path("access/", views.AccessListView.as_view()),
    path("access/presets/", views.AccessPresetsView.as_view()),
    path("access/<uuid:user_id>/", views.AccessDetailView.as_view()),
] + router.urls + [path("", include("apps.arenda.urls_team"))]

from .urls_docs import urlpatterns as _docs_urlpatterns  # noqa: E402
urlpatterns = _docs_urlpatterns + urlpatterns  # этап 3: договоры/шаблоны (до роутера: leases/preview/, leases/sign/)
