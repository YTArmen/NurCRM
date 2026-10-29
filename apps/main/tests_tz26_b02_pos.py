"""Б-02: «Расход» (отрицательная цена) для старой кассы — скидка на чек; новый клиент — 400; expected_total для мобилки."""
from decimal import Decimal

from apps.main.models import Cart, Sale
from apps.main.tests_tz_n15_checkout import CheckoutBase
from apps.users.models import CompanyAddon

NEW = {"HTTP_X_NURMARKET_CLIENT": "kassa/1.17.61"}
EXPENSE = {"custom": True, "name": "Расход", "qty": "1", "price": "-10.00"}


class NegativePriceCompatTests(CheckoutBase):
    client_hdr = None  # старая касса без заголовка

    def line(self):
        return {"product": str(self.product.id), "qty": "1", "price": "100.00"}

    def test_checkout_old_client_expense_is_order_discount(self):
        self.open_shift()
        r = self.co([self.line(), EXPENSE], expected_total="90.00")
        self.assertEqual(r.status_code, 201, r.data)
        sale = Sale.objects.get(pk=r.data["id"] if "id" in r.data else r.data["sale_id"])
        self.assertEqual(sale.total, Decimal("90.00"))
        self.assertEqual(sale.discount_total, Decimal("10.00"))

    def test_checkout_replay_same_total(self):
        self.open_shift()
        a = self.co([self.line(), EXPENSE], key="b02-replay-key-0001")
        b = self.co([self.line(), EXPENSE], key="b02-replay-key-0001")
        self.assertEqual(b.status_code, 200)
        self.assertTrue(b.data["replayed"])
        self.assertEqual(a.data["total"], b.data["total"])

    def test_checkout_new_client_400(self):
        self.open_shift()
        self.client_hdr = "kassa/1.17.61"
        r = self.co([self.line(), EXPENSE])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "negative_price")

    def test_checkout_flag_true_old_client_400(self):
        self.open_shift()
        CompanyAddon.objects.create(company=self.company, code="api_strict_2026_10")
        r = self.co([self.line(), EXPENSE])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "negative_price")

    def test_custom_item_old_client_discount(self):
        self.open_shift()
        cart = self.api.post("/api/main/pos/sales/start/", {}, format="json").data["id"]
        self.api.post(f"/api/main/pos/sales/{cart}/add-item/", {"product_id": str(self.product.id), "quantity": "1"}, format="json")
        r = self.api.post(f"/api/main/pos/carts/{cart}/custom-item/", {"name": "Расход", "price": "-10.00"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        c = Cart.objects.get(pk=cart)
        # горячая правка 29.10: старая касса — «Расход» отдельной строкой с минусом, как до 29.10
        self.assertEqual(c.total, Decimal("90.00"))
        self.assertEqual(c.items.count(), 2)

    def test_bulk_old_client_discount(self):
        self.open_shift()
        cart = self.api.post("/api/main/pos/sales/start/", {}, format="json").data["id"]
        r = self.api.post(f"/api/main/pos/sales/{cart}/items/bulk/", {"items": [
            {"product_id": str(self.product.id), "quantity": "1"},
            {"custom": True, "name": "Расход", "price": "-10", "quantity": "1"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        c = Cart.objects.get(pk=cart)
        self.assertEqual((c.total, c.discount_total), (Decimal("90.00"), Decimal("10.00")))

    def test_expected_total_skipped_for_old_mobile(self):
        self.open_shift()
        body = {"items": [self.line()], "payment": {"method": "cash", "received": "1000.00"}, "expected_total": "1.00"}
        r = self.api.post("/api/main/pos/checkout/", body, format="json", HTTP_IDEMPOTENCY_KEY="b02-mob-old-0000001",
                          HTTP_X_NURMARKET_CLIENT="mobile/1.18.4")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.api.post("/api/main/pos/checkout/", body, format="json", HTTP_IDEMPOTENCY_KEY="b02-mob-new-0000001",
                          HTTP_X_NURMARKET_CLIENT="mobile/1.18.5")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "total_mismatch")


class NegativeLineTotalTests(NegativePriceCompatTests):
    """Горячая правка 29.10: строка «Расход» с минусом уменьшает итог чека (раньше минус возвращался «скидкой»)."""

    def test_negative_line_reduces_total(self):
        self.open_shift()
        cart = self.api.post("/api/main/pos/sales/start/", {}, format="json").data["id"]
        self.api.post(f"/api/main/pos/sales/{cart}/add-item/", {"product_id": str(self.product.id), "quantity": "1"}, format="json")
        self.api.post(f"/api/main/pos/carts/{cart}/custom-item/", {"name": "Расход", "price": "-10.00"}, format="json")
        c = Cart.objects.get(pk=cart)
        self.assertEqual((c.subtotal, c.discount_total, c.total), (Decimal("90.00"), Decimal("0.00"), Decimal("90.00")))
