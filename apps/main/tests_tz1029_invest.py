from decimal import Decimal

from django.utils import timezone

from apps.construction.models import CashFlow
from apps.main.models import InvestorPayout
from apps.main.tests_kassa_api import KassaBase
from apps.users.models import User


class InvestTz1029Tests(KassaBase):
    def mk_expense(self, amount, **kw):
        return CashFlow.objects.create(
            company=self.company, cashbox=self.cashbox, type=CashFlow.Type.EXPENSE, name="Ремонт",
            amount=Decimal(amount), status=CashFlow.Status.APPROVED, **kw,
        )

    def pnl(self):
        d = timezone.localdate().isoformat()
        r = self.api.get(f"/api/main/analytics/pnl/?date_from={d}&date_to={d}")
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r.json()

    def test_startup_excluded_from_pnl_and_payback(self):
        self.assertEqual(self.quick().status_code, 201)  # выручка 200
        before = self.pnl()
        self.mk_expense("1200000", kind="startup")
        after = self.pnl()
        self.assertEqual(before["opex"]["total"], after["opex"]["total"])
        self.assertEqual(before["operating_profit"], after["operating_profit"])
        self.mk_expense("50")  # обычный расход входит
        self.assertEqual(Decimal(self.pnl()["opex"]["total"]), Decimal("50"))
        r = self.api.get("/api/main/analytics/startup-payback/")
        self.assertEqual(r.status_code, 200, r.content[:300])
        d = r.json()
        self.assertEqual(d["invested"], "1200000.00")
        self.assertEqual(d["returned"], "150.00")  # 200 - 50
        self.assertEqual(d["remaining"], "1199850.00")
        self.assertEqual(len(d["by_month"]), 1)

    def test_kind_via_api_and_rights(self):
        body = {"cashbox": str(self.cashbox.id), "type": "expense", "name": "Ремонт", "amount": "100", "kind": "startup"}
        r = self.api.post("/api/construction/cashflows/", body, format="json")
        self.assertIn(r.status_code, (200, 201), r.content[:300])
        self.assertEqual(r.json()["kind"], "startup")
        self.assertEqual(self.api.get("/api/construction/cashflows/?kind=startup").status_code, 200)

    def test_investor_payouts_and_rights(self):
        self.product.quantity = Decimal("5000")
        self.product.save()
        self.assertEqual(self.quick(items=[{"product": str(self.product.id), "qty": "1000", "price": "100.00"}],
                                    payment={"method": "cash", "received": "100000"}).status_code, 201)
        r = self.api.post("/api/main/investors/", {"name": "Инвестор", "amount": "500000", "terms": {"percent": 20}}, format="json")
        self.assertEqual(r.status_code, 201, r.content[:300])
        inv = r.json()["id"]
        month = timezone.localdate().replace(day=1).isoformat()
        r = self.api.post("/api/main/investor-payouts/", {"investor": inv, "month": month, "paid": "5000", "tax": "100"}, format="json")
        self.assertEqual(r.status_code, 201, r.content[:300])
        self.assertEqual(r.json()["accrued"], "20000.00")
        # больше чистой прибыли месяца начислить нельзя
        r = self.api.post("/api/main/investor-payouts/", {"investor": inv, "month": month, "accrued": "90000"}, format="json")
        self.assertEqual(r.status_code, 400, r.content[:300])
        # версия растёт при изменении
        r = self.api.patch(f"/api/main/investors/{inv}/", {"terms": {"percent": 30}}, format="json")
        self.assertEqual(r.json()["version"], 2)
        self.assertEqual(InvestorPayout.objects.count(), 1)

    def test_cashier_forbidden(self):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(self.cashier)
        for url in ("/api/main/investors/", "/api/main/investor-payouts/", "/api/main/analytics/startup-payback/"):
            self.assertEqual(c.get(url).status_code, 403, url)
        body = {"cashbox": str(self.cashbox.id), "type": "expense", "name": "x", "amount": "1", "kind": "startup"}
        self.assertIn(c.post("/api/construction/cashflows/", body, format="json").status_code, (400, 403))
