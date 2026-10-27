"""ТЗ Н-28 (23.10.2026): GET pos/sales/ за последней страницей — пустой список, не 500."""
from decimal import Decimal
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main.models import Sale
from apps.users.models import Company, User


class SalesBeyondLastPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.company = Company.objects.create(name="Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        cb = Cashbox.objects.create(company=self.company, name="Касса")
        for _ in range(3):
            Sale.objects.create(company=self.company, cashbox=cb, user=self.owner, status=Sale.Status.PAID,
                                total=Decimal("1.00"), paid_at=timezone.now())
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def test_page_beyond_last_is_empty(self):
        # Как в журнале 23.10: page=3&page_size=500 при одной странице чеков.
        r = self.api.get("/api/main/pos/sales/", {"page": 3, "page_size": 500})
        self.assertEqual(r.status_code, 200, getattr(r, "data", r.content))
        self.assertEqual(r.data["results"], [])
        self.assertIsNone(r.data["next"])
        self.assertIn("page=1", r.data["previous"])

    def test_last_page_still_works(self):
        r = self.api.get("/api/main/pos/sales/", {"page": 2, "page_size": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["results"]), 1)
