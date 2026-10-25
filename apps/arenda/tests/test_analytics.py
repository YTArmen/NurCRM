from datetime import date, datetime

from django.utils import timezone

from apps.arenda.models import Asset, Expense, ExpenseCategory, Payment, Remark

from .base import ArendaBase, orm_charge, orm_lease, orm_payment

URL = "/api/arenda/analytics/"
Q = "&period=month&date=2026-09-15"


class AnalyticsBase(ArendaBase):
    def setUp(self):
        super().setUp()
        self.client.get("/api/arenda/me/")
        self.u1 = self.unit("101", floor=1, area="10.00", rate=100000, branch=self.branch)
        self.u2 = self.unit("201", floor=2, area="20.00", rate=100000, branch=self.branch)
        self.u3 = self.unit("301", floor=1, area="10.00", rate=200000, branch=self.branch2)
        self.u4 = self.unit("302", floor=1, area="10.00", rate=200000, branch=self.branch2)  # свободное
        self.t1, self.t2, self.t3 = self.tenant("ИП Один"), self.tenant("ИП Два"), self.tenant("ИП Три")
        self.l1 = orm_lease(self, self.t1, [self.u1], date(2026, 1, 1), label="А-1")
        self.l2 = orm_lease(self, self.t2, [self.u2], date(2026, 1, 1), date(2026, 10, 30), label="А-2")
        self.l3 = orm_lease(self, self.t3, [self.u3], date(2026, 1, 1), branch=self.branch2, label="А-3")
        # август и сентябрь: начисления
        for m in (8, 9):
            orm_charge(self, self.t1, self.l1, date(2026, m, 10), 1_000_000)
            orm_charge(self, self.t2, self.l2, date(2026, m, 10), 2_000_000)
            orm_charge(self, self.t3, self.l3, date(2026, m, 10), 2_000_000, branch=self.branch2)
        # оплаты
        orm_payment(self, self.t1, 1_000_000, date(2026, 8, 9))            # август вовремя
        orm_payment(self, self.t1, 1_000_000, date(2026, 9, 12))           # сентябрь с опозданием
        orm_payment(self, self.t3, 1_500_000, date(2026, 9, 5), branch=self.branch2, method="bank")
        orm_payment(self, None, 100_000, date(2026, 9, 7), method="qr")    # прочий доход
        # расходы
        cat = {n: ExpenseCategory.objects.get(company=self.company, name=n) for n in ("Налоги", "Зарплата")}
        Expense.objects.create(company=self.company, branch=self.branch, category=cat["Налоги"], date=date(2026, 9, 3), amount=300_000)
        Expense.objects.create(company=self.company, branch=self.branch2, category=cat["Зарплата"], date=date(2026, 9, 25), amount=500_000)
        Expense.objects.create(company=self.company, branch=self.branch, category=cat["Налоги"], date=date(2026, 8, 3), amount=100_000)
        Expense.objects.create(company=self.company, branch=self.branch, category=cat["Налоги"], date=date(2026, 9, 4), amount=999, voided_at=timezone.make_aware(datetime(2026, 9, 5)))

    def get(self, tab, extra="", **kw):
        r = (kw.get("client") or self.client).get(f"{URL}?tab={tab}{Q}{extra}")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        return r.data


