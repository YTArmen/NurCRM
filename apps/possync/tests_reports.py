"""
Тесты отчётных эндпоинтов. Запуск:
    python manage.py test apps.possync.tests_reports --settings=core.settings_test_sqlite
"""
from decimal import Decimal

from django.utils import timezone

from apps.main.models import (
    Client,
    Product, ProductInventoryItem, ProductInventorySession, Sale, SaleItem, SaleReturn,
    SupplierReceipt, SupplierReceiptItem,
)
from apps.users.models import Branch

from .models import StockAdjustment, StockAdjustmentLine
from .tests import PossyncBase


def mk_sale(company, product, qty=None, price="100.00", status=Sale.Status.PAID, user=None, branch=None, cashbox=None, **kw):
    sale = Sale.objects.create(company=company, status=status, user=user, branch=branch, cashbox=cashbox,
                               total=Decimal(price) * Decimal(qty), paid_at=timezone.now(), **kw)
    item = SaleItem.objects.create(company=company, sale=sale, product=product, name_snapshot=product.name,
                                   unit_price=Decimal(price), quantity=Decimal(qty))
    return sale, item


class ProductHistoryTests(PossyncBase):
    def url(self, pid=None):
        return f"/api/main/products/{pid or self.product.id}/history/"

    def test_merges_sources_newest_first(self):
        sale, item = mk_sale(self.company, self.product, cashbox=self.cashbox, qty="2", user=self.owner)
        sup = Client.objects.create(company=self.company, type=Client.StatusClient.SUPPLIERS, full_name="Пост")
        rec = SupplierReceipt.objects.create(company=self.company, supplier=sup, created_by=self.owner)
        SupplierReceiptItem.objects.create(receipt=rec, product=self.product, qty=5, purchase_price=Decimal("60"))
        adj = StockAdjustment.objects.create(company=self.company, seq=1, kind="write_off", created_by=self.owner)
        StockAdjustmentLine.objects.create(adjustment=adj, product=self.product, delta=Decimal("-1"),
                                           before=Decimal("50"), after=Decimal("49"))
        sess = ProductInventorySession.objects.create(company=self.company, created_by=self.owner,
                                                      status="applied", applied_at=timezone.now())
        ProductInventoryItem.objects.create(session=sess, product=self.product,
                                            quantity_before=Decimal("49"), quantity_fact=Decimal("47"))
        r = self.api.get(self.url())
        self.assertEqual(r.status_code, 200, r.data)
        types = [x["type"] for x in r.data["results"]]
        self.assertEqual(r.data["count"], 4)
        self.assertEqual(sorted(types), ["inventory", "receipt", "sale", "write_off"])
        ats = [x["at"] for x in r.data["results"]]
        self.assertEqual(ats, sorted(ats, reverse=True))
        by = {x["type"]: x for x in r.data["results"]}
        self.assertEqual(Decimal(by["sale"]["qty_delta"]), Decimal("-2"))
        self.assertEqual(Decimal(by["receipt"]["qty_delta"]), Decimal("5"))
        self.assertEqual(by["write_off"]["doc_number"], "СП-000001")
        self.assertEqual(Decimal(by["write_off"]["before"]), Decimal("50"))
        self.assertEqual(Decimal(by["inventory"]["qty_delta"]), Decimal("-2"))

    def test_partial_return_restores_sold_qty(self):
        sale, item = mk_sale(self.company, self.product, cashbox=self.cashbox, qty="5", user=self.owner)
        item.quantity = Decimal("3")  # частичный возврат 2 уменьшил строку
        item.save()
        sale.status = Sale.Status.PARTIALLY_RETURNED
        sale.save()
        SaleReturn.objects.create(
            sale=sale, company=self.company, idempotency_key="k1", is_full=False,
            returned_items=[{"sale_item": str(item.id), "product": str(self.product.id), "qty": "2.000",
                             "price": "100.00", "restock": True}])
        r = self.api.get(self.url())
        by = {x["type"]: x for x in r.data["results"]}
        self.assertEqual(Decimal(by["sale"]["qty_delta"]), Decimal("-5"))
        self.assertEqual(Decimal(by["return"]["qty_delta"]), Decimal("2"))

    def test_full_return_and_defect(self):
        sale, item = mk_sale(self.company, self.product, cashbox=self.cashbox, qty="1", status=Sale.Status.CANCELED)
        SaleReturn.objects.create(
            sale=sale, company=self.company, idempotency_key="k2", is_full=True, is_defect=True,
            returned_items=[{"sale_item": str(item.id), "product": str(self.product.id), "qty": "1.000",
                             "restock": False}])
        r = self.api.get(self.url())
        by = {x["type"]: x for x in r.data["results"]}
        self.assertEqual(Decimal(by["sale"]["qty_delta"]), Decimal("-1"))
        self.assertEqual(Decimal(by["return"]["qty_delta"]), Decimal("0"))
        self.assertTrue(by["return"]["is_defect"])

    def test_canceled_sale_without_return_hidden(self):
        mk_sale(self.company, self.product, cashbox=self.cashbox, qty="1", status=Sale.Status.CANCELED)
        self.assertEqual(self.api.get(self.url()).data["count"], 0)

    def test_pagination_and_period(self):
        for _ in range(5):
            mk_sale(self.company, self.product, cashbox=self.cashbox, qty="1")
        r = self.api.get(self.url() + "?page=2&page_size=2")
        self.assertEqual(r.data["count"], 5)
        self.assertEqual(len(r.data["results"]), 2)
        r = self.api.get(self.url() + "?date_from=2000-01-01&date_to=2000-01-02")
        self.assertEqual(r.data["count"], 0)

    def test_branch_filter_and_other_company(self):
        b = Branch.objects.create(company=self.company, name="Филиал")
        from apps.construction.models import Cashbox
        cb = Cashbox.objects.create(name="Касса Ф", company=self.company, branch=b)
        mk_sale(self.company, self.product, cashbox=cb, qty="1", branch=b)
        mk_sale(self.company, self.product, cashbox=self.cashbox, qty="1")
        self.assertEqual(self.api.get(self.url() + f"?branch={b.id}").data["count"], 1)
        other = Product.objects.create(company=self.company, name="Другой", price=Decimal("1"), quantity=Decimal("1"))
        self.assertEqual(self.api.get(self.url(other.id)).data["count"], 0)
        import uuid
        self.assertEqual(self.api.get(self.url(uuid.uuid4())).status_code, 404)


