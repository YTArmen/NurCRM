"""ТЗ Н-06 (23.10.2026): drawer_cash / drawer_cash_at / last_closed_shift в cashboxes/."""
import uuid
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.users.models import Company, User

D = Decimal


class DrawerCashTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.company = Company.objects.create(name="Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        self.cb = Cashbox.objects.create(company=self.company, name="Касса", role=Cashbox.CashboxRole.POS_MAIN)
        self.other = Cashbox.objects.create(company=self.company, name="Расходы", role=Cashbox.CashboxRole.EXPENSE_VARIABLE)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def _get(self, cb):
        r = self.api.get(f"/api/construction/cashboxes/{cb.id}/")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_closed_then_deposit_then_open(self):
        shift = CashShift.objects.create(company=self.company, cashbox=self.cb, cashier=self.owner,
                                         status=CashShift.Status.OPEN, opening_cash=D("46.00"))
        d = self._get(self.cb)
        self.assertEqual(d["drawer_cash"], "46.00")  # открытая смена: её expected_cash
        shift.close(D("51.00"))
        d = self._get(self.cb)
        self.assertEqual(d["drawer_cash"], "51.00")
        self.assertEqual(d["last_closed_shift"], str(shift.id))
        self.assertTrue(d["drawer_cash_at"])
        # внесение 5 нал. вне смены
        CashFlow.objects.create(company=self.company, cashbox=self.cb, type=CashFlow.Type.INCOME, amount=D("5.00"),
                                status=CashFlow.Status.APPROVED, payment_method="cash")
        self.assertEqual(self._get(self.cb)["drawer_cash"], "56.00")
        # безнал вне смены ящик не меняет
        CashFlow.objects.create(company=self.company, cashbox=self.cb, type=CashFlow.Type.INCOME, amount=D("7.00"),
                                status=CashFlow.Status.APPROVED, payment_method="transfer")
        self.assertEqual(self._get(self.cb)["drawer_cash"], "56.00")
        # открыли смену: expected_cash смены
        s2 = CashShift.objects.create(company=self.company, cashbox=self.cb, cashier=self.owner,
                                      status=CashShift.Status.OPEN, opening_cash=D("56.00"))
        self.assertEqual(self._get(self.cb)["drawer_cash"], str(s2.calc_live_totals(refresh=True)["expected_cash"]))

    def test_list_and_non_pos_cashbox(self):
        r = self.api.get("/api/construction/cashboxes/")
        self.assertEqual(r.status_code, 200)
        rows = r.data["results"] if isinstance(r.data, dict) else r.data
        by = {x["id"]: x for x in rows}
        self.assertIn("drawer_cash", by[str(self.cb.id)])
        o = by[str(self.other.id)]
        self.assertIsNone(o["drawer_cash"])
        self.assertIsNone(o["drawer_cash_at"])
        self.assertIsNone(o["last_closed_shift"])
