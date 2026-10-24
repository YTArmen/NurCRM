"""ТЗ Н-17: версии по ключам, _expected, лимиты, secret_*."""
import uuid

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import Company, User

URL = "/main/app-settings/nurmarket-kassa/"


def _mk(company, role):
    return User.objects.create_user(
        email=f"{role}_{uuid.uuid4().hex[:8]}@t.kg", password="p12345678", company=company, role=role
    )


class N17Tests(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:8]}@t.kg", password="p12345678", role="owner")
        self.company = Company.objects.create(name="N17", owner=owner)
        owner.company = self.company
        owner.save()
        self.owner = owner
        self.cashier = _mk(self.company, "cashier")
        self.c = APIClient()
        self.c.force_authenticate(owner)

    def test_key_versions_and_expected(self):
        r = self.c.patch(URL, {"a": 1, "b": 2}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["key_versions"], {"a": 1, "b": 1})
        self.assertNotIn("_meta", r.data["settings"])
        r = self.c.patch(URL, {"a": 5, "_expected": {"a": 1}}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["key_versions"]["a"], 2)
        self.assertNotIn("_expected", r.data["settings"])
        r = self.c.patch(URL, {"a": 9, "_expected": {"a": 1}}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "version_conflict")
        self.assertEqual(r.data["conflict_keys"]["a"], {"value": 5, "version": 2})
        self.assertEqual(self.c.get(URL).data["settings"]["a"], 5)

    def test_if_match_still_412(self):
        v = self.c.patch(URL, {"a": 1}, format="json").data["version"]
        r = self.c.patch(URL, {"a": 2}, format="json", HTTP_IF_MATCH=f'W/"{v - 1}"')
        self.assertEqual(r.status_code, 412)

    def test_meta_not_writable(self):
        r = self.c.patch(URL, {"_meta": {"keys": {}}}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_empty_string_keeps_key_null_deletes(self):
        self.c.patch(URL, {"a": "x"}, format="json")
        r = self.c.patch(URL, {"a": ""}, format="json")
        self.assertEqual(r.data["settings"]["a"], "")
        r = self.c.patch(URL, {"a": None}, format="json")
        self.assertNotIn("a", r.data["settings"])

    def test_too_large(self):
        r = self.c.patch(URL, {"big": "x" * (65 * 1024)}, format="json")
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.data["code"], "too_large")
        for i in range(5):
            self.c.patch(URL, {f"k{i}": "y" * (60 * 1024)}, format="json")
        r = self.c.patch(URL, {"k9": "y" * (60 * 1024)}, format="json")
        self.assertEqual(r.status_code, 413)

    def test_secret_hidden_and_write_denied(self):
        self.c.patch(URL, {"secret_token": "abc", "pub": 1}, format="json")
        cc = APIClient()
        cc.force_authenticate(self.cashier)
        g = cc.get(URL)
        self.assertEqual(g.status_code, 200)
        self.assertNotIn("secret_token", g.data["settings"])
        self.assertNotIn("secret_token", g.data["key_versions"])
        self.assertEqual(g.data["settings"]["pub"], 1)
        self.assertEqual(cc.patch(URL, {"x": 1}, format="json").status_code, 403)
        self.assertEqual(self.c.get(URL).data["settings"]["secret_token"], "abc")
