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


class EmployeeLimitTests(APITestCase):
    """Лимит сотрудников вместе с владельцем: «Старт» 3, «Стандарт» 10, «По договору» — из конструктора."""

    def setUp(self):
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.company = Company.objects.create(name="C", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        self.client.force_authenticate(self.owner)

    def use_plan(self, name):
        self.company.subscription_plan = SubscriptionPlan.objects.create(name=name, price=Decimal("1"))
        self.company.save()

    def add(self):
        return self.client.post("/api/users/employees/create/", {
            "email": f"e_{uuid.uuid4().hex[:6]}@t.kg", "first_name": "A", "last_name": "B", "role": "salesperson",
        }, format="json")

    def fill(self, total):
        for _ in range(total - 1):
            User.objects.create_user(email=f"s_{uuid.uuid4().hex[:6]}@t.kg", password="x", role="salesperson", company=self.company)

    def test_start_three(self):
        self.use_plan("Старт")
        self.fill(2)
        first = self.add()
        self.assertEqual(first.status_code, 201, first.data)
        res = self.add()
        self.assertEqual(res.status_code, 400)
        self.assertIn("3", str(res.data))
        self.assertEqual(get_company_limits(self.company, "start")["employees"], {"used": 3, "max": 3})

    def test_start_extra_employees_addon_lifts_limit(self):
        self.use_plan("Старт")
        self.fill(3)
        CompanyAddon.objects.create(company=self.company, code="extra-employees")
        self.assertEqual(self.add().status_code, 201)

    def test_standard_ten(self):
        self.use_plan("Стандарт")
        self.fill(9)
        self.assertEqual(self.add().status_code, 201)
        self.assertEqual(self.add().status_code, 400)

    def test_custom_from_constructor_or_unlimited(self):
        self.use_plan("Прайм")
        self.fill(12)
        self.assertEqual(self.add().status_code, 201)
        CompanyAddon.objects.create(company=self.company, code="constructor", note="конструктор support; max_employees=13")
        self.assertEqual(self.add().status_code, 400)
