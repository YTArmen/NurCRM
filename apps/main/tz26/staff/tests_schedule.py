"""Б-64: плановый график и табель."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.construction.models import Cashbox, CashShift
from apps.main.tz26.staff.tests import client_for, make_company

SCH = "/main/staff-schedule/"
TS = "/main/staff/timesheet/"


class ScheduleTests(TestCase):
    def setUp(self):
        self.owner, self.company, self.cashier = make_company()
        self.box = Cashbox.objects.create(name="К", role=Cashbox.CashboxRole.POS_MAIN, company=self.company)
        self.api = client_for(self.owner)
        self.day = timezone.localdate() - timedelta(days=2)

    def _plan(self, **o):
        d = {"employee": str(self.cashier.id), "date": self.day.isoformat(), "start": "09:00", "end": "18:00"}
        d.update(o)
        return self.api.post(SCH, d, format="json")

    def _shift(self, h, m, hours=8, rev="100", night=False):
        tz = timezone.get_current_timezone()
        opened = timezone.make_aware(datetime.combine(self.day, time(h, m)), tz)
        s = CashShift.objects.create(company=self.company, cashbox=self.box, cashier=self.cashier,
                                     opening_cash=Decimal("0"), status="closed")
        CashShift.objects.filter(pk=s.pk).update(opened_at=opened, closed_at=opened + timedelta(hours=hours),
                                                 sales_total=Decimal(rev))

    def test_late_and_worked(self):
        key = {"HTTP_IDEMPOTENCY_KEY": "s" * 32}
        r1 = self._plan(**{})
        self.assertEqual(r1.status_code, 201, r1.data)
        self._shift(9, 12)
        r = self.api.get(f"{TS}?employee={self.cashier.id}&date_from={self.day}&date_to={self.day}")
        day = r.data["results"][0]["days"][0]
        self.assertEqual((day["late_minutes"], day["worked_minutes"], day["revenue"]), (12, 480, "100.00"))
        self.assertEqual((day["planned_start"], day["planned_end"]), ("09:00", "18:00"))
        self.assertEqual(r.data["results"][0]["totals"]["late_minutes"], 12)

    def test_overnight_counts_open_day(self):
        self._plan(start="22:00", end="06:00")
        self._shift(22, 5, hours=8)  # закрылась на следующий день
        r = self.api.get(f"{TS}?date_from={self.day}&date_to={self.day}")
        day = r.data["results"][0]["days"][0]
        self.assertEqual((day["late_minutes"], day["planned_minutes"]), (5, 480))

    def test_idempotency_and_rights(self):
        key = {"HTTP_IDEMPOTENCY_KEY": "t" * 32}
        body = {"employee": str(self.cashier.id), "date": self.day.isoformat(), "start": "09:00", "end": "18:00"}
        a = self.api.post(SCH, body, format="json", **key)
        b = self.api.post(SCH, body, format="json", **key)
        self.assertTrue(b.data["replayed"])
        self.assertEqual(len(self.api.get(SCH).data), 1)
        cash = client_for(self.cashier)
        self.assertEqual(cash.post(SCH, body, format="json").status_code, 403)
        self.assertEqual(len(cash.get(SCH).data), 1)
        self.assertEqual(cash.get(f"{TS}?employee={self.owner.id}").status_code, 403)
        self.assertEqual(cash.get(TS).status_code, 200)
        self.assertEqual(self.api.delete(f"{SCH}{a.data['id']}/").status_code, 204)
        self.assertEqual(self._plan(start="xx").data["code"], "schedule_invalid")
