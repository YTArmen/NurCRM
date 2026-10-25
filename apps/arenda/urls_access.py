"""Маршруты этапов 5–6 (доступы, уведомления, аналитика). Подключается из urls.py одной строкой."""
from django.urls import path

from . import views_access as va
from . import views_analytics as vn
from . import views_notify as vf

urlpatterns = [
    path("me/", va.ArendaMeView.as_view()),  # перекрывает views.MeView: as_user, дата смены пароля
    path("me/password/", va.PasswordView.as_view()),
    path("auth/login/", va.LoginView.as_view()),
    path("employees/<uuid:pk>/access/", va.EmployeeAccessView.as_view()),
    path("access/<uuid:user_id>/invite/", va.AccessInviteView.as_view()),
    path("audit/", va.AuditListView.as_view()),
    path("notifications/summary/", vf.SummaryView.as_view()),
    path("notifications/remind/", vf.RemindView.as_view()),
    path("reminders/", vf.ReminderListView.as_view()),
    path("reminder-rules/", vf.RulesView.as_view()),
    path("analytics/", vn.AnalyticsView.as_view()),
    path("analytics/<str:tab>/", vn.AnalyticsView.as_view()),
]
