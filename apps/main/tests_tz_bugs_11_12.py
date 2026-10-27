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


class DebtPatchAmountTests(KassaBase):
    """12.2: PATCH долга с меньшей суммой (= погашение) не уменьшает остаток дважды."""

    def test_lower_amount_reduces_balance_once(self):
        from apps.main.models import Debt
        debt = Debt.objects.create(company=self.company, name="Долг", phone="+996555111222", amount=Decimal("1000.00"))
        url = f"/api/main/debts/{debt.id}/"
        r = self.api.patch(url, {"amount": "700.00", "payment_method": "cash"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(debt.payments.count(), 1)
        self.assertEqual(debt.payments.get().amount, Decimal("300.00"))
        g = self.api.get(url)
        self.assertEqual(g.data["balance"], Decimal("700.00"))
        self.assertEqual(g.data["paid_total"], Decimal("300.00"))
        # повторное погашение: ещё раз 200 -> остаток 500, а не 300
        r = self.api.patch(url, {"amount": "800.00", "payment_method": "cash"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.api.get(url).data["balance"], Decimal("500.00"))


class DealPatchTitleWithPaymentsTests(KassaBase):
    """12.6: PATCH {title, note} у сделки с оплатами не пересобирает график."""

    def test_title_note_only(self):
        deal = ClientDeal.objects.create(
            company=self.company, client=self.client_obj, title="Старое", kind=ClientDeal.Kind.DEBT,
            amount=Decimal("900.00"), debt_months=3, first_due_date=date.today(), schedule_version="v2",
        )
        pay = self.api.post(
            f"/api/main/clients/{self.client_obj.id}/deals/{deal.id}/pay/",
            {"amount": "100.00", "idempotency_key": str(uuid.uuid4()), "cashbox_id": str(self.cashbox.id)},
            format="json",
        )
        self.assertEqual(pay.status_code, 200, pay.data)
        before = list(deal.installments.order_by("number").values_list("id", "paid_amount"))
        r = self.api.patch(
            f"/api/main/clients/{self.client_obj.id}/deals/{deal.id}/", {"title": "Новое", "note": "заметка"}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        deal.refresh_from_db()
        self.assertEqual((deal.title, deal.note), ("Новое", "заметка"))
        self.assertEqual(list(deal.installments.order_by("number").values_list("id", "paid_amount")), before)


class BulkDeleteMessageTests(KassaBase):
    """12.10: понятный текст для кассира и машинный code."""

    def test_paid_message(self):
        s = Sale.objects.create(company=self.company, user=self.owner, cashbox=self.cashbox, status=Sale.Status.PAID)
        r = self.api.post("/api/main/sales/bulk-delete/", {"ids": [str(s.id)]}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "paid_sales_need_confirmation")
        self.assertNotIn("allow_paid", r.data["detail"])
        self.assertIn("оплаченные", r.data["detail"])
        self.assertTrue(Sale.objects.filter(pk=s.pk).exists())


class SalePaymentFilterTests(KassaBase):
    """12.11: payment_method=transfer включает банки."""

    def test_transfer_matches_banks(self):
        ids = {}
        for pm in ("cash", "transfer", "mbank", "optima", "obank", "bakai", "debt", "mixed"):
            ids[pm] = str(Sale.objects.create(
                company=self.company, user=self.owner, cashbox=self.cashbox, status=Sale.Status.PAID, payment_method=pm
            ).id)
        r = self.api.get("/api/main/pos/sales/", {"payment_method": "transfer"})
        self.assertEqual({x["id"] for x in r.data["results"]}, {ids[k] for k in ("transfer", "mbank", "optima", "obank", "bakai")})
        r = self.api.get("/api/main/pos/sales/", {"payment_method": "cash"})
        self.assertEqual({x["id"] for x in r.data["results"]}, {ids["cash"]})


class CreateManualTests(KassaBase):
    """12.12 доп. поля сохраняются; 12.13 ошибка доп. штрихкода откатывает товар."""

    URL = "/api/main/products/create-manual/"

    def test_saves_extra_fields(self):
        from apps.main.models import Product
        r = self.api.post(self.URL, {
            "name": "Колбаса", "price": "300", "quantity": "5", "is_weight": True,
            "is_adult": True, "minimum_quantity": "2.5", "plu": 17, "shelf_life_days": 30,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        p = Product.objects.get(name="Колбаса")
        self.assertEqual((p.is_adult, p.minimum_quantity, p.plu, p.shelf_life_days), (True, Decimal("2.500"), 17, 30))

    def test_bad_number_is_400(self):
        r = self.api.post(self.URL, {"name": "X", "price": "1", "plu": "abc"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_bad_alternate_barcode_rolls_back(self):
        from apps.main.models import Product
        self.product.barcode = "4600000000011"
        self.product.save()
        before = Product.objects.filter(company=self.company).count()
        r = self.api.post(self.URL, {
            "name": "Новый", "price": "10", "barcode": "4600000000028",
            "alternate_barcodes": [{"barcode": "4600000000011", "name": "дубль"}],
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(Product.objects.filter(company=self.company).count(), before)
        self.assertFalse(Product.objects.filter(name="Новый").exists())


class SupplierWithoutPhoneTests(KassaBase):
    """12.16: поставщика можно создать без телефона, клиента - нет."""

    def test_supplier_without_phone(self):
        r = self.api.post("/api/main/clients/", {"full_name": "ОсОО Без телефона", "type": "suppliers"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Client.objects.get(full_name="ОсОО Без телефона").phone, "")

    def test_client_still_needs_phone(self):
        r = self.api.post("/api/main/clients/", {"full_name": "Покупатель"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("phone", r.data)


class CompanySettingsOwnerOnlyTests(KassaBase):
    """12.18: PATCH users/settings/company/ только владелец или администратор."""

    URL = "/api/users/settings/company/"

    def test_owner_ok_cashier_403(self):
        r = self.api.patch(self.URL, {"name": "Новое имя"}, format="json")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        self.company.refresh_from_db()
        self.assertEqual(self.company.name, "Новое имя")

        from rest_framework.test import APIClient
        other = APIClient()
        other.force_authenticate(self.cashier)
        self.assertEqual(other.get(self.URL).status_code, 200)
        r = other.patch(self.URL, {"name": "Взлом"}, format="json")
        self.assertEqual(r.status_code, 403, getattr(r, "data", None))
        self.company.refresh_from_db()
        self.assertEqual(self.company.name, "Новое имя")

    def test_admin_can_patch(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        admin = get_user_model().objects.create_user(email=f"a{uuid.uuid4().hex[:6]}@t.kg", password="x", company=self.company, role="admin")
        c = APIClient()
        c.force_authenticate(admin)
        r = c.patch(self.URL, {"name": "Имя от админа"}, format="json")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))


class SocialApplicationsTests(KassaBase):
    """12.19: GET main/socialapplications/ не падает с 500."""

    def test_list_and_create(self):
        r = self.api.get("/api/main/socialapplications/")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        r = self.api.post("/api/main/socialapplications/", {"company": self.company.name, "text": "Подключите Instagram"}, format="json")
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        r = self.api.get("/api/main/socialapplications/")
        self.assertEqual(r.status_code, 200)
        rows = r.data["results"] if isinstance(r.data, dict) else r.data
        self.assertEqual(len(rows), 1)
        pk = rows[0]["id"]
        self.assertEqual(self.api.get(f"/api/main/socialapplications/{pk}/").status_code, 200)


class NotificationUnreadCountTests(KassaBase):
    """12.20: GET notifications/unread-count/."""

    def test_count(self):
        from apps.main.models import Notification
        for i, read in enumerate((False, False, True)):
            Notification.objects.create(company=self.company, user=self.owner, type="t", title=f"n{i}", message="m", is_read=read)
        Notification.objects.create(company=self.company, user=self.cashier, type="t", title="чужое", message="m")
        r = self.api.get("/api/main/notifications/unread-count/")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        self.assertEqual(r.data, {"count": 2})


class SubscriptionBranchesLimitTests(KassaBase):
    """12.22: limits.branches в данных подписки."""

    def test_limits_branches(self):
        from apps.users.models import Branch
        from apps.users.services_subscription import get_company_limits
        Branch.objects.create(company=self.company, name="Филиал 1")
        Branch.objects.create(company=self.company, name="Филиал 2")
        self.assertEqual(get_company_limits(self.company)["branches"], {"used": 2, "max": None})


class ShiftSalesTransferFilterTests(KassaBase):
    """12.11 (продажи смены): transfer включает банки."""

    def test_transfer(self):
        a = Sale.objects.create(company=self.company, user=self.owner, cashbox=self.cashbox, shift=self.shift,
                                status=Sale.Status.PAID, payment_method="mbank")
        Sale.objects.create(company=self.company, user=self.owner, cashbox=self.cashbox, shift=self.shift,
                            status=Sale.Status.PAID, payment_method="cash")
        r = self.api.get(f"/api/construction/cash/shifts/{self.shift.id}/sales/", {"payment_method": "transfer"})
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        rows = r.data["results"] if isinstance(r.data, dict) else r.data
        self.assertEqual([x["id"] for x in rows], [str(a.id)])
