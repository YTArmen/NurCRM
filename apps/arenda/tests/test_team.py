from datetime import date

from apps.arenda.models import AuditLog, Asset, Expense, PayrollLine, TaxPayment
from apps.arenda.services import expenses as ex, payroll as pr
from apps.arenda.services.money import round_int

from .base import ArendaBase

A = "/api/arenda/"


class FakeSettings:
    payroll_social_employee = __import__("decimal").Decimal("10.00")
    payroll_income_tax = __import__("decimal").Decimal("10.00")
    payroll_social_employer = __import__("decimal").Decimal("17.25")
    advance_percent = 40


class FormulaTests(ArendaBase):
    def test_line_formulas_exact(self):
        # оклад 30 000 сом + премия 5 000 = 3 500 000 тыйынов
        r = pr.calc_line(3_000_000, 500_000, FakeSettings)
        self.assertEqual(r["accrued"], 3_500_000)
        self.assertEqual(r["social"], 350_000)
        self.assertEqual(r["income_tax"], 315_000)          # (3 500 000-350 000)*10%
        self.assertEqual(r["net"], 2_835_000)
        self.assertEqual(r["advance"], 1_200_000)           # 40% от оклада = 12 000 сом
        self.assertEqual(r["to_pay"], 1_635_000)
        self.assertEqual(r["employer_contribution"], 603_750)  # 17.25%

    def test_rounding(self):
        r = pr.calc_line(1_234_567, 0, FakeSettings)
        self.assertEqual(r["social"], 123_457)  # 123 456.7
        self.assertEqual(r["income_tax"], 111_111)  # (1234567-123457)=1111110 *10%
        # 40% от 1 234 567 = 493 826.8 -> до 100 сом (10 000): 490 000
        self.assertEqual(r["advance"], 490_000)
        self.assertEqual(r["employer_contribution"], round_int(1_234_567 * 0.1725))

    def test_advance_not_more_than_net(self):
        s = FakeSettings
        s.advance_percent = 100
        try:
            r = pr.calc_line(1_000_000, 0, s)
        finally:
            s.advance_percent = 40
        self.assertLessEqual(r["advance"], r["net"])
        self.assertEqual(r["to_pay"], r["net"] - r["advance"])

    def test_depreciation_stops_at_zero(self):
        a = Asset(cost=1_000_001, purchase_date=date(2026, 1, 15), life_years=1)
        total = sum(ex.depreciation_for_month(a, ex.add_months(date(2026, 1, 1), i)) for i in range(0, 40))
        self.assertEqual(total, 1_000_001)
        self.assertEqual(ex.depreciation_for_month(a, date(2026, 1, 1)), 0)   # в месяц покупки нет
        self.assertGreater(ex.depreciation_for_month(a, date(2026, 2, 1)), 0)
        self.assertEqual(ex.depreciation_for_month(a, date(2027, 2, 1)), 0)   # после 12 месяцев
        self.assertEqual(ex.asset_state(a, date(2030, 1, 1))["residual"], 0)
        self.assertEqual(ex.asset_state(a, date(2026, 1, 1))["residual"], 1_000_001)


