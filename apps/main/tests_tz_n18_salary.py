"""ТЗ Н-18: выплаты зарплаты магазина."""
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.main.models import Branch, MarketSalaryPayout, MarketSaleEmployeePayProfile
from apps.users.models import Company

User = get_user_model()
URL = "/main/market-salary/payouts/"
AN = "/main/analytics/market/?tab=salary"


class N18Tests(TestCase):
    def setUp(self):
        cache.clear()
        s = uuid.uuid4().hex[:6]
        self.owner = User.objects.create_user(email=f"o{s}@t.kg", password="p12345678", role="owner")
        self.company = Company.objects.create(name=f"N18 {s}", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        self.branch = Branch.objects.create(name="Main", company=self.company)
        self.pos = Cashbox.objects.create(name="Касса", role=Cashbox.CashboxRole.POS_MAIN, company=self.company, branch=self.branch)
        self.cashier = User.objects.create_user(email=f"c{s}@t.kg", password="p12345678", role="cashier", company=self.company)
        self.shift = CashShift.objects.create(
            company=self.company, branch=self.branch, cashbox=self.pos, cashier=self.cashier,
            opening_cash=Decimal("100.00"), status=CashShift.Status.OPEN,
        )
        MarketSaleEmployeePayProfile.objects.create(company=self.company, user=self.cashier, pay_scheme="salary", monthly_base_salary=Decimal("3000"))
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def _post(self, **over):
        data = {"employee": str(self.cashier.id), "kind": "salary", "amount": "1.00", "cashbox": str(self.pos.id)}
        data.update(over)
        return self.api.post(URL, data, format="json")

    def _drawer(self):
        return CashShift.objects.get(pk=self.shift.pk).calc_live_totals()["drawer_expected_cash"]

    def _row(self):
        r = self.api.get(AN + "&date_from=2000-01-01&date_to=2100-01-01")
        self.assertEqual(r.status_code, 200, r.data)
        return next(x for x in r.data["rows"] if x["user_id"] == str(self.cashier.id))

    def test_payout_and_replay_and_analytics(self):
        before = self._row()
        self.assertEqual(before["paid"], "0.00")
        r = self.api.post(URL, {"employee": str(self.cashier.id), "kind": "salary", "amount": "1", "cashbox": str(self.pos.id),
                                "idempotency_key": "k1"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r2 = self.api.post(URL, {"employee": str(self.cashier.id), "kind": "salary", "amount": "1", "cashbox": str(self.pos.id),
                                 "idempotency_key": "k1"}, format="json")
        self.assertTrue(r2.data.get("replayed"))
        self.assertEqual(r2.data["id"], r.data["id"])
        self.assertEqual(MarketSalaryPayout.objects.count(), 1)
        cf = CashFlow.objects.get(pk=r.data["cashflow_id"])
        self.assertEqual((cf.type, cf.source_kind, cf.source_id), ("expense", "salary_payout", r.data["id"]))
        self.assertEqual(cf.category.code, "salary")
        after = self._row()
        self.assertEqual(after["paid"], "1.00")
        self.assertEqual(Decimal(before["left"]) - Decimal(after["left"]), Decimal("1.00"))
        for k in ("accrued", "advance", "bonus", "deductions", "left"):
            self.assertIn(k, after)

    def test_advance_from_shift_drawer_and_reversal(self):
        d0 = self._drawer()
        r = self._post(kind="advance", shift=str(self.shift.id), cashbox=None)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self._drawer(), d0 - Decimal("1.00"))
        self.assertEqual(r.data["shift"], str(self.shift.id))
        self.assertEqual(self._row()["advance"], "1.00")
        dr = self.api.delete(f"{URL}{r.data['id']}/")
        self.assertEqual(dr.status_code, 200, dr.data)
        self.assertEqual(self._drawer(), d0)
        self.assertEqual(self._row()["advance"], "0.00")
        self.assertEqual(CashFlow.objects.filter(source_kind="salary_payout").count(), 2)
        self.assertEqual(self.api.delete(f"{URL}{r.data['id']}/").status_code, 409)

    def test_deduction_no_money(self):
        r = self._post(kind="deduction", amount="5.00")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(r.data["cashflow_id"])
        self.assertEqual(CashFlow.objects.filter(source_kind="salary_payout").count(), 0)
        self.assertEqual(self._row()["deductions"], "5.00")

    def test_errors(self):
        self.assertEqual(self._post(employee=str(uuid.uuid4())).data["code"], "employee_not_found")
        self.assertEqual(self._post(amount="0").data["code"], "amount_invalid")
        self.assertEqual(self._post(amount="abc").data["code"], "amount_invalid")
        self.assertEqual(self._post(cashbox=str(uuid.uuid4())).data["code"], "cashbox_not_found")
        self.assertEqual(self._post(source="owner").data["code"], "owner_source_not_supported")
        self.shift.status = CashShift.Status.CLOSED
        self.shift.save()
        r = self._post(shift=str(self.shift.id), cashbox=None)
        self.assertEqual((r.status_code, r.data["code"]), (400, "shift_closed"))
        c = APIClient()
        c.force_authenticate(self.cashier)
        r = c.post(URL, {"employee": str(self.cashier.id), "amount": "1"}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (403, "permission_denied"))

    def test_list_filters(self):
        self._post()
        self._post(kind="bonus")
        r = self.api.get(URL + "?kind=bonus")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(self.api.get(URL + f"?employee={self.cashier.id}").data["count"], 2)
        c = APIClient()
        c.force_authenticate(self.cashier)
        self.assertEqual(c.get(URL).data["count"], 2)
