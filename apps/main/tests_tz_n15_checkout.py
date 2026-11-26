"""ТЗ Н-15: продажа одним запросом (большие чеки, коды отказов, досылка)."""
import time
import uuid
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import CashShift
from apps.main.models import (
    Cart, CartItem, Client, ClientDeal, Product, ProductPackage, ProductVariant, Sale,
)
from apps.possync.tests import PossyncBase


class CheckoutBase(PossyncBase):
    client_hdr = "kassa/1.17.61"  # новый клиент: строгое поведение (Б-02)

    def co(self, items, payment=None, key=None, **extra):
        body = {"items": items, "payment": payment or {"method": "cash", "received": "1000000.00"}}
        body.update(extra)
        hdr = {"HTTP_X_NURMARKET_CLIENT": self.client_hdr} if self.client_hdr else {}
        return self.api.post("/api/main/pos/checkout/", body, format="json",
                             HTTP_IDEMPOTENCY_KEY=key or f"k-{uuid.uuid4()}", **hdr)

    def mk(self, n=1, **kw):
        return [Product.objects.create(company=self.company, name=f"Т{uuid.uuid4().hex[:5]}",
                                       price=Decimal("10.00"), quantity=Decimal("100"), **kw) for _ in range(n)]


class BigCheckoutTests(CheckoutBase):
    def _run(self, n):
        prods = self.mk(n)
        items = [{"product": str(p.id), "qty": "2", "price": "10.00"} for p in prods]
        t = time.monotonic()
        with CaptureQueriesContext(connection) as ctx:
            r = self.co(items, expected_total=str(Decimal("20.00") * n))
        return r, len(ctx), time.monotonic() - t

    def test_300_items_fast_and_flat_queries(self):
        """ТЗ Н-15: 300 позиций — < 3 с, число запросов не растёт с размером чека."""
        self.open_shift()
        r10, q10, _ = self._run(10)
        self.assertEqual(r10.status_code, 201, getattr(r10, "data", None))
        r, q300, dt = self._run(300)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.assertEqual(len(r.data["items"]), 300)
        self.assertTrue(r.data["number"])
        self.assertLess(dt, 3.0)
        self.assertLessEqual(q300, q10 + 15, (q10, q300))  # константа, а не «по запросу на позицию»
        self.assertLess(q300, 260)

    def test_1000_items_accepted_1001_rejected(self):
        self.open_shift()
        r, _q, dt = self._run(1000)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        items = [{"product": str(uuid.uuid4()), "qty": "1", "price": "1.00"}] * 1001
        r = self.co(items)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "too_many_items")
        self.assertEqual(r.data["limit"], 1000)


