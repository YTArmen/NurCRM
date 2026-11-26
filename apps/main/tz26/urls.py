from django.urls import include, path

urlpatterns = [
    path("", include("apps.main.tz26.hw_units.urls")),
    path("", include("apps.main.tz26.hw_prices.urls")),
    path("", include("apps.main.tz26.hw_catalog.urls")),
    path("", include("apps.main.tz26.cust_orders.urls")),
    path("", include("apps.main.tz26.staff.urls")),
    path("", include("apps.main.tz26.ai_stock.urls")),
    path("", include("apps.main.tz26.pos_sync.urls")),
    path("", include("apps.main.tz26.pos_print.urls")),
    path("", include("apps.main.tz26.hw_batches.urls")),
]
