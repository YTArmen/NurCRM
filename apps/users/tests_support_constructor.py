import uuid
from decimal import Decimal

from django.test import override_settings
from rest_framework.test import APITestCase

from apps.users.models import Company, CompanyAddon, SubscriptionPlan, User
from apps.users.services_subscription import get_company_limits

TOKEN = "t" * 40
URL = "/api/users/support/company-features/"


@override_settings(NURCRM_SUPPORT_TOKEN=TOKEN)
class SupportConstructorTests(APITestCase):
    def setUp(self):
        self.custom = SubscriptionPlan.objects.create(name="По договору", price=Decimal("0"))
        self.start = SubscriptionPlan.objects.create(name="Старт", price=Decimal("1300"))
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.company = Company.objects.create(name="C", owner=self.owner, subscription_plan=self.custom)
        self.owner.company = self.company
        self.owner.save()

    def post(self, body, token=TOKEN):
        return self.client.post(URL, body, format="json", HTTP_X_SUPPORT_TOKEN=token)

    def test_sets_chosen_features_and_marker(self):
        CompanyAddon.objects.create(company=self.company, code="loyalty")
        res = self.post({"email": self.owner.email, "codes": ["branches", "analytics"], "max_employees": 5, "price": 2500})
        self.assertEqual(res.status_code, 200, res.data)
        active = set(CompanyAddon.objects.filter(company=self.company, active=True).values_list("code", flat=True))
        self.assertEqual(active, {"branches", "analytics", "constructor", "loyalty"})
        self.assertEqual(get_company_limits(self.company, "custom")["employees"]["max"], 5)

    def test_unchecked_feature_is_switched_off(self):
        self.post({"email": self.owner.email, "codes": ["branches", "clients"]})
        res = self.post({"email": self.owner.email, "codes": ["clients"], "until": "2027-01-01"})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(CompanyAddon.objects.get(company=self.company, code="branches").active)
        clients = CompanyAddon.objects.get(company=self.company, code="clients")
        self.assertTrue(clients.active)
        self.assertEqual(str(clients.until), "2027-01-01")
        self.assertIsNone(get_company_limits(self.company, "custom")["employees"]["max"])

    def test_wrong_token_and_disabled(self):
        self.assertEqual(self.post({"email": self.owner.email, "codes": []}, token="bad").status_code, 403)
        with override_settings(NURCRM_SUPPORT_TOKEN=""):
            self.assertEqual(self.post({"email": self.owner.email, "codes": []}).status_code, 404)
        self.assertFalse(CompanyAddon.objects.filter(company=self.company).exists())

    def test_only_custom_plan_and_known_codes(self):
        res = self.post({"email": self.owner.email, "codes": ["loyalty"]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("codes", res.data)
        self.company.subscription_plan = self.start
        self.company.save()
        res = self.post({"email": self.owner.email, "codes": ["branches"]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("email", res.data)
        self.assertEqual(self.post({"email": "nobody@t.kg", "codes": []}).status_code, 400)
        self.assertEqual(self.post({"email": self.owner.email, "codes": [], "max_employees": 0}).status_code, 400)
        self.assertFalse(CompanyAddon.objects.filter(company=self.company).exists())
