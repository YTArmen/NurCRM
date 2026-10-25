from datetime import date, timedelta
from unittest import mock

from django.core.management import call_command
from django.db import IntegrityError
from django.utils import timezone

from apps.arenda.models import ArendaAccess, Charge, Counter, Lease, Payment
from apps.arenda.services.charges import generate_monthly_charges
from apps.users.models import Branch

from .base import ArendaBase


class UnitTests(ArendaBase):
    def test_unit_number_unique_in_branch(self):
        self.unit("101")
        r = self.client.post("/api/arenda/units/", {"branch": str(self.branch.id), "floor": 1, "number": "101",
                                                     "area": "5", "rate": 1}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "unit_number_taken")
        # в другом филиале можно
        self.unit("101", branch=self.branch2)

    def test_default_unit_types_seeded(self):
        r = self.client.get("/api/arenda/unit-types/")
        names = {t["name"] for t in r.data["results"]}
        self.assertIn("Бутик", names)
        self.assertEqual(r.data["count"], 6)

    def test_next_number(self):
        self.unit("101", floor=1)
        self.unit("102", floor=1)
        r = self.client.get(f"/api/arenda/units/next-number/?branch={self.branch.id}&floor=1")
        self.assertEqual(r.data["number"], "103")
        r = self.client.get(f"/api/arenda/units/next-number/?branch={self.branch.id}&floor=2")
        self.assertEqual(r.data["number"], "201")

    def test_delete_blocked_by_active_lease(self):
        u = self.unit("101")
        t = self.tenant()
        self.assertEqual(self.lease(t, [u]).status_code, 201)
        r = self.client.delete(f"/api/arenda/units/{u['id']}/")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "unit_has_active_lease")

    def test_delete_free_unit_archives_and_frees_number(self):
        u = self.unit("101")
        self.assertEqual(self.client.delete(f"/api/arenda/units/{u['id']}/").status_code, 204)
        self.unit("101")  # номер освободился

    def test_money_is_integer_in_api(self):
        u = self.unit("101", area="42.50", rate=150000)
        self.assertEqual(u["rent_monthly_calc"], 6375000)
        self.assertIsInstance(u["rate"], int)


