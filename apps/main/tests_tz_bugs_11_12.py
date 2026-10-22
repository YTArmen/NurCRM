"""ТЗ 11–12 (22.10.2026): мелкие серверные ошибки, найденные при проверке сайта."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.construction.models import Cashbox, CashFlow
from apps.main.models import Client, ClientDeal, Sale
from apps.main.tests_kassa_api import KassaBase


class SalesDateToTests(KassaBase):
    """11.1: date_to=YYYY-MM-DD включает весь последний день."""

    def test_bare_date_to_includes_whole_day(self):
        today = timezone.localdate()
        morning = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time())) + timedelta(hours=3)
        evening = morning.replace(hour=23, minute=40)
        s1 = Sale.objects.create(company=self.company, user=self.owner, cashbox=self.cashbox, status=Sale.Status.PAID)
        s2 = Sale.objects.create(company=self.company, user=self.owner, cashbox=self.cashbox, status=Sale.Status.PAID)
        Sale.objects.filter(pk=s1.pk).update(created_at=morning)
        Sale.objects.filter(pk=s2.pk).update(created_at=evening)
        r = self.api.get("/api/main/pos/sales/", {"date_from": today.isoformat(), "date_to": today.isoformat()})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual({row["id"] for row in r.data["results"]}, {str(s1.id), str(s2.id)})


class PayDebtSupplierTests(KassaBase):
    """11.2: оплата долга поставщику через pay-debt - расход."""

    def _pay(self, client):
        deal = ClientDeal.objects.create(
            company=self.company, client=client, title="Долг", kind=ClientDeal.Kind.DEBT,
            amount=Decimal("500.00"), debt_months=1, first_due_date=date.today(),
        )
        cb = Cashbox.objects.create(name="Расходы", company=self.company, role=Cashbox.CashboxRole.EXPENSE_VARIABLE)
        r = self.api.post(
            f"/api/main/clients/{client.id}/pay-debt/",
            {"amount": "200.00", "idempotency_key": str(uuid.uuid4()), "cashbox_id": str(cb.id)},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        return deal

    def test_supplier_creates_expense(self):
        sup = Client.objects.create(company=self.company, full_name="Пост", type=Client.StatusClient.SUPPLIERS)
        self._pay(sup)
        cf = CashFlow.objects.get(source_kind=CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT)
        self.assertEqual(cf.type, CashFlow.Type.EXPENSE)
        self.assertEqual(cf.amount, Decimal("200.00"))

    def test_customer_still_income(self):
        self._pay(self.client_obj)
        cf = CashFlow.objects.get(source_kind=CashFlow.SourceKind.DEBT_REPAYMENT)
        self.assertEqual(cf.type, CashFlow.Type.INCOME)


class CheckoutAliasTests(KassaBase):
    """11.5 quantity как qty; 12.9 cash_received как payment.received."""

    def test_quantity_alias(self):
        r = self.quick(items=[{"product": str(self.product.id), "quantity": "3", "price": "100.00"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["total"], "300.00")

    def test_missing_qty_is_400(self):
        r = self.quick(items=[{"product": str(self.product.id), "price": "100.00"}])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 0)

    def test_cash_received_alias(self):
        r = self.quick(payment={"method": "cash", "cash_received": "500.00"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["change"], "300.00")


class ProductPatchCharacteristicsTests(KassaBase):
    """11.6: PATCH products/{id}/ принимает characteristics."""

    def test_patch_characteristics(self):
        url = f"/api/main/products/{self.product.id}/"
        r = self.api.patch(url, {"characteristics": {"height_cm": "12.5", "description": "Круглый"}}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["characteristics"]["height_cm"], "12.50")
        r = self.api.patch(url, {"characteristics": {"width_cm": "7"}}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        ch = self.product.characteristics
        ch.refresh_from_db()
        self.assertEqual((ch.height_cm, ch.width_cm, ch.description), (Decimal("12.50"), Decimal("7.00"), "Круглый"))

    def test_bad_value_is_400(self):
        r = self.api.patch(f"/api/main/products/{self.product.id}/", {"characteristics": {"height_cm": "abc"}}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
