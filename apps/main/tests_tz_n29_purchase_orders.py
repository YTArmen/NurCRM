"""ТЗ Н-29: заказ поставщику как документ + приход с order_id."""
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.main.models import Client, Product, PurchaseOrder
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()
URL = "/api/main/purchase-orders/"


class PurchaseOrderTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="Co", owner=self.owner, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.api.force_authenticate(user=self.owner)
        self.sup = Client.objects.create(full_name="Альфа", phone="+996700111222",
                                         type=Client.StatusClient.SUPPLIERS, company=self.company)
        self.sup2 = Client.objects.create(full_name="Бета", phone="+996700111223",
                                          type=Client.StatusClient.SUPPLIERS, company=self.company)
        self.p1 = Product.objects.create(name="Молоко", company=self.company, quantity=Decimal("0"),
                                         purchase_price=Decimal("1"), price=Decimal("2"))
        self.p2 = Product.objects.create(name="Хлеб", company=self.company, quantity=Decimal("0"),
                                         purchase_price=Decimal("1"), price=Decimal("2"))

    def create(self, supplier=None, lines=None, **kw):
        body = {"supplier": str(( supplier or self.sup).id), "note": "н",
                "lines": lines if lines is not None else [{"product": str(self.p1.id), "qty": "2", "purchase_price": "10"}], **kw}
        return self.api.post(URL, body, format="json")

    def receipt(self, qty, order_id=None, product=None, supplier=None):
        body = {"items": [{"product": str((product or self.p1).id), "qty": qty}]}
        if order_id:
            body["order_id"] = order_id
        return self.api.post(f"/api/main/suppliers/{(supplier or self.sup).id}/receipt/", body, format="json")

    def test_create_numbers_and_fields(self):
        r1 = self.create()
        self.assertEqual(r1.status_code, 201, r1.data)
        r2 = self.create()
        self.assertEqual((r1.data["number"], r2.data["number"]), (1, 2))
        self.assertEqual(r1.data["status"], "draft")
        self.assertEqual(r1.data["lines"][0]["received_qty"], "0.000")
        self.assertEqual(r1.data["created_by"], str(self.owner.id))
        self.assertIsNone(r1.data["sent_at"])

    def test_receipt_partial_then_received(self):
        oid = self.create(status="sent").data["id"]
        r = self.receipt(1, oid)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["order_id"], oid)
        d = self.api.get(f"{URL}{oid}/").data
        self.assertEqual((d["status"], d["lines"][0]["received_qty"]), ("partial", "1.000"))
        self.assertTrue(d["sent_at"])
        self.assertEqual(self.receipt(1, oid).status_code, 200)
        d = self.api.get(f"{URL}{oid}/").data
        self.assertEqual((d["status"], d["lines"][0]["received_qty"]), ("received", "2.000"))
        # закрытый заказ больше не принимает
        r = self.receipt(1, oid)
        self.assertEqual((r.status_code, r.data["code"]), (400, "order_closed"))
        # приход виден с order_id
        lst = self.api.get("/api/main/suppliers/receipts/").data["results"]
        self.assertEqual({x["order_id"] for x in lst}, {oid})

    def test_receipt_wrong_order(self):
        oid = self.create().data["id"]
        r = self.receipt(1, oid, supplier=self.sup2)
        self.assertEqual((r.status_code, r.data["code"]), (400, "order_supplier_mismatch"))
        r = self.receipt(1, str(uuid.uuid4()))
        self.assertEqual((r.status_code, r.data["code"]), (400, "order_not_found"))
        r = self.receipt(1, "bad")
        self.assertEqual((r.status_code, r.data["code"]), (400, "order_not_found"))

    def test_receipt_without_order_unchanged(self):
        r = self.receipt(3)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data["order_id"])

    def test_receipt_other_product_does_not_touch_order(self):
        oid = self.create().data["id"]
        self.receipt(5, oid, product=self.p2)
        self.assertEqual(self.api.get(f"{URL}{oid}/").data["status"], "draft")

    def test_filters_and_pagination(self):
        self.create()
        self.create(supplier=self.sup2, status="sent")
        self.assertEqual(self.api.get(URL).data["count"], 2)
        self.assertEqual(self.api.get(URL + f"?supplier={self.sup2.id}").data["count"], 1)
        self.assertEqual(self.api.get(URL + "?status=sent").data["count"], 1)
        self.assertEqual(self.api.get(URL + "?search=Молоко").data["count"], 2)
        self.assertEqual(self.api.get(URL + "?search=Бета").data["count"], 1)
        self.assertEqual(self.api.get(URL + "?date_from=2000-01-01&date_to=2999-01-01").data["count"], 2)
        self.assertEqual(self.api.get(URL + "?status=zzz").status_code, 400)
        d = self.api.get(URL + "?limit=1").data
        self.assertEqual((d["count"], len(d["results"])), (2, 1))
        self.assertIsNotNone(d["next"])

    def test_patch_delete(self):
        oid = self.create().data["id"]
        r = self.api.patch(f"{URL}{oid}/", {"status": "sent", "note": "новый",
                           "lines": [{"product": str(self.p2.id), "qty": "5"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["status"], r.data["note"], len(r.data["lines"])), ("sent", "новый", 1))
        self.assertTrue(r.data["sent_at"])
        self.assertEqual(self.api.delete(f"{URL}{oid}/").status_code, 400)
        self.api.patch(f"{URL}{oid}/", {"status": "cancelled"}, format="json")
        self.assertEqual(self.api.patch(f"{URL}{oid}/", {"lines": []}, format="json").status_code, 400)
        self.assertEqual(self.api.delete(f"{URL}{oid}/").status_code, 204)
        self.assertEqual(self.api.get(f"{URL}{oid}/").status_code, 404)

    def test_company_isolation_and_validation(self):
        oid = self.create().data["id"]
        o2 = User.objects.create_user(email=f"x_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        c2 = Company.objects.create(name="C2", owner=o2, subscription_plan=SubscriptionPlan.objects.first())
        o2.company = c2
        o2.save()
        other = APIClient()
        other.force_authenticate(user=o2)
        self.assertEqual(other.get(f"{URL}{oid}/").status_code, 404)
        self.assertEqual(other.get(URL).data["count"], 0)
        r = other.post(URL, {"supplier": str(self.sup.id), "lines": []}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.create(lines=[{"product": str(self.p1.id), "qty": "0"}]).status_code, 400)