class RefusalCodesTests(CheckoutBase):
    def setUp(self):
        super().setUp()
        self.open_shift()

    def line(self, p, qty="1", price="10.00", **kw):
        return {"product": str(p.id), "qty": qty, "price": price, **kw}

    def err(self, r, code, status=400):
        self.assertEqual(r.status_code, status, getattr(r, "data", None))
        self.assertEqual(r.data["code"], code, r.data)
        return r.data["errors"][0] if r.data.get("errors") else None

    def test_variant_required_and_not_found_with_index(self):
        good, shirt = self.mk(2)
        ProductVariant.objects.create(company=self.company, product=shirt, size="S", color="Ч",
                                      quantity=Decimal("5"), is_active=True)
        r = self.co([self.line(good), self.line(shirt)])
        e = self.err(r, "variant_required")
        self.assertEqual(e["index"], 1)
        self.assertIn("items", r.data)  # прежний ключ сохранён
        r = self.co([self.line(good), self.line(good), self.line(shirt, variant=str(uuid.uuid4()))])
        self.assertEqual(self.err(r, "variant_not_found")["index"], 2)
        self.assertEqual(Sale.objects.count(), 0)

    def test_product_not_found(self):
        good = self.mk(1)[0]
        r = self.co([self.line(good), {"product": str(uuid.uuid4()), "qty": "1", "price": "1.00"}])
        self.assertEqual(self.err(r, "product_not_found")["index"], 1)

    def test_insufficient_stock_index_available_requested(self):
        a, b = self.mk(2)
        r = self.co([self.line(a), self.line(b, qty="150")])
        e = self.err(r, "insufficient_stock")
        self.assertEqual((e["index"], e["available"], e["requested"]), (1, "100.000", "150.000"))
        self.assertEqual(Sale.objects.count(), 0)
        b.refresh_from_db()
        self.assertEqual(b.quantity, Decimal("100"))

    def test_price_below_cost(self):
        p = self.mk(1, )[0]
        Product.objects.filter(pk=p.pk).update(purchase_price=Decimal("8.00"))
        r = self.co([self.line(p, price="5.00")])
        e = self.err(r, "price_below_cost")
        self.assertEqual(e["index"], 0)
        self.assertEqual(e["min_price"], "8.00")

    def test_discount_limit_exceeded_in_errors_old_code_kept(self):
        p = self.mk(1)[0]
        self.company.max_discount_percent = Decimal("10")
        self.company.save()
        cashier = get_user_model().objects.create_user(
            email=f"c{uuid.uuid4().hex[:6]}@t.kg", password="x", company=self.company, role="salesperson")
        sh = CashShift.objects.create(company=self.company, cashbox=self.cashbox, cashier=cashier,
                                      status=CashShift.Status.OPEN)
        api = APIClient()
        api.force_authenticate(cashier)
        r = api.post("/api/main/pos/checkout/", {
            "shift": str(sh.id),
            "items": [self.line(p, qty="2", discount="10.00")],
            "payment": {"method": "cash", "received": "100"},
        }, format="json", HTTP_IDEMPOTENCY_KEY="dl-1")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "discount_limit")
        self.assertEqual(r.data["errors"][0]["code"], "discount_limit_exceeded")
        self.assertEqual(r.data["errors"][0]["index"], 0)

    def test_cash_less_than_total_and_mixed_mismatch(self):
        p = self.mk(1)[0]
        r = self.co([self.line(p, qty="5")], payment={"method": "cash", "received": "10.00"})
        e = self.err(r, "cash_less_than_total")
        self.assertEqual((r.data["total"], r.data["cash_received"]), ("50.00", "10.00"))
        self.assertIn("detail", r.data)
        r = self.co([self.line(p, qty="5")],
                    payment={"method": "mixed", "cash_amount": "20.00", "card_amount": "10.00"})
        self.err(r, "mixed_sum_mismatch")
        self.assertEqual(r.data["total"], "50.00")
        r = self.co([self.line(p, qty="5")], payment={"payments": [{"method": "cash", "amount": "20.00"}]})
        self.err(r, "mixed_sum_mismatch")
        self.assertEqual(Sale.objects.count(), 0)

    def test_shift_closed_has_errors(self):
        CashShift.objects.update(status=CashShift.Status.CLOSED)
        p = self.mk(1)[0]
        r = self.co([self.line(p)])
        e = self.err(r, "shift_closed")
        self.assertEqual(e["code"], "shift_closed")

    def test_expected_total_mismatch_409_no_sale(self):
        p = self.mk(1)[0]
        r = self.co([self.line(p, qty="3")], expected_total="29.00")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["code"], "total_mismatch")
        self.assertEqual((r.data["server_total"], r.data["expected_total"]), ("30.00", "29.00"))
        self.assertEqual(len(r.data["lines"]), 1)
        self.assertEqual(r.data["lines"][0]["amount"], "30.00")
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(Cart.objects.count(), 0)
        # в пределах 0.01 — проходит; тот же ключ после 409 не «залип»
        r = self.co([self.line(p, qty="3")], expected_total="30.01", key="same")
        self.assertEqual(r.status_code, 201, r.data)

    def test_expected_total_counts_order_discount(self):
        p = self.mk(1)[0]
        r = self.co([self.line(p, qty="10")], order_discount_total="10.00", expected_total="90.00")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["total"], "90.00")


