from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.users.models import Company, SubscriptionPlan, User


class PlanMini2011Tests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="mini2011@x.kg", password="pass12345!", role="owner")
        self.plan = SubscriptionPlan.objects.create(name="Мини", code="mini", price=Decimal("500"))
        self.company = Company.objects.create(name="Мини Ко", owner=self.owner, is_active=True, subscription_plan=self.plan)
        self.owner.company = self.company
        self.owner.save()

    def login(self, client=None, **extra):
        c = APIClient()
        kw = {"HTTP_X_NURMARKET_CLIENT": client} if client else {}
        return c.post("/api/users/auth/login/", {"email": "mini2011@x.kg", "password": "pass12345!", **extra}, format="json", **kw)

    def test_phone_ok_kassa_owner_403(self):
        self.assertEqual(self.login("mobile/1.0.0", device_key="phone-one-k").status_code, 200)
        self.assertEqual(self.login().status_code, 200)  # браузер (mini2211: только оплата)
        for hdr in ("kassa/1.0.0", "owner/1.0.0", "android-pos/1.0.0"):
            r = self.login(hdr)
            self.assertEqual(r.status_code, 403, hdr)
            self.assertEqual(r.data["code"], "plan_client_not_allowed")
        r = self.login(client_type="desktop_pos", device_key="pc-one-key")
        self.assertEqual(r.status_code, 403)

    def test_requests_and_refresh_blocked_from_kassa(self):
        tok = self.login("mobile/1.0.0", device_key="phone-one-k").data
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Bearer " + tok["access"], HTTP_X_NURMARKET_CLIENT="kassa/1.0.0")
        self.assertEqual(c.get("/api/users/auth/devices/").status_code, 403)
        cap = c.get("/api/main/capabilities/")
        self.assertEqual(cap.status_code, 200)
        self.assertEqual(cap.data["plan"]["code"], "mini")
        self.assertEqual(cap.data["plan"]["limits"], {"max_staff": 1, "max_devices": 1, "max_cashboxes": 1})
        self.assertEqual(cap.data["plan"]["allowed_clients"], ["mobile"])
        r = APIClient().post("/api/users/auth/refresh/", {"refresh": tok["refresh"]}, format="json",
                             HTTP_X_NURMARKET_CLIENT="owner/1.0.0")
        self.assertEqual(r.status_code, 403)
        r = APIClient().post("/api/users/auth/refresh/", {"refresh": tok["refresh"]}, format="json",
                             HTTP_X_NURMARKET_CLIENT="mobile/1.0.0")
        self.assertEqual(r.status_code, 200)

    def test_second_staff_and_cashbox(self):
        c = APIClient()
        c.force_authenticate(self.owner)
        r = c.post("/api/users/employees/create/", {"email": "e2011@x.kg", "first_name": "A", "last_name": "B"}, format="json")
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(r.data["code"], "plan_limit")
        self.assertEqual((r.data["limit"], r.data["max"], r.data["upgrade_plan"]), ("max_staff", 1, "start"))
        Cashbox.objects.create(name="Касса 1", company=self.company)
        r = c.post("/api/construction/cashboxes/", {"name": "Касса 2"}, format="json")
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(r.data["limit"], "max_cashboxes")

    def test_one_phone_even_for_owner(self):
        self.assertEqual(self.login("mobile/1.0.0", client_type="mobile", device_key="phone-one-k").status_code, 200)
        self.assertEqual(self.login("mobile/1.0.0", client_type="mobile", device_key="phone-two-k").status_code, 409)

    def test_other_plan_has_plan_block(self):
        self.plan.code = "standard"; self.plan.save()
        c = APIClient(); c.force_authenticate(self.owner)
        cap = c.get("/api/main/capabilities/", HTTP_X_NURMARKET_CLIENT="kassa/1.0.0")
        self.assertEqual(cap.data["plan"]["code"], "standard")
        self.assertEqual(cap.data["plan"]["limits"]["max_staff"], 10)
