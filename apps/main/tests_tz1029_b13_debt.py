"""ТЗ 29.10 Б-13: оплата долга вне смены, коды ошибок, ответ с received_by/shift/cashbox/applied."""
import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.main.models import Client, ClientDeal, Product, Sale
from apps.users.models import Branch, Company, User

STRICT = {"HTTP_X_NURMARKET_CLIENT": "kassa/1.17.61"}
OWNER_APP = {"HTTP_X_NURMARKET_CLIENT": "owner/1.17.61"}  # программа владельца


class B13Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"b13-{uuid.uuid4().hex}@t.com", password="x")
        self.company = Company.objects.create(name="B13", owner=self.owner)
        self.owner.company = self.company
        self.branch = Branch.objects.create(company=self.company, name="Main")
        self.owner.branch = self.branch
        self.owner.save()
        # «Основную кассу компании» создаёт сама компания (role пустая, branch пустой)
        self.main_box = Cashbox.objects.get(company=self.company, name="Основная касса компании")
        self.pos_box = Cashbox.objects.create(
            company=self.company, branch=self.branch, name="POS", role=Cashbox.CashboxRole.POS_BRANCH
        )
        self.cashier = User.objects.create_user(
            email=f"c-{uuid.uuid4().hex}@t.com", password="x", company=self.company, role="cashier"
        )
        self.client_obj = Client.objects.create(
            company=self.company, branch=self.branch, full_name="Должник", phone="+996700000009",
            salesperson=self.cashier,
        )
        self.sale = Sale.objects.create(
            company=self.company, branch=self.branch, cashbox=self.pos_box, user=self.owner, client=self.client_obj,
            payment_method="debt", status=Sale.Status.DEBT, subtotal=Decimal("100.00"), total=Decimal("100.00"),
            debt_initial=Decimal("100.00"), debt_remaining=Decimal("100.00"),
        )
        self.deal = ClientDeal.objects.create(
            company=self.company, branch=self.branch, client=self.client_obj, sale=self.sale,
            kind=ClientDeal.Kind.DEBT, title="Долг", amount=Decimal("100.00"), debt_months=1,
            first_due_date=date.today(),
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.capi = APIClient()
        self.capi.force_authenticate(self.cashier)

    def shift(self, user, box=None):
        return CashShift.objects.create(
            company=self.company, cashbox=box or self.pos_box, cashier=user,
            status=CashShift.Status.OPEN, opening_cash=Decimal("0.00"),
        )

    def expected(self, shift):
        return shift.calc_live_totals(refresh=True)["expected_cash"]

    def pay(self, api=None, key=None, hdr=None, **body):
        body.setdefault("amount", "30.00")
        return (api or self.api).post(
            f"/api/main/clients/{self.client_obj.id}/pay-debt/", body, format="json",
            HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()), **(hdr or {}),
        )