class NumberAndPathsTests(CheckoutBase):
    def setUp(self):
        super().setUp()
        self.open_shift()

    def test_number_everywhere(self):
        p = self.mk(1)[0]
        item = {"product": str(p.id), "qty": "1", "price": "10.00"}
        r = self.co([item], key="n1")
        self.assertEqual(r.status_code, 201)
        num = r.data["number"]
        self.assertTrue(num)
        again = self.co([item], key="n1")
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.data["replayed"])
        self.assertEqual(again.data["number"], num)
        # старый путь
        cart = self.api.post("/api/main/pos/sales/start/", {}, format="json").data["id"]
        self.api.post(f"/api/main/pos/sales/{cart}/add-item/", {"product_id": str(p.id), "quantity": "1"}, format="json")
        old = self.api.post(f"/api/main/pos/sales/{cart}/checkout/", {"payment_method": "cash", "cash_received": "100"},
                            format="json", HTTP_IDEMPOTENCY_KEY="n-old")
        self.assertEqual(old.status_code, 201, old.data)
        self.assertEqual(old.data["number"], num + 1)
        # пакет
        b = self.api.post("/api/main/pos/checkout/batch/", {"items": [
            {"idempotency_key": "b1", "body": {"items": [item], "payment": {"method": "cash", "received": "10"}}},
            {"idempotency_key": "b2", "body": {"items": [item], "payment": {"method": "cash", "received": "1"}}},
        ]}, format="json")
        self.assertEqual(b.status_code, 200)
        self.assertEqual(b.data["results"][0]["status"], 201, b.data)
        self.assertTrue(b.data["results"][0]["sale"]["number"])
        self.assertEqual(b.data["results"][1]["status"], 400)
        self.assertEqual(b.data["results"][1]["detail"]["code"], "cash_less_than_total")

    def test_old_path_codes(self):
        p = self.mk(1)[0]
        cart = self.api.post("/api/main/pos/sales/start/", {}, format="json").data["id"]
        self.api.post(f"/api/main/pos/sales/{cart}/add-item/", {"product_id": str(p.id), "quantity": "1"}, format="json")
        r = self.api.post(f"/api/main/pos/sales/{cart}/checkout/", {"payment_method": "cash", "cash_received": "1"},
                          format="json", HTTP_IDEMPOTENCY_KEY="o1")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "cash_less_than_total")
        self.assertIn("detail", r.data)

    def test_old_path_variant_required_code(self):
        p = self.mk(1)[0]
        ProductVariant.objects.create(company=self.company, product=p, size="S", color="Ч",
                                      quantity=Decimal("5"), is_active=True)
        cart = self.api.post("/api/main/pos/sales/start/", {}, format="json").data["id"]
        self.api.post(f"/api/main/pos/sales/{cart}/add-item/", {"product_id": str(p.id), "quantity": "1"}, format="json")
        r = self.api.post(f"/api/main/pos/sales/{cart}/checkout/", {"payment_method": "cash", "cash_received": "100"},
                          format="json", HTTP_IDEMPOTENCY_KEY="o2")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "variant_required")

    def test_negative_custom_rejected_on_both_paths(self):
        r = self.co([{"custom": True, "name": "Скидка", "qty": "1", "price": "-5.00"}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "negative_price")
        self.assertEqual(r.data["errors"][0]["index"], 0)
        cart = self.api.post("/api/main/pos/sales/start/", {}, format="json").data["id"]
        r = self.api.post(f"/api/main/pos/carts/{cart}/custom-item/", {"name": "Скидка", "price": "-5.00"}, format="json",
                          HTTP_X_NURMARKET_CLIENT="kassa/1.17.61")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "negative_price")
        # положительная — как раньше на обоих
        r = self.co([{"custom": True, "name": "Доставка", "qty": "1", "price": "5.00"}])
        self.assertEqual(r.status_code, 201, r.data)
        r = self.api.post(f"/api/main/pos/carts/{cart}/custom-item/", {"name": "Доставка", "price": "5.00"}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_sale_package_on_checkout_and_batch_same_result(self):
        p = self.mk(1)[0]
        Product.objects.filter(pk=p.pk).update(purchase_price=Decimal("0.50"))
        pack = ProductPackage.objects.create(company=self.company, product=p, name="Пачка", quantity_in_package=Decimal("10"))
        line = {"product": str(p.id), "sale_package_id": str(pack.id), "qty": "2", "price": "90.00"}
        pay = {"method": "cash", "received": "1000"}
        r1 = self.co([line], payment=pay)
        self.assertEqual(r1.status_code, 201, r1.data)
        b = self.api.post("/api/main/pos/checkout/batch/", {"items": [
            {"idempotency_key": "pk1", "body": {"items": [line], "payment": pay}}]}, format="json")
        r2 = b.data["results"][0]
        self.assertEqual(r2["status"], 201, r2)
        for resp in (r1.data, r2["sale"]):
            self.assertEqual(resp["total"], "180.00")
            self.assertEqual(resp["items"][0]["qty"], "20.000")
        p.refresh_from_db()
        self.assertEqual(p.quantity, Decimal("60.000"))  # 2 продажи по 20 ед.


class OfflineResendTests(CheckoutBase):
    def setUp(self):
        super().setUp()
        self.open_shift()
        self.sold_at = (timezone.now() - timedelta(hours=2)).isoformat()

    def test_offline_negative_stock_and_variant_missing(self):
        a, shirt = self.mk(2)
        Product.objects.filter(pk=a.pk).update(purchase_price=Decimal("9.00"))
        ProductVariant.objects.create(company=self.company, product=shirt, size="S", color="Ч",
                                      quantity=Decimal("5"), is_active=True)
        items = [
            self.line(a, qty="150", price="5.00"),     # остаток 100, цена ниже закупки — офлайн проводим
            self.line(shirt, qty="2", price="10.00"),  # вариант не выбран
        ]
        r = self.co(items, offline=True, sold_at=self.sold_at, key="off1")
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        codes = {(w["code"], w["index"]) for w in r.data["warnings"]}
        self.assertIn(("negative_stock", 0), codes)
        self.assertIn(("variant_missing", 1), codes)
        flagged = [it for it in r.data["items"] if it.get("variant_missing")]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["product"], str(shirt.id))
        a.refresh_from_db()
        self.assertEqual(a.quantity, Decimal("-50"))
        self.assertEqual(r.data["total"], "770.00")

    def line(self, p, qty="1", price="10.00", **kw):
        return {"product": str(p.id), "qty": qty, "price": price, **kw}

    def test_online_same_body_is_rejected(self):
        a = self.mk(1)[0]
        r = self.co([self.line(a, qty="150")])
        self.assertEqual(r.data["code"], "insufficient_stock")

    def test_offline_still_rejects_product_not_found_and_cash(self):
        a = self.mk(1)[0]
        r = self.co([{"product": str(uuid.uuid4()), "qty": "1", "price": "1.00"}], offline=True, sold_at=self.sold_at)
        self.assertEqual(r.data["code"], "product_not_found")
        r = self.co([self.line(a, qty="5")], offline=True, sold_at=self.sold_at,
                    payment={"method": "cash", "received": "1.00"})
        self.assertEqual(r.data["code"], "cash_less_than_total")

    def test_offline_replay_same_key(self):
        a = self.mk(1)[0]
        r1 = self.co([self.line(a)], offline=True, sold_at=self.sold_at, key="off-r")
        r2 = self.co([self.line(a)], offline=True, sold_at=self.sold_at, key="off-r")
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertTrue(r2.data["replayed"])
        self.assertEqual(r2.data["number"], r1.data["number"])


