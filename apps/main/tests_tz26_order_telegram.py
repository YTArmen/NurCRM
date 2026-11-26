"""ТЗ Б-22: заказ витрины → сообщение в Telegram (серверный бот), source, кнопки."""
from unittest.mock import patch

from apps.main.models import ShowcaseOrder
from apps.main.telegram_bot.models import TelegramBotAudit, TelegramBotSettings, TelegramOrderNotice
from apps.main.tests_tz_n27_showcase_orders import N27Base

TG = "apps.main.telegram_bot.services.telegram_api."


class OrderTelegramTests(N27Base):
    def setUp(self):
        super().setUp()
        self.bot = TelegramBotSettings.objects.create(
            company=self.company, mode=TelegramBotSettings.Mode.SERVER, token="123456:TEST",
            owner_chat_id="777",
        )
        p = patch(TG + "send_message", return_value={"ok": True})
        self.send = p.start()
        self.addCleanup(p.stop)
        p2 = patch(TG + "edit_message_reply_markup", return_value={"ok": True})
        p2.start()
        self.addCleanup(p2.stop)
        p3 = patch(TG + "answer_callback_query", return_value={"ok": True})
        self.answer = p3.start()
        self.addCleanup(p3.stop)

    def make(self, **extra):
        body = self.body(self.item(self.nike, self.v46))
        body.update(extra)
        with patch("apps.main.telegram_bot.tasks.send_order_notification.delay",
                   side_effect=lambda oid: __import__("apps.main.telegram_bot.services.order_notify", fromlist=["x"]).notify_new_order(oid)), \
                self.captureOnCommitCallbacks(execute=True):
            return self.order(body)

    def test_new_order_notifies_owner_without_amount_and_audit(self):
        r = self.make()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.send.call_count, 1)
        args, kw = self.send.call_args
        self.assertEqual(args[1], "777")
        self.assertIn("Nike", args[2])
        self.assertNotIn("1000", args[2])  # orders_show_amount=false
        self.assertIn("+996555123456", args[2])  # orders_show_phone=true
        cb = [b["callback_data"] for row in kw["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
        self.assertEqual(len(cb), 2)
        a = TelegramBotAudit.objects.get(action="order_notified")
        self.assertEqual((a.changes["ok"], a.changes["chat_id"]), (True, "777"))

    def test_amount_and_phone_flags(self):
        TelegramBotSettings.objects.filter(pk=self.bot.pk).update(orders_show_amount=True, orders_show_phone=False)
        self.make()
        text = self.send.call_args.args[2]
        self.assertIn("1000", text)
        self.assertNotIn("555123456", text)

    def test_one_notification_per_order(self):
        from apps.main.telegram_bot.services import order_notify
        self.make()
        oid = ShowcaseOrder.objects.get().id
        order_notify.notify_new_order(oid)
        self.assertEqual(self.send.call_count, 1)

    def test_source_telegram_bot_visible_and_filter(self):
        r = self.make(source="telegram_bot", telegram_chat_id="555")
        self.assertEqual(r.json()["source"], "telegram_bot")
        rows = self.api.get("/api/main/showcase/orders/?source=telegram_bot").json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.api.get("/api/main/showcase/orders/?source=voice_ai").json(), [])

    def test_bad_source_400(self):
        r = self.make(source="hack")
        self.assertEqual(r.status_code, 400)

    def test_failed_send_records_error_and_retry(self):
        self.send.return_value = {"ok": False, "description": "Forbidden: bot was blocked"}
        with patch("apps.main.telegram_bot.tasks.retry_order_notification.apply_async") as retry:
            self.make()
        self.bot.refresh_from_db()
        self.assertIn("blocked", self.bot.orders_last_error)
        retry.assert_called_once()
        self.assertEqual(retry.call_args.kwargs["countdown"], 60)
        self.assertFalse(TelegramOrderNotice.objects.get().ok)

    def test_buttons_accept_owner_only_and_cancel_releases(self):
        self.make()
        order = ShowcaseOrder.objects.get()
        self.v46.refresh_from_db()
        self.assertEqual(self.v46.quantity, 2)
        from apps.main.telegram_bot.services import callback_handler

        def press(action, uid):
            callback_handler.handle_callback_query(self.bot, {
                "id": "1", "from": {"id": uid}, "data": f"ordn:{order.id}:{action}",
                "message": {"chat": {"id": 777}, "message_id": 5}})

        press("a", 999)  # чужой
        order.refresh_from_db()
        self.assertEqual(order.status, "new")
        press("a", 777)
        order.refresh_from_db()
        self.assertEqual(order.status, "accepted")
        press("c", 777)
        order.refresh_from_db()
        self.assertEqual(order.status, "canceled")
        self.v46.refresh_from_db()
        self.assertEqual(self.v46.quantity, 3)

    def test_local_mode_does_not_send(self):
        TelegramBotSettings.objects.filter(pk=self.bot.pk).update(mode="local")
        self.make()
        self.assertEqual(self.send.call_count, 0)
