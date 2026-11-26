"""ТЗ Н-08 (23.10.2026): ключ повтора у денежных/складских операций и GET idempotency/{key}/."""
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main.models import Product, Sale
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()


class IdempotencyLookupTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="Co", owner=self.owner, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.api.force_authenticate(user=self.owner)
        self.cashbox = Cashbox.objects.create(company=self.company, name="Касса")

    def create_manual(self, key, name="Кола"):
        return self.api.post(
            "/api/main/products/create-manual/",
            {"name": name, "price": "10.00", "purchase_price": "7.00", "quantity": "1"},
            format="json", HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_create_manual_replay_and_lookup(self):
        key = f"k-{uuid.uuid4()}"
        r1 = self.create_manual(key)
        self.assertIn(r1.status_code, (200, 201), r1.data)
        r2 = self.create_manual(key)
        self.assertEqual(r2.status_code, r1.status_code)
        self.assertEqual(Product.objects.filter(company=self.company, name="Кола").count(), 1)
        # Тот же ключ, другое тело — конфликт, второго товара нет.
        r3 = self.create_manual(key, name="Фанта")
        self.assertEqual(r3.status_code, 409)
        self.assertFalse(Product.objects.filter(company=self.company, name="Фанта").exists())

        look = self.api.get(f"/api/main/idempotency/{key}/")
        self.assertEqual(look.status_code, 200, look.data)
        self.assertTrue(look.data["found"])
        self.assertEqual(look.data["state"], "done")
        self.assertIn("create-manual", look.data["endpoint"])

    def test_lookup_sale_key_and_missing(self):
        Sale.objects.create(company=self.company, cashbox=self.cashbox, user=self.owner, status=Sale.Status.PAID,
                            total=Decimal("5.00"), idempotency_key="sale-key-1")
        look = self.api.get("/api/main/idempotency/sale-key-1/")
        self.assertEqual(look.status_code, 200, look.data)
        self.assertEqual(look.data["response"]["total"], "5.00")
        miss = self.api.get("/api/main/idempotency/nope-123/")
        self.assertEqual(miss.status_code, 404)
        self.assertEqual(miss.data["code"], "idempotency_key_not_found")

    def test_other_company_does_not_see_key(self):
        Sale.objects.create(company=self.company, cashbox=self.cashbox, user=self.owner, status=Sale.Status.PAID,
                            total=Decimal("5.00"), idempotency_key="sale-key-2")
        other = User.objects.create_user(email=f"x_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="P2", price=Decimal("1.00"))
        other.company = Company.objects.create(name="Other", owner=other, subscription_plan=plan)
        other.save()
        api = APIClient()
        api.force_authenticate(user=other)
        self.assertEqual(api.get("/api/main/idempotency/sale-key-2/").status_code, 404)
