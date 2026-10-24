"""ТЗ Н-23: notify-owner, коды владельца, лимиты отправки, queue-health для владельца."""
import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from apps.main.telegram_bot.models import TelegramBotSettings, TelegramOwnerCode
from apps.main.telegram_bot.services import telegram_api
from apps.main.telegram_bot.tasks import process_telegram_update
from apps.main.tests_kassa_api import KassaBase

TG = "apps.main.telegram_bot.services.telegram_api."


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class N23Base(KassaBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.bot = TelegramBotSettings.objects.create(
            company=self.company, mode=TelegramBotSettings.Mode.SERVER, token="123456:TEST", owner_chat_id="777",
        )
        p = patch(TG + "send_message", return_value={"ok": True, "result": {"message_id": 1}})
        self.send = p.start()
        self.addCleanup(p.stop)


class NotifyOwnerTests(N23Base):
    def test_sends_and_dedupes(self):
        body = {"text": "Отчёт", "dedupe_key": "morning-1", "buttons": [{"text": "Открыть", "url": "https://x.kg"}]}
        r1 = self.api.post("/api/main/bot/notify-owner/", body, format="json")
        self.assertEqual(r1.status_code, 200, r1.data)
        self.assertFalse(r1.data["duplicate"])
        r2 = self.api.post("/api/main/bot/notify-owner/", body, format="json")
        self.assertTrue(r2.data["duplicate"])
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("inline_keyboard", self.send.call_args.kwargs["reply_markup"])

    def test_no_owner_chat(self):
        TelegramBotSettings.objects.filter(id=self.bot.id).update(owner_chat_id=None)
        r = self.api.post("/api/main/bot/notify-owner/", {"text": "x"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_failed_send_not_deduped(self):
        self.send.return_value = {"ok": False, "description": "boom"}
        body = {"text": "a", "dedupe_key": "k"}
        self.assertEqual(self.api.post("/api/main/bot/notify-owner/", body, format="json").status_code, 502)
        self.send.return_value = {"ok": True}
        r = self.api.post("/api/main/bot/notify-owner/", body, format="json")
        self.assertFalse(r.data["duplicate"])


class OwnerCodeTests(N23Base):
    def _issue(self):
        r = self.api.post("/api/main/telegram-bot/owner-code/", {}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["code"]

    def test_issue_stored_hashed_and_verify_once(self):
        code = self._issue()
        self.assertEqual(len(code), 6)
        self.assertNotIn(code, TelegramOwnerCode.objects.get().code_hash)
        r = self.api.post("/api/main/telegram-bot/owner-code/verify/",
                          {"code": code, "chat_id": "999", "from_id": "42"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.bot.refresh_from_db()
        self.assertEqual((self.bot.owner_chat_id, self.bot.owner_user_id), ("999", "42"))
        again = self.api.post("/api/main/telegram-bot/owner-code/verify/", {"code": code, "chat_id": "5"}, format="json")
        self.assertEqual(again.status_code, 400)

    def test_wrong_code_locks(self):
        code = self._issue()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.api.post("/api/main/telegram-bot/owner-code/verify/", {"code": wrong, "chat_id": "9"}, format="json")
        r = self.api.post("/api/main/telegram-bot/owner-code/verify/", {"code": code, "chat_id": "9"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_expired(self):
        code = self._issue()
        TelegramOwnerCode.objects.update(expires_at=timezone.now() - timezone.timedelta(seconds=1))
        r = self.api.post("/api/main/telegram-bot/owner-code/verify/", {"code": code, "chat_id": "9"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_cashier_cannot_issue(self):
        self.api.force_authenticate(self.cashier)
        self.assertEqual(self.api.post("/api/main/telegram-bot/owner-code/", {}, format="json").status_code, 403)

    def test_webhook_binds_owner_by_code(self):
        code = self._issue()
        upd = {"update_id": 1, "message": {"message_id": 1, "date": int(timezone.now().timestamp()),
               "chat": {"id": 555}, "from": {"id": 555, "first_name": "New"}, "text": code}}
        process_telegram_update(str(self.bot.id), upd, None)
        self.bot.refresh_from_db()
        self.assertEqual((self.bot.owner_chat_id, self.bot.owner_user_id), ("555", "555"))


class RateLimitTests(N23Base):
    def test_chat_retry_after(self):
        cache.clear()
        self.assertEqual(telegram_api.check_rate_limit("1:T", "c"), 0)
        self.assertGreater(telegram_api.check_rate_limit("1:T", "c"), 0)
        self.assertEqual(telegram_api.check_rate_limit("1:T", "other"), 0)

    def test_bot_limit(self):
        cache.clear()
        res = [telegram_api.check_rate_limit("2:T", f"c{i}") for i in range(telegram_api.BOT_MESSAGES_PER_SECOND + 5)]
        self.assertTrue(any(res))

    def test_webhook_dedup_by_update_id(self):
        url = f"/api/main/telegram-bot/webhook/{self.bot.bot_uuid}/"
        from rest_framework.test import APIClient
        c = APIClient()
        with patch("apps.main.telegram_bot.views_public.process_telegram_update.delay") as d:
            c.post(url, {"update_id": 77}, format="json")
            r = c.post(url, {"update_id": 77}, format="json")
        self.assertEqual(r.data.get("status"), "duplicate")
        self.assertEqual(d.call_count, 1)


class QueueHealthTests(N23Base):
    def test_owner_ok_cashier_forbidden(self):
        self.assertEqual(self.api.get("/api/main/telegram-bot/queue-health/").status_code, 200)
        self.api.force_authenticate(self.cashier)
        self.assertEqual(self.api.get("/api/main/telegram-bot/queue-health/").status_code, 403)
