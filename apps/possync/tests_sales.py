"""
Тесты серверных пунктов для веб-кассы (лимит долга, упаковки, строки чеков, даты, удаления, график, возврат).
    python manage.py test apps.possync.tests_sales --settings=core.settings_test_sqlite
"""
from decimal import Decimal

from apps.main.models import Client, ClientDeal, Sale
from apps.users.models import CompanyAppSettings

from .models import ClientDebtLimit
from .tests import PossyncBase


class SalesBase(PossyncBase):
    def checkout(self, key, items=None, payment=None, **extra):
        body = {
            "items": items or [{"product": str(self.product.id), "qty": "1", "price": "100.00"}],
            "payment": payment or {"method": "cash", "received": "1000.00"},
        }
        body.update(extra)
        return self.api.post("/api/main/pos/checkout/", body, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def debt_sale(self, key, client, amount="100.00", **extra):
        qty = str(Decimal(amount) / Decimal("100.00"))
        return self.checkout(
            key,
            items=[{"product": str(self.product.id), "qty": qty, "price": "100.00"}],
            payment={"method": "debt"},
            client=str(client.id),
            **extra,
        )

    def new_client(self, name="Должник"):
        return Client.objects.create(company=self.company, full_name=name, phone="+996555111222")


class DebtLimitTests(SalesBase):
    def test_client_limit_blocks_second_debt(self):
        self.open_shift()
        c = self.new_client()
        r = self.api.patch(f"/api/main/clients/{c.id}/", {"debt_limit": "500"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["debt_limit"], "500.00")
        self.assertEqual(r.data["debt_available"], "500.00")

        r = self.debt_sale("d1", c, "300.00")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.api.get(f"/api/main/clients/{c.id}/")
        self.assertEqual(r.data["debt_available"], "200.00")

        r = self.debt_sale("d2", c, "300.00")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "debt_limit_exceeded")
        self.assertEqual(r.data["limit"], 500)
        self.assertEqual(r.data["current"], 300)
        self.assertIn("Лимит долга клиента 500", r.data["detail"])
        # откат: продажа и списание остатка не остались
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("47.000"))

        # в пределах лимита — проходит
        r = self.debt_sale("d3", c, "200.00")
        self.assertEqual(r.status_code, 201, r.data)

    def test_no_limit_by_default_and_null_resets(self):
        self.open_shift()
        c = self.new_client()
        r = self.api.get(f"/api/main/clients/{c.id}/")
        self.assertIsNone(r.data["debt_limit"])
        self.assertIsNone(r.data["debt_available"])
        self.assertEqual(self.debt_sale("n1", c, "900.00").status_code, 201)
        self.api.patch(f"/api/main/clients/{c.id}/", {"debt_limit": "100"}, format="json")
        self.api.patch(f"/api/main/clients/{c.id}/", {"debt_limit": None}, format="json")
        self.assertIsNone(ClientDebtLimit.objects.get(client=c).limit)
        self.assertEqual(self.debt_sale("n2", c, "100.00").status_code, 201)

    def test_prepayment_reduces_debt_part(self):
        self.open_shift()
        c = self.new_client()
        ClientDebtLimit.objects.create(client=c, company=self.company, limit=Decimal("200"))
        r = self.checkout(
            "m1",
            items=[{"product": str(self.product.id), "qty": "5", "price": "100.00"}],
            payment={"method": "debt", "cash_amount": "200.00"},  # предоплата 200, в долг 300
            client=str(c.id),
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "debt_limit_exceeded")

    def test_cash_sale_ignores_limit(self):
        self.open_shift()
        c = self.new_client()
        ClientDebtLimit.objects.create(client=c, company=self.company, limit=Decimal("1"))
        self.assertEqual(self.checkout("c1", client=str(c.id)).status_code, 201)

    def test_cashier_limit_from_debt_access(self):
        self.open_shift()
        CompanyAppSettings.objects.create(
            company=self.company, app="nurmarket-mobile",
            settings={"DebtAccess": [{"user": str(self.owner.id), "mode": "limit", "max_receipts": 1, "max_sum": 1000}]},
        )
        c1, c2 = self.new_client("А"), self.new_client("Б")
        self.assertEqual(self.debt_sale("k1", c1, "300.00").status_code, 201)
        r = self.debt_sale("k2", c2, "100.00")  # второй открытый чек
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "debt_limit_exceeded")
        self.assertEqual(r.data["limit"], 1)
        self.assertEqual(r.data["current"], 1)

    def test_cashier_sum_limit_and_deny(self):
        self.open_shift()
        row = CompanyAppSettings.objects.create(
            company=self.company, app="nurmarket-mobile",
            settings={"DebtAccess": [{"user": str(self.owner.id), "mode": "limit", "max_sum": 400}]},
        )
        c = self.new_client()
        self.assertEqual(self.debt_sale("s1", c, "300.00").status_code, 201)
        r = self.debt_sale("s2", c, "200.00")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["limit"], 400)
        self.assertEqual(r.data["current"], 300)
        row.settings = {"DebtAccess": [{"user": str(self.owner.id), "mode": "deny"}]}
        row.save()
        r = self.debt_sale("s3", c, "100.00")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "debt_forbidden")
