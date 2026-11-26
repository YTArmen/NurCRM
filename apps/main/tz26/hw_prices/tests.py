# Тесты куска hw_prices: python manage.py test apps.main.tz26.hw_prices --settings=core.settings_test_sqlite
import uuid
from decimal import Decimal

from rest_framework.test import APIClient

from apps.main.models import Client, ClientObject, PriceLevel, Product
from apps.possync.tests import PossyncBase
from apps.users.models import User


class HwBase(PossyncBase):
    def user(self, **flags):
        u = User.objects.create_user(email=f"u{uuid.uuid4().hex[:6]}@t.kg", password="x", company=self.company, **flags)
        api = APIClient()
        api.force_authenticate(u)
        return u, api

    def client_obj(self, **kw):
        return Client.objects.create(company=self.company, full_name="Прораб Иван", phone="+996700000001", **kw)


class PriceLevelTests(HwBase):
    def test_system_levels_created_lazily_on_get(self):
        self.assertEqual(PriceLevel.objects.filter(company=self.company).count(), 0)
        r = self.api.get("/api/main/price-levels/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([x["code"] for x in r.json()], ["retail", "wholesale"])
        self.assertTrue(all(x["is_system"] for x in r.json()))
        self.api.get("/api/main/price-levels/")
        self.assertEqual(PriceLevel.objects.filter(company=self.company).count(), 2)

    def test_create_custom_up_to_five_then_limit(self):
        for i in range(5):
            r = self.api.post("/api/main/price-levels/", {"name": f"Ур{i}"}, format="json")
            self.assertEqual(r.status_code, 201, r.content)
            self.assertFalse(r.json()["is_system"])
        r = self.api.post("/api/main/price-levels/", {"name": "Шестой"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "price_levels_limit")

    def test_code_from_name_and_patch_name_only(self):
        r = self.api.post("/api/main/price-levels/", {"name": "Прораб"}, format="json")
        lid = r.json()["id"]
        self.assertEqual(r.json()["code"], "prorab")
        r = self.api.patch(f"/api/main/price-levels/{lid}/", {"name": "Бригада"}, format="json")
        self.assertEqual(r.json()["name"], "Бригада")
        self.assertEqual(r.json()["code"], "prorab")
        r = self.api.patch(f"/api/main/price-levels/{lid}/", {"code": "x"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_cashier_reads_but_cannot_create(self):
        _, api = self.user()
        self.assertEqual(api.get("/api/main/price-levels/").status_code, 200)
        r = api.post("/api/main/price-levels/", {"name": "Прораб"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "permission_denied")
        _, api2 = self.user(can_view_products=True)
        self.assertEqual(api2.post("/api/main/price-levels/", {"name": "Прораб"}, format="json").status_code, 201)

    def test_other_company_level_not_visible(self):
        other = User.objects.create_user(email="x@t.kg", password="x")
        from apps.users.models import Company
        co2 = Company.objects.create(name="Чужая", owner=other)
        lv = PriceLevel.objects.create(company=co2, code="zz", name="Чужой")
        self.assertEqual(self.api.patch(f"/api/main/price-levels/{lv.id}/", {"name": "x"}, format="json").status_code, 404)

    def test_delete_system_and_used(self):
        self.api.get("/api/main/price-levels/")
        sys_id = PriceLevel.objects.get(company=self.company, code="retail").id
        self.assertEqual(self.api.delete(f"/api/main/price-levels/{sys_id}/").status_code, 400)
        lv = self.api.post("/api/main/price-levels/", {"name": "Прораб"}, format="json").json()
        self.client_obj(price_level_id=lv["id"])
        r = self.api.delete(f"/api/main/price-levels/{lv['id']}/")
        self.assertEqual(r.status_code, 409)


class ClientFieldsTests(HwBase):
    def test_owner_sets_fields_and_reads_back(self):
        lv = self.api.post("/api/main/price-levels/", {"name": "Прораб"}, format="json").json()
        c = self.client_obj()
        r = self.api.patch(
            f"/api/main/clients/{c.id}/",
            {"price_level": lv["id"], "credit_limit": "1000.00", "default_due_days": 14, "client_kind": "foreman"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["price_level"], lv["id"])
        self.assertEqual(body["credit_limit"], "1000.00")
        self.assertEqual(body["default_due_days"], 14)
        self.assertEqual(body["client_kind"], "foreman")

    def test_bad_kind_rejected(self):
        c = self.client_obj()
        r = self.api.patch(f"/api/main/clients/{c.id}/", {"client_kind": "king"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_credit_limit_only_owner(self):
        _, api = self.user(can_view_clients=True)
        c = self.client_obj()
        r = api.patch(f"/api/main/clients/{c.id}/", {"credit_limit": "500"}, format="json")
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(r.json()["code"], "permission_denied")

    def test_price_level_of_other_company_rejected(self):
        from apps.users.models import Company
        other = User.objects.create_user(email="y@t.kg", password="x")
        co2 = Company.objects.create(name="Чужая", owner=other)
        lv = PriceLevel.objects.create(company=co2, code="zz", name="Чужой")
        c = self.client_obj()
        r = self.api.patch(f"/api/main/clients/{c.id}/", {"price_level": lv.id}, format="json")
        self.assertEqual(r.status_code, 400)


class ClientObjectsTests(HwBase):
    def test_crud_and_isolation(self):
        c = self.client_obj()
        r = self.api.post(f"/api/main/clients/{c.id}/objects/", {"name": "Коттедж", "address": "ул. 1"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        oid = r.json()["id"]
        self.assertEqual(r.json()["is_active"], True)
        r = self.api.patch(f"/api/main/clients/{c.id}/objects/{oid}/", {"is_active": False}, format="json")
        self.assertEqual(r.json()["is_active"], False)
        self.assertEqual(len(self.api.get(f"/api/main/clients/{c.id}/objects/").json()), 1)
        self.assertEqual(len(self.api.get(f"/api/main/clients/{c.id}/objects/?active=1").json()), 0)
        c2 = self.client_obj()
        self.assertEqual(self.api.patch(f"/api/main/clients/{c2.id}/objects/{oid}/", {"name": "x"}, format="json").status_code, 404)

    def test_needs_can_view_clients(self):
        c = self.client_obj()
        _, api = self.user()
        self.assertEqual(api.get(f"/api/main/clients/{c.id}/objects/").status_code, 403)
        _, api2 = self.user(can_view_clients=True)
        self.assertEqual(api2.get(f"/api/main/clients/{c.id}/objects/").status_code, 200)