class ClientPayDebtOutOfShiftTests(B13Base):
    def test_no_shift_cash_does_not_touch_open_shift(self):
        sh = self.shift(self.owner)
        before = self.expected(sh)
        r = self.pay(key="K1", hdr=OWNER_APP)
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual((d["paid"], d["left"]), ("30.00", "70.00"))
        self.assertEqual(d["received_by"]["id"], str(self.owner.pk))
        self.assertIsNone(d["shift"])
        self.assertEqual(d["cashbox"], str(self.main_box.id))
        self.assertEqual((d["method"], d["payment_method"]), ("cash", "cash"))
        self.assertFalse(d["replayed"])
        self.assertEqual(self.expected(sh), before)
        cf = CashFlow.objects.get(source_kind=CashFlow.SourceKind.DEBT_REPAYMENT)
        self.assertIsNone(cf.shift_id)
        self.assertEqual(cf.type, CashFlow.Type.INCOME)
        self.assertEqual(cf.cashbox_id, self.main_box.id)

    def test_owner_at_kassa_with_open_shift_goes_to_shift(self):
        # Владелец сам стоит за кассой: старая касса (без заголовка) и новая касса shift не шлют —
        # наличные идут в его открытую смену, как раньше.
        sh = self.shift(self.owner)
        before = self.expected(sh)
        self.assertEqual(self.pay(amount="10.00").status_code, 200)
        self.assertEqual(self.expected(sh), before + 10)
        r = self.pay(amount="5.00", hdr=STRICT)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["shift"], str(sh.id))
        self.assertEqual(self.expected(sh), before + 15)

    def test_applied_has_sale_number_and_debt_left(self):
        d = self.pay().json()
        a = d["applied"][0]
        self.assertEqual(a["sale"], str(self.sale.id))
        self.assertEqual(a["sale_number"], self.sale.doc_number)
        self.assertEqual(a["amount"], "30.00")
        self.assertEqual(a["sale_debt_left"], "70.00")

    def test_replay_after_shift_closed(self):
        sh = self.shift(self.owner)
        first = self.pay(key="K2", shift=str(sh.id)).json()
        sh.status = CashShift.Status.CLOSED
        sh.save(update_fields=["status"])
        again = self.pay(key="K2", shift=str(sh.id))
        self.assertEqual(again.status_code, 200, again.content)
        j = again.json()
        self.assertTrue(j["replayed"])
        self.assertEqual(j["paid"], "30.00")
        self.assertEqual(j["shift"], first["shift"])
        self.assertEqual(j["applied"][0]["amount"], "30.00")
        self.assertEqual(ClientDeal.objects.get(pk=self.deal.pk).remaining_debt, Decimal("70.00"))  # не удвоилось

    def test_cashier_with_shift_still_goes_to_shift(self):
        sh = self.shift(self.cashier)
        before = self.expected(sh)
        r = self.pay(api=self.capi, shift=str(sh.id))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["shift"], str(sh.id))
        self.assertEqual(self.expected(sh), before + Decimal("30.00"))

    def test_cashier_without_shift_param_uses_own_open_shift(self):
        sh = self.shift(self.cashier)
        r = self.pay(api=self.capi)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["shift"], str(sh.id))

    def test_transfer_with_shift_not_in_expected_cash(self):
        sh = self.shift(self.owner)
        before = self.expected(sh)
        r = self.pay(method="transfer", amount="10.00", shift=str(sh.id))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["shift"], str(sh.id))
        self.assertEqual(self.expected(sh), before)

    def test_explicit_cashbox_out_of_shift(self):
        other = Cashbox.objects.create(company=self.company, name="Сейф-2", role=Cashbox.CashboxRole.POS_MAIN)
        r = self.pay(cashbox=str(other.id))
        self.assertEqual(r.json()["cashbox"], str(other.id))
        self.assertIsNone(r.json()["shift"])


class ClientPayDebtCodesTests(B13Base):
    def test_amount_exceeds_debt(self):
        self.deal.installments.update(paid_amount=Decimal("90.00"))
        r = self.pay(amount="500.00")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "amount_exceeds_debt")
        self.assertEqual(r.json()["data"]["max"], "10.00")

    def test_no_debt(self):
        other = Client.objects.create(company=self.company, branch=self.branch, full_name="Чистый", phone="+996700000010")
        r = self.api.post(f"/api/main/clients/{other.id}/pay-debt/", {"amount": "5.00"}, format="json",
                          HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "no_debt")

    def test_amount_invalid(self):
        r = self.pay(amount="0")
        self.assertEqual(r.status_code, 400)
        self.assertIn(r.json()["code"], ("amount_invalid", "validation_error"))

    def test_field_conflict(self):
        r = self.pay(method="cash", payment_method="transfer")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "field_conflict")

    def test_shift_codes(self):
        r = self.pay(shift=str(uuid.uuid4()))
        self.assertEqual(r.json()["code"], "shift_not_found")
        sh = self.shift(self.owner)
        sh.status = CashShift.Status.CLOSED
        sh.save(update_fields=["status"])
        self.assertEqual(self.pay(shift=str(sh.id)).json()["code"], "shift_closed")
        owners = self.shift(self.owner)
        r = self.pay(api=self.capi, shift=str(owners.id))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "shift_not_owned")

    def test_cashbox_not_found(self):
        self.assertEqual(self.pay(cashbox=str(uuid.uuid4())).json()["code"], "cashbox_not_found")

    def test_shift_required_for_cashier_strict_only(self):
        r = self.pay(api=self.capi, hdr=STRICT)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "shift_required")
        # владельцу смена не нужна
        self.assertEqual(self.pay(hdr=STRICT).status_code, 200)
        # старый клиент без заголовка: прежнее поведение, ошибки нет
        self.assertEqual(self.pay(api=self.capi).status_code, 200)

    def test_method_not_allowed_strict_only(self):
        r = self.pay(method="bitcoin", hdr=STRICT)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "method_not_allowed")
        self.assertIn("cash", r.json()["data"]["allowed"])
        # старый клиент: тихая замена на transfer, как раньше
        r = self.pay(method="bitcoin")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["payment_method"], "transfer")


