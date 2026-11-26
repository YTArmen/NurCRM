"""
Вид магазина «просто так» — ТОЛЬКО для тестовой компании (17.11.2026, просьба Умара).

Тестовый аккаунт market@nur.kg (компания NBS) может сам переключать виды маркета в Настройки → «Вид магазина»,
без поддержки — чтобы проверять разделы разных видов. Остальным компаниям: GET → {"allowed": false}, POST → 403.
Список компаний — здесь, на сервере (фронт только показывает кнопки).

GET  /api/users/company/test-kind/ → {"allowed": bool, "spheres": [...], "flags": {...}}
POST /api/users/company/test-kind/ {"spheres": ["grocery", …]?, "flags": {"hardware_2026_11": true, …}?}
"""
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import Company, CompanyAddon
from apps.users.sphere_support_views import KIND_FLAGS, _payload

TEST_COMPANY_IDS = {
    "4b759359-2239-4e2a-a07e-f0e42a4dd19d",  # NBS — market@nur.kg
    "ad59b2ae-6c57-4db0-a5a5-83d205920b2a",  # ТЕСТ Автозапчасти — autoparts@nur.kg (23.11, просьба Умара)
}


# 23.11: «несколько видов» — основной вид задаёт оформление сайта; храним отметкой CompanyAddon kind-main-<вид>
MAIN_KINDS = ("grocery", "clothing", "services", "autoparts", "hardware", "electronics")
MAIN_PREFIX = "kind-main-"


def _main(company):
    a = CompanyAddon.objects.filter(company=company, code__startswith=MAIN_PREFIX, active=True).order_by("-updated_at").first()
    return a.code[len(MAIN_PREFIX):] if a else None


def _set_main(company, kind, note):
    for a in CompanyAddon.objects.select_for_update().filter(company=company, code__startswith=MAIN_PREFIX, active=True):
        if a.code != MAIN_PREFIX + kind:
            a.active, a.note = False, note
            a.save(update_fields=["active", "note", "updated_at"])
    a = CompanyAddon.objects.filter(company=company, code=MAIN_PREFIX + kind).first() or CompanyAddon(company=company, code=MAIN_PREFIX + kind)
    a.active, a.until, a.source, a.note = True, None, "support", note
    a.save()


def _company(request):
    cid = getattr(request.user, "company_id", None)
    return Company.objects.filter(id=cid).first() if cid and str(cid) in TEST_COMPANY_IDS else None


class TestCompanyKindAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _company(request)
        if company is None:
            return Response({"allowed": False})
        return Response({"allowed": True, **_payload(company), "main": _main(company)})

    def post(self, request):
        company = _company(request)
        if company is None:
            return Response({"detail": "Только для тестового аккаунта."}, status=status.HTTP_403_FORBIDDEN)
        data = request.data if isinstance(request.data, dict) else {}
        spheres = data.get("spheres")
        flags = data.get("flags") or {}
        allowed = set(Company.MarketSphere.values)
        if spheres is not None:
            if not isinstance(spheres, list) or not spheres or any(s not in allowed for s in spheres):
                return Response({"detail": "Нужен хотя бы один известный вид магазина."}, status=status.HTTP_400_BAD_REQUEST)
            spheres = list(dict.fromkeys(spheres))
        main = data.get("main")
        if main is not None and main not in MAIN_KINDS:
            return Response({"detail": "Неизвестный основной вид."}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(flags, dict) or any(k not in KIND_FLAGS for k in flags):
            return Response({"detail": "Неизвестный флаг."}, status=status.HTTP_400_BAD_REQUEST)
        note = f"Вид магазина (тест-аккаунт): {getattr(request.user, 'email', '')}, {timezone.localdate():%d.%m.%Y}"[:255]
        with transaction.atomic():
            if spheres is not None:
                company.market_spheres = spheres
                company.market_sphere = spheres[0]
                company.save(update_fields=["market_spheres", "market_sphere"])
            for code, on in flags.items():
                addon = CompanyAddon.objects.select_for_update().filter(company=company, code=code).first()
                if on:
                    addon = addon or CompanyAddon(company=company, code=code)
                    addon.active, addon.until, addon.source, addon.note = True, None, "support", note
                    addon.save()
                elif addon is not None and addon.active:
                    addon.active, addon.note = False, note
                    addon.save(update_fields=["active", "note", "updated_at"])
            if main is not None:
                _set_main(company, main, note)
        return Response({"allowed": True, **_payload(company), "main": _main(company)})
