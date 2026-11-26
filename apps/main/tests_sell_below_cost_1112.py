"""12.11.2026 (ruflo S1/S3): скидка ниже закупочной — только с правом «Продажа ниже закупки»."""
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.main.models import Product
from apps.main.tests_tz_n15_checkout import CheckoutBase


class SellBelowCostTests(CheckoutBase):
    def setUp(self):
        super().setUp()
        self.p = self.mk(1)[0]
        Product.objects.filter(pk=self.p.pk).update(purchase_price=Decimal("8.00"))
        U = get_user_model()
        self.cashier = U.objects.create_user(email=f"c{uuid.uuid4().hex[:6]}@t.kg", password="x", role="cashier",
                                             company=self.company)
        self.open_shift()

    def line(self, p, qty="1", price="10.00", **kw):
        return {"product": str(p.id), "qty": qty, "price": price, **kw}

    def err(self, r, code, status=400):
        self.assertEqual(r.status_code, status, getattr(r, "data", None))
        self.assertEqual(r.data["code"], code, r.data)
        return r.data["errors"][0] if r.data.get("errors") else None

    def as_cashier(self, right=None):
        self.cashier.can_sell_below_cost = right
        self.cashier.save()
        from apps.construction.models import CashShift
        CashShift.objects.filter(company=self.company, status=CashShift.Status.OPEN).update(cashier=self.cashier)
        self.api = APIClient()
        self.api.force_authenticate(self.cashier)

    def test_discount_one_tyiyn_below_cost_rejected_without_right(self):
        """S1: цена 8.00 + скидка 0.01 раньше снимала проверку — теперь 400 без права."""
        self.as_cashier()
        r = self.co([self.line(self.p, price="8.00", discount="0.01")])
        e = self.err(r, "price_below_cost")
        self.assertEqual(e["min_price"], "8.00")

    def test_discount_below_cost_allowed_with_right_and_warns(self):
        self.as_cashier(True)
        r = self.co([self.line(self.p, price="10.00", discount="5.00")])
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.assertTrue(any(w.get("code") == "below_cost" for w in r.data.get("warnings") or []), r.data)

    def test_discount_above_cost_ok_without_right(self):
        self.as_cashier()
        r = self.co([self.line(self.p, price="10.00", discount="1.00")])
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))

    def test_owner_discount_below_cost_allowed(self):
        r = self.co([self.line(self.p, price="10.00", discount="5.00")])
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))

    def test_no_discount_below_cost_still_rejected_even_with_right(self):
        self.as_cashier(True)
        r = self.co([self.line(self.p, price="5.00")])
        self.err(r, "price_below_cost")
