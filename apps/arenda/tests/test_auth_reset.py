import re
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.arenda.models import AuditLog, PasswordResetCode
from apps.arenda.services import sender  # noqa: F401
from apps.arenda.defaults import ensure_company_defaults
from apps.users.models import User

from .base import ArendaBase, make_user

GW = dict(ARENDA_SMS_URL="https://sms.example/send", ARENDA_SMS_TOKEN="tok")
REQ = "/api/arenda/auth/reset/request/"
CONF = "/api/arenda/auth/reset/confirm/"


class PhoneLoginTests(ArendaBase):
    def test_owner_logs_in_by_phone_field(self):
        self.owner.phone_number = "0555 123 456"
        self.owner.save()
        r = APIClient().post("/api/arenda/auth/login/", {"login": "+996555123456", "password": "pass12345"},
                             format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("access", r.data)

    def test_ambiguous_phone_does_not_log_in(self):
        self.owner.phone_number = "+996555123456"
        self.owner.save()
        other = make_user("o2@a.kg", self.company, phone_number="0555123456")
        r = APIClient().post("/api/arenda/auth/login/", {"login": "0555123456", "password": "pass12345"},
                             format="json")
        self.assertNotEqual(r.status_code, 200, r.data)
        self.assertTrue(other.pk)


@override_settings(**GW)
class ResetTests(ArendaBase):
    def setUp(self):
        super().setUp()
        self.owner.phone_number = "+996555123456"
        self.owner.save()
        ensure_company_defaults(self.company)
        self.sent = []
        p = mock.patch("apps.arenda.services.sender._post",
                       side_effect=lambda url, token, payload: self.sent.append(payload) or 200)
        p.start()
        self.addCleanup(p.stop)
        self.anon = APIClient()

    def code(self):
        return re.search(r"\b(\d{6})\b", self.sent[-1]["text"]).group(1)

    def test_full_flow_ends_sessions_and_logs(self):
        r = self.anon.post(REQ, {"login": "0555123456"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.sent[0]["phone"], "+996555123456")
        code = self.code()
        self.assertEqual(len(code), 6)
        old = self.anon.post("/api/arenda/auth/login/", {"login": "owner@a.kg", "password": "pass12345"},
                             format="json").data["access"]
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Bearer " + old)
        self.assertEqual(c.get("/api/arenda/me/").status_code, 200)
        r = self.anon.post(CONF, {"login": "owner@a.kg", "code": code, "new_password": "newpass99"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(c.get("/api/arenda/me/").status_code, 401)  # старые сеансы завершены
        ok = self.anon.post("/api/arenda/auth/login/", {"login": "owner@a.kg", "password": "newpass99"}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="password_reset", user=self.owner).exists())
        # код одноразовый
        r = self.anon.post(CONF, {"login": "owner@a.kg", "code": code, "new_password": "another99"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_same_reply_for_unknown_user_and_no_sms_sent(self):
        a = self.anon.post(REQ, {"login": "0555123456"}, format="json")
        n = len(self.sent)
        b = self.anon.post(REQ, {"login": "+996700999999"}, format="json")
        self.assertEqual((a.status_code, a.data), (b.status_code, b.data))
        self.assertEqual(len(self.sent), n)

    def test_once_per_minute(self):
        self.anon.post(REQ, {"login": "0555123456"}, format="json")
        r = self.anon.post(REQ, {"login": "0555123456"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.sent), 1)
        PasswordResetCode.objects.update(created_at=timezone.now() - timedelta(seconds=61))
        self.anon.post(REQ, {"login": "0555123456"}, format="json")
        self.assertEqual(len(self.sent), 2)
        # старый код погашен, новый работает
        r = self.anon.post(CONF, {"login": "0555123456", "code": self.code(), "new_password": "newpass99"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_five_wrong_codes_burn_it(self):
        self.anon.post(REQ, {"login": "0555123456"}, format="json")
        code = self.code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            r = self.anon.post(CONF, {"login": "0555123456", "code": wrong, "new_password": "newpass99"}, format="json")
            self.assertEqual(r.status_code, 400)
        r = self.anon.post(CONF, {"login": "0555123456", "code": code, "new_password": "newpass99"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_expired_code(self):
        self.anon.post(REQ, {"login": "0555123456"}, format="json")
        PasswordResetCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        r = self.anon.post(CONF, {"login": "0555123456", "code": self.code(), "new_password": "newpass99"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_password_rules(self):
        self.anon.post(REQ, {"login": "0555123456"}, format="json")
        code = self.code()
        for bad in ("short1", "longpasswordnodigit"):
            r = self.anon.post(CONF, {"login": "0555123456", "code": code, "new_password": bad}, format="json")
            self.assertEqual(r.status_code, 400)
        r = self.anon.post(CONF, {"login": "0555123456", "code": code, "new_password": "goodpass1"}, format="json")
        self.assertEqual(r.status_code, 200)  # неверный пароль не сжёг верный код

    @override_settings(ARENDA_SMS_URL="")
    def test_sms_unavailable_503(self):
        r = self.anon.post(REQ, {"login": "0555123456"}, format="json")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data["code"], "sms_unavailable")
