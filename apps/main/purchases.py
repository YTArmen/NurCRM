"""
ТЗ 29.10 Б-96: покупки платных функций на сервере (для кассы, подписанные).

GET  /api/main/purchases/          → {purchases:[{feature, expires_at, source}], updated_at, company,
                                      alg:"HS256", kid, signature}
POST /api/main/purchases/activate/ {key} → то же + activated:{feature, expires_at, replayed}

Покупки = действующие CompanyAddon (ключи, пробный доступ, выдача поддержкой); тарифные функции сюда не входят.
Ключи и лимит подбора — те же, что у users/company/features/activate/ (FeatureActivationKey, FeatureKeyAttempt).

Подпись: HMAC-SHA256(hex) от канонического JSON (sort_keys, без пробелов) всего тела без поля `signature`.
Секрет: settings.PURCHASES_SIGNING_KEY (env), иначе ключ, выведенный из SECRET_KEY.
"""
import hashlib
import hmac
import json

from django.conf import settings
from django.db.models import Max
from django.utils import timezone
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.app_settings_views import CompanyFeatureActivateAPIView, _company_of
from apps.users.models import CompanyAddon

ALG = "HS256"


def _signing_secret():
    key = getattr(settings, "PURCHASES_SIGNING_KEY", "") or ""
    if key:
        return key.encode(), "env-1"
    derived = hmac.new(settings.SECRET_KEY.encode(), b"purchases-v1", hashlib.sha256).digest()
    return derived, "derived-1"


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign(body: dict) -> dict:
    secret, kid = _signing_secret()
    body = dict(body, alg=ALG, kid=kid)
    body["signature"] = hmac.new(secret, canonical(body), hashlib.sha256).hexdigest()
    return body


def build_purchases(company) -> dict:
    today = timezone.localdate()
    addons = [a for a in CompanyAddon.objects.filter(company=company).order_by("code") if a.is_effective(today)]
    last = CompanyAddon.objects.filter(company=company).aggregate(m=Max("updated_at"))["m"]
    return sign({
        "purchases": [
            {"feature": a.code, "expires_at": a.until.isoformat() if a.until else None,
             "source": a.source if a.source in ("key", "trial", "support") else "support"}
            for a in addons
        ],
        "updated_at": last.isoformat() if last else None,
        "company": str(company.id),
    })


class PurchasesListAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company_of(request.user)
        if company is None:
            raise PermissionDenied("Пользователь не привязан к компании.")
        return Response(build_purchases(company))


class PurchaseActivateAPIView(CompanyFeatureActivateAPIView):
    """Обёртка над активацией ключа: те же правила (403, 409 key_already_used, 429), код неверного ключа key_invalid."""

    force_strict = True

    def post(self, request):
        resp = super().post(request)
        data = resp.data if isinstance(resp.data, dict) else {}
        if resp.status_code == 400 and data.get("code") == "invalid_key":
            data["code"] = "key_invalid"
            data["detail"] = "Ключ не найден."
            return resp
        if resp.status_code == 200:
            company = _company_of(request.user)
            body = build_purchases(company)
            body["activated"] = {"feature": data.get("code"), "expires_at": data.get("until"),
                                 "replayed": bool(data.get("replayed"))}
            # activated не входит в подпись: подписывается список покупок
            return Response(body)
        return resp
