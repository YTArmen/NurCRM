from django.urls import include, path

urlpatterns = [
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
