from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.support import bot

URL = "/api/support/password-reset-requests/"
OK = {"name": "Асанов Улан", "phone": "+996 700 111 222", "login": "ulan@mail.kg", "company": "Premium", "program": "market"}


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}, SUPPORT_TELEGRAM_BOT_TOKEN="t", SUPPORT_TELEGRAM_CHAT_ID="-100", PASSWORD_RESET_TELEGRAM_CHAT_ID="-1004361594959")
class PasswordResetRequestTests(TestCase):
    def setUp(self):
        cache.clear()
        bot._CONFIG_CACHE.update(at=0.0, cfg=None)
        self.api = APIClient()

    def test_sends_to_reset_chat_without_auth(self):
        with mock.patch("apps.support.forgot._send_raw", return_value=True) as send:
            r = self.api.post(URL, OK, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        token, chat, text = send.call_args[0]
        self.assertEqual(chat, "-1004361594959")
        self.assertIn("Асанов Улан", text)
        self.assertIn("Маркет", text)
        self.assertIn("перезвоните", text)

    def test_validation_and_honeypot(self):
        with mock.patch("apps.support.forgot._send_raw", return_value=True) as send:
            r = self.api.post(URL, {"name": "А", "phone": "123"}, format="json")
            self.assertEqual(r.status_code, 400)
            self.assertEqual(set(r.data["fields"]), {"name", "phone"})
            r = self.api.post(URL, {**OK, "website": "spam.kg"}, format="json")
            self.assertEqual(r.status_code, 201)
        send.assert_not_called()

    def test_html_escaped(self):
        with mock.patch("apps.support.forgot._send_raw", return_value=True) as send:
            self.api.post(URL, {**OK, "name": "<b>x</b>"}, format="json")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", send.call_args[0][2])

    def test_ip_limit(self):
        with mock.patch("apps.support.forgot._send_raw", return_value=True):
            codes = [self.api.post(URL, OK, format="json").status_code for _ in range(4)]
        self.assertEqual(codes, [201, 201, 201, 429])

    def test_not_delivered_gives_phone(self):
        with mock.patch("apps.support.forgot._send_raw", return_value=False):
            r = self.api.post(URL, OK, format="json")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data["code"], "not_delivered")
        self.assertIn("559 900 556", r.data["detail"])
