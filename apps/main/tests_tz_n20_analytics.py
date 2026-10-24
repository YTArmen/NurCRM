"""ТЗ Н-20: аналитика по категориям — фильтры, себестоимость на день продажи, прибыль, возвраты."""
import uuid
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main.models import Product, ProductCategory, Sale, SaleItem, SaleReturn
from apps.users.models import Company, User

D = Decimal


class CategoryAnalyticsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.company = Company.objects.create(name="Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        self.cb = Cashbox.objects.create(company=self.company, name="Касса")
        self.parent = ProductCategory.objects.create(company=self.company, name="Напитки")
        self.child = ProductCategory.objects.create(company=self.company, name="Соки", parent=self.parent)
        self.other = ProductCategory.objects.create(company=self.company, name="Хлеб")
        self.p_child = Product.objects.create(company=self.company, name="Сок", category=self.child, quantity=D("10"),
                                              purchase_price=D("50.00"), price=D("20.00"))
        self.p_other = Product.objects.create(company=self.company, name="Батон", category=self.other, quantity=D("10"),
                                              purchase_price=D("5.00"), price=D("8.00"))
        now = timezone.now()
        # Сок продан дважды: закупка тогда была 10 и 12 (потом выросла до 50).
        self.items = []
        for snap in ("10.00", "12.00"):
            sale = Sale.objects.create(company=self.company, cashbox=self.cb, user=self.owner, status=Sale.Status.PAID,
                                       payment_method="cash", total=D("20.00"), paid_at=now)
            self.items.append(SaleItem.objects.create(
                sale=sale, company=self.company, product=self.p_child, name_snapshot="Сок", unit_price=D("20.00"),
                quantity=D("1.000"), purchase_price_snapshot=D(snap)))
        sale = Sale.objects.create(company=self.company, cashbox=self.cb, user=self.owner, status=Sale.Status.PAID,
                                   payment_method="cash", total=D("8.00"), paid_at=now)
        SaleItem.objects.create(sale=sale, company=self.company, product=self.p_other, name_snapshot="Батон",
                                unit_price=D("8.00"), quantity=D("1.000"), purchase_price_snapshot=D("5.00"))
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.d = timezone.localdate().isoformat()

    def _get(self, tab, **extra):
        r = self.api.get("/api/main/analytics/market/", {"tab": tab, "date_from": self.d, "date_to": self.d, "limit": 100, **extra})
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        return r.data

    def test_category_cost_is_snapshot_not_current(self):
        d = self._get("products")
        cats = {c["category"]: c for c in d["tables"]["categories"]}
        soki = cats["Соки"]
        self.assertEqual(soki["cost"], "22.00")  # 10 + 12, а не 2 × 50
        self.assertEqual(soki["revenue"], "40.00")
        self.assertEqual(soki["gross_profit"], "18.00")
        self.assertEqual(soki["margin_percent"], 45.0)
        self.assertEqual(soki["cost_basis"], "snapshot")
        self.assertEqual(soki["parent_id"], str(self.parent.id))
        self.assertEqual(soki["returns_sum"], "0.00")
        self.assertIsNone(cats["Хлеб"]["parent_id"])

    def test_product_rows_new_fields_and_top_by_profit(self):
        d = self._get("products")
        row = next(r for r in d["tables"]["top_by_revenue"] if r["name"] == "Сок")
        self.assertEqual(row["category_id"], str(self.child.id))
        self.assertEqual(row["cogs"], "22.00")
        self.assertEqual(row["returned_qty"], "0.000")
        self.assertEqual(row["returned_sum"], "0.00")
        prof = d["tables"]["top_by_profit"]
        self.assertEqual([r["name"] for r in prof], ["Сок", "Батон"])
        self.assertEqual(prof[0]["gross_profit"], "18.00")
        self.assertEqual(prof[0]["abc"], "A")
        self.assertIn(prof[1]["abc"], ("A", "B", "C"))

    def test_category_filter_products_and_children(self):
        d = self._get("products", category=str(self.other.id))
        self.assertEqual([c["category"] for c in d["tables"]["categories"]], ["Хлеб"])
        self.assertEqual([r["name"] for r in d["tables"]["top_by_revenue"]], ["Батон"])
        # родитель без детей — пусто, с include_children — Соки
        d = self._get("products", category=str(self.parent.id))
        self.assertEqual(d["tables"]["categories"], [])
        d = self._get("products", category=str(self.parent.id), include_children="1")
        self.assertEqual([c["category"] for c in d["tables"]["categories"]], ["Соки"])
        d = self._get("products", categories=f"{self.child.id},{self.other.id}")
        self.assertEqual({c["category"] for c in d["tables"]["categories"]}, {"Соки", "Хлеб"})

    def test_sales_tab_category_filter(self):
        full = self._get("sales")
        self.assertEqual(full["cards"]["revenue"], "48.00")  # без фильтра числа прежние
        d = self._get("sales", category=str(self.other.id))
        self.assertEqual(d["cards"]["revenue"], "8.00")
        self.assertEqual(d["cards"]["transactions"], 1)
        self.assertEqual(d["cards"]["cogs"], "5.00")
        self.assertEqual(sum(Decimal(x["value"]) for x in d["charts"]["sales_dynamics"]), D("8.00"))
        d = self._get("sales", category=str(self.parent.id), include_children="1")
        self.assertEqual(d["cards"]["revenue"], "40.00")
        self.assertEqual(d["cards"]["transactions"], 2)

    def test_returns_in_rows_and_categories(self):
        sale = self.items[0].sale
        SaleReturn.objects.create(
            sale=sale, company=self.company, user=self.owner, idempotency_key="k1", returned_amount=D("20.00"),
            is_full=False, returned_items=[{"sale_item": str(self.items[0].id), "product": str(self.p_child.id),
                                            "qty": "1.000", "amount": "20.00", "total": "20.00"}])
        d = self._get("products")
        row = next(r for r in d["tables"]["top_by_revenue"] if r["name"] == "Сок")
        self.assertEqual(row["returned_qty"], "1.000")
        self.assertEqual(row["returned_sum"], "20.00")
        cats = {c["category"]: c for c in d["tables"]["categories"]}
        self.assertEqual(cats["Соки"]["returns_sum"], "20.00")
