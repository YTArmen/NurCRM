"""ТЗ Н-02 (23.10.2026): возврат учитывает скидку на чек, всего не больше оплаченного."""
from decimal import Decimal
import uuid

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.construction.models import Cashbox, CashShift
from apps.main.models import Product, Sale, SaleItem, SalePayment, SaleReturn
from apps.main.pos_views import SaleReturnAPIView
from apps.users.models import Branch, Company, User


class ReturnCheckDiscountTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="o_n02@t.kg", password="p")
        self.company = Company.objects.create(name="Co", owner=self.owner, is_active=True)
        self.owner.company = self.company
        self.owner.role = "owner"
        self.owner.save()
        self.branch = Branch.objects.create(name="B", company=self.company)
        self.cashier = User.objects.create_user(email="c_n02@t.kg", password="p", company=self.company, role="cashier")
        self.cashier.branches.add(self.branch)
        self.cashbox = Cashbox.objects.create(name="К", company=self.company, branch=self.branch, role=Cashbox.CashboxRole.POS_MAIN)
        self.shift = CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=self.cashier,
            status=CashShift.Status.OPEN, opening_cash=Decimal("100.00"),
        )
        self.factory = APIRequestFactory()

    def make_sale(self):
        # Чек 80 сом (30 + 50), скидка на чек 5 → оплачено 75.
        sale = Sale.objects.create(
            company=self.company, branch=self.branch, shift=self.shift, user=self.cashier,
            payment_method="cash", status=Sale.Status.PAID,
            subtotal=Decimal("80.00"), discount_total=Decimal("5.00"), total=Decimal("75.00"),
        )
        items = []
        for name, price in (("А", "30.00"), ("Б", "50.00")):
            p = Product.objects.create(company=self.company, name=name, price=Decimal(price), quantity=Decimal("10"))
            items.append(SaleItem.objects.create(
                company=self.company, branch=self.branch, sale=sale, product=p,
                unit_price=Decimal(price), quantity=Decimal("1"), name_snapshot=name,
            ))
        SalePayment.objects.create(company=self.company, sale=sale, method="cash", amount=Decimal("75.00"))
        return sale, items

    def ret(self, sale, body):
        req = self.factory.post(f"/main/pos/sales/{sale.id}/return/", {"idempotency_key": str(uuid.uuid4()), **body}, format="json")
        force_authenticate(req, user=self.cashier)
        return SaleReturnAPIView.as_view()(req, pk=sale.id)

    def test_line_then_rest(self):
        sale, (a, b) = self.make_sale()
        r1 = self.ret(sale, {"items": [{"sale_item_id": str(a.id), "quantity": "1"}]})
        self.assertEqual(r1.status_code, 200, r1.data)
        self.assertEqual(r1.data["refund"]["amount"], "28.13")
        sale.refresh_from_db()
        self.assertEqual(sale.total, Decimal("46.87"))

        r2 = self.ret(sale, {})
        self.assertEqual(r2.status_code, 200, r2.data)
        amounts = sorted(SaleReturn.objects.filter(sale=sale).values_list("returned_amount", flat=True))
        self.assertEqual(amounts, [Decimal("28.13"), Decimal("46.87")])
        self.assertEqual(sum(amounts), Decimal("75.00"))

        r3 = self.ret(sale, {})
        self.assertEqual(r3.status_code, 400)  # серверная ветка: статус как раньше, код новый
        self.assertEqual(r3.data["code"], "refund_exceeds_paid")

    def test_all_lines_partially_equal_paid(self):
        sale, (a, b) = self.make_sale()
        self.ret(sale, {"items": [{"sale_item_id": str(a.id), "quantity": "1"}]})
        self.ret(sale, {"items": [{"sale_item_id": str(b.id), "quantity": "1"}]})
        total = sum(SaleReturn.objects.filter(sale=sale).values_list("returned_amount", flat=True))
        self.assertEqual(total, Decimal("75.00"))
        # Строки возврата сходятся с документами (цена со скидкой на чек).
        lines = sum(
            Decimal(l["total"]) for r in SaleReturn.objects.filter(sale=sale) for l in (r.returned_items or [])
        )
        self.assertEqual(lines, Decimal("75.00"))

    def test_single_line_with_check_discount(self):
        # Случай кассы 27.10: чек 4,00, скидка на чек 0,40, оплачено 3,60; возврат строкой.
        sale = Sale.objects.create(
            company=self.company, branch=self.branch, shift=self.shift, user=self.cashier,
            payment_method="cash", status=Sale.Status.PAID,
            subtotal=Decimal("4.00"), discount_total=Decimal("0.40"), total=Decimal("3.60"),
        )
        p = Product.objects.create(company=self.company, name="Т", price=Decimal("4.00"), quantity=Decimal("10"))
        it = SaleItem.objects.create(
            company=self.company, branch=self.branch, sale=sale, product=p,
            unit_price=Decimal("4.00"), quantity=Decimal("1"), name_snapshot="Т",
        )
        SalePayment.objects.create(company=self.company, sale=sale, method="cash", amount=Decimal("3.60"))
        r = self.ret(sale, {"items": [{"sale_item_id": str(it.id), "quantity": "1"}]})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(SaleReturn.objects.get(sale=sale).returned_amount, Decimal("3.60"))
