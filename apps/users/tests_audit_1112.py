"""Аудит 12.11.2026: проверки закрытых дыр (компании/подписка, API-ключ, пароль, Bid, Telegram, вход)."""
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.users.models import Company

User = get_user_model()


def _company(name="C"):
    owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:8]}@t.kg", password="password123", role="owner")
    now = timezone.now()
    c = Company.objects.create(name=f"{name} {uuid.uuid4().hex[:5]}", owner=owner,
                               start_date=now - timedelta(days=1), end_date=now + timedelta(days=10))
    owner.company = c
    owner.save()
    return c, owner


class AuditCompaniesTests(APITestCase):
    def setUp(self):
        self.c1, self.owner1 = _company("A")
        self.c2, self.owner2 = _company("B")
        self.admin1 = User.objects.create_user(email=f"a_{uuid.uuid4().hex[:8]}@t.kg", password="password123",
                                               role="admin", company=self.c1)

    def test_company_admin_sees_only_own_company(self):
        self.client.force_authenticate(self.admin1)
        r = self.client.get("/api/users/companies/")
        self.assertEqual(r.status_code, 200)
        ids = {row["id"] for row in (r.data.get("results", r.data) if isinstance(r.data, dict) else r.data)}
        self.assertNotIn(str(self.c2.id), ids)

    def test_company_admin_cannot_touch_foreign_subscription(self):
        self.client.force_authenticate(self.admin1)
        r = self.client.patch(f"/api/users/companies/{self.c2.id}/subscription/", {"extend_months": 12}, format="json")
        self.assertIn(r.status_code, (403, 404))

    def test_owner_cannot_extend_own_subscription(self):
        self.client.force_authenticate(self.owner1)
        end = Company.objects.get(pk=self.c1.pk).end_date
        r = self.client.patch(f"/api/users/companies/{self.c1.id}/subscription/", {"extend_months": 1200}, format="json")
        self.assertIn(r.status_code, (403, 404))
        self.assertEqual(Company.objects.get(pk=self.c1.pk).end_date, end)

    def test_profile_password_needs_current(self):
        self.client.force_authenticate(self.owner1)
        r = self.client.patch("/api/users/profile/", {"password": "newpassword123"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.owner1.refresh_from_db()
        self.assertTrue(self.owner1.check_password("password123"))
        r = self.client.patch("/api/users/profile/", {"password": "newpassword123", "current_password": "password123"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.owner1.refresh_from_db()
        self.assertTrue(self.owner1.check_password("newpassword123"))

    def test_bids_hidden_from_shop_users(self):
        self.client.force_authenticate(self.owner1)
        self.assertEqual(self.client.get("/api/main/applications/").status_code, 403)


class AuditApiKeyTests(APITestCase):
    def test_api_key_cannot_reach_users_paths(self):
        from apps.integrations.models import ApiKey, hash_api_key
        c, owner = _company()
        raw = ApiKey.generate()
        ApiKey.objects.create(company=c, name="t", prefix=raw[:12], key_hash=hash_api_key(raw), scopes=["write"], created_by=owner)
        ok = self.client.get("/api/main/products/list/", HTTP_AUTHORIZATION=f"Api-Key {raw}")
        self.assertNotIn(ok.status_code, (401, 403))
        r = self.client.patch("/api/users/profile/", {"first_name": "x"}, format="json", HTTP_AUTHORIZATION=f"Api-Key {raw}")
        self.assertEqual(r.status_code, 403)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "audit1112"}})
class AuditLoginThrottleTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.c, self.owner = _company()

    def test_failed_logins_limited_per_email(self):
        for _ in range(10):
            r = self.client.post("/api/users/auth/login/", {"email": self.owner.email, "password": "wrong"}, format="json")
            self.assertIn(r.status_code, (400, 401))
        r = self.client.post("/api/users/auth/login/", {"email": self.owner.email, "password": "password123"}, format="json")
        self.assertEqual(r.status_code, 429)


class AuditTelegramPhoneTests(APITestCase):
    def test_start_with_phone_asks_contact_and_does_not_link(self):
        from apps.main.models import Client
        from apps.main.telegram_bot.services.customer_handler import (
            CONTACT_REQUEST_TEXT, link_client_by_start, link_client_by_verified_contact,
        )
        c, _ = _company()
        cl = Client.objects.create(company=c, full_name="Покупатель", phone="+996555123456")
        self.assertEqual(link_client_by_start(c, "777", "0555123456"), CONTACT_REQUEST_TEXT)
        cl.refresh_from_db()
        self.assertFalse(cl.telegram_chat_id)
        # чужой контакт — нет
        link_client_by_verified_contact(c, "777", {"id": 1}, {"user_id": 2, "phone_number": "+996555123456"})
        cl.refresh_from_db()
        self.assertFalse(cl.telegram_chat_id)
        # свой контакт — да
        link_client_by_verified_contact(c, "777", {"id": 1}, {"user_id": 1, "phone_number": "+996555123456"})
        cl.refresh_from_db()
        self.assertEqual(cl.telegram_chat_id, "777")
