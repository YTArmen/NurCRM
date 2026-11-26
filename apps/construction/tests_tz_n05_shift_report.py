"""ТЗ Н-05 (23.10.2026): отчёт смены — выручка без долга, долг, предоплаты, раскладка «в ящике»."""
import uuid
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.construction.auto_cashflow import create_auto_cashflow
from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.construction.views import build_shift_report
from apps.main.models import Product, Sale, SaleItem, SalePayment
from apps.main.pos_views import SaleReturnAPIView
from apps.users.models import Company, User

D = Decimal


class ShiftReportN05Tests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.company = Company.objects.create(name="Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        self.cashbox = Cashbox.objects.create(company=self.company, name="Касса", role=Cashbox.CashboxRole.POS_MAIN)
        self.shift = CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=self.owner, status=CashShift.Status.OPEN,
            opening_cash=D("46.00"),
        )
        self.product = Product.objects.create(company=self.company, name="Т", price=D("1.00"), quantity=D("50"))

    def _sale(self, **kw):
        return Sale.objects.create(company=self.company, cashbox=self.cashbox, shift=self.shift, user=self.owner, **kw)

    def _paid_cash_sale(self, qty):
        total = D(qty)
        sale = self._sale(status=Sale.Status.PAID, payment_method="cash", subtotal=total, total=total)
        SaleItem.objects.create(company=self.company, sale=sale, product=self.product, unit_price=D("1.00"),
                                quantity=D(qty), name_snapshot="Т")
        SalePayment.objects.create(company=self.company, sale=sale, method="cash", amount=total)
        return sale

    def test_scenario_from_spec(self):
        # Чек 4: предоплата 1 нал., долг 3 (как в проде: status=debt, строки cash:1 + legacy debt:3)
        debt = self._sale(status=Sale.Status.DEBT, payment_method="debt", total=D("4.00"), paid_now=D("1.00"),
                          cash_amount=D("1.00"), debt_initial=D("3.00"), debt_remaining=D("3.00"))
        SalePayment.objects.create(company=self.company, sale=debt, method="cash", amount=D("1.00"))
        SalePayment.objects.bulk_create([SalePayment(company=self.company, sale=debt, method="debt", amount=D("3.00"))])
        # Обычная продажа 5 нал. и её частичный возврат 1 нал. через настоящий API возврата
        sale = self._paid_cash_sale(5)
        req = APIRequestFactory().post(
            f"/main/pos/sales/{sale.id}/return/",
            {"idempotency_key": str(uuid.uuid4()), "items": [{"sale_item_id": str(sale.items.first().id), "quantity": "1"}]},
            format="json",
        )
        force_authenticate(req, user=self.owner)
        res = SaleReturnAPIView.as_view()(req, pk=sale.id)
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        # Оплата долга 3 нал. и внесение 2
        for kind, amt in ((CashFlow.SourceKind.DEBT_REPAYMENT, "3.00"), (CashFlow.SourceKind.SHIFT_DRAWER_INFLOW, "2.00")):
            create_auto_cashflow(company=self.company, branch=None, cashbox=self.cashbox, user=self.owner,
                                 shift=self.shift, type=CashFlow.Type.INCOME, amount=D(amt), source_kind=kind,
                                 source_id=str(uuid.uuid4()), affects_shift_drawer=True)

        rep = build_shift_report(self.shift)
        self.assertEqual(rep["prepayment_total"], "1.00")
        self.assertEqual(rep["credit_sales_total"], "3.00")
        self.assertEqual(rep["debt_repaid_total"], "3.00")
        self.assertEqual(rep["debt_repaid_cash"], "3.00")
        self.assertEqual(rep["debt_repaid_noncash"], "0.00")
        self.assertIsNone(rep["closing_cash"])
        self.assertFalse(rep["revenue_excl_debt_includes_returns"])
        # продажи: чек 4 + чек 5 за вычетом возврата 1 = 8; без долга 3 → 5
        self.assertEqual(rep["revenue_excl_debt"], str(D(rep["sales_total"]) - D("3.00")))

        rows = rep["cash_in_drawer_breakdown"]
        self.assertEqual(rows[-1]["code"], "expected_cash")
        by = {r["code"]: D(r["amount"]) for r in rows}
        # Предоплата — ровно в одной строке раскладки
        self.assertEqual(by["prepayments_cash"], D("1.00"))
        self.assertEqual(by["cash_sales_excl_prepayments"], D("5.00"))
        self.assertEqual(by["returns_cash"], D("-1.00"))
        # Сумма строк (кроме последней) = expected_cash, который считает сама смена
        self.assertEqual(sum(D(r["amount"]) for r in rows[:-1]), self.shift.calc_live_totals(refresh=True)["expected_cash"])
        self.assertEqual(rows[-1]["amount"], rep["expected_cash"])
        # Б-07: cash_sales (как был) = cash_sales_excl_prepayments + prepayments_cash
        self.assertEqual(rep["cash_sales_excl_prepayments"], "4.00")  # 5 - возврат 1
        self.assertEqual(D(rep["cash_sales"]), D(rep["cash_sales_excl_prepayments"]) + by["prepayments_cash"])
        self.assertNotIn("cash_sales", by)

    def test_supplier_and_salary_rows(self):
        self._paid_cash_sale(10)
        for kind, amt in ((CashFlow.SourceKind.PROCUREMENT_RECEIPT, "3.00"), (CashFlow.SourceKind.SALARY_PAYOUT, "2.00")):
            create_auto_cashflow(company=self.company, branch=None, cashbox=self.cashbox, user=self.owner,
                                 shift=self.shift, type=CashFlow.Type.EXPENSE, amount=D(amt), source_kind=kind,
                                 source_id=str(uuid.uuid4()), affects_shift_drawer=True)
        rep = build_shift_report(self.shift)
        rows = rep["cash_in_drawer_breakdown"]
        by = {r["code"]: D(r["amount"]) for r in rows}
        self.assertEqual(by["supplier_payments_cash"], D("-3.00"))
        self.assertEqual(by["salary_payouts_cash"], D("-2.00"))
        self.assertNotIn("other_cash", by)
        self.assertEqual(sum(D(r["amount"]) for r in rows[:-1]), D(rep["expected_cash"]))

    def test_closed_shift_closing_cash(self):
        self._paid_cash_sale(2)
        self.shift.close(D("48.00"))
        rep = build_shift_report(self.shift)
        self.assertEqual(rep["closing_cash"], rep["counted_cash"])
        self.assertEqual(rep["closing_cash"], "48.00")

    def test_acceptance_cash_sales_split(self):
        """Б-07: чек 4 (предоплата 1 нал., долг 3) + продажа 2 нал. → cash_sales 3, без предоплат 2, предоплаты 1."""
        debt = self._sale(status=Sale.Status.DEBT, payment_method="debt", total=D("4.00"), paid_now=D("1.00"),
                          cash_amount=D("1.00"), debt_initial=D("3.00"), debt_remaining=D("3.00"))
        SalePayment.objects.create(company=self.company, sale=debt, method="cash", amount=D("1.00"))
        SalePayment.objects.bulk_create([SalePayment(company=self.company, sale=debt, method="debt", amount=D("3.00"))])
        self._paid_cash_sale(2)
        rep = build_shift_report(self.shift)
        by = {r["code"]: D(r["amount"]) for r in rep["cash_in_drawer_breakdown"]}
        self.assertEqual(D(rep["cash_sales"]), D("3.00"))
        self.assertEqual(D(rep["cash_sales_excl_prepayments"]), D("2.00"))
        self.assertEqual(by["prepayments_cash"], D("1.00"))
        self.assertEqual(sum(D(r["amount"]) for r in rep["cash_in_drawer_breakdown"][:-1]), D(rep["expected_cash"]))
