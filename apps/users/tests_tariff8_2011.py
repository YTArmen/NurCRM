from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import Company, SubscriptionPlan, User


class Tariff8Tests(TestCase):
    def setUp(self):
        SubscriptionPlan.objects.create(name="Старт", code="start", price=Decimal("1300"))
        self.std = SubscriptionPlan.objects.create(name="Стандарт", code="standard", price=Decimal("3600"))
        self.owner = User.objects.create_user(email="t8@x.kg", password="pass12345!", role="owner")
        self.company = Company.objects.create(name="T8", owner=self.owner, is_active=True, subscription_plan=self.std,
                                              market_sphere="grocery")
        self.owner.company = self.company
        self.owner.save()
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def test_quote_on_standard_with_modules(self):
        r = self.api.post("/api/users/company/tariff/quote/",
                          {"packs": [], "modules": ["scales_labels", "serials", "repair"], "sphere": "grocery"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["base_plan"], r.data["base_price"], r.data["sphere"]), ("standard", "3600.00", "grocery"))
        self.assertEqual([m["id"] for m in r.data["included"]], ["scales_labels"])
        self.assertEqual([(m["id"], m["monthly"]) for m in r.data["extra"]], [("serials", "200.00"), ("repair", "300.00")])
        self.assertEqual((r.data["total_month"], r.data["total_once"]), ("4100.00", "0.00"))
        for old in ("monthly", "once", "base", "packs", "standard_cheaper", "standard_price"):
            self.assertIn(old, r.data)

    def test_quote_defaults_and_errors(self):
        r = self.api.post("/api/users/company/tariff/quote/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["sphere"], r.data["extra"]), ("grocery", []))
        self.assertEqual(r.data["total_month"], "3600.00")
        bad = self.api.post("/api/users/company/tariff/quote/", {"modules": ["nope"]}, format="json")
        self.assertEqual((bad.status_code, bad.data["code"]), (400, "module_unknown"))
        bad = self.api.post("/api/users/company/tariff/quote/", {"sphere": "zzz"}, format="json")
        self.assertEqual(bad.data["code"], "profile_unknown")

    def test_public_modules(self):
        r = APIClient().get("/api/public/modules/")
        self.assertEqual(r.status_code, 200)
        m = {x["id"]: x for x in r.data["modules"]}
        self.assertEqual((m["estimate"]["monthly"], m["quick_items"]["monthly"]), ("300.00", "100.00"))
        self.assertTrue(any(p["id"] == "stroymag" for p in r.data["profiles"]))

    def test_delete_module(self):
        url = "/api/users/company/tariff/modules/"
        self.assertEqual(self.api.delete(url + "nope/").status_code, 404)
        self.assertEqual(self.api.delete(url + "serials/").data["code"], "module_not_enabled")
        r = self.api.delete(url + "scales_labels/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn("scales_labels", r.data["modules"])
        self.assertIn("scales_labels", r.data["removed"])
        self.assertEqual(r.data["version"], 1)
        cashier = User.objects.create_user(email="t8c@x.kg", password="pass12345!", role="cashier", company=self.company)
        c = APIClient(); c.force_authenticate(cashier)
        self.assertEqual(c.delete(url + "quick_items/").status_code, 403)
