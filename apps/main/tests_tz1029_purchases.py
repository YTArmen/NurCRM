"""ТЗ 29.10 Б-96 (покупки) и Б-01 (версия сервера в capabilities)."""
import hashlib
import hmac
import json
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.main import purchases
from apps.users.models import Company, CompanyAddon, FeatureActivationKey
from core import capabilities

User = get_user_model()
LIST = "/api/main/purchases/"
ACT = "/api/main/purchases/activate/"


def mk(role="owner", company=None):
    u = User.objects.create_user(email=f"u_{uuid.uuid4().hex[:6]}@t.com", password="p", role=role)
    if company is None:
        company = Company.objects.create(name="C" + uuid.uuid4().hex[:4], owner=u)
    u.company = company
    u.save()
    api = APIClient()
    api.force_authenticate(u)
    return company, api


def verify(body, secret):
    b = dict(body)
    sig = b.pop("signature")
    b.pop("activated", None)
    raw = json.dumps(b, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(secret, raw, hashlib.sha256).hexdigest() == sig


class PurchasesTests(TestCase):
    def setUp(self):
        self.company, self.api = mk()
        self.key = FeatureActivationKey.objects.create(code="ai", days=30)

    def test_empty_and_signature(self):
        r = self.api.get(LIST)
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual(b["purchases"], [])
        self.assertEqual(b["alg"], "HS256")
        self.assertTrue(b["kid"])
        secret, _ = purchases._signing_secret()
        self.assertTrue(verify(b, secret))

    @override_settings(PURCHASES_SIGNING_KEY="topsecret")
    def test_env_key_used(self):
        b = self.api.get(LIST).json()
        self.assertEqual(b["kid"], "env-1")
        self.assertTrue(verify(b, b"topsecret"))
        b["purchases"].append({"feature": "x", "expires_at": None, "source": "key"})
        self.assertFalse(verify(b, b"topsecret"))

    def test_activate_then_list(self):
        r = self.api.post(ACT, {"key": self.key.key}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        b = r.json()
        self.assertEqual(b["activated"]["feature"], "ai")
        self.assertFalse(b["activated"]["replayed"])
        self.assertEqual([p["feature"] for p in b["purchases"]], ["ai"])
        self.assertEqual(b["purchases"][0]["source"], "key")
        self.assertTrue(b["purchases"][0]["expires_at"])
        secret, _ = purchases._signing_secret()
        self.assertTrue(verify(b, secret))
        g = self.api.get(LIST).json()
        self.assertEqual(g["purchases"], b["purchases"])
        self.assertEqual(g["signature"], b["signature"])
        # повтор той же компанией
        r2 = self.api.post(ACT, {"key": self.key.key}, format="json")
        self.assertTrue(r2.json()["activated"]["replayed"])

    def test_expired_not_listed(self):
        CompanyAddon.objects.create(company=self.company, code="old", until="2020-01-01", source="key")
        self.assertEqual(self.api.get(LIST).json()["purchases"], [])

    def test_invalid_key(self):
        r = self.api.post(ACT, {"key": "NOPE"}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "key_invalid"))

    def test_other_company_409(self):
        self.api.post(ACT, {"key": self.key.key}, format="json")
        _, api2 = mk()
        r = api2.post(ACT, {"key": self.key.key}, format="json")  # без заголовка клиента: всё равно 409
        self.assertEqual((r.status_code, r.json()["code"]), (409, "key_already_used"))

    def test_brute_force_429(self):
        for i in range(10):
            self.assertEqual(self.api.post(ACT, {"key": f"BAD{i}"}, format="json").status_code, 400)
        r = self.api.post(ACT, {"key": "BAD11"}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "too_many_attempts"))

    def test_only_owner_or_admin_activate(self):
        _, cashier = mk(role="employee", company=self.company)
        self.assertEqual(cashier.post(ACT, {"key": self.key.key}, format="json").status_code, 403)
        self.assertEqual(cashier.get(LIST).status_code, 200)  # смотреть может любой сотрудник
        self.assertEqual(APIClient().get(LIST).status_code, 401)


class VersionTests(TestCase):
    def setUp(self):
        capabilities._VERSION_CACHE.clear()
        self.addCleanup(capabilities._VERSION_CACHE.clear)

    def test_env(self):
        with mock.patch.dict("os.environ", {"SERVER_VERSION": "2026.10.29"}):
            self.assertEqual(capabilities.server_version(), "2026.10.29")

    def test_git_or_dev_never_raises(self):
        with mock.patch.dict("os.environ", {"SERVER_VERSION": "", "NURCRM_SERVER_VERSION": ""}), \
                mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(capabilities._detect_version(), "dev")

    def test_features_added(self):
        _, api = mk()
        f = api.get("/api/main/capabilities/").json()["features"]
        self.assertIn("etag_304", f)
        self.assertIn("purchases", f)
