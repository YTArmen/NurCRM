"""Доработки для кассы NurMarket (BACKEND_API.md)."""
import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashFlow, CashShift
from apps.integrations.models import ApiKey, WebhookEndpoint
from apps.main.models import (
    Cart,
    CartItem,
    Client,
    ClientBonusTransaction,
    ClientDeal,
    Product,
    Sale,
    SaleDocCounter,
    SalePayment,
    SaleReturn,
)
from apps.users.models import Company, CompanyAddon, Feature, SubscriptionPlan, User


def _ok_delivery():
    resp = mock.MagicMock()
    resp.status = 200
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: False
    return resp


class KassaBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.plan = SubscriptionPlan.objects.create(name="Стандарт", price=Decimal("1.00"))
        self.company = Company.objects.create(
            name="Kassa Co", owner=self.owner, is_active=True, subscription_plan=self.plan
        )
        self.owner.company = self.company
        self.owner.save()
        self.cashier = User.objects.create_user(
            email=f"c{uuid.uuid4().hex[:6]}@t.kg", password="x", company=self.company, role="salesperson"
        )
        self.cashbox = Cashbox.objects.create(
            name="Касса", company=self.company, role=Cashbox.CashboxRole.POS_MAIN
        )
        self.shift = CashShift.objects.create(
            company=self.company,
            cashbox=self.cashbox,
            cashier=self.owner,
            status=CashShift.Status.OPEN,
            opening_cash=Decimal("1000.00"),
        )
        self.product = Product.objects.create(
            company=self.company, name="Хлеб", price=Decimal("100.00"), quantity=Decimal("50")
        )
        self.client_obj = Client.objects.create(company=self.company, full_name="Айбек", phone="+996555000222")
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def quick(self, key=None, **body):
        payload = {
            "items": [{"product": str(self.product.id), "qty": "2", "price": "100.00"}],
            "payment": {"method": "cash", "received": "500.00"},
        }
        payload.update(body)
        return self.api.post(
            "/api/main/pos/checkout/", payload, format="json", HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4())
        )


