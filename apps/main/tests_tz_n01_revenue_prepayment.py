"""ТЗ Н-01 (23.10.2026): выручка сайта включает предоплату по чекам в долг, долг — отдельно."""
import uuid
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main.models import Sale, SalePayment
from apps.users.models import Company, User


class RevenuePrepaymentTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.company = Company.objects.create(name="Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        cb = Cashbox.objects.create(company=self.company, name="Касса")
        now = timezone.now()
        paid = Sale.objects.create(company=self.company, cashbox=cb, user=self.owner, status=Sale.Status.PAID,
                                   payment_method="cash", total=Decimal("10.00"), paid_at=now)
        SalePayment.objects.create(company=self.company, sale=paid, method="cash", amount=Decimal("10.00"))
        # Чек 4 сома: 1 наличными сразу, 3 в долг (так их пишет касса: status=debt, paid_at пустой).
        debt = Sale.objects.create(company=self.company, cashbox=cb, user=self.owner, status=Sale.Status.DEBT,
                                   payment_method="debt", total=Decimal("4.00"), paid_now=Decimal("1.00"),
                                   cash_amount=Decimal("1.00"), debt_initial=Decimal("3.00"), debt_remaining=Decimal("3.00"))
        SalePayment.objects.create(company=self.company, sale=debt, method="cash", amount=Decimal("1.00"))
        # Старые чеки хранят и строку «долг» — обходим проверку модели, как в боевых данных.
        SalePayment.objects.bulk_create([SalePayment(company=self.company, sale=debt, method="debt", amount=Decimal("3.00"))])
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def test_prepayment_in_revenue_debt_separate(self):
        d = timezone.localdate().isoformat()
        r = self.api.get("/api/main/analytics/market/", {"tab": "sales", "date_from": d, "date_to": d})
        self.assertEqual(r.status_code, 200, r.data)
        cards = r.data["cards"]
        self.assertEqual(cards["revenue"], "11.00")
        self.assertEqual(cards["transactions"], 2)
        self.assertEqual(cards["prepayment_total"], "1.00")
        self.assertEqual(cards["credit_sales_total"], "3.00")
        methods = {m["method"]: m for m in r.data["charts"]["payment_methods"]}
        self.assertEqual(methods["cash"]["total"], "11.00")
        # ТЗ Б-03: amount рядом с total; отдельный элемент debt, в сумму выручки не входит.
        self.assertEqual(methods["cash"]["amount"], "11.00")
        self.assertEqual(methods["cash"]["count"], 2)
        self.assertEqual(methods["debt"]["amount"], "3.00")
        self.assertEqual(methods["debt"]["count"], 1)
        self.assertEqual(
            sum(Decimal(m["amount"]) for k, m in methods.items() if k != "debt"), Decimal("11.00"))
