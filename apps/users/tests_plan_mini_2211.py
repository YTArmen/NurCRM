"""mini2211: «Мини» — сайт только для оплаты, claim ncl, device_key, профиль Базар, market_profile, volume_prices."""
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import Company, Industry, Sector, SubscriptionPlan, User


class PlanMini2211Tests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(email="mini2211@x.kg", password="pass12345!", role="owner")
        self.plan = SubscriptionPlan.objects.create(name="Мини", code="mini", price=Decimal("500"))
        self.company = Company.objects.create(name="Мини Ко", owner=self.owner, is_active=True, subscription_plan=self.plan)
        self.owner.company = self.company
        self.owner.save()

    def login(self, client=None, **extra):
        kw = {"HTTP_X_NURMARKET_CLIENT": client} if client else {}
        return APIClient().post("/api/users/auth/login/", {"email": "mini2211@x.kg", "password": "pass12345!", **extra},
                                format="json", **kw)

    def api(self, tok, client=None):
        c = APIClient()
        kw = {"HTTP_AUTHORIZATION": "Bearer " + tok}
        if client:
            kw["HTTP_X_NURMARKET_CLIENT"] = client
        c.credentials(**kw)
        return c

    def test_phone_needs_device_key(self):
        r = self.login("mobile/1.0.0")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "device_key_required")
        r = self.login("mobile/1.0.0", client_type="mobile", device_key="phone-one-k")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("plan_restriction", r.data)

    def test_kassa_cannot_pretend_mobile_by_client_type(self):
        r = self.login("kassa/1.0.0", client_type="mobile", device_key="phone-one-k")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data["code"], "plan_client_not_allowed")

    def test_web_only_billing(self):
        r = self.login()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["plan_restriction"]["client"], "web")
        c = self.api(r.data["access"])
        self.assertEqual(c.get("/api/users/company/").status_code, 200)
        self.assertEqual(c.get("/api/users/billing/invoices/").status_code, 200)
        self.assertEqual(c.get("/api/main/capabilities/").status_code, 200)
        r2 = c.get("/api/main/products/list/")
        self.assertEqual(r2.status_code, 403)
        self.assertEqual((r2.data["code"], r2.data["client"]), ("plan_client_not_allowed", "web"))
        self.assertEqual(c.patch("/api/users/company/", {"name": "X"}, format="json").status_code, 403)
        # подмена заголовка у токена с сайта не помогает
        self.assertEqual(self.api(r.data["access"], "mobile/1.0.0").get("/api/main/products/list/").status_code, 403)
        # после обновления входа claim сохраняется
        ref = APIClient().post("/api/users/auth/refresh/", {"refresh": r.data["refresh"]}, format="json")
        self.assertEqual(ref.status_code, 200)
        self.assertEqual(self.api(ref.data["access"], "mobile/1.0.0").get("/api/main/products/list/").status_code, 403)

    def test_phone_token_works(self):
        r = self.login("mobile/1.0.0", client_type="mobile", device_key="phone-one-k")
        self.assertEqual(self.api(r.data["access"], "mobile/1.0.0").get("/api/main/products/list/").status_code, 200)

    def test_other_plan_web_free(self):
        self.plan.code = "start"; self.plan.save()
        r = self.login()
        self.assertNotIn("plan_restriction", r.data)
        self.assertEqual(self.api(r.data["access"]).get("/api/main/products/list/").status_code, 200)

    def test_company_market_profile(self):
        c = APIClient(); c.force_authenticate(self.owner)
        d = c.get("/api/users/company/").data
        self.assertIn("market_profile", d)
        self.company.market_sphere = "bazaar_wholesale"; self.company.market_spheres = ["bazaar_wholesale"]
        self.company.save()
        c = APIClient(); c.force_authenticate(User.objects.get(pk=self.owner.pk))
        d = c.get("/api/users/company/").data
        self.assertEqual((d["market_profile"], d["market_profile_base"], d["market_profile_mode"]),
                         ("bazaar_wholesale", "bazaar", "wholesale"))

    def _register(self, **extra):
        ind = Industry.objects.create(name="Торговля")
        sec = Sector.objects.create(name="Маркет")
        sec.industries.add(ind)
        return APIClient().post("/api/users/auth/register/", {
            "email": "new2211@x.kg", "password": "pass12345!", "password2": "pass12345!",
            "first_name": "А", "last_name": "Б", "company_name": "Контейнер 5", "company_sector_id": str(sec.id),
            "subscription_plan_id": str(self.plan.id), **extra}, format="json")

    def test_register_mini_is_bazaar(self):
        r = self._register()
        self.assertEqual(r.status_code, 201, r.data)
        co = Company.objects.get(name="Контейнер 5")
        self.assertEqual(co.market_sphere, "bazaar_retail")
        from apps.users.market_modules import state
        self.assertEqual(state(co)["profile"], "bazaar_retail")

    def test_register_with_profile(self):
        r = self._register(market_profile="bazaar_wholesale")
        self.assertEqual(r.status_code, 201, r.data)
        from apps.users.market_modules import state
        self.assertEqual(state(Company.objects.get(name="Контейнер 5"))["profile"], "bazaar_wholesale")
        self.assertEqual(self._bad_profile().status_code, 400)

    def _bad_profile(self):
        return APIClient().post("/api/users/auth/register/", {
            "email": "bad2211@x.kg", "password": "pass12345!", "password2": "pass12345!", "company_name": "Y",
            "company_sector_id": "00000000-0000-0000-0000-000000000000", "subscription_plan_id": str(self.plan.id),
            "market_profile": "moon"}, format="json")
