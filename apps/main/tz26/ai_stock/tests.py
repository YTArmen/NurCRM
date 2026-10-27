# Тесты куска ai_stock: python manage.py test apps.main.tz26.ai_stock --settings=core.settings_test_sqlite
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.main.models import Product
from apps.main.tz26.ai_stock.models import AiAction
from apps.main.tz26.ai_stock.services import purge_old
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        plan = SubscriptionPlan.objects.create(name="Стандарт", price=Decimal("1.00"))
        self.company = Company.objects.create(name="Co", owner=self.owner, is_active=True, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.cashier = self._staff("cashier")
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def _staff(self, role="cashier", **flags):
        u = User.objects.create_user(email=f"s{uuid.uuid4().hex[:6]}@t.kg", password="x", role=role, **flags)
        u.company = self.company
        u.save()
        return u

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c


class AiActionTests(Base):
    URL = "/api/main/ai-actions/"

    def post(self, api, key, **body):
        payload = {"device": "phone-1", "client": "mobile", "tool": "create_expense", "args": {"a": 1},
                   "result_status": "ok", "say": "Готово", "object_type": "expense", "object_id": "e1",
                   "reversible": True, "model": "x"}
        payload.update(body)
        return api.post(self.URL, payload, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_repeat_gives_one_record(self):
        c = self.client_for(self.cashier)
        r1 = self.post(c, "call-1")
        r2 = self.post(c, "call-1")
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.data["id"], r2.data["id"])
        self.assertEqual(AiAction.objects.count(), 1)

    def test_key_required(self):
        r = self.api.post(self.URL, {"tool": "x"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "validation_error")

    def test_utterance_text_dropped_by_default_hash_kept(self):
        r = self.post(self.client_for(self.cashier), "c-2", utterance_text="Привет мир")
        self.assertEqual(r.status_code, 201)
        self.assertIsNone(r.data["utterance_text"])
        self.assertEqual(len(r.data["utterance_hash"]), 64)

    def test_utterance_text_stored_when_enabled(self):
        r = self.api.patch(self.URL + "settings/", {"store_ai_utterances": True}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["store_ai_utterances"])
        r = self.post(self.client_for(self.cashier), "c-3", utterance_text="Привет мир")
        self.assertEqual(r.data["utterance_text"], "Привет мир")

    def test_staff_cannot_change_setting(self):
        r = self.client_for(self.cashier).patch(self.URL + "settings/", {"store_ai_utterances": True}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_read_rights_and_filters(self):
        cashier2 = self._staff()
        self.post(self.client_for(self.cashier), "a1", object_type="sale", reversible=False)
        self.post(self.client_for(cashier2), "a2", object_type="expense", reversible=True)
        self.post(self.client_for(self.cashier), "a3", object_type="expense", reversible=True,
                  compensation_of="a1", at=(timezone.now() - timedelta(days=10)).isoformat())
        # владелец видит все
        self.assertEqual(self.api.get(self.URL).data["count"], 3)
        # сотрудник — только свои
        mine = self.client_for(self.cashier).get(self.URL).data
        self.assertEqual(mine["count"], 2)
        # чужой user=… сотрудник не может подсмотреть
        self.assertEqual(self.client_for(self.cashier).get(self.URL, {"user": str(cashier2.id)}).data["count"], 2)
        self.assertEqual(self.api.get(self.URL, {"user": str(cashier2.id)}).data["count"], 1)
        self.assertEqual(self.api.get(self.URL, {"object_type": "expense"}).data["count"], 2)
        self.assertEqual(self.api.get(self.URL, {"reversible": "true"}).data["count"], 2)
        self.assertEqual(self.api.get(self.URL, {"reversible": "false"}).data["count"], 1)
        d_from = (timezone.now() - timedelta(days=2)).date().isoformat()
        self.assertEqual(self.api.get(self.URL, {"date_from": d_from}).data["count"], 2)
        d_to = (timezone.now() - timedelta(days=5)).date().isoformat()
        self.assertEqual(self.api.get(self.URL, {"date_to": d_to}).data["count"], 1)
        row = [x for x in self.api.get(self.URL, {"object_type": "expense"}).data["results"] if x["callId"] == "a3"][0]
        self.assertEqual(row["compensation_of"], "a1")

    def test_other_company_isolated(self):
        self.post(self.client_for(self.cashier), "z1")
        o2 = User.objects.create_user(email="o2@t.kg", password="x", role="owner")
        co2 = Company.objects.create(name="Co2", owner=o2, is_active=True, subscription_plan=self.company.subscription_plan)
        o2.company = co2
        o2.save()
        self.assertEqual(self.client_for(o2).get(self.URL).data["count"], 0)
        # тот же callId в другой компании — своя запись
        self.assertEqual(self.post(self.client_for(o2), "z1").status_code, 201)

    def test_purge_180_days(self):
        old = timezone.now() - timedelta(days=181)
        self.post(self.client_for(self.cashier), "old", at=old.isoformat())
        self.post(self.client_for(self.cashier), "new")
        self.assertEqual(purge_old(), 1)
        self.assertEqual(list(AiAction.objects.values_list("call_id", flat=True)), ["new"])
