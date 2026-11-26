"""Б-02/Б-36: старый клиент (без заголовка) получает прежний 400 key_used, новый — 409/429."""
from django.test import TestCase

from apps.users.models import FeatureActivationKey
from apps.users.tests_tz_n32_features import mk

URL = "/api/users/company/features/activate/"


class OldClientKeyTests(TestCase):
    def setUp(self):
        self.c1, self.a1, _ = mk("C1")
        self.c2, self.a2, _ = mk("C2")
        for a in (self.a1, self.a2):
            a.defaults.pop("HTTP_X_NURMARKET_CLIENT", None)
        self.key = FeatureActivationKey.objects.create(code="ai", days=30)

    def test_foreign_key_400_key_used(self):
        self.assertEqual(self.a1.post(URL, {"key": self.key.key}, format="json").status_code, 200)
        r = self.a2.post(URL, {"key": self.key.key}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "key_used"))
        r = self.a2.post(URL, {"key": self.key.key}, format="json", HTTP_X_NURMARKET_CLIENT="kassa/1.17.61")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "key_already_used"))

    def test_brute_force_old_gets_400_new_gets_429(self):
        for i in range(10):
            self.a1.post(URL, {"key": f"BAD-{i}"}, format="json")
        r = self.a1.post(URL, {"key": "BAD-11"}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "key_used"))
        r = self.a1.post(URL, {"key": "BAD-12"}, format="json", HTTP_X_NURMARKET_CLIENT="kassa/1.17.61")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "too_many_attempts"))
