import json
import os
import tempfile
from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.users.models import Company, SubscriptionPlan, User


class ClientVersions2011Tests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="cv2011@x.kg", password="pass12345!", role="owner")
        plan = SubscriptionPlan.objects.create(name="Стандарт", code="standard", price=Decimal("3600"))
        self.company = Company.objects.create(name="CV Ко", owner=self.owner, is_active=True, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.ov = override_settings(CLIENT_VERSIONS_FILE=self.path)
        self.ov.enable()

    def tearDown(self):
        self.ov.disable()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def write(self, data):
        with open(self.path, "w") as fh:
            json.dump(data, fh)
        os.utime(self.path, (1, os.path.getmtime(self.path) + 1))

    def client_for(self, hdr):
        tok = APIClient().post("/api/users/auth/login/", {"email": "cv2011@x.kg", "password": "pass12345!"},
                               format="json", HTTP_X_NURMARKET_CLIENT="mobile/9.0.0").data
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Bearer " + tok["access"], HTTP_X_NURMARKET_CLIENT=hdr)
        return c

    def test_default_null_no_block(self):
        r = APIClient().get("/api/public/client-versions/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["kassa"], {"min": None, "latest": None})
        self.assertEqual(set(r.data), {"kassa", "android_pos", "owner", "mobile"})
        self.assertEqual(self.client_for("kassa/0.0.1").get("/api/users/auth/devices/").status_code, 200)

    def test_426_only_below_min(self):
        self.write({"kassa": {"min": "1.17.80", "latest": "1.17.90"}, "android_pos": {"min": "2.0"}})
        self.assertEqual(APIClient().get("/api/public/client-versions/").data["kassa"], {"min": "1.17.80", "latest": "1.17.90"})
        old = self.client_for("kassa/1.17.79")
        r = old.get("/api/users/auth/devices/")
        self.assertEqual(r.status_code, 426, r.data)
        self.assertEqual((r.data["code"], r.data["client"], r.data["min"], r.data["latest"]),
                         ("client_outdated", "kassa", "1.17.80", "1.17.90"))
        self.assertEqual(old.get("/api/main/capabilities/").status_code, 200)  # причину видно
        self.assertEqual(self.client_for("kassa/1.17.80").get("/api/users/auth/devices/").status_code, 200)
        self.assertEqual(self.client_for("owner/0.1").get("/api/users/auth/devices/").status_code, 200)  # owner min null
        self.assertEqual(self.client_for("android-pos/1.9.9").get("/api/users/auth/devices/").status_code, 426)
        self.assertEqual(APIClient().get("/api/users/auth/devices/", HTTP_X_NURMARKET_CLIENT="kassa/1.0").status_code, 401)
