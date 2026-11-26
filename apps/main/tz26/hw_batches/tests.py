# python manage.py test apps.main.tz26.hw_batches --settings=core.settings_test_sqlite
"""Б-61: партии с оттенком/калибром, отрезной товар и обрезки."""
import uuid
from decimal import Decimal

from apps.main.models import Cart, CartItem, Client, Product, Sale, StockMovement
from apps.possync.tests import PossyncBase

from .models import ProductBatch, ProductRemnant, ProductRoll, SaleItemBatch


class Base(PossyncBase):
    def setUp(self):
        super().setUp()
        self.supplier = Client.objects.create(
            company=self.company, type=Client.StatusClient.SUPPLIERS, full_name="Плитка Опт", phone="+996700000001")
        self.tile = Product.objects.create(
            company=self.company, name="Плитка 30x30", price=Decimal("500.00"), quantity=Decimal("0"),
            client=self.supplier)

    def receive(self, product, qty, label=None, **extra):
        row = {"product": str(product.id), "qty": qty}
        if label is not None:
            row["batch_label"] = label
        row.update(extra)
        return self.api.post(
            f"/api/main/suppliers/{self.supplier.id}/receipt/",
            {"items": [row], "payment_type": "debt"}, format="json")

    def checkout(self, items, key=None):
        return self.api.post(
            "/api/main/pos/checkout/",
            {"items": items, "payment": {"method": "cash", "received": "1000000.00"}},
            format="json", HTTP_IDEMPOTENCY_KEY=key or f"k-{uuid.uuid4()}")


class ReceiptBatchTests(Base):
    def test_two_batches_and_remaining(self):
        self.assertEqual(self.receive(self.tile, 10, "Тон 3 / калибр 2").status_code, 200)
        self.assertEqual(self.receive(self.tile, 5, "Тон 4").status_code, 200)
        self.assertEqual(self.receive(self.tile, 2, "Тон 3 / калибр 2").status_code, 200)  # допоставка в ту же партию
        r = self.api.get(f"/api/main/products/{self.tile.id}/batches/")
        self.assertEqual(r.status_code, 200, r.data)
        by = {b["label"]: b for b in r.data["results"]}
        self.assertEqual(Decimal(by["Тон 3 / калибр 2"]["remaining"]), Decimal("12"))
        self.assertEqual(Decimal(by["Тон 4"]["remaining"]), Decimal("5"))
        self.assertEqual(Decimal(r.data["unbatched_quantity"]), Decimal("0"))
        self.tile.refresh_from_db()
        self.assertEqual(self.tile.quantity, Decimal("17"))

    def test_without_label_nothing_changes(self):
        self.assertEqual(self.receive(self.tile, 4).status_code, 200)
        self.assertEqual(ProductBatch.objects.count(), 0)
        r = self.api.get(f"/api/main/products/{self.tile.id}/batches/")
        self.assertEqual(r.data["count"], 0)
        self.assertEqual(Decimal(r.data["unbatched_quantity"]), Decimal("4"))

    def test_label_too_long_rejected_and_rolled_back(self):
        r = self.receive(self.tile, 3, "x" * 33)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "batch_label_too_long")
        self.tile.refresh_from_db()
        self.assertEqual(self.tile.quantity, Decimal("0"))
        self.assertEqual(ProductBatch.objects.count(), 0)


