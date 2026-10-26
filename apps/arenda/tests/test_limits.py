"""Границы ввода (найдено стресс-тестом): огромные суммы не должны давать 500; настройки с разумными пределами;
уволенный сотрудник остаётся в ведомости за месяцы работы."""
from datetime import date

from apps.arenda.constants import MAX_MONEY

from .base import ArendaBase

A = "/api/arenda/"
HUGE = 10 ** 25


class LimitsTests(ArendaBase):
    def test_huge_money_is_400_not_500(self):
        t = self.tenant()
        for body in (
            {"branch": str(self.branch.id), "floor": 1, "number": "H1", "area": "1", "rate": HUGE},
        ):
            self.assertEqual(self.client.post(A + "units/", body, format="json").status_code, 400)
        self.assertEqual(self.client.post(A + "payments/", {"tenant": t["id"], "amount": HUGE, "method": "cash"},
                                          format="json").status_code, 400)
        self.assertEqual(self.client.post(A + "payments/", {"tenant": t["id"], "amount": MAX_MONEY + 1,
                                                            "method": "cash"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(A + f"tenants/{t['id']}/remarks/",
                                          {"type": "fine", "text": "x", "fine_amount": HUGE}, format="json").status_code, 400)
        cat = self.client.get(A + "expense-categories/").data[0]["id"]
        self.assertEqual(self.client.post(A + "expenses/", {"branch": str(self.branch.id), "category": cat,
                                                            "date": "2026-01-01", "amount": HUGE, "method": "cash"},
                                          format="json").status_code, 400)
        self.assertEqual(self.client.post(A + "employees/", {"branch": str(self.branch.id), "full_name": "A",
                                                             "salary": HUGE}, format="json").status_code, 400)

    def test_max_money_still_accepted(self):
        t = self.tenant()
        r = self.client.post(A + "payments/", {"tenant": t["id"], "amount": MAX_MONEY, "method": "cash",
                                                           "branch": str(self.branch.id)}, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_settings_ranges(self):
        for body in ({"penalty_rate": "-1"}, {"advance_percent": 150}, {"advance_day": 45}, {"settle_day": 99},
                     {"payroll_social_employee": "200"}, {"payroll_income_tax": "-5"}, {"email": "nomail"},
                     {"lease_expiring_days": 100000}):
            r = self.client.patch(A + "settings/", body, format="json")
            self.assertEqual(r.status_code, 400, body)
        r = self.client.patch(A + "settings/", {"payroll_social_employee": "10.00", "payroll_income_tax": "10.00",
                                                "payroll_social_employer": "17.25", "advance_percent": 40,
                                                "settle_day": 0, "email": "a@b.kg"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_fired_before_hired_rejected(self):
        r = self.client.post(A + "employees/", {"branch": str(self.branch.id), "full_name": "A", "salary": 1000,
                                                "hired_at": "2026-01-01", "fired_at": "2020-01-01"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_archived_employee_stays_in_past_payroll(self):
        r = self.client.post(A + "employees/", {"branch": str(self.branch.id), "full_name": "Уволенный",
                                                "salary": 5_000_000, "hired_at": "2026-01-01"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        eid = r.data["id"]
        self.assertEqual(self.client.delete(A + f"employees/{eid}/").status_code, 204)  # fired_at = сегодня
        from apps.arenda.models import Employee
        Employee.objects.filter(pk=eid).update(fired_at=date(2026, 5, 20))
        may = self.client.get(A + "payroll/?month=2026-05").data
        self.assertEqual([x["full_name"] for x in may["lines"]], ["Уволенный"])
        june = self.client.get(A + "payroll/?month=2026-06").data
        self.assertEqual(june["lines"], [])


class UnitListQueriesTests(ArendaBase):
    def test_units_list_query_count_does_not_grow_with_units(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(12):
            self.unit(number=str(100 + i))
        with CaptureQueriesContext(connection) as q:
            r = self.client.get(A + "units/?page_size=50")
        self.assertEqual(r.data["count"], 12)
        self.assertLess(len(q), 20)  # раньше 1 + 5×N запросов: карта статусов пересчитывалась на каждое помещение


class LeaseUnitEdgeTests(ArendaBase):
    def test_terminate_before_start_rejected(self):
        t, u = self.tenant(), self.unit("T1")
        r = self.lease(t, [u], start="2026-10-01")
        self.assertEqual(r.status_code, 201, r.data)
        bad = self.client.post(A + f"leases/{r.data['id']}/terminate/", {"date": "2026-09-01"}, format="json")
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post(A + f"leases/{r.data['id']}/terminate/", {"date": "2026-10-15"}, format="json")
        self.assertEqual(ok.status_code, 200)

    def test_unit_patch_response_has_fresh_reserved_status(self):
        from datetime import timedelta
        from django.utils import timezone
        u = self.unit("R1")
        until = (timezone.localdate() + timedelta(days=5)).isoformat()
        r = self.client.patch(A + f"units/{u['id']}/", {"reserved_for": "Иван", "reserved_until": until}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "reserved")


class BranchLimitedStaffTests(ArendaBase):
    """Сотрудник с доступом к одному филиалу не видит арендаторов, акты и напоминания чужого филиала."""

    def test_foreign_branch_tenant_is_hidden(self):
        from apps.arenda.models import ArendaAccess

        t_in = self.client.post(A + "tenants/", {"name": "Свой", "branch": str(self.branch.id)}, format="json").data
        t_out = self.client.post(A + "tenants/", {"name": "Чужой", "branch": str(self.branch2.id)}, format="json").data
        act = self.client.post(A + f"tenants/{t_out['id']}/acts/", {"date_from": "2026-01-01", "date_to": "2026-01-31"},
                               format="json")
        self.assertEqual(act.status_code, 201, act.data)
        u, cl = self.staff("lim@a.kg")
        acc = ArendaAccess.objects.create(user=u, company=self.company, sections=["tenants", "cash", "notifications"],
                                          all_branches=False)
        acc.branches.set([self.branch])
        for path in (f"tenants/{t_out['id']}/", f"tenants/{t_out['id']}/card/", f"tenants/{t_out['id']}/acts/",
                     f"acts/{act.data['id']}/", f"acts/{act.data['id']}/print/", f"tenants/{t_out['id']}/remarks/"):
            self.assertEqual(cl.get(A + path).status_code, 404, path)
        self.assertEqual(cl.get(A + f"tenants/{t_in['id']}/").status_code, 200)
        self.assertEqual(cl.get(A + "acts/").data["count"], 0)
        self.assertEqual(cl.post(A + "notifications/remind/", {"tenant_ids": [t_out["id"]], "channel": "sms"},
                                 format="json").status_code, 404)
        self.assertEqual(cl.get(A + "reminders/?tenant=not-a-uuid").status_code, 400)
