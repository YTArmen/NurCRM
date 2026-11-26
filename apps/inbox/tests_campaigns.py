"""Б-81: рассылки."""
from datetime import datetime
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from apps.inbox import campaigns, contacts, crypto, services
from apps.inbox.models import Campaign, CampaignRecipient, Channel, ChannelContact, Message, WaTemplate
from apps.inbox.tests import BASE, InboxBase
from apps.inbox.tests_core import Mixin
from apps.main.models import Client, Sale


def at(h, m=0, day=28):
    return timezone.make_aware(datetime(2026, 10, day, h, m))


class CampBase(Mixin, InboxBase):
    def setUp(self):
        super().setUp()
        self.ch = self.mk_channel()
        for chat, marketing in (("1", True), ("2", False), ("3", True)):
            self.inbound(self.ch, chat=chat, mid=f"{chat}:1")
        self.c1, self.c2, self.c3 = (ChannelContact.objects.get(channel=self.ch, external_id=x) for x in "123")
        contacts.set_consents(self.c1, service=True, marketing=True, source="customer")
        contacts.set_consents(self.c3, service=False, marketing=False, source="customer")  # /stop
        enq = mock.patch.object(services, "enqueue_send")
        enq.start()
        self.addCleanup(enq.stop)

    def mk(self, **kw):
        body = {"channel": str(self.ch.id), "kind": "promo", "text": "Акция, {имя}!", "name": "Акция"}
        body.update(kw)
        r = self.api.post(f"{BASE}/campaigns/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return Campaign.objects.get(pk=r.json()["id"])


class CampaignTests(CampBase):
    def test_unofficial_channel_forbidden(self):
        u = Channel.objects.create(company=self.company, kind="whatsapp_bsp", provider="unofficial", name="U",
                                   status="active", risk_accepted_at=timezone.now())
        r = self.api.post(f"{BASE}/campaigns/", {"channel": str(u.id), "text": "x"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "channel_not_allowed")

    def test_instagram_and_widget_not_allowed(self):
        ig = Channel.objects.create(company=self.company, kind="instagram", provider="meta", name="I", status="active")
        r = self.api.post(f"{BASE}/campaigns/", {"channel": str(ig.id), "text": "x"}, format="json")
        self.assertEqual(r.json()["code"], "channel_not_allowed")

    def test_preview_counts_consents_and_stop(self):
        c = self.mk()
        r = self.api.post(f"{BASE}/campaigns/{c.id}/preview/", {}, format="json").json()
        self.assertEqual(r["recipients"], 1)
        self.assertEqual(r["excluded"]["no_consent"], 1)
        self.assertEqual(r["excluded"]["stopped"], 1)
        self.assertEqual(r["targeted"], 3)
        # служебная рассылка идёт по service-согласию: c1 и c2 (c3 — СТОП)
        s = self.mk(kind="service")
        r = self.api.post(f"{BASE}/campaigns/{s.id}/preview/", {}, format="json").json()
        self.assertEqual((r["recipients"], r["excluded"]["stopped"]), (2, 1))
        d = self.api.post(f"{BASE}/campaigns/preview/", {"channel": str(self.ch.id), "text": "x"}, format="json")
        self.assertEqual(d.json()["recipients"], 1)

    def test_quiet_hours_22_goes_out_at_09(self):
        c = self.mk(scheduled_at=at(22).isoformat())
        self.assertEqual(self.api.post(f"{BASE}/campaigns/{c.id}/start/", {}, format="json").status_code, 200)
        self.assertEqual(campaigns.start_time(c, at(22, 5)), at(9, 0, day=29))
        self.assertEqual(campaigns.process(c, at(22, 30)), 0)
        self.assertEqual(Message.objects.filter(kind="text", sender="user").count(), 0)
        self.assertEqual(campaigns.process(c, at(8, 59, day=29)), 0)
        self.assertEqual(campaigns.process(c, at(9, 1, day=29)), 1)
        c.refresh_from_db()
        self.assertEqual(c.status, "done")
        m = Message.objects.get(sender="user", kind="text")
        self.assertEqual(m.text, "Акция, Асан!")  # имя контакта из ingest
        self.assertEqual(m.conversation.contact_id, self.c1.id)  # сообщение — в диалоге получателя

    def test_start_idempotent_and_20h_rule(self):
        c = self.mk(scheduled_at=at(12).isoformat())
        hdr = {"HTTP_IDEMPOTENCY_KEY": "camp-start-key-0001"}
        self.assertEqual(self.api.post(f"{BASE}/campaigns/{c.id}/start/", {}, format="json", **hdr).status_code, 200)
        self.assertEqual(self.api.post(f"{BASE}/campaigns/{c.id}/start/", {}, format="json", **hdr).status_code, 200)
        self.assertEqual(Campaign.objects.get(pk=c.id).status, "scheduled")
        self.assertEqual(campaigns.process(c, at(12, 1)), 1)
        # вторая рассылка тому же получателю через 3 часа — исключён (20 ч)
        c2 = self.mk(scheduled_at=at(15).isoformat())
        pv = campaigns.preview(c2, at(15))
        self.assertEqual(pv["recipients"], 0)
        self.assertEqual(pv["excluded"]["recent_20h"], 1)
        # через 21 час можно
        self.assertEqual(campaigns.preview(c2, at(9, day=29))["recipients"], 1)

    def test_stop_after_start_excluded_at_send(self):
        c = self.mk(scheduled_at=at(12).isoformat())
        self.api.post(f"{BASE}/campaigns/{c.id}/start/", {}, format="json")
        contacts.set_consents(self.c1, service=False, marketing=False, source="customer")
        self.assertEqual(campaigns.process(c, at(12, 1)), 0)
        self.assertEqual(CampaignRecipient.objects.get(contact=self.c1).skip_reason, "no_consent")

    def test_requires_can_broadcast(self):
        u = self.mk_user(can_view_clients=True)
        self.assertEqual(self.client_for(u).get(f"{BASE}/campaigns/").status_code, 403)


class DebtReminderTests(CampBase):
    def test_debt_reminder_whatsapp_template(self):
        wa = Channel.objects.create(company=self.company, kind="whatsapp_cloud", provider="meta", name="WA", status="active",
                                    external={"phone_number_id": "P1"}, health={"messaging_tier": "TIER_250"})
        t = WaTemplate.objects.create(channel=wa, company=self.company, name="debt_reminder", language="ru",
                                      category="utility", status="approved",
                                      components=[{"type": "BODY", "text": "{{1}}, долг {{2}} сом до {{3}}"}])
        cl = Client.objects.create(company=self.company, full_name="Асан Асанов", phone="0700111222")
        Sale.objects.create(company=self.company, client=cl, status="debt", total=Decimal("500"), cash_received=Decimal("100"))
        services.ingest_inbound(wa, "996700111222", message_external_id="w1", text="hi", phone="996700111222")
        contact = ChannelContact.objects.get(channel=wa)
        self.assertEqual(contact.client_id, cl.id)
        r = self.api.post(f"{BASE}/campaigns/", {"channel": str(wa.id), "kind": "debt_reminder", "template_id": t.id,
                                                  "scheduled_at": at(12).isoformat()}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        cid = r.json()["id"]
        self.assertEqual(r.json()["template_params"], campaigns.DEBT_PARAMS)
        self.assertEqual(self.api.post(f"{BASE}/campaigns/{cid}/start/", {}, format="json").status_code, 200)
        c = Campaign.objects.get(pk=cid)
        self.assertEqual(campaigns.process(c, at(12, 1)), 1)
        m = Message.objects.get(kind="template")
        self.assertEqual(m.template["params"][0], "Асан Асанов")
        self.assertTrue(m.template["params"][1].startswith("400"))
        self.assertEqual(m.template["category"], "utility")
        out = self.api.get(f"{BASE}/campaigns/{cid}/").json()
        self.assertEqual(out["stats"]["sent"], 1)
        self.assertEqual(out["stats"]["replied"], 0)
        # ответ покупателя → replied
        services.ingest_inbound(wa, "996700111222", message_external_id="w2", text="оплачу завтра")
        self.assertEqual(self.api.get(f"{BASE}/campaigns/{cid}/").json()["stats"]["replied"], 1)

    def test_marketing_template_required_for_promo(self):
        wa = Channel.objects.create(company=self.company, kind="whatsapp_cloud", provider="meta", name="WA", status="active")
        t = WaTemplate.objects.create(channel=wa, company=self.company, name="u", language="ru", category="utility",
                                      status="approved", components=[{"type": "BODY", "text": "x"}])
        r = self.api.post(f"{BASE}/campaigns/", {"channel": str(wa.id), "kind": "promo", "template_id": t.id}, format="json")
        self.assertEqual(r.status_code, 400)
