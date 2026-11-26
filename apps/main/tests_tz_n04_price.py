"""ТЗ Н-04 (23.10.2026): смена закупочной цены не меняет цену продажи без явного флага."""
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.main.models import Client, Product
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()


class PurchasePriceKeepsSalePriceTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="Co", owner=self.owner, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.api.force_authenticate(user=self.owner)
        self.supplier = Client.objects.create(full_name="Пост", type=Client.StatusClient.SUPPLIERS, company=self.company)
        self.product = Product.objects.create(
            name="Тест", company=self.company, quantity=Decimal("5"),
            purchase_price=Decimal("11.00"), price=Decimal("15.00"), markup_percent=Decimal("36.3636"),
        )

    def patch(self, body):
        res = self.api.patch(f"/api/main/products/{self.product.id}/", body, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        return res

    def receipt(self, line):
        res = self.api.post(
            f"/api/main/suppliers/{self.supplier.id}/receipt/",
            {"items": [{"product": str(self.product.id), "qty": 1, **line}], "payment_type": "debt"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()

    def test_patch_purchase_only_keeps_price(self):
        res = self.patch({"purchase_price": "1.00"})
        self.assertEqual(self.product.price, Decimal("15.00"))
        self.assertIs(res.data["price_changed"], False)
        self.patch({"purchase_price": "11.00"})
        self.assertEqual(self.product.price, Decimal("15.00"))

    def test_receipt_then_patch_keeps_price(self):
        # Сценарий из ТЗ: приход 11 → 1, затем PATCH 11 давал цену 165.
        self.receipt({"purchase_price": "1.00"})
        self.assertEqual(self.product.price, Decimal("15.00"))
        self.patch({"purchase_price": "11.00"})
        self.assertEqual(self.product.price, Decimal("15.00"))
        # Правка другого поля тоже не сдвигает цену.
        self.patch({"name": "Тест 2"})
        self.assertEqual(self.product.price, Decimal("15.00"))

    def test_explicit_recalc(self):
        res = self.patch({"purchase_price": "22.00", "recalc_price": True})
        self.assertEqual(self.product.price, Decimal("30.00"))
        self.assertIs(res.data["price_changed"], True)
        self.receipt({"purchase_price": "11.00", "recalc_price": True})
        self.assertEqual(self.product.price, Decimal("15.00"))
