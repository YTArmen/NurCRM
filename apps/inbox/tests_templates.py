"""Б-78: быстрые ответы, шаблоны WhatsApp (Meta мокается), отправка kind=template."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from apps.inbox import crypto, meta_api, services, tasks, templates
from apps.inbox.models import Channel, Conversation, Message, QuickReply, WaTemplate
from apps.inbox.tests import BASE, InboxBase
from apps.main.models import Client


class Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b


def fake_meta(routes):
    """routes: список (подстрока URL, метод, ответ|callable)."""
    calls = []

    def req(method, url, **kw):
        calls.append((method, url, kw))
        for part, m, res in routes:
            if part in url and m == method:
                return Resp(res(kw) if callable(res) else res)
        return Resp({"error": {"message": "no route", "code": 100}}, 400)
    req.calls = calls
    return req


class WaBase(InboxBase):
    def setUp(self):
        super().setUp()
        self.ch = Channel.objects.create(company=self.company, kind="whatsapp_cloud", provider="meta", name="WA",
                                         status="active", external={"waba_id": "W1", "phone_number_id": "P1"},
                                         webhook_secret="s")
        crypto.set_secrets(self.ch, {"access_token": "tok"})
        self.ch.save()

    def mk_conv(self, expired=False):
        c, _ = services.ingest_inbound(self.ch, "996700111222", message_external_id="wamid.in1", text="Привет",
                                       display_name="Асан")
        conv = c.conversation
        if expired:
            conv.window_expires_at = timezone.now() - timedelta(hours=1)
            conv.save()
        return conv


class QuickReplyTests(WaBase):
    def test_crud_and_render_variables(self):
        r = self.api.post(f"{BASE}/quick-replies/", {"shortcut": "/dolg", "title": "Долг",
                                                      "text": {"ru": "{имя}, ваш долг {долг} сом до {срок}. {магазин}"}},
                          format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(sorted(r.json()["variables"]), ["долг", "имя", "магазин", "срок"])
        qid = r.json()["id"]
        conv = self.mk_conv()
        cl = Client.objects.create(company=self.company, full_name="Асан Асанов", phone="0700111222")
        conv.contact.client = cl
        conv.contact.save()
        with mock.patch.object(services, "enqueue_send"), self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(f"{BASE}/conversations/{conv.id}/messages/", {"quick_reply_id": qid}, format="json")
        # окно открыто (входящее только что) — отправка идёт
        self.assertEqual(r.status_code, 201, r.content)
        txt = r.json()["message"]["text"]
        self.assertTrue(txt.startswith("Асан Асанов, ваш долг "))
        self.assertIn("Inbox Co", txt)

    def test_unknown_variable_and_shared_rights(self):
        r = self.api.post(f"{BASE}/quick-replies/", {"shortcut": "x", "text": {"ru": "{нет}"}}, format="json")
        self.assertEqual(r.status_code, 400)
        u = self.mk_user(can_view_clients=True)
        r = self.client_for(u).post(f"{BASE}/quick-replies/", {"shortcut": "x", "text": {"ru": "ок"}, "shared": True}, format="json")
        self.assertEqual(r.status_code, 403)
        r = self.client_for(u).post(f"{BASE}/quick-replies/", {"shortcut": "x", "text": {"ru": "ок"}}, format="json")
        self.assertEqual(r.status_code, 201)
        # чужой личный не виден, общий виден
        self.api.post(f"{BASE}/quick-replies/", {"shortcut": "all", "text": {"ru": "общий"}, "shared": True}, format="json")
        got = [q["shortcut"] for q in self.client_for(u).get(f"{BASE}/quick-replies/").json()["results"]]
        self.assertEqual(sorted(got), ["all", "x"])
        got_owner = [q["shortcut"] for q in self.api.get(f"{BASE}/quick-replies/").json()["results"]]
        self.assertEqual(sorted(got_owner), ["all"])


class TemplateTests(WaBase):
    def test_standard_set_creates_12_and_sync_statuses(self):
        fm = fake_meta([("/W1/message_templates", "POST", {"id": "T1", "status": "PENDING"})])
        with mock.patch.object(meta_api.requests, "request", fm):
            r = self.api.post(f"{BASE}/channels/{self.ch.id}/templates/standard/", {}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(len(r.json()["created"]), 12)
        self.assertEqual(WaTemplate.objects.count(), 12)
        self.assertEqual(WaTemplate.objects.filter(status="pending").count(), 12)
        # повтор — ничего нового
        with mock.patch.object(meta_api.requests, "request", fm):
            r = self.api.post(f"{BASE}/channels/{self.ch.id}/templates/standard/", {}, format="json")
        self.assertEqual(len(r.json()["created"]), 0)
        # синхронизация: Meta одобрила одно, отклонила другое
        data = {"data": [
            {"id": "T1", "name": "order_ready", "language": "ru", "status": "APPROVED", "category": "UTILITY",
             "components": [{"type": "BODY", "text": "Здравствуйте, {{1}}! Ваш заказ №{{2}} готов к выдаче."}]},
            {"id": "T2", "name": "thanks", "language": "ru", "status": "REJECTED", "category": "MARKETING",
             "rejected_reason": "INVALID_FORMAT", "components": [{"type": "BODY", "text": "x"}]}]}
        with mock.patch.object(meta_api.requests, "request", fake_meta([("/W1/message_templates", "GET", data)])):
            r = self.api.post(f"{BASE}/channels/{self.ch.id}/templates/sync/", {}, format="json")
        self.assertEqual(r.json()["synced"], 2)
        got = {(t["name"], t["language"]): t for t in self.api.get(f"{BASE}/channels/{self.ch.id}/templates/").json()["results"]}
        self.assertEqual(got[("order_ready", "ru")]["status"], "approved")
        self.assertEqual(got[("thanks", "ru")]["status"], "rejected")
        self.assertEqual(got[("thanks", "ru")]["rejected_reason"], "INVALID_FORMAT")

    def test_create_template_validation_and_permission(self):
        r = self.api.post(f"{BASE}/channels/{self.ch.id}/templates/",
                          {"name": "Bad Name", "language": "ru", "category": "utility", "components": []}, format="json")
        self.assertEqual(r.status_code, 400)
        fm = fake_meta([("/W1/message_templates", "POST", {"id": "T9", "status": "PENDING"})])
        with mock.patch.object(meta_api.requests, "request", fm):
            r = self.api.post(f"{BASE}/channels/{self.ch.id}/templates/", {
                "name": "hello", "language": "ru", "category": "utility",
                "components": [{"type": "BODY", "text": "Привет, {{1}}"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["params"], 1)
        u = self.mk_user(can_view_clients=True)
        r = self.client_for(u).get(f"{BASE}/channels/{self.ch.id}/templates/")
        self.assertEqual(r.status_code, 403)

    def test_meta_error_becomes_502(self):
        fm = fake_meta([])
        with mock.patch.object(meta_api.requests, "request", fm):
            r = self.api.post(f"{BASE}/channels/{self.ch.id}/templates/sync/", {}, format="json")
        self.assertIn(r.status_code, (400, 502))
        self.assertEqual(r.json()["code"], "provider_error")

    def test_pricing(self):
        r = self.api.get(f"{BASE}/channels/{self.ch.id}/pricing/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("marketing", r.json()["per_message"])


class SendTemplateTests(WaBase):
    def setUp(self):
        super().setUp()
        WaTemplate.objects.create(channel=self.ch, company=self.company, name="order_ready", language="ru",
                                  category="utility", status="approved",
                                  components=[{"type": "BODY", "text": "Здравствуйте, {{1}}! Заказ №{{2}} готов."}])

    def post(self, conv, tpl):
        with mock.patch.object(services, "enqueue_send"), self.captureOnCommitCallbacks(execute=True):
            return self.api.post(f"{BASE}/conversations/{conv.id}/messages/", {"kind": "template", "template": tpl}, format="json")

    def test_params_mismatch_400(self):
        conv = self.mk_conv(expired=True)
        r = self.post(conv, {"name": "order_ready", "language": "ru", "params": ["Асан"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "template_params_mismatch")

    def test_text_outside_window_closed_template_sent(self):
        conv = self.mk_conv(expired=True)
        r = self.api.post(f"{BASE}/conversations/{conv.id}/messages/", {"text": "привет"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "window_closed")
        r = self.post(conv, {"name": "order_ready", "language": "ru", "params": ["Асан", "12"]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["cost"]["category"], "utility")
        self.assertIn("Заказ №12", r.json()["message"]["text"])
        msg = Message.objects.get(kind="template")
        fm = fake_meta([("/P1/messages", "POST", {"messages": [{"id": "wamid.out1"}]})])
        with mock.patch.object(meta_api.requests, "request", fm):
            self.assertEqual(tasks.send_outbound(msg.id), "sent")
        msg.refresh_from_db()
        self.assertEqual((msg.status, msg.external_message_id), ("sent", "wamid.out1"))
        sent = fm.calls[0][2]["json"]
        self.assertEqual(sent["type"], "template")
        self.assertEqual(sent["template"]["components"][0]["parameters"][1]["text"], "12")

    def test_unapproved_template_rejected(self):
        WaTemplate.objects.update(status="pending")
        conv = self.mk_conv()
        r = self.post(conv, {"name": "order_ready", "language": "ru", "params": ["a", "b"]})
        self.assertEqual(r.json()["code"], "template_not_approved")

    def test_revoked_token_marks_channel_error(self):
        conv = self.mk_conv()
        with mock.patch.object(services, "enqueue_send"), self.captureOnCommitCallbacks(execute=True):
            self.api.post(f"{BASE}/conversations/{conv.id}/messages/", {"text": "hi"}, format="json")
        msg = Message.objects.get(direction="out")

        def bad(method, url, **kw):
            return Resp({"error": {"message": "expired", "code": 190}}, 401)
        with mock.patch.object(meta_api.requests, "request", bad), mock.patch("apps.inbox.ai.notify_owner") as n:
            self.assertEqual(tasks.send_outbound(msg.id), "failed")
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.status, "error")
        n.assert_called_once()
