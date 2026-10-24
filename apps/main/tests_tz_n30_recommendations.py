"""ТЗ Н-30: рекомендуемый заказ — статусы, фильтры, порядок, упаковка, валидация."""
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main.models import Client, Product, ProductPackage, Sale, SaleItem
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()


class RecommendationsN30Tests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="Co", owner=self.owner, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.api.force_authenticate(user=self.owner)
        self.supplier = Client.objects.create(full_name="Пост", phone="+996700111222",
                                              type=Client.StatusClient.SUPPLIERS, company=self.company)
        self.cashbox = Cashbox.objects.create(name="К", company=self.company)
        self.url = f"/api/main/suppliers/{self.supplier.id}/recommendations/"

    def prod(self, name, stock, sold=0, **kw):
        p = Product.objects.create(company=self.company, name=name, quantity=Decimal(stock), client=self.supplier,
                                   price=Decimal("10"), purchase_price=Decimal("5"), **kw)
        if sold:
            s = Sale.objects.create(company=self.company, user=self.owner, status=Sale.Status.PAID,
                                    cashbox=self.cashbox, paid_at=timezone.now())
            SaleItem.objects.create(sale=s, product=p, name_snapshot=name, quantity=Decimal(sold), unit_price=Decimal("10"))
        return p

    def rows(self, qs=""):
        r = self.api.get(self.url + qs)
        self.assertEqual(r.status_code, 200, r.content)
        return r.data

    def test_statuses_and_default_order(self):
        self.prod("Б-нет продаж", "5")
        self.prod("В-ok", "1000", sold=30)
        self.prod("А-кончился", "0", sold=30)
        self.prod("Г-критичный", "1", sold=30)
        d = self.rows()
        st = {r["product_name"][2:]: r["stock_forecast"]["status"] for r in d["results"]}
        self.assertEqual(st, {"нет продаж": "no_sales", "ok": "ok", "кончился": "out_of_stock", "критичный": "critical"})
        self.assertEqual([r["product_name"][0] for r in d["results"]], ["А", "Г", "В", "Б"])
        self.assertEqual(d["summary"]["critical_products_count"], 2)

    def test_filters(self):
        self.prod("ok", "1000", sold=30)
        self.prod("кончился", "0", sold=30)
        self.assertEqual([r["product_name"] for r in self.rows("?only_needed=true")["results"]], ["кончился"])
        self.assertEqual(self.rows("?critical=true")["count"], 1)
        self.assertEqual(self.rows("?search=ok")["count"], 1)
        self.assertEqual(self.rows("?stock_status=out_of_stock")["count"], 1)

    def test_ordering_param(self):
        self.prod("Б", "0", sold=30)
        self.prod("А", "1000", sold=30)
        names = lambda q: [r["product_name"] for r in self.rows(q)["results"]]
        self.assertEqual(names("?ordering=name"), ["А", "Б"])
        self.assertEqual(names("?ordering=-name"), ["Б", "А"])
        r = self.api.get(self.url + "?ordering=zzz")
        self.assertEqual((r.status_code, r.data["code"]), (400, "invalid_param"))

    def test_estimated_total_zero(self):
        self.assertEqual(self.rows()["summary"]["estimated_total"], 0)
        self.prod("ok", "1000", sold=30)
        self.assertEqual(self.rows()["summary"]["estimated_total"], 0)

    def test_pack_size_rounds_up_and_weight(self):
        p = self.prod("Пачки", "0", sold=30)  # 1/день, цель 9 → 9, упаковка 6 → 12
        ProductPackage.objects.create(company=self.company, product=p, name="уп", quantity_in_package=Decimal("6"))
        w = self.prod("Вес", "0", sold=Decimal("10.001"), is_weight=True)  # 10.001/30*9=3.0003 → 3.001
        by = {r["product_name"]: r for r in self.rows()["results"]}
        self.assertEqual(by["Пачки"]["recommended_quantity"], 12)
        self.assertEqual(by["Вес"]["recommended_quantity"], 3.001)

    def test_invalid_params(self):
        for q in ("analysis_days=5", "analysis_days=x", "coverage_days=-1", "safety_days=1.5", "coverage_days=abc"):
            r = self.api.get(self.url + "?" + q)
            self.assertEqual((r.status_code, r.data["code"]), (400, "invalid_param"), q)
        self.assertEqual(self.api.get(self.url + "?coverage_days=0&safety_days=0").status_code, 200)