class QuickCheckoutTests(KassaBase):
    def test_creates_paid_sale_with_number_and_is_idempotent(self):
        key = str(uuid.uuid4())
        r1 = self.quick(key=key)
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertEqual(r1.data["status"], "paid")
        self.assertEqual(r1.data["total"], "200.00")
        self.assertEqual(r1.data["change"], "300.00")
        self.assertEqual(r1.data["number"], 1)

        r2 = self.quick(key=key)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.data["replayed"])
        self.assertEqual(r2.data["id"], r1.data["id"])
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("48"))

    def test_numbers_are_sequential_and_continue_after_existing(self):
        Sale.objects.create(company=self.company, user=self.owner, cashbox=self.cashbox, status=Sale.Status.PAID, doc_number=41)
        n1 = self.quick().data["number"]
        n2 = self.quick().data["number"]
        self.assertEqual((n1, n2), (42, 43))
        self.assertEqual(SaleDocCounter.objects.get(company=self.company).last_number, 43)
        r = self.api.get("/api/main/pos/sales/?number=43")
        self.assertEqual(r.status_code, 200)
        rows = r.data["results"] if isinstance(r.data, dict) else r.data
        self.assertEqual([row["number"] for row in rows], [43])

    def test_requires_idempotency_key(self):
        r = self.api.post(
            "/api/main/pos/checkout/",
            {"items": [{"product": str(self.product.id), "qty": "1"}], "payment": {"method": "cash", "received": "100"}},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

    def test_custom_item_and_mixed_split(self):
        r = self.quick(
            items=[
                {"product": str(self.product.id), "qty": "3"},
                {"custom": True, "name": "Доставка", "price": "100.00", "qty": "1"},
            ],
            payment={"method": "mixed", "cash_amount": "300.00", "card_amount": "100.00"},
        )
        self.assertEqual(r.status_code, 201, r.data)
        sale = Sale.objects.get(pk=r.data["id"])
        self.assertEqual(sale.payment_method, Sale.PaymentMethod.MIXED)
        lines = {p.method: p.amount for p in SalePayment.objects.filter(sale=sale)}
        self.assertEqual(lines, {"cash": Decimal("300.00"), "transfer": Decimal("100.00")})
        detail = self.api.get(f"/api/main/pos/sales/{sale.id}/").data
        self.assertEqual((detail["cash_amount"], detail["card_amount"]), ("300.00", "100.00"))
        self.assertEqual(self.shift.calc_live_totals(refresh=True)["expected_cash"], Decimal("1300.00"))

    def test_cashier_discount_over_limit_is_400(self):
        self.company.max_discount_percent = Decimal("10")
        self.company.save()
        shift = CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=self.cashier, status=CashShift.Status.OPEN
        )
        api = APIClient()
        api.force_authenticate(self.cashier)
        r = api.post(
            "/api/main/pos/checkout/",
            {
                "shift": str(shift.id),
                "items": [{"product": str(self.product.id), "qty": "1"}],
                "order_discount_total": "50.00",
                "payment": {"method": "cash", "received": "100"},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="k1",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "discount_limit")
        self.assertIn("нужно разрешение владельца", r.data["detail"])
        self.assertFalse(Sale.objects.exists())

    def test_bonus_redeemed_separate_from_discount(self):
        self.client_obj.bonus_balance = Decimal("150.00")
        self.client_obj.save()
        r = self.quick(client=str(self.client_obj.id), bonus_redeemed="50.00",
                       payment={"method": "cash", "received": "150.00"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["total"], "150.00")
        self.assertEqual(r.data["discount_total"], "0.00")
        self.assertEqual(r.data["bonus_redeemed"], "50.00")
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.bonus_balance, Decimal("100.00"))
        self.assertEqual(ClientBonusTransaction.objects.get().reason, "redeem")


class CartDiscountLimitTests(KassaBase):
    def test_order_discount_amount_over_limit_is_400_not_500(self):
        self.company.max_discount_percent = Decimal("10")
        self.company.save()
        cart = Cart.objects.create(company=self.company, user=self.cashier, shift=self.shift, status=Cart.Status.ACTIVE)
        CartItem(company=self.company, cart=cart, product=self.product, quantity=Decimal("2"),
                 unit_price=Decimal("100.00")).save(skip_full_clean=True)
        api = APIClient()
        api.force_authenticate(self.cashier)
        r = api.patch(f"/api/main/pos/carts/{cart.id}/", {"order_discount_total": "30.00"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["detail"], "Скидка больше разрешённой (10%), нужно разрешение владельца")
        self.assertEqual(r.data["code"], "discount_limit")
        r = api.patch(f"/api/main/pos/carts/{cart.id}/", {"order_discount_total": "20.00"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def _cashier_cart(self, limit="10"):
        self.company.max_discount_percent = Decimal(limit)
        self.company.save()
        cart = Cart.objects.create(company=self.company, user=self.cashier, shift=self.shift, status=Cart.Status.ACTIVE)
        api = APIClient()
        api.force_authenticate(self.cashier)
        return cart, api

    def assertDiscountLimit(self, r):
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "discount_limit")
        self.assertEqual(Decimal(r.data["max_discount_percent"]), Decimal("10"))

    def test_order_discount_percent_over_limit(self):
        cart, api = self._cashier_cart()
        r = api.patch(f"/api/main/pos/carts/{cart.id}/", {"order_discount_percent": "50"}, format="json")
        self.assertDiscountLimit(r)

    def test_add_item_with_discount_over_limit_is_rejected(self):
        cart, api = self._cashier_cart()
        url = f"/api/main/pos/sales/{cart.id}/add-item/"
        r = api.post(url, {"product_id": str(self.product.id), "quantity": "2", "discount_total": "50.00"}, format="json")
        self.assertDiscountLimit(r)
        self.assertFalse(CartItem.objects.filter(cart=cart).exists())
        r = api.post(url, {"product_id": str(self.product.id), "quantity": "1", "discount_percent": "50"}, format="json")
        self.assertDiscountLimit(r)
        r = api.post(url, {"product_id": str(self.product.id), "quantity": "2", "discount_total": "20.00"}, format="json")
        self.assertIn(r.status_code, (200, 201), r.data)

    def test_item_patch_discount_over_limit(self):
        cart, api = self._cashier_cart()
        item = CartItem(company=self.company, cart=cart, product=self.product, quantity=Decimal("1"),
                        unit_price=Decimal("100.00"))
        item.save(skip_full_clean=True)
        url = f"/api/main/pos/carts/{cart.id}/items/{item.id}/"
        self.assertDiscountLimit(api.patch(url, {"discount_total": "50.00"}, format="json"))
        self.assertDiscountLimit(api.patch(url, {"discount_percent": "50"}, format="json"))
        item.refresh_from_db()
        self.assertEqual(item.line_discount, Decimal("0.00"))
        r = api.patch(url, {"discount_total": "10.00"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_checkout_line_discount_over_limit(self):
        cart, api = self._cashier_cart()
        shift = CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=self.cashier, status=CashShift.Status.OPEN
        )
        r = api.post(
            "/api/main/pos/checkout/",
            {
                "shift": str(shift.id),
                "items": [{"product": str(self.product.id), "qty": "1", "discount": "50.00"}],
                "payment": {"method": "cash", "received": "100"},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="k-line",
        )
        self.assertDiscountLimit(r)
        self.assertFalse(Sale.objects.exists())

    def test_owner_is_not_limited(self):
        cart, _ = self._cashier_cart()
        cart.user = self.owner
        cart.save()
        api = APIClient()
        api.force_authenticate(self.owner)
        r = api.post(f"/api/main/pos/sales/{cart.id}/add-item/",
                     {"product_id": str(self.product.id), "quantity": "1", "discount_total": "50.00"}, format="json")
        self.assertIn(r.status_code, (200, 201), r.data)


class CheckoutMixedSplitTests(KassaBase):
    def test_classic_checkout_with_cash_and_card_amount(self):
        cart = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)
        CartItem(company=self.company, cart=cart, product=self.product, quantity=Decimal("5"),
                 unit_price=Decimal("100.00")).save(skip_full_clean=True)
        r = self.api.post(
            f"/api/main/pos/sales/{cart.id}/checkout/",
            {"payment_method": "mixed", "cash_amount": "300.00", "card_amount": "200.00", "card_method": "mbank"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        sale = Sale.objects.get(pk=r.data["sale_id"])
        self.assertIsNotNone(sale.doc_number)
        self.assertEqual(
            {p.method: p.amount for p in sale.payments.all()},
            {"cash": Decimal("300.00"), "mbank": Decimal("200.00")},
        )


class IdempotentCheckoutTests(KassaBase):
    def test_checkout_repeated_with_same_key_returns_same_201_no_duplicate(self):
        cart = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)
        CartItem(company=self.company, cart=cart, product=self.product, quantity=Decimal("2"),
                 unit_price=Decimal("100.00")).save(skip_full_clean=True)
        init_qty = self.product.quantity
        key = str(uuid.uuid4())

        r1 = self.api.post(
            f"/api/main/pos/sales/{cart.id}/checkout/",
            {"payment_method": "cash", "cash_received": "200.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(r1.status_code, 201, r1.data)
        sale_id = r1.data["sale_id"]
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, init_qty - Decimal("2"))

        # Повторный запрос с тем же Idempotency-Key
        r2 = self.api.post(
            f"/api/main/pos/sales/{cart.id}/checkout/",
            {"payment_method": "cash", "cash_received": "200.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2.data["sale_id"], sale_id)
        self.assertEqual(r2.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, init_qty - Decimal("2"))

    def test_checkout_with_body_idempotency_key(self):
        cart = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)
        CartItem(company=self.company, cart=cart, product=self.product, quantity=Decimal("1"),
                 unit_price=Decimal("100.00")).save(skip_full_clean=True)
        key = f"body-key-{uuid.uuid4()}"

        r1 = self.api.post(
            f"/api/main/pos/sales/{cart.id}/checkout/",
            {"payment_method": "cash", "cash_received": "100.00", "idempotency_key": key},
            format="json",
        )
        self.assertEqual(r1.status_code, 201, r1.data)
        sale_id = r1.data["sale_id"]

        r2 = self.api.post(
            f"/api/main/pos/sales/{cart.id}/checkout/",
            {"payment_method": "cash", "cash_received": "100.00", "idempotency_key": key},
            format="json",
        )
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2.data["sale_id"], sale_id)
        self.assertEqual(r2.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 1)

    def test_checkout_retry_after_400_error_retries_logic(self):
        cart = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)
        CartItem(company=self.company, cart=cart, product=self.product, quantity=Decimal("1"),
                 unit_price=Decimal("100.00")).save(skip_full_clean=True)
        key = str(uuid.uuid4())

        # Недостаточно наличных -> 400
        r1 = self.api.post(
            f"/api/main/pos/sales/{cart.id}/checkout/",
            {"payment_method": "cash", "cash_received": "50.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(r1.status_code, 400)
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 0)

        # Ретрай с тем же ключом и валидной суммой должен выполниться, а не вернуть 400
        r2 = self.api.post(
            f"/api/main/pos/sales/{cart.id}/checkout/",
            {"payment_method": "cash", "cash_received": "100.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(r2.status_code, 201, r2.data)
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 1)

    def test_checkout_in_progress_returns_409(self):
        from apps.integrations.models import IdempotencyRecord
        from apps.integrations.idempotency import _body_hash
        key = str(uuid.uuid4())
        cart = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)
        scope = f"POST /main/pos/sales/{cart.id}/checkout/"
        payload = {"payment_method": "cash", "cash_received": "100.00"}
        class DummyRequest:
            data = payload
        IdempotencyRecord.objects.create(
            company=self.company,
            key=key,
            scope=scope,
            body_hash=_body_hash(DummyRequest()),
            state=IdempotencyRecord.State.IN_PROGRESS,
        )
        r = self.api.post(
            f"/api/main/pos/sales/{cart.id}/checkout/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data.get("code"), "in_progress")

    def test_start_sale_is_idempotent(self):
        key = str(uuid.uuid4())
        r1 = self.api.post(
            "/api/main/pos/sales/start/",
            {"shift": str(self.shift.id)},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertIn(r1.status_code, (200, 201), r1.data)
        cart_id = r1.data.get("id") or r1.data.get("active_sale_id")

        r2 = self.api.post(
            "/api/main/pos/sales/start/",
            {"shift": str(self.shift.id)},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertIn(r2.status_code, (200, 201))
        self.assertEqual(r2.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(r2.data.get("id") or r2.data.get("active_sale_id"), cart_id)

    def test_key_length_validation(self):
        cart = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)
        CartItem(company=self.company, cart=cart, product=self.product, quantity=Decimal("1"),
                 unit_price=Decimal("100.00")).save(skip_full_clean=True)
        # 255 символов - ОК
        key_255 = "k" * 255
        r1 = self.api.post(
            f"/api/main/pos/sales/{cart.id}/checkout/",
            {"payment_method": "cash", "cash_received": "100.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key_255,
        )
        self.assertEqual(r1.status_code, 201)

        # 256 символов - 400
        cart2 = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)
        key_256 = "k" * 256
        r2 = self.api.post(
            f"/api/main/pos/sales/{cart2.id}/checkout/",
            {"payment_method": "cash", "cash_received": "100.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key_256,
        )
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(r2.data.get("code"), "invalid")


class DrawerInflowTests(KassaBase):
    def test_inflow_and_outflow_move_expected_cash_905_915_905(self):
        """BE2-05: внесение 10 → expected_cash 905 → 915, изъятие 10 → снова 905."""
        self.shift.opening_cash = Decimal("905.00")
        self.shift.save()

        def expected():
            r = self.api.get(f"/api/construction/shifts/{self.shift.id}/")
            self.assertEqual(r.status_code, 200, r.data)
            return Decimal(str(r.data["expected_cash"]))

        self.assertEqual(expected(), Decimal("905.00"))
        for kind, typ, want in (("shift_drawer_inflow", "income", "915.00"), ("shift_drawer_outflow", "expense", "905.00")):
            r = self.api.post("/api/construction/cashflows/", {
                "cashbox": str(self.cashbox.id), "shift": str(self.shift.id), "type": typ, "name": kind,
                "amount": "10.00", "source_kind": kind, "source_id": kind, "payment_method": "cash",
            }, format="json")
            self.assertEqual(r.status_code, 201, r.data)
            self.assertEqual(expected(), Decimal(want))

    def test_inflow_counts_in_expected_cash_and_is_deduplicated(self):
        body = {
            "cashbox": str(self.cashbox.id), "shift": str(self.shift.id), "type": "income", "name": "Внесение",
            "amount": "500.00", "source_kind": "shift_drawer_inflow", "source_id": "op-1", "payment_method": "cash",
        }
        r1 = self.api.post("/api/construction/cashflows/", body, format="json")
        self.assertEqual(r1.status_code, 201, r1.data)
        r2 = self.api.post("/api/construction/cashflows/", body, format="json")
        self.assertEqual(r2.data["id"], r1.data["id"])
        totals = self.shift.calc_live_totals(refresh=True)
        self.assertEqual(totals["expected_cash"], Decimal("1500.00"))
        self.assertEqual(totals["income_total"], Decimal("500.00"))

        r = self.api.get("/api/construction/cashflows/?source_id=op-1&source_kind=shift_drawer_inflow")
        self.assertEqual(r.data["count"], 1)
        r = self.api.get("/api/construction/cashflows/?source_id=nope")
        self.assertEqual(r.data["count"], 0)

    def test_inflow_must_be_income(self):
        r = self.api.post("/api/construction/cashflows/", {
            "cashbox": str(self.cashbox.id), "type": "expense", "name": "x", "amount": "5",
            "source_kind": "shift_drawer_inflow",
        }, format="json")
        self.assertEqual(r.status_code, 400)


class ShiftReportTests(KassaBase):
    def test_report_has_deposits_withdrawals_returns(self):
        self.quick(payment={"method": "mixed", "cash_amount": "150.00", "card_amount": "50.00"})
        sale_id = self.quick().data["id"]
        for kind, typ, amt in (("shift_drawer_inflow", "income", "500"), ("shift_drawer_outflow", "expense", "200")):
            self.api.post("/api/construction/cashflows/", {
                "cashbox": str(self.cashbox.id), "shift": str(self.shift.id), "type": typ, "name": kind,
                "amount": amt, "source_kind": kind,
            }, format="json")
        r = self.api.post(f"/api/main/pos/sales/{sale_id}/return/", {"reason": "брак"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

        rep = self.api.get(f"/api/construction/shifts/{self.shift.id}/report/").data
        self.assertEqual(rep["deposits"], "500.00")
        self.assertEqual(rep["withdrawals"], "200.00")
        self.assertEqual(rep["by_payment"]["mixed_cash"], "150.00")
        self.assertEqual(rep["by_payment"]["mixed_card"], "50.00")
        self.assertEqual(rep["returns_count"], 1)
        self.assertEqual(rep["returns_total"], "200.00")

        returns = self.api.get("/api/main/pos/returns/").data
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]["reason"], "брак")
        self.assertEqual(returns[0]["items"][0]["qty"], "2.000")
        self.assertEqual(returns[0]["sale_number"], Sale.objects.get(pk=sale_id).doc_number)


class DebtTests(KassaBase):
    def _debt_sale(self, total, prepay="0"):
        r = self.quick(
            client=str(self.client_obj.id),
            items=[{"product": str(self.product.id), "qty": str(Decimal(total) / 100)}],
            payment={"method": "debt", "received": prepay},
        )
        self.assertEqual(r.status_code, 201, r.data)
        return Sale.objects.get(pk=r.data["id"])

    def test_debtors_and_pay_debt_oldest_first(self):
        s1 = self._debt_sale("300", prepay="100")
        s2 = self._debt_sale("500")
        other = Client.objects.create(company=self.company, full_name="Без долга", phone="1")

        r = self.api.get("/api/main/clients/debtors/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 1)
        row = r.data[0]
        self.assertEqual((row["client"], row["debt_total"], row["sales_count"]),
                         (str(self.client_obj.id), "700.00", 2))
        self.assertNotIn(str(other.id), [x["client"] for x in r.data])

        if not ClientDeal.objects.filter(sale=s1).first().installments.exists():
            self.skipTest("долговые сделки без графика взносов — pay-any не применим")
        r = self.api.post(f"/api/main/clients/{self.client_obj.id}/pay-debt/",
                          {"amount": "250.00", "method": "cash", "shift": str(self.shift.id)},
                          format="json", HTTP_IDEMPOTENCY_KEY="pay-1")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["paid"], "250.00")
        self.assertEqual(r.data["left"], "450.00")
        self.assertEqual(r.data["applied"][0]["sale"], str(s1.id))
        r2 = self.api.post(f"/api/main/clients/{self.client_obj.id}/pay-debt/",
                           {"amount": "250.00", "method": "cash"}, format="json", HTTP_IDEMPOTENCY_KEY="pay-1")
        self.assertEqual(r2.data["left"], "450.00")


class ProductFilterTests(KassaBase):
    def test_quantity_lte_and_is_service(self):
        Product.objects.create(company=self.company, name="Мало", price=1, quantity=Decimal("3"))
        Product.objects.create(company=self.company, name="Стрижка", price=1, quantity=Decimal("0"), kind="service")
        r = self.api.get("/api/main/products/list/?quantity_lte=5&is_service=false")
        self.assertEqual(r.status_code, 200)
        rows = r.data["results"] if isinstance(r.data, dict) else r.data
        self.assertEqual([p["name"] for p in rows], ["Мало"])


class ClientFieldsTests(KassaBase):
    def test_telegram_chat_id_and_bonus_endpoints(self):
        r = self.api.patch(f"/api/main/clients/{self.client_obj.id}/", {"telegram_chat_id": "123456789"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.telegram_chat_id, "123456789")

        r = self.api.post(f"/api/main/clients/{self.client_obj.id}/bonus/", {"delta": "100", "reason": "earn"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.api.post(f"/api/main/clients/{self.client_obj.id}/bonus/", {"delta": "-150", "reason": "redeem"}, format="json")
        self.assertEqual(r.status_code, 400)
        hist = self.api.get(f"/api/main/clients/{self.client_obj.id}/bonus/history/").data
        self.assertEqual(hist["bonus_balance"], "100.00")
        self.assertEqual(len(hist["results"]), 1)


class CompanyTests(KassaBase):
    def test_market_sphere_features_addons(self):
        self.plan.features.add(Feature.objects.create(name="debts"))
        CompanyAddon.objects.create(company=self.company, code="loyalty")
        r = self.api.patch("/api/users/company/", {"market_sphere": "clothing"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["market_sphere"], "clothing")
        self.assertEqual(r.data["features"], ["debts", "loyalty"])
        addons = self.api.get("/api/users/company/addons/").data
        self.assertEqual(addons, [{"code": "loyalty", "active": True, "until": None}])

        api = APIClient()
        api.force_authenticate(self.cashier)
        self.assertEqual(api.patch("/api/users/company/", {"market_sphere": "grocery"}, format="json").status_code, 403)

    def test_market_spheres_list_and_features_detail(self):
        """BE2-02: несколько видов магазина и функции со сроком."""
        import datetime

        self.plan.features.add(Feature.objects.create(name="variants"))
        CompanyAddon.objects.create(company=self.company, code="certificates", until=datetime.date(2027, 1, 1))
        r = self.api.patch("/api/users/company/", {"market_spheres": ["clothing", "services"]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["market_sphere"], r.data["market_spheres"]), ("clothing", ["clothing", "services"]))
        detail = {f["code"]: f["until"] for f in r.data["features_detail"]}
        self.assertEqual(detail["certificates"][:10], "2027-01-01")
        self.assertIn("variants", detail)

        r = self.api.patch("/api/users/company/", {"market_sphere": "services"}, format="json")
        self.assertEqual(r.data["market_spheres"], ["services"])
        r = self.api.patch("/api/users/company/", {"market_spheres": ["cafe"]}, format="json")
        self.assertEqual(r.status_code, 400)


class ApiKeyTests(KassaBase):
    def test_read_only_key(self):
        r = self.api.post("/api/users/api-keys/", {"name": "Telegram-бот", "scopes": ["read"]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        raw = r.data["key"]
        self.assertTrue(raw.startswith("nk_live_"))
        self.assertNotIn("key", self.api.get("/api/users/api-keys/").data[0])

        bot = APIClient()
        bot.credentials(HTTP_AUTHORIZATION=f"Api-Key {raw}")
        self.assertEqual(bot.get("/api/main/pos/sales/").status_code, 200)
        self.assertEqual(bot.post("/api/main/clients/", {"full_name": "x", "phone": "1"}, format="json").status_code, 403)
        self.assertEqual(bot.get("/api/users/api-keys/").status_code, 403)

        self.assertEqual(self.api.delete(f"/api/users/api-keys/{r.data['id']}/").status_code, 204)
        self.assertEqual(bot.get("/api/main/pos/sales/").status_code, 401)

    def test_cashier_cannot_manage_keys(self):
        api = APIClient()
        api.force_authenticate(self.cashier)
        self.assertEqual(api.post("/api/users/api-keys/", {"name": "x"}, format="json").status_code, 403)


class WebhookTests(KassaBase):
    @mock.patch("apps.integrations.events.validate_public_url")
    def test_sale_paid_and_shift_closed_are_delivered_signed(self, _v):
        ep = WebhookEndpoint.objects.create(
            company=self.company, url="https://bot.example.kg/hook", events=["sale.paid", "shift.closed"],
            secret="s" * 32,
        )
        with mock.patch("apps.integrations.tasks._opener.open", return_value=_ok_delivery()) as op, \
                mock.patch("apps.integrations.events.validate_public_url"), \
                self.captureOnCommitCallbacks(execute=True):
            self.quick()
            self.api.post(f"/api/construction/shifts/{self.shift.id}/close/", {"closing_cash": "1200"}, format="json")
        events = [json.loads(c.args[0].data)["event"] for c in op.call_args_list]
        self.assertEqual(events, ["sale.paid", "shift.closed"])
        req = op.call_args_list[0].args[0]
        self.assertEqual(
            req.headers["X-signature"],
            hmac.new(ep.secret.encode(), req.data, hashlib.sha256).hexdigest(),
        )
        ep.refresh_from_db()
        self.assertEqual(ep.last_status, 200)

    def test_rejects_internal_url(self):
        r = self.api.post("/api/users/webhooks/", {"url": "http://127.0.0.1/x", "events": ["sale.paid"]}, format="json")
        self.assertEqual(r.status_code, 400)


class ReviewFixesTests(KassaBase):
    def test_shift_opened_webhook(self):
        WebhookEndpoint.objects.create(company=self.company, url="https://bot.example.kg/h", events=["shift.opened"],
                                       secret="s" * 32)
        with mock.patch("apps.integrations.tasks._opener.open", return_value=_ok_delivery()) as op, \
                mock.patch("apps.integrations.events.validate_public_url"), \
                self.captureOnCommitCallbacks(execute=True):
            CashShift.objects.create(company=self.company, cashbox=self.cashbox, cashier=self.cashier,
                                     status=CashShift.Status.OPEN)
        body = json.loads(op.call_args.args[0].data)
        self.assertEqual(body["event"], "shift.opened")
        self.assertEqual(body["data"]["cashier"], str(self.cashier.id))

    def test_debt_return_gets_items_and_shift(self):
        sale_id = self.quick(client=str(self.client_obj.id), payment={"method": "debt", "received": "0"}).data["id"]
        r = self.api.post(f"/api/main/pos/sales/{sale_id}/return/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        ret = self.api.get("/api/main/pos/returns/").data[0]
        self.assertEqual(ret["shift"], str(self.shift.id))
        self.assertEqual(ret["items"][0]["qty"], "2.000")

    def test_backfill_old_returns(self):
        from django.core.management import call_command

        sale_id = self.quick().data["id"]
        self.api.post(f"/api/main/pos/sales/{sale_id}/return/", {}, format="json")
        SaleReturn.objects.update(returned_items=None, shift=None)
        call_command("backfill_sale_returns", stdout=mock.MagicMock())
        r = SaleReturn.objects.get()
        self.assertEqual(r.shift_id, self.shift.id)
        self.assertEqual(r.returned_items[0]["qty"], "2.000")


class PromotionVsManualDiscountTests(KassaBase):
    """BE2-06/07: акция и ручная скидка кассира на одной строке."""

    def setUp(self):
        super().setUp()
        from apps.main.models import ProductPromotionTier

        self.promo = Product.objects.create(
            company=self.company, name="Адыгене 1л", price=Decimal("50.00"), quantity=Decimal("50"), stock=True
        )
        self.tier = ProductPromotionTier.objects.create(
            product=self.promo, min_amount=Decimal("32.00"), discount_percent=Decimal("15")
        )

    def _checkout(self, price, discount):
        return self.quick(items=[{"product": str(self.promo.id), "qty": "1", "price": price, "discount": discount}],
                          payment={"method": "cash", "received": "100.00"})

    def test_manual_discount_applies_when_promotion_does_not_match(self):
        r = self._checkout("20.00", "5.00")  # 20 < 32 — акция не подошла
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["total"], "15.00")
        line = r.data["items"][0]
        self.assertEqual((line["discount"], line["discount_source"], line["total"]), ("5.00", "manual", "15.00"))
        self.assertFalse(line["manual_discount_ignored"])

    def test_promotion_wins_and_ignored_manual_discount_is_reported(self):
        r = self._checkout("50.00", "5.00")  # 50 ≥ 32 — акция −15 %
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["total"], "42.50")
        line = r.data["items"][0]
        self.assertEqual((line["discount"], line["discount_source"]), ("7.50", "promotion"))
        self.assertEqual(line["promotion_id"], str(self.tier.id))
        self.assertTrue(line["manual_discount_ignored"])
        item = Sale.objects.get(id=r.data["id"]).items.get()
        self.assertEqual((item.discount_source, item.manual_discount), ("promotion", Decimal("5.00")))

    def test_manual_discount_survives_quantity_change_in_cart(self):
        cart = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)
        item = CartItem(company=self.company, cart=cart, product=self.promo, quantity=Decimal("1"),
                        unit_price=Decimal("20.00"), line_discount=Decimal("5.00"), manual_discount=Decimal("5.00"))
        item.save(skip_full_clean=True)
        cart.recalc()
        item.refresh_from_db()
        self.assertEqual((item.line_discount, item.discount_source), (Decimal("5.00"), "manual"))

        CartItem.objects.filter(pk=item.pk).update(quantity=Decimal("2"))  # 40 ≥ 32 — акция
        cart.recalc()
        item.refresh_from_db()
        self.assertEqual((item.line_discount, item.discount_source), (Decimal("6.00"), "promotion"))

        CartItem.objects.filter(pk=item.pk).update(quantity=Decimal("1"))  # снова ниже порога
        cart.recalc()
        item.refresh_from_db()
        self.assertEqual((item.line_discount, item.discount_source), (Decimal("5.00"), "manual"))

    def test_cart_response_shows_discount_source(self):
        cart = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)
        r = self.api.post(f"/api/main/pos/sales/{cart.id}/add-item/",
                          {"product_id": str(self.promo.id), "quantity": "1", "discount_total": "5.00"}, format="json")
        self.assertIn(r.status_code, (200, 201), r.data)
        line = CartItem.objects.get(cart=cart)
        self.assertEqual((line.line_discount, line.discount_source, line.manual_discount),
                         (Decimal("7.50"), "promotion", Decimal("5.00")))
        data = self.api.get(f"/api/main/pos/carts/{cart.id}/").data
        row = data["items"][0]
        self.assertEqual((row["discount_source"], row["manual_discount_ignored"]), ("promotion", True))


class ReturnLinesTests(KassaBase):
    """BE2-01: строки возврата с ценой, скидкой, причиной и сменой."""

    def test_partial_return_lines_and_shift_filter(self):
        r = self.quick(items=[{"product": str(self.product.id), "qty": "2", "price": "100.00", "discount": "20.00"}],
                       shift=str(self.shift.id))
        self.assertEqual(r.status_code, 201, r.data)
        sale = Sale.objects.get(id=r.data["id"])
        item = sale.items.get()
        r = self.api.post(f"/api/main/pos/sales/{sale.id}/return/",
                          {"items": [{"sale_item_id": str(item.id), "quantity": 1}], "reason": "не подошёл",
                           "refund_method": "cash"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

        rows = self.api.get(f"/api/main/pos/returns/?shift={self.shift.id}").data
        self.assertEqual(len(rows), 1)
        ret = rows[0]
        self.assertEqual(ret["shift"], str(self.shift.id))
        self.assertEqual((ret["total"], ret["refund_method"]), ("90.00", "cash"))
        line = ret["items"][0]
        self.assertEqual(
            (line["product"], line["qty"], line["price"], line["discount"], line["total"], line["reason"], line["restock"]),
            (str(self.product.id), "1.000", "100.00", "10.00", "90.00", "не подошёл", True),
        )
        other = CashShift.objects.create(company=self.company, cashbox=self.cashbox, cashier=self.cashier,
                                         status=CashShift.Status.OPEN)
        self.assertEqual(self.api.get(f"/api/main/pos/returns/?shift={other.id}").data, [])

    def test_defect_return_is_not_restocked(self):
        sale_id = self.quick(shift=str(self.shift.id)).data["id"]
        r = self.api.post(f"/api/main/pos/sales/{sale_id}/return/", {"is_defect": True}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        line = self.api.get("/api/main/pos/returns/").data[0]["items"][0]
        self.assertEqual((line["reason"], line["restock"], line["total"]), ("defect", False, "200.00"))


class HealthAndErrorFormatTests(KassaBase):
    """BE2-09 и п. 2.1/2.4: health без авторизации, JSON-ошибки с code."""

    def test_health_ok_without_auth(self):
        r = APIClient().get("/api/health/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["status"], body["db"]), ("ok", "ok"))
        self.assertIn("time", body)
        self.assertIn("version", body)

    def test_health_db_down_is_503_json_with_retry_after(self):
        with mock.patch("core.views_health.connection") as conn:
            conn.cursor.side_effect = Exception("db is down")
            r = APIClient().get("/api/health/")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r["Retry-After"], "30")
        self.assertEqual(r.json()["db"], "down")

    def test_errors_carry_code(self):
        r = APIClient().get("/api/main/pos/returns/")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.data["code"], "not_authenticated")
        r = self.api.get(f"/api/main/pos/sales/{uuid.uuid4()}/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.data["code"], "not_found")

    def test_field_errors_keep_their_shape(self):
        # ТЗ-28 §4: ошибки полей остаются на верхнем уровне (совместимость с сайтом)
        # и дополняются машинным code, текстом detail и плоским fields.
        r = self.api.get("/api/main/pos/returns/?date_from=bad")
        self.assertEqual(r.status_code, 400)
        self.assertIn("date_from", r.data)
        self.assertEqual(r.data["code"], "validation_error")
        self.assertIn("date_from", r.data["fields"])
        self.assertTrue(r.data["detail"])
        self.assertEqual(set(r.data) - {"code", "detail", "fields"}, {"date_from"})

    def test_django_validation_error_is_400_not_500(self):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from core.exceptions import api_exception_handler

        resp = api_exception_handler(DjangoValidationError("Остаток меньше нуля."), {})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual((resp.data["detail"], resp.data["code"]), ("Остаток меньше нуля.", "invalid"))


class IdempotencyKeyTests(KassaBase):
    """BE2-11: повтор операции с тем же Idempotency-Key не создаёт дубль."""

    def test_return_twice_with_same_key_creates_one_return(self):
        sale_id = self.quick(shift=str(self.shift.id)).data["id"]
        url = f"/api/main/pos/sales/{sale_id}/return/"
        r1 = self.api.post(url, {"reason": "брак"}, format="json", HTTP_IDEMPOTENCY_KEY="ret-1")
        self.assertEqual(r1.status_code, 200, r1.data)
        r2 = self.api.post(url, {"reason": "брак"}, format="json", HTTP_IDEMPOTENCY_KEY="ret-1")
        self.assertEqual(r2.status_code, 200, r2.data)
        self.assertEqual(r2["Idempotent-Replayed"], "true")
        self.assertEqual(r2.json(), r1.json())
        self.assertEqual(SaleReturn.objects.count(), 1)

    def test_same_key_other_body_is_conflict(self):
        body = {"cashbox": str(self.cashbox.id), "shift": str(self.shift.id), "type": "income", "name": "Внесение",
                "amount": "10.00", "source_kind": "shift_drawer_inflow", "payment_method": "cash"}
        r = self.api.post("/api/construction/cashflows/", body, format="json", HTTP_IDEMPOTENCY_KEY="cf-1")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.api.post("/api/construction/cashflows/", body, format="json", HTTP_IDEMPOTENCY_KEY="cf-1")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(CashFlow.objects.filter(source_kind="shift_drawer_inflow").count(), 1)
        r = self.api.post("/api/construction/cashflows/", {**body, "amount": "20.00"}, format="json",
                          HTTP_IDEMPOTENCY_KEY="cf-1")
        self.assertEqual((r.status_code, r.data["code"]), (409, "idempotency_conflict"))

    def test_client_create_and_shift_open_close(self):
        body = {"full_name": "Айгуль", "phone": "+996700000001"}
        ids = {self.api.post("/api/main/clients/", body, format="json", HTTP_IDEMPOTENCY_KEY="cl-1").data["id"]
               for _ in range(2)}
        self.assertEqual(len(ids), 1)

        self.shift.status = CashShift.Status.CLOSED
        self.shift.save()
        open_body = {"cashbox": str(self.cashbox.id), "opening_cash": "100.00"}
        r1 = self.api.post("/api/construction/shifts/open/", open_body, format="json", HTTP_IDEMPOTENCY_KEY="sh-1")
        self.assertEqual(r1.status_code, 201, r1.data)
        r2 = self.api.post("/api/construction/shifts/open/", open_body, format="json", HTTP_IDEMPOTENCY_KEY="sh-1")
        self.assertEqual((r2.status_code, r2.data["id"]), (201, r1.data["id"]))

        close_url = f"/api/construction/shifts/{r1.data['id']}/close/"
        c1 = self.api.post(close_url, {"closing_cash": "100.00"}, format="json", HTTP_IDEMPOTENCY_KEY="sh-close")
        self.assertIn(c1.status_code, (200, 201), c1.data)
        c2 = self.api.post(close_url, {"closing_cash": "100.00"}, format="json", HTTP_IDEMPOTENCY_KEY="sh-close")
        self.assertEqual(c2.status_code, c1.status_code)
        self.assertEqual(c2["Idempotent-Replayed"], "true")

    def test_failed_request_can_be_retried_with_same_key(self):
        r = self.api.post("/api/main/clients/", {"phone": "x"}, format="json", HTTP_IDEMPOTENCY_KEY="cl-bad")
        self.assertEqual(r.status_code, 400)
        r = self.api.post("/api/main/clients/", {"full_name": "Бек", "phone": "+996700000002"}, format="json",
                          HTTP_IDEMPOTENCY_KEY="cl-bad")
        self.assertEqual(r.status_code, 201, r.data)


class OfflineSaleTests(KassaBase):
    """BE2-10: продажа без связи попадает в свой день и свою (даже закрытую) смену."""

    def test_offline_sale_into_closed_shift_two_days_later(self):
        from datetime import timedelta
        from django.utils import timezone

        sold_at = timezone.now() - timedelta(days=2)
        CashShift.objects.filter(pk=self.shift.pk).update(opened_at=sold_at - timedelta(hours=1))
        self.shift.refresh_from_db()
        self.shift.close(closing_cash=Decimal("1000.00"))
        self.assertEqual(self.shift.sales_count, 0)

        body = {"offline": True, "offline_created_at": sold_at.isoformat(), "shift": str(self.shift.id)}
        r = self.quick(key="off-1", **body)
        self.assertEqual(r.status_code, 201, r.data)
        sale = Sale.objects.get(id=r.data["id"])
        self.assertTrue(sale.is_offline)
        self.assertEqual(sale.shift_id, self.shift.id)
        self.assertEqual((sale.created_at, sale.paid_at), (sold_at, sold_at))
        self.assertIsNotNone(sale.received_at)
        flow = CashFlow.objects.get(source_id=str(sale.id))
        self.assertEqual((flow.created_at, flow.shift_id), (sold_at, self.shift.id))

        self.shift.refresh_from_db()
        self.assertEqual((self.shift.sales_count, self.shift.sales_total), (1, Decimal("200.00")))

        again = self.quick(key="off-1", **body)
        self.assertEqual((again.status_code, again.data["replayed"]), (200, True))
        self.assertEqual(Sale.objects.count(), 1)

    def test_offline_time_is_validated(self):
        from datetime import timedelta
        from django.utils import timezone

        r = self.quick(offline=True)
        self.assertEqual(r.status_code, 400)
        r = self.quick(offline=True, offline_created_at=(timezone.now() + timedelta(hours=1)).isoformat())
        self.assertEqual(r.status_code, 400)
        r = self.quick(offline=True, offline_created_at=(timezone.now() - timedelta(days=8)).isoformat())
        self.assertEqual(r.status_code, 400)


class CatalogSyncTests(KassaBase):
    """BE2-19: products/list/?updated_since= — изменённые, удалённые, server_time."""

    def _sync(self, since):
        r = self.api.get("/api/main/products/list/", {"updated_since": since})
        self.assertEqual(r.status_code, 200, r.data)
        return r.data, {row["id"] for row in r.data["results"]}

    def _stale(self, *products):
        from datetime import timedelta
        from django.utils import timezone

        Product.objects.filter(pk__in=[p.pk for p in products]).update(
            updated_at=timezone.now() - timedelta(hours=1)
        )

    def test_changes_sales_promotions_and_deletions_are_synced(self):
        from apps.main.models import ProductPromotionTier

        other = Product.objects.create(company=self.company, name="Молоко", price=Decimal("80.00"),
                                       quantity=Decimal("10"))
        gone = Product.objects.create(company=self.company, name="Кефир", price=Decimal("70.00"),
                                      quantity=Decimal("5"))
        self._stale(self.product, other, gone)
        since = self._sync("2000-01-01T00:00:00+06:00")[0]["server_time"]
        self.assertEqual(self._sync(since)[1], set())

        self.quick()  # продажа хлеба меняет остаток массовым bulk_update
        ProductPromotionTier.objects.create(product=other, min_amount=Decimal("1"), discount_percent=Decimal("5"))
        gone_id = str(gone.id)
        gone.delete()

        data, ids = self._sync(since)
        self.assertEqual(ids, {str(self.product.id), str(other.id)})
        self.assertEqual(data["deleted"], [gone_id])
        self.assertIn("server_time", data)

        data, ids = self._sync(data["server_time"])
        self.assertEqual((ids, data["deleted"]), (set(), []))

    def test_without_param_response_is_unchanged(self):
        r = self.api.get("/api/main/products/list/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("deleted", r.data)

    def test_bad_timestamp_is_400(self):
        r = self.api.get("/api/main/products/list/", {"updated_since": "вчера"})
        self.assertEqual(r.status_code, 400)


class AbcAnalyticsTests(KassaBase):
    """BE2-13: ABC на сервере вместо выгрузки всех чеков."""

    def test_abc_by_product_revenue_and_profit(self):
        from django.core.cache import cache
        from django.utils import timezone

        cache.clear()
        plan = {"A-товар": ("700.00", "400.00"), "B-товар": ("200.00", "50.00"),
                "B2-товар": ("70.00", "10.00"), "C-товар": ("30.00", "29.00")}
        for name, (price, cost) in plan.items():
            p = Product.objects.create(company=self.company, name=name, price=Decimal(price),
                                       purchase_price=Decimal(cost), quantity=Decimal("10"))
            r = self.quick(items=[{"product": str(p.id), "qty": "1", "price": price}],
                           payment={"method": "cash", "received": price})
            self.assertEqual(r.status_code, 201, r.data)

        today = timezone.localdate().isoformat()
        r = self.api.get("/api/main/analytics/market/", {"tab": "abc", "date_from": today, "date_to": today})
        self.assertEqual(r.status_code, 200, r.data)
        rows = [(i["name"], i["share"], i["cum_share"], i["class"]) for i in r.data["items"]]
        self.assertEqual(rows, [("A-товар", 70.0, 70.0, "A"), ("B-товар", 20.0, 90.0, "B"),
                                ("B2-товар", 7.0, 97.0, "C"), ("C-товар", 3.0, 100.0, "C")])
        self.assertEqual(r.data["totals"]["revenue"], "1000.00")
        self.assertEqual(r.data["thresholds"], {"A": 80, "B": 95})

        r = self.api.get("/api/main/analytics/market/",
                         {"tab": "abc", "metric": "profit", "date_from": today, "date_to": today})
        self.assertEqual([(i["name"], i["profit"]) for i in r.data["items"]][:2],
                         [("A-товар", "300.00"), ("B-товар", "150.00")])

        r = self.api.get("/api/main/analytics/market/", {"tab": "abc", "by": "shelf"})
        self.assertEqual(r.status_code, 400)


class DebtV2MonthsTests(KassaBase):
    """08-debt-v2-months-xor: срок в месяцах к сделке, созданной продажей в долг."""

    def _url(self):
        return f"/api/main/clients/{self.client_obj.id}/deals/"

    def _body(self, **extra):
        from datetime import date, timedelta

        return {"title": "Рассрочка", "kind": "debt", "amount": "4000.00", "prepayment": "0.00", "schedule_version": "v2",
                "first_due_date": (date.today() + timedelta(days=30)).isoformat(), **extra}

    def test_months_on_deal_created_by_debt_sale(self):
        sale = Sale.objects.create(company=self.company, user=self.owner, client=self.client_obj, cashbox=self.cashbox,
                                   shift=self.shift, status=Sale.Status.DEBT, total=Decimal("4000.00"))
        # так продажа в долг создаёт сделку, когда срок в чеке не передан
        ClientDeal.objects.create(company=self.company, client=self.client_obj, sale=sale, kind=ClientDeal.Kind.DEBT,
                                  title=f"Продажа в долг №{sale.id}",
                                  amount=Decimal("4000.00"), debt_days=30, schedule_version="v2")
        r = self.api.post(self._url(), self._body(sale_id=str(sale.id), debt_months=4, interval_months=1),
                          format="json")
        self.assertIn(r.status_code, (200, 201), r.data)
        deal = ClientDeal.objects.get(sale=sale)
        self.assertEqual((deal.debt_months, deal.debt_days), (4, None))
        self.assertEqual(deal.installments.count(), 4)

    def test_new_deal_by_months_and_by_days(self):
        r = self.api.post(self._url(), self._body(debt_months=4, interval_months=1), format="json")
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(ClientDeal.objects.get(pk=r.data["id"]).installments.count(), 4)

        r = self.api.post(self._url(), self._body(debt_days=10, interval_days=2), format="json")
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(ClientDeal.objects.get(pk=r.data["id"]).debt_months, None)

    def test_both_in_body_is_still_400(self):
        r = self.api.post(self._url(), self._body(debt_days=10, debt_months=4), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("debt_months", r.data)


class SaleDealLinkageTests(KassaBase):
    """09-sale-deal-linkage: продажа в долг следует за погашением своей сделки."""

    def _debt_sale(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.quick(client=str(self.client_obj.id), shift=str(self.shift.id), payment={"method": "debt"})
        self.assertEqual(r.status_code, 201, r.data)
        sale = Sale.objects.get(id=r.data["id"])
        self.assertEqual(sale.status, Sale.Status.DEBT)
        return sale, ClientDeal.objects.get(sale=sale)

    def _pay(self, deal, inst):
        url = f"/api/main/clients/{self.client_obj.id}/deals/{deal.id}/pay/"
        inst.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(url, {"installment_id": str(inst.id), "amount": str(inst.amount - inst.paid_amount),
                                    "idempotency_key": str(uuid.uuid4()), "payment_method": "cash",
                                    "cashbox_id": str(self.cashbox.id), "shift_id": str(self.shift.id)},
                              format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_full_repayment_marks_sale_paid_and_refund_reverts(self):
        sale, deal = self._debt_sale()
        listed = self.api.get("/api/main/pos/sales/").data["results"][0]
        self.assertEqual((listed["deal_id"], listed["debt_amount"]), (str(deal.id), Decimal("200.00")))

        installments = list(deal.installments.order_by("number"))
        self._pay(deal, installments[0])  # частично — продажа остаётся в долге, остаток виден
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.DEBT)
        left = Decimal("200.00") - installments[0].amount
        listed = self.api.get("/api/main/pos/sales/").data["results"][0]
        self.assertEqual(listed["debt_amount"], left)
        detail = self.api.get(f"/api/main/pos/sales/{sale.id}/").data
        self.assertEqual((detail["deal_id"], Decimal(detail["remaining_debt"])), (str(deal.id), left))

        for inst in installments[1:]:
            self._pay(deal, inst)
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.PAID)
        self.assertEqual(sale.payment_method, Sale.PaymentMethod.DEBT)
        self.assertEqual(self.api.get("/api/main/pos/sales/").data["results"][0]["debt_amount"], Decimal("0.00"))

        url = f"/api/main/clients/{self.client_obj.id}/deals/{deal.id}/refund/"
        with self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(url, {"amount": "1.00", "idempotency_key": str(uuid.uuid4())}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.DEBT)

    def test_deal_without_sale_is_ignored(self):
        from apps.main.models import sync_sale_status_from_deal

        deal = ClientDeal.objects.create(company=self.company, client=self.client_obj, title="Без продажи",
                                         kind=ClientDeal.Kind.DEBT, amount=Decimal("100.00"), debt_months=1)
        sync_sale_status_from_deal(deal.id)  # не падает и ничего не трогает


class ShiftReturnsReportingTests(KassaBase):
    """11-shift-returns-reporting: возвраты в итогах смены, по факту выдачи денег."""

    def _shift(self, shift=None):
        r = self.api.get(f"/api/construction/shifts/{(shift or self.shift).id}/")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _return(self, sale_id, **body):
        r = self.api.post(f"/api/main/pos/sales/{sale_id}/return/", body, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_full_and_partial_returns_by_actual_refund_method(self):
        from apps.main.models import SaleItem

        full_id = self.quick(shift=str(self.shift.id)).data["id"]  # 200 наличными
        self._return(full_id)
        part_id = self.quick(shift=str(self.shift.id)).data["id"]  # 200 наличными
        item = SaleItem.objects.get(sale_id=part_id)
        self._return(part_id, items=[{"sale_item_id": str(item.id), "quantity": 1}], refund_method="mbank")

        data = self._shift()
        self.assertEqual(
            (data["returns_count"], data["returns_total"], data["returns_cash"], data["returns_noncash"]),
            (2, "300.00", "200.00", "100.00"),
        )
        # полностью возвращённый чек не в продажах, от частичного осталась половина
        self.assertEqual(Decimal(data["sales_total"]), Decimal("100.00"))
        report = self.api.get(f"/api/construction/shifts/{self.shift.id}/report/").data
        self.assertEqual((report["returns_cash"], report["returns_noncash"]), ("200.00", "100.00"))

        listed = self.api.get("/api/construction/shifts/").data
        row = next(s for s in listed.get("results", listed) if s["id"] == str(self.shift.id))
        self.assertEqual(row["returns_count"], 2)

    def test_return_counts_in_shift_where_it_was_made(self):
        sale_id = self.quick(shift=str(self.shift.id)).data["id"]
        self.shift.close(closing_cash=Decimal("1200.00"))
        new_shift = CashShift.objects.create(company=self.company, cashbox=self.cashbox, cashier=self.owner,
                                             status=CashShift.Status.OPEN, opening_cash=Decimal("0.00"))
        self._return(sale_id)
        self.assertEqual(self._shift()["returns_count"], 0)
        data = self._shift(new_shift)
        self.assertEqual((data["returns_count"], data["returns_cash"]), (1, "200.00"))

    def test_debt_sale_return_gives_no_money(self):
        sale_id = self.quick(client=str(self.client_obj.id), shift=str(self.shift.id),
                             payment={"method": "debt"}).data["id"]
        self._return(sale_id)
        data = self._shift()
        self.assertEqual(
            (data["returns_count"], data["returns_total"], data["returns_cash"], data["returns_noncash"]),
            (1, "200.00", "0.00", "0.00"),
        )


class MassIncomingBatchTests(KassaBase):
    """mass-incoming-batch: «Провести приход» одним запросом, всё или ничего."""

    URL = "/api/main/products/mass-incoming/"

    def setUp(self):
        super().setUp()
        from apps.main.models import GlobalBrand, GlobalProduct

        self.product.quantity = Decimal("5")
        self.product.save()
        self.other = Product.objects.create(company=self.company, name="Молоко", price=Decimal("100.00"),
                                            quantity=Decimal("1"))
        GlobalProduct.objects.create(name="Кофе глобальный", barcode="4601234567890",
                                     brand=GlobalBrand.objects.create(name="Nescafe"))

    def _post(self, items):
        return self.api.post(self.URL, {"comment": "Массовое сканирование", "items": items}, format="json")

    def test_adds_to_current_stock_and_updates_price(self):
        Product.objects.filter(pk=self.product.pk).update(quantity=Decimal("3"))  # между сканом и приходом продали 2
        r = self._post([{"product_id": str(self.product.id), "quantity": "10"},
                        {"product_id": str(self.other.id), "quantity": "1", "price": "120"}])
        self.assertEqual(r.status_code, 200, r.data)
        self.product.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("13"))
        self.assertEqual((self.other.quantity, self.other.price), (Decimal("2"), Decimal("120.00")))
        row = r.data["items"][0]  # ТЗ 29.10 Б-42: к старым полям добавлены client_row_id/unit/price…
        self.assertEqual({k: row[k] for k in ("product_id", "quantity")}, {"product_id": str(self.product.id), "quantity": "13.000"})

    def test_creates_from_global_and_manually(self):
        r = self._post([
            {"barcode": "4601234567890", "name": "Кофе 90г", "price": "300", "quantity": "5", "from_global": True},
            {"barcode": "4601234567891", "name": "Скотч", "price": "120", "quantity": "2", "from_global": False},
        ])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual([c["barcode"] for c in r.data["created"]], ["4601234567890", "4601234567891"])
        coffee = Product.objects.get(company=self.company, barcode="4601234567890")
        self.assertEqual((coffee.name, coffee.price, coffee.quantity, coffee.brand.name),
                         ("Кофе 90г", Decimal("300.00"), Decimal("5"), "Nescafe"))
        self.assertEqual(Product.objects.get(company=self.company, barcode="4601234567891").quantity, Decimal("2"))

    def assertNothingChanged(self, count_before):
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("5"))
        self.assertEqual(Product.objects.filter(company=self.company).count(), count_before)

    def test_all_or_nothing(self):
        from apps.users.models import Company as CompanyModel

        foreign_owner = User.objects.create_user(email=f"f{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        foreign = Product.objects.create(
            company=CompanyModel.objects.create(name="Чужая", owner=foreign_owner, subscription_plan=self.plan),
            name="Чужой", price=Decimal("1"), quantity=Decimal("1"),
        )
        before = Product.objects.filter(company=self.company).count()
        cases = [
            ([{"product_id": str(self.product.id), "quantity": "10"},
              {"barcode": "4601234567890", "name": "Кофе", "price": "300", "quantity": "5", "from_global": True},
              {"product_id": str(foreign.id), "quantity": "1"}], 404),
            ([{"product_id": str(self.product.id), "quantity": "10"},
              {"barcode": "0000000000000", "name": "Нет в базе", "price": "1", "quantity": "1", "from_global": True}],
             400),
            ([{"product_id": str(self.product.id), "quantity": "10"},
              {"barcode": self.other.barcode or "dup", "name": "Дубль", "price": "1", "quantity": "1",
               "from_global": False}], None),
            ([{"product_id": str(self.product.id), "quantity": "-1"}], 400),
            ([{"barcode": "123", "name": "Без цены", "quantity": "1", "from_global": False}], 400),
        ]
        Product.objects.filter(pk=self.other.pk).update(barcode="dup")
        for items, want in cases:
            r = self._post(items)
            self.assertEqual(r.status_code, want or 409, r.data)
            self.assertIn("detail", r.data)
            self.assertNothingChanged(before)

    def test_bad_body(self):
        self.assertEqual(self._post([]).status_code, 400)
        self.assertEqual(self.api.post(self.URL, {}, format="json").status_code, 400)
