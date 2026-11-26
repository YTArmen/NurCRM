"""ТЗ Н-27: заказ витрины с вариантом, резерв, «сообщить о поступлении», приход с вариантом."""
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.main.models import (
    Client, Product, ProductVariant, ShowcaseOrder, ShowcaseStockRequest,
)
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()
PUB = "/api/main/public/companies/n27-shop/"


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "n27"}}


@override_settings(CACHES=LOCMEM)
class N27Base(TestCase):
    def setUp(self):
        cache.clear()
        self.pub = APIClient()
        self.api = APIClient()
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="N27", slug="n27-shop", owner=self.owner, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.api.force_authenticate(user=self.owner)
        self.nike = Product.objects.create(company=self.company, name="Nike", price=Decimal("1000"), quantity=Decimal("0"))
        self.other = Product.objects.create(company=self.company, name="Adidas", price=Decimal("500"), quantity=Decimal("0"))
        self.v46 = ProductVariant.objects.create(company=self.company, product=self.nike, size="46", color="Белый", quantity=Decimal("3"))
        self.v48 = ProductVariant.objects.create(company=self.company, product=self.nike, size="48", color="Белый", quantity=Decimal("1"), price=Decimal("900"))
        self.v0 = ProductVariant.objects.create(company=self.company, product=self.nike, size="50", color="Белый", quantity=Decimal("0"))
        self.va = ProductVariant.objects.create(company=self.company, product=self.other, size="M", color="Чёрный", quantity=Decimal("4"))
        self.nike.refresh_from_db()

    def body(self, *items):
        return {"customer": {"name": "Аика", "phone": "+996555123456"}, "items": list(items)}

    def item(self, product, variant=None, qty="1"):
        d = {"product": str(product.id), "qty": qty}
        if variant is not None:
            d["variant"] = str(variant.id)
        return d

    def order(self, body, key=None, strict=True):
        # Б-02: 409/variant_required — для нового клиента; без заголовка (старая касса, сайт) — 400 not_enough_stock
        kw = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        if strict:
            kw["HTTP_X_NURMARKET_CLIENT"] = "mobile/1.18.5"
        return self.pub.post(PUB + "orders/", body, format="json", secure=True, **kw)


class OrderValidationTests(N27Base):
    def test_variant_required_with_index(self):
        r = self.order(self.body(self.item(self.nike, self.v46), self.item(self.nike)))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual((r.json()["code"], r.json()["index"]), ("variant_required", 1))

    def test_old_client_gets_not_enough_stock(self):
        # без размера — variant_required и у старого клиента (так было до Б-02)
        r = self.order(self.body(self.item(self.nike)), strict=False)
        self.assertEqual((r.status_code, r.json()["code"]), (400, "variant_required"))
        r = self.order(self.body(self.item(self.nike, self.v46, "5")), strict=False)
        self.assertEqual((r.status_code, r.json()["code"]), (400, "not_enough_stock"))
        # флаг компании включает новое поведение и для сайта без заголовка
        from apps.users.models import CompanyAddon
        CompanyAddon.objects.create(company=self.company, code="api_strict_2026_10", active=True)
        r = self.order(self.body(self.item(self.nike, self.v46, "5")), strict=False)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "variant_out_of_stock"))

    def test_site_client_is_strict(self):
        # новый сайт витрины (front-market) шлёт X-NurMarket-Client: site/<версия> — строгие ошибки без флага компании
        r = self.pub.post(PUB + "orders/", self.body(self.item(self.nike, self.v46, "5")), format="json", secure=True, HTTP_X_NURMARKET_CLIENT="site/1.0")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "variant_out_of_stock"))
        r = self.pub.post(PUB + "orders/", self.body(self.item(self.nike)), format="json", secure=True, HTTP_X_NURMARKET_CLIENT="site/1.0")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "variant_required"))

    def test_rate_limit_10_per_hour_per_ip(self):
        # товар без вариантов с запасом на все 11 заказов: проверяем только лимит
        plain = Product.objects.create(company=self.company, name="Носки", price=Decimal("100"), quantity=Decimal("50"))
        for i in range(10):
            r = self.order(self.body(self.item(plain)))
            self.assertEqual(r.status_code, 201, (i, r.content))
        self.assertEqual(self.order(self.body(self.item(plain))).status_code, 429)

    def test_variant_mismatch(self):
        r = self.order(self.body(self.item(self.nike, self.va)))
        self.assertEqual((r.status_code, r.json()["code"], r.json()["index"]), (400, "variant_mismatch", 0))

    def test_out_of_stock_409_with_available(self):
        r = self.order(self.body(self.item(self.nike, self.v46, "5")))
        self.assertEqual((r.status_code, r.json()["code"], r.json()["available"]), (409, "variant_out_of_stock", 3))
        # две строки одного варианта суммируются
        r = self.order(self.body(self.item(self.nike, self.v46, "2"), self.item(self.nike, self.v46, "2")))
        self.assertEqual((r.status_code, r.json()["code"], r.json()["index"]), (409, "variant_out_of_stock", 1))
        r = self.order(self.body(self.item(self.nike, self.v0)))
        self.assertEqual((r.status_code, r.json()["available"]), (409, 0))
        self.assertEqual(ShowcaseOrder.objects.count(), 0)

    def test_quote_same_rules_and_variant_prices(self):
        url = PUB + "showcase/cart/quote/"
        h = {"HTTP_X_NURMARKET_CLIENT": "mobile/1.18.5"}
        r = self.pub.post(url, {"items": [self.item(self.nike)]}, format="json", secure=True, **h)
        self.assertEqual((r.status_code, r.json()["code"]), (400, "variant_required"))
        r = self.pub.post(url, {"items": [self.item(self.nike, self.v46, "9")]}, format="json", secure=True, **h)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "variant_out_of_stock"))
        r = self.pub.post(url, {"items": [self.item(self.nike, self.v48)]}, format="json", secure=True)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["items"][0]["price"], "900.00")


