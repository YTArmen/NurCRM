"""
Вид магазина из support.nurcrm.kg (16.11.2026): поддержка находит компанию и одним нажатием меняет её вид.

Доступ — как у промокодов: платформенный админ ИЛИ X-Support-Key (nginx support /nursphere/ добавляет ключ сам).

GET  /api/users/platform-admin/company-kind/?q=<название | почта | slug | id>  → {"results": [компания, …]} (до 15)
POST /api/users/platform-admin/company-kind/
     {"company_id": "<uuid>", "spheres": ["grocery", …], "flags": {"hardware_2026_11": true, …}}
     → компания. Виды — Company.market_spheres (основной — первый). Флаги — CompanyAddon (бессрочно, source=support).
"""
import uuid
from urllib.parse import unquote

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import Company, CompanyAddon, User
from apps.users.promo_views import IsPlatformAdminOrSupportKey

# Что поддержка включает «видом магазина» поверх основного вида (флаги компании).
KIND_FLAGS = {
    "hardware_2026_11": "Хозмаг и стройматериалы",
    "electronics_2026_11": "Техника: серийники и гарантия",
    "kassa-repair": "Ремонт",
    "kassa-trade_in": "Trade-in и скупка",
}


def _payload(company):
    today = timezone.localdate()
    addons = {a.code: a for a in CompanyAddon.objects.filter(company=company, code__in=list(KIND_FLAGS))}
    spheres = company.market_spheres or ([company.market_sphere] if company.market_sphere else [])
    owner = getattr(company, "owner", None)
    return {
        "id": str(company.id),
        "name": company.name,
        "slug": company.slug,
        "owner_email": getattr(owner, "email", "") or "",
        "spheres": spheres,
        "flags": {c: bool(addons.get(c) and addons[c].is_effective(today)) for c in KIND_FLAGS},
    }


class SupportCompanyKindAPIView(APIView):
    permission_classes = [IsPlatformAdminOrSupportKey]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Response({"results": []})
        cond = Q(name__icontains=q) | Q(slug__iexact=q) | Q(owner__email__iexact=q)
        try:
            cond |= Q(id=uuid.UUID(q))
        except ValueError:
            pass
        ids = list(Company.objects.filter(cond).values_list("id", flat=True)[:15])
        # аккаунт сотрудника (почта) → его компания
        emp = User.objects.filter(email__iexact=q, company__isnull=False).values_list("company_id", flat=True).first()
        if emp and emp not in ids:
            ids.insert(0, emp)
        rows = Company.objects.filter(id__in=ids).select_related("owner").order_by("name")
        return Response({"results": [_payload(c) for c in rows]})

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            company = Company.objects.filter(id=uuid.UUID(str(data.get("company_id")))).first()
        except ValueError:
            company = None
        if company is None:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_404_NOT_FOUND)
        spheres = data.get("spheres")
        flags = data.get("flags") or {}
        allowed = set(Company.MarketSphere.values)
        if spheres is not None:
            if not isinstance(spheres, list) or any(s not in allowed for s in spheres):
                return Response({"detail": "Неизвестный вид магазина."}, status=status.HTTP_400_BAD_REQUEST)
            spheres = list(dict.fromkeys(spheres))
        if not isinstance(flags, dict) or any(k not in KIND_FLAGS for k in flags):
            return Response({"detail": "Неизвестный флаг."}, status=status.HTTP_400_BAD_REQUEST)
        who = unquote(request.headers.get("X-Support-Operator", ""))[:60] or getattr(request.user, "email", "") or "поддержка"
        note = f"Вид магазина: {who}, {timezone.localdate():%d.%m.%Y}"
        with transaction.atomic():
            if spheres is not None:
                company.market_spheres = spheres
                company.market_sphere = spheres[0] if spheres else None
                company.save(update_fields=["market_spheres", "market_sphere"])
            for code, on in flags.items():
                addon = CompanyAddon.objects.select_for_update().filter(company=company, code=code).first()
                if on:
                    addon = addon or CompanyAddon(company=company, code=code)
                    addon.active, addon.until, addon.source, addon.note = True, None, "support", note[:255]
                    addon.save()
                elif addon is not None and addon.active:
                    addon.active, addon.note = False, note[:255]
                    addon.save(update_fields=["active", "note", "updated_at"])
        return Response(_payload(company))
