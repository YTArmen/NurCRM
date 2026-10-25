from datetime import date, timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.arenda.models import Charge, Reminder, ReminderRule

from .base import ArendaBase, orm_charge, orm_lease, orm_payment

D = date(2026, 10, 20)


class NotifyBase(ArendaBase):
    def scenario(self, today=None):
        today = today or timezone.localdate()
        self.today = today
        self.ua, self.ub = self.unit("101"), self.unit("102")
        self.ta, self.tb = self.tenant("ИП Просрочил"), self.tenant("ИП Скоро")
        self.la = orm_lease(self, self.ta, [self.ua], today - timedelta(days=90), today + timedelta(days=30), label="А-1")
        self.lb = orm_lease(self, self.tb, [self.ub], today - timedelta(days=90), None, label="А-2")
        orm_charge(self, self.ta, self.la, today - timedelta(days=5), 1_000_000)
        orm_charge(self, self.tb, self.lb, today + timedelta(days=3), 500_000)


class SummaryTests(NotifyBase):
    def test_summary_counts_and_penalty(self):
        self.scenario()
        r = self.client.get("/api/arenda/notifications/summary/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["count"], r.data["overdue_count"], r.data["due_soon_count"]), (2, 1, 1))
        deb = r.data["debtors"][0]
        self.assertEqual((deb["debt"], deb["overdue_days"]), (1_000_000, 5))
        self.assertEqual(deb["penalty_calc"], 5000)  # 1 000 000 * 0,1% * 5
        self.assertEqual(r.data["due_soon"][0]["amount"], 500_000)
        self.assertEqual(r.data["due_soon"][0]["days_left"], 3)
        self.assertEqual(r.data["expiring_count"], 1)
        self.assertEqual(r.data["expiring_leases"][0]["days_left"], 30)

    def test_branch_filter_and_section_403(self):
        self.scenario()
        r = self.client.get(f"/api/arenda/notifications/summary/?branch={self.branch2.id}")
        self.assertEqual(r.data["count"], 0)
        u, c = self.staff()
        self.assertEqual(c.get("/api/arenda/notifications/summary/").status_code, 403)

    def test_partial_payment_reduces_debt(self):
        self.scenario()
        orm_payment(self, self.ta, 400_000, self.today)
        r = self.client.get("/api/arenda/notifications/summary/")
        self.assertEqual(r.data["debtors"][0]["debt"], 600_000)


class RemindTests(NotifyBase):
    def test_remind_all_and_one(self):
        self.scenario()
        r = self.client.post("/api/arenda/notifications/remind/", {"all": True, "channel": "sms"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["count"], 1)  # только должники
        self.assertEqual(r.data["results"][0]["status"], "not_sent")
        self.assertIn("ИП Просрочил", r.data["results"][0]["text"])
        r = self.client.post("/api/arenda/notifications/remind/",
                             {"tenant_ids": [self.tb["id"]], "channel": "whatsapp"}, format="json")
        self.assertEqual(r.data["results"][0]["channel"], "whatsapp")
        h = self.client.get(f"/api/arenda/reminders/?tenant={self.ta['id']}")
        self.assertEqual(h.data["count"], 1)
        self.assertEqual(self.client.post("/api/arenda/notifications/remind/", {}, format="json").status_code, 400)

    @override_settings(ARENDA_TELEGRAM_BOT_TOKEN="T")
    def test_telegram_to_owner_mocked(self):
        self.scenario()
        self.client.get("/api/arenda/reminder-rules/")
        ReminderRule.objects.filter(company=self.company, kind="due_day").update(params={"telegram_chat_id": "42"})
        with mock.patch("apps.arenda.services.reminders._telegram_post", return_value=True) as m:
            r = self.client.post("/api/arenda/notifications/remind/", {"all": True, "channel": "telegram"}, format="json")
        self.assertEqual(r.data["results"][0]["status"], "sent")
        m.assert_called_once()
        self.assertEqual(m.call_args[0][:2], ("T", "42"))

    def test_telegram_not_configured_is_not_sent(self):
        self.scenario()
        with mock.patch("apps.arenda.services.reminders._telegram_post") as m:
            r = self.client.post("/api/arenda/notifications/remind/", {"all": True, "channel": "telegram"}, format="json")
        m.assert_not_called()
        self.assertEqual(r.data["results"][0]["status"], "not_sent")
        self.assertEqual(r.data["results"][0]["error"], "Telegram не настроен")


class RulesTests(NotifyBase):
    def test_five_rules_toggle(self):
        r = self.client.get("/api/arenda/reminder-rules/")
        self.assertEqual([x["kind"] for x in r.data], ["before_3_days", "due_day", "overdue_every_3_days",
                                                      "penalty_from_day_1", "lease_end_45_days"])
        self.assertFalse(any(x["enabled"] for x in r.data))
        r = self.client.put("/api/arenda/reminder-rules/", [{"kind": "due_day", "enabled": True}], format="json")
        self.assertEqual([x["kind"] for x in r.data if x["enabled"]], ["due_day"])
        self.assertEqual(self.client.put("/api/arenda/reminder-rules/", [{"kind": "zzz"}], format="json").status_code, 400)


class AutoRemindersTests(NotifyBase):
    def enable(self, *kinds):
        self.client.get("/api/arenda/reminder-rules/")
        ReminderRule.objects.filter(company=self.company, kind__in=kinds).update(enabled=True)

    def run_cmd(self, d=D):
        out = StringIO()
        call_command("arenda_auto_reminders", date=d.isoformat(), stdout=out)
        return out.getvalue()

    def test_idempotent_all_rules(self):
        self.scenario(D)
        self.enable("before_3_days", "due_day", "overdue_every_3_days", "penalty_from_day_1", "lease_end_45_days")
        # дополнительные арендаторы на «срок через 3 дня» и «срок сегодня»
        t3, t0 = self.tenant("ИП Три"), self.tenant("ИП Сегодня")
        l3 = orm_lease(self, t3, [self.unit("103")], D - timedelta(days=60), None, label="А-3")
        l0 = orm_lease(self, t0, [self.unit("104")], D - timedelta(days=60), D + timedelta(days=45), label="А-4")
        orm_charge(self, t3, l3, D + timedelta(days=3), 700_000)
        orm_charge(self, t0, l0, D, 300_000)
        # просрочка ровно 3 дня
        tx = self.tenant("ИП Три дня")
        lx = orm_lease(self, tx, [self.unit("105")], D - timedelta(days=60), None, label="А-5")
        orm_charge(self, tx, lx, D - timedelta(days=3), 1_000_000)
        self.run_cmd()
        n1, c1 = Reminder.objects.count(), Charge.objects.filter(kind="penalty").count()
        kinds = {r.rule_kind for r in Reminder.objects.all()}
        self.assertEqual(kinds, {"before_3_days", "due_day", "overdue_every_3_days", "lease_end_45_days"})
        # сумма по сроку «через 3 дня» и реквизиты в день оплаты
        self.assertTrue(Reminder.objects.filter(rule_kind="before_3_days", text__contains="7 000 с").exists())
        self.assertTrue(Reminder.objects.filter(rule_kind="due_day", text__contains="3 000 с").exists())
        # просрочка 3 дня: SMS арендатору + копия владельцу в Telegram
        self.assertEqual(Reminder.objects.filter(rule_kind="overdue_every_3_days", channel="sms").count(), 1)
        self.assertEqual(Reminder.objects.filter(rule_kind="overdue_every_3_days", channel="telegram").count(), 1)
        # просрочка 5 дней (ИП Просрочил) — не кратна 3, но пеня начислена всем просроченным
        self.assertEqual(c1, 2)
        p = Charge.objects.filter(kind="penalty", tenant_id=tx["id"]).get()
        self.assertEqual(p.amount, 1000)  # 1 000 000 * 0,1% * 1 день
        self.assertEqual(p.due_date, D)
        # повторный запуск и ещё раз — дублей нет
        self.run_cmd()
        self.run_cmd()
        self.assertEqual(Reminder.objects.count(), n1)
        self.assertEqual(Charge.objects.filter(kind="penalty").count(), c1)

    def test_penalty_only_if_rule_on_and_no_penalty_on_penalty(self):
        self.scenario(D)
        self.enable("due_day")
        self.run_cmd()
        self.assertEqual(Charge.objects.filter(kind="penalty").count(), 0)
        self.enable("penalty_from_day_1")
        self.run_cmd()
        ch = Charge.objects.get(kind="penalty")
        self.assertEqual(ch.amount, 1000)
        # на следующий день база — только аренда (пеня вчерашнего дня в базу не входит)
        self.run_cmd(D + timedelta(days=1))
        amounts = sorted(Charge.objects.filter(kind="penalty").values_list("amount", flat=True))
        self.assertEqual(amounts, [1000, 1000])

    def test_rules_off_does_nothing(self):
        self.scenario(D)
        self.run_cmd()
        self.assertEqual(Reminder.objects.count(), 0)
        self.assertEqual(Charge.objects.filter(kind="penalty").count(), 0)

    def test_lease_end_45_days_to_owner(self):
        self.scenario(D)
        self.enable("lease_end_45_days")
        t = self.tenant("ИП Конец")
        orm_lease(self, t, [self.unit("110")], D - timedelta(days=300), D + timedelta(days=45), label="А-9")
        self.run_cmd()
        self.run_cmd()
        qs = Reminder.objects.filter(rule_kind="lease_end_45_days")
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.get().channel, "telegram")
        self.assertIn("Владельцу", qs.get().text)

    def test_indexation_notice_30_days(self):
        d = date(2026, 11, 20)
        t = self.tenant("ИП Индекс")
        orm_lease(self, t, [self.unit("120")], date(2025, 12, 20), None, label="А-7", indexation_percent=5)
        self.run_cmd(d)
        self.run_cmd(d)
        qs = Reminder.objects.filter(rule_kind="indexation")
        self.assertEqual(qs.count(), 1)
        self.assertIn("5.00%", qs.get().text)
        self.run_cmd(d + timedelta(days=1))
        self.assertEqual(Reminder.objects.filter(rule_kind="indexation").count(), 1)
