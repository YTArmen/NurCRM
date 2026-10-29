"""ТЗ 29.10 Б-42: products/mass-incoming/ — unit строки, ответ по строкам, частичный успех."""
import uuid
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.main.models import Product
from apps.users.models import Company, User

URL = "/api/main/products/mass-incoming/"


class MassIncomingUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email=f"mi-{uuid.uuid4().hex}@t.com", password="x")
        self.company = Company.objects.create(name="MI", owner=self.user)
        self.user.company = self.company
        self.user.save()
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.p = Product.objects.create(
            company=self.company, name="Сахар", price=Decimal("15.00"), purchase_price=Decimal("11.00"),
            quantity=Decimal("10"), unit="шт.",
        )

    def post(self, body, key=None):
        kw = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.api.post(URL, body, format="json", **kw)

    def test_unit_applied_to_existing_product(self):
        r = self.post({"items": [{"product_id": str(self.p.id), "quantity": "2", "unit": "кг",
                                  "purchase_price": "1.00", "client_row_id": "r1"}]})
        self.assertEqual(r.status_code, 200, r.data)
        row = r.json()["items"][0]
        self.assertEqual(row["unit"], "кг")
        self.assertEqual(row["client_row_id"], "r1")
        self.assertEqual(row["product_id"], str(self.p.id))  # старые поля на месте
        self.assertEqual(row["quantity"], "12.000")
        self.assertEqual((row["purchase_price"], row["price"], row["price_changed"]), ("1.00", "15.00", False))
        self.assertEqual(r.json()["errors"], [])
        self.assertFalse(r.json()["replayed"])
        self.p.refresh_from_db()
        self.assertEqual((self.p.unit, self.p.unit_code), ("кг", "kg"))

    def test_unit_code_and_aliases(self):
        for raw, label, code in (("kg", "кг", "kg"), ("шт", "шт.", "pcs"), ("L", "л", "l"), ("м", "м", "m")):
            r = self.post({"items": [{"product_id": str(self.p.id), "quantity": "1", "unit": raw}]})
            self.assertEqual(r.status_code, 200, r.data)
            self.p.refresh_from_db()
            self.assertEqual((self.p.unit, self.p.unit_code), (label, code), raw)

    def test_row_without_unit_keeps_unit(self):
        r = self.post({"items": [{"product_id": str(self.p.id), "quantity": "1"}]})
        self.assertEqual(r.json()["items"][0]["unit"], "шт.")

    def test_unit_applied_to_created_product(self):
        r = self.post({"items": [{"barcode": "4600000000011", "name": "Мука", "price": "50", "quantity": "3",
                                  "from_global": False, "unit": "кг", "client_row_id": "n1"}]})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.json()["items"][0]["unit"], "кг")
        prod = Product.objects.get(company=self.company, barcode="4600000000011")
        self.assertEqual((prod.unit, prod.unit_code), ("кг", "kg"))
        self.assertTrue(r.json()["created"])

    def test_unknown_unit_atomic_batch_rolls_back(self):
        other = Product.objects.create(company=self.company, name="Соль", price=Decimal("5.00"), quantity=Decimal("1"))
        r = self.post({"items": [
            {"product_id": str(other.id), "quantity": "4"},
            {"product_id": str(self.p.id), "quantity": "1", "unit": "бочка", "client_row_id": "bad"},
        ]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "unit_unknown")
        err = r.json()["errors"][0]
        self.assertEqual((err["index"], err["client_row_id"], err["code"]), (1, "bad", "unit_unknown"))
        other.refresh_from_db()
        self.assertEqual(other.quantity, Decimal("1"))  # пачка не применена

    def test_partial_mode_skips_bad_row(self):
        other = Product.objects.create(company=self.company, name="Соль", price=Decimal("5.00"), quantity=Decimal("1"))
        r = self.post({"partial": True, "items": [
            {"product_id": str(other.id), "quantity": "4", "client_row_id": "ok"},
            {"product_id": str(self.p.id), "quantity": "1", "unit": "бочка", "client_row_id": "bad"},
            {"product_id": str(uuid.uuid4()), "quantity": "1", "client_row_id": "ghost"},
        ]})
        self.assertEqual(r.status_code, 200, r.data)
        d = r.json()
        self.assertEqual([i["client_row_id"] for i in d["items"]], ["ok"])
        self.assertEqual({(e["index"], e["code"]) for e in d["errors"]}, {(1, "unit_unknown"), (2, "product_not_found")})
        other.refresh_from_db()
        self.p.refresh_from_db()
        self.assertEqual(other.quantity, Decimal("5"))
        self.assertEqual(self.p.quantity, Decimal("10"))

    def test_partial_mode_duplicate_barcode_and_parse_error(self):
        r = self.post({"partial": True, "items": [
            {"barcode": "111", "name": "A", "price": "1", "quantity": "1", "from_global": False},
            {"barcode": "111", "name": "A2", "price": "1", "quantity": "1", "from_global": False, "client_row_id": "dup"},
            {"product_id": str(self.p.id), "quantity": "-1", "client_row_id": "neg"},
        ]})
        self.assertEqual(r.status_code, 201, r.data)
        codes = {e["client_row_id"]: e["code"] for e in r.json()["errors"]}
        self.assertEqual(codes, {"dup": "barcode_duplicate", "neg": "validation_error"})
        self.assertEqual(len(r.json()["items"]), 1)

    def test_replay_does_not_double_stock(self):
        body = {"items": [{"product_id": str(self.p.id), "quantity": "2", "unit": "кг"}]}
        first = self.post(body, key="mi-1")
        again = self.post(body, key="mi-1")
        self.assertEqual(first.status_code, 200)
        self.assertTrue(again.json()["replayed"])
        self.p.refresh_from_db()
        self.assertEqual(self.p.quantity, Decimal("12"))

    def test_price_changed_flag(self):
        r = self.post({"items": [{"product_id": str(self.p.id), "quantity": "1", "price": "20"}]})
        row = r.json()["items"][0]
        self.assertTrue(row["price_changed"])
        self.assertEqual(row["price"], "20.00")

    def test_default_atomic_old_errors_kept(self):
        r = self.post({"items": [{"product_id": str(uuid.uuid4()), "quantity": "1"}]})
        self.assertEqual(r.status_code, 404)
        self.assertIn("detail", r.json())
        self.assertEqual(r.json()["index"], 0)
