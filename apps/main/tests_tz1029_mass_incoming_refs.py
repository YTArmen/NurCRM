"""ТЗ 29.10 Б-42: mass-incoming — markup_percent, category, supplier в строке."""
import uuid
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.main.models import Client, Product, ProductCategory
from apps.users.models import Company, User

URL = "/api/main/products/mass-incoming/"


class MassIncomingRefsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email=f"mr-{uuid.uuid4().hex}@t.com", password="x")
        self.company = Company.objects.create(name="MR", owner=self.user)
        self.user.company = self.company
        self.user.save()
        other_owner = User.objects.create_user(email=f"mo-{uuid.uuid4().hex}@t.com", password="x")
        self.other = Company.objects.create(name="Other", owner=other_owner)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.p = Product.objects.create(
            company=self.company, name="Сахар", price=Decimal("15.00"), purchase_price=Decimal("11.00"),
            quantity=Decimal("10"),
        )
        self.cat = ProductCategory.objects.create(company=self.company, name="Бакалея")
        self.other_cat = ProductCategory.objects.create(company=self.other, name="Чужая")
        self.sup = Client.objects.create(company=self.company, full_name="Пост", phone="+996555000111",
                                         type=Client.StatusClient.SUPPLIERS)
        self.other_sup = Client.objects.create(company=self.other, full_name="Чуж", phone="+996555000112",
                                               type=Client.StatusClient.SUPPLIERS)
        self.plain = Client.objects.create(company=self.company, full_name="Покуп", phone="+996555000113")

    def post(self, items, **kw):
        return self.api.post(URL, {"items": items, **kw}, format="json")

    def row(self, **kw):
        return {"product_id": str(self.p.id), "quantity": "1", **kw}

    def test_markup_without_price_computes_price(self):
        r = self.post([self.row(purchase_price="12.00", markup_percent="25")])
        self.assertEqual(r.status_code, 200, r.data)
        item = r.json()["items"][0]
        self.assertEqual((item["price"], item["price_changed"]), ("15.00", False))
        r = self.post([self.row(purchase_price="10.50", markup_percent="33,3")])
        item = r.json()["items"][0]
        self.assertEqual((item["price"], item["price_changed"]), ("14.00", True))  # 10.5*1.333=13.9965
        self.p.refresh_from_db()
        self.assertEqual(self.p.markup_percent, Decimal("33.3"))
        self.assertEqual(self.p.price, Decimal("14.00"))

    def test_markup_uses_current_purchase_when_not_sent(self):
        r = self.post([self.row(markup_percent="10")])
        self.assertEqual(r.json()["items"][0]["price"], "12.10")

    def test_explicit_price_wins_markup_recalculated(self):
        r = self.post([self.row(purchase_price="10.00", price="20.00", markup_percent="5")])
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.json()["items"][0]["price"], "20.00")
        self.p.refresh_from_db()
        self.assertEqual(self.p.markup_percent, Decimal("100.0000"))

    def test_markup_invalid(self):
        for bad in ("-5", "abc", "nan"):
            r = self.post([self.row(markup_percent=bad)])
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.json()["code"], "markup_invalid")
        r = self.post([self.row(markup_percent="-1"), self.row()], partial=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["errors"][0]["code"], "markup_invalid")
        self.assertEqual(len(r.json()["items"]), 1)

    def test_category_existing_and_only_when_passed(self):
        r = self.post([self.row()])
        self.p.refresh_from_db()
        self.assertIsNone(self.p.category_id)
        r = self.post([self.row(category=str(self.cat.id))])
        self.assertEqual(r.status_code, 200, r.data)
        self.p.refresh_from_db()
        self.assertEqual(self.p.category_id, self.cat.id)
        self.post([self.row()])
        self.p.refresh_from_db()
        self.assertEqual(self.p.category_id, self.cat.id)

    def test_category_not_found(self):
        for cid in (str(self.other_cat.id), str(uuid.uuid4()), "zzz"):
            r = self.post([self.row(category=cid)])
            self.assertEqual(r.status_code, 404, cid)
            self.assertEqual(r.json()["code"], "category_not_found")

    def test_supplier_linked_and_foreign_rejected(self):
        r = self.post([self.row(supplier=str(self.sup.id))])
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.json()["items"][0]["supplier"], str(self.sup.id))
        self.assertEqual(list(self.p.suppliers.all()), [self.sup])
        for sid in (str(self.other_sup.id), str(self.plain.id), str(uuid.uuid4())):
            r = self.post([self.row(supplier=sid)])
            self.assertEqual(r.json()["code"], "supplier_not_found", sid)

    def test_create_row_with_all_fields(self):
        r = self.post([{"barcode": "4600000000099", "name": "Соль", "quantity": "5", "from_global": False,
                        "purchase_price": "20", "markup_percent": "50", "category": str(self.cat.id),
                        "supplier": str(self.sup.id)}])
        self.assertEqual(r.status_code, 201, r.data)
        p = Product.objects.get(barcode="4600000000099")
        self.assertEqual((p.price, p.markup_percent, p.purchase_price), (Decimal("30.00"), Decimal("50"), Decimal("20")))
        self.assertEqual(p.category_id, self.cat.id)
        self.assertEqual(list(p.suppliers.all()), [self.sup])

    def test_create_row_price_still_required_without_markup(self):
        r = self.post([{"barcode": "4600000000098", "name": "Х", "quantity": "5", "from_global": False}])
        self.assertEqual(r.status_code, 400)
        r = self.post([{"barcode": "4600000000098", "name": "Х", "quantity": "5", "from_global": False,
                        "markup_percent": "10"}])
        self.assertEqual(r.status_code, 400)
