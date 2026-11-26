"""POST /api/users/company/addons/module/ {code, action: install|uninstall} — раздел из Маркетплейса (mm1811)."""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.menu_modules import MODULES, START_PRICE_MONTH, is_start
from apps.users.permissions import IsCompanyOwnerOrAdmin


class CompanyMenuModuleAPIView(APIView):
    permission_classes = [IsAuthenticated, IsCompanyOwnerOrAdmin]

    def post(self, request):
        from apps.users.models import CompanyAddon

        u = request.user
        company = getattr(u, "owned_company", None) or getattr(u, "company", None)
        if company is None:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_404_NOT_FOUND)
        code = str(request.data.get("code") or "").strip()
        action = str(request.data.get("action") or "install").strip()
        if code not in MODULES:
            return Response({"detail": "Такого раздела нет.", "code": "unknown_module"}, status=status.HTTP_400_BAD_REQUEST)
        if action not in ("install", "uninstall"):
            return Response({"detail": "action: install или uninstall."}, status=status.HTTP_400_BAD_REQUEST)
        addon = CompanyAddon.objects.filter(company=company, code=code).first()
        start = is_start(company)
        if action == "install":
            if addon and addon.is_effective():
                return Response({"code": code, "active": True})
            if start:
                return Response(
                    {"detail": f"На тарифе «Старт» раздел платный: {START_PRICE_MONTH} сом в месяц.", "code": "start_paid", "price_month": START_PRICE_MONTH},
                    status=status.HTTP_402_PAYMENT_REQUIRED,
                )
            if addon:
                addon.active, addon.until, addon.source = True, None, "install"
                addon.save(update_fields=["active", "until", "source", "updated_at"])
            else:
                CompanyAddon.objects.create(company=company, code=code, active=True, source="install")
            return Response({"code": code, "active": True})
        # uninstall: на «Старте» раздел оплачен через поддержку — сам не снимается
        if start and addon and addon.source not in ("install", "auto"):
            return Response({"detail": "Оплаченный раздел отключает поддержка.", "code": "paid_module"}, status=status.HTTP_409_CONFLICT)
        if addon and addon.active:
            addon.active = False
            addon.save(update_fields=["active", "updated_at"])
        return Response({"code": code, "active": False})
