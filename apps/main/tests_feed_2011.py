"""feed2011: sale_changed, customer_order_changed, plan_changed в ленте и потоке.

python manage.py test apps.main.tests_feed_2011 --settings=core.settings_test_sqlite
"""
import uuid
from datetime import timedelta
from types import SimpleNamespace

from django.utils import timezone

from apps.main.change_feed import serialize_event
from apps.main.event_names import feed_types_for, unified_name
from apps.main.events_stream import to_stream_event
from apps.main.models import ChangeEvent
from apps.main.tz26.cust_orders.tests import CustOrderBase, URL, key


class FeedOrderTests(CustOrderBase):
    def _types(self, t):
        return list(ChangeEvent.objects.filter(company=self.company, type=t).order_by("id"))

    def test_order_actions_emit_changed(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.create(qty="5", status="confirmed")
        self.assertEqual(r.status_code, 201, r.data)
        oid = r.data["id"]
        with self.captureOnCommitCallbacks(execute=True):
            p = self.api.patch(f"{URL}{oid}/", {"note": "x"}, format="json")
        self.assertEqual(p.status_code, 200, p.data)
        with self.captureOnCommitCallbacks(execute=True):
            rs = self.act(oid, "reserve")
        self.assertEqual(rs.status_code, 200, rs.data)
        with self.captureOnCommitCallbacks(execute=True):
            c = self.act(oid, "cancel")
        self.assertEqual(c.status_code, 200, c.data)
        evs = self._types("customer_order_changed")
        self.assertEqual([e.data["action"] for e in evs], ["created", "updated", "reserved", "canceled"])
        self.assertEqual(evs[-1].data["status"], "canceled")
        self.assertEqual(evs[0].entity_id, oid)
        msg = to_stream_event(serialize_event(evs[-1]))
        self.assertEqual((msg["type"], msg["order_id"], msg["action"], msg["ids"]), ("customer_order_changed", oid, "canceled", [oid]))

    def test_replay_does_not_duplicate(self):
        k = key()
        with self.captureOnCommitCallbacks(execute=True):
            self.create(qty="1", k=k)
            again = self.create(qty="1", k=k)
        self.assertTrue(again.data.get("replayed"))
        self.assertEqual(len(self._types("customer_order_changed")), 1)

    def test_sale_changed_from_return_edit(self):
        from apps.main.pos_return_edit import _feed_sale_changed

        sale = SimpleNamespace(company_id=self.company.pk, branch_id=None, pk=uuid.uuid4(), doc_number=7)
        r = SimpleNamespace(pk=uuid.uuid4(), version=2, shift_id=None)
        with self.captureOnCommitCallbacks(execute=True):
            _feed_sale_changed(r, sale, None, "return_voided")
        ev = self._types("sale_changed")[0]
        msg = to_stream_event(serialize_event(ev))
        self.assertEqual((msg["type"], msg["action"], msg["sale_id"]), ("sale_changed", "return_voided", str(sale.pk)))
        self.assertEqual(serialize_event(ev)["event"], "sale_changed")

    def test_plan_changed(self):
        from apps.users.models import Company, SubscriptionPlan

        mini = SubscriptionPlan.objects.create(name="Мини", code="mini", price=500)
        co = Company.objects.get(pk=self.company.pk)
        with self.captureOnCommitCallbacks(execute=True):
            co.save()  # без изменений — без события
        self.assertEqual(self._types("plan_changed"), [])
        with self.captureOnCommitCallbacks(execute=True):
            co.subscription_plan = mini
            co.end_date = timezone.now() + timedelta(days=30)
            co.save()
        ev = self._types("plan_changed")[0]
        self.assertEqual((ev.data["plan"], ev.data["plan_name"], ev.data["plan_switched"]), ("mini", "Мини", True))
        msg = to_stream_event(serialize_event(ev))
        self.assertEqual((msg["type"], msg["plan"]), ("plan_changed", "mini"))
        with self.captureOnCommitCallbacks(execute=True):
            co.end_date = co.end_date + timedelta(days=30)
            co.save()
        last = self._types("plan_changed")[-1]
        self.assertEqual((last.data["plan_switched"], last.data["prev_plan"]), (False, "mini"))


class FeedNamesTests(CustOrderBase):
    def test_names_and_filters(self):
        self.assertIn("sale_changed", feed_types_for("sale_changed"))
        self.assertIn("sale_returned", feed_types_for("sale_changed"))
        self.assertEqual(unified_name("customer_order_changed"), "customer_order_changed")
        m = to_stream_event({"id": 1, "type": "sale_paid", "at": "", "data": {"sale": "s1"}})
        self.assertEqual((m["type"], m["action"]), ("sale_created", "created"))
        m = to_stream_event({"id": 2, "type": "sale_returned", "at": "", "data": {"sale": "s1", "return": "r1"}})
        self.assertEqual((m["type"], m["action"], m["return_id"]), ("sale_changed", "returned", "r1"))
