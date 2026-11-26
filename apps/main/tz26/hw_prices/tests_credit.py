# Тесты клиента-прораба Б-56: python manage.py test apps.main.tz26.hw_prices.tests_credit --settings=core.settings_test_sqlite
import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.main.models import ClientDeal, ClientObject, Sale
from apps.main.tz26.hw_prices.tests import HwBase
from apps.users.models import CompanyAddon

NEW = {"HTTP_X_NURMARKET_CLIENT": "kassa/1.17.61"}


class CreditBase(HwBase):
    def setUp(self):
        super().setUp()
        self.product.quantity = Decimal("1000")
        self.product.save()  # цена 100
        self.foreman = self.client_obj(credit_limit=Decimal("1000.00"), client_kind="foreman")
        self.open_shift()
        CompanyAddon.objects.create(company=self.company, code="hardware_2026_11")

    def debt_sale(self, qty, headers=NEW, client=None, **extra):
        client = client or self.foreman
        body = {
            "items": [{"product": str(self.product.id), "qty": str(qty), "price": "100.00"}],
            "client": str(client.id),
            "payment": {"method": "debt"},
        }
        body.update(extra)
        return self.api.post(
            "/api/main/pos/checkout/", body, format="json", HTTP_IDEMPOTENCY_KEY=f"k-{uuid.uuid4()}", **(headers or {})
        )


class CreditLimitTests(CreditBase):
    """Приёмка Х5: лимит 1000, долг 900, ещё 200 -> 409; override владельцем проходит."""

    def test_over_limit_409_and_nothing_created(self):
        self.assertEqual(self.debt_sale(9).status_code, 201)
        n = Sale.objects.count()
        r = self.debt_sale(2)
        self.assertEqual(r.status_code, 409, r.content)
        body = r.json()
        self.assertEqual(body["code"], "credit_limit_exceeded")
        self.assertEqual(body["data"], {"limit": "1000.00", "debt": "900.00", "requested": "200.00"})
        self.assertEqual(Sale.objects.count(), n)
        self.assertEqual(ClientDeal.objects.filter(client=self.foreman).count(), 1)

    def test_exactly_at_limit_passes(self):
        self.assertEqual(self.debt_sale(9).status_code, 201)
        self.assertEqual(self.debt_sale(1).status_code, 201)

    def test_owner_override_passes(self):
        self.debt_sale(9)
        r = self.debt_sale(2, override_credit_limit=True)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Sale.objects.filter(client=self.foreman, status=Sale.Status.DEBT).count(), 2)

    def test_old_kassa_not_limited(self):
        self.debt_sale(9)
        self.assertEqual(self.debt_sale(2, headers=None).status_code, 201)

    def test_flag_off_not_limited(self):
        CompanyAddon.objects.filter(company=self.company).delete()
        self.debt_sale(9)
        self.assertEqual(self.debt_sale(2).status_code, 201)

    def test_no_limit_no_check(self):
        c = self.client_obj()
        self.assertEqual(self.debt_sale(50, client=c).status_code, 201)

    def test_cash_sale_not_checked(self):
        body = {
            "items": [{"product": str(self.product.id), "qty": "50", "price": "100.00"}],
            "client": str(self.foreman.id),
            "payment": {"method": "cash", "received": "100000"},
        }
        r = self.api.post("/api/main/pos/checkout/", body, format="json", HTTP_IDEMPOTENCY_KEY="k-" + uuid.uuid4().hex, **NEW)
        self.assertEqual(r.status_code, 201, r.content)

    def test_direct_debt_limit_and_override_right(self):
        url = f"/api/main/clients/{self.foreman.id}/debts/"
        self.assertEqual(self.api.post(url, {"amount": "900"}, format="json", **NEW).status_code, 201)
        r = self.api.post(url, {"amount": "200"}, format="json", **NEW)
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json()["code"], "credit_limit_exceeded")
        _, cashier = self.user()
        r = cashier.post(url, {"amount": "200", "override_credit_limit": True}, format="json", **NEW)
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(r.json()["required"], "can_override_credit_limit")
        boss = self.api
        r = boss.post(url, {"amount": "200", "override_credit_limit": True}, format="json", **NEW)
        self.assertEqual(r.status_code, 201, r.content)


class ObjectsAndDueTests(CreditBase):
    def setUp(self):
        super().setUp()
        self.obj_a = ClientObject.objects.create(client=self.foreman, name="Дом А", address="ул. 1")
        self.obj_b = ClientObject.objects.create(client=self.foreman, name="Дом Б")
        self.foreman.credit_limit = None
        self.foreman.save()

    def test_sale_with_object_and_debtors_by_objects(self):
        r = self.debt_sale(3, object=self.obj_a.id)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["object"], self.obj_a.id)
        self.assertEqual(Sale.objects.get(pk=r.json()["id"]).client_object_id, self.obj_a.id)
        url = f"/api/main/clients/{self.foreman.id}/debts/"
        self.api.post(url, {"amount": "200", "object": self.obj_b.id}, format="json")
        self.api.post(url, {"amount": "100"}, format="json")
        rows = self.api.get("/api/main/clients/debtors/?include=objects").json()
        row = next(x for x in rows if x["client"] == str(self.foreman.id))
        self.assertEqual(row["debt_total"], "600.00")
        by = {o["object"]: o for o in row["objects"]}
        self.assertEqual(by[self.obj_a.id]["debt_total"], "300.00")
        self.assertEqual(by[self.obj_a.id]["name"], "Дом А")
        self.assertEqual(by[self.obj_b.id]["debt_total"], "200.00")
        self.assertEqual(by[None]["debt_total"], "100.00")
        plain = self.api.get("/api/main/clients/debtors/").json()
        self.assertNotIn("objects", plain[0])

    def test_foreign_object_rejected(self):
        other = self.client_obj()
        foreign = ClientObject.objects.create(client=other, name="Чужой")
        r = self.debt_sale(1, object=foreign.id)
        self.assertEqual(r.status_code, 404, r.content)
        self.assertEqual(r.json()["code"], "object_not_found")
        self.assertEqual(Sale.objects.count(), 0)

    def test_default_due_days(self):
        self.foreman.default_due_days = 14
        self.foreman.save()
        r = self.debt_sale(1)
        self.assertEqual(r.status_code, 201, r.content)
        deal = ClientDeal.objects.get(sale_id=r.json()["id"])
        self.assertEqual(deal.first_due_date, timezone.localdate() + timedelta(days=14))
        r = self.api.post(f"/api/main/clients/{self.foreman.id}/debts/", {"amount": "10"}, format="json", **NEW)
        self.assertEqual(r.json()["due_date"], (timezone.localdate() + timedelta(days=14)).isoformat())
        r = self.api.post(f"/api/main/clients/{self.foreman.id}/debts/", {"amount": "10"}, format="json")
        self.assertEqual(r.json()["due_date"], (timezone.localdate() + timedelta(days=30)).isoformat())
