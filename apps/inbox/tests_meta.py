"""Б-85, Б-86: Embedded Signup, качество номера, Instagram Login (Meta мокается)."""
import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.inbox import crypto, meta_api, meta_channels, services, tasks
from apps.inbox.models import Channel, Conversation, Message
from apps.inbox.tests import BASE, InboxBase
from apps.inbox.tests_templates import fake_meta

WA_ENV = dict(META_APP_ID="app1", META_APP_SECRET="appsecret", META_VERIFY_TOKEN="vt")
IG_ENV = dict(INSTAGRAM_APP_ID="ig1", INSTAGRAM_APP_SECRET="igsecret", INSTAGRAM_VERIFY_TOKEN="ivt")

WA_ROUTES = [
    ("oauth/access_token", "GET", {"access_token": "BUSINESS_TOKEN"}),
    ("debug_token", "GET", {"data": {"granular_scopes": [{"scope": "whatsapp_business_management", "target_ids": ["W1"]}]}}),
    ("/W1/phone_numbers", "GET", {"data": [{"id": "P1", "display_phone_number": "+996 700 111 222", "verified_name": "Магазин",
                                            "quality_rating": "GREEN", "name_status": "APPROVED",
                                            "messaging_limit_tier": "TIER_1K"}]}),
    ("/W1/subscribed_apps", "POST", {"success": True}),
    ("/P1/register", "POST", {"success": True}),
]


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class DisabledTests(InboxBase):
    def test_disabled_by_default(self):
        for url in ("channels/whatsapp/embedded-signup/start/", "channels/instagram/oauth/start/"):
            r = self.api.post(f"{BASE}/{url}", {}, format="json")
            self.assertEqual(r.status_code, 501, url)
            self.assertEqual(r.json()["code"], "integration_disabled")
        caps = self.api.get(f"{BASE}/capabilities/").json()
        self.assertFalse(caps["whatsapp_embedded"])
        self.assertFalse(caps["instagram_oauth"])