class PayAnyAndSalePayDebtTests(B13Base):
    def test_pay_any_out_of_shift_and_fields(self):
        sh = self.shift(self.owner)
        before = self.expected(sh)
        r = self.api.post(
            f"/api/main/clients/{self.client_obj.id}/deals/pay-any/",
            {"amount": "30.00", "method": "cash", "idempotency_key": str(uuid.uuid4())}, format="json", **OWNER_APP,
        )
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual(d["paid_total"], "30.00")  # прежнее поле
        self.assertEqual(d["paid"], "30.00")
        self.assertIsNone(d["shift"])
        self.assertEqual(d["received_by"]["id"], str(self.owner.pk))
        self.assertEqual(self.expected(sh), before)

    def test_pay_any_field_conflict_and_replay_after_close(self):
        url = f"/api/main/clients/{self.client_obj.id}/deals/pay-any/"
        r = self.api.post(url, {"amount": "5.00", "method": "cash", "payment_method": "transfer",
                                "idempotency_key": str(uuid.uuid4())}, format="json")
        self.assertEqual(r.json()["code"], "field_conflict")
        sh = self.shift(self.owner)
        body = {"amount": "30.00", "shift": str(sh.id), "idempotency_key": str(uuid.uuid4())}
        self.assertEqual(self.api.post(url, body, format="json").status_code, 200)
        sh.status = CashShift.Status.CLOSED
        sh.save(update_fields=["status"])
        r = self.api.post(url, body, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["replayed"])

    def _sale_pay(self, api=None, hdr=None, **body):
        body.setdefault("amount", "30.00")
        body.setdefault("payment_method", "cash")
        return (api or self.api).post(
            f"/api/main/pos/sales/{self.sale.id}/pay-debt/", body, format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()), **(hdr or {}),
        )

    def test_sale_pay_debt_out_of_shift(self):
        sh = self.shift(self.owner)
        before = self.expected(sh)
        r = self._sale_pay(hdr=OWNER_APP)
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertIsNone(d["shift"])
        self.assertEqual(d["cashbox"], str(self.main_box.id))
        self.assertEqual(d["applied"][0]["sale_debt_left"], "70.00")
        self.assertEqual(d["applied"][0]["sale_number"], self.sale.doc_number)
        self.assertEqual(self.expected(sh), before)

    def test_sale_pay_debt_cashier_codes(self):
        r = self._sale_pay(api=self.capi, hdr=STRICT)
        self.assertEqual(r.json()["code"], "shift_required")
        r = self._sale_pay(api=self.capi)
        self.assertEqual(r.json()["code"], "shift_not_open")  # старый клиент: прежний код
        sh = self.shift(self.cashier)
        self.assertEqual(self._sale_pay(api=self.capi).status_code, 200)
        sh.status = CashShift.Status.CLOSED
        sh.save(update_fields=["status"])
        r = self._sale_pay(api=self.capi, shift=str(sh.id))
        self.assertEqual(r.json()["code"], "shift_closed")
        r = self._sale_pay(api=self.capi, shift=str(uuid.uuid4()))
        self.assertEqual(r.json()["code"], "shift_not_found")


class CheckoutShiftCodesTests(B13Base):
    def _checkout(self, api, hdr=None, **extra):
        p = Product.objects.create(company=self.company, name="Т", price=Decimal("10.00"), quantity=Decimal("50"))
        body = {"items": [{"product": str(p.id), "qty": "1", "price": "10.00"}],
                "payment": {"method": "cash", "received": "10.00"}}
        body.update(extra)
        return api.post("/api/main/pos/checkout/", body, format="json",
                        HTTP_IDEMPOTENCY_KEY=f"k-{uuid.uuid4()}", **(hdr or {}))

    def test_closed_shift_code(self):
        sh = self.shift(self.cashier)
        sh.status = CashShift.Status.CLOSED
        sh.save(update_fields=["status"])
        for hdr in (None, STRICT):
            r = self._checkout(self.capi, hdr, shift=str(sh.id))
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["code"], "shift_closed")

    def test_foreign_shift_code_and_status(self):
        foreign = self.shift(self.owner)
        r = self._checkout(self.capi, None, shift=str(foreign.id))
        self.assertEqual(r.status_code, 400)  # старый клиент: статус прежний
        self.assertEqual(r.json()["code"], "shift_not_owned")
        r = self._checkout(self.capi, STRICT, shift=str(foreign.id))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "shift_not_owned")
