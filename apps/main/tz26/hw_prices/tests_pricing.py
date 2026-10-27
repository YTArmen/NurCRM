# Тесты цен и продажи Б-55: python manage.py test apps.main.tz26.hw_prices.tests_pricing --settings=core.settings_test_sqlite
import uuid
from decimal import Decimal

from apps.main.models import PriceLevel, SaleItem
from apps.main.tz26.hw_prices.models import ProductLevelPrice, ProductVolumePrice
from apps.main.tz26.hw_prices.tests import HwBase
from apps.users.models import CompanyAddon

NEW = {"HTTP_X_NURMARKET_CLIENT": "kassa/1.17.61"}


class ProductPricesTests(HwBase):
    def test_put_get_prices_and_volume(self):
        self.api.post("/api/main/price-levels/", {"name": "Прораб"}, format="json")
        url = f"/api/main/products/{self.product.id}/prices/"
        r = self.api.put(
            url,
            {
                "prices": [
                    {"level": "retail", "price": "520"},
                    {"level": "wholesale", "price": "500"},
                    {"level": "prorab", "price": "480"},
                ],
                "volume_prices": [{"min_qty": 10, "price": 470}],
            },
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("520.00"))
        self.assertEqual(self.product.wholesale_price, Decimal("500.00"))
        body = self.api.get(url).json()
        self.assertEqual(
            {x["level"]: x["price"] for x in body["prices"]},
            {"retail": "520.00", "wholesale": "500.00", "prorab": "480.00"},
        )
        self.assertEqual(body["volume_prices"], [{"min_qty": "10", "price": "470.00", "level": None}])

    def test_put_without_prices_key_keeps_levels_and_replaces_volume(self):
        self.api.post("/api/main/price-levels/", {"name": "Прораб"}, format="json")
        url = f"/api/main/products/{self.product.id}/prices/"
        self.api.put(url, {"prices": [{"level": "retail", "price": "100"}, {"level": "prorab", "price": "90"}]}, format="json")
        self.api.put(url, {"volume_prices": [{"min_qty": "2.5", "price": "80", "level": "prorab"}]}, format="json")
        body = self.api.get(url).json()
        self.assertIn("prorab", [x["level"] for x in body["prices"]])
        self.assertEqual(body["volume_prices"][0]["min_qty"], "2.5")
        self.assertEqual(body["volume_prices"][0]["level"], "prorab")
        self.api.put(url, {"prices": [{"level": "retail", "price": "100"}]}, format="json")
        self.assertNotIn("prorab", [x["level"] for x in self.api.get(url).json()["prices"]])

    def test_bad_input(self):
        url = f"/api/main/products/{self.product.id}/prices/"
        r = self.api.put(url, {"prices": [{"level": "nope", "price": "1"}]}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.api.put(url, {"volume_prices": [{"min_qty": 0, "price": "1"}]}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.api.put(url, {"volume_prices": [{"min_qty": 5, "price": "1"}, {"min_qty": 5, "price": "2"}]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_cashier_cannot_write(self):
        _, api = self.user()
        r = api.put(f"/api/main/products/{self.product.id}/prices/", {"prices": []}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(api.get(f"/api/main/products/{self.product.id}/prices/").status_code, 200)

    def test_product_card_has_prices_and_patch_writes_them(self):
        self.api.post("/api/main/price-levels/", {"name": "Прораб"}, format="json")
        url = f"/api/main/products/{self.product.id}/"
        r = self.api.patch(
            url,
            {"prices": [{"level": "prorab", "price": "77"}], "volume_prices": [{"min_qty": 3, "price": "70"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        body = self.api.get(url).json()
        self.assertEqual({x["level"]: x["price"] for x in body["prices"]}["prorab"], "77.00")
        self.assertEqual(body["volume_prices"][0]["price"], "70.00")
        self.assertEqual(ProductLevelPrice.objects.filter(product=self.product).count(), 1)

    def test_product_list_has_no_prices_keys(self):
        r = self.api.get("/api/main/products/list/")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        rows = data.get("results") if isinstance(data, dict) else data
        self.assertTrue(rows)
        self.assertNotIn("volume_prices", rows[0])


class SalePricingTests(HwBase):
    """Приёмка Х4: прораб 480 (новая касса) и 520 (старая); от 10 мешков — 470."""

    def setUp(self):
        super().setUp()
        self.product.price = Decimal("520.00")
        self.product.wholesale_price = Decimal("500.00")
        self.product.quantity = Decimal("500")
        self.product.save()
        self.level = PriceLevel.objects.create(company=self.company, code="foreman", name="Прораб")
        ProductLevelPrice.objects.create(product=self.product, level=self.level, price=Decimal("480.00"))
        self.foreman = self.client_obj(price_level=self.level)
        self.retail_client = self.client_obj()
        self.open_shift()
        CompanyAddon.objects.create(company=self.company, code="hardware_2026_11")

    def co(self, client, qty="1", price="520.00", headers=None, **extra):
        item = {"product": str(self.product.id), "qty": qty}
        if price is not None:
            item["price"] = price
        body = {"items": [item], "client": str(client.id), "payment": {"method": "cash", "received": "1000000.00"}}
        body.update(extra)
        return self.api.post(
            "/api/main/pos/checkout/", body, format="json", HTTP_IDEMPOTENCY_KEY=f"k-{uuid.uuid4()}", **(headers or {})
        )

    def test_new_kassa_gets_level_price(self):
        r = self.co(self.foreman, headers=NEW, expected_total="480.00")
        self.assertEqual(r.status_code, 201, r.content)
        it = r.json()["items"][0]
        self.assertEqual(it["price"], "480.00")
        self.assertEqual(it["price_source"], "level:foreman")
        self.assertEqual(it["base_price"], "520.00")

    def test_new_kassa_price_omitted(self):
        r = self.co(self.foreman, price=None, headers=NEW)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["items"][0]["price"], "480.00")

    def test_kassa_sent_level_price_itself(self):
        r = self.co(self.foreman, price="480.00", headers=NEW, expected_total="480.00")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["items"][0]["price_source"], "level:foreman")

    def test_old_kassa_keeps_retail_without_total_mismatch(self):
        r = self.co(self.foreman, expected_total="520.00")
        self.assertEqual(r.status_code, 201, r.content)
        it = r.json()["items"][0]
        self.assertEqual(it["price"], "520.00")
        self.assertFalse(it.get("price_source"))

    def test_flag_off_new_kassa_unchanged(self):
        CompanyAddon.objects.filter(company=self.company).delete()
        r = self.co(self.foreman, headers=NEW, expected_total="520.00")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["items"][0]["price"], "520.00")

    def test_sale_item_saved_with_source(self):
        r = self.co(self.foreman, headers=NEW)
        si = SaleItem.objects.get(sale_id=r.json()["id"])
        self.assertEqual((si.price_source, si.base_price, si.unit_price), ("level:foreman", Decimal("520.00"), Decimal("480.00")))

    def test_volume_price_from_ten(self):
        ProductVolumePrice.objects.create(product=self.product, min_qty=Decimal("10"), price=Decimal("470.00"))
        r = self.co(self.foreman, qty="10", headers=NEW, expected_total="4700.00")
        self.assertEqual(r.status_code, 201, r.content)
        it = r.json()["items"][0]
        self.assertEqual((it["price"], it["price_source"]), ("470.00", "volume"))
        r9 = self.co(self.foreman, qty="9", headers=NEW)
        self.assertEqual(r9.json()["items"][0]["price"], "480.00")

    def test_volume_never_raises_price_above_level(self):
        ProductVolumePrice.objects.create(product=self.product, min_qty=Decimal("2"), price=Decimal("510.00"))
        r = self.co(self.foreman, qty="3", headers=NEW)
        self.assertEqual(r.json()["items"][0]["price"], "480.00")

    def test_volume_for_client_without_level(self):
        ProductVolumePrice.objects.create(product=self.product, min_qty=Decimal("10"), price=Decimal("470.00"))
        r = self.co(self.retail_client, qty="10", headers=NEW)
        self.assertEqual((r.json()["items"][0]["price"], r.json()["items"][0]["price_source"]), ("470.00", "volume"))
        r = self.co(self.retail_client, qty="1", headers=NEW)
        self.assertEqual((r.json()["items"][0]["price"], r.json()["items"][0]["price_source"]), ("520.00", "retail"))

    def test_manual_price_wins(self):
        r = self.co(self.foreman, price="495.00", headers=NEW)
        it = r.json()["items"][0]
        self.assertEqual((it["price"], it["price_source"], it["base_price"]), ("495.00", "manual", "520.00"))

    def test_manual_wholesale_wins(self):
        r = self.co(self.foreman, price="500.00", headers=NEW)
        it = r.json()["items"][0]
        self.assertEqual((it["price"], it["price_source"]), ("500.00", "wholesale"))
        r = self.co(self.foreman, price="520.00", headers=NEW, is_wholesale=True)
        self.assertEqual(r.json()["items"][0]["price_source"], "wholesale")

    def test_wholesale_level_uses_wholesale_price(self):
        lv = PriceLevel.objects.create(company=self.company, code="wholesale", name="Опт", is_system=True)
        c = self.client_obj(price_level=lv)
        r = self.co(c, headers=NEW)
        self.assertEqual((r.json()["items"][0]["price"], r.json()["items"][0]["price_source"]), ("500.00", "wholesale"))

    def test_cart_flow_checkout_with_client(self):
        r = self.api.post("/api/main/pos/sales/start/", {}, format="json", HTTP_IDEMPOTENCY_KEY=f"s-{uuid.uuid4()}", **NEW)
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        cart_id = body.get("id") or body.get("active_sale_id") or (body.get("sale") or {}).get("id")
        r = self.api.post(
            f"/api/main/pos/sales/{cart_id}/add-item/",
            {"product_id": str(self.product.id), "quantity": "1"},
            format="json", **NEW,
        )
        self.assertEqual(r.status_code, 201, r.content)
        r = self.api.post(
            f"/api/main/pos/sales/{cart_id}/checkout/",
            {"client_id": str(self.foreman.id), "payment_method": "cash", "cash_received": "1000"},
            format="json", HTTP_IDEMPOTENCY_KEY=f"c-{uuid.uuid4()}", **NEW,
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["total"], "480.00")

    def test_quote(self):
        payload = {"client": str(self.foreman.id), "items": [{"product": str(self.product.id), "qty": "2"}]}
        r = self.api.post("/api/main/price-levels/quote/", payload, format="json", **NEW)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["applied"])
        self.assertEqual(r.json()["items"][0]["price"], "480.00")
        r = self.api.post("/api/main/price-levels/quote/", payload, format="json")
        self.assertFalse(r.json()["applied"])
