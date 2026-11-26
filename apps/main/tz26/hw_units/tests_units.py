# Тесты Б-53: единицы продажи. python manage.py test apps.main.tz26.hw_units --settings=core.settings_test_sqlite
import uuid
from decimal import Decimal

from apps.main.models import Cart, CartItem, ProductSaleUnit, SaleItem

from .tests import NEW, HwBase

BOX_CODE = "2000000000017"


class SaleUnitsBase(HwBase):
    def setUp(self):
        super().setUp()
        self.screw = self.mk(name="Саморез", price=Decimal("2.00"), purchase_price=Decimal("1.50"),
                             quantity=Decimal("1000"), unit_code="pcs")
        r = self.api.patch(f"/api/main/products/{self.screw.id}/", {"sale_units_input": [
            {"name": "Коробка", "unit_code": "box", "factor": "200", "barcode": BOX_CODE, "price": "350.00",
             "is_default_receipt": True},
        ]}, format="json")
        assert r.status_code == 200, r.data
        self.unit = ProductSaleUnit.objects.get(product=self.screw)

    def new_cart(self):
        return Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)

    def add_unit(self, cart, **extra):
        body = {"product_id": str(self.screw.id), "sale_unit_id": self.unit.id, **extra}
        return self.api.post(f"/api/main/pos/sales/{cart.id}/add-item/", body, format="json")


class SaleUnitsCrudTests(SaleUnitsBase):
    def test_read_write(self):
        r = self.api.get(f"/api/main/products/{self.screw.id}/")
        self.assertEqual(r.status_code, 200)
        su = r.data["sale_units"]
        self.assertEqual(len(su), 1)
        self.assertEqual(su[0]["factor"], "200.0000")
        self.assertEqual(su[0]["price"], "350.00")
        self.assertEqual(su[0]["barcode"], BOX_CODE)
        self.assertTrue(su[0]["is_default_receipt"])
        r = self.api.patch(f"/api/main/products/{self.screw.id}/", {"sale_units_input": []}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["sale_units"], [])

    def test_create_manual_with_units(self):
        r = self.api.post("/api/main/products/create-manual/", {
            "name": "Цемент", "price": "10.00", "unit_code": "kg",
            "sale_units_input": [{"name": "Мешок", "unit_code": "bag", "factor": "50"}],
        }, format="json", HTTP_IDEMPOTENCY_KEY=f"k-{uuid.uuid4()}")
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.assertEqual(r.data["sale_units"][0]["name"], "Мешок")
        self.assertIsNone(r.data["sale_units"][0]["price"])

    def test_barcode_taken(self):
        other = self.mk(name="Гвоздь")
        r = self.api.patch(f"/api/main/products/{other.id}/", {"sale_units_input": [
            {"name": "Пачка", "factor": "100", "barcode": BOX_CODE}]}, format="json")
        self.assertEqual(r.status_code, 409, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "barcode_taken")

    def test_validation(self):
        bad = (
            [{"name": "X", "factor": "0"}],
            [{"factor": "2"}],
            [{"name": "X", "factor": "2", "unit_code": "zz"}],
            [{"name": "A", "factor": "2", "is_default_sale": True}, {"name": "B", "factor": "3", "is_default_sale": True}],
        )
        for rows in bad:
            r = self.api.patch(f"/api/main/products/{self.screw.id}/", {"sale_units_input": rows}, format="json")
            self.assertEqual(r.status_code, 400, (rows, getattr(r, "data", None)))

    def test_delete_unit_in_open_cart_409(self):
        cart = self.new_cart()
        r = self.add_unit(cart, quantity="1")
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        r = self.api.patch(f"/api/main/products/{self.screw.id}/", {"sale_units_input": []}, format="json")
        self.assertEqual(r.status_code, 409, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "sale_unit_in_use")


class SaleUnitsBarcodeTests(SaleUnitsBase):
    def test_products_barcode(self):
        r = self.api.get(f"/api/main/products/barcode/{BOX_CODE}/")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        self.assertEqual(r.data["matched_by"], "sale_unit_barcode")
        self.assertEqual(r.data["quantity"], "200.000")
        self.assertEqual(r.data["sale_unit"]["name"], "Коробка")
        self.assertEqual(r.data["product"]["id"], str(self.screw.id))