class HoursSourcesTests(PossyncBase):
    URL = "/api/main/analytics/market/hours-sources/"

    def test_hours_and_sources(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from apps.main.models import ShowcaseOrder
        tz = ZoneInfo("Asia/Bishkek")
        s1, _ = mk_sale(self.company, self.product, cashbox=self.cashbox, qty="1", price="100.00")
        s2, _ = mk_sale(self.company, self.product, cashbox=self.cashbox, qty="2", price="100.00")
        s3, _ = mk_sale(self.company, self.product, cashbox=self.cashbox, qty="1", price="50.00")
        Sale.objects.filter(pk=s1.pk).update(paid_at=datetime(2026, 10, 5, 9, 30, tzinfo=tz))
        Sale.objects.filter(pk=s2.pk).update(paid_at=datetime(2026, 10, 5, 9, 59, tzinfo=tz))
        Sale.objects.filter(pk=s3.pk).update(paid_at=datetime(2026, 10, 6, 23, 10, tzinfo=tz))
        ShowcaseOrder.objects.create(company=self.company, number=1, sale=s3, total=Decimal("50"))
        # вне периода и отменённая не считаются
        s4, _ = mk_sale(self.company, self.product, cashbox=self.cashbox, qty="1")
        Sale.objects.filter(pk=s4.pk).update(paid_at=datetime(2026, 9, 1, 9, 0, tzinfo=tz))
        mk_sale(self.company, self.product, cashbox=self.cashbox, qty="1", status=Sale.Status.CANCELED)

        r = self.api.get(self.URL + "?date_from=2026-10-01&date_to=2026-10-31")
        self.assertEqual(r.status_code, 200, r.data)
        hours = {h["hour"]: h for h in r.data["hours"]}
        self.assertEqual(len(r.data["hours"]), 24)
        self.assertEqual(hours[9]["count"], 2)
        self.assertEqual(Decimal(hours[9]["sum"]), Decimal("300.00"))
        self.assertEqual(hours[23]["count"], 1)
        self.assertEqual(hours[0]["count"], 0)
        src = {x["source"]: x for x in r.data["sources"]}
        self.assertEqual(src["cashbox"]["count"], 2)
        self.assertEqual(src["showcase"]["count"], 1)
        self.assertEqual(Decimal(src["showcase"]["sum"]), Decimal("50.00"))
        self.assertEqual(src["other"]["count"], 0)


class SalesVelocityTests(PossyncBase):
    URL = "/api/main/products/sales-velocity/"

    def test_velocity_and_days_left(self):
        mk_sale(self.company, self.product, cashbox=self.cashbox, qty="30")  # 30 за 30 дней = 1/день, остаток 50
        idle = Product.objects.create(company=self.company, name="Лежит", price=Decimal("1"), quantity=Decimal("9"))
        low = Product.objects.create(company=self.company, name="Мало", price=Decimal("1"),
                                     quantity=Decimal("2"), minimum_quantity=Decimal("5"))
        r = self.api.get(self.URL + "?days=30")
        self.assertEqual(r.status_code, 200, r.data)
        by = {x["name"]: x for x in r.data["results"]}
        self.assertNotIn("Лежит", by)
        self.assertIn("Мало", by)
        h = by["Хлеб"]
        self.assertEqual(Decimal(h["sold_qty"]), Decimal("30"))
        self.assertEqual(Decimal(h["per_day"]), Decimal("1"))
        self.assertEqual(Decimal(h["days_left"]), Decimal("50"))
        self.assertIsNone(by["Мало"]["days_left"])
        self.assertEqual(r.data["results"][0]["name"], "Хлеб")  # с days_left выше, чем без продаж

        r = self.api.get(self.URL + f"?product_ids={idle.id}")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(Decimal(r.data["results"][0]["sold_qty"]), Decimal("0"))
        self.assertEqual(self.api.get(self.URL + "?product_ids=zzz").status_code, 400)

    def test_old_sales_excluded_and_pagination(self):
        s, _ = mk_sale(self.company, self.product, cashbox=self.cashbox, qty="10")
        Sale.objects.filter(pk=s.pk).update(paid_at=timezone.now() - timezone.timedelta(days=40))
        self.assertEqual(self.api.get(self.URL + "?days=30").data["count"], 0)
        self.assertEqual(self.api.get(self.URL + "?days=60").data["count"], 1)
        self.assertEqual(self.api.get(self.URL + "?days=60&page=2&page_size=1").data["results"], [])