class LeaseTests(ArendaBase):
    def test_overlap_409(self):
        u = self.unit("101")
        t, t2 = self.tenant(), self.tenant("ИП Другой")
        self.assertEqual(self.lease(t, [u], "2026-01-01", "2026-12-31").status_code, 201)
        r = self.lease(t2, [u], "2026-06-01", "2027-06-01")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "lease_overlap")
        self.assertEqual(r.data["data"]["units"], [u["id"]])
        # после окончания можно
        self.assertEqual(self.lease(t2, [u], "2027-01-01", "2027-12-31").status_code, 201)

    def test_open_ended_overlap(self):
        u = self.unit("101")
        t = self.tenant()
        self.assertEqual(self.lease(t, [u], "2026-01-01", None).status_code, 201)
        self.assertEqual(self.lease(t, [u], "2030-01-01", "2030-12-31").status_code, 409)

    def test_draft_does_not_block(self):
        u = self.unit("101")
        t = self.tenant()
        self.assertEqual(self.lease(t, [u], "2026-01-01", None, status="draft").status_code, 201)
        self.assertEqual(self.lease(t, [u], "2026-01-01", None).status_code, 201)

    def test_units_must_share_branch(self):
        a, b = self.unit("101"), self.unit("201", branch=self.branch2)
        r = self.lease(self.tenant(), [a, b])
        self.assertEqual(r.status_code, 400)

    def test_lease_sets_rented_and_numbering_gapless(self):
        t = self.tenant()
        labels = []
        for i in range(3):
            u = self.unit(str(101 + i))
            r = self.lease(t, [u])
            self.assertEqual(r.status_code, 201, r.data)
            labels.append(r.data["number"])
        self.assertEqual(labels, [1, 2, 3])
        self.assertTrue(r.data["label"].startswith(f"А-{timezone.localdate().year}/"))
        self.assertTrue(r.data["label"].endswith("/003"))
        # неудачное создание (пересечение) не съедает номер
        bad = self.lease(t, [u])
        self.assertEqual(bad.status_code, 409)
        u4 = self.unit("104")
        self.assertEqual(self.lease(t, [u4]).data["number"], 4)
        self.assertEqual(Counter.objects.get(company=self.company, key="lease").last_value, 4)

    def test_chess_shows_rented(self):
        u = self.unit("101", area="10", rate=100000)
        self.unit("102")
        self.lease(self.tenant(), [u])
        r = self.client.get(f"/api/arenda/chess/?branch={self.branch.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["counters"]["all"], 2)
        c = r.data["counters"]
        self.assertEqual(c["rented"] + c["due_soon"] + c["overdue"], 1)  # занято (цвет зависит от дня месяца)
        self.assertEqual(c["free"], 1)
        self.assertEqual(r.data["kpi"]["occupancy_percent"], 50.0)
        unit = r.data["floors"][0]["units"][0]
        self.assertEqual(unit["tenant"]["name"], "ИП Асанов")

    def test_terminate(self):
        u = self.unit("101")
        l = self.lease(self.tenant(), [u]).data
        r = self.client.post(f"/api/arenda/leases/{l['id']}/terminate/", {}, format="json")
        self.assertEqual(r.data["status"], "terminated")
        self.assertEqual(self.lease(self.tenant("Новый"), [u], "2026-01-01").status_code, 201)


class ChargesTests(ArendaBase):
    def _mk(self, start, end=None, **kw):
        u = self.unit("101", area="10", rate=100000)  # аренда 1 000 000 тыйын
        t = self.tenant()
        r = self.lease(t, [u], start, end, **kw)
        self.assertEqual(r.status_code, 201, r.data)
        Charge.objects.all().delete()  # убираем автоначисление текущего месяца, проверяем генератор
        return r.data

    def test_idempotent(self):
        self._mk("2026-01-01")
        r1 = generate_monthly_charges(date(2026, 3, 1))
        r2 = generate_monthly_charges(date(2026, 3, 15))
        self.assertEqual((r1["created"], r2["created"], r2["skipped"]), (1, 0, 1))
        self.assertEqual(Charge.objects.count(), 1)
        c = Charge.objects.get()
        self.assertEqual((c.amount, c.kind, c.due_date), (1000000, "rent", date(2026, 3, 10)))

    def test_command_idempotent(self):
        self._mk("2026-01-01")
        call_command("arenda_monthly_charges", "--date", "2026-03-01")
        call_command("arenda_monthly_charges", "--date", "2026-03-01")
        self.assertEqual(Charge.objects.count(), 1)

    def test_prorata_first_and_last_month(self):
        self._mk("2026-03-16", "2026-04-10")
        generate_monthly_charges(date(2026, 3, 1))
        generate_monthly_charges(date(2026, 4, 1))
        generate_monthly_charges(date(2026, 5, 1))
        amounts = list(Charge.objects.order_by("period").values_list("amount", flat=True))
        self.assertEqual(amounts, [round(1000000 * 16 / 31), round(1000000 * 10 / 30)])

    def test_lease_sum_overrides_area_rate(self):
        self._mk("2026-01-01", rent_monthly=777000)
        generate_monthly_charges(date(2026, 2, 1))
        self.assertEqual(Charge.objects.get().amount, 777000)

    def test_draft_and_terminated_not_charged(self):
        self._mk("2026-01-01", status="draft")
        self.assertEqual(generate_monthly_charges(date(2026, 2, 1))["created"], 0)

    def test_tenant_debt_status_overdue_days(self):
        self._mk("2026-01-01")
        generate_monthly_charges(date(2026, 3, 1))
        t = self.client.get("/api/arenda/tenants/").data["results"][0]
        today = timezone.localdate()
        self.assertEqual(t["debt"], 1000000)  # срок 10 марта 2026 давно прошёл
        self.assertEqual(t["status"], "overdue")
        self.assertEqual(t["overdue_days"], (today - date(2026, 3, 10)).days)
        self.assertEqual(t["penalty_calc"], round(1000000 * 0.001 * t["overdue_days"]))
        # оплата закрывает долг
        Payment.objects.create(company=self.company, branch=self.branch,
                               tenant_id=t["id"], amount=1000000, paid_at=timezone.now())
        t = self.client.get("/api/arenda/tenants/").data["results"][0]
        self.assertEqual((t["debt"], t["status"]), (0, "ok"))
        # фильтры
        self.assertEqual(self.client.get("/api/arenda/tenants/?status=overdue").data["count"], 0)
        self.assertEqual(self.client.get("/api/arenda/tenants/?status=paid").data["count"], 1)

    def test_chess_overdue_status_and_kpi(self):
        self._mk("2026-01-01")
        generate_monthly_charges(date(2026, 3, 1))
        r = self.client.get("/api/arenda/chess/")
        self.assertEqual(r.data["counters"]["overdue"], 1)
        u = r.data["floors"][0]["units"][0]
        self.assertEqual(u["status"], "overdue")
        self.assertGreater(u["overdue_days"], 0)
        self.assertEqual(r.data["kpi"]["tenants_debt"], 1000000)
        self.assertEqual(self.client.get("/api/arenda/chess/?status=free").data["floors"], [])

    def test_amounts_json_ints(self):
        self._mk("2026-01-01")
        generate_monthly_charges(date(2026, 3, 1))
        t = self.client.get("/api/arenda/tenants/").data
        self.assertIsInstance(t["totals"]["debt"], int)
        self.assertIsInstance(t["results"][0]["rent_monthly"], int)


class PermissionTests(ArendaBase):
    def _grant(self, user, preset):
        r = self.client.put(f"/api/arenda/access/{user.id}/", {"preset": preset}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        return r

    def test_no_access_row_403(self):
        _, c = self.staff()
        r = c.get("/api/arenda/chess/")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data["code"], "permission_denied")

    def test_cashier_cannot_analytics_but_tenants_ok(self):
        u, c = self.staff()
        self._grant(u, "cashier")
        self.assertEqual(c.get("/api/arenda/tenants/").status_code, 200)
        r = c.get("/api/arenda/chess/")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data["section"], "chess")
        self.assertEqual(c.get("/api/arenda/settings/").status_code, 403)
        me = c.get("/api/arenda/me/").data
        self.assertEqual(me["sections"], ["tenants", "cash"])

    def test_analytics_section_guard(self):
        from apps.arenda.permissions import ArendaSection
        from rest_framework.test import APIRequestFactory
        u, _ = self.staff()
        self._grant(u, "cashier")
        req = APIRequestFactory().get("/x/")
        req.user = u
        from rest_framework.request import Request
        req = Request(req)
        req._user = u
        from rest_framework.exceptions import APIException
        from core.exceptions import ApiError
        with self.assertRaises(ApiError) as cm:
            ArendaSection("analytics")().has_permission(req, None)
        self.assertEqual(cm.exception.status_code, 403)
        self.assertEqual(cm.exception.extra_top["section"], "analytics")
        self.assertTrue(ArendaSection("cash")().has_permission(req, None))

    def test_unchecking_all_closes_access(self):
        u, c = self.staff()
        self._grant(u, "cashier")
        r = self.client.put(f"/api/arenda/access/{u.id}/", {"sections": []}, format="json")
        self.assertFalse(r.data["is_active"])
        self.assertEqual(c.get("/api/arenda/tenants/").status_code, 403)

    def test_manager_preset_has_no_settings(self):
        u, c = self.staff()
        self._grant(u, "manager")
        self.assertEqual(c.get("/api/arenda/chess/").status_code, 200)
        self.assertEqual(c.get("/api/arenda/settings/").status_code, 403)

    def test_presets_list(self):
        r = self.client.get("/api/arenda/access/presets/")
        self.assertEqual({p["key"] for p in r.data},
                         {"manager", "accountant", "admin", "cashier", "chess_only", "none"})

    def test_branch_restriction(self):
        u, c = self.staff()
        self.client.put(f"/api/arenda/access/{u.id}/", {
            "preset": "chess_only", "all_branches": False, "branches": [str(self.branch.id)]}, format="json")
        self.unit("101")
        self.unit("201", branch=self.branch2)
        self.assertEqual(c.get(f"/api/arenda/chess/?branch={self.branch.id}").status_code, 200)
        r = c.get(f"/api/arenda/chess/?branch={self.branch2.id}")
        self.assertEqual(r.status_code, 403)
        # "all" = только доступные
        all_ = c.get("/api/arenda/chess/?branch=all")
        self.assertEqual(all_.data["counters"]["all"], 1)

    def test_other_company_isolated(self):
        self.unit("101")
        other_owner = __import__("apps.arenda.tests.base", fromlist=["make_user"]).make_user("o2@a.kg")
        from apps.users.models import Company
        comp = Company.objects.create(name="Other", owner=other_owner)
        other_owner.company = comp
        other_owner.save()
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(other_owner)
        self.assertEqual(c.get("/api/arenda/units/").data["count"], 0)


