"""ТЗ 29.10 Б-99 (срок бонусов) и Б-100 (закрытый период для инвесторов и стартовых расходов)."""
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.utils import timezone

from apps.clientapp.models import AppShopSettings
from apps.construction.models import CashFlow
from apps.main.kassa_views import change_bonus
from apps.main.models import ClientBonusTransaction as T, InvestorPayout
from apps.main.tests_kassa_api import KassaBase
from apps.main.tz1029_bonus_expire import expire_bonuses


class BonusExpireTests(KassaBase):
    def setUp(self):
        super().setUp()
        self.row, _ = AppShopSettings.objects.get_or_create(company=self.company, branch=None)

    def earn(self, amount, days_ago, reason="earn"):
        tx = change_bonus(client=self.client_obj, delta=Decimal(amount), reason=reason)
        T.objects.filter(pk=tx.pk).update(created_at=timezone.now() - timedelta(days=days_ago))

    def set_days(self, n):
        self.row.points_expire_days = n
        self.row.save()

    def bal(self):
        self.client_obj.refresh_from_db()
        return self.client_obj.bonus_balance

    def test_expire_all_old(self):
        self.earn("100", 40)
        self.set_days(30)
        expire_bonuses()
        self.assertEqual(self.bal(), Decimal("0.00"))
        tx = T.objects.get(reason="expire")
        self.assertEqual(tx.delta, Decimal("-100.00"))

    def test_fifo_partially_spent(self):
        self.earn("100", 40)
        change_bonus(client=self.client_obj, delta=Decimal("-30"), reason="redeem")
        self.set_days(30)
        expire_bonuses()
        self.assertEqual(T.objects.get(reason="expire").delta, Decimal("-70.00"))
        self.assertEqual(self.bal(), Decimal("0.00"))

    def test_young_lot_stays(self):
        self.earn("100", 40)
        self.earn("50", 5)
        self.set_days(30)
        expire_bonuses()
        self.assertEqual(self.bal(), Decimal("50.00"))

    def test_no_term_no_expire(self):
        self.earn("100", 400)
        expire_bonuses()
        self.assertFalse(T.objects.filter(reason="expire").exists())
        self.assertEqual(self.bal(), Decimal("100.00"))

    def test_idempotent_and_dry_run(self):
        self.earn("100", 40)
        self.set_days(30)
        call_command("expire_bonuses", "--dry-run")
        self.assertFalse(T.objects.filter(reason="expire").exists())
        expire_bonuses()
        expire_bonuses()
        self.assertEqual(T.objects.filter(reason="expire").count(), 1)

    def test_setting_api(self):
        r = self.api.patch("/api/main/app-shop-settings/", {"points_expire_days": 90}, format="json")
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(r.json()["points_expire_days"], 90)
        r = self.api.patch("/api/main/app-shop-settings/", {"points_expire_days": None}, format="json")
        self.assertIsNone(r.json()["points_expire_days"])


class FinanceClosedTests(KassaBase):
    def close(self, d):
        self.company.finance_closed_until = d
        self.company.save()

    def inv_payout(self, month):
        r = self.api.post("/api/main/investors/", {"name": "И", "amount": "1000"}, format="json")
        return self.api.post("/api/main/investor-payouts/",
                             {"investor": r.json()["id"], "month": month.isoformat(), "accrued": "0"}, format="json")

    def test_payout_closed_open_and_read(self):
        first = timezone.localdate().replace(day=1)
        self.close(first)
        r = self.inv_payout(first)
        self.assertEqual(r.status_code, 409, r.content[:300])
        self.assertEqual((r.json()["code"], r.json()["closed_until"]), ("period_closed", first.isoformat()))
        self.close(first - timedelta(days=1))
        r = self.inv_payout(first)
        self.assertEqual(r.status_code, 201, r.content[:300])
        pid = r.json()["id"]
        self.close(first)
        self.assertEqual(self.api.get("/api/main/investor-payouts/").status_code, 200)
        self.assertEqual(self.api.patch(f"/api/main/investor-payouts/{pid}/", {"tax": "0"}, format="json").status_code, 409)
        self.assertEqual(self.api.delete(f"/api/main/investor-payouts/{pid}/").status_code, 409)
        self.assertEqual(InvestorPayout.objects.count(), 1)

    def test_startup_cashflow(self):
        today = timezone.localdate()
        body = {"cashbox": str(self.cashbox.id), "type": "expense", "name": "Ремонт", "amount": "100", "kind": "startup"}
        self.close(today)
        r = self.api.post("/api/construction/cashflows/", body, format="json")
        self.assertEqual(r.status_code, 409, r.content[:300])
        self.assertEqual(r.json()["code"], "period_closed")
        # обычный расход не трогаем
        r = self.api.post("/api/construction/cashflows/", {**body, "kind": ""}, format="json")
        self.assertIn(r.status_code, (200, 201), r.content[:300])
        self.close(today - timedelta(days=1))
        r = self.api.post("/api/construction/cashflows/", body, format="json")
        self.assertIn(r.status_code, (200, 201), r.content[:300])
        fid = r.json()["id"]
        self.close(today)
        self.assertEqual(self.api.get(f"/api/construction/cashflows/{fid}/").status_code, 200)
        self.assertEqual(self.api.delete(f"/api/construction/cashflows/{fid}/").status_code, 409)
        self.assertTrue(CashFlow.objects.filter(pk=fid).exists())

    def test_company_setting_api(self):
        r = self.api.patch("/api/users/settings/company/", {"finance_closed_until": "2026-09-30"}, format="json")
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(r.json()["finance_closed_until"], "2026-09-30")
        self.assertEqual(self.api.get("/api/users/company/").json()["finance_closed_until"], "2026-09-30")