class SaleUnitsSaleTests(SaleUnitsBase):
    def test_scan_box_then_checkout_and_return(self):
        cart = self.new_cart()
        r = self.api.post(f"/api/main/pos/sales/{cart.id}/scan/", {"barcode": BOX_CODE}, format="json")
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        item = CartItem.objects.get(cart=cart)
        self.assertEqual(item.sale_unit_id, self.unit.id)
        self.assertEqual(item.quantity, Decimal("1.000"))
        self.assertEqual(item.unit_price, Decimal("350.00"))
        self.assertEqual(item.base_quantity, Decimal("200.000"))
        row = r.data["sale"]["items"][0]
        self.assertEqual(row["sale_unit"]["factor"], "200.0000")
        self.assertEqual(row["base_quantity"], "200.000")
        r = self.api.post(f"/api/main/pos/sales/{cart.id}/scan/", {"barcode": BOX_CODE}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(CartItem.objects.get(cart=cart).quantity, Decimal("2.000"))
        r = self.api.post(f"/api/main/pos/sales/{cart.id}/checkout/",
                          {"payment_method": "cash", "cash_received": "1000.00"}, format="json")
        self.assertIn(r.status_code, (200, 201), getattr(r, "data", None))
        self.screw.refresh_from_db()
        self.assertEqual(self.screw.quantity, Decimal("600.000"))
        si = SaleItem.objects.get()
        self.assertEqual(si.sale_unit_id, self.unit.id)
        self.assertEqual(si.quantity, Decimal("2.000"))
        self.assertEqual(si.base_quantity, Decimal("400.000"))
        self.assertEqual(si.purchase_price_snapshot, Decimal("300.00"))  # 1.50 x 200
        rr = self.ret(si.sale_id, si.id, "1")
        self.assertEqual(rr.status_code, 200, getattr(rr, "data", None))
        self.screw.refresh_from_db()
        self.assertEqual(self.screw.quantity, Decimal("800.000"))
        det = self.api.get(f"/api/main/pos/sales/{si.sale_id}/")
        line = det.data["items"][0]
        self.assertEqual(Decimal(str(line["returnable_qty"])), Decimal("1.000"))
        self.assertEqual(line["sale_unit"]["name"], "Коробка")

    def test_quick_checkout_bag_x3(self):
        cement = self.mk(name="Цемент", price=Decimal("10.00"), quantity=Decimal("500"), unit_code="kg")
        self.api.patch(f"/api/main/products/{cement.id}/", {"sale_units_input": [
            {"name": "Мешок", "unit_code": "bag", "factor": "50"}]}, format="json")
        bag = ProductSaleUnit.objects.get(product=cement)
        r = self.co([{"product": str(cement.id), "sale_unit": bag.id, "qty": "3"}])
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        it = r.data["items"][0]
        self.assertEqual(it["price"], "500.00")
        self.assertEqual(it["line_total"], "1500.00")
        self.assertEqual(it["base_quantity"], "150.000")
        cement.refresh_from_db()
        self.assertEqual(cement.quantity, Decimal("350.000"))

    def test_other_unit_conflict_and_not_found(self):
        cart = self.new_cart()
        r = self.api.post(f"/api/main/pos/sales/{cart.id}/add-item/",
                          {"product_id": str(self.screw.id), "quantity": "5"}, format="json")
        self.assertEqual(r.status_code, 201)
        r = self.add_unit(cart)
        self.assertEqual(r.status_code, 409, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "sale_unit_conflict")
        cart2 = self.new_cart()
        r = self.api.post(f"/api/main/pos/sales/{cart2.id}/add-item/",
                          {"product_id": str(self.screw.id), "sale_unit_id": 999999}, format="json")
        self.assertEqual(r.status_code, 404, getattr(r, "data", None))

    def test_insufficient_stock_in_base_units(self):
        self.screw.quantity = Decimal("150")
        self.screw.save()
        r = self.add_unit(self.new_cart())
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "insufficient_stock")

    def test_step_check_uses_base_quantity(self):
        self.flag()
        self.screw.quantity_step = Decimal("300")
        self.screw.save()
        r = self.co([{"product": str(self.screw.id), "sale_unit": self.unit.id, "qty": "1"}], client=NEW)
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "quantity_step_mismatch")
        r = self.co([{"product": str(self.screw.id), "sale_unit": self.unit.id, "qty": "3"}], client=NEW)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
