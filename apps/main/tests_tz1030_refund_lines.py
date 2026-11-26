"""ТЗ 30.10 №10: точная сумма возврата по строке от сервера (чек 001916: 2 × 2,00, скидка на чек 0,40)."""
import uuid
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.main.models import Product, Sale, SaleItem, SalePayment
from apps.main.pos_serializers import SaleDetailSerializer
from apps.main.pos_views import SaleReturnAPIView
from apps.main.tests_tz_n02_return_discount_srv import ReturnCheckDiscountTests


class RefundLinesTests(ReturnCheckDiscountTests):
    def make_glue_sale(self):
        sale = Sale.objects.create(
            company=self.company, branch=self.branch, shift=self.shift, user=self.cashier,
            payment_method="cash", status=Sale.Status.PAID,
            subtotal=Decimal("4.00"), discount_total=Decimal("0.40"), total=Decimal("3.60"),
        )
        p = Product.objects.create(company=self.company, name="Клей", price=Decimal("2.00"), quantity=Decimal("10"))
        item = SaleItem.objects.create(company=self.company, branch=self.branch, sale=sale, product=p,
                                       unit_price=Decimal("2.00"), quantity=Decimal("2"), name_snapshot="Клей")
        SalePayment.objects.create(company=self.company, sale=sale, method="cash", amount=Decimal("3.60"))
        return sale, item

    def line(self, sale):
        sale = Sale.objects.prefetch_related("items__product", "returns").get(pk=sale.pk)
        return SaleDetailSerializer(sale).data["items"][0]

    def test_fields_and_two_returns_by_one(self):
        sale, item = self.make_glue_sale()
        ln = self.line(sale)
        self.assertEqual((ln["refundable_quantity"], ln["refundable_amount"], ln["unit_refund_price"]),
                         ("2.000", "3.60", "1.80"))
        r1 = self.ret(sale, {"items": [{"sale_item_id": str(item.id), "quantity": "1"}]})
        self.assertEqual(r1.status_code, 200, r1.data)
        self.assertEqual(r1.data["refund"]["amount"], "1.80")
        self.assertEqual(r1.data["refund"]["lines"], [{"sale_item_id": str(item.id), "quantity": "1.000", "amount": "1.80"}])
        ln = self.line(sale)
        self.assertEqual((ln["refundable_quantity"], ln["refundable_amount"]), ("1.000", "1.80"))
        r2 = self.ret(sale, {"items": [{"sale_item_id": str(item.id), "quantity": "1"}]})
        self.assertEqual(r2.data["refund"]["amount"], "1.80")

    def test_odd_kopecks_last_return_is_remainder(self):
        # 3 × 1,00, скидка на чек 0,10 → 2,90; по одной: 0,97 + 0,97 + 0,96
        sale = Sale.objects.create(company=self.company, branch=self.branch, shift=self.shift, user=self.cashier,
                                   payment_method="cash", status=Sale.Status.PAID, subtotal=Decimal("3.00"),
                                   discount_total=Decimal("0.10"), total=Decimal("2.90"))
        p = Product.objects.create(company=self.company, name="Р", price=Decimal("1.00"), quantity=Decimal("10"))
        item = SaleItem.objects.create(company=self.company, branch=self.branch, sale=sale, product=p,
                                       unit_price=Decimal("1.00"), quantity=Decimal("3"), name_snapshot="Р")
        SalePayment.objects.create(company=self.company, sale=sale, method="cash", amount=Decimal("2.90"))
        got = []
        for _ in range(3):
            ln = self.line(sale)
            r = self.ret(sale, {"items": [{"sale_item_id": str(item.id), "quantity": "1"}]})
            self.assertEqual(r.status_code, 200, r.data)
            got.append(r.data["refund"]["amount"])
            if ln["refundable_quantity"] == "1.000":
                self.assertEqual(ln["refundable_amount"], r.data["refund"]["amount"])
        self.assertEqual(sum(Decimal(x) for x in got), Decimal("2.90"))

    def test_canceled_zero(self):
        sale, item = self.make_glue_sale()
        self.ret(sale, {})
        ln = self.line(sale)
        self.assertEqual((ln["refundable_quantity"], ln["refundable_amount"]), ("0.000", "0.00"))

    def test_too_much_strict_code(self):
        sale, item = self.make_glue_sale()
        with mock.patch("core.api_compat.is_strict", return_value=True):
            r = self.ret(sale, {"items": [{"sale_item_id": str(item.id), "quantity": "3"}]})
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["code"], "refund_exceeds_paid")
        self.assertEqual(r.data["data"]["line_id"], str(item.id))
        self.assertEqual(r.data["data"]["max_amount"], "3.60")
        self.assertEqual(r.data["data"]["max_quantity"], "2.000")

    def test_too_much_legacy_400(self):
        sale, item = self.make_glue_sale()
        with mock.patch("core.api_compat.is_strict", return_value=False):
            r = self.ret(sale, {"items": [{"sale_item_id": str(item.id), "quantity": "3"}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("items", r.data)
