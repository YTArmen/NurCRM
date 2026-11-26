from datetime import timedelta

from django.test import SimpleTestCase, TestCase

from apps.support.bot import _parse_duration, esc
from apps.support.rules import DEFAULT_RULES, is_valid_version, sort_versions, version_gte
from apps.support.sanitizer import sanitize_context, sanitize_text


class SanitizerTests(SimpleTestCase):
    def test_bearer_and_jwt(self):
        out = sanitize_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOjF9.abc_DEF-1")
        self.assertNotIn("eyJ", out)
        self.assertNotIn("abc_DEF", out)
        self.assertNotIn("eyJ", sanitize_text("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOjF9.sig"))

    def test_bot_tokens(self):
        for raw in (
            "bot123:AAxyz",
            "https://api.telegram.org/bot1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage",
            "token 1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw end",
        ):
            out = sanitize_text(raw)
            self.assertNotIn("AAHdq", out)
            self.assertNotIn("AAxyz", out)

    def test_google_key(self):
        self.assertNotIn("AIzaSy", sanitize_text("key=AIzaSyA1234567890abcdefghijklmnopqrstuv"))

    def test_json_passwords(self):
        self.assertEqual(sanitize_text('{"password": "hunter2"}'), '{"password": "[REDACTED]"}')
        self.assertNotIn("b", sanitize_text('{"password": "a\\"b"}').split(":")[1].replace("REDACTED", ""))
        self.assertNotIn("s3cr3t", sanitize_text("{'pin': 's3cr3t'}"))
        self.assertNotIn("qwerty", sanitize_text("login?user=a&password=qwerty&x=1"))

    def test_cards_luhn(self):
        self.assertNotIn("4111", sanitize_text("card 4111 1111 1111 1111 ok"))
        self.assertNotIn("4111", sanitize_text("card 4111-1111-1111-1111"))
        # таймстемпы и id не трогаем
        self.assertIn("1727850000000", sanitize_text("ts 1727850000000"))
        self.assertIn("2026100210011234", sanitize_text("order 2026100210011234"))

    def test_plain_text_kept(self):
        msg = "PAY failed: 500 от /api/main/pos/carts/12/checkout/"
        self.assertEqual(sanitize_text(msg), msg)

    def test_context_limits(self):
        lines = [""] + ["x" * 1000] * 100
        out = sanitize_context(lines)
        self.assertLessEqual(len(out), 50)
        self.assertLessEqual(sum(len(x) + 1 for x in out), 16 * 1024)
        self.assertEqual(sanitize_context(["", "Bearer abc.def"]), ["", "Bearer [REDACTED]"])


class VersionTests(SimpleTestCase):
    def test_compare(self):
        self.assertTrue(version_gte("1.17.100", "1.17.38"))
        self.assertTrue(version_gte("1.17.38", "1.17.38"))
        self.assertFalse(version_gte("1.17.9", "1.17.38"))
        self.assertTrue(version_gte("1.18", "1.17.38"))
        self.assertEqual(sort_versions(["1.17.100", "1.17.38", "1.17.9"]), ["1.17.9", "1.17.38", "1.17.100"])
        self.assertTrue(is_valid_version("1.17.38"))
        self.assertFalse(is_valid_version("abc"))


class BotHelpersTests(SimpleTestCase):
    def test_duration(self):
        self.assertEqual(_parse_duration("24h"), timedelta(hours=24))
        self.assertEqual(_parse_duration("7d"), timedelta(days=7))
        self.assertEqual(_parse_duration("30m"), timedelta(minutes=30))
        self.assertIsNone(_parse_duration("abc"))

    def test_escape(self):
        self.assertEqual(esc("<module> & List<T>"), "&lt;module&gt; &amp; List&lt;T&gt;")

    def test_default_rules_cover_spec(self):
        codes = {r["code"] for r in DEFAULT_RULES}
        self.assertEqual(len(codes), len(DEFAULT_RULES))
        self.assertGreaterEqual(len(DEFAULT_RULES), 10)


from django.test import override_settings


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "er1030"}})
class ErrorReportsTolerantTests(TestCase):
    """30.10.2026: плохой отчёт не роняет пачку; длинные поля обрезаются."""

    def rep(self, **kw):
        import uuid
        r = {"client_report_id": str(uuid.uuid4()), "app": "kassa", "version": "1.17.61", "device_id": "dev-1",
             "fingerprint": "f" * 10, "message": "boom", "first_at": "2026-10-30T10:00:00Z",
             "last_at": "2026-10-30T10:01:00Z"}
        r.update(kw)
        return r

    def post(self, reports):
        from rest_framework.test import APIClient
        return APIClient().post("/api/support/error-reports/", {"reports": reports}, format="json")

    def test_bad_item_does_not_reject_batch(self):
        r = self.post([self.rep(), self.rep(first_at="не дата"), self.rep(fingerprint="x" * 200, version="9" * 50)])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["accepted"], r.json()["invalid"]), (2, 1))

    def test_all_bad_400(self):
        self.assertEqual(self.post([self.rep(first_at="нет")]).status_code, 400)
        self.assertEqual(self.post([]).status_code, 400)
