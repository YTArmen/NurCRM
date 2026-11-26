"""ТЗ 30.10 №11: чек из очереди при нулевом остатке — принять в минус, отдать stock_negative."""
from decimal import Decimal

from apps.main.models import Product
from apps.main.tests_tz_n15_checkout import OfflineResendTests


class NegativeStockQueueTests(OfflineResendTests):
    def test_queue_flag_accepts_and_lists_negative(self):
        a, b = self.mk(2)
        Product.objects.filter(pk=a.pk).update(quantity=Decimal("0"))
        # чек из очереди, досланный обычным запросом — раньше отказ «insufficient_stock»
        r = self.co([self.line(a, qty="3"), self.line(b, qty="1")], allow_negative_stock=True)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.assertEqual(r.data["stock_negative"], [{"product_id": str(a.id), "variant_id": None, "balance": "-3.000"}])
        a.refresh_from_db()
        self.assertEqual(a.quantity, Decimal("-3"))

    def test_offline_with_sold_at_also_lists_negative(self):
        a = self.mk(1)[0]
        Product.objects.filter(pk=a.pk).update(quantity=Decimal("1"))
        r = self.co([self.line(a, qty="2")], offline=True, sold_at=self.sold_at)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.assertEqual(r.data["stock_negative"][0]["balance"], "-1.000")

    def test_without_flag_still_refuses(self):
        a = self.mk(1)[0]
        Product.objects.filter(pk=a.pk).update(quantity=Decimal("0"))
        r = self.co([self.line(a, qty="1")])
        self.assertEqual(r.data["code"], "insufficient_stock")
        self.assertNotIn("stock_negative", r.data)

    def test_flag_with_enough_stock_empty_list(self):
        a = self.mk(1)[0]
        r = self.co([self.line(a, qty="1")], allow_negative_stock=True)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.assertEqual(r.data["stock_negative"], [])
