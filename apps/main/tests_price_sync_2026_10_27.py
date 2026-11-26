"""
ТЗ «цена продажи меняется сама и не доходит до касс» (27.10.2026).

Б-06: PATCH purchase_price / приход не трогают price; пересчёт только по recalc_price.
Б-46: любое изменение двигает updated_at; лента GET products/changes/?since=.
Б-03: pos/checkout/ сверяет цены строк с каталогом: price_mismatch в ответе, 409 в строгом режиме.
"""
import uuid
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import AuditLog, Cashbox, CashShift
from apps.main.models import Client, Product, Sale
from apps.main.tests_kassa_api import KassaBase
from apps.users.models import Company, SubscriptionPlan, User


class _Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.plan = SubscriptionPlan.objects.create(name="Стандарт", price=Decimal("1.00"))
        self.company = Company.objects.create(name="Price Co", owner=self.owner, is_active=True, subscription_plan=self.plan)
        self.owner.company = self.company
        self.owner.save()
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        # Пример из ТЗ: закупка 150, цена 195 (наценка 30 %).
        self.product = Product.objects.create(
            company=self.company, name="Товар 30%", purchase_price=Decimal("150"),
            markup_percent=Decimal("30"), price=Decimal("195"), quantity=Decimal("10"),
        )

    def patch(self, body, pk=None):
        return self.api.patch(f"/api/main/products/{pk or self.product.id}/", body, format="json")


class PatchPurchasePriceTests(_Base):
    """Б-06, приёмка из ТЗ: PATCH purchase_price не меняет цену продажи."""

    def test_purchase_price_alone_keeps_price_and_recalcs_markup(self):
        r = self.patch({"purchase_price": "160"})
        self.assertEqual(r.status_code, 200, r.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("195.000"))
        self.assertEqual(self.product.purchase_price, Decimal("160.000"))
        self.assertEqual(self.product.markup_percent, Decimal("21.8750"))
        self.assertIs(r.data["price_changed"], False)
        self.assertEqual(Decimal(r.data["price"]), Decimal("195.00"))
        self.assertEqual(Decimal(r.data["markup_percent"]), Decimal("21.8750"))

    def test_acceptance_nbs_11_to_1(self):
        p = Product.objects.create(
            company=self.company, name="NBS", purchase_price=Decimal("11"),
            markup_percent=Decimal("36.3636"), price=Decimal("15"), quantity=Decimal("1"),
        )
        r = self.patch({"purchase_price": "1.00"}, pk=p.id)
        self.assertEqual(r.status_code, 200, r.data)
        p.refresh_from_db()
        self.assertEqual(p.price, Decimal("15.000"))
        self.assertIs(r.data["price_changed"], False)

        r = self.patch({"purchase_price": "11.00", "recalc_price": True}, pk=p.id)
        self.assertEqual(r.status_code, 200, r.data)
        p.refresh_from_db()
        # наценка после первого PATCH = (15-1)/1*100 = 1400 % → 11 × 15 = 165
        self.assertEqual(p.price, Decimal("165.000"))
        self.assertIs(r.data["price_changed"], True)

    def test_recalc_price_flag_recalculates_by_current_markup(self):
        r = self.patch({"purchase_price": "160", "recalc_price": True})
        self.assertEqual(r.status_code, 200, r.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("208.000"))  # 160 × 1.30
        self.assertEqual(self.product.markup_percent, Decimal("30.0000"))
        self.assertIs(r.data["price_changed"], True)

    def test_explicit_price_still_wins(self):
        r = self.patch({"purchase_price": "160", "price": "200"})
        self.assertEqual(r.status_code, 200, r.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("200.000"))
        self.assertEqual(self.product.markup_percent, Decimal("25.0000"))
        self.assertIs(r.data["price_changed"], True)

    def test_explicit_markup_recalculates_price(self):
        r = self.patch({"markup_percent": "40"})
        self.assertEqual(r.status_code, 200, r.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("210.000"))
        self.assertIs(r.data["price_changed"], True)

    def test_price_change_is_journaled(self):
        self.patch({"price": "199"})
        row = AuditLog.objects.filter(company=self.company, object_type="product", action="price_change").first()
        self.assertIsNotNone(row)
        self.assertEqual(row.before["price"], "195")
        self.assertEqual(row.after["price"], "199")

    def test_model_save_does_not_recalc_price_for_existing_product(self):
        # Любой save() существующего товара (ревизия, остаток, название) цену не трогает.
        self.product.purchase_price = Decimal("160")
        self.product.save()
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("195.000"))

    def test_model_create_still_derives_price_from_markup(self):
        p = Product.objects.create(
            company=self.company, name="Новый", purchase_price=Decimal("100"), markup_percent=Decimal("25"),
        )
        self.assertEqual(p.price, Decimal("125.00"))


