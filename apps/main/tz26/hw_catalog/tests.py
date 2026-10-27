# Тесты куска hw_catalog: python manage.py test apps.main.tz26.hw_catalog --settings=core.settings_test_sqlite
from django.test import TestCase
from rest_framework.test import APIClient


class SphereProfilesTests(TestCase):
    def test_hardware_profile_public(self):
        r = APIClient().get("/api/public/sphere-profiles/")
        self.assertEqual(r.status_code, 200)
        prof = {p["id"]: p for p in r.json()}["hardware"]
        self.assertEqual(prof["base"], "grocery")
        for lang in ("ru", "ky", "en", "tr", "uz"):
            self.assertTrue(prof["title"][lang])
            self.assertEqual(len(prof["category_hints"]), 15)
            self.assertTrue(all(h[lang] for h in prof["category_hints"]))
        self.assertEqual(prof["fractional_units"]["m2"], {"step": "0.01", "min": "0.5"})
        self.assertTrue(prof["features"]["storage_locations"])
