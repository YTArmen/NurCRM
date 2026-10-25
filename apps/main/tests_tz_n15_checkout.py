"""ТЗ Н-15: продажа одним запросом (большие чеки, коды отказов, досылка)."""
import time
import uuid
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.main.models import Product
from apps.possync.tests import PossyncBase


class CheckoutBase(PossyncBase):
    def co(self, items, payment=None, key=None, **extra):
        body = {"items": items, "payment": payment or {"method": "cash", "received": "1000000.00"}}
        body.update(extra)
        return self.api.post("/api/main/pos/checkout/", body, format="json",
                             HTTP_IDEMPOTENCY_KEY=key or f"k-{uuid.uuid4()}")

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


class _ProbeTests(CheckoutBase):
    def test_probe_old_negative_custom(self):
        self.open_shift()
        p = self.mk(1)[0]
        r = self.api.post("/api/main/pos/sales/start/", {}, format="json")
        print("START", r.status_code, list(r.data)[:8])
        cid = r.data.get("id")
        r = self.api.post(f"/api/main/pos/sales/{cid}/add-item/", {"product_id": str(p.id), "quantity": "2"}, format="json")
        print("ADD", r.status_code)
        r = self.api.post(f"/api/main/pos/carts/{cid}/custom-item/", {"name": "Скидка", "price": "-5.00", "quantity": 1}, format="json")
        print("CUSTOM", r.status_code, r.data.get("total") if hasattr(r.data, "get") else r.data)
        r = self.api.post(f"/api/main/pos/sales/{cid}/checkout/", {"payment_method": "cash", "cash_received": "100"}, format="json", HTTP_IDEMPOTENCY_KEY="x1")
        print("CO", r.status_code, r.data.get("total"), r.data.get("subtotal"), r.data.get("discount_total"), [k for k in r.data][:6])
