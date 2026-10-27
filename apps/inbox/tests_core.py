"""Б-74, Б-75, Б-76 (ядро): контакты, диалоги, статусы, исходящие, вебхук, бот, push, события."""
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from apps.inbox import bot_hooks, contacts, crypto, push, services, tasks, telegram
from apps.inbox.models import (
    Channel, ChannelContact, Conversation, DataErasureLog, Label, Message, OwnerAlert, PendingStatus, PushLog,
    WebhookEvent,
)
from apps.inbox.tests import BASE, InboxBase
from apps.main.models import Client
from apps.main.telegram_bot.models import TelegramBotSettings
from core.exceptions import ApiError


class Mixin:
    def mk_channel(self, kind="telegram_bot", provider="telegram", token="1:tok", **kw):
        ch = Channel.objects.create(company=self.company, kind=kind, provider=provider, name="Ch",
                                    status="active", webhook_secret="whsecret", **kw)
        if token:
            crypto.set_secrets(ch, {"token": token})
            ch.save()
        return ch

    def inbound(self, ch, chat="555", text="Привет", mid=None, **kw):
        return services.ingest_inbound(ch, chat, message_external_id=mid or f"{chat}:{Message.objects.count() + 1}",
                                       text=text, display_name="Асан", **kw)


class IngestTests(Mixin, InboxBase):
    def test_dedupe_and_one_open_conversation(self):
        ch = self.mk_channel()
        m1, c1 = self.inbound(ch, mid="555:1")
        m2, c2 = self.inbound(ch, mid="555:1")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(m1.id, m2.id)
        self.inbound(ch, mid="555:2", text="Ещё")
        self.assertEqual(Message.objects.filter(channel=ch).count(), 2)
        self.assertEqual(Conversation.objects.filter(channel=ch, status="open").count(), 1)
        conv = Conversation.objects.get()
        self.assertEqual(conv.waiting_since is not None, True)
        self.assertEqual(conv.last_message_text, "Ещё")

    def test_reopen_recent_closed_and_new_after_24h(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        conv = Conversation.objects.get()
        services.set_status(conv, "closed", closed_reason="sold")
        _, _ = self.inbound(ch, mid="555:2")
        conv.refresh_from_db()
        self.assertEqual(conv.status, "open")  # переоткрыт
        services.set_status(conv, "closed")
        Conversation.objects.filter(pk=conv.pk).update(closed_at=timezone.now() - timedelta(hours=25))
        self.inbound(ch, mid="555:3")
        self.assertEqual(Conversation.objects.filter(channel=ch).count(), 2)  # новый диалог

    def test_status_monotonic_and_before_message(self):
        ch = self.mk_channel()
        m, _ = self.inbound(ch, mid="555:1")
        # исходящее с внешним id
        conv = Conversation.objects.get()
        out = Message.objects.create(conversation=conv, channel=ch, company=self.company, direction="out",
                                     sender="user", text="x", status="sent", external_message_id="555:9")
        self.assertEqual(services.apply_status(ch, "555:9", "read"), "applied")
        self.assertEqual(services.apply_status(ch, "555:9", "delivered"), "ignored")  # read раньше delivered → итог read
        out.refresh_from_db()
        self.assertEqual(out.status, "read")
        self.assertEqual(services.apply_status(ch, "555:9", "failed"), "ignored")  # failed не затирает read
        # статус раньше сообщения
        self.assertEqual(services.apply_status(ch, "555:77", "delivered"), "pending")
        self.assertEqual(PendingStatus.objects.count(), 1)
        out2 = Message.objects.create(conversation=conv, channel=ch, company=self.company, direction="out",
                                      sender="user", text="y", status="sent", external_message_id="555:77")
        services._apply_pending(ch, out2)
        out2.refresh_from_db()
        self.assertEqual(out2.status, "delivered")
        self.assertEqual(PendingStatus.objects.count(), 0)

    def test_stop_command_and_consent_required(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        self.inbound(ch, mid="555:2", text="СТОП")
        c = ChannelContact.objects.get()
        self.assertFalse(c.consent_service)
        self.assertFalse(c.consent_marketing)
        conv = Conversation.objects.get()
        self.assertTrue(conv.messages.filter(sender="system", kind="note").exists())
        with self.assertRaises(ApiError) as cm:
            services.create_outgoing(conv, self.owner, {"text": "Привет"})
        self.assertEqual(cm.exception.code, "consent_required")
        # заметка внутри допустима
        note, _ = services.create_outgoing(conv, self.owner, {"kind": "note", "text": "заметка"})
        self.assertEqual(note.kind, "note")
        # /start возвращает служебные
        self.inbound(ch, mid="555:3", text="/start")
        c.refresh_from_db()
        self.assertTrue(c.consent_service)
        for word in ("stop", "/stop"):
            self.assertTrue(contacts.is_stop(word))


class LinkTests(Mixin, InboxBase):
    def mk_client(self, name, phone, **kw):
        return Client.objects.create(company=self.company, full_name=name, phone=phone, **kw)

    def test_phone_autolink_single_and_candidates(self):
        wa = self.mk_channel("whatsapp_cloud", "meta", token="")
        a = self.mk_client("Асан", "0700 123 456")
        m, _ = services.ingest_inbound(wa, "996700123456", message_external_id="w1", text="hi", phone="996700123456")
        c = ChannelContact.objects.get(external_id="996700123456")
        self.assertEqual(c.client_id, a.id)
        # два клиента с одним номером → не связывать, показать кандидатов
        b1 = self.mk_client("Б1", "0555 111 222")
        b2 = self.mk_client("Б2", "+996555111222")
        services.ingest_inbound(wa, "996555111222", message_external_id="w2", text="hi", phone="996555111222")
        c2 = ChannelContact.objects.get(external_id="996555111222")
        self.assertIsNone(c2.client_id)
        self.assertEqual({x["id"] for x in c2.client_candidates}, {str(b1.id), str(b2.id)})
        # ручная связь снимает кандидатов
        contacts.link(c2, b1)
        c2.refresh_from_db()
        self.assertEqual((c2.client_id, c2.client_candidates), (b1.id, []))
        contacts.unlink(c2)
        c2.refresh_from_db()
        self.assertIsNone(c2.client_id)

    def test_telegram_chat_id_and_start_label(self):
        ch = self.mk_channel()
        a = self.mk_client("Асан", "0700000001", telegram_chat_id="555")
        services.ingest_inbound(ch, "555", message_external_id="555:1", text="hi")
        self.assertEqual(ChannelContact.objects.get(external_id="555").client_id, a.id)
        b = self.mk_client("Бека", "0700000002")
        services.ingest_inbound(ch, "666", message_external_id="666:1", text=f"/start s_{b.id}", start_payload=f"s_{b.id}")
        self.assertEqual(ChannelContact.objects.get(external_id="666").client_id, b.id)
        b.refresh_from_db()
        self.assertEqual(b.telegram_chat_id, "666")  # записали, потому что было пусто


class OutgoingTests(Mixin, InboxBase):
    def test_idempotency_note_and_window(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        conv = Conversation.objects.get()
        key = "key-0123456789abcdef"
        with mock.patch.object(services, "enqueue_send") as enq, self.captureOnCommitCallbacks(execute=True):
            m1, r1 = services.create_outgoing(conv, self.owner, {"text": "Ответ"}, key)
            m2, r2 = services.create_outgoing(conv, self.owner, {"text": "Ответ"}, key)
        self.assertEqual((r1, r2, m1.id), (False, True, m2.id))
        self.assertEqual(m1.status, "queued")
        self.assertEqual(enq.call_count, 1)  # в очередь на отправку поставлено один раз
        with self.assertRaises(ApiError) as cm:
            services.create_outgoing(conv, self.owner, {"text": "Другое"}, key)
        self.assertEqual(cm.exception.code, "idempotency_conflict")
        conv.refresh_from_db()
        self.assertIsNone(conv.waiting_since)
        self.assertEqual(conv.assignee_id, self.owner.id)
        self.assertGreater(conv.ai_paused_until, timezone.now())
        self.assertTrue(services.ai_paused(ch, "555"))
        # заметка наружу не уходит
        with mock.patch.object(services, "enqueue_send") as enq, self.captureOnCommitCallbacks(execute=True):
            n, _ = services.create_outgoing(conv, self.owner, {"kind": "note", "text": "внутр"})
        enq.assert_not_called()
        self.assertEqual(n.status, "sent")
        from apps.main.models import ChangeEvent
        self.assertTrue(ChangeEvent.objects.filter(company=self.company, type="inbox_message_created").exists())

    def test_window_closed_whatsapp_and_blocked(self):
        wa = self.mk_channel("whatsapp_cloud", "meta", token="")
        services.ingest_inbound(wa, "996700000009", message_external_id="w1", text="hi")
        conv = Conversation.objects.get()
        self.assertIsNotNone(conv.window_expires_at)
        services.create_outgoing(conv, self.owner, {"text": "ок, окно открыто"})
        Conversation.objects.filter(pk=conv.pk).update(window_expires_at=timezone.now() - timedelta(minutes=1))
        conv.refresh_from_db()
        with self.assertRaises(ApiError) as cm:
            services.create_outgoing(conv, self.owner, {"text": "поздно"})
        self.assertEqual(cm.exception.code, "window_closed")
        ChannelContact.objects.update(blocked=True)
        conv = Conversation.objects.select_related("contact", "channel").get(pk=conv.pk)
        with self.assertRaises(ApiError) as cm:
            services.create_outgoing(conv, self.owner, {"text": "x"})
        self.assertEqual(cm.exception.code, "contact_blocked")

    def test_instagram_human_agent_window(self):
        ig = self.mk_channel("instagram", "meta", token="")
        services.ingest_inbound(ig, "igsid1", message_external_id="i1", text="hi")
        conv = Conversation.objects.get()
        Conversation.objects.filter(pk=conv.pk).update(window_expires_at=timezone.now() - timedelta(hours=2),
                                                       last_inbound_at=timezone.now() - timedelta(hours=30))
        conv.refresh_from_db()
        m, _ = services.create_outgoing(conv, self.owner, {"text": "от человека"})
        self.assertTrue(m.human_agent)
        Conversation.objects.filter(pk=conv.pk).update(last_inbound_at=timezone.now() - timedelta(days=8))
        conv.refresh_from_db()
        with self.assertRaises(ApiError) as cm:
            services.create_outgoing(conv, self.owner, {"text": "через 8 дней"})
        self.assertEqual(cm.exception.code, "window_closed")

    def test_send_task_success_retry_fail_blocked(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        conv = Conversation.objects.get()
        m, _ = services.create_outgoing(conv, self.owner, {"text": "Ответ"}, "")
        with mock.patch.object(tasks, "_chat_rate_wait", return_value=0), \
                mock.patch.object(telegram, "send_message", return_value={"message_id": 42}) as sm:
            self.assertEqual(tasks.send_outbound(m.id), "sent")
        sm.assert_called_once()
        m.refresh_from_db()
        self.assertEqual((m.status, m.external_message_id), ("sent", "555:42"))
        self.assertEqual(tasks.send_outbound(m.id), "skipped")  # повторно не отправляется
        # временные ошибки: повторы 1, 5, 30, 120 с → failed
        m2, _ = services.create_outgoing(conv, self.owner, {"text": "Второе"}, "")
        err = telegram.TelegramError("Telegram: Bad Gateway", code=502)
        delays = []
        with mock.patch.object(tasks, "_chat_rate_wait", return_value=0), \
                mock.patch.object(telegram, "send_message", side_effect=err), \
                mock.patch.object(services, "enqueue_send", side_effect=lambda i, countdown=0: delays.append(countdown)):
            res = [tasks.send_outbound(m2.id) for _ in range(5)]
        self.assertEqual(delays, [1, 5, 30, 120])
        self.assertEqual(res[-1], "failed")
        m2.refresh_from_db()
        self.assertEqual((m2.status, m2.error["code"]), ("failed", "channel_error"))
        # 403 — бота заблокировали
        m3, _ = services.create_outgoing(conv, self.owner, {"text": "Третье"}, "")
        with mock.patch.object(tasks, "_chat_rate_wait", return_value=0), \
                mock.patch.object(telegram, "send_message",
                                  side_effect=telegram.TelegramError("Telegram: Forbidden: bot was blocked", code=403, permanent=True)):
            tasks.send_outbound(m3.id)
        m3.refresh_from_db()
        self.assertEqual(m3.error["code"], "contact_blocked")
        self.assertTrue(ChannelContact.objects.get().blocked)

    def test_send_task_rate_limit_and_retry_after(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        conv = Conversation.objects.get()
        m, _ = services.create_outgoing(conv, self.owner, {"text": "x"}, "")
        calls = []
        with mock.patch.object(tasks, "_chat_rate_wait", return_value=0.6), \
                mock.patch.object(services, "enqueue_send", side_effect=lambda i, countdown=0: calls.append(countdown)):
            self.assertEqual(tasks.send_outbound(m.id), "rate_limited")
        self.assertEqual(calls, [1])
        with mock.patch.object(tasks, "_chat_rate_wait", return_value=0), \
                mock.patch.object(telegram, "send_message", side_effect=telegram.TelegramError("429", code=429, retry_after=7)), \
                mock.patch.object(services, "enqueue_send", side_effect=lambda i, countdown=0: calls.append(countdown)):
            self.assertEqual(tasks.send_outbound(m.id), "retry_after")
        self.assertEqual(calls[-1], 8)
        m.refresh_from_db()
        self.assertEqual(m.attempts, 0)


class ActionsTests(Mixin, InboxBase):
    def test_take_is_atomic(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        conv = Conversation.objects.get()
        u2 = self.mk_user()
        services.take(conv.id, self.owner)
        with self.assertRaises(ApiError) as cm:
            services.take(conv.id, u2)
        self.assertEqual((cm.exception.code, cm.exception.status_code), ("already_assigned", 409))
        services.take(conv.id, self.owner)  # свой повтор — не ошибка

    def test_status_snooze_and_closed_reason(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        conv = Conversation.objects.get()
        with self.assertRaises(ApiError):
            services.set_status(conv, "snoozed")
        services.set_status(conv, "snoozed", timezone.now() + timedelta(hours=1))
        conv.refresh_from_db()
        self.assertEqual(conv.status, "snoozed")
        Conversation.objects.filter(pk=conv.pk).update(snoozed_until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(tasks.wake_snoozed(), 1)
        with self.assertRaises(ApiError):
            services.set_status(conv, "closed", closed_reason="bad")

    def test_visibility(self):
        ch = self.mk_channel()
        self.inbound(ch, chat="1", mid="1:1")
        self.inbound(ch, chat="2", mid="2:1")
        c1, c2 = Conversation.objects.order_by("id")
        u = self.mk_user(can_view_clients=True)
        Conversation.objects.filter(pk=c1.pk).update(assignee=self.owner)
        vis = set(services.visible_conversations(u, self.company).values_list("pk", flat=True))
        self.assertEqual(vis, {c2.pk})  # чужой назначенный не виден, неназначенный виден
        services.take(c2.pk, u)
        self.assertEqual(services.unread_counts(u, self.company)["mine"], 1)
        # all_conversations — видит всё
        from apps.inbox.models import InboxAccess
        InboxAccess.objects.create(user=u, can_view_inbox=True, can_view_all_conversations=True)
        u = type(u).objects.get(pk=u.pk)
        self.assertEqual(services.visible_conversations(u, self.company).count(), 2)

    def test_erasure_logs_and_cleans(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        c = ChannelContact.objects.get()
        r = self.api.delete(f"{BASE}/contacts/{c.id}/data/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Message.objects.count(), 0)
        c.refresh_from_db()
        self.assertEqual(c.display_name, "")
        log = DataErasureLog.objects.get()
        self.assertEqual(log.messages_deleted, 1)
        cashier = self.client_for(self.mk_user(can_view_clients=True))
        self.assertEqual(cashier.delete(f"{BASE}/contacts/{c.id}/data/").status_code, 403)


class ApiFlowTests(Mixin, InboxBase):
    def test_conversations_flow(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1", text="Здравствуйте")
        conv = Conversation.objects.get()
        # список: unread 1, телефон маскируется для роли без can_view_clients
        r = self.api.get(f"{BASE}/conversations/?status=open")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["count"], 1)
        item = r.data["results"][0]
        self.assertEqual(item["unread_count"], 1)
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["last_message"]["text"], "Здравствуйте")
        self.assertEqual(self.api.get(f"{BASE}/unread-count/").data["total"], 1)
        # лента
        r = self.api.get(f"{BASE}/conversations/{conv.id}/messages/")
        self.assertEqual(len(r.data["results"]), 1)
        # отправка с Idempotency-Key: queued, повтор -> replayed, одно сообщение
        key = "send-key-0123456789"
        r1 = self.api.post(f"{BASE}/conversations/{conv.id}/messages/", {"text": "Ответ"}, format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertEqual(r1.data["message"]["status"], "queued")
        r2 = self.api.post(f"{BASE}/conversations/{conv.id}/messages/", {"text": "Ответ"}, format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.data["replayed"])
        self.assertEqual(r2.data["message"]["id"], r1.data["message"]["id"])
        self.assertEqual(Message.objects.filter(direction="out", kind="text").count(), 1)
        # прочитано
        self.api.post(f"{BASE}/conversations/{conv.id}/read/", {}, format="json")
        self.assertEqual(self.api.get(f"{BASE}/unread-count/").data["total"], 0)
        # метки и статус
        lab = self.api.post(f"{BASE}/labels/", {"name": "VIP", "color": "#f00"}, format="json")
        self.assertEqual(lab.status_code, 201)
        r = self.api.post(f"{BASE}/conversations/{conv.id}/labels/", {"add": [lab.data["id"]]}, format="json")
        self.assertEqual([l["name"] for l in r.data["labels"]], ["VIP"])
        r = self.api.post(f"{BASE}/conversations/{conv.id}/status/", {"status": "closed", "closed_reason": "sold"}, format="json")
        self.assertEqual(r.data["status"], "closed")
        # ETag/304 при updated_after
        r = self.api.get(f"{BASE}/conversations/?updated_after=2020-01-01T00:00:00Z")
        etag = r["ETag"]
        self.assertTrue(etag)
        r304 = self.api.get(f"{BASE}/conversations/?updated_after=2020-01-01T00:00:00Z", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r304.status_code, 304)
        # событие в ленте (после коммита; в TestCase проверяем только что вызов не падает)
        # другой сотрудник без «всех чатов» не видит чужой назначенный диалог (assignee = owner после ответа)
        other = self.client_for(self.mk_user(can_view_clients=True))
        self.assertEqual(other.get(f"{BASE}/conversations/").data["count"], 0)
        self.assertEqual(other.get(f"{BASE}/conversations/{conv.id}/").status_code, 403)

    def test_take_race_via_api(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        conv = Conversation.objects.get()
        a = self.client_for(self.mk_user(can_view_clients=True))
        b = self.client_for(self.mk_user(can_view_clients=True))
        self.assertEqual(a.post(f"{BASE}/conversations/{conv.id}/take/", {}, format="json").status_code, 200)
        r = b.post(f"{BASE}/conversations/{conv.id}/take/", {}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (409, "already_assigned"))

    def test_client_channels_and_link(self):
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        cl = Client.objects.create(company=self.company, full_name="Асан", phone="0700111222")
        c = ChannelContact.objects.get()
        r = self.api.post(f"{BASE}/contacts/{c.id}/link/", {"client": str(cl.id)}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["client"]["full_name"], "Асан")
        r = self.api.get(f"{BASE}/clients/{cl.id}/channels/")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["channel"]["kind"], "telegram_bot")


class WebhookTests(Mixin, InboxBase):
    def update(self, uid, text="Привет", chat=555, mid=10):
        return {"update_id": uid, "message": {"message_id": mid, "date": int(timezone.now().timestamp()),
                "chat": {"id": chat, "type": "private"}, "from": {"id": chat, "first_name": "Асан", "language_code": "ru"},
                "text": text}}

    def test_signature_dedupe_and_health(self):
        ch = self.mk_channel()
        url = f"/api/inbox/webhooks/{ch.id}/"
        bad = self.api.post(url, self.update(1), format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="wrong")
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(WebhookEvent.objects.filter(signature_ok=False).count(), 1)
        self.assertEqual(Message.objects.count(), 0)
        no_header = self.api.post(url, self.update(1), format="json")
        self.assertEqual(no_header.status_code, 401)
        ok = self.api.post(url, self.update(2), format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="whsecret")
        self.assertEqual(ok.status_code, 200)
        dup = self.api.post(url, self.update(2), format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="whsecret")
        self.assertTrue(dup.json().get("duplicate"))
        # тот же update под другим update_id, но тем же message_id → одно сообщение
        self.api.post(url, self.update(3), format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="whsecret")
        self.assertEqual(Message.objects.filter(direction="in").count(), 1)
        self.assertEqual(Conversation.objects.get().status, "open")
        ev = WebhookEvent.objects.get(provider_event_id="2")
        self.assertIsNotNone(ev.processed_at)
        # группа в ящик не попадает
        grp = self.update(4, chat=-100)
        grp["message"]["chat"]["type"] = "supergroup"
        self.api.post(url, grp, format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="whsecret")
        self.assertEqual(ChannelContact.objects.count(), 1)
        h = self.api.get(f"{BASE}/webhooks/health/")
        self.assertEqual(h.status_code, 200)
        row = h.data["channels"][0]
        self.assertEqual(row["invalid_signatures_24h"], 2)
        self.assertIsNotNone(row["last_event_at"])

    def test_meta_verify_and_signature(self):
        import hashlib
        import hmac
        ch = self.mk_channel("whatsapp_cloud", "meta", token="")
        crypto.set_secrets(ch, {"app_secret": "appsec"})
        ch.save()
        url = f"/api/inbox/webhooks/{ch.id}/"
        r = self.api.get(url + "?hub.mode=subscribe&hub.verify_token=whsecret&hub.challenge=12345")
        self.assertEqual(r.content, b"12345")
        self.assertEqual(self.api.get(url + "?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=1").status_code, 403)
        body = b'{"entry": []}'
        sig = "sha256=" + hmac.new(b"appsec", body, hashlib.sha256).hexdigest()
        bad = self.api.generic("POST", url, body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256="sha256=00")
        self.assertEqual(bad.status_code, 401)
        good = self.api.generic("POST", url, body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig)
        self.assertEqual(good.status_code, 200)


class BotTests(Mixin, InboxBase):
    def setUp(self):
        super().setUp()
        self.bs = TelegramBotSettings.objects.create(company=self.company, bot_username="nbs_bot", webhook_ok=True,
                                                     owner_chat_id="999")
        self.bs.token = "1:tok"
        self.bs.save()

    def msg(self, text="/start", chat=555, mid=1, typ="private"):
        return {"message_id": mid, "date": int(timezone.now().timestamp()), "chat": {"id": chat, "type": typ},
                "from": {"id": chat, "first_name": "Асан"}, "text": text}

    def test_incoming_and_reply_recorded_pause(self):
        paused = bot_hooks.on_customer_message(self.bs, self.msg())
        self.assertFalse(paused)
        conv = Conversation.objects.get()
        self.assertEqual(conv.channel.kind, "telegram_bot")
        self.assertEqual(conv.channel.bot_settings_id, self.bs.id)
        self.assertEqual(conv.messages.filter(direction="in").count(), 1)
        # ответ бота через telegram_api.send_message (сеть замоканa)
        from apps.main.telegram_bot.services import telegram_api
        fake = mock.MagicMock()
        fake.__enter__.return_value.post.return_value.json.return_value = {"ok": True, "result": {"message_id": 7}}
        fake.__enter__.return_value.post.return_value.status_code = 200
        with mock.patch.object(telegram_api.httpx, "Client", return_value=fake), \
                mock.patch.object(telegram_api, "_wait_rate_limit"):
            telegram_api.send_message("1:tok", "555", "<b>Здравствуйте!</b> Чем помочь?")
            telegram_api.send_message("1:tok", "999", "Сводка владельцу")  # чат владельца — не в ящик
        out = list(conv.messages.filter(direction="out"))
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].sender, out[0].text, out[0].status), ("ai", "Здравствуйте! Чем помочь?", "sent"))
        self.assertEqual(Message.objects.count(), 2)
        # человек ответил → бот молчит
        services.create_outgoing(conv, self.owner, {"text": "Я на связи"})
        self.assertTrue(bot_hooks.on_customer_message(self.bs, self.msg("Вопрос", mid=2)))
        # группа заказов не попадает
        self.assertFalse(bot_hooks.on_customer_message(self.bs, self.msg(chat=-100, typ="supergroup")))
        self.assertEqual(ChannelContact.objects.count(), 1)

    def test_process_update_hook_pause_and_owner(self):
        from apps.main.telegram_bot import tasks as bot_tasks
        update = lambda uid, chat, text, mid: {"update_id": uid, "message": self.msg(text, chat, mid)}
        with mock.patch("apps.main.telegram_bot.services.customer_handler.handle_customer_message") as h:
            bot_tasks.process_telegram_update(str(self.bs.id), update(1, 555, "Привет", 1))
            self.assertEqual(h.call_count, 1)
            self.assertEqual(Message.objects.filter(direction="in").count(), 1)
            # человек ответил → следующий update не доходит до бота, но в ящике есть
            conv = Conversation.objects.get()
            services.create_outgoing(conv, self.owner, {"text": "Я тут"})
            bot_tasks.process_telegram_update(str(self.bs.id), update(2, 555, "Ещё", 2))
            self.assertEqual(h.call_count, 1)
            self.assertEqual(Message.objects.filter(direction="in").count(), 2)
        with mock.patch("apps.main.telegram_bot.services.owner_handler.handle_owner_message") as oh:
            bot_tasks.process_telegram_update(str(self.bs.id), update(3, 999, "команда", 3))
            self.assertEqual(oh.call_count, 1)
        self.assertEqual(ChannelContact.objects.count(), 1)  # чат владельца в ящик не попал


class PushAndAlertTests(Mixin, InboxBase):
    def test_push_throttle_text_and_recipients(self):
        ch = self.mk_channel()
        u = self.mk_user(can_view_clients=True)
        tokens = [{"user_id": u.id, "device_key": "d1", "platform": "android", "token": "T"}]
        with mock.patch.object(push, "get_device_tokens", return_value=tokens) as gt:
            m, _ = self.inbound(ch, mid="555:1", text="Позвоните мне +996 700 123 456 пожалуйста срочно очень нужно сегодня")
            conv = Conversation.objects.get()
            first = PushLog.objects.filter(conversation=conv).count()
            self.assertGreaterEqual(first, 1)  # владелец и сотрудник с доступом
            log = PushLog.objects.filter(user=u).get()
            self.assertEqual((log.provider, log.tokens), ("log", 1))
            self.assertLessEqual(len(log.body), 60)
            self.assertNotIn("700", log.body)
            self.inbound(ch, mid="555:2", text="второе")  # < 30 с — нового push нет
            self.assertEqual(PushLog.objects.filter(conversation=conv).count(), first)
            PushLog.objects.update(created_at=timezone.now() - timedelta(seconds=40))
            self.inbound(ch, mid="555:3", text="третье")
            self.assertGreater(PushLog.objects.filter(conversation=conv).count(), first)
        # назначенный диалог: только ответственному
        PushLog.objects.all().delete()
        Conversation.objects.filter(pk=conv.pk).update(assignee=u)
        with mock.patch.object(push, "get_device_tokens", return_value=[]):
            self.inbound(ch, mid="555:4", text="четвёртое")
        self.assertEqual(list(PushLog.objects.values_list("user_id", flat=True)), [u.id])

    def test_provider_choice_and_fcm_disabled(self):
        self.assertEqual(push.get_provider().name, "log")
        with self.settings(INBOX_PUSH_PROVIDER="fcm"):
            p = push.get_provider()
            self.assertEqual(p.name, "fcm")
            with mock.patch("requests.post") as rp:
                self.assertFalse(p.send([{"token": "t"}], "a", "b", {}))
                rp.assert_not_called()  # по умолчанию выключен, сети нет

    def test_devicepushtoken_optional(self):
        self.assertEqual(push.get_device_tokens([self.owner.id]), [])  # модели нет/пусто — не падает

    def test_owner_alert_once_per_hour(self):
        bs = TelegramBotSettings.objects.create(company=self.company, bot_username="b", owner_chat_id="999")
        bs.token = "1:tok"
        bs.save()
        ch = self.mk_channel()
        self.inbound(ch, mid="555:1")
        Conversation.objects.update(waiting_since=timezone.now() - timedelta(minutes=20))
        from apps.main.telegram_bot.services import telegram_api
        with mock.patch.object(telegram_api, "send_message", return_value={"ok": True}) as sm:
            self.assertEqual(tasks.alert_unanswered(), 1)
            self.assertEqual(tasks.alert_unanswered(), 0)  # не чаще раза в час
        self.assertEqual(sm.call_count, 1)
        self.assertIn("1 чат без ответа", sm.call_args[0][2])
        self.assertEqual(OwnerAlert.objects.count(), 1)
