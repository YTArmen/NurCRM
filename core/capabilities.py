"""
Признак версии API для касс и мобилок (единое ТЗ бэкенду 26.10.2026, Б-01).

GET /api/main/capabilities/ — любой вошедший сотрудник:
    {"api": "2026-10", "server_version": "...", "features": [...], "company_flags": {...},
     "client": "kassa/1.17.61", "client_is_new": true}
GET /api/main/client-versions/?days=7 — поддержка и суперпользователь: компания → версии клиентов.

Список features — только добавлением: касса включает новое поведение по наличию кода.
"""
import os
from datetime import timedelta

from django.db.models import Max, Sum
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from core.api_compat import API_VERSION, client_label, company_flags, is_new_client

FEATURES = (
    "shift_report_v2", "cashbox_drawer_cash", "idempotency_lookup", "price_stable",
    "negative_custom_rejected", "client_documents", "bot_owner_server", "salary_payouts",
    "supplier_pay_debt", "access_codes", "changes_feed", "receipt_create_deal", "rentals_paged",
    "strict_permissions", "route_not_found", "client_header", "employee_limits", "support_constructor",
    "events_stream",  # ТЗ 29.10 Б-102: WebSocket ws/main/events/ (SSE-адрес отвечает use_websocket)
    "etag_304", "purchases",
)


_VERSION_CACHE = []


def _detect_version():
    """ТЗ 29.10 Б-01: SERVER_VERSION (env) → /home/nur/.server_version → git short hash → "dev"."""
    for name in ("SERVER_VERSION", "NURCRM_SERVER_VERSION"):
        v = (os.getenv(name) or "").strip()
        if v:
            return v
    try:
        with open("/home/nur/.server_version", encoding="utf-8") as fh:
            v = fh.read().strip()
        if v:
            return v
    except OSError:
        pass
    try:
        import subprocess

        from django.conf import settings

        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(settings.BASE_DIR),
                             capture_output=True, text=True, timeout=3)
        v = (out.stdout or "").strip()
        if out.returncode == 0 and v:
            return v
    except Exception:
        pass
    return "dev"


def server_version():
    if not _VERSION_CACHE:
        _VERSION_CACHE.append(_detect_version())
    return _VERSION_CACHE[0]


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


class CapabilitiesAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request.user)
        return Response({
            "api": API_VERSION,
            "server_version": server_version(),
            "features": list(FEATURES),
            "company_flags": company_flags(company),
            "client": client_label(request) or None,
            "client_is_new": is_new_client(request),
        })


class IsSupportOrSuperuser(permissions.BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and (u.is_superuser or getattr(u, "is_platform_admin", False)))


class ClientVersionsReportAPIView(APIView):
    permission_classes = [IsSupportOrSuperuser]

    def get(self, request):
        from apps.users.models import ClientVersionSeen

        try:
            days = max(1, min(int(request.query_params.get("days", 7)), 90))
        except ValueError:
            days = 7
        since = timezone.now() - timedelta(days=days)
        rows = (
            ClientVersionSeen.objects.filter(last_seen_at__gte=since)
            .values("company_id", "company__name", "client")
            .annotate(last_seen_at=Max("last_seen_at"), hits=Sum("hits"))
            .order_by("company__name", "client")
        )
        companies = {}
        for r in rows:
            c = companies.setdefault(str(r["company_id"]), {"company": str(r["company_id"]), "name": r["company__name"], "clients": []})
            c["clients"].append({"client": r["client"], "last_seen_at": r["last_seen_at"], "hits": r["hits"]})
        return Response({"days": days, "companies": list(companies.values())})
