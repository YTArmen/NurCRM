"""ТЗ Н-10 (23.10.2026): приход поставщика — оплата и долг одной операцией (опция create_deal)."""
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.main.models import Client, ClientDeal, Product, SupplierReceipt
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()


class ReceiptDealTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="Co", owner=self.owner, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.api.force_authenticate(user=self.owner)
        self.supplier = Client.objects.create(full_name="Пост Альфа", phone="+996700111222",
                                              type=Client.StatusClient.SUPPLIERS, company=self.company)
        self.product = Product.objects.create(
            name="Молоко", barcode="4600000000017", company=self.company, quantity=Decimal("5"),
            purchase_price=Decimal("1.00"), price=Decimal("2.00"),
        )
        self.cashbox = Cashbox.objects.create(name="Касса 1", company=self.company)
        self.shift = CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=self.owner, status=CashShift.Status.OPEN,
        )

    def post(self, extra, headers=None, qty=2, price="1.00"):
        return self.api.post(
            f"/api/main/suppliers/{self.supplier.id}/receipt/",
            {"items": [{"product": str(self.product.id), "qty": qty, "purchase_price": price}], **extra},
            format="json", **(headers or {}),
        )

    def test_old_request_does_not_create_deal(self):
        res = self.post({"payment_type": "debt"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(ClientDeal.objects.exists())
        self.assertNotIn("deal_id", res.data)

    def test_debt_creates_single_installment_deal(self):
        res = self.post({"payment_type": "debt", "create_deal": True, "first_due_date": "2030-11-20"})
        self.assertEqual(res.status_code, 200, res.data)
        deal = ClientDeal.objects.get(id=res.data["deal_id"])
        self.assertEqual(deal.amount, Decimal("2.00"))
        self.assertEqual(deal.installments.count(), 1)
        inst = deal.installments.get()
        self.assertEqual(str(inst.due_date), "2030-11-20")
        self.assertEqual(inst.amount, Decimal("2.00"))
        self.assertFalse(CashFlow.objects.exists())
        self.assertEqual(res.data["debt_amount"], "2.00")
        self.assertEqual(res.data["paid_amount"], "0.00")
        self.assertEqual(res.data["supplier_debt_total"], "2.00")
        self.assertIsNone(res.data["cashflow_id"])

    def test_prepayment_one_expense_and_deal_for_rest(self):
        res = self.post({"payment_type": "prepayment", "paid_amount": "0.50", "create_deal": True,
                         "first_due_date": "2030-11-20", "shift": str(self.shift.id)})
        self.assertEqual(res.status_code, 200, res.data)
        flows = CashFlow.objects.filter(source_id=res.data["id"])
        self.assertEqual(flows.count(), 1)
        cf = flows.get()
        self.assertEqual(cf.amount, Decimal("0.50"))
        self.assertEqual(cf.shift_id, self.shift.id)
        self.assertEqual(res.data["cashflow_id"], str(cf.id))
        deal = ClientDeal.objects.get(id=res.data["deal_id"])
        self.assertEqual(deal.amount, Decimal("1.50"))
        self.assertEqual(res.data["debt_amount"], "1.50")
        self.assertEqual(res.data["supplier_debt_total"], "1.50")

    def test_errors(self):
        r = self.api.post(f"/api/main/suppliers/{self.supplier.id}/receipt/",
                          {"items": [], "create_deal": True}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "items_empty")
        r = self.post({"payment_type": "weird", "create_deal": True})
        self.assertEqual(r.data["code"], "payment_type_invalid")
        r = self.post({"payment_type": "prepayment", "paid_amount": "9.00", "create_deal": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "paid_amount_exceeds_total")
        # откат: ни остатков, ни прихода, ни сделок
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("5"))
        self.assertFalse(SupplierReceipt.objects.exists())

    def test_idempotency_replay(self):
        body = {"payment_type": "debt", "create_deal": True, "first_due_date": "2030-11-20"}
        r1 = self.post(body, headers={"HTTP_IDEMPOTENCY_KEY": "key-n10-1"})
        r2 = self.post(body, headers={"HTTP_IDEMPOTENCY_KEY": "key-n10-1"})
        self.assertEqual(r1.data["id"], r2.data["id"])
        self.assertEqual(r1.data["deal_id"], r2.data["deal_id"])
        self.assertTrue(r2.data["replayed"])
        self.assertEqual(SupplierReceipt.objects.count(), 1)
        self.assertEqual(ClientDeal.objects.count(), 1)

    def test_list_fields_and_filters(self):
        d = self.post({"payment_type": "debt", "create_deal": True, "first_due_date": "2030-11-20"}).data
        c = self.post({"payment_type": "cash"}).data
        res = self.api.get("/api/main/suppliers/receipts/")
        rows = {r["id"]: r for r in res.data["results"]}
        self.assertEqual(rows[d["id"]]["payment_type"], "debt")
        self.assertEqual(rows[d["id"]]["deal_id"], d["deal_id"])
        self.assertEqual(rows[d["id"]]["debt_amount"], "2.00")
        self.assertEqual(rows[c["id"]]["payment_type"], "cash")
        self.assertEqual(rows[c["id"]]["paid_amount"], "2.00")
        res = self.api.get("/api/main/suppliers/receipts/?payment_type=debt")
        self.assertEqual([r["id"] for r in res.data["results"]], [d["id"]])
        self.assertEqual(res.data["count"], 1)
        self.assertEqual(self.api.get("/api/main/suppliers/receipts/?search=zzzzqqqq").data["count"], 0)
        self.assertEqual(self.api.get("/api/main/suppliers/receipts/?search=Молок").data["count"], 2)
        self.assertEqual(self.api.get("/api/main/suppliers/receipts/?search=4600000000017").data["count"], 2)
        self.assertEqual(self.api.get("/api/main/suppliers/receipts/?search=111222").data["count"], 2)
        det = self.api.get(f"/api/main/suppliers/receipts/{d['id']}/")
        self.assertEqual(det.data["payment_type"], "debt")
