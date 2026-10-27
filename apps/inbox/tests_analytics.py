"""Б-84: аналитика на подменных данных (10 диалогов, известные времена)."""
from datetime import datetime, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.inbox import analytics
from apps.inbox.models import AiRun, Channel, ChannelContact, Conversation, Message
from apps.inbox.tests import BASE, InboxBase
from apps.main.models import Sale, ShowcaseOrder


def at(h, m=0, day=20):
    return timezone.make_aware(datetime(2026, 10, day, h, m))


class AnalyticsTests(InboxBase):
    def setUp(self):
        super().setUp()
        self.ch = Channel.objects.create(company=self.company, kind="telegram_bot", provider="telegram", name="TG",
                                         status="active", settings={"working_hours": {"from": "09:00", "to": "18:00"}})
        self.staff = self.mk_user(can_view_clients=True)
        self.convs = []
        for i in range(10):
            contact = ChannelContact.objects.create(channel=self.ch, company=self.company, external_id=str(i))
            conv = Conversation.objects.create(company=self.company, channel=self.ch, contact=contact,
                                               assignee=self.owner if i < 6 else self.staff)
            Conversation.objects.filter(pk=conv.pk).update(created_at=at(10))
            self.msg(conv, "in", "contact", at(10))
            self.msg(conv, "out", "user", at(10, i + 1), user=conv.assignee)  # ответ через i+1 минут
            self.convs.append(conv)

    def msg(self, conv, direction, sender, when, user=None):
        m = Message.objects.create(conversation=conv, channel=self.ch, company=self.company, direction=direction,
                                   sender=sender, sender_user=user, text="x", status="sent")
        Message.objects.filter(pk=m.pk).update(created_at=when)
        return m

    def get(self, client=None, **params):
        qs = "&".join(f"{k}={v}" for k, v in {"date_from": "2026-10-20", "date_to": "2026-10-20", **params}.items())
        return (client or self.api).get(f"{BASE}/analytics/?{qs}")

    def test_median_p90_match_manual(self):
        d = self.get().json()
        self.assertEqual(d["first_response"], {"median_s": 330, "p90_s": 540})  # 60…600 с
        self.assertEqual(d["conversations"]["new"], 10)
        self.assertEqual(d["by_hour"][10], 10)
        self.assertEqual(sum(d["by_hour"]), 10)

    def test_closed_reasons_conversion_revenue_ai(self):
        for conv, reason in zip(self.convs[:6], ["sold", "sold", "sold", "no_answer", "no_answer", "other"]):
            Conversation.objects.filter(pk=conv.pk).update(status="closed", closed_reason=reason, closed_at=at(12))
        sale = Sale.objects.create(company=self.company, status="paid", total=Decimal("700"))
        order = ShowcaseOrder.objects.create(company=self.company, number=1, customer_name="a", customer_phone="1", sale=sale)
        Conversation.objects.filter(pk=self.convs[0].pk).update(order=order)
        AiRun.objects.create(conversation=self.convs[0], channel=self.ch, company=self.company, outcome="answered", sent_by="auto")
        AiRun.objects.create(conversation=self.convs[1], channel=self.ch, company=self.company, outcome="answered", sent_by="user")
        AiRun.objects.create(conversation=self.convs[2], channel=self.ch, company=self.company, outcome="handoff")
        AiRun.objects.update(created_at=at(11))
        d = self.get().json()
        self.assertEqual(d["conversations"], {"new": 10, "closed": 6, "sold": 3, "no_answer": 2, "lost": 1})
        self.assertEqual(d["conversion"]["orders"], 1)
        self.assertEqual(d["conversion"]["to_sale"], 0.1)
        self.assertEqual(d["revenue_from_chats"], "700.00")
        self.assertEqual(d["ai"], {"answered": 1, "drafts_sent": 1, "handed_off": 1})
        self.assertEqual(d["waiting_now"], 0)
        mine = {r["user"]["id"]: r for r in d["by_user"]}
        self.assertEqual(mine[str(self.owner.id)]["conversations"], 6)
        self.assertEqual(mine[str(self.owner.id)]["sold"], 3)
        self.assertEqual(d["by_channel"][0]["conversations"], 10)

    def test_filters(self):
        d = self.get(user=self.staff.id).json()
        self.assertEqual(d["conversations"]["new"], 4)
        d = self.get(channel="00000000-0000-0000-0000-000000000000").json()
        self.assertEqual(d["conversations"]["new"], 0)
        self.assertIsNone(d["first_response"]["median_s"])

    def test_staff_sees_only_own(self):
        d = self.get(client=self.client_for(self.staff)).json()
        self.assertEqual(d["scope"], "own")
        self.assertEqual(d["conversations"]["new"], 4)

    def test_business_hours_only(self):
        hours = {"from": "09:00", "to": "18:00"}
        # 17:50 → 09:10 следующего дня = 10 + 10 минут рабочего времени
        self.assertEqual(analytics.business_seconds(at(17, 50), at(9, 10, day=21), hours), 1200)
        self.assertEqual(analytics.business_seconds(at(17, 50), at(9, 10, day=21), {}), (at(9, 10, day=21) - at(17, 50)).total_seconds())
        weekdays = {"tue": ["09:00", "18:00"]}  # остальные дни — выходные
        self.assertEqual(analytics.business_seconds(at(17, 50), at(9, 10, day=21), weekdays), 600)
        self.assertEqual(analytics.business_seconds(at(7), at(8), hours), 0)
