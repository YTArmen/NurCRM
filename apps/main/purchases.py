"""
ТЗ 29.10 Б-96: покупки платных функций на сервере (для кассы, подписанные).

GET  /api/main/purchases/          → {purchases:[{feature, expires_at, source}], updated_at, company,
                                      alg:"HS256", kid, signature}
POST /api/main/purchases/activate/ {key} → то же + activated:{feature, expires_at, replayed}

Покупки = действующие CompanyAddon (ключи, пробный доступ, выдача поддержкой); тарифные функции сюда не входят.
Ключи и лимит подбора — те же, что у users/company/features/activate/ (FeatureActivationKey, FeatureKeyAttempt).

Подпись (30.10.2026, по просьбе кассы): Ed25519, если на сервере есть ключи, иначе прежний HS256.
  Подписывается канонический JSON всего тела без поля `signature`: UTF-8, ключи по возрастанию,
  без пробелов (separators ",", ":"), не-ASCII как есть (ensure_ascii=False).
  Ed25519: alg "Ed25519", kid, signature — base64 (64 байта), signed_at — время подписи (ISO).
  Ключи: каталог settings.PURCHASES_KEYS_DIR (по умолчанию /etc/nurcrm/purchases-keys, вне git):
    keys.json {"active": "<kid>", "keys": [{"kid", "not_before", "not_after"}], "revoked_kids": []}
    <kid>.pem — закрытый ключ (PKCS8, права 600); для старых ключей можно оставить только <kid>.pub (raw 32 байта, base64).
GET /api/main/purchases/keys/ — открытые ключи: {keys: [{kid, alg, public_key, not_before, not_after, active}], revoked_kids}.
HS256 (запасной): HMAC-SHA256(hex), секрет PURCHASES_SIGNING_KEY или выведенный из SECRET_KEY.
"""
import base64
import hashlib
import hmac
import json
import os

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
ED_ALG = "Ed25519"


def _keys_dir():
    return getattr(settings, "PURCHASES_KEYS_DIR", "") or os.getenv("PURCHASES_KEYS_DIR", "/etc/nurcrm/purchases-keys")


def load_keyring():
    """{"active", "keys": [...], "revoked_kids": [...]} из keys.json; нет файла — None (подпись HS256)."""
    path = os.path.join(_keys_dir(), "keys.json")
    try:
        with open(path, encoding="utf-8") as f:
            ring = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(ring, dict) or not ring.get("active"):
        return None
    ring.setdefault("keys", [])
    ring.setdefault("revoked_kids", [])
    return ring


def _private_key(kid):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    with open(os.path.join(_keys_dir(), f"{kid}.pem"), "rb") as f:
        return load_pem_private_key(f.read(), password=None)


def public_key_b64(kid):
    """Открытый ключ kid: raw 32 байта в base64 (из <kid>.pem или <kid>.pub)."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    try:
        raw = _private_key(kid).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw).decode()
    except OSError:
        with open(os.path.join(_keys_dir(), f"{kid}.pub"), encoding="ascii") as f:
            return f.read().strip()


def _signing_secret():
    key = getattr(settings, "PURCHASES_SIGNING_KEY", "") or ""
    if key:
        return key.encode(), "env-1"
    derived = hmac.new(settings.SECRET_KEY.encode(), b"purchases-v1", hashlib.sha256).digest()
    return derived, "derived-1"


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign(body: dict) -> dict:
    ring = load_keyring()
    if ring is not None and ring["active"] not in ring["revoked_kids"]:
        kid = ring["active"]
        body = {k: v for k, v in body.items() if k != "signature"}
        body.update(alg=ED_ALG, kid=kid, signed_at=timezone.now().isoformat())
        body["signature"] = base64.b64encode(_private_key(kid).sign(canonical(body))).decode()
        return body
    secret, kid = _signing_secret()
    body = dict(body, alg=ALG, kid=kid)
    body["signature"] = hmac.new(secret, canonical(body), hashlib.sha256).hexdigest()
    return body


def build_purchases_body(company) -> dict:
    """Тело ответа без подписи."""
    today = timezone.localdate()
    addons = [a for a in CompanyAddon.objects.filter(company=company).order_by("code") if a.is_effective(today)]
    last = CompanyAddon.objects.filter(company=company).aggregate(m=Max("updated_at"))["m"]
    return {
        "purchases": [
            {"feature": a.code, "expires_at": a.until.isoformat() if a.until else None,
             "source": a.source if a.source in ("key", "trial", "support") else "support"}
            for a in addons
        ],
        "updated_at": last.isoformat() if last else None,
        "company": str(company.id),
    }


def build_purchases(company) -> dict:
    return sign(build_purchases_body(company))


class PurchaseKeysAPIView(APIView):
    """GET purchases/keys/ — открытые ключи Ed25519 (без входа). Касса доверяет зашитому ключу, этот список — для ротации."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        ring = load_keyring()
        if ring is None:
            return Response({"keys": [], "revoked_kids": []})
        keys = []
        for k in ring["keys"]:
            kid = k.get("kid")
            if not kid or kid in ring["revoked_kids"]:
                continue
            try:
                pub = public_key_b64(kid)
            except OSError:
                continue
            keys.append({"kid": kid, "alg": ED_ALG, "public_key": pub, "not_before": k.get("not_before"),
                         "not_after": k.get("not_after"), "active": kid == ring["active"]})
        return Response({"keys": keys, "revoked_kids": list(ring["revoked_kids"])})


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
            body = build_purchases_body(company)
            body["activated"] = {"feature": data.get("code"), "expires_at": data.get("until"),
                                 "replayed": bool(data.get("replayed"))}
            ring = load_keyring()
            if ring is None:
                # HS256 (прежнее поведение): activated не входит в подпись
                activated = body.pop("activated")
                return Response(dict(sign(body), activated=activated))
            # Ed25519: подписано всё тело, включая activated
            return Response(sign(body))
        return resp