class TeamApiTests(ArendaBase):
    def emp(self, name="Бек", salary=3_000_000, bonus=500_000, **kw):
        r = self.client.post(A + "employees/", {"branch": str(self.branch.id), "full_name": name, "salary": salary,
                                                "bonus": bonus, "hired_at": "2026-01-01", **kw}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_expenses_categories_void(self):
        cats = self.client.get(A + "expense-categories/").data
        self.assertEqual(len(cats), 5)
        r = self.client.post(A + "expense-categories/", {"name": "Реклама"}, format="json")
        self.assertEqual(r.status_code, 201)
        dup = self.client.post(A + "expense-categories/", {"name": "реклама"}, format="json")
        self.assertEqual(dup.status_code, 400)
        body = {"branch": str(self.branch.id), "category": r.data["id"], "date": "2026-03-05", "amount": 50000,
                "description": "Баннер", "method": "bank"}
        e = self.client.post(A + "expenses/", body, format="json")
        self.assertEqual(e.status_code, 201, e.data)
        self.client.post(A + "expenses/", {**body, "amount": 25000}, format="json")
        s = self.client.get(A + "expenses/summary/?month=2026-03").data
        self.assertEqual(s["total"], 75000)
        self.assertEqual(self.client.delete(A + f"expenses/{e.data['id']}/").status_code, 405)
        self.assertEqual(self.client.post(A + f"expenses/{e.data['id']}/void/", {}, format="json").status_code, 400)
        v = self.client.post(A + f"expenses/{e.data['id']}/void/", {"reason": "ошибка"}, format="json")
        self.assertTrue(v.data["voided"])
        self.assertEqual(self.client.get(A + "expenses/summary/?month=2026-03").data["total"], 25000)
        self.assertTrue(AuditLog.objects.filter(object_type="expense", action="void").exists())

    def test_assets_and_summary_profit(self):
        a = self.client.post(A + "assets/", {"branch": str(self.branch.id), "name": "Кондиционер", "cost": 1_200_000,
                                             "purchase_date": "2026-01-10", "life_years": 1}, format="json")
        self.assertEqual(a.status_code, 201, a.data)
        lst = self.client.get(A + "assets/?month=2026-03").data["results"][0]
        self.assertEqual((lst["monthly"], lst["accumulated"], lst["residual"]), (100_000, 200_000, 1_000_000))
        cat = self.client.get(A + "expense-categories/").data[0]["id"]
        self.client.post(A + "expenses/", {"branch": str(self.branch.id), "category": cat, "date": "2026-03-02",
                                           "amount": 30000}, format="json")
        u = self.unit(rate=100000, area="10.00")
        t = self.tenant()
        lr = self.lease(t, [u], start="2026-03-01")
        self.assertEqual(lr.status_code, 201, lr.data)
        from apps.arenda.models import Charge, Payment
        charged = sum(c.amount for c in Charge.objects.filter(period=date(2026, 3, 1)))
        if not charged:
            from apps.arenda.services.charges import generate_monthly_charges
            generate_monthly_charges(date(2026, 3, 1))
            charged = sum(c.amount for c in Charge.objects.filter(period=date(2026, 3, 1)))
        self.assertGreater(charged, 0)
        Payment.objects.create(company=self.company, branch=self.branch, tenant=None, amount=7000, method="cash",
                               paid_at="2026-03-10T10:00:00+06:00", description="Парковка")
        s = self.client.get(A + "finance/summary/?month=2026-03").data
        self.assertEqual(s["depreciation"], 100_000)
        self.assertEqual(s["expenses"], 30000)
        self.assertEqual(s["other_income"], 7000)
        self.assertEqual(s["charged"], charged)
        self.assertEqual(s["net_profit"], charged + 7000 - 30000 - 100_000)
        # филиал без данных
        s2 = self.client.get(A + f"finance/summary/?month=2026-03&branch={self.branch2.id}").data
        self.assertEqual(s2["net_profit"], 0)
        self.assertEqual(self.client.get(A + "assets/summary/?month=2026-03").data["residual"], 1_000_000)

    def test_payroll_sheet_and_payout_once(self):
        e = self.emp()
        self.emp("Айна", 2_000_000, 0)
        sh = self.client.get(A + "payroll/?month=2026-03").data
        self.assertEqual(len(sh["lines"]), 2)
        ln = next(x for x in sh["lines"] if x["employee"] == e["id"])
        self.assertEqual((ln["net"], ln["advance"], ln["to_pay"]), (2_835_000, 1_200_000, 1_635_000))
        self.assertEqual(sh["totals"]["accrued"], 3_500_000 + 2_000_000)
        self.assertEqual(sh["staff_cost"], sh["totals"]["accrued"] + sh["totals"]["employer_contribution"])
        self.assertEqual(sh["advance_date"], "2026-03-10")
        self.assertEqual(sh["settle_date"], "2026-03-31")
        r1 = self.client.post(A + "payroll/2026-03/advance/", {}, format="json")
        self.assertEqual(r1.data["created"], 2)
        r2 = self.client.post(A + "payroll/2026-03/advance/", {}, format="json")
        self.assertEqual(r2.data["created"], 0)
        self.assertEqual(Expense.objects.filter(category__name="Зарплата").count(), 2)
        e1 = Expense.objects.get(description__contains="Бек", description__startswith="Аванс")
        self.assertEqual((e1.amount, str(e1.date)), (1_200_000, "2026-03-10"))
        self.client.post(A + "payroll/2026-03/salary/", {"employees": [e["id"]]}, format="json")
        self.client.post(A + "payroll/2026-03/salary/", {"employees": [e["id"]]}, format="json")
        self.assertEqual(Expense.objects.filter(description__startswith="Зарплата за").count(), 1)
        self.assertEqual(Expense.objects.get(description__startswith="Зарплата за").amount, 1_635_000)
        # сторно выплаты снова разрешает выплату
        self.client.post(A + f"expenses/{e1.id}/void/", {"reason": "тест"}, format="json")
        self.assertEqual(PayrollLine.objects.filter(advance_expense__isnull=False).count(), 1)
        self.client.post(A + "payroll/2026-03/advance/", {}, format="json")
        self.assertEqual(Expense.objects.filter(description__startswith="Аванс", voided_at__isnull=True).count(), 2)
        self.assertTrue(AuditLog.objects.filter(action="pay_advance").exists())
        # заморозка: смена оклада не меняет посчитанное
        self.client.patch(A + f"employees/{e['id']}/", {"salary": 9_000_000}, format="json")
        ln2 = next(x for x in self.client.get(A + "payroll/?month=2026-03").data["lines"] if x["employee"] == e["id"])
        self.assertEqual(ln2["accrued"], 3_500_000)

    def test_tax_calendar_from_payroll(self):
        self.emp()
        r = self.client.get(A + "tax-payments/?month=2026-04")
        self.assertEqual(r.status_code, 200)
        rows = r.data["results"]
        self.assertEqual(len(rows), 3)
        amounts = sorted(x["amount"] for x in rows)
        self.assertEqual(amounts, sorted([350_000, 315_000, 603_750]))
        self.assertTrue(all(x["due_date"] == "2026-04-25" for x in rows))
        self.client.get(A + "tax-payments/?month=2026-04")          # идемпотентно
        self.assertEqual(TaxPayment.objects.filter(source="payroll", due_date="2026-04-25").count(), 3)
        tid = rows[0]["id"]
        p = self.client.post(A + f"tax-payments/{tid}/pay/", {}, format="json")
        self.assertEqual(p.status_code, 200, p.data)
        self.assertTrue(p.data["paid"])
        self.client.post(A + f"tax-payments/{tid}/pay/", {}, format="json")
        self.assertEqual(Expense.objects.filter(category__name="Налоги").count(), 1)
        # void расхода -> налог снова не оплачен
        eid = Expense.objects.get(category__name="Налоги").id
        self.client.post(A + f"expenses/{eid}/void/", {"reason": "x"}, format="json")
        self.assertFalse(TaxPayment.objects.get(id=tid).paid)

    def test_permissions(self):
        _, c = self.staff()
        self.assertEqual(c.get(A + "employees/").status_code, 403)
        self.assertEqual(c.get(A + "expenses/").status_code, 403)

    def test_employee_archive_and_contract(self):
        e = self.emp()
        self.assertIsNone(e["contract"])
        self.assertEqual(self.client.delete(A + f"employees/{e['id']}/").status_code, 204)
        self.assertEqual(self.client.get(A + "employees/").data["count"], 0)
