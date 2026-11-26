# python manage.py test apps.inbox --settings=core.settings_test_sqlite
"""Общая основа тестов ящика."""
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import Company, SubscriptionPlan

User = get_user_model()
BASE = "/api/main/inbox"


class InboxBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.plan = SubscriptionPlan.objects.create(name="Стандарт", price=Decimal("1.00"))
        self.company = Company.objects.create(
            name="Inbox Co", owner=self.owner, is_active=True, subscription_plan=self.plan,
            can_view_telegram=True, can_view_whatsapp=True,
        )
        self.owner.company = self.company
        self.owner.save()
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def mk_user(self, role="cashier", **flags):
        u = User.objects.create_user(email=f"u{uuid.uuid4().hex[:6]}@t.kg", password="x", role=role, company=self.company)
        for k, v in flags.items():
            setattr(u, k, v)
        if flags:
            u.save()
        return u

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c
