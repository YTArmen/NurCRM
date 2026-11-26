"""ТЗ Н-11 (23.10.2026): долг поставщику — список, оплата одной суммой, расход всегда."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.main.models import Client, ClientDeal, Product
from apps.users.models import Company

User = get_user_model()


class PayablesN11Tests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = User.objects.create_user(email=f"p-{uuid.uuid4().hex}@test.com", password="x",
                                             first_name="Айбек")
        self.company = Company.objects.create(name="Payables", owner=self.user)
        self.user.company = self.company
        self.user.save()
        self.api.force_authenticate(self.user)
        self.sup = Client.objects.create(company=self.company, full_name="Альфа", type=Client.StatusClient.SUPPLIERS)
        self.sup2 = Client.objects.create(company=self.company, full_name="Бета", type=Client.StatusClient.SUPPLIERS)
        self.today = date.today()

    def debt(self, client, amount, due):
        return ClientDeal.objects.create(
            company=self.company, client=client, title="d", kind=ClientDeal.Kind.DEBT,
            amount=Decimal(amount), debt_months=1, first_due_date=due,
        )

    def test_new_fields_ordering_overdue_pagination(self):
        self.debt(self.sup, "100.00", self.today - timedelta(days=5))
        self.debt(self.sup2, "300.00", self.today + timedelta(days=5))
        ClientDeal.objects.create(company=self.company, client=self.sup2, title="p",
                                  kind=ClientDeal.Kind.PREPAYMENT, amount=Decimal("40.00"))
        p = Product.objects.create(name="T", company=self.company, quantity=1, purchase_price=1, price=2)
        self.api.post(f"/api/main/suppliers/{self.sup2.id}/receipt/",
                      {"items": [{"product": str(p.id), "qty": 1, "purchase_price": "1"}], "payment_type": "debt"},
                      format="json")
        r = self.api.get("/api/main/suppliers/payables/")
        self.assertEqual([i["name"] for i in r.data["items"]], ["Бета", "Альфа"])
        beta = r.data["items"][0]
        self.assertEqual(beta["prepayment_total"], "40.00")
        self.assertEqual(beta["receipts_count"], 1)
        self.assertIsNotNone(beta["last_receipt_at"])
        self.assertEqual(r.data["items"][1]["receipts_count"], 0)
        self.assertNotIn("results", r.data)
        r = self.api.get("/api/main/suppliers/payables/?ordering=debt")
        self.assertEqual([i["name"] for i in r.data["items"]], ["Альфа", "Бета"])
        r = self.api.get("/api/main/suppliers/payables/?overdue=true")
        self.assertEqual([i["name"] for i in r.data["items"]], ["Альфа"])
        self.assertEqual(r.data["total"], "100.00")
        r = self.api.get("/api/main/suppliers/payables/?page=1&page_size=1")
        self.assertEqual(r.data["count"], 2)
        self.assertEqual(len(r.data["results"]), 1)
        self.assertEqual(r.data["total"], "400.00")
        self.assertIsNotNone(r.data["next"])


class SupplierPayN11Tests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = User.objects.create_user(email=f"o-{uuid.uuid4().hex}@test.com", password="x",
                                             first_name="Айбек", role="owner")
        self.company = Company.objects.create(name="Co", owner=self.user)
        self.user.company = self.company
        self.user.save()
        self.api.force_authenticate(self.user)
        self.sup = Client.objects.create(company=self.company, full_name="Альфа", type=Client.StatusClient.SUPPLIERS)
        self.cashbox = Cashbox.objects.create(name="Касса 1", company=self.company)
        self.shift = CashShift.objects.create(company=self.company, cashbox=self.cashbox, cashier=self.user,
                                              status=CashShift.Status.OPEN)
        d = self.today = date.today()
        self.deal1 = ClientDeal.objects.create(company=self.company, client=self.sup, title="a", kind="debt",
                                               amount=Decimal("1.00"), debt_months=1, first_due_date=d)
        self.deal2 = ClientDeal.objects.create(company=self.company, client=self.sup, title="b", kind="debt",
                                               amount=Decimal("1.00"), debt_months=1,
                                               first_due_date=d + timedelta(days=10))

    def test_deal_pay_writes_expense_cashflow_id_and_payment_fields(self):
        inst = self.deal1.installments.get()
        res = self.api.post(f"/api/main/clients/{self.sup.id}/deals/{self.deal1.id}/pay/",
                            {"installment_id": str(inst.id), "amount": "1.00",
                             "idempotency_key": str(uuid.uuid4()), "shift": str(self.shift.id)}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        cf = CashFlow.objects.get(type=CashFlow.Type.EXPENSE, source_kind=CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT)
        self.assertEqual(res.data["cashflow_id"], str(cf.id))
        self.assertEqual(cf.shift_id, self.shift.id)
        pay = res.data["payments"][0]
        self.assertEqual(pay["created_by_name"], "Айбек")
        self.assertEqual(pay["cashbox"], str(self.cashbox.id))
        self.assertEqual(pay["cashbox_name"], "Касса 1")
        self.assertEqual(pay["payment_method"], "cash")

    def test_deal_pay_without_cashbox_is_rejected_not_silent(self):
        # нет кассы — оплата не проходит целиком (без «платёж есть, расхода нет»)
        CashShift.objects.all().delete()
        Cashbox.objects.all().delete()
        inst = self.deal1.installments.get()
        res = self.api.post(f"/api/main/clients/{self.sup.id}/deals/{self.deal1.id}/pay/",
                            {"installment_id": str(inst.id), "amount": "1.00",
                             "idempotency_key": str(uuid.uuid4())}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(self.deal1.payments.exists())
        self.assertFalse(CashFlow.objects.exists())

    def test_pay_supplier_debt(self):
        h = {"HTTP_IDEMPOTENCY_KEY": "n11-key-1"}
        body = {"amount": "1.50", "method": "cash", "shift": str(self.shift.id)}
        r1 = self.api.post(f"/api/main/clients/{self.sup.id}/pay-supplier-debt/", body, format="json", **h)
        self.assertEqual(r1.status_code, 200, r1.data)
        self.assertEqual(r1.data["paid"], "1.50")
        self.assertEqual(r1.data["left"], "0.50")
        self.assertEqual(len(r1.data["applied"]), 2)
        self.assertEqual(len(r1.data["cashflows"]), 2)
        self.assertEqual(r1.data["paid_by"]["name"], "Айбек")
        self.assertFalse(r1.data["replayed"])
        r2 = self.api.post(f"/api/main/clients/{self.sup.id}/pay-supplier-debt/", body, format="json", **h)
        self.assertTrue(r2.data["replayed"])
        self.assertEqual(CashFlow.objects.filter(type=CashFlow.Type.EXPENSE).count(), 2)
        # без ключа и для не-поставщика — ошибка
        r3 = self.api.post(f"/api/main/clients/{self.sup.id}/pay-supplier-debt/", body, format="json")
        self.assertEqual(r3.data["code"], "idempotency_key_required")
        cust = Client.objects.create(company=self.company, full_name="Покупатель")
        r4 = self.api.post(f"/api/main/clients/{cust.id}/pay-supplier-debt/", body, format="json",
                           HTTP_IDEMPOTENCY_KEY="n11-key-2")
        self.assertEqual(r4.data["code"], "not_supplier")