class SaleBatchTests(Base):
    def setUp(self):
        super().setUp()
        self.open_shift()
        self.receive(self.tile, 10, "Тон 3")
        self.receive(self.tile, 5, "Тон 4")
        self.b3 = ProductBatch.objects.get(label="Тон 3")
        self.b4 = ProductBatch.objects.get(label="Тон 4")

    def test_sale_writes_off_batch(self):
        r = self.checkout([{"product": str(self.tile.id), "qty": "4", "price": "500.00", "batch": "Тон 3"}])
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.b3.refresh_from_db()
        self.assertEqual(self.b3.remaining, Decimal("6"))
        self.b4.refresh_from_db()
        self.assertEqual(self.b4.remaining, Decimal("5"))
        link = SaleItemBatch.objects.get()
        self.assertEqual(link.batch_id, self.b3.id)
        self.assertEqual(link.quantity, Decimal("4"))
        self.tile.refresh_from_db()
        self.assertEqual(self.tile.quantity, Decimal("11"))

    def test_sale_by_batch_id(self):
        r = self.checkout([{"product": str(self.tile.id), "qty": "1", "price": "500.00", "batch": str(self.b4.id)}])
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.b4.refresh_from_db()
        self.assertEqual(self.b4.remaining, Decimal("4"))

    def test_batch_insufficient(self):
        r = self.checkout([{"product": str(self.tile.id), "qty": "6", "price": "500.00", "batch": "Тон 4"}])
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "batch_insufficient")
        self.b4.refresh_from_db()
        self.assertEqual(self.b4.remaining, Decimal("5"))
        self.assertEqual(Sale.objects.count(), 0)
        self.tile.refresh_from_db()
        self.assertEqual(self.tile.quantity, Decimal("15"))

    def test_batch_not_found(self):
        r = self.checkout([{"product": str(self.tile.id), "qty": "1", "price": "500.00", "batch": "Нет такой"}])
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "batch_not_found")

    def test_sale_without_batch_works_as_before(self):
        r = self.checkout([{"product": str(self.tile.id), "qty": "2", "price": "500.00"}])
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.b3.refresh_from_db()
        self.assertEqual(self.b3.remaining, Decimal("10"))
        self.assertEqual(SaleItemBatch.objects.count(), 0)

    def test_quote_mixed_batches_by_items(self):
        r = self.api.post("/api/main/pos/cart/quote/", {"items": [
            {"product": str(self.tile.id), "qty": "1", "batch": "Тон 3"},
            {"product": str(self.tile.id), "qty": "1", "batch": "Тон 4"},
        ]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([w["code"] for w in r.data["warnings"]], ["mixed_batches"])
        self.assertEqual(len(r.data["warnings"][0]["batches"]), 2)

    def test_quote_same_batch_no_warning(self):
        r = self.api.post("/api/main/pos/cart/quote/", {"items": [
            {"product": str(self.tile.id), "qty": "1", "batch": "Тон 3"},
            {"product": str(self.tile.id), "qty": "2", "batch": "Тон 3"},
        ]}, format="json")
        self.assertEqual(r.data["warnings"], [])

    def test_cart_item_batch_endpoint_quote_and_checkout(self):
        cart = Cart.objects.create(company=self.company, user=self.owner, status=Cart.Status.ACTIVE)
        item = CartItem.objects.create(company=self.company, cart=cart, product=self.tile,
                                       unit_price=Decimal("500.00"), quantity=Decimal("2"))
        r = self.api.put(f"/api/main/pos/carts/{cart.id}/items/{item.id}/batch/", {"batch": "Тон 4"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        q = self.api.post("/api/main/pos/cart/quote/", {"cart": str(cart.id)}, format="json")
        self.assertEqual(q.data["warnings"], [])
        self.assertEqual(q.data["lines"][0]["batch_label"], "Тон 4")
        r = self.api.post(f"/api/main/pos/sales/{cart.id}/checkout/",
                          {"payment_method": "cash", "cash_received": "100000.00"}, format="json")
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.b4.refresh_from_db()
        self.assertEqual(self.b4.remaining, Decimal("3"))


class CutTests(Base):
    def setUp(self):
        super().setUp()
        self.open_shift()
        self.cable = Product.objects.create(
            company=self.company, name="Кабель ВВГ", unit="м", price=Decimal("100.00"),
            purchase_price=Decimal("60.00"), quantity=Decimal("100"))
        r = self.api.put(f"/api/main/products/{self.cable.id}/cut/", {
            "roll_length": "100", "min_cut": "1", "remnant_threshold": "5", "remnant_discount_percent": "30"},
            format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def sell(self, qty):
        return self.checkout([{"product": str(self.cable.id), "qty": str(qty), "price": "100.00"}])

    def test_settings_get_and_validation(self):
        r = self.api.get(f"/api/main/products/{self.cable.id}/cut/")
        self.assertEqual(Decimal(r.data["cut"]["roll_length"]), Decimal("100"))
        r = self.api.put(f"/api/main/products/{self.cable.id}/cut/", {"roll_length": "0"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_cut_reduces_roll_without_remnant(self):
        r = self.sell(30)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        roll = ProductRoll.objects.get(status="active")
        self.assertEqual(roll.remaining, Decimal("70"))
        self.assertEqual(ProductRemnant.objects.count(), 0)

    def test_min_cut_enforced(self):
        r = self.sell("0.5")
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "below_min_cut")
        self.assertEqual(Sale.objects.count(), 0)

    def test_small_rest_becomes_remnant(self):
        self.assertEqual(self.sell(97).status_code, 201)  # осталось 3 м < порога 5
        self.cable.refresh_from_db()
        self.assertEqual(self.cable.quantity, Decimal("0"))  # 100 - 97 продано - 3 ушло в обрезок
        info = ProductRemnant.objects.get()
        self.assertEqual(info.remnant_of_id, self.cable.id)
        self.assertEqual(info.length, Decimal("3"))
        rem = info.product
        self.assertEqual(rem.quantity, Decimal("3"))
        self.assertEqual(rem.price, Decimal("70.00"))  # скидка 30%
        self.assertEqual(ProductRoll.objects.get(status="remnant").remaining, Decimal("0"))
        moves = StockMovement.objects.filter(type="remnant")
        self.assertEqual(moves.count(), 2)
        self.assertEqual(sorted(m.change for m in moves), [Decimal("-3"), Decimal("3")])
        # денег нет: ни одного дополнительного чека, кроме самой продажи
        self.assertEqual(Sale.objects.count(), 1)
        r = self.api.get(f"/api/main/products/{self.cable.id}/rolls/")
        self.assertEqual(len(r.data["remnants"]), 1)

    def test_remnant_not_created_when_rest_above_threshold(self):
        self.sell(90)  # осталось 10 >= 5
        self.assertEqual(ProductRemnant.objects.count(), 0)
        self.sell(6)   # осталось 4 < 5
        self.assertEqual(ProductRemnant.objects.get().length, Decimal("4"))

    def test_zero_rest_consumes_roll(self):
        self.assertEqual(self.sell(100).status_code, 201)
        self.assertEqual(ProductRoll.objects.get().status, "consumed")
        self.assertEqual(ProductRemnant.objects.count(), 0)
