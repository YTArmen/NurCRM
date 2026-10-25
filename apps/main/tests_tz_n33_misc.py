"""ТЗ Н-33: мелочи (pickup_code, роли касс safe/bank, таблицы размеров, дизайн-ключи, скидка прихода и т.д.)."""
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main.models import Client, Product, ProductVariant, ShowcaseOrder
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "n33"}}


@override_settings(CACHES=LOCMEM)
class N33Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="N33", slug="n33-shop", owner=self.owner, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.pub = APIClient()


class PickupCodeTests(N33Base):
    def test_generated_and_writable(self):
        o = ShowcaseOrder.objects.create(company=self.company, number=1, customer_name="A", customer_phone="1")
        self.assertTrue(o.pickup_code.isdigit() and 4 <= len(o.pickup_code) <= 6)
        r = self.api.get(f"/api/main/showcase/orders/{o.id}/")
        self.assertEqual(r.json()["pickup_code"], o.pickup_code)
        r = self.api.patch(f"/api/main/showcase/orders/{o.id}/", {"pickup_code": "7777"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        o.refresh_from_db()
        self.assertEqual(o.pickup_code, "7777")
        r = self.api.patch(f"/api/main/showcase/orders/{o.id}/", {"pickup_code": "ab"}, format="json")
        self.assertEqual(r.status_code, 400)


class CashboxRoleTests(N33Base):
    def test_safe_bank_roles_not_drawers(self):
        r = self.api.post("/api/construction/cashboxes/", {"name": "Сейф", "role": "safe"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["role"], "safe")
        r = self.api.post("/api/construction/cashboxes/", {"name": "Р/с", "role": "bank"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        for cb in Cashbox.objects.filter(company=self.company, role__in=("safe", "bank")):
            self.assertIsNone(cb.drawer_cash_info())