class ReceiptPriceTests(_Base):
    def setUp(self):
        super().setUp()
        self.supplier = Client.objects.create(
            company=self.company, type=Client.StatusClient.SUPPLIERS, full_name="Поставщик", phone="+996700000001"
        )

    def receipt(self, item):
        return self.api.post(
            f"/api/main/suppliers/{self.supplier.id}/receipt/", {"items": [item], "payment_type": "cash"}, format="json"
        )

    def test_receipt_with_new_purchase_price_keeps_price(self):
        r = self.receipt({"product": str(self.product.id), "qty": 5, "purchase_price": 160})
        self.assertEqual(r.status_code, 200, r.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("15"))
        self.assertEqual(self.product.purchase_price, Decimal("160.000"))
        self.assertEqual(self.product.price, Decimal("195.000"))
        self.assertEqual(self.product.markup_percent, Decimal("21.8750"))  # наценка хранится с 4 знаками (ТЗ Н-04)

    def test_receipt_recalc_price_flag(self):
        r = self.receipt({"product": str(self.product.id), "qty": 5, "purchase_price": 160, "recalc_price": True})
        self.assertEqual(r.status_code, 200, r.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("208.000"))
        self.assertEqual(self.product.markup_percent, Decimal("30.0000"))
        row = AuditLog.objects.filter(company=self.company, object_type="product", action="price_change").first()
        self.assertIsNotNone(row)
        self.assertEqual(row.after["price"], "208.00")

    def test_mass_incoming_purchase_price_keeps_price(self):
        r = self.api.post(
            "/api/main/products/mass-incoming/",
            {"items": [{"product_id": str(self.product.id), "quantity": "2", "purchase_price": "160"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.product.refresh_from_db()
        self.assertEqual((self.product.quantity, self.product.price), (Decimal("12"), Decimal("195.000")))
        self.assertEqual(self.product.purchase_price, Decimal("160.000"))

    def test_mass_incoming_recalc_price(self):
        r = self.api.post(
            "/api/main/products/mass-incoming/",
            {"items": [{"product_id": str(self.product.id), "quantity": "2", "purchase_price": "160",
                        "recalc_price": True}]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("208.000"))


class UpdatedAtAndChangesFeedTests(_Base):
    URL = "/api/main/products/changes/"

    def test_partial_save_moves_updated_at(self):
        before = self.product.updated_at
        self.product.price = Decimal("199")
        self.product.save(update_fields=["price"])
        self.product.refresh_from_db()
        self.assertGreater(self.product.updated_at, before)

    def test_queryset_update_moves_updated_at(self):
        before = self.product.updated_at
        Product.objects.filter(pk=self.product.pk).update(quantity=Decimal("3"))
        self.product.refresh_from_db()
        self.assertGreater(self.product.updated_at, before)

    def test_kassa_version_query_sees_price_change(self):
        # Тот же запрос, что делает касса.
        other = Product.objects.create(company=self.company, name="Другой", price=Decimal("5"))
        r = self.api.get("/api/main/products/list/?page=1&page_size=1&ordering=-updated_at")
        self.assertEqual(r.data["results"][0]["id"], str(other.id))
        self.api.patch(f"/api/main/products/{self.product.id}/", {"price": "199"}, format="json")
        r = self.api.get("/api/main/products/list/?page=1&page_size=1&ordering=-updated_at")
        self.assertEqual(r.data["results"][0]["id"], str(self.product.id))

    def test_changes_feed_returns_only_changed_and_304(self):
        r0 = self.api.get(self.URL)
        self.assertEqual(r0.status_code, 200, r0.data)
        self.assertEqual({c["id"] for c in r0.data["changes"]}, {str(self.product.id)})
        cursor = r0.data["cursor"]
        self.assertIsInstance(cursor, int)

        r1 = self.api.get(self.URL, {"since": cursor})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.data["changes"], [])
        etag = r1["ETag"]
        r1b = self.api.get(self.URL, {"since": cursor}, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r1b.status_code, 304)

        self.api.patch(f"/api/main/products/{self.product.id}/", {"price": "199"}, format="json")
        r2 = self.api.get(self.URL, {"since": cursor}, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(len(r2.data["changes"]), 1)
        row = r2.data["changes"][0]
        self.assertEqual(row["id"], str(self.product.id))
        self.assertEqual(Decimal(row["price"]), Decimal("199"))
        self.assertEqual(Decimal(row["purchase_price"]), Decimal("150"))
        self.assertIn("quantity", row)
        self.assertIn("discount_percent", row)
        self.assertFalse(r2.data["has_more"])

    def test_changes_feed_reports_deleted(self):
        r0 = self.api.get(self.URL)
        cursor = r0.data["cursor"]
        pid = str(self.product.id)
        self.api.delete(f"/api/main/products/{self.product.id}/")
        r = self.api.get(self.URL, {"since": cursor})
        self.assertIn(pid, r.data["deleted"])

    def test_changes_feed_has_more_and_cursor(self):
        for i in range(3):
            Product.objects.create(company=self.company, name=f"P{i}", price=Decimal("1"))
        r = self.api.get(self.URL, {"limit": 2})
        self.assertTrue(r.data["has_more"])
        self.assertEqual(len(r.data["changes"]), 2)
        r2 = self.api.get(self.URL, {"since": r.data["cursor"], "limit": 100})
        ids = {c["id"] for c in r.data["changes"]} | {c["id"] for c in r2.data["changes"]}
        self.assertEqual(len(ids), 4)

    def test_changes_feed_bad_since(self):
        r = self.api.get(self.URL, {"since": "вчера"})
        self.assertEqual(r.status_code, 400)


class CheckoutPriceMismatchTests(KassaBase):
    """Б-03: товар в каталоге стоит 100, касса шлёт 205."""

    def _mismatch(self, r):
        return r.data.get("price_mismatch")

    def test_mismatch_reported_sale_passes(self):
        r = self.quick(items=[{"product": str(self.product.id), "qty": "1", "price": "205.00"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(self._mismatch(r)), 1)
        m = self._mismatch(r)[0]
        self.assertEqual((m["product_id"], m["sent"], m["current"]), (str(self.product.id), "205.00", "100.00"))
        self.assertEqual(Sale.objects.count(), 1)
        row = AuditLog.objects.filter(company=self.company, object_type="sale", action="price_mismatch").first()
        self.assertIsNotNone(row)
        self.assertEqual(row.after["items"][0]["sent"], "205.00")

    def test_no_mismatch_when_price_matches(self):
        r = self.quick()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self._mismatch(r), [])

    def test_no_mismatch_without_price_or_with_override_or_discount(self):
        r = self.quick(items=[{"product": str(self.product.id), "qty": "1"}])
        self.assertEqual(self._mismatch(r), [])
        r = self.quick(items=[{"product": str(self.product.id), "qty": "1", "price": "90.00", "price_override": True}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self._mismatch(r), [])
        r = self.quick(price_override=True, items=[{"product": str(self.product.id), "qty": "1", "price": "90.00"}])
        self.assertEqual(self._mismatch(r), [])
        r = self.quick(items=[{"product": str(self.product.id), "qty": "1", "price": "100.00", "discount": "10.00"}])
        self.assertEqual(self._mismatch(r), [])

    def test_strict_header_returns_409_and_nothing_is_sold(self):
        r = self.api.post(
            "/api/main/pos/checkout/",
            {"items": [{"product": str(self.product.id), "qty": "1", "price": "205.00"}],
             "payment": {"method": "cash", "received": "500.00"}},
            format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()), HTTP_X_NURMARKET_STRICT_PRICE="1",
        )
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["code"], "price_changed")
        self.assertEqual(r.data["price_mismatch"][0]["current"], "100.00")
        self.assertEqual(Sale.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("50"))

    def test_strict_header_with_matching_price_sells(self):
        r = self.api.post(
            "/api/main/pos/checkout/",
            {"items": [{"product": str(self.product.id), "qty": "1", "price": "100.00"}],
             "payment": {"method": "cash", "received": "500.00"}},
            format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()), HTTP_X_NURMARKET_STRICT_PRICE="1",
        )
        self.assertEqual(r.status_code, 201, r.data)

    def test_batch_checkout_strict_per_item(self):
        r = self.api.post(
            "/api/main/pos/checkout/batch/",
            {"items": [
                {"idempotency_key": str(uuid.uuid4()),
                 "body": {"items": [{"product": str(self.product.id), "qty": "1", "price": "205.00"}],
                          "payment": {"method": "cash", "received": "500.00"}}},
                {"idempotency_key": str(uuid.uuid4()),
                 "body": {"items": [{"product": str(self.product.id), "qty": "1", "price": "100.00"}],
                          "payment": {"method": "cash", "received": "500.00"}}},
            ]},
            format="json", HTTP_X_NURMARKET_STRICT_PRICE="1",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([row["status"] for row in r.data["results"]], [409, 201])
        self.assertEqual(r.data["results"][0]["detail"]["code"], "price_changed")
