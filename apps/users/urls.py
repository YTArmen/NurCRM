from apps.users.app_settings_views import CompanyFeatureActivateAPIView, UserAppSettingsAPIView
from django.urls import path
from .views import (
    CompanyAddonListAPIView,
    RegisterAPIView,
    CustomTokenObtainPairView,
    CustomTokenRefreshView,
    EmployeeListAPIView,
    EmployeeCreateAPIView,
    CurrentUserAPIView,
    IndustryListAPIView,
    SubscriptionPlanListAPIView, 
    FeatureListAPIView,
    EmployeeDestroyAPIView, 
    CompanyDetailAPIView,
    CompanySubscriptionDetailAPIView,
    SectorListAPIView,
    RegionListAPIView,
    EmployeeDetailAPIView,
    ChangePasswordView,
    CompanyUpdateAPIView,
    CompanyCheckSlugAPIView,
    # 👇 новые для ролей
    RoleListAPIView,
    CustomRoleCreateAPIView,
    CustomRoleDetailAPIView,
    BranchDetailAPIView, 
    BranchListCreateAPIView,
    #таски лист для админа и апдейт на подписку
    CompanyListAPIView,
    CompanySubscriptionAdminAPIView,
    UserUiPreferencesAPIView,
)

from apps.integrations.views import (
    ApiKeyListCreateAPIView,
    ApiKeyRevokeAPIView,
    ApiKeyCallLogAPIView,
    WebhookDetailAPIView,
    WebhookEventsAPIView,
    WebhookListCreateAPIView,
)
from apps.users.scale_views import send_products_to_scale, get_scale_api_token, register_scale

from .pin import PinSetView, PinVerifyView, PinSettingsView

urlpatterns = [
    # 🔐 Авторизация / регистрация
    path('auth/register/', RegisterAPIView.as_view(), name='user-register'),
    path('auth/login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/pin/', PinSetView.as_view(), name='admin-pin-set'),
    path('auth/pin/verify/', PinVerifyView.as_view(), name='admin-pin-verify'),
    path('auth/pin/settings/', PinSettingsView.as_view(), name='admin-pin-settings'),
    path('auth/refresh/', CustomTokenRefreshView.as_view(), name='token_refresh'),
    
    path("scales/token/", get_scale_api_token, name="scale-api-token"),
    path("api-keys/", ApiKeyListCreateAPIView.as_view(), name="api-key-list"),
    path("api-keys/<uuid:pk>/", ApiKeyRevokeAPIView.as_view(), name="api-key-revoke"),
    path("api-keys/<uuid:pk>/calls/", ApiKeyCallLogAPIView.as_view(), name="api-key-calls"),
    path("webhooks/", WebhookListCreateAPIView.as_view(), name="webhook-list"),
    path("webhooks/events/", WebhookEventsAPIView.as_view(), name="webhook-events"),
    path("webhooks/<uuid:pk>/", WebhookDetailAPIView.as_view(), name="webhook-detail"),
    path("scales/register/", register_scale, name="scale-api-register"),
    

    # ⚙️ Настройки
    path('ui-preferences/', UserUiPreferencesAPIView.as_view(), name='user-ui-preferences'),
    path('app-settings/<str:app>/', UserAppSettingsAPIView.as_view(), name='user-app-settings'),
    path('company/features/activate/', CompanyFeatureActivateAPIView.as_view(), name='company-feature-activate'),
    path('settings/change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('settings/company/', CompanyUpdateAPIView.as_view(), name='company-update'),
    path('company/check-slug/', CompanyCheckSlugAPIView.as_view(), name='company-check-slug'),
    path('company/addons/', CompanyAddonListAPIView.as_view(), name='company-addons'),

    # 👥 Работа с сотрудниками
    path('employees/', EmployeeListAPIView.as_view(), name='employee-list'),
    path('employees/create/', EmployeeCreateAPIView.as_view(), name='employee-create'),
    path('employees/<uuid:pk>/', EmployeeDetailAPIView.as_view(), name='employee-detail'),
    path('employees/<uuid:pk>/delete/', EmployeeDestroyAPIView.as_view(), name='employee-delete'),

    # 👤 Личный кабинет
    path('profile/', CurrentUserAPIView.as_view(), name='user-me'),

    # 📚 Справочники
    path('industries/', IndustryListAPIView.as_view(), name='industry-list'),
    path('sectors/', SectorListAPIView.as_view(), name='sector-list'),
    path('regions/', RegionListAPIView.as_view(), name='region-list'),
    path('subscription-plans/', SubscriptionPlanListAPIView.as_view(), name='subscription-plan-list'),
    path('features/', FeatureListAPIView.as_view(), name='feature-list'),
    path('company/subscription/', CompanySubscriptionDetailAPIView.as_view(), name='company-subscription-detail'),
    path('company/', CompanyDetailAPIView.as_view(), name='company-detail'),
    
    path("branches/", BranchListCreateAPIView.as_view(), name="branch-list"),
    path("branches/<uuid:pk>/", BranchDetailAPIView.as_view(), name="branch-detail"),

    # 🎭 Роли
    path('roles/', RoleListAPIView.as_view(), name='role-list'),  # системные + кастомные
    path('roles/custom/', CustomRoleCreateAPIView.as_view(), name='custom-role-create'),
    path('roles/custom/<uuid:pk>/', CustomRoleDetailAPIView.as_view(), name='custom-role-detail'),
    
    
    path("scales/send-products/", send_products_to_scale),

    path('companies/', CompanyListAPIView.as_view(), name='company-list'),#Список всех компаний для Админа
    path('companies/<uuid:pk>/subscription/', CompanySubscriptionAdminAPIView.as_view(), name='company-subscription',
    ),
]