class DebtDueDateTests(CheckoutBase):
    def setUp(self):
        super().setUp()
        self.open_shift()
        self.client_obj = Client.objects.create(company=self.company, full_name="Должник", phone="+996555111222")

    def test_debt_due_date_sets_deal(self):
        p = self.mk(1)[0]
        due = (timezone.localdate() + timedelta(days=10)).isoformat()
        for ver in ("v2", "v1"):
            r = self.co([{"product": str(p.id), "qty": "5", "price": "10.00"}],
                        payment={"method": "debt", "debt_due_date": due}, client=str(self.client_obj.id),
                        schedule_version=ver, key=f"dd-{ver}")
            self.assertEqual(r.status_code, 201, r.data)
            deal = ClientDeal.objects.get(sale_id=r.data["id"])
            self.assertEqual(deal.first_due_date.isoformat(), due)
            self.assertEqual(deal.schedule_version, ver)
            inst = list(deal.installments.all())
            self.assertEqual([i.due_date.isoformat() for i in inst], [due])
            self.assertEqual(inst[0].amount, Decimal("50.00"))

    def test_without_due_date_default_30_days(self):
        p = self.mk(1)[0]
        r = self.co([{"product": str(p.id), "qty": "5", "price": "10.00"}],
                    payment={"method": "debt"}, client=str(self.client_obj.id))
        deal = ClientDeal.objects.get(sale_id=r.data["id"])
        self.assertEqual(deal.installments.first().due_date, timezone.localdate() + timedelta(days=30))

    def test_past_due_date_rejected(self):
        p = self.mk(1)[0]
        past = (timezone.localdate() - timedelta(days=1)).isoformat()
        r = self.co([{"product": str(p.id), "qty": "5", "price": "10.00"}],
                    payment={"method": "debt", "debt_due_date": past}, client=str(self.client_obj.id))
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "invalid_debt_schedule")
        self.assertEqual(Sale.objects.count(), 0)


