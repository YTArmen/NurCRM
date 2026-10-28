"""Б-80: карточка клиента, заказ из диалога, файл в чат."""
import tempfile
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from django.core.files.base import ContentFile

from apps.inbox import actions, services, tasks, telegram
from apps.inbox.models import Conversation, Message
from apps.inbox.tests import BASE, InboxBase
from apps.inbox.tests_core import Mixin
from apps.main.models import Client, MainMedia, Product, Sale, ShowcaseOrder


class ActionsBase(Mixin, InboxBase):
    def setUp(self):
        super().setUp()
        self.ch = self.mk_channel()
        self.inbound(self.ch, text="Хочу кролика")
        self.conv = Conversation.objects.get()
        self.client_obj = Client.objects.create(company=self.company, full_name="Асан", phone="0700111222")
        self.conv.contact.client = self.client_obj
        self.conv.contact.save()
        self.p = Product.objects.create(company=self.company, name="Кола", price=Decimal("120"), quantity=Decimal("10"))


class ChatCardTests(ActionsBase):
    def test_card_debt_and_sales(self):
        Sale.objects.create(company=self.company, client=self.client_obj, status="debt", total=Decimal("500"),
                            cash_received=Decimal("100"))
        r = self.api.get(f"{BASE}/clients/{self.client_obj.id}/chat-card/")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual(Decimal(d["debt"]["amount"]), Decimal("400"))
        self.assertEqual(len(d["last_sales"]), 1)
        self.assertEqual(d["documents"], [])
        self.assertIn("bonus_balance", d)

    def test_card_needs_clients_permission(self):
        u = self.mk_user(can_view_clients=False)
        r = self.client_for(u).get(f"{BASE}/clients/{self.client_obj.id}/chat-card/")
        self.assertEqual(r.status_code, 403)


class OrderTests(ActionsBase):
    def test_order_from_conversation_with_note_and_replay(self):
        body = {"items": [{"product": str(self.p.id), "qty": 2}]}
        hdr = {"HTTP_IDEMPOTENCY_KEY": "order-key-0000000001"}
        r = self.api.post(f"{BASE}/conversations/{self.conv.id}/order/", body, format="json", **hdr)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["conversation_id"], self.conv.id)
        order = ShowcaseOrder.objects.get()
        self.assertEqual(order.source, "telegram_bot")
        self.assertEqual(order.total, Decimal("240.00"))
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.order_id, order.id)
        self.assertTrue(Message.objects.filter(conversation=self.conv, sender="system", text=f"Заказ №{order.number} создан").exists())
        r2 = self.api.post(f"{BASE}/conversations/{self.conv.id}/order/", body, format="json", **hdr)
        self.assertEqual(ShowcaseOrder.objects.count(), 1)
        self.assertEqual(r2.status_code, 201)

    def test_public_order_hook_requires_phone_match(self):
        self.conv.contact.phone = "+996700111222"
        self.conv.contact.save()
        o = ShowcaseOrder.objects.create(company=self.company, number=1, customer_name="x", customer_phone="0700111222")
        self.assertIsNotNone(actions.link_public_order(o, {"conversation_id": str(self.conv.id)}))
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.order_id, o.id)
        o2 = ShowcaseOrder.objects.create(company=self.company, number=2, customer_name="x", customer_phone="0555999888")
        self.assertIsNone(actions.link_public_order(o2, {"conversation_id": str(self.conv.id)}))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class FileTests(ActionsBase):
    def test_send_file_to_telegram(self):
        m = MainMedia.objects.create(company=self.company, size=3, content_type="application/pdf")
        m.file.save("kp.pdf", ContentFile(b"%PDF"), save=True)
        with mock.patch.object(services, "enqueue_send"), self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(f"{BASE}/conversations/{self.conv.id}/messages/",
                              {"kind": "file", "media_id": str(m.id), "text": "КП"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        msg = Message.objects.get(kind="file")
        self.assertEqual(msg.attachments[0]["media_id"], str(m.id))
        with mock.patch.object(tasks, "_chat_rate_wait", return_value=0), \
                mock.patch.object(telegram, "send_document", return_value={"message_id": 7}) as sd:
            tasks.send_outbound(msg.id)
        sd.assert_called_once()
        msg.refresh_from_db()
        self.assertEqual(msg.status, "sent")

    def test_foreign_media_rejected(self):
        r = self.api.post(f"{BASE}/conversations/{self.conv.id}/messages/",
                          {"kind": "file", "media_id": "00000000-0000-0000-0000-000000000000"}, format="json")
        self.assertEqual(r.status_code, 404)
