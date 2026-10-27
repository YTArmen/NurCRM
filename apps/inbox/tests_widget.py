"""Б-83: чат на витрине."""
from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient

from apps.inbox import contacts, widget
from apps.inbox.models import Channel, ChannelContact, Conversation, Message, WidgetSession
from apps.inbox.tests import BASE, InboxBase
from apps.main.models import Client

PUB = "/api/inbox/public/companies/inbox-co/chat"


class WidgetBase(InboxBase):
    def setUp(self):
        super().setUp()
        self.company.slug = "inbox-co"
        self.company.save()
        self.anon = APIClient()
        r = self.api.put(f"{BASE}/widget/settings/", {"enabled": True, "greeting": {"ru": "Здравствуйте!"},
                                                      "placement": ["all"], "offer_telegram": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def new_session(self, ip="1.1.1.1"):
        r = self.anon.post(f"{PUB}/sessions/", {}, format="json", REMOTE_ADDR=ip)
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def say(self, s, text="Привет", cmid="m1"):
        return self.anon.post(f"{PUB}/sessions/{s['session_id']}/messages/", {"text": text, "client_msg_id": cmid},
                              format="json", HTTP_X_VISITOR_TOKEN=s["visitor_token"])


class SessionTests(WidgetBase):
    def test_disabled_widget_404(self):
        self.api.put(f"{BASE}/widget/settings/", {"enabled": False}, format="json")
        r = self.anon.post(f"{PUB}/sessions/", {}, format="json")
        self.assertEqual(r.status_code, 404)

    def test_token_strength_and_hashed(self):
        s = self.new_session()
        self.assertGreaterEqual(len(s["visitor_token"]), 32)  # 43 символа = 256 бит
        row = WidgetSession.objects.get()
        self.assertNotIn(s["visitor_token"], row.token_hash)
        self.assertEqual(Channel.objects.get(kind="web_widget").status, "active")

    def test_ip_limit_5_per_minute(self):
        for _ in range(5):
            self.new_session()
        r = self.anon.post(f"{PUB}/sessions/", {}, format="json", REMOTE_ADDR="1.1.1.1")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r)
        self.new_session(ip="2.2.2.2")  # другой IP — можно

    def test_wrong_token_401(self):
        s = self.new_session()
        r = self.anon.post(f"{PUB}/sessions/{s['session_id']}/messages/", {"text": "x", "client_msg_id": "a"},
                           format="json", HTTP_X_VISITOR_TOKEN="x" * 43)
        self.assertEqual(r.status_code, 401)


class MessageTests(WidgetBase):
    def test_message_creates_conversation_and_dedupes(self):
        s = self.new_session()
        r1, r2 = self.say(s), self.say(s)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["duplicate"])
        self.assertEqual(Message.objects.filter(direction="in").count(), 1)
        conv = Conversation.objects.get()
        self.assertEqual(conv.channel.kind, "web_widget")

    def test_rate_limit_20_per_minute_retry_after(self):
        s = self.new_session()
        for i in range(20):
            self.assertEqual(self.say(s, f"т{i}", f"c{i}").status_code, 201)
        r = self.say(s, "лишнее", "c99")
        self.assertEqual(r.status_code, 429)
        self.assertTrue(int(r["Retry-After"]) >= 1)

    def test_length_limit(self):
        s = self.new_session()
        self.assertEqual(self.say(s, "я" * 2001).status_code, 400)
        self.assertEqual(self.say(s, "я" * 2000).status_code, 201)

    def test_poll_returns_staff_reply_without_staff_name(self):
        s = self.new_session()
        self.say(s)
        conv = Conversation.objects.get()
        self.owner.first_name = "СекретноеИмя"
        self.owner.save()
        from apps.inbox import services
        with mock.patch.object(services, "enqueue_send"), self.captureOnCommitCallbacks(execute=True):
            self.api.post(f"{BASE}/conversations/{conv.id}/messages/", {"text": "Здравствуйте"}, format="json")
        services.system_note(conv, "внутренняя заметка")
        r = self.anon.get(f"{PUB}/sessions/{s['session_id']}/messages/?after=0&wait=0", HTTP_X_VISITOR_TOKEN=s["visitor_token"])
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertNotIn("СекретноеИмя", body)
        self.assertNotIn("внутренняя заметка", body)
        self.assertNotIn(s["visitor_token"], body)
        roles = [m["sender"] for m in r.json()["results"]]
        self.assertEqual(roles, ["visitor", "seller"])
        last = r.json()["last_id"]
        r = self.anon.get(f"{PUB}/sessions/{s['session_id']}/messages/?after={last}&wait=0", HTTP_X_VISITOR_TOKEN=s["visitor_token"])
        self.assertEqual(r.json()["results"], [])

    def test_spam_marks_session_and_skips_ai(self):
        s = self.new_session()
        self.say(s, "http://a.ru http://b.ru http://c.ru", "x1")
        self.assertTrue(WidgetSession.objects.get().spam)
        from apps.inbox import ai
        ch = Channel.objects.get(kind="web_widget")
        ch.settings = {**ch.settings, "ai_mode": "auto"}
        ch.save()
        msg = Message.objects.filter(direction="in").first()
        self.assertEqual(ai.auto_reply(msg.conversation_id, msg.id), "spam")
        self.assertFalse(msg.conversation.needs_human)


class ContactTests(WidgetBase):
    def test_phone_requires_consent_and_links_client(self):
        cl = Client.objects.create(company=self.company, full_name="Асан", phone="0700111222")
        s = self.new_session()
        self.say(s)
        url = f"{PUB}/sessions/{s['session_id']}/contact/"
        hdr = {"HTTP_X_VISITOR_TOKEN": s["visitor_token"]}
        r = self.anon.post(url, {"phone": "0700111222"}, format="json", **hdr)
        self.assertEqual(r.status_code, 400)
        r = self.anon.post(url, {"name": "Асан", "phone": "0700111222", "consent": True}, format="json", **hdr)
        self.assertEqual(r.status_code, 200, r.content)
        c = ChannelContact.objects.get(channel__kind="web_widget")
        self.assertEqual(c.client_id, cl.id)
        self.assertTrue(c.consent_service)

    def test_telegram_continue_glues_contact(self):
        cl = Client.objects.create(company=self.company, full_name="Асан", phone="0700111222")
        tg = Channel.objects.create(company=self.company, kind="telegram_bot", provider="telegram", status="active",
                                    external={"bot_username": "shopbot"})
        s = self.new_session()
        self.say(s)
        ws = WidgetSession.objects.get()
        ws.contact.client = cl
        ws.contact.save()
        r = self.anon.get(f"{PUB}/sessions/{s['session_id']}/telegram-link/", HTTP_X_VISITOR_TOKEN=s["visitor_token"])
        url = r.json()["url"]
        self.assertTrue(url.startswith("https://t.me/shopbot?start=w_"))
        self.assertNotIn(s["visitor_token"], url)
        payload = url.split("start=")[1]
        tgc, _ = contacts.get_or_create_contact(tg, "777", display_name="Ася", start_payload=payload)
        self.assertEqual(tgc.client_id, cl.id)
