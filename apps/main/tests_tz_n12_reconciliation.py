"""ТЗ Н-12 (23.10.2026): поставщики — акт сверки «мы должны», дубли сделок, должники."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main.models import Client, ClientDeal, Product
from apps.users.models import Company

User = get_user_model()


class SupplierReconTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = User.objects.create_user(email=f"r-{uuid.uuid4().hex}@t.com", password="x", role="owner")
        self.company = Company.objects.create(name="NBS", owner=self.user)
        self.user.company = self.company
        self.user.save()
        self.api.force_authenticate(self.user)
        self.sup = Client.objects.create(company=self.company, full_name="Поставщик", type=Client.StatusClient.SUPPLIERS)
        self.cli = Client.objects.create(company=self.company, full_name="Покупатель")
        Cashbox.objects.create(name="К", company=self.company)

    def deal(self, client, amount, kind="debt", prepayment="0"):
        return ClientDeal.objects.create(
            company=self.company, client=client, title="Долги", kind=kind,
            amount=Decimal(amount), prepayment=Decimal(prepayment),
            debt_months=1 if kind == "debt" else None, first_due_date=date.today() if kind == "debt" else None,
        )

    def recon(self, client):
        s = (date.today() - timedelta(days=30)).isoformat()
        e = (date.today() + timedelta(days=1)).isoformat()
        return self.api.get(f"/api/main/clients/{client.id}/reconciliation/json/?start={s}&end={e}")

    def test_supplier_direction_and_amount_equal_kpis(self):
        d1 = self.deal(self.sup, "11700.00")
        self.deal(self.sup, "11700.00")
        self.deal(self.sup, "19000000.00", kind="sale")  # закупка сайта, оплачена — в долг не входит
        # частичная оплата и зачёт возврата
        inst = d1.installments.get()
        pay = self.api.post(f"/api/main/clients/{self.sup.id}/deals/{d1.id}/pay/",
                            {"installment_id": str(inst.id), "amount": "700.00",
                             "idempotency_key": str(uuid.uuid4())}, format="json")
        self.assertEqual(pay.status_code, 200, pay.data)
        kpi = self.api.get(f"/api/main/clients/{self.sup.id}/kpis/").data["debt"]["amount"]
        self.assertEqual(kpi, "22700.00")
        r = self.recon(self.sup)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["debt"]["debtor"], "NBS")
        self.assertEqual(r.data["debt"]["creditor"], "Поставщик")
        self.assertEqual(r.data["debt"]["amount"], kpi)
        self.assertEqual(r.data["direction"], "we_owe")
        refs = {e["ref_type"] for e in r.data["entries"]}
        self.assertIn("deal", refs)
        self.assertIn("installment_payment", refs)

    def test_receipt_and_return_rows(self):
        p = Product.objects.create(name="T", company=self.company, quantity=5, purchase_price=1, price=2)
        rc = self.api.post(f"/api/main/suppliers/{self.sup.id}/receipt/",
                           {"items": [{"product": str(p.id), "qty": 2, "purchase_price": "3.00"}],
                            "payment_type": "debt", "create_deal": True, "first_due_date": "2030-01-01"},
                           format="json")
        self.assertEqual(rc.status_code, 200, rc.data)
        r = self.recon(self.sup)
        rows = {e["ref_type"]: e for e in r.data["entries"]}
        self.assertEqual(rows["receipt"]["doc_amount"], "6.00")
        self.assertEqual(r.data["debt"]["amount"], "6.00")
        self.assertEqual(r.data["debt"]["debtor"], "NBS")

    def test_client_recon_unchanged_direction(self):
        self.deal(self.cli, "100.00")
        r = self.recon(self.cli)
        self.assertEqual(r.data["debt"]["debtor"], "Покупатель")
        self.assertEqual(r.data["debt"]["amount"], "100.00")

    def test_supplier_pdf_ok(self):
        self.deal(self.sup, "10.00")
        s = (date.today() - timedelta(days=30)).isoformat()
        e = (date.today() + timedelta(days=1)).isoformat()
        r = self.api.get(f"/api/main/clients/{self.sup.id}/reconciliation/?start={s}&end={e}")
        self.assertEqual(r.status_code, 200)


class DealIdempotencyTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = User.objects.create_user(email=f"d-{uuid.uuid4().hex}@t.com", password="x", role="owner")
        self.company = Company.objects.create(name="NBS", owner=self.user)
        self.user.company = self.company
        self.user.save()
        self.api.force_authenticate(self.user)
        self.sup = Client.objects.create(company=self.company, full_name="Поставщик", type=Client.StatusClient.SUPPLIERS)
        Cashbox.objects.create(name="К", company=self.company)

    def body(self, **kw):
        return {"title": "Долги", "kind": "debt", "amount": "11700.00", "client": str(self.sup.id),
                "debt_months": 1, "auto_schedule": True, "first_due_date": "2030-01-01", **kw}

    def test_same_key_same_deal(self):
        url = f"/api/main/clients/{self.sup.id}/deals/"
        a = self.api.post(url, self.body(), format="json", HTTP_IDEMPOTENCY_KEY="deal-k1")
        b = self.api.post(url, self.body(), format="json", HTTP_IDEMPOTENCY_KEY="deal-k1")
        self.assertEqual(a.status_code, 201, a.data)
        self.assertEqual(a.data["id"], b.data["id"])
        self.assertEqual(ClientDeal.objects.count(), 1)

    def test_without_key_old_behaviour(self):
        url = f"/api/main/clients/{self.sup.id}/deals/"
        self.api.post(url, self.body(), format="json")
        self.api.post(url, self.body(), format="json")
        self.assertEqual(ClientDeal.objects.count(), 2)

    def test_receipt_reference_deal_exists_409(self):
        p = Product.objects.create(name="T", company=self.company, quantity=5, purchase_price=1, price=2)
        rc = self.api.post(f"/api/main/suppliers/{self.sup.id}/receipt/",
                           {"items": [{"product": str(p.id), "qty": 1, "purchase_price": "1.00"}],
                            "payment_type": "debt"}, format="json").data
        url = f"/api/main/clients/{self.sup.id}/deals/"
        a = self.api.post(url, self.body(receipt=rc["id"]), format="json")
        self.assertEqual(a.status_code, 201, a.data)
        b = self.api.post(url, self.body(receipt=rc["id"]), format="json")
        self.assertEqual(b.status_code, 409)
        self.assertEqual(b.data["code"], "deal_exists")
        self.assertEqual(b.data["deal_id"], a.data["id"])
        # теперь приход «знает» свою сделку
        lst = self.api.get("/api/main/suppliers/receipts/").data["results"][0]
        self.assertEqual(lst["deal_id"], a.data["id"])
        self.assertEqual(lst["payment_type"], "debt")


class WithDebtsTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = User.objects.create_user(email=f"w-{uuid.uuid4().hex}@t.com", password="x", role="owner")
        self.company = Company.objects.create(name="NBS", owner=self.user)
        self.user.company = self.company
        self.user.save()
        self.api.force_authenticate(self.user)
        self.sup = Client.objects.create(company=self.company, full_name="Поставщик", type=Client.StatusClient.SUPPLIERS)
        self.cli = Client.objects.create(company=self.company, full_name="Покупатель")
        for c, a in ((self.sup, "50.00"), (self.cli, "70.00")):
            ClientDeal.objects.create(company=self.company, client=c, title="d", kind="debt",
                                      amount=Decimal(a), debt_months=1, first_due_date=date.today())

    def rows(self, qs=""):
        r = self.api.get(f"/api/main/clients/with-debts/{qs}")
        self.assertEqual(r.status_code, 200)
        return r.data["results"] if isinstance(r.data, dict) else r.data

    def test_type_filter(self):
        c = self.rows()
        self.assertEqual([r["id"] for r in c], [str(self.cli.id)])
        self.assertEqual(c[0]["debt_total"], "70.00")
        self.assertEqual(c[0]["direction"], "they_owe")
        s = self.rows("?type=suppliers")
        self.assertEqual([r["id"] for r in s], [str(self.sup.id)])
        self.assertEqual(s[0]["debt_total"], "50.00")
        self.assertEqual(s[0]["direction"], "we_owe")
