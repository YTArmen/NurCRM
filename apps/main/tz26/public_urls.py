from django.urls import include, path

from apps.users.tz31_tariffs import PublicTariffsAPIView  # ТЗ кассы 05.11 п.32
from apps.users.client_versions import PublicClientVersionsAPIView  # cver2011
from apps.main.tz31_themes import PublicMarketplaceThemesAPIView  # ТЗ кассы 05.11 п.33

from apps.main.server_status import PublicErrorCodesAPIView, PublicStatusAPIView  # П1-2 (14.11)

urlpatterns = [
    path("status/", PublicStatusAPIView.as_view(), name="public-status"),
    path("error-codes/", PublicErrorCodesAPIView.as_view(), name="public-error-codes"),
    path("support/info/", __import__("apps.support.requests", fromlist=["x"]).PublicSupportInfoAPIView.as_view(), name="public-support-info"),  # П2-6 p2614
    path("tariffs/", PublicTariffsAPIView.as_view(), name="public-tariffs"),
    path("client-versions/", PublicClientVersionsAPIView.as_view(), name="public-client-versions"),  # cver2011
    path("modules/", __import__("apps.users.tariff_modules", fromlist=["x"]).PublicModulesAPIView.as_view(), name="public-modules"),  # tariff8
    path("marketplace/themes/", PublicMarketplaceThemesAPIView.as_view(), name="public-marketplace-themes"),
    path("", include("apps.main.tz26.hw_units.public_urls")),
    path("", include("apps.main.tz26.hw_prices.public_urls")),
    path("", include("apps.main.tz26.hw_catalog.public_urls")),
    path("", include("apps.main.tz26.cust_orders.public_urls")),
    path("", include("apps.main.tz26.staff.public_urls")),
    path("", include("apps.main.tz26.ai_stock.public_urls")),
    path("", include("apps.main.tz26.pos_sync.public_urls")),
    path("", include("apps.main.tz26.pos_print.public_urls")),
    path("", include("apps.main.tz26.hw_batches.public_urls")),
]
