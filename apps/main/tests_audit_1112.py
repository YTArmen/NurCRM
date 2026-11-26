"""Аудит 12.11.2026: касса — удаление/статус чека, закрытая смена в корзинном пути, переполнение суммы."""
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.construction.models import CashShift
from apps.main.models import Sale
from apps.main.tests_tz_n15_checkout import CheckoutBase

User = get_user_model()


class Audit1112PosTests(CheckoutBase):
    def _cashier(self):
        u = User.objects.create_user(email=f"c{uuid.uuid4().hex[:6]}@t.kg", password="x", role="cashier", company=self.company)
        api = APIClient()
        api.force_authenticate(u)
        return u, api

    def test_overflow_is_400_not_500(self):
        self.open_shift()
        p = self.mk(1)[0]
        r = self.co([{"product": str(p.id), "qty": "999999", "price": "99999999.99"}])
        self.assertEqual(r.status_code, 400, getattr(r, "data", r.content))
        r = self.co([{"product": str(p.id), "qty": "2000000", "price": "1.00"}])
        self.assertEqual(r.status_code, 400, getattr(r, "data", r.content))

    def test_cashier_cannot_delete_or_cancel_paid_sale(self):
        self.open_shift()
        p = self.mk(1)[0]
        r = self.co([{"product": str(p.id), "qty": "1", "price": "10.00"}])
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        sid = r.data["id"]
        _, capi = self._cashier()
        self.assertEqual(capi.delete(f"/api/main/pos/sales/{sid}/").status_code, 403)
        self.assertEqual(capi.patch(f"/api/main/pos/sales/{sid}/", {"status": "canceled"}, format="json").status_code, 403)
        self.assertEqual(capi.post("/api/main/sales/bulk-delete/", {"ids": [sid], "allow_paid": True}, format="json").status_code, 403)
        self.assertTrue(Sale.objects.filter(pk=sid, status=Sale.Status.PAID).exists())
        self.assertIn(self.api.delete(f"/api/main/pos/sales/{sid}/").status_code, (200, 204))

    def test_cart_checkout_into_closed_shift_rebinds_or_refuses(self):
        shift = self.open_shift()
        r = self.api.post("/api/main/pos/sales/start/", {}, format="json")
        self.assertIn(r.status_code, (200, 201), getattr(r, "data", None))
        cart_id = r.data.get("id") or r.data.get("active_sale_id")
        r = self.api.post(f"/api/main/pos/sales/{cart_id}/add-item/", {"product_id": str(self.product.id), "quantity": "1"}, format="json")
        self.assertIn(r.status_code, (200, 201), getattr(r, "data", None))
        CashShift.objects.filter(pk=shift.pk).update(status=CashShift.Status.CLOSED)
        r = self.api.post(f"/api/main/pos/sales/{cart_id}/checkout/", {"payment_method": "cash", "cash_received": "1000"}, format="json")
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertTrue("shift_closed" in str(r.data) or "смен" in str(r.data))
        new_shift = self.open_shift()
        r = self.api.post(f"/api/main/pos/sales/{cart_id}/checkout/", {"payment_method": "cash", "cash_received": "1000"}, format="json")
        self.assertIn(r.status_code, (200, 201), getattr(r, "data", None))
        sale = Sale.objects.get(pk=r.data.get("sale_id") or r.data.get("id"))
        self.assertEqual(sale.shift_id, new_shift.id)
