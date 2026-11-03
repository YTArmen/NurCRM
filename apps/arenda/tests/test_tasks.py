from datetime import date

from django.conf import settings

from apps.arenda import tasks
from apps.arenda.models import Charge, Employee, Reminder, ReminderRule, TaxPayment
from apps.arenda.defaults import ensure_company_defaults

from .base import ArendaBase, orm_lease


class TasksTests(ArendaBase):
    def setUp(self):
        super().setUp()
        ensure_company_defaults(self.company)
        self.u = self.unit("101", area="10.00", rate=100000)
        self.t = self.tenant()
        self.lease_obj = orm_lease(self, self.t, [self.u], date(2026, 1, 1), pay_day=5)

    def test_beat_schedule_has_arenda(self):
        names = {v["task"] for v in settings.CELERY_BEAT_SCHEDULE.values()}
        self.assertIn("apps.arenda.tasks.monthly_charges", names)
        self.assertIn("apps.arenda.tasks.auto_reminders", names)
        self.assertIn("apps.arenda.tasks", settings.CELERY_IMPORTS)

    def test_monthly_charges_idempotent(self):
        r1 = tasks.monthly_charges("2026-03-10")
        self.assertEqual(r1["created"], 1)
        r2 = tasks.monthly_charges("2026-03-11")
        self.assertEqual(r2["created"], 0)
        self.assertEqual(Charge.objects.filter(kind="rent").count(), 1)

    def test_tax_calendar_filled_for_two_months(self):
        Employee.objects.create(company=self.company, branch=self.branch, full_name="Сотр", salary=3000000,
                                hired_at=date(2026, 1, 1))
        res = tasks.fill_tax_calendar(date(2026, 3, 10))
        self.assertEqual(res["failed"], 0)
        months = {t.dedupe_key.split(":")[1] for t in TaxPayment.objects.filter(source="payroll")}
        self.assertEqual(months, {"2026-03", "2026-04"})
        n = TaxPayment.objects.count()
        tasks.fill_tax_calendar(date(2026, 3, 11))
        self.assertEqual(TaxPayment.objects.count(), n)

    def test_auto_reminders_idempotent(self):
        ReminderRule.objects.filter(company=self.company, kind="due_day").update(enabled=True)
        tasks.monthly_charges("2026-03-05")
        r1 = tasks.auto_reminders("2026-03-05")
        self.assertEqual(r1["due_day"], 1)
        r2 = tasks.auto_reminders("2026-03-05")
        self.assertEqual(r2.get("due_day", 0), 0)
        self.assertEqual(Reminder.objects.filter(rule_kind="due_day").count(), 1)
