# Тесты Б-53 п.7, п.8: витрина (карточка и заказ в единицах продажи).
from decimal import Decimal

from apps.main.models import Product, ProductSaleUnit, ShowcaseOrder
from apps.main.tests_tz_n27_showcase_orders import PUB, N27Base


class ShowcaseUnitsTests(N27Base):
    def setUp(self):
        super().setUp()
        self.screw = Product.objects.create(
            company=self.company, name="Саморез", price=Decimal("2.00"), purchase_price=Decimal("1.00"),
            quantity=Decimal("1000"), unit_code="pcs", quantity_step=Decimal("1"))
        self.box = ProductSaleUnit.objects.create(product=self.screw, name="Коробка", unit_code="box",
                                                  factor=Decimal("200"), price=Decimal("350.00"),
                                                  wholesale_price=Decimal("300.00"), barcode="111")

    def test_public_card_has_units_without_purchase_fields(self):
        r = self.pub.get(PUB + f"showcase/{self.screw.id}/", secure=True)
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual(d["unit_code"], "pcs")
        self.assertEqual(d["sale_units"][0]["name"], "Коробка")
        self.assertEqual(d["sale_units"][0]["price"], "350.00")
        self.assertNotIn("wholesale_price", d["sale_units"][0])
        self.assertNotIn("for_receipt", d["sale_units"][0])

    def test_order_in_boxes_reserves_base(self):
        body = self.body({"product": str(self.screw.id), "qty": "2", "sale_unit": self.box.id})
        r = self.order(body)
        self.assertEqual(r.status_code, 201, r.content)
        o = ShowcaseOrder.objects.get()
        self.assertEqual(o.total, Decimal("700.00"))
        it = o.items.get()
        self.assertEqual(it.qty, Decimal("400.000"))
        # резерв/снятие идут по it.qty (базовые); остаток товара без вариантов витрина и раньше не резервировала

    def test_unknown_unit(self):
        r = self.order(self.body({"product": str(self.screw.id), "qty": "1", "sale_unit": 999999}))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json()["code"], "sale_unit_not_found")
