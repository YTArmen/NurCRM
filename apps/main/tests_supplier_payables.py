import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.main.models import Client, ClientDeal
from apps.users.models import Company

User = get_user_model()


class SupplierPayablesTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = User.objects.create_user(email=f"p-{uuid.uuid4().hex}@test.com", password="x")
        self.company = Company.objects.create(name="Payables", owner=self.user)
        self.user.company = self.company
        self.user.save()
        self.api.force_authenticate(self.user)
        self.sup = Client.objects.create(
            company=self.company, full_name="ОсОО Поставщик", type=Client.StatusClient.SUPPLIERS
        )
        self.cli = Client.objects.create(company=self.company, full_name="Покупатель")

    def debt(self, client, amount, due):
        return ClientDeal.objects.create(
            company=self.company, client=client, title="d", kind=ClientDeal.Kind.DEBT,
            amount=Decimal(amount), debt_months=1, first_due_date=due,
        )

    def test_payables(self):
        today = date.today()
        self.debt(self.sup, "1000.00", today - timedelta(days=10))
        self.debt(self.sup, "500.00", today + timedelta(days=5))
        self.debt(self.cli, "999.00", today - timedelta(days=3))  # покупатель — не считается
        r = self.api.get("/main/suppliers/payables/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["total"], "1500.00")
        self.assertEqual(r.data["overdue_total"], "1000.00")
        self.assertEqual(len(r.data["items"]), 1)
        it = r.data["items"][0]
        self.assertEqual(it["supplier_id"], str(self.sup.id))
        self.assertEqual(it["overdue"], "1000.00")
        self.assertEqual(it["oldest_due_date"], (today - timedelta(days=10)).isoformat())
        self.assertEqual(it["next_due_date"], (today + timedelta(days=5)).isoformat())

    def test_empty(self):
        r = self.api.get("/main/suppliers/payables/")
        self.assertEqual(r.data, {"total": "0.00", "overdue_total": "0.00", "items": []})
