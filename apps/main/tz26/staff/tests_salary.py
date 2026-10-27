"""Б-63: штрафы, премии, авансы, сменная ставка, выписка."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.main.models import Branch, MarketSalaryPayout, MarketSaleEmployeePayProfile
from apps.main.tz26.staff.tests import client_for, make_company

URL = "/main/market-salary/payouts/"
ST = "/main/market-salary/statement/"
PERIOD = "period_start=2000-01-01&period_end=2100-01-01"


class SalaryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner, self.company, self.cashier = make_company()
        self.branch = Branch.objects.create(name="Main", company=self.company)
        self.pos = Cashbox.objects.create(name="Касса", role=Cashbox.CashboxRole.POS_MAIN, company=self.company, branch=self.branch)
        MarketSaleEmployeePayProfile.objects.create(
            company=self.company, user=self.cashier, pay_scheme="salary", monthly_base_salary=Decimal("3000"))
        self.api = client_for(self.owner)

    def _post(self, **over):
        data = {"employee": str(self.cashier.id), "kind": "salary", "amount": "1.00", "cashbox": str(self.pos.id)}
        data.update(over)
        return self.api.post(URL, data, format="json")

    def _st(self, api=None, extra=""):
        r = (api or self.api).get(f"{ST}?employee={self.cashier.id}&{PERIOD}{extra}")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _cash_movements(self):
        return CashFlow.objects.filter(source_kind="salary_payout").count()

    def test_fine_and_advance_statement(self):
        base = self._st()
        r = self._post(kind="fine", reason="опоздание", cashbox=None)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(r.data["cashflow_id"])
        self.assertEqual(r.data["reason"], "опоздание")
        self.assertEqual(self._cash_movements(), 0)
        key = {"HTTP_IDEMPOTENCY_KEY": "k" * 32}
        a1 = self.api.post(URL, {"employee": str(self.cashier.id), "kind": "advance", "amount": "1", "cashbox": str(self.pos.id)},
                           format="json", **key)
        a2 = self.api.post(URL, {"employee": str(self.cashier.id), "kind": "advance", "amount": "1", "cashbox": str(self.pos.id)},
                           format="json", **key)
        self.assertEqual(a1.status_code, 201, a1.data)
        self.assertTrue(a2.data.get("replayed"))
        self.assertEqual(MarketSalaryPayout.objects.filter(kind="advance").count(), 1)
        self.assertEqual(self._cash_movements(), 1)
        st = self._st()
        self.assertEqual((st["fines"], st["advances"]), ("1.00", "1.00"))
        self.assertEqual(Decimal(base["left"]) - Decimal(st["left"]), Decimal("2.00"))
        self.assertEqual(st["paid"], "0.00")
        kinds = {l["kind"] for l in st["lines"] if l["type"] == "payout"}
        self.assertEqual(kinds, {"fine", "advance"})

    def test_bonus_no_money_and_fine_warning(self):
        r = self._post(kind="bonus", amount="50", cashbox=None)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(r.data["cashflow_id"])
        self.assertEqual(self._st()["bonuses"], "50.00")
        r = self._post(kind="fine", amount="999999", cashbox=None)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual([w["code"] for w in r.data.get("warnings", [])], ["fine_exceeds_accrued"])

    def test_cancel_fine_and_advance(self):
        f = self._post(kind="fine", cashbox=None).data["id"]
        a = self._post(kind="advance").data["id"]
        self.assertEqual(self.api.delete(f"{URL}{f}/").status_code, 200)
        self.assertEqual(self.api.delete(f"{URL}{a}/").status_code, 200)
        st = self._st()
        self.assertEqual((st["fines"], st["advances"]), ("0.00", "0.00"))
        self.assertEqual(self._cash_movements(), 2)  # расход и обратный приход

    def _shift(self, hours, status=CashShift.Status.CLOSED, days_ago=1, cashbox=None):
        opened = timezone.now() - timedelta(days=days_ago, hours=hours)
        s = CashShift.objects.create(
            company=self.company, branch=self.branch, cashbox=cashbox or self.pos, cashier=self.cashier,
            opening_cash=Decimal("0"), status=status)
        CashShift.objects.filter(pk=s.pk).update(
            opened_at=opened, closed_at=opened + timedelta(hours=hours) if status == CashShift.Status.CLOSED else None)
        return s

    def test_per_shift_accrual(self):
        MarketSaleEmployeePayProfile.objects.filter(user=self.cashier).update(
            pay_scheme="per_shift", per_shift_amount=Decimal("100"), monthly_base_salary=Decimal("0"))
        self._shift(8, days_ago=3)
        self._shift(8, days_ago=2)
        self._shift(1, days_ago=1)                              # короткая смена - ставка полностью
        self._shift(8, status=CashShift.Status.OPEN, days_ago=0)  # открытая не считается
        st = self._st()
        self.assertEqual(st["accrued_per_shift"], "300.00")
        self.assertEqual(st["shifts"], 3)
        self.assertEqual(st["hours"], "17.00")
        self.assertEqual(st["accrued"], "300.00")
        self.assertEqual(st["left"], "300.00")
        # то же в tab=salary
        r = self.api.get("/main/analytics/market/?tab=salary&date_from=2000-01-01&date_to=2100-01-01")
        row = next(x for x in r.data["rows"] if x["user_id"] == str(self.cashier.id))
        self.assertEqual((row["per_shift_bonus"], row["total"], row["left"]), ("300.00", "300.00", "300.00"))

    def test_per_shift_plus_percent_scheme_valid(self):
        p = MarketSaleEmployeePayProfile(
            company=self.company, user=self.owner, pay_scheme="per_shift_plus_percent",
            per_shift_amount=Decimal("50"), sales_percent=Decimal("2"))
        p.save()
        bad = MarketSaleEmployeePayProfile(company=self.company, user=self.owner, pay_scheme="per_shift")
        with self.assertRaises(Exception):
            bad.full_clean()

    def test_legacy_tagged_expense_in_paid(self):
        CashFlow.objects.create(
            company=self.company, branch=self.branch, cashbox=self.pos, type="expense", amount=Decimal("700"),
            name=f"Зарплата: Айбек [#{self.cashier.id} 01.10.2026–31.10.2026]", status="approved")
        CashFlow.objects.create(  # чужая метка и не расход - не считаются
            company=self.company, branch=self.branch, cashbox=self.pos, type="expense", amount=Decimal("5"),
            name=f"Зарплата [#{uuid.uuid4()} 01.10.2026–31.10.2026]", status="approved")
        before = self._st(extra="")  # период 2000..2100 - попадает
        self.assertEqual(before["paid"], "700.00")
        self.assertEqual(Decimal(before["accrued"]) - Decimal(before["left"]), Decimal("700.00"))
        self.assertEqual([l["type"] for l in before["lines"] if l["type"] == "legacy_expense"], ["legacy_expense"])
        # вне периода
        r = self.api.get(f"{ST}?employee={self.cashier.id}&period_start=2026-11-01&period_end=2026-11-30")
        self.assertEqual(r.data["paid"], "0.00")
        r = self.api.get(f"{ST}?employee={self.cashier.id}&period_start=2026-10-01&period_end=2026-10-31")
        self.assertEqual(r.data["paid"], "700.00")

    def test_permissions(self):
        cash = client_for(self.cashier)
        # свой statement - можно, чужой - нельзя
        r0 = cash.get(f"{ST}?{PERIOD}"); self.assertEqual(r0.status_code, 200, r0.data)
        r = cash.get(f"{ST}?employee={self.owner.id}&{PERIOD}")
        self.assertEqual((r.status_code, r.data["code"]), (403, "permission_denied"))
        # запись без can_edit_salary - 403, с флагом - можно
        r = cash.post(URL, {"employee": str(self.cashier.id), "kind": "fine", "amount": "1"}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (403, "permission_denied"))
        self.cashier.can_edit_salary = True
        self.cashier.save()
        r = client_for(self.cashier).post(URL, {"employee": str(self.cashier.id), "kind": "fine", "amount": "1"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.api.get(f"{ST}?employee={uuid.uuid4()}&{PERIOD}").data["code"], "employee_not_found")
