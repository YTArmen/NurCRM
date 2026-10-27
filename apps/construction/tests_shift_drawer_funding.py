"""
«Оплачено из кассы (ящика) смены» — приёмка по shift-drawer-funding-purchases-backend.md §8.

Кассир платит поставщику наличными из своего ящика. Сама закупка уходит на
кассу «Переменные расходы» как обычно, а факт изъятия наличных фиксируется
отдельным движением на POS-кассе смены — и должен уменьшать drawer_expected_cash.
"""
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.main.models import Branch, Sale
from apps.users.models import Company

User = get_user_model()

URL = "/api/construction/cashflows/"


class ShiftDrawerFundingTests(TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex[:6]
        self.owner = User.objects.create_user(
            email=f"owner_sdf_{suffix}@test.com", password="pass", role="owner"
        )
        self.company = Company.objects.create(name=f"SDF Co {suffix}", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()

        self.branch = Branch.objects.create(name="Main", company=self.company)
        self.pos = Cashbox.objects.create(
            name="Касса смены", role=Cashbox.CashboxRole.POS_MAIN,
            company=self.company, branch=self.branch,
        )
        self.cashier = User.objects.create_user(
            email=f"cashier_sdf_{suffix}@test.com", password="pass",
            company=self.company, first_name="Кассир", last_name="Тестовый",
        )
        self.shift = CashShift.objects.create(
            company=self.company, branch=self.branch, cashbox=self.pos,
            cashier=self.cashier, opening_cash=Decimal("50.00"),
            status=CashShift.Status.OPEN,
        )
        Sale.objects.create(
            company=self.company, branch=self.branch, cashbox=self.pos, shift=self.shift,
            user=self.cashier, total=Decimal("2000.00"),
            payment_method=Sale.PaymentMethod.CASH, status=Sale.Status.PAID,
        )

        self.api = APIClient()
        self.api.force_authenticate(self.cashier)

    def _payload(self, **over):
        data = {
            "cashbox": str(self.pos.id),
            "type": "expense",
            "amount": "1000.00",
            "name": "Приход поставщика — оплата из кассы смены: ООО Ромашка",
            "source_kind": CashFlow.SourceKind.SHIFT_DRAWER_OUTFLOW,
        }
        data.update(over)
        return data

    def _drawer(self):
        return CashShift.objects.get(pk=self.shift.pk).calc_live_totals()["drawer_expected_cash"]

    # ── §8, кейсы 1–2 ──
    def test_outflow_binds_shift_and_reduces_drawer(self):
        self.assertEqual(self._drawer(), Decimal("2050.00"))  # 50 размен + 2000 наличных

        resp = self.api.post(URL, self._payload(), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

        cf = CashFlow.objects.get(pk=resp.data["id"])
        self.assertEqual(cf.shift_id, self.shift.id)          # смена подставлена сама
        self.assertTrue(cf.affects_shift_drawer)              # флаг выставлен save()
        self.assertEqual(cf.cashier_id, self.cashier.id)

        self.assertEqual(self._drawer(), Decimal("1050.00"))  # 2050 − 1000

    # ── §8, кейс 3 ──
    def test_plain_manual_expense_does_not_touch_drawer(self):
        resp = self.api.post(
            URL,
            {"cashbox": str(self.pos.id), "type": "expense", "amount": "1000.00", "name": "Просто расход"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        cf = CashFlow.objects.get(pk=resp.data["id"])
        self.assertIsNone(cf.shift_id)
        self.assertFalse(cf.affects_shift_drawer)
        self.assertEqual(self._drawer(), Decimal("2050.00"))

    # ── §8, кейс 4 ──
    def test_no_open_shift_is_rejected(self):
        self.shift.status = CashShift.Status.CLOSED
        self.shift.save(update_fields=["status"])
        resp = self.api.post(URL, self._payload(), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("shift", resp.data)

    # ── Б-02: старая касса не должна повторять очередь вечно — 400 с кодом, не 403 ──
    def test_foreign_shift_is_400_shift_not_owned(self):
        other = User.objects.create_user(
            email=f"other2_{uuid.uuid4().hex[:6]}@test.com", password="pass", company=self.company
        )
        self.api.force_authenticate(other)
        resp = self.api.post(URL, self._payload(shift=str(self.shift.id)), format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["code"], "shift_not_owned", resp.data)
        self.assertEqual(list(resp.data.keys())[0], "detail")

    def test_closed_shift_is_400_shift_closed(self):
        self.shift.status = CashShift.Status.CLOSED
        self.shift.save(update_fields=["status"])
        resp = self.api.post(URL, self._payload(shift=str(self.shift.id)), format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["code"], "shift_closed")

    # ── §8, кейс 5 ──
    def test_several_open_shifts_require_explicit_shift(self):
        other = User.objects.create_user(
            email=f"other_{uuid.uuid4().hex[:6]}@test.com", password="pass", company=self.company
        )
        CashShift.objects.create(
            company=self.company, branch=self.branch, cashbox=self.pos,
            cashier=other, opening_cash=Decimal("0.00"), status=CashShift.Status.OPEN,
        )
        self.api.force_authenticate(self.owner)
        resp = self.api.post(URL, self._payload(), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("несколько смен", str(resp.data["shift"]))

        # с явной сменой — проходит и списывает из нужного ящика
        resp = self.api.post(URL, self._payload(shift=str(self.shift.id)), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(self._drawer(), Decimal("1050.00"))

    # ── §8, кейс 6 ──
    def test_pending_outflow_is_not_counted_until_approved(self):
        self.company.cashflow_requests_enabled = True
        self.company.save(update_fields=["cashflow_requests_enabled"])

        resp = self.api.post(URL, self._payload(status="pending"), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        cf = CashFlow.objects.get(pk=resp.data["id"])
        self.assertEqual(cf.status, CashFlow.Status.PENDING)
        self.assertEqual(self._drawer(), Decimal("2050.00"))  # ещё не учтено

        cf.status = CashFlow.Status.APPROVED
        cf.save(update_fields=["status"])
        self.assertEqual(self._drawer(), Decimal("1050.00"))  # после одобрения — учтено

    # ── Q3: связь с исходной операцией ──
    def test_source_id_links_to_business_operation(self):
        op_id = str(uuid.uuid4())
        resp = self.api.post(URL, self._payload(source_id=op_id), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        cf = CashFlow.objects.get(pk=resp.data["id"])
        self.assertEqual(cf.source_id, op_id)
        self.assertEqual(cf.source_kind, CashFlow.SourceKind.SHIFT_DRAWER_OUTFLOW)

    # ── защита от неверного типа ──
    def test_income_with_this_source_kind_is_rejected(self):
        resp = self.api.post(URL, self._payload(type="income"), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("type", resp.data)

    # ── Q1: id кассы уже есть в ответе смены ──
    def test_shift_response_exposes_cashbox_id(self):
        self.api.force_authenticate(self.owner)
        d = self.api.get(f"/api/construction/shifts/{self.shift.id}/").data
        self.assertEqual(str(d["cashbox"]), str(self.pos.id))
        self.assertEqual(d["resolved_cashbox_id"], str(self.pos.id))