class BulkAndClearTests(CheckoutBase):
    def setUp(self):
        super().setUp()
        self.open_shift()
        self.cart = self.api.post("/api/main/pos/sales/start/", {}, format="json").data["id"]

    def test_bulk_adds_many_and_merges(self):
        prods = self.mk(120)
        body = {"items": [{"product_id": str(p.id), "quantity": "2"} for p in prods]
                + [{"product_id": str(prods[0].id), "quantity": "1"},
                   {"custom": True, "name": "Доставка", "price": "50.00", "quantity": "1"}]}
        t = time.monotonic()
        r = self.api.post(f"/api/main/pos/sales/{self.cart}/items/bulk/", body, format="json")
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.assertLess(time.monotonic() - t, 30.0)
        self.assertEqual(CartItem.objects.filter(cart_id=self.cart).count(), 121)
        self.assertEqual(CartItem.objects.get(cart_id=self.cart, product=prods[0]).quantity, Decimal("3.000"))
        self.assertEqual(len(r.data["added_items"]), 122)
        cart = Cart.objects.get(pk=self.cart)
        self.assertEqual(cart.total, Decimal("20.00") * 119 + Decimal("30.00") + Decimal("50.00") + Decimal("0"))
        # продаётся нормально
        co = self.api.post(f"/api/main/pos/sales/{self.cart}/checkout/", {"payment_method": "cash", "cash_received": "100000"},
                           format="json", HTTP_IDEMPOTENCY_KEY="bk1")
        self.assertEqual(co.status_code, 201, co.data)

    def test_bulk_all_or_nothing_with_index(self):
        a, b = self.mk(2)
        r = self.api.post(f"/api/main/pos/sales/{self.cart}/items/bulk/", {"items": [
            {"product_id": str(a.id), "quantity": "1"},
            {"product_id": str(b.id), "quantity": "500"},
        ]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "insufficient_stock")
        self.assertEqual(r.data["errors"][0]["index"], 1)
        self.assertEqual(CartItem.objects.filter(cart_id=self.cart).count(), 0)
        r = self.api.post(f"/api/main/pos/sales/{self.cart}/items/bulk/", {"items": [
            {"custom": True, "name": "x", "price": "-1", "quantity": "1"}]}, format="json",
            HTTP_X_NURMARKET_CLIENT="kassa/1.17.61")
        self.assertEqual(r.data["code"], "negative_price")
        r = self.api.post(f"/api/main/pos/sales/{self.cart}/items/bulk/", {"items": [
            {"product_id": str(uuid.uuid4()), "quantity": "1"}]}, format="json")
        self.assertEqual(r.data["code"], "product_not_found")

    def test_clear_removes_items_and_order_discount(self):
        a = self.mk(1)[0]
        self.api.post(f"/api/main/pos/sales/{self.cart}/items/bulk/", {"items": [
            {"product_id": str(a.id), "quantity": "10", "discount_total": "5.00"}]}, format="json")
        r = self.api.patch(f"/api/main/pos/carts/{self.cart}/", {"order_discount_total": "20.00"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        cart = Cart.objects.get(pk=self.cart)
        self.assertEqual(cart.order_discount_total, Decimal("20.00"))
        r = self.api.post(f"/api/main/pos/carts/{self.cart}/clear/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        cart.refresh_from_db()
        self.assertEqual(CartItem.objects.filter(cart=cart).count(), 0)
        self.assertEqual(cart.order_discount_total, Decimal("0.00"))
        self.assertIsNone(cart.order_discount_percent)
        self.assertEqual((cart.subtotal, cart.discount_total, cart.total), (Decimal("0.00"),) * 3)
        # чужая/закрытая корзина — 404
        self.assertEqual(self.api.post(f"/api/main/pos/carts/{uuid.uuid4()}/clear/", {}, format="json").status_code, 404)
