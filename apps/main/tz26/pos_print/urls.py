from django.urls import path

from apps.main.tz26.pos_print import views

urlpatterns = [
    path("pos/print-jobs/", views.PrintJobListCreateAPIView.as_view(), name="pos-print-jobs"),
    path("pos/print-jobs/<uuid:pk>/", views.PrintJobDetailAPIView.as_view(), name="pos-print-job-detail"),
    path("pos/print-jobs/<uuid:pk>/claim/", views.PrintJobClaimAPIView.as_view(), name="pos-print-job-claim"),
    path("pos/print-jobs/<uuid:pk>/done/", views.PrintJobDoneAPIView.as_view(), name="pos-print-job-done"),
    path("pos/print-jobs/<uuid:pk>/fail/", views.PrintJobFailAPIView.as_view(), name="pos-print-job-fail"),
    path("pos/print-jobs/<uuid:pk>/cancel/", views.PrintJobCancelAPIView.as_view(), name="pos-print-job-cancel"),
]