@override_settings(**WA_ENV)
class EmbeddedSignupTests(InboxBase):
    def connect(self):
        st = self.api.post(f"{BASE}/channels/whatsapp/embedded-signup/start/", {}, format="json")
        self.assertEqual(st.status_code, 200, st.content)
        state = st.json()["state"]
        with mock.patch.object(meta_api.requests, "request", fake_meta(WA_ROUTES)):
            return self.api.post(f"{BASE}/channels/whatsapp/embedded-signup/callback/",
                                 {"code": "CODE", "state": state}, format="json")

    def test_start_has_url_and_state(self):
        r = self.api.post(f"{BASE}/channels/whatsapp/embedded-signup/start/", {}, format="json").json()
        self.assertIn("client_id=app1", r["url"])
        self.assertIn("state=", r["url"])

    def test_callback_creates_active_channel_without_leaking_token(self):
        r = self.connect()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertNotIn("BUSINESS_TOKEN", r.content.decode())
        ch = Channel.objects.get(kind="whatsapp_cloud")
        self.assertEqual(ch.status, "active")
        self.assertEqual(ch.external["phone_number_id"], "P1")
        self.assertEqual(ch.health["quality_rating"], "green")
        self.assertEqual(crypto.get_secrets(ch)["access_token"], "BUSINESS_TOKEN")
        self.assertEqual(self.api.post(f"{BASE}/channels/whatsapp/embedded-signup/callback/", {"code": "x", "state": "bad"},
                                       format="json").json()["code"], "invalid_state")
        self.connect()  # повторное подключение того же номера — тот же канал
        self.assertEqual(Channel.objects.filter(kind="whatsapp_cloud").count(), 1)

    def test_needs_manage_channels(self):
        u = self.mk_user(can_view_clients=True)
        r = self.client_for(u).post(f"{BASE}/channels/whatsapp/embedded-signup/start/", {}, format="json")
        self.assertEqual(r.status_code, 403)

    def webhook(self, payload, secret="appsecret"):
        body = json.dumps(payload).encode()
        return APIClient().post("/api/inbox/meta/whatsapp/", data=body, content_type="application/json",
                                HTTP_X_HUB_SIGNATURE_256=sign(secret, body))

    def test_verify_and_signature(self):
        r = APIClient().get("/api/inbox/meta/whatsapp/?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=777")
        self.assertEqual(r.content, b"777")
        self.assertEqual(self.webhook({"entry": []}, secret="wrong").status_code, 401)

    def test_inbound_status_and_quality(self):
        self.connect()
        ch = Channel.objects.get(kind="whatsapp_cloud")
        msg_payload = {"object": "whatsapp_business_account", "entry": [{"id": "W1", "changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "P1"}, "contacts": [{"wa_id": "996700555444", "profile": {"name": "Асан"}}],
            "messages": [{"from": "996700555444", "id": "wamid.A", "type": "text", "text": {"body": "Здравствуйте"}}]}}]}]}
        with mock.patch.object(tasks.process_webhook_event, "delay", side_effect=lambda i: tasks.process_webhook_event(i)):
            self.assertEqual(self.webhook(msg_payload).status_code, 200)
        conv = Conversation.objects.get()
        self.assertEqual(conv.channel_id, ch.id)
        self.assertIsNotNone(conv.window_expires_at)
        self.assertEqual(Message.objects.get(direction="in").text, "Здравствуйте")
        # исходящее и статус delivered
        out = Message.objects.create(conversation=conv, channel=ch, company=self.company, direction="out", sender="user",
                                     text="ок", status="sent", external_message_id="wamid.OUT")
        st = {"entry": [{"id": "W1", "changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "P1"}, "statuses": [{"id": "wamid.OUT", "status": "delivered"}]}}]}]}
        with mock.patch.object(tasks.process_webhook_event, "delay", side_effect=lambda i: tasks.process_webhook_event(i)):
            self.webhook(st)
        out.refresh_from_db()
        self.assertEqual(out.status, "delivered")
        # качество номера
        q = {"entry": [{"id": "W1", "changes": [{"field": "phone_number_quality_update", "value": {
            "display_phone_number": "996700111222", "event": "DOWNGRADE", "current_limit": "TIER_250"}}]}]}
        routes = [("/P1", "GET", {"quality_rating": "RED", "messaging_limit_tier": "TIER_250", "name_status": "APPROVED"})]
        with mock.patch.object(tasks.process_webhook_event, "delay", side_effect=lambda i: tasks.process_webhook_event(i)), \
                mock.patch.object(meta_api.requests, "request", fake_meta(routes)), \
                mock.patch("apps.inbox.ai.notify_owner") as n:
            self.webhook(q)
        ch.refresh_from_db()
        self.assertEqual(ch.health["quality_rating"], "red")
        self.assertEqual(ch.health["messaging_tier"], "TIER_250")
        n.assert_called_once()
        shown = self.api.get(f"{BASE}/channels/{ch.id}/").json()["health"]
        self.assertEqual(shown["quality_rating"], "red")

    def test_access_revoked_sets_error(self):
        self.connect()
        ch = Channel.objects.get(kind="whatsapp_cloud")
        p = {"entry": [{"id": "W1", "changes": [{"field": "account_update", "value": {"event": "PARTNER_REMOVED"}}]}]}
        with mock.patch.object(tasks.process_webhook_event, "delay", side_effect=lambda i: tasks.process_webhook_event(i)), \
                mock.patch("apps.inbox.ai.notify_owner") as n:
            self.webhook(p)
        ch.refresh_from_db()
        self.assertEqual(ch.status, "error")
        n.assert_called_once()


IG_ROUTES = [
    ("api.instagram.com/oauth/access_token", "POST", {"access_token": "SHORT", "user_id": "IG1"}),
    ("access_token", "GET", {"access_token": "LONG", "expires_in": 5184000}),
    ("/me/subscribed_apps", "POST", {"success": True}),
    ("/me", "GET", {"user_id": "IG1", "username": "shop", "account_type": "BUSINESS"}),
]


@override_settings(**IG_ENV)
class InstagramTests(InboxBase):
    def connect(self, routes=IG_ROUTES):
        state = self.api.post(f"{BASE}/channels/instagram/oauth/start/", {}, format="json").json()["state"]
        with mock.patch.object(meta_api.requests, "request", fake_meta(routes)):
            return self.api.post(f"{BASE}/channels/instagram/oauth/callback/", {"code": "C", "state": state}, format="json")

    def test_connect_and_not_professional(self):
        r = self.connect()
        self.assertEqual(r.status_code, 201, r.content)
        ch = Channel.objects.get(kind="instagram")
        self.assertEqual((ch.status, ch.external["ig_user_id"]), ("active", "IG1"))
        self.assertEqual(crypto.get_secrets(ch)["access_token"], "LONG")
        self.assertNotIn("LONG", r.content.decode())
        ch.delete()
        routes = [(p, m, ({**b, "account_type": "PERSONAL"} if p == "/me" else b)) for p, m, b in IG_ROUTES]
        r = self.connect(routes)
        self.assertEqual(r.json()["code"], "ig_not_professional")

    def test_token_refresh_10_days_before_expiry(self):
        self.connect()
        ch = Channel.objects.get(kind="instagram")
        ch.health = {**ch.health, "token_expires_at": (timezone.now() + timedelta(days=30)).isoformat()}
        ch.save()
        self.assertEqual(meta_channels.refresh_instagram_tokens(), {"refreshed": 0, "failed": 0})
        ch.health = {**ch.health, "token_expires_at": (timezone.now() + timedelta(days=5)).isoformat()}
        ch.save()
        fm = fake_meta([("refresh_access_token", "GET", {"access_token": "NEW", "expires_in": 5184000})])
        with mock.patch.object(meta_api.requests, "request", fm):
            self.assertEqual(meta_channels.refresh_instagram_tokens(), {"refreshed": 1, "failed": 0})
        ch.refresh_from_db()
        self.assertEqual(crypto.get_secrets(ch)["access_token"], "NEW")
        ch.health = {**ch.health, "token_expires_at": (timezone.now() + timedelta(days=2)).isoformat()}
        ch.save()
        with mock.patch.object(meta_api.requests, "request", fake_meta([])), mock.patch("apps.inbox.ai.notify_owner") as n:
            self.assertEqual(meta_channels.refresh_instagram_tokens(), {"refreshed": 0, "failed": 1})
        ch.refresh_from_db()
        self.assertEqual(ch.status, "error")
        n.assert_called_once()

    def test_inbound_and_window_rules(self):
        self.connect()
        ch = Channel.objects.get(kind="instagram")
        body = json.dumps({"object": "instagram", "entry": [{"id": "IG1", "messaging": [{
            "sender": {"id": "U9"}, "recipient": {"id": "IG1"}, "message": {"mid": "m1", "text": "Цена?"}}]}]}).encode()
        with mock.patch.object(tasks.process_webhook_event, "delay", side_effect=lambda i: tasks.process_webhook_event(i)):
            r = APIClient().post("/api/inbox/meta/instagram/", data=body, content_type="application/json",
                                 HTTP_X_HUB_SIGNATURE_256=sign("igsecret", body))
        self.assertEqual(r.status_code, 200)
        conv = Conversation.objects.get()
        # 25 ч спустя: обычный ответ — window_closed, человек — human_agent, sent
        conv.window_expires_at = timezone.now() - timedelta(hours=1)
        conv.last_inbound_at = timezone.now() - timedelta(hours=25)
        conv.save()
        with mock.patch.object(services, "enqueue_send"), self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(f"{BASE}/conversations/{conv.id}/messages/", {"text": "Здравствуйте"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        msg = Message.objects.get(sender="user")
        self.assertTrue(msg.human_agent)
        fm = fake_meta([("/me/messages", "POST", {"message_id": "mid.OUT"})])
        with mock.patch.object(meta_api.requests, "request", fm):
            self.assertEqual(tasks.send_outbound(msg.id), "sent")
        self.assertEqual(fm.calls[0][2]["json"]["tag"], "HUMAN_AGENT")
        conv.last_inbound_at = timezone.now() - timedelta(days=8)
        conv.save()
        r = self.api.post(f"{BASE}/conversations/{conv.id}/messages/", {"text": "поздно"}, format="json")
        self.assertEqual(r.json()["code"], "window_closed")
