from django.urls import include, path

urlpatterns = [
    path("", include("apps.main.tz26.hw_units.users_urls")),
    path("", include("apps.main.tz26.hw_prices.users_urls")),
    path("", include("apps.main.tz26.hw_catalog.users_urls")),
    path("", include("apps.main.tz26.cust_orders.users_urls")),
    path("", include("apps.main.tz26.staff.users_urls")),
    path("", include("apps.main.tz26.ai_stock.users_urls")),
    path("", include("apps.main.tz26.pos_sync.users_urls")),
    path("", include("apps.main.tz26.pos_print.users_urls")),
    path("", include("apps.main.tz26.hw_batches.users_urls")),
]
