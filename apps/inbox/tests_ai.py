"""Б-79: ИИ-ответчик (модель мокается)."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from apps.inbox import ai, services
from apps.inbox.models import AiRun, Conversation, InboxAccess, Message
from apps.inbox.tests import BASE, InboxBase
from apps.inbox.tests_core import Mixin
from apps.main.models import Client, Product

MODEL = "apps.main.telegram_bot.services.ai_service.generate_chat_response_with_tools"


def fake_model(tool=None, args=None, text="Кола 1 л стоит 120 сом."):
    def fn(api_key, system_instruction, contents, tools, execute_tool_fn, **kw):
        if tool:
            execute_tool_fn(tool, args or {})
        return text, "gemini-test", [tool] if tool else []
    return fn


class AiBase(Mixin, InboxBase):
    def setUp(self):
        super().setUp()
        self.ch = self.mk_channel(settings={"ai_mode": "draft"})
        self.inbound(self.ch, text="Сколько стоит кола 1 л?")
        self.conv = Conversation.objects.get()
        self.p = Product.objects.create(company=self.company, name="Кола 1 л", price=Decimal("120"), quantity=Decimal("5"))
        for patcher in (mock.patch.object(ai, "_api_key", return_value="k"),
                        mock.patch("apps.inbox.tasks_ext.ai_auto_reply.delay"),
                        mock.patch.object(services, "enqueue_send")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def draft(self, **body):
        return self.api.post(f"{BASE}/conversations/{self.conv.id}/ai-draft/", body, format="json")


class DraftTests(AiBase):
    def test_price_from_catalog_logged(self):
        with mock.patch(MODEL, fake_model("catalog_search", {"q": "кола"})):
            r = self.draft()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("120", r.json()["text"])
        run = AiRun.objects.get(pk=r.json()["run_id"])
        self.assertEqual(run.tools[0]["name"], "catalog.search")
        self.assertEqual(Message.objects.filter(sender="ai").count(), 0)  # не отправляет
        lg = self.api.get(f"{BASE}/ai-runs/?conversation={self.conv.id}").json()
        self.assertEqual(lg["results"][0]["tools"][0]["name"], "catalog.search")
        self.assertEqual(lg["results"][0]["mode"], "draft")

    def test_number_not_from_tool_goes_to_human(self):
        with mock.patch(MODEL, fake_model("catalog_search", {"q": "кола"}, text="Кола стоит 999 сом.")):
            r = self.draft()
        self.assertIn("Уточню", r.json()["text"])
        self.conv.refresh_from_db()
        self.assertTrue(self.conv.needs_human)

    def test_no_product_no_data(self):
        with mock.patch(MODEL, fake_model("catalog_search", {"q": "нет такого"}, text="НЕТ_ДАННЫХ")):
            r = self.draft()
        self.assertIn("Уточню", r.json()["text"])
        self.conv.refresh_from_db()
        self.assertTrue(self.conv.needs_human)

    def test_debt_unlinked_refused_and_no_model(self):
        with mock.patch(MODEL) as m:
            r = self.draft(question="Сколько должен Асан?")
        m.assert_not_called()
        self.assertEqual(AiRun.objects.get().outcome, "refused")
        self.assertIn("только самому клиенту", r.json()["text"])

    def test_own_summary_only_linked_client(self):
        contact = self.conv.contact
        out = ai.tool_client_own_summary(self.company, self.ch, contact, {"client": "чужой"})
        self.assertEqual(out["error"], "not_linked")
        cl = Client.objects.create(company=self.company, full_name="Асан", phone="0700111222")
        contact.client = cl
        contact.save()
        out = ai.tool_client_own_summary(self.company, self.ch, contact, {})
        self.assertEqual(out["client"], "[Д1]")
        self.assertIn("debt", out)

    def test_handoff_words_and_amount(self):
        for text in ("Позовите оператора", "Хочу вернуть, это возврат", "Это жалоба!"):
            with mock.patch(MODEL) as m:
                r = self.draft(question=text)
            m.assert_not_called()
            self.assertEqual(r.json()["outcome"], "handoff")
        self.assertEqual(ai.handoff_reason("Куплю на 50000 сом", self.ch), "amount")

    def test_hourly_limit(self):
        for _ in range(20):
            AiRun.objects.create(conversation=self.conv, channel=self.ch, company=self.company)
        with mock.patch(MODEL, fake_model()):
            r = self.draft()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["code"], "ai_rate_limited")

    def test_budget_turns_ai_off(self):
        self.ch.settings = {**self.ch.settings, "ai_monthly_budget_tokens": 100}
        self.ch.save()
        AiRun.objects.create(conversation=self.conv, channel=self.ch, company=self.company, tokens=150)
        with mock.patch.object(ai, "notify_owner") as n, mock.patch(MODEL, fake_model()):
            r = self.draft()
        self.assertEqual(r.json()["code"], "ai_budget_exceeded")
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.settings["ai_mode"], "off")
        n.assert_called_once()

    def test_permissions(self):
        u = self.mk_user(can_view_clients=True)
        r = self.client_for(u).post(f"{BASE}/conversations/{self.conv.id}/ai-draft/", {}, format="json")
        self.assertEqual(r.status_code, 403)
        InboxAccess.objects.create(user=u, can_view_inbox=True, can_reply_inbox=True, can_use_ai_inbox=True,
                                   can_view_all_conversations=True)
        with mock.patch(MODEL, fake_model("catalog_search", {"q": "кола"})):
            r = self.client_for(u).post(f"{BASE}/conversations/{self.conv.id}/ai-draft/", {}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_three_unclear_handoff(self):
        with mock.patch(MODEL, fake_model(text="НЕ_ПОНЯЛ")):
            outs = [self.draft().json()["outcome"] for _ in range(3)]
        self.assertEqual(outs, ["unclear", "unclear", "handoff"])

    def test_send_draft_marks_run(self):
        with mock.patch(MODEL, fake_model("catalog_search", {"q": "кола"})):
            run_id = self.draft().json()["run_id"]
        with mock.patch.object(services, "enqueue_send"), self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(f"{BASE}/conversations/{self.conv.id}/messages/",
                              {"text": "Кола 1 л - 120 сом", "ai_run_id": run_id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        run = AiRun.objects.get(pk=run_id)
        self.assertEqual(run.sent_by, "user")
        self.assertIsNotNone(run.sent_message_id)

    def test_pii_masked_in_log(self):
        self.conv.contact.display_name = "Асан"
        self.conv.contact.save()
        with mock.patch(MODEL, fake_model(text="Асан, кола 120 сом")):
            r = self.draft(question="Асан спрашивает цену 120")
        self.assertEqual(AiRun.objects.get().question, "[Д1] спрашивает цену 120")
        self.assertIn("Асан,", r.json()["text"])


class AutoTests(AiBase):
    def setUp(self):
        super().setUp()
        self.ch.settings = {**self.ch.settings, "ai_mode": "auto", "ai_topics_auto": ["price"]}
        self.ch.save()
        self.msg = Message.objects.filter(direction="in").first()

    def test_auto_sends_with_intro_once(self):
        with mock.patch(MODEL, fake_model("catalog_search", {"q": "кола"})), \
                mock.patch.object(services, "enqueue_send") as enq:
            self.assertEqual(ai.auto_reply(self.conv.id, self.msg.id), "answered")
        out = Message.objects.get(sender="ai")
        self.assertTrue(out.text.startswith(ai.SHARED_NOTICE))
        enq.assert_called_once()

    def test_auto_topic_not_allowed_needs_human(self):
        m = services.ingest_inbound(self.ch, "555", message_external_id="555:99", text="Хочу заказать колу")[0]
        with mock.patch(MODEL) as mm:
            ai.auto_reply(self.conv.id, m.id)
        mm.assert_not_called()
        self.conv.refresh_from_db()
        self.assertTrue(self.conv.needs_human)

    def test_human_replied_ai_silent(self):
        self.conv.ai_paused_until = timezone.now() + timedelta(minutes=30)
        self.conv.save()
        self.assertEqual(ai.auto_reply(self.conv.id, self.msg.id), "paused")

    def test_outside_schedule(self):
        self.ch.settings = {**self.ch.settings, "ai_schedule": {"mode": "custom", "from": "03:00", "to": "03:01"}}
        self.ch.save()
        with mock.patch.object(timezone, "now", return_value=timezone.now().replace(hour=12)):
            self.assertEqual(ai.auto_reply(self.conv.id, self.msg.id), "outside_schedule")

    def test_instagram_window_closed(self):
        ig = self.mk_channel(kind="instagram", provider="meta", token="", settings={"ai_mode": "auto"})
        m, _ = services.ingest_inbound(ig, "ig1", message_external_id="ig1:1", text="Цена?")
        conv = m.conversation
        conv.window_expires_at = timezone.now() - timedelta(hours=1)
        conv.save()
        r = self.api.post(f"{BASE}/conversations/{conv.id}/ai-draft/", {}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "window_closed")
        self.assertEqual(ai.auto_reply(conv.id, m.id), "window_closed")

    def test_settings_validation(self):
        r = self.api.patch(f"{BASE}/channels/{self.ch.id}/", {"settings": {"ai_topics_auto": ["bogus"]}}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.api.patch(f"{BASE}/channels/{self.ch.id}/", {"settings": {"handoff_amount": 5000}}, format="json")
        self.assertEqual(r.json()["settings"]["handoff_amount"], 5000)
        self.assertEqual(r.json()["ai_warning"], "shared_key")
