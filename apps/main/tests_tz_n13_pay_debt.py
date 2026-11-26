"""ТЗ Н-13: оплата долга — method/payment_method, received_by/shift/cashbox, коды ошибок."""
import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashShift
from apps.main.models import Client, ClientDeal, DealInstallment, Sale
from apps.users.models import Branch, Company, User


class TzN13PayDebtTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email=f"n13-{uuid.uuid4().hex}@test.com", password="x")
        self.company = Company.objects.create(name="N13", owner=self.user)
        self.user.company = self.company
        self.branch = Branch.objects.create(company=self.company, name="Main")
        self.user.branch = self.branch
        self.user.save()
        self.cashbox = Cashbox.objects.create(
            company=self.company, branch=self.branch, name="POS", role=Cashbox.CashboxRole.POS_BRANCH
        )
        self.client_obj = Client.objects.create(company=self.company, branch=self.branch, full_name="Должник", phone="+996700000001")
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.sale = Sale.objects.create(
            company=self.company, branch=self.branch, cashbox=self.cashbox, user=self.user, client=self.client_obj,
            payment_method="debt", status=Sale.Status.DEBT, subtotal=Decimal("100.00"), total=Decimal("100.00"),
            debt_initial=Decimal("100.00"), debt_remaining=Decimal("100.00"),
        )
        self.deal = ClientDeal.objects.create(
            company=self.company, branch=self.branch, client=self.client_obj, sale=self.sale,
            kind=ClientDeal.Kind.DEBT, title="Долг", amount=Decimal("100.00"), debt_months=1,
            first_due_date=date.today(),
        )

    def _open_shift(self):
        return CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=self.user,
            status=CashShift.Status.OPEN, opening_cash=Decimal("0.00"),
        )

    def _client_pay(self, **body):
        body.setdefault("amount", "30.00")
        return self.api.post(
            f"/api/main/clients/{self.client_obj.id}/pay-debt/", body, format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

    def _sale_pay(self, **body):
        body.setdefault("amount", "30.00")
        return self.api.post(
            f"/api/main/pos/sales/{self.sale.id}/pay-debt/", body, format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

    def test_client_pay_debt_accepts_payment_method_and_echoes_both(self):
        shift = self._open_shift()
        r = self._client_pay(payment_method="transfer", shift=str(shift.id))
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual(d["method"], "transfer")
        self.assertEqual(d["payment_method"], "transfer")
        self.assertEqual(d["received_by"]["id"], str(self.user.pk))
        self.assertTrue(d["received_at"])
        self.assertEqual(d["shift"], str(shift.id))
        self.assertEqual(d["cashbox"], str(self.cashbox.id))

    def test_client_pay_debt_method_default_cash(self):
        r = self._client_pay(cashbox=str(self.cashbox.id))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["method"], "cash")
        self.assertIsNone(r.json()["shift"])
        self.assertEqual(r.json()["cashbox"], str(self.cashbox.id))

    def test_client_pay_debt_field_conflict(self):
        r = self._client_pay(method="cash", payment_method="transfer")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "field_conflict")

    def test_client_pay_debt_same_values_ok(self):
        r = self._client_pay(method="cash", payment_method="cash", cashbox=str(self.cashbox.id))
        self.assertEqual(r.status_code, 200, r.content)

    def test_client_pay_debt_error_codes(self):
        r = self._client_pay(amount="500.00", cashbox=str(self.cashbox.id))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "amount_exceeds_debt")
        self.assertEqual(r.json()["data"]["max"], "100.00")
        r = self._client_pay(cashbox=str(uuid.uuid4()))
        self.assertEqual(r.json()["code"], "cashbox_not_found")
        shift = self._open_shift()
        shift.status = CashShift.Status.CLOSED
        shift.save(update_fields=["status"])
        r = self._client_pay(shift=str(shift.id))
        self.assertEqual(r.json()["code"], "shift_closed")
        self.deal.installments.update(paid_amount=Decimal("100.00"))
        r = self._client_pay()
        self.assertEqual(r.json()["code"], "no_debt")

    def test_client_pay_debt_replay_keeps_receipt_info(self):
        shift = self._open_shift()
        key = str(uuid.uuid4())
        url = f"/api/main/clients/{self.client_obj.id}/pay-debt/"
        body = {"amount": "30.00", "shift": str(shift.id)}
        first = self.api.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY=key).json()
        again = self.api.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY=key).json()
        self.assertTrue(again["replayed"])
        self.assertEqual(again["received_by"], first["received_by"])
        self.assertEqual(again["shift"], str(shift.id))

    def test_sale_pay_debt_accepts_method_name(self):
        shift = self._open_shift()
        r = self._sale_pay(method="transfer", shift=str(shift.id))
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual(d["method"], "transfer")
        self.assertEqual(d["payment_method"], "transfer")
        self.assertEqual(d["received_by"]["id"], str(self.user.pk))
        self.assertEqual(d["shift"], str(shift.id))
        self.assertEqual(d["cashbox"], str(self.cashbox.id))
        self.assertEqual(d["left"], "70.00")

    def test_sale_pay_debt_card_is_transfer_and_conflict(self):
        shift = self._open_shift()
        r = self._sale_pay(method="card", shift=str(shift.id))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["payment_method"], "transfer")
        r = self._sale_pay(method="cash", payment_method="transfer")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "field_conflict")

    def test_sale_pay_debt_error_codes(self):
        r = self._sale_pay(method="bitcoin")
        self.assertEqual(r.json()["code"], "method_not_allowed")
        self.assertIn("cash", r.json()["data"]["allowed"])
        r = self._sale_pay(method="transfer", amount="-5")
        self.assertEqual(r.json()["code"], "amount_invalid")
        r = self._sale_pay(method="transfer", amount="999.00")
        self.assertEqual(r.json()["code"], "amount_exceeds_debt")
        self.assertEqual(r.json()["data"]["max"], "100.00")
        r = self._sale_pay(method="transfer", cashbox_id=str(uuid.uuid4()))
        self.assertEqual(r.json()["code"], "cashbox_not_found")
        # ТЗ 29.10 Б-13: владелец без shift принимает наличные вне смены (раньше было shift_not_open)
        r = self._sale_pay(method="cash")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(r.json()["shift"])

    def test_sale_pay_debt_other_cashier_shift_forbidden(self):
        other = User.objects.create_user(email=f"o-{uuid.uuid4().hex}@test.com", password="x", company=self.company, role="cashier")
        shift = CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=other,
            status=CashShift.Status.OPEN, opening_cash=Decimal("0.00"),
        )
        api = APIClient()
        # третий кассир чужую смену использовать не может
        third = User.objects.create_user(email=f"t-{uuid.uuid4().hex}@test.com", password="x", company=self.company, role="cashier")
        api.force_authenticate(third)
        r = api.post(
            f"/api/main/pos/sales/{self.sale.id}/pay-debt/",
            {"amount": "10.00", "method": "transfer", "shift": str(shift.id)}, format="json",
        )
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(r.json()["code"], "shift_not_owned")
