"""06.11.2026: продление входа (rotate refresh, предел 90 дней, коды 401)."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.auth_session import AUTH_TIME_CLAIM

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class AuthSessionRefreshTests(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()
        self.user = get_user_model().objects.create_user(email="kassa@shop.kg", password="Pass12345!")

    def login(self):
        r = self.c.post("/api/users/auth/login/", {"email": "kassa@shop.kg", "password": "Pass12345!"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def refresh(self, token):
        return self.c.post("/api/users/auth/refresh/", {"refresh": token}, format="json")

    def test_login_stamps_auth_time(self):
        data = self.login()
        tok = RefreshToken(data["refresh"])
        self.assertIn(AUTH_TIME_CLAIM, tok.payload)

    def test_refresh_returns_new_refresh_and_keeps_auth_time(self):
        data = self.login()
        r = self.refresh(data["refresh"])
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)
        self.assertNotEqual(body["refresh"], data["refresh"])
        old, new = RefreshToken(data["refresh"]), RefreshToken(body["refresh"])
        self.assertEqual(old[AUTH_TIME_CLAIM], new[AUTH_TIME_CLAIM])
        # новый refresh живёт ~30 дней
        self.assertGreater(new["exp"] - new["iat"], 29 * 86400)

    def test_old_refresh_still_works_after_rotation(self):
        """Льготное окно: потерянный ответ / два запроса сразу не выбрасывают."""
        data = self.login()
        self.assertEqual(self.refresh(data["refresh"]).status_code, 200)
        self.assertEqual(self.refresh(data["refresh"]).status_code, 200)

    def test_max_age_90_days(self):
        data = self.login()
        later = timezone.now() + timedelta(days=91)
        with mock.patch("apps.users.auth_session.timezone.now", return_value=later):
            r = self.refresh(data["refresh"])
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "refresh_expired")
        self.assertEqual(r.json()["reason"], "max_age")

    def test_new_refresh_capped_by_max_age(self):
        data = self.login()
        later = timezone.now() + timedelta(days=80)
        with mock.patch("apps.users.auth_session.timezone.now", return_value=later):
            body = self.refresh(data["refresh"]).json()
        payload = RefreshToken(body["refresh"], verify=False).payload
        self.assertLessEqual(payload["exp"], payload[AUTH_TIME_CLAIM] + 90 * 86400 + 1)

    def test_old_token_without_auth_time_uses_iat(self):
        tok = RefreshToken.for_user(self.user)
        r = self.refresh(str(tok))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RefreshToken(r.json()["refresh"])[AUTH_TIME_CLAIM], tok["iat"])

    def test_disabled_user(self):
        data = self.login()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        r = self.refresh(data["refresh"])
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "user_disabled")

    def test_deleted_user_is_401_not_503(self):
        data = self.login()
        self.user.delete()
        r = self.refresh(data["refresh"])
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "user_disabled")

    def test_invalid_token(self):
        r = self.refresh("abc.def.ghi")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "token_invalid")

    def test_expired_token(self):
        data = self.login()
        later = timezone.now() + timedelta(days=31)
        with mock.patch("rest_framework_simplejwt.tokens.aware_utcnow", return_value=later), \
                mock.patch("apps.users.auth_session.timezone.now", return_value=later):
            r = self.refresh(data["refresh"])
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "refresh_expired")


@override_settings(CACHES=LOCMEM)
class DeviceSlotsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()
        self.user = get_user_model().objects.create_user(email="slot@shop.kg", password="Pass12345!")

    def login(self, key, name="Касса 1", client_type="desktop_pos", header=None, **extra):
        body = {"email": "slot@shop.kg", "password": "Pass12345!", "device_key": key, "device_name": name}
        if client_type:
            body["client_type"] = client_type
        body.update(extra)
        kw = {"HTTP_X_NURMARKET_CLIENT": header} if header else {}
        return self.c.post("/api/users/auth/login/", body, format="json", **kw)

    def test_old_client_without_device_key_not_limited(self):
        r1 = self.c.post("/api/users/auth/login/", {"email": "slot@shop.kg", "password": "Pass12345!"}, format="json",
                         HTTP_X_NURMARKET_CLIENT="kassa/1.17.77")
        r2 = self.c.post("/api/users/auth/login/", {"email": "slot@shop.kg", "password": "Pass12345!"}, format="json",
                         HTTP_X_NURMARKET_CLIENT="kassa/1.17.77")
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))

    def test_second_device_same_type_gets_409(self):
        self.assertEqual(self.login("pc-aaaaaaaa").status_code, 200)
        r = self.login("pc-bbbbbbbb", name="Касса 2")
        self.assertEqual(r.status_code, 409, r.content)
        body = r.json()
        self.assertEqual(body["code"], "device_slot_taken")
        self.assertEqual(body["slot"], "desktop_pos")
        self.assertEqual(body["device"]["name"], "Касса 1")

    def test_different_types_ok(self):
        self.assertEqual(self.login("pc-aaaaaaaa").status_code, 200)
        self.assertEqual(self.login("ph-aaaaaaaa", client_type="mobile").status_code, 200)
        self.assertEqual(self.login("ow-aaaaaaaa", client_type="desktop_owner").status_code, 200)
        self.assertEqual(self.login("an-aaaaaaaa", client_type=None, header="android-pos/1.17.78").status_code, 200)

    def test_web_not_limited(self):
        self.assertEqual(self.login("web-aaaaaaa", client_type="web").status_code, 200)
        self.assertEqual(self.login("web-bbbbbbb", client_type="web").status_code, 200)

    def test_same_device_relogin_ok(self):
        self.assertEqual(self.login("pc-aaaaaaaa").status_code, 200)
        self.assertEqual(self.login("pc-aaaaaaaa").status_code, 200)

    def test_replace_revokes_old(self):
        old = self.login("pc-aaaaaaaa").json()
        r = self.login("pc-bbbbbbbb", name="Касса 2", replace_device=True)
        self.assertEqual(r.status_code, 200, r.content)
        # старый refresh -> 401 session_revoked
        rr = self.c.post("/api/users/auth/refresh/", {"refresh": old["refresh"]}, format="json")
        self.assertEqual(rr.status_code, 401)
        self.assertEqual(rr.json()["code"], "session_revoked")
        self.assertEqual(rr.json()["reason"], "device_replaced")
        self.assertEqual(rr.json()["by_device_name"], "Касса 2")
        # старый access -> тоже 401 session_revoked сразу
        ra = self.c.get("/api/users/auth/devices/", HTTP_AUTHORIZATION=f"Bearer {old['access']}")
        self.assertEqual(ra.status_code, 401)
        self.assertEqual(ra.json()["code"], "session_revoked")
        # новый работает
        new = r.json()
        self.assertEqual(self.c.post("/api/users/auth/refresh/", {"refresh": new["refresh"]}, format="json").status_code, 200)

    def test_replace_device_id(self):
        self.login("pc-aaaaaaaa")
        dev_id = self.login("pc-bbbbbbbb").json()["device"]["id"]
        self.assertEqual(self.login("pc-bbbbbbbb", replace_device_id=dev_id).status_code, 200)

    def test_devices_list_and_delete(self):
        pc = self.login("pc-aaaaaaaa").json()
        ph = self.login("ph-aaaaaaaa", name="Телефон", client_type="mobile").json()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {ph['access']}"}
        r = self.c.get("/api/users/auth/devices/", **auth)
        self.assertEqual(r.status_code, 200)
        res = {d["slot"]: d for d in r.json()["results"]}
        self.assertTrue(res["mobile"]["current"])
        self.assertFalse(res["desktop_pos"]["current"])
        d = self.c.delete(f"/api/users/auth/devices/{res['desktop_pos']['id']}/", **auth)
        self.assertEqual(d.status_code, 204)
        rr = self.c.post("/api/users/auth/refresh/", {"refresh": pc["refresh"]}, format="json")
        self.assertEqual(rr.json()["code"], "session_revoked")
        self.assertEqual(rr.json()["reason"], "logged_out_remotely")
        # после выхода слот свободен
        self.assertEqual(self.login("pc-cccccccc").status_code, 200)

    def test_multi_pos_flag(self):
        from apps.users.models import Company, CompanyAddon

        company = Company.objects.create(name="Shop", owner=self.user)
        self.user.company = company
        self.user.save(update_fields=["company"])
        CompanyAddon.objects.create(company=company, code="allow_multi_desktop_pos")
        self.assertEqual(self.login("pc-aaaaaaaa").status_code, 200)
        self.assertEqual(self.login("pc-bbbbbbbb").status_code, 200)
        # телефон всё равно один
        self.assertEqual(self.login("ph-aaaaaaaa", client_type="mobile").status_code, 200)
        self.assertEqual(self.login("ph-bbbbbbbb", client_type="mobile").status_code, 409)

    def test_refresh_keeps_sid(self):
        data = self.login("pc-aaaaaaaa").json()
        r = self.c.post("/api/users/auth/refresh/", {"refresh": data["refresh"]}, format="json").json()
        self.assertEqual(RefreshToken(r["refresh"])["sid"], RefreshToken(data["refresh"])["sid"])

    def test_capabilities_has_flags(self):
        data = self.login("pc-aaaaaaaa").json()
        r = self.c.get("/api/main/capabilities/", HTTP_AUTHORIZATION=f"Bearer {data['access']}")
        self.assertIn("device_slots", r.json()["features"])
        self.assertIn("refresh_rotation", r.json()["features"])
