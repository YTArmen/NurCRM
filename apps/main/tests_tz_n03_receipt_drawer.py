"""ТЗ Н-03 (23.10.2026): приход «оплачено из ящика смены» — один расход со сменой."""
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.main.models import Client, Product
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()


class ReceiptFromShiftDrawerTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="Co", owner=self.owner, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.api.force_authenticate(user=self.owner)
        self.supplier = Client.objects.create(full_name="Пост", type=Client.StatusClient.SUPPLIERS, company=self.company)
        self.product = Product.objects.create(
            name="Тест", company=self.company, quantity=Decimal("5"),
            purchase_price=Decimal("1.00"), price=Decimal("2.00"),
        )
        self.cashbox = Cashbox.objects.create(name="Касса 1", company=self.company)
        self.shift = CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=self.owner, status=CashShift.Status.OPEN,
        )

    def post(self, extra):
        return self.api.post(
            f"/api/main/suppliers/{self.supplier.id}/receipt/",
            {"items": [{"product": str(self.product.id), "qty": 1, "purchase_price": "1.00"}],
             "payment_type": "cash", **extra},
            format="json",
        )

    def test_one_expense_in_shift(self):
        res = self.post({"shift": str(self.shift.id)})
        self.assertEqual(res.status_code, 200, res.data)
        flows = CashFlow.objects.filter(source_id=res.data["id"])
        self.assertEqual(flows.count(), 1)
        cf = flows.get()
        self.assertEqual(cf.shift_id, self.shift.id)
        self.assertEqual(cf.cashbox_id, self.cashbox.id)
        self.assertTrue(cf.affects_shift_drawer)
        self.assertEqual(cf.amount, Decimal("1.00"))
        self.assertEqual(res.data["cashflow_id"], str(cf.id))
        self.assertEqual(res.data["shift"], str(self.shift.id))

    def test_closed_shift_rejected_and_nothing_written(self):
        self.shift.status = CashShift.Status.CLOSED
        self.shift.save(update_fields=["status"])
        res = self.post({"shift": str(self.shift.id)})
        self.assertEqual(res.status_code, 400, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("5"))

    def test_foreign_shift_rejected(self):
        cashier = User.objects.create_user(email=f"c_{uuid.uuid4().hex[:6]}@t.com", password="p", role="cashier")
        cashier.company = self.company
        cashier.save()
        other = User.objects.create_user(email=f"x_{uuid.uuid4().hex[:6]}@t.com", password="p", role="cashier")
        other.company = self.company
        other.save()
        self.shift.cashier = other
        self.shift.save(update_fields=["cashier"])
        self.api.force_authenticate(user=cashier)
        res = self.post({"shift": str(self.shift.id)})
        self.assertIn(res.status_code, (403, 400), res.data)
        self.assertFalse(CashFlow.objects.filter(source_kind=CashFlow.SourceKind.PROCUREMENT_RECEIPT).exists())

    def test_codes_and_bad_cashbox(self):
        # Б-05: коды shift_closed / cashbox_not_found, а не общий 400.
        self.shift.status = CashShift.Status.CLOSED
        self.shift.save(update_fields=["status"])
        res = self.post({"shift": str(self.shift.id)})
        self.assertEqual(res.data.get("code"), "shift_closed", res.data)
        res = self.post({"cashbox": str(uuid.uuid4())})
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data.get("code"), "cashbox_not_found", res.data)
        self.assertFalse(CashFlow.objects.filter(source_kind=CashFlow.SourceKind.PROCUREMENT_RECEIPT).exists())

    def test_find_doubled_receipt_expenses_command(self):
        """Б-05: команда показывает пару «расход прихода + второй расход той же суммы» и ничего не удаляет."""
        from datetime import timedelta
        from io import StringIO

        from django.core.management import call_command
        from django.utils import timezone

        res = self.post({"shift": str(self.shift.id)})
        self.assertEqual(res.status_code, 200, res.data)
        first = CashFlow.objects.get(source_id=res.data["id"])
        # «сайт» дописал второй расход той же суммы через 2 секунды (без смены)
        dup = CashFlow.objects.create(company=self.company, cashbox=self.cashbox, type=CashFlow.Type.EXPENSE,
                                      amount=first.amount, name="Приход поставщика", status=CashFlow.Status.APPROVED)
        CashFlow.objects.filter(pk=dup.pk).update(created_at=first.created_at + timedelta(seconds=2))
        # расход той же суммы через минуту — не дубль
        late = CashFlow.objects.create(company=self.company, cashbox=self.cashbox, type=CashFlow.Type.EXPENSE,
                                       amount=first.amount, name="Другой", status=CashFlow.Status.APPROVED)
        CashFlow.objects.filter(pk=late.pk).update(created_at=timezone.now() + timedelta(minutes=5))
        out = StringIO()
        call_command("find_doubled_receipt_expenses", company=str(self.company.id), stdout=out)
        text = out.getvalue()
        self.assertIn("Найдено пар: 1", text)
        self.assertIn(str(first.id), text)
        self.assertIn(str(dup.id), text)
        self.assertEqual(CashFlow.objects.filter(company=self.company).count(), 3)  # ничего не удалено
