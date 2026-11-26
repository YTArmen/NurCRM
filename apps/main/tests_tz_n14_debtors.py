"""ТЗ Н-14: должники (include=sales, пагинация, просрочка, ручные долги, поставщики) и поля долга в pos/sales/."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main.models import Client, ClientDeal, DealInstallment, Sale
from apps.users.models import Branch, Company, User


class TzN14DebtorsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email=f"n14-{uuid.uuid4().hex}@test.com", password="x")
        self.company = Company.objects.create(name="N14", owner=self.user)
        self.user.company = self.company
        self.branch = Branch.objects.create(company=self.company, name="Main")
        self.user.branch = self.branch
        self.user.save()
        self.cashbox = Cashbox.objects.create(
            company=self.company, branch=self.branch, name="POS", role=Cashbox.CashboxRole.POS_BRANCH
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.debtor = Client.objects.create(company=self.company, branch=self.branch, full_name="Должник", phone="+996700000010")

    def _debt_sale(self, client, total="100.00", prepay="0.00", days=10, created_shift_days=0):
        total, prepay = Decimal(total), Decimal(prepay)
        sale = Sale.objects.create(
            company=self.company, branch=self.branch, cashbox=self.cashbox, user=self.user, client=client,
            payment_method="debt", status=Sale.Status.DEBT, subtotal=total, total=total,
            cash_received=prepay, paid_now=prepay, debt_initial=total - prepay, debt_remaining=total - prepay,
        )
        deal = ClientDeal.objects.create(
            company=self.company, branch=self.branch, client=client, sale=sale, kind=ClientDeal.Kind.DEBT,
            title="Долг", amount=total, prepayment=prepay, debt_months=1,
            first_due_date=date.today() + timedelta(days=days),
        )
        return sale, deal

    def _debtors(self, qs=""):
        return self.api.get(f"/api/main/clients/debtors/{qs}")

    def test_default_is_bare_array_with_new_keys(self):
        self._debt_sale(self.debtor)
        r = self._debtors()
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.json()
        self.assertIsInstance(rows, list)
        row = rows[0]
        self.assertNotIn("sales", row)
        self.assertEqual(row["debt_total"], "100.00")
        self.assertEqual(row["manual_debts_total"], "0.00")
        self.assertEqual(row["overdue_amount"], "0.00")
        self.assertIsNone(row["last_payment_at"])

    def test_include_sales_oldest_first_with_due_date_and_deal(self):
        s1, d1 = self._debt_sale(self.debtor, "100.00", days=5)
        s2, d2 = self._debt_sale(self.debtor, "50.00", days=20)
        Sale.objects.filter(pk=s1.pk).update(created_at=s2.created_at - timedelta(days=3))
        rows = self._debtors("?include=sales").json()
        sales = rows[0]["sales"]
        self.assertEqual([x["sale"] for x in sales], [str(s1.id), str(s2.id)])
        self.assertEqual(sales[0]["debt_remaining"], "100.00")
        self.assertEqual(sales[0]["total"], "100.00")
        self.assertEqual(sales[0]["deal_id"], str(d1.id))
        self.assertEqual(sales[0]["due_date"], (date.today() + timedelta(days=5)).isoformat())
        self.assertEqual(rows[0]["debt_total"], "150.00")
        self.assertEqual(rows[0]["sales_count"], 2)

    def test_manual_debt_separate_and_in_total(self):
        self._debt_sale(self.debtor, "100.00")
        self.api.post(f"/api/main/clients/{self.debtor.id}/debts/", {"amount": "40.00", "comment": "ручной"}, format="json")
        row = self._debtors("?include=sales").json()[0]
        self.assertEqual(row["manual_debts_total"], "40.00")
        self.assertEqual(row["debt_total"], "140.00")
        self.assertEqual(len(row["sales"]), 1)  # ручной долг без чека в sales[] не входит

    def test_overdue_and_last_payment(self):
        sale, deal = self._debt_sale(self.debtor, "100.00", days=-5)
        row = self._debtors().json()[0]
        self.assertEqual(row["overdue_amount"], "100.00")
        r = self.api.post(
            f"/api/main/pos/sales/{sale.id}/pay-debt/",
            {"amount": "30.00", "method": "transfer"}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r.status_code, 200, r.content)
        row = self._debtors().json()[0]
        self.assertEqual(row["overdue_amount"], "70.00")
        self.assertIsNotNone(row["last_payment_at"])

    def test_pagination_only_when_requested(self):
        for i in range(3):
            c = Client.objects.create(company=self.company, branch=self.branch, full_name=f"К{i}", phone=f"+99670000002{i}")
            self._debt_sale(c, str(100 + i))
        data = self._debtors("?page_size=2").json()
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["results"]), 2)
        self.assertIsNotNone(data["next"])
        page2 = self._debtors("?page_size=2&page=2&include=sales").json()
        self.assertEqual(len(page2["results"]), 1)
        self.assertIn("sales", page2["results"][0])
        self.assertIsNone(page2["next"])
        self.assertIsInstance(self._debtors().json(), list)

    def test_suppliers_excluded_by_default(self):
        sup = Client.objects.create(
            company=self.company, branch=self.branch, full_name="Поставщик", phone="+996700000030",
            type=Client.StatusClient.SUPPLIERS,
        )
        self._debt_sale(sup, "500.00")
        self._debt_sale(self.debtor, "100.00")
        names = [r["full_name"] for r in self._debtors().json()]
        self.assertEqual(names, ["Должник"])
        names = [r["full_name"] for r in self._debtors("?include_suppliers=1").json()]
        self.assertEqual(sorted(names), ["Должник", "Поставщик"])

    def test_pos_sales_debt_rows_have_paid_prepayment_due_date(self):
        sale, deal = self._debt_sale(self.debtor, "200.00", prepay="50.00", days=7)
        self.api.post(
            f"/api/main/pos/sales/{sale.id}/pay-debt/",
            {"amount": "30.00", "method": "transfer"}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        res = self.api.get("/api/main/pos/sales/?status=debt").json()
        rows = res["results"] if isinstance(res, dict) else res
        row = next(r for r in rows if r["id"] == str(sale.id))
        self.assertEqual(row["debt_initial"], "150.00")
        self.assertEqual(row["debt_remaining"], "120.00")
        self.assertEqual(row["debt_paid"], "30.00")
        self.assertEqual(row["prepayment"], "50.00")
        self.assertEqual(row["due_date"], (date.today() + timedelta(days=7)).isoformat())