class AnalyticsTests(AnalyticsBase):
    def test_cashier_403_and_validation(self):
        from rest_framework.test import APIClient
        from apps.arenda.models import ArendaAccess
        u, c = self.staff()
        ArendaAccess.objects.create(company=self.company, user=u, preset="cashier", sections=["tenants", "cash"])
        r = c.get(URL + "?tab=overview")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data["section"], "analytics")
        self.assertEqual(self.client.get(URL + "?tab=zzz").status_code, 400)
        self.assertEqual(self.client.get(URL + "?period=week").status_code, 400)
        self.assertEqual(self.client.get(URL + "?date=bad").status_code, 400)
        ArendaAccess.objects.filter(user=u).update(sections=["analytics"])
        self.assertEqual(c.get(URL + "?tab=overview").status_code, 200)

    def test_overview_matches_payments_and_expenses(self):
        d = self.get("overview")
        self.assertEqual(d["charged"], 5_000_000)
        self.assertEqual(d["collected"], 2_500_000)
        self.assertEqual(d["other_income"], 100_000)
        self.assertEqual(d["expenses"], 800_000)  # аннулированный не считается
        self.assertEqual(d["profit"], 5_000_000 + 100_000 - 800_000)
        # сверка с ORM «как касса и расходы»
        from django.db.models import Sum
        pay = Payment.objects.filter(status="posted", paid_at__date__gte="2026-09-01", paid_at__date__lte="2026-09-30")
        self.assertEqual(pay.filter(tenant__isnull=False).aggregate(s=Sum("amount"))["s"], d["collected"])
        exp = Expense.objects.filter(voided_at__isnull=True, date__gte="2026-09-01", date__lte="2026-09-30")
        self.assertEqual(exp.aggregate(s=Sum("amount"))["s"], d["expenses"])

    def test_branch_and_all_consistent(self):
        allv, b1, b2 = (self.get("overview", f"&branch={b}") for b in ("all", self.branch.id, self.branch2.id))
        for k in ("charged", "collected", "other_income", "expenses", "profit"):
            self.assertEqual(allv[k], b1[k] + b2[k], k)
        self.assertEqual(b1["expenses"], 300_000)
        self.assertEqual(b2["collected"], 1_500_000)
        for tab, key in (("revenue", "charged"), ("revenue", "collected"), ("expenses", "total")):
            a = self.get(tab, "&branch=all")[key]
            parts = sum(self.get(tab, f"&branch={b.id}")[key] for b in (self.branch, self.branch2))
            self.assertEqual(a, parts, (tab, key))

    def test_branch_forbidden_for_restricted_staff(self):
        from apps.arenda.models import ArendaAccess
        u, c = self.staff()
        a = ArendaAccess.objects.create(company=self.company, user=u, sections=["analytics"], all_branches=False)
        a.branches.set([self.branch])
        self.assertEqual(c.get(f"{URL}?tab=overview&branch={self.branch2.id}").status_code, 403)
        mine = c.get(f"{URL}?tab=overview{Q}").data  # «all» = только доступные
        self.assertEqual(mine["expenses"], 300_000)

    def test_depreciation_in_profit(self):
        Asset.objects.create(company=self.company, branch=self.branch, name="Лифт", cost=12_000_000,
                             purchase_date=date(2026, 3, 15), life_years=10)
        d = self.get("overview")
        self.assertEqual(d["depreciation"], 100_000)  # 12 000 000 / 120
        self.assertEqual(d["profit"], 5_000_000 + 100_000 - 800_000 - 100_000)

    def test_overview_occupancy_debt_attention(self):
        d = self.get("overview")
        # 40 м² из 50 сданы: у площадки 302 нет договора, 101/201/301 сданы -> 40/50 = 80%
        self.assertEqual(d["occupancy_percent"], 80.0)
        # долг на конец сентября: t1 0, t2 4 000 000 (оба месяца не оплачены), t3 2 500 000
        self.assertEqual(d["debt"], 4_000_000 + 2_500_000)
        self.assertEqual(d["overdue_tenants"], 2)
        kinds = {a["kind"] for a in d["attention"]}
        self.assertTrue({"overdue", "free_units", "expiring_leases"} <= kinds)

    def test_revenue_tab(self):
        d = self.get("revenue")
        self.assertEqual(len(d["months"]), 12)
        self.assertEqual(d["months"][-1]["month"], date(2026, 9, 1))
        self.assertEqual(d["months"][-1]["charged"], 5_000_000)
        self.assertEqual(d["months"][-1]["collected"], 2_500_000)
        self.assertEqual(d["months"][-1]["collection_percent"], 50.0)
        self.assertEqual(d["months"][-2]["collected"], 1_000_000)
        self.assertEqual(d["months"][0]["charged"], 0)
        self.assertEqual(sum(x["amount"] for x in d["by_floor"]), d["charged"])
        self.assertEqual(sum(x["amount"] for x in d["by_unit_type"]), d["charged"])
        floors = {x["floor"]: x["amount"] for x in d["by_floor"]}
        self.assertEqual(floors, {1: 3_000_000, 2: 2_000_000})
        methods = {x["method"]: x["amount"] for x in d["payment_methods"]}
        self.assertEqual(methods, {"cash": 1_000_000, "bank": 1_500_000, "qr": 100_000})

    def test_expenses_tab(self):
        d = self.get("expenses")
        self.assertEqual(d["total"], 800_000)
        cats = {c["category"]: c for c in d["by_category"]}
        self.assertEqual(cats["Налоги"]["amount"], 300_000)
        self.assertEqual(cats["Налоги"]["share_percent"], 37.5)
        self.assertEqual(sum(c["amount"] for c in d["by_category"]), d["total"])
        self.assertEqual(len(d["months"]), 12)
        self.assertEqual(d["months"][-2]["expenses"], 100_000)
        self.assertEqual(d["staff"]["salary_expenses"], 500_000)
        self.assertEqual(d["share_of_revenue_percent"], round(800_000 * 100 / 5_100_000, 1))

    def test_tenants_tab(self):
        Remark.objects.create(company=self.company, tenant_id=self.t2["id"], type="warning", date=date(2026, 9, 9))
        d = self.get("tenants")
        self.assertEqual([r["tenant"]["name"] for r in d["top_debtors"]], ["ИП Два", "ИП Три"])
        self.assertEqual(d["top_debtors"][0]["debt"], 4_000_000)
        self.assertEqual(d["top_debtors"][0]["overdue_days"], 51)  # с 10 августа до 30 сентября
        # дисциплина по начислениям со сроком в сентябре: t1 опоздал, t2 не платил, t3 не платил полностью
        disc = d["discipline"]
        self.assertEqual(disc["on_time"], 0)
        self.assertEqual(disc["late"], 3)
        self.assertEqual(d["remarks"]["total"], 1)
        self.assertEqual(d["remarks"]["by_type"], {"warning": 1})
        self.assertEqual(len(d["expiring_leases"]), 1)  # А-2 до 30.10 в пределах 45 дней от 30.09

    def test_units_tab(self):
        d = self.get("units")
        self.assertEqual(len(d["months"]), 12)
        self.assertEqual(d["months"][-1]["occupancy_percent"], 80.0)
        self.assertEqual(d["months"][0]["occupancy_percent"], 0.0)  # в октябре 2025 договоров ещё нет
        floors = {f["floor"]: f for f in d["by_floor"]}
        self.assertEqual(floors[1]["occupancy_percent"], round(20 / 30 * 100, 1))
        self.assertEqual(floors[2]["occupancy_percent"], 100.0)
        self.assertEqual(d["idle_count"], 1)
        self.assertEqual(d["idle_units"][0]["number"], "302")
        self.assertEqual(d["lost_rent_monthly"], 10 * 200000)
        q = self.client.get(f"{URL}units/?period=quarter&date=2026-09-15").data
        self.assertEqual(q["lost_rent_period"], 3 * 10 * 200000)

    def test_terminated_lease_frees_unit(self):
        self.l1.status, self.l1.terminated_at = "terminated", date(2026, 8, 31)
        self.l1.save()
        d = self.get("units")
        self.assertEqual(d["idle_count"], 2)
        idle = {i["number"]: i for i in d["idle_units"]}
        self.assertEqual(idle["101"]["idle_days"], 30)  # с 31 августа по 30 сентября

    def test_periods(self):
        q = self.client.get(f"{URL}?tab=overview&period=quarter&date=2026-09-15").data
        self.assertEqual((q["date_from"], q["date_to"]), (date(2026, 7, 1), date(2026, 9, 30)))
        self.assertEqual(q["charged"], 10_000_000)
        self.assertEqual(q["expenses"], 900_000)
        y = self.client.get(f"{URL}?tab=overview&period=year&date=2026-09-15").data
        self.assertEqual((y["date_from"], y["date_to"]), (date(2026, 1, 1), date(2026, 12, 31)))
        self.assertEqual(y["charged"], 10_000_000)
