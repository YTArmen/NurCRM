from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import AuthSession, User


class Slots2011Tests(TestCase):
    URL = "/api/users/auth/login/"

    def setUp(self):
        self.owner = User.objects.create_user(email="o2011@x.kg", password="pass12345!", role="owner")
        self.cashier = User.objects.create_user(email="c2011@x.kg", password="pass12345!", role=None)

    def login(self, email, **extra):
        c = APIClient()
        return c.post(self.URL, {"email": email, "password": "pass12345!", **extra}, format="json")

    def test_pos_and_owner_slots_independent(self):
        r1 = self.login("c2011@x.kg", client_type="desktop_pos", device_key="pc-one-key")
        r2 = self.login("c2011@x.kg", client_type="desktop_owner", device_key="pc-one-key")
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(AuthSession.objects.filter(user=self.cashier, revoked_at__isnull=True).count(), 2)

    def test_plain_login_takes_no_slot(self):
        self.login("c2011@x.kg", client_type="desktop_pos", device_key="pc-one-key")
        r = self.login("c2011@x.kg")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("device_session_id", r.data)
        self.assertEqual(AuthSession.objects.filter(user=self.cashier, revoked_at__isnull=True).count(), 1)

    def test_same_key_passes_other_key_409_for_cashier(self):
        self.login("c2011@x.kg", client_type="desktop_pos", device_key="pc-one-key")
        self.assertEqual(self.login("c2011@x.kg", client_type="desktop_pos", device_key="pc-one-key").status_code, 200)
        self.assertEqual(self.login("c2011@x.kg", client_type="desktop_pos", device_key="pc-two-key").status_code, 409)

    def test_owner_two_pcs_allowed(self):
        a = self.login("o2011@x.kg", client_type="desktop_pos", device_key="pc-one-key")
        b = self.login("o2011@x.kg", client_type="desktop_pos", device_key="pc-two-key")
        self.assertEqual((a.status_code, b.status_code), (200, 200))
        self.assertEqual(AuthSession.objects.filter(user=self.owner, revoked_at__isnull=True).count(), 2)

    def test_devices_has_client_type(self):
        r = self.login("c2011@x.kg", client_type="desktop_owner", device_key="pc-one-key")
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Bearer " + r.data["access"])
        d = c.get("/api/users/auth/devices/")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.data["results"][0]["client_type"], "desktop_owner")
