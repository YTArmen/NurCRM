import traceback
"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf.urls.static import static
from django.conf import settings
from .views_media_proxy import media_proxy
from .views_version import ClientVersionView
from .views_health import health_check
from apps.main.telegram_bot.views_public import TelegramWebhookPublicView
from apps.clientapp.landing import ReferralLandingView
from apps.main.media_receipts import PublicReceiptAPIView, PublicReceiptPdfAPIView, PublicReceiptRatingAPIView

if settings.ENABLE_API_DOCS:
    from rest_framework import permissions
    from drf_yasg.views import get_schema_view
    from drf_yasg import openapi

    schema_view = get_schema_view(
       openapi.Info(
          title="Nur CRM API",
          default_version='v1',
          description="API для проекта Nur CRM",
          terms_of_service="#",
          contact=openapi.Contact(email="support@NurCRM.com"),
          license=openapi.License(name="Nur CRM License"),
       ),
       public=True,
       permission_classes=(permissions.AllowAny,),
    )

# Пути для включения различных приложений
apps_includes = [
    # ТЗ-28: bootstrap, stock/movements, products/stock, clients/<id>/debts (GET),
    # pos/checkout с проверкой тела по Idempotency-Key, shifts/current.
    # Стоят ПЕРЕД apps.main.urls и apps.construction.urls намеренно — перекрывают старые пути.
    path('main/', include('apps.possync.urls')),
    path('construction/', include('apps.possync.urls_construction')),
    path('main/', include('apps.main.urls')),
    path('users/', include('apps.main.tz26.users_urls')),  # ТЗ 26.10 Б-70: устройства компании
    path('main/inbox/', include('apps.inbox.urls')),  # ТЗ 26.10 Б-73…Б-87: ящик «Чаты»
    path('inbox/', include('apps.inbox.public_urls')),  # вебхуки ящика (без входа)
    path('users/', include('apps.users.urls')),
    path('platform-admin/', include('apps.users.platform_admin_urls')),
    path('construction/', include('apps.construction.urls')),
    path('building/', include('apps.building.urls')),
    path('booking/', include('apps.booking.urls')),  
    path('barbershop/', include('apps.barber.urls')),   
    path('education/', include('apps.education.urls')),   
    path('cafe/', include('apps.cafe.urls')),   
    path('whatsapp/', include('apps.whatsapp.urls')), 
    path('storehouse/', include('apps.storehouse.urls')), 
    path('consalting/', include('apps.consalting.urls')),
    path('logistics/', include('apps.logistics.urls')),
    path('instagram/', include('apps.instagram.urls')),   
    path('warehouse/', include("apps.warehouse.urls")),
    path('ekassa/', include('apps.ekassa.urls')),
    path('rentals/', include('apps.main.rental_urls')),
    path('support/', include('apps.support.urls')),
    path('onec/', include('apps.onec.urls')),
    # path('crm/', include('apps.crm.urls')),
]

# API-роуты
api_urlpatterns = [
    path("api/public/", include("apps.main.tz26.public_urls")),  # ТЗ 26.10 Б-54 и др.: публичные адреса
    path("api/media-proxy/", media_proxy, name="media-proxy"),
    path("api/version/", ClientVersionView.as_view(), name="client-version"),
    path("api/public/receipts/<str:token>/", PublicReceiptAPIView.as_view(), name="public-receipt"),  # ТЗ Н-32
    path("api/public/receipts/<str:token>/pdf/", PublicReceiptPdfAPIView.as_view(), name="public-receipt-pdf"),
    path("api/public/receipts/<str:token>/rating/", PublicReceiptRatingAPIView.as_view(), name="public-receipt-rating"),
    path("api/health/", health_check, name="api-health-check"),
    path("api/telegram/webhook/<uuid:bot_uuid>/", TelegramWebhookPublicView.as_view(), name="telegram-bot-webhook"),
    path("api/telegram/webhook/", TelegramWebhookPublicView.as_view(), name="telegram-bot-webhook-info"),
    path("telegram/webhook/<uuid:bot_uuid>/", TelegramWebhookPublicView.as_view(), name="telegram-bot-webhook-alt"),
    path("telegram/webhook/", TelegramWebhookPublicView.as_view(), name="telegram-bot-webhook-alt-info"),
    path('api/v1/', include('apps.clientapp.urls')),  # приложение клиентов (покупателей)
    path('r/<str:code>/', ReferralLandingView.as_view(), name='capp-referral-landing'),  # ссылка-приглашение
    path('r/<str:code>', ReferralLandingView.as_view()),
    path('api/', include(apps_includes)),
    path('', include(apps_includes)),
]

# Основные пути проекта
urlpatterns = [
    path('health/', health_check, name='health-check'),
    path('admin/', admin.site.urls),  # Админка

    # Подключение API
    path('', include(api_urlpatterns)),
]

if settings.ENABLE_API_DOCS:
    urlpatterns += [
        path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
        path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
    ]

# Статические и медиафайлы
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


from django.http import JsonResponse

def custom_handler404(request, exception=None):
    if request.path.startswith('/api/'):
        # Единое ТЗ 26.10.2026, Б-01 / О2: «адреса нет» (route_not_found) отличается от «объекта нет» (not_found).
        from django.urls import Resolver404
        if exception is None or isinstance(exception, Resolver404):
            resp = JsonResponse({"detail": "Такого адреса API нет.", "code": "route_not_found"}, status=404)
        else:
            resp = JsonResponse({"detail": "Запрашиваемый ресурс не найден.", "code": "not_found"}, status=404)
        return resp
    from django.views.defaults import page_not_found
    return page_not_found(request, exception)

def custom_handler500(request):
    traceback.print_exc()
    if request.path.startswith('/api/'):
        return JsonResponse({"detail": "Внутренняя ошибка сервера (500).", "code": "server_error"}, status=500)
    from django.views.defaults import server_error
    return server_error(request)

handler404 = 'core.urls.custom_handler404'
handler500 = 'core.urls.custom_handler500'
