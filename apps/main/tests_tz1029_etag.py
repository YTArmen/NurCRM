"""ТЗ 29.10 Б-46 п.1: ETag и 304 на справочных GET."""
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from apps.users.models import Company, User
from core.http_cache import etag_matches


class MatchTests(SimpleTestCase):
    def test_variants(self):
        e = '"abc"'
        self.assertTrue(etag_matches('"abc"', e))
        self.assertTrue(etag_matches('W/"abc"', e))
        self.assertTrue(etag_matches('"x", W/"abc"', e))
        self.assertTrue(etag_matches('*', e))
        self.assertTrue(etag_matches('"abc"', 'W/"abc"'))
        self.assertFalse(etag_matches('"zzz"', e))
        self.assertFalse(etag_matches('', e))


class EtagApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="o@e.kg", password="x", role="owner")
        self.company = Company.objects.create(name="E", owner=self.user)
        self.user.company = self.company
        self.user.save()
        self.c = APIClient()
        self.c.force_authenticate(self.user)

    def _check(self, url):
        r = self.c.get(url)
        self.assertEqual(r.status_code, 200, url)
        et = r["ETag"]
        self.assertTrue(et)
        for hdr in (et, "W/" + et.removeprefix("W/"), '"zz", ' + et, "*"):
            r2 = self.c.get(url, HTTP_IF_NONE_MATCH=hdr)
            self.assertEqual(r2.status_code, 304, (url, hdr))
            self.assertEqual(r2.content, b"")
        r3 = self.c.get(url, HTTP_IF_NONE_MATCH='"other"')
        self.assertEqual(r3.status_code, 200)
        return et

    def test_six_urls(self):
        for url in ("/api/main/products/catalog-version/", "/api/main/products/changes/?since=1700000000000",
                    "/api/main/app-settings/nurmarket-kassa/", "/api/users/company/",
                    "/api/users/employees/", "/api/users/roles/"):
            self._check(url)

    def test_etag_changes_with_data(self):
        a = self._check("/api/users/roles/")
        from apps.users.models import CustomRole
        CustomRole.objects.create(company=self.company, name="Кассир2")
        r = self.c.get("/api/users/roles/", HTTP_IF_NONE_MATCH=a)
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r["ETag"], a)

    def test_post_untouched(self):
        r = self.c.patch("/api/users/company/", {"name": "N"}, format="json", HTTP_IF_NONE_MATCH="*")
        self.assertNotEqual(r.status_code, 304)

    def test_304_not_counted_in_throttle(self):
        from rest_framework.throttling import UserRateThrottle
        url = "/api/users/roles/"
        et = self.c.get(url)["ETag"]
        old = UserRateThrottle.THROTTLE_RATES["user"]
        with override_settings():
            UserRateThrottle.THROTTLE_RATES = dict(UserRateThrottle.THROTTLE_RATES, user="3/minute")
            try:
                cache.clear()
                for _ in range(10):
                    self.assertEqual(self.c.get(url, HTTP_IF_NONE_MATCH=et).status_code, 304)
                self.assertEqual(self.c.get(url).status_code, 200)
            finally:
                UserRateThrottle.THROTTLE_RATES = dict(UserRateThrottle.THROTTLE_RATES, user=old)