class OrderReserveTests(N27Base):
    def test_two_sizes_reserve_cancel_and_idempotency(self):
        body = self.body(self.item(self.nike, self.v46), self.item(self.nike, self.v48))
        r1 = self.order(body, key="k1")
        self.assertEqual(r1.status_code, 201, r1.content)
        r2 = self.order(body, key="k1")
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json().get("replayed"))
        self.assertNotIn("replayed", r1.json())
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertEqual(ShowcaseOrder.objects.count(), 1)
        for v in (self.v46, self.v48, self.v0):
            v.refresh_from_db()
        self.assertEqual((self.v46.quantity, self.v48.quantity, self.v0.quantity), (Decimal("2"), Decimal("0"), Decimal("0")))
        self.nike.refresh_from_db()
        self.assertEqual(self.nike.quantity, Decimal("2"))  # сумма вариантов
        # reserved виден владельцу
        rows = self.api.get(f"/api/main/products/{self.nike.id}/variants/").json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        by = {x["size"]: x for x in rows}
        self.assertEqual(Decimal(by["46"]["reserved"]), Decimal("1"))
        self.assertEqual(Decimal(by["50"]["reserved"]), Decimal("0"))
        # отмена возвращает и вариант, и товар
        r = self.api.patch(f"/api/main/showcase/orders/{r1.json()['id']}/", {"status": "canceled"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        for v in (self.v46, self.v48):
            v.refresh_from_db()
        self.nike.refresh_from_db()
        self.assertEqual((self.v46.quantity, self.v48.quantity, self.nike.quantity), (Decimal("3"), Decimal("1"), Decimal("4")))

    def test_product_qty_is_sum_of_active_variants_only(self):
        ProductVariant.objects.filter(pk=self.v48.pk).update(is_active=False)
        r = self.order(self.body(self.item(self.nike, self.v46)))
        self.assertEqual(r.status_code, 201, r.content)
        self.nike.refresh_from_db()
        self.assertEqual(self.nike.quantity, Decimal("2"))  # 46: 3-1, 48 неактивен, 50: 0


class NotifyTests(N27Base):
    def url(self, product=None):
        return PUB + f"showcase/{(product or self.nike).id}/notify/"

    def test_create_dedupe_and_owner_list_and_ready(self):
        r = self.pub.post(self.url(), {"variant": str(self.v0.id), "phone": "+996 555 111 222"}, format="json", secure=True)
        self.assertEqual(r.status_code, 201, r.content)
        r2 = self.pub.post(self.url(), {"variant": str(self.v0.id), "phone": "+996555111222"}, format="json", secure=True)
        self.assertEqual((r2.status_code, r2.json()["id"]), (200, r.json()["id"]))
        self.assertEqual(ShowcaseStockRequest.objects.count(), 1)
        lst = self.api.get("/api/main/showcase/stock-requests/?status=open").json()
        self.assertEqual(lst["count"], 1)
        self.assertEqual(lst["results"][0]["variant_label"], "50, Белый")
        # остаток появился — заявка ready
        self.v0.quantity = Decimal("2")
        self.v0.save()
        self.assertEqual(self.api.get("/api/main/showcase/stock-requests/?status=open").json()["count"], 0)
        ready = self.api.get("/api/main/showcase/stock-requests/?status=ready").json()
        self.assertEqual(ready["count"], 1)
        self.assertIsNotNone(ready["results"][0]["notified_at"])

    def test_validation(self):
        post = lambda d: self.pub.post(self.url(), d, format="json", secure=True)
        self.assertEqual(post({"variant": str(self.v0.id)}).json()["code"], "required")
        self.assertEqual(post({"phone": "+996555111222"}).json()["code"], "variant_required")
        self.assertEqual(post({"variant": str(self.va.id), "phone": "+996555111222"}).json()["code"], "variant_mismatch")
        self.assertEqual(post({"variant": str(self.v46.id), "phone": "+996555111222"}).json()["code"], "already_in_stock")

    def test_limit_5_per_day(self):
        for i in range(5):
            r = self.pub.post(self.url(), {"variant": str(self.v0.id), "phone": f"+99655511{i:04d}"}, format="json", secure=True)
            self.assertEqual(r.status_code, 201, (i, r.content))
        r = self.pub.post(self.url(), {"variant": str(self.v0.id), "phone": "+996555119999"}, format="json", secure=True)
        self.assertEqual(r.status_code, 429)

    def test_released_stock_marks_ready(self):
        ProductVariant.objects.filter(pk=self.v46.pk).update(quantity=Decimal("1"))
        r = self.order(self.body(self.item(self.nike, self.v46)))
        self.assertEqual(r.status_code, 201, r.content)
        ShowcaseStockRequest.objects.create(company=self.company, product=self.nike, variant=self.v46, phone="+996555000111")
        self.api.patch(f"/api/main/showcase/orders/{r.json()['id']}/", {"status": "canceled"}, format="json")
        self.assertEqual(ShowcaseStockRequest.objects.get().status, "ready")


class ReceiptVariantTests(N27Base):
    def setUp(self):
        super().setUp()
        self.supplier = Client.objects.create(full_name="Пост", phone="+996700111222",
                                              type=Client.StatusClient.SUPPLIERS, company=self.company)
        self.url = f"/api/main/suppliers/{self.supplier.id}/receipt/"

    def test_receipt_with_variant(self):
        r = self.api.post(self.url, {"items": [{"product": str(self.nike.id), "qty": 5, "variant": str(self.v0.id)}]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.v0.refresh_from_db()
        self.nike.refresh_from_db()
        self.assertEqual(self.v0.quantity, Decimal("5"))
        self.assertEqual(self.nike.quantity, Decimal("9"))  # 3+1+5

    def test_receipt_sizes_split_and_mismatch(self):
        item = {"product": str(self.nike.id), "qty": 5,
                "variants": [{"variant": str(self.v46.id), "qty": 2}, {"variant": str(self.v0.id), "qty": 3}]}
        r = self.api.post(self.url, {"items": [item]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.v46.refresh_from_db()
        self.v0.refresh_from_db()
        self.assertEqual((self.v46.quantity, self.v0.quantity), (Decimal("5"), Decimal("3")))
        item["qty"] = 6
        r = self.api.post(self.url, {"items": [item]}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "variant_qty_mismatch"))
        self.v46.refresh_from_db()
        self.assertEqual(self.v46.quantity, Decimal("5"))

    def test_receipt_foreign_variant(self):
        r = self.api.post(self.url, {"items": [{"product": str(self.nike.id), "qty": 1, "variant": str(self.va.id)}]}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "variant_mismatch"))
