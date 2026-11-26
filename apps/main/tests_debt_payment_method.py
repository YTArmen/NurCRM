from datetime import date
from decimal import Decimal
import uuid
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.main.models import (
    Branch,
    Client,
    ClientDeal,
    DealInstallment,
    DealPayment,
    Debt,
    DebtPayment,
)
from apps.main.services_debt import (
    DEBT_PAYMENT_DEFAULT,
    DEBT_PAYMENT_FALLBACK,
    DEBT_PAYMENT_METHODS,
    normalize_debt_payment_method,
)
from apps.users.models import Company

User = get_user_model()


class DebtPaymentMethodTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        email = f"owner_{uuid.uuid4().hex[:8]}@test.com"
        self.user = User.objects.create_user(email=email, password="password123", is_staff=True)
        self.company = Company.objects.create(name=f"Test Company {uuid.uuid4().hex[:6]}", owner=self.user)
        self.user.company = self.company
        self.user.save()

        self.branch = Branch.objects.create(name="Central Branch", company=self.company)
        self.user.branch = self.branch
        self.user.save()

        self.pos_cashbox = Cashbox.objects.create(
            name="Касса филиала",
            role=Cashbox.CashboxRole.POS_BRANCH,
            company=self.company,
            branch=self.branch,
        )

        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        try:
            self.company.delete()
            self.user.delete()
        except Exception:
            pass

    def test_normalization(self):
        """Test normalization of valid, default, and fallback payment methods."""
        self.assertEqual(normalize_debt_payment_method(None), DEBT_PAYMENT_DEFAULT)
        self.assertEqual(normalize_debt_payment_method(""), DEBT_PAYMENT_DEFAULT)
        self.assertEqual(normalize_debt_payment_method("  "), DEBT_PAYMENT_DEFAULT)
        self.assertEqual(normalize_debt_payment_method("cash"), "cash")
        self.assertEqual(normalize_debt_payment_method("CASH"), "cash")
        self.assertEqual(normalize_debt_payment_method("MBank"), "mbank")
        self.assertEqual(normalize_debt_payment_method("optima"), "optima")
        self.assertEqual(normalize_debt_payment_method("obank"), "obank")
        self.assertEqual(normalize_debt_payment_method("bakai"), "bakai")
        self.assertEqual(normalize_debt_payment_method("demir"), "demir")
        self.assertEqual(normalize_debt_payment_method("other"), "other")
        self.assertEqual(normalize_debt_payment_method("transfer"), "transfer")

        # Unknown value fallback
        self.assertEqual(normalize_debt_payment_method("crypto_wallet"), DEBT_PAYMENT_FALLBACK)
        self.assertEqual(normalize_debt_payment_method("kaspi"), DEBT_PAYMENT_FALLBACK)

    def test_debt_v1_patch_cash_increases_shift_drawer(self):
        """
        PATCH /main/debts/{id}/ with payment_method='cash'
        - Creates DebtPayment with payment_method='cash'
        - Auto CashFlow has affects_shift_drawer=True
        - CashShift live totals drawer_expected_cash increases
        """
        shift = CashShift.objects.create(
            company=self.company,
            branch=self.branch,
            cashbox=self.pos_cashbox,
            cashier=self.user,
            opening_cash=Decimal("100.00"),
            status=CashShift.Status.OPEN,
        )

        debt = Debt.objects.create(
            company=self.company,
            branch=self.branch,
            name="John Cash",
            phone="+996555111222",
            amount=Decimal("1000.00"),
        )

        url = f"/main/debts/{debt.id}/"
        payload = {
            "amount": "800.00",
            "payment_method": "cash",
            "cashbox_id": str(self.pos_cashbox.id),
            "cashbox_role": "pos_branch",
            "shift_id": str(shift.id),
        }
        res = self.client.patch(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        debt.refresh_from_db()
        # ТЗ 12.2: сумма долга остаётся прежней, остаток падает один раз - за счёт платежа.
        self.assertEqual(debt.amount, Decimal("1000.00"))
        self.assertEqual(debt.balance, Decimal("800.00"))
        self.assertEqual(debt.payment_method, "cash")

        pm = DebtPayment.objects.filter(debt=debt).first()
        self.assertIsNotNone(pm)
        self.assertEqual(pm.amount, Decimal("200.00"))
        self.assertEqual(pm.payment_method, "cash")

        cf = CashFlow.objects.filter(source_kind=CashFlow.SourceKind.DEBT_REPAYMENT, source_id=str(debt.id)).first()
        self.assertIsNotNone(cf)
        self.assertEqual(cf.payment_method, "cash")
        self.assertTrue(cf.affects_shift_drawer)
        self.assertEqual(cf.amount, Decimal("200.00"))

        cf.status = CashFlow.Status.APPROVED
        cf.save(update_fields=["status"])

        totals = shift.calc_live_totals()
        # Opening cash 100 + 200 cash repayment = 300
        self.assertEqual(totals["drawer_expected_cash"], Decimal("300.00"))

    def test_debt_v1_patch_mbank_does_not_affect_shift_drawer(self):
        """
        PATCH /main/debts/{id}/ with payment_method='mbank'
        - Creates DebtPayment with payment_method='mbank'
        - Auto CashFlow has affects_shift_drawer=False
        - CashShift live totals drawer_expected_cash does NOT increase
        - CashShift calc_payment_breakdown includes mbank
        """
        shift = CashShift.objects.create(
            company=self.company,
            branch=self.branch,
            cashbox=self.pos_cashbox,
            cashier=self.user,
            opening_cash=Decimal("500.00"),
            status=CashShift.Status.OPEN,
        )

        debt = Debt.objects.create(
            company=self.company,
            branch=self.branch,
            name="Alice MBank",
            phone="+996555333444",
            amount=Decimal("1500.00"),
        )

        url = f"/main/debts/{debt.id}/"
        payload = {
            "amount": "1000.00",
            "payment_method": "mbank",
            "cashbox_id": str(self.pos_cashbox.id),
            "cashbox_role": "pos_branch",
            "shift_id": str(shift.id),
        }
        res = self.client.patch(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        pm = DebtPayment.objects.filter(debt=debt).first()
        self.assertIsNotNone(pm)
        self.assertEqual(pm.amount, Decimal("500.00"))
        self.assertEqual(pm.payment_method, "mbank")

        cf = CashFlow.objects.filter(source_kind=CashFlow.SourceKind.DEBT_REPAYMENT, source_id=str(debt.id)).first()
        self.assertIsNotNone(cf)
        self.assertEqual(cf.payment_method, "mbank")
        self.assertFalse(cf.affects_shift_drawer)

        cf.status = CashFlow.Status.APPROVED
        cf.save(update_fields=["status"])

        totals = shift.calc_live_totals()
        # Non-cash repayment does not affect drawer cash
        self.assertEqual(totals["drawer_expected_cash"], Decimal("500.00"))

        breakdown = shift.calc_payment_breakdown()
        mbank_item = next((item for item in breakdown if item["method"] == "mbank"), None)
        self.assertIsNotNone(mbank_item)
        self.assertEqual(Decimal(mbank_item["amount"]), Decimal("500.00"))

    def test_debt_pay_endpoint_unknown_fallback_to_transfer(self):
        """
        POST /main/debts/{id}/pay/ with unrecognized payment_method
        - Mapped to 'transfer' (no 400 error)
        - affects_shift_drawer=False
        """
        shift = CashShift.objects.create(
            company=self.company,
            branch=self.branch,
            cashbox=self.pos_cashbox,
            cashier=self.user,
            opening_cash=Decimal("200.00"),
            status=CashShift.Status.OPEN,
        )

        debt = Debt.objects.create(
            company=self.company,
            branch=self.branch,
            name="Bob Fallback",
            phone="+996555777888",
            amount=Decimal("700.00"),
        )

        url = f"/main/debts/{debt.id}/pay/"
        payload = {
            "amount": "300.00",
            "payment_method": "unsupported_pay",
            "cashbox_id": str(self.pos_cashbox.id),
            "shift_id": str(shift.id),
        }
        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        pm = DebtPayment.objects.filter(debt=debt).first()
        self.assertIsNotNone(pm)
        self.assertEqual(pm.amount, Decimal("300.00"))
        self.assertEqual(pm.payment_method, "transfer")

        cf = CashFlow.objects.filter(source_kind=CashFlow.SourceKind.DEBT_REPAYMENT, source_id=str(debt.id)).first()
        self.assertIsNotNone(cf)
        self.assertEqual(cf.payment_method, "transfer")
        self.assertFalse(cf.affects_shift_drawer)

        cf.status = CashFlow.Status.APPROVED
        cf.save(update_fields=["status"])

        totals = shift.calc_live_totals()
        self.assertEqual(totals["drawer_expected_cash"], Decimal("200.00"))

    def test_client_deal_pay_v2_with_bank_method(self):
        """
        POST /main/clients/{clientId}/deals/{dealId}/pay/
        - Accepts payment_method='optima'
        - Creates DealPayment with payment_method='optima'
        - Auto CashFlow has payment_method='optima', affects_shift_drawer=False
        """
        client_obj = Client.objects.create(
            company=self.company,
            full_name="Deal Client",
            phone="+996777000111",
        )
        deal = ClientDeal.objects.create(
            company=self.company,
            branch=self.branch,
            client=client_obj,
            title="Debt Deal",
            kind=ClientDeal.Kind.DEBT,
            amount=Decimal("1000.00"),
            debt_months=1,
        )
        inst = deal.installments.first()
        if not inst:
            inst = DealInstallment.objects.create(
                company=self.company,
                branch=self.branch,
                deal=deal,
                number=1,
                amount=Decimal("1000.00"),
                paid_amount=Decimal("0.00"),
                due_date=date.today(),
                balance_after=Decimal("1000.00"),
            )

        shift = CashShift.objects.create(
            company=self.company,
            branch=self.branch,
            cashbox=self.pos_cashbox,
            cashier=self.user,
            opening_cash=Decimal("1000.00"),
            status=CashShift.Status.OPEN,
        )

        idem = str(uuid.uuid4())
        url = f"/main/clients/{client_obj.id}/deals/{deal.id}/pay/"
        payload = {
            "installment_id": str(inst.id),
            "amount": "400.00",
            "idempotency_key": idem,
            "payment_method": "optima",
            "cashbox_id": str(self.pos_cashbox.id),
            "shift_id": str(shift.id),
        }
        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        payment = DealPayment.objects.filter(deal=deal, idempotency_key=idem).first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.payment_method, "optima")
        self.assertEqual(payment.amount, Decimal("400.00"))

        cf = CashFlow.objects.filter(source_kind=CashFlow.SourceKind.DEBT_REPAYMENT, source_id=str(deal.id)).first()
        self.assertIsNotNone(cf)
        self.assertEqual(cf.payment_method, "optima")
        self.assertFalse(cf.affects_shift_drawer)

        cf.status = CashFlow.Status.APPROVED
        cf.save(update_fields=["status"])

        totals = shift.calc_live_totals()
        self.assertEqual(totals["drawer_expected_cash"], Decimal("1000.00"))

    def test_cashflow_endpoint_and_alias_accept_payment_method(self):
        """
        POST /main/cashflows/ alias and POST /construction/cashflows/
        accept payment_method and store it on CashFlow.
        """
        url_main = "/main/cashflows/"
        payload_main = {
            "name": "Возврат долга (МБанк)",
            "amount": "150.00",
            "type": "income",
            "payment_method": "mbank",
            "cashbox": str(self.pos_cashbox.id),
        }
        res_main = self.client.post(url_main, payload_main, format="json")
        self.assertEqual(res_main.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_main.data.get("payment_method"), "mbank")

        url_const = "/construction/cashflows/"
        payload_const = {
            "name": "Возврат долга (Бакай)",
            "amount": "250.00",
            "type": "income",
            "payment_method": "bakai",
            "cashbox": str(self.pos_cashbox.id),
        }
        res_const = self.client.post(url_const, payload_const, format="json")
        self.assertEqual(res_const.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_const.data.get("payment_method"), "bakai")
