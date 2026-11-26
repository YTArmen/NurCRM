"""Б-73, Б-87: каналы, секреты, check, capabilities."""
import json
from unittest import mock

from apps.inbox import crypto, telegram
from apps.inbox.models import Channel, InboxAccess
from apps.inbox.tests import BASE, InboxBase
from apps.main.telegram_bot.models import TelegramBotSettings


class ChannelTests(InboxBase):
    def test_server_bot_visible_as_single_channel(self):
        bs = TelegramBotSettings.objects.create(company=self.company, bot_username="nbs_bot", webhook_ok=True)
        bs.token = "123:SECRETTOKEN"
        bs.save()
        r = self.api.get(f"{BASE}/channels/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["count"], 1)
        row = r.data["results"][0]
        self.assertEqual((row["kind"], row["status"]), ("telegram_bot", "active"))
        self.assertTrue(row["token_set"])
        self.assertEqual(row["external"]["bot_username"], "nbs_bot")
        self.api.get(f"{BASE}/channels/")  # повторно: та же запись
        self.assertEqual(Channel.objects.filter(company=self.company).count(), 1)
        self.assertNotIn("SECRETTOKEN", json.dumps(r.data, default=str))

    def test_token_write_only_and_encrypted(self):
        r = self.api.post(f"{BASE}/channels/", {"kind": "telegram_bot", "name": "Бот", "token": "777:ABCSECRET"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data["token_set"])
        self.assertNotIn("ABCSECRET", json.dumps(r.data))
        ch = Channel.objects.get(id=r.data["id"])
        self.assertNotIn("ABCSECRET", ch.secrets_enc)
        self.assertEqual(crypto.get_secrets(ch)["token"], "777:ABCSECRET")
        r2 = self.api.patch(f"{BASE}/channels/{ch.id}/", {"token": "888:NEWSECRET"}, format="json")
        self.assertEqual(r2.status_code, 200, r2.data)
        self.assertTrue(r2.data["token_set"])
        self.assertNotIn("NEWSECRET", json.dumps(r2.data))
        r3 = self.api.get(f"{BASE}/channels/{ch.id}/")
        self.assertNotIn("NEWSECRET", json.dumps(r3.data))
        r4 = self.api.patch(f"{BASE}/channels/{ch.id}/", {"token": ""}, format="json")
        self.assertFalse(r4.data["token_set"])

    def test_aesgcm_roundtrip_and_tamper(self):
        blob = crypto.encrypt_dict({"token": "x"})
        self.assertEqual(crypto.decrypt_dict(blob), {"token": "x"})
        self.assertEqual(crypto.decrypt_dict(blob[:-3] + "AAA"), {})
        with mock.patch.dict("os.environ", {"INBOX_SECRET_KEY": "other"}):
            self.assertEqual(crypto.decrypt_dict(blob), {})

    def test_cashier_without_right_gets_403_on_post(self):
        c = self.client_for(self.mk_user())
        r = c.post(f"{BASE}/channels/", {"kind": "telegram_bot", "token": "1:a"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data["code"], "permission_denied")
        self.assertEqual(r.data["required"], "can_manage_channels")

    def test_view_rights_default_and_explicit(self):
        u = self.mk_user()
        self.assertEqual(self.client_for(u).get(f"{BASE}/channels/").status_code, 403)
        u2 = self.mk_user(can_view_clients=True)
        self.assertEqual(self.client_for(u2).get(f"{BASE}/channels/").status_code, 200)
        InboxAccess.objects.create(user=u2, can_view_inbox=False)
        u2 = type(u2).objects.get(pk=u2.pk)
        self.assertEqual(self.client_for(u2).get(f"{BASE}/channels/").status_code, 403)

    def test_check_wrong_token(self):
        r = self.api.post(f"{BASE}/channels/", {"kind": "telegram_bot", "token": "1:bad"}, format="json")
        cid = r.data["id"]
        resp = mock.Mock()
        resp.status_code = 401
        resp.json.return_value = {"ok": False, "error_code": 401, "description": "Unauthorized"}
        with mock.patch.object(telegram.requests, "get", return_value=resp) as g:
            out = self.api.post(f"{BASE}/channels/{cid}/check/", {}, format="json")
        self.assertEqual(out.status_code, 200)
        self.assertFalse(out.data["ok"])
        self.assertEqual(out.data["detail"], "Telegram: Unauthorized")
        self.assertEqual(out.data["status"], "error")
        self.assertEqual(g.call_count, 1)

    def test_check_ok(self):
        r = self.api.post(f"{BASE}/channels/", {"kind": "telegram_bot", "token": "1:good"}, format="json")
        cid = r.data["id"]

        def fake_get(url, timeout=0):
            m = mock.Mock()
            if url.endswith("getMe"):
                m.json.return_value = {"ok": True, "result": {"username": "shop_bot", "first_name": "Shop"}}
            else:
                m.json.return_value = {"ok": True, "result": {"url": "https://x/", "pending_update_count": 0}}
            return m
        with mock.patch.object(telegram.requests, "get", side_effect=fake_get):
            out = self.api.post(f"{BASE}/channels/{cid}/check/", {}, format="json")
        self.assertTrue(out.data["ok"], out.data)
        self.assertEqual(out.data["status"], "active")
        self.assertEqual(self.api.get(f"{BASE}/channels/{cid}/").data["external"]["bot_username"], "shop_bot")

    def test_capabilities(self):
        r = self.api.get(f"{BASE}/capabilities/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["inbox"])
        self.assertIn("telegram_bot", r.data["kinds"])
        self.assertNotIn("instagram", r.data["kinds"])  # у компании нет can_view_instagram

    def test_kind_limited_by_company_flag(self):
        r = self.api.post(f"{BASE}/channels/", {"kind": "instagram"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data["code"], "channel_not_allowed")

    def test_post_idempotent(self):
        body = {"kind": "telegram_bot", "token": "1:a", "name": "A"}
        k = "idem-key-0123456789"
        r1 = self.api.post(f"{BASE}/channels/", body, format="json", HTTP_IDEMPOTENCY_KEY=k)
        r2 = self.api.post(f"{BASE}/channels/", body, format="json", HTTP_IDEMPOTENCY_KEY=k)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.data["id"], r1.data["id"])
        self.assertEqual(Channel.objects.filter(company=self.company).count(), 1)

    def test_delete_empty_channel(self):
        r = self.api.post(f"{BASE}/channels/", {"kind": "telegram_bot", "token": "1:a"}, format="json")
        self.assertEqual(self.api.delete(f"{BASE}/channels/{r.data['id']}/").status_code, 204)


class UnofficialTests(InboxBase):
    def test_unofficial_requires_risk_accept(self):
        r = self.api.post(f"{BASE}/channels/", {"kind": "whatsapp_bsp", "provider": "unofficial"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "risk_not_accepted")
        r = self.api.post(f"{BASE}/channels/", {"kind": "whatsapp_bsp", "provider": "unofficial", "risk_accepted": True}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data["risk_accepted_at"])
        self.assertEqual(r.data["risk_accepted_by"]["id"], str(self.owner.id))

    def test_unofficial_ai_auto_forbidden(self):
        r = self.api.post(f"{BASE}/channels/", {"kind": "whatsapp_bsp", "provider": "unofficial", "risk_accepted": True,
                                                "settings": {"ai_mode": "auto"}}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "channel_not_allowed")
        cid = self.api.post(f"{BASE}/channels/", {"kind": "whatsapp_bsp", "provider": "unofficial", "risk_accepted": True},
                            format="json").data["id"]
        r = self.api.patch(f"{BASE}/channels/{cid}/", {"settings": {"ai_mode": "auto"}}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "channel_not_allowed")
        self.assertEqual(self.api.patch(f"{BASE}/channels/{cid}/", {"settings": {"ai_mode": "draft"}}, format="json").status_code, 200)

    def test_broadcast_guard(self):
        from apps.inbox import channels as svc
        from core.exceptions import ApiError
        with self.assertRaises(ApiError) as cm:
            svc.ensure_channel_allowed_for_broadcast(Channel(provider="unofficial"))
        self.assertEqual(cm.exception.code, "channel_not_allowed")
