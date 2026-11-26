"""30.10.2026: список товаров — число запросов не растёт с числом товаров (доп. штрихкоды по prefetch)."""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.main.models import Product, ProductAlternateBarcode
from apps.main.tests_tz_n33_misc import N33Base


class ProductListQueriesTests(N33Base):
    made = 0

    def make(self, n):
        for _ in range(n):
            i = self.made = self.made + 1
            p = Product.objects.create(name=f"Т{i}", company=self.company, quantity=Decimal("1"), price=Decimal("5"),
                                       barcode=f"20{i:09d}")
            ProductAlternateBarcode.objects.create(product=p, company=self.company, barcode=f"3{i:06d}9", quantity=1)
            ProductAlternateBarcode.objects.create(product=p, company=self.company, barcode=f"3{i:06d}1", quantity=1)

    def count(self):
        with CaptureQueriesContext(connection) as c:
            r = self.api.get("/api/main/products/list/", {"page_size": 100})
        self.assertEqual(r.status_code, 200, r.content)
        return len(c.captured_queries), r.json()

    def test_constant_queries_and_sorted_barcodes(self):
        self.make(3)
        few, _ = self.count()
        self.make(30)
        many, data = self.count()
        self.assertLessEqual(many, few + 2)
        rows = data["results"] if isinstance(data, dict) else data
        codes = [b["barcode"] for b in rows[0]["alternate_barcodes"]]
        self.assertEqual(codes, sorted(codes))
        self.assertEqual(len(codes), 2)