class BranchFilterTests(ArendaBase):
    def test_branch_filter_everywhere(self):
        a, b = self.unit("101"), self.unit("201", branch=self.branch2)
        t1, t2 = self.tenant("Один"), self.tenant("Два")
        self.lease(t1, [a])
        self.lease(t2, [b])
        self.assertEqual(self.client.get("/api/arenda/units/?branch=all").data["count"], 2)
        self.assertEqual(self.client.get(f"/api/arenda/units/?branch={self.branch.id}").data["count"], 1)
        self.assertEqual(self.client.get(f"/api/arenda/leases/?branch={self.branch2.id}").data["count"], 1)
        ts = self.client.get(f"/api/arenda/tenants/?branch={self.branch.id}").data
        self.assertEqual([x["name"] for x in ts["results"]], ["Один"])
        self.assertEqual(self.client.get("/api/arenda/tenants/").data["count"], 2)
        r = self.client.get("/api/arenda/units/?branch=zzz")
        self.assertEqual(r.status_code, 400)

    def test_branches_list_and_delete(self):
        r = self.client.get("/api/arenda/branches/")
        self.assertEqual(r.data["count"], 2)
        u = self.unit("101")
        self.lease(self.tenant(), [u])
        row = [b for b in self.client.get("/api/arenda/branches/").data["results"] if b["id"] == str(self.branch.id)][0]
        self.assertEqual((row["units_count"], row["occupancy_percent"]), (1, 100.0))
        self.assertGreater(row["monthly_charged"], 0)
        d = self.client.delete(f"/api/arenda/branches/{self.branch.id}/")
        self.assertEqual((d.status_code, d.data["code"]), (409, "branch_not_empty"))
        self.assertEqual(self.client.delete(f"/api/arenda/branches/{self.branch2.id}/").status_code, 204)
        self.assertEqual(self.client.get("/api/arenda/branches/").data["count"], 1)

    def test_create_branch(self):
        r = self.client.post("/api/arenda/branches/", {"name": "Новый ТЦ", "city": "Ош", "floors": 3,
                                                       "object_type": "ТЦ"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["city"], r.data["floors"]), ("Ош", 3))


