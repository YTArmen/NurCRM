"""ТЗ Н-32 (3): ключи функций — одноразовые, защита от подбора, пробный доступ, source."""
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from django.test import TestCase

from apps.users.models import (
    Company, CompanyAddon, Feature, FeatureActivationKey, FeatureTrial, SubscriptionPlan,
)

User = get_user_model()


def mk(name):
    owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
    plan = SubscriptionPlan.objects.create(name=f"P{uuid.uuid4().hex[:4]}", price=Decimal("1.00"))
    company = Company.objects.create(name=name, owner=owner, subscription_plan=plan)
    owner.company = company
    owner.save()
    api = APIClient()
    api.force_authenticate(owner)
    api.defaults["HTTP_X_NURMARKET_CLIENT"] = "kassa/1.17.61"  # новый клиент: 409/429 (Б-02)
    return company, api, plan


class FeatureKeyTests(TestCase):
    URL = "/api/users/company/features/activate/"

    def setUp(self):
        self.c1, self.a1, self.plan = mk("C1")
        self.c2, self.a2, _ = mk("C2")
        self.key = FeatureActivationKey.objects.create(code="ai", days=30)

    def test_bind_replay_and_other_company(self):
        r = self.a1.post(self.URL, {"key": self.key.key}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn("replayed", r.json())
        until = r.json()["until"]
        r = self.a1.post(self.URL, {"key": self.key.key}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["replayed"])
        self.assertEqual(r.json()["until"], until)  # повтор не продлевает
        r = self.a2.post(self.URL, {"key": self.key.key}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "key_already_used"))

    def test_brute_force_429(self):
        codes = [self.a1.post(self.URL, {"key": f"BAD-{i}"}, format="json").status_code for i in range(10)]
        self.assertEqual(set(codes), {400})
        r = self.a1.post(self.URL, {"key": "BAD-11"}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "too_many_attempts"))
        # даже верный ключ в блокировке не принимается
        self.assertEqual(self.a1.post(self.URL, {"key": self.key.key}, format="json").status_code, 429)
        # другая компания с другого IP не задета
        self.assertEqual(self.a2.post(self.URL, {"key": self.key.key}, format="json", REMOTE_ADDR="10.1.1.1").status_code, 200)

    def test_features_source(self):
        f = Feature.objects.create(name="clients")
        self.plan.features.add(f)
        self.a1.post(self.URL, {"key": self.key.key}, format="json")
        CompanyAddon.objects.create(company=self.c1, code="voice")  # source по умолчанию support
        d = {x["code"]: x for x in self.a1.get("/api/users/company/features/").json()}
        self.assertEqual(d["clients"]["source"], "tariff")
        self.assertEqual(d["ai"]["source"], "key")
        self.assertEqual(d["voice"]["source"], "support")


class FeatureTrialTests(TestCase):
    URL = "/api/users/company/features/trial/"

    def setUp(self):
        self.c1, self.a1, _ = mk("T1")
        FeatureActivationKey.objects.create(code="ai", days=30)

    def test_trial_once(self):
        r = self.a1.post(self.URL, {"code": "ai"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(set(r.json()), {"code", "expires_at"})
        d = {x["code"]: x for x in self.a1.get("/api/users/company/features/").json()}
        self.assertEqual(d["ai"]["source"], "trial")
        # после окончания пробного — второй раз нельзя
        CompanyAddon.objects.filter(company=self.c1, code="ai").update(active=False)
        r = self.a1.post(self.URL, {"code": "ai"}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "trial_used"))
        self.assertEqual(FeatureTrial.objects.count(), 1)

    def test_unknown_feature(self):
        r = self.a1.post(self.URL, {"code": "nope"}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "unknown_feature"))
