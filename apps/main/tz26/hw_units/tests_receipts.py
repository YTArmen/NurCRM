# Тесты Б-53 п.5: приход и заказ поставщику в единицах закупки.
import uuid
from decimal import Decimal

from apps.main.models import Client, ProductSaleUnit, PurchaseOrder

from .tests import HwBase


class ReceiptUnitsTests(HwBase):
    def setUp(self):
        super().setUp()
        self.supplier = Client.objects.create(company=self.company, full_name="Поставщик", phone="+996555000111",
                                              type=Client.StatusClient.SUPPLIERS)
        self.screw = self.mk(name="Саморез", price=Decimal("2.00"), purchase_price=Decimal("1.00"),
                             quantity=Decimal("0"), client=self.supplier)
        r = self.api.patch(f"/api/main/products/{self.screw.id}/", {"sale_units_input": [
            {"name": "Коробка", "unit_code": "box", "factor": "200", "is_default_receipt": True},
            {"name": "Дробная", "unit_code": "pack", "factor": "1.5"},
        ]}, format="json")
        assert r.status_code == 200, r.data
        self.box = ProductSaleUnit.objects.get(product=self.screw, name="Коробка")
        self.frac = ProductSaleUnit.objects.get(product=self.screw, name="Дробная")

    def receipt(self, **line):
        body = {"items": [{"product": str(self.screw.id), **line}], "payment_type": "debt"}
        return self.api.post(f"/api/main/suppliers/{self.supplier.id}/receipt/", body, format="json")

    def test_receipt_10_boxes(self):
        r = self.receipt(qty=10, purchase_price="300", sale_unit_id=self.box.id)
        self.assertIn(r.status_code, (200, 201), getattr(r, "data", None))
        self.screw.refresh_from_db()
        self.assertEqual(self.screw.quantity, Decimal("2000.000"))
        self.assertEqual(self.screw.purchase_price, Decimal("1.500"))

    def test_receipt_fractional_base_rejected(self):
        r = self.receipt(qty=1, purchase_price="3", sale_unit_id=self.frac.id)
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "receipt_qty_not_integer")
        self.screw.refresh_from_db()
        self.assertEqual(self.screw.quantity, Decimal("0"))

    def test_receipt_wrong_unit_404(self):
        r = self.receipt(qty=1, sale_unit_id=999999)
        self.assertEqual(r.status_code, 404, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "sale_unit_not_found")

    def test_purchase_order_line_in_boxes(self):
        r = self.api.post("/api/main/purchase-orders/", {
            "supplier": str(self.supplier.id),
            "lines": [{"product": str(self.screw.id), "qty": "5", "purchase_price": "300", "sale_unit_id": self.box.id}],
        }, format="json")
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        line = PurchaseOrder.objects.get().lines.get()
        self.assertEqual(line.qty, Decimal("1000.000"))
        self.assertEqual(line.purchase_price, Decimal("1.500"))

    def test_pack_size_from_default_receipt_unit(self):
        from apps.main.tz26.hw_units.receipts import default_receipt_pack_sizes

        self.assertEqual(default_receipt_pack_sizes([self.screw.id]), {self.screw.id: 200.0})