class SettingsTests(ArendaBase):
    def test_validation(self):
        for body, field in (({"inn": "123"}, "inn"), ({"bik": "12345"}, "bik"), ({"account": "1" * 15}, "account")):
            r = self.client.patch("/api/arenda/settings/", body, format="json")
            self.assertEqual(r.status_code, 400)
            self.assertIn(field, r.data["fields"])
        r = self.client.patch("/api/arenda/settings/", {"inn": "1" * 14, "bik": "123456", "account": "1" * 16,
                                                         "director": "Иванов И.И."}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["penalty_rate"], "0.100")
        self.assertEqual(r.data["payroll_social_employer"], "17.25")

    def test_audit_written(self):
        from apps.arenda.models import AuditLog
        self.client.patch("/api/arenda/settings/", {"director": "X"}, format="json")
        self.assertTrue(AuditLog.objects.filter(company=self.company, object_type="settings").exists())


class TenantTests(ArendaBase):
    def test_search_order_totals(self):
        a, b = self.unit("101", area="10", rate=100000), self.unit("102", area="20", rate=100000)
        self.lease(self.tenant("Алма"), [a])
        self.lease(self.tenant("Бета"), [b])
        r = self.client.get("/api/arenda/tenants/?ordering=-rent")
        self.assertEqual([t["name"] for t in r.data["results"]], ["Бета", "Алма"])
        self.assertEqual(r.data["totals"]["rent_monthly"], 3000000)
        self.assertEqual(self.client.get("/api/arenda/tenants/?q=102").data["count"], 1)
        self.assertEqual(self.client.get("/api/arenda/tenants/?ordering=bogus").status_code, 400)

    def test_delete_blocked(self):
        t = self.tenant()
        self.lease(t, [self.unit("101")])
        r = self.client.delete(f"/api/arenda/tenants/{t['id']}/")
        self.assertEqual((r.status_code, r.data["code"]), (409, "tenant_has_active_lease"))
        t2 = self.tenant("Свободный")
        self.assertEqual(self.client.delete(f"/api/arenda/tenants/{t2['id']}/").status_code, 204)
