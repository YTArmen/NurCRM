from django.urls import path

from apps.main.tz26.pos_print import held_views, scanner_views, views

urlpatterns = [
    # Б-69
    path("pos/print-jobs/", views.PrintJobListCreateAPIView.as_view(), name="pos-print-jobs"),
    path("pos/print-jobs/<uuid:pk>/", views.PrintJobDetailAPIView.as_view(), name="pos-print-job-detail"),
    path("pos/print-jobs/<uuid:pk>/claim/", views.PrintJobClaimAPIView.as_view(), name="pos-print-job-claim"),
    path("pos/print-jobs/<uuid:pk>/done/", views.PrintJobDoneAPIView.as_view(), name="pos-print-job-done"),
    path("pos/print-jobs/<uuid:pk>/fail/", views.PrintJobFailAPIView.as_view(), name="pos-print-job-fail"),
    path("pos/print-jobs/<uuid:pk>/cancel/", views.PrintJobCancelAPIView.as_view(), name="pos-print-job-cancel"),
    # Б-71
    path("pos/held-carts/", held_views.HeldCartListCreateAPIView.as_view(), name="pos-held-carts"),
    path("pos/held-carts/<uuid:pk>/", held_views.HeldCartDetailAPIView.as_view(), name="pos-held-cart-detail"),
    path("pos/held-carts/<uuid:pk>/take/", held_views.HeldCartTakeAPIView.as_view(), name="pos-held-cart-take"),
    path("pos/held-carts/<uuid:pk>/close/", held_views.HeldCartCloseAPIView.as_view(), name="pos-held-cart-close"),
    # Б-72
    path("pos/scanner-sessions/", scanner_views.ScannerSessionCreateAPIView.as_view(), name="pos-scanner-sessions"),
    path("pos/scanner-sessions/<uuid:pk>/events/", scanner_views.ScannerEventsAPIView.as_view(),
         name="pos-scanner-session-events"),
    path("pos/scanner-sessions/<uuid:pk>/close/", scanner_views.ScannerSessionCloseAPIView.as_view(),
         name="pos-scanner-session-close"),
    path("pos/scanner-sessions/<str:code>/scan/", scanner_views.ScannerScanAPIView.as_view(),
         name="pos-scanner-session-scan"),
]
