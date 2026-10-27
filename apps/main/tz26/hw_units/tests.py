# Тесты куска hw_units: python manage.py test apps.main.tz26.hw_units --settings=core.settings_test_sqlite
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashShift
from apps.main.models import Cart, CartItem, Product, ProductSaleUnit, Sale, SaleItem
from apps.users.models import Company, CompanyAddon, SubscriptionPlan

User = get_user_model()
NEW = "kassa/1.17.61"


class HwBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.plan = SubscriptionPlan.objects.create(name="Стандарт", price=Decimal("1.00"))
        self.company = Company.objects.create(name="Хозмаг", owner=self.owner, is_active=True, subscription_plan=self.plan)
        self.owner.company = self.company
        self.owner.save()
        self.cashbox = Cashbox.objects.create(name="Касса", company=self.company, role=Cashbox.CashboxRole.POS_MAIN)
        self.shift = CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=self.owner,
            status=CashShift.Status.OPEN, opening_cash=Decimal("1000.00"),
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def flag(self):
        CompanyAddon.objects.create(company=self.company, code="hardware_2026_11", active=True)

    def mk(self, **kw):
        kw.setdefault("name", "Тест м")
        kw.setdefault("price", Decimal("1.00"))
        kw.setdefault("quantity", Decimal("100"))
        return Product.objects.create(company=self.company, **kw)

    def co(self, items, key=None, client=None, **extra):
        body = {"items": items, "payment": {"method": "cash", "received": "100000.00"}}
        body.update(extra)
        headers = {"HTTP_IDEMPOTENCY_KEY": key or f"k-{uuid.uuid4()}"}
        if client:
            headers["HTTP_X_NURMARKET_CLIENT"] = client
        return self.api.post("/api/main/pos/checkout/", body, format="json", **headers)

    def ret(self, sale_id, item_id, qty):
        return self.api.post(
            f"/api/main/pos/sales/{sale_id}/return/",
            {"items": [{"sale_item_id": str(item_id), "quantity": str(qty)}]}, format="json",
        )


class FractionalQuantityTests(HwBase):
    """Б-52 п.1: дробное количество штучного товара проходит продажу и возврат."""

    def test_sale_2_5_then_return_1_5_and_replay(self):
        p = self.mk(unit_code="m", is_weight=False, quantity_step=Decimal("0.1"), quantity=Decimal("10"))
        key = f"k-{uuid.uuid4()}"
        r = self.co([{"product": str(p.id), "qty": "2.5", "price": "1.00"}], key=key)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.assertEqual(r.data["items"][0]["line_total"], "2.50")
        p.refresh_from_db()
        self.assertEqual(p.quantity, Decimal("7.500"))
        sale_id, item_id = r.data["id"], r.data["items"][0]["id"]
        rr = self.ret(sale_id, item_id, "1.5")
        self.assertEqual(rr.status_code, 200, getattr(rr, "data", None))
        p.refresh_from_db()
        self.assertEqual(p.quantity, Decimal("9.000"))
        item = SaleItem.objects.get(pk=item_id)
        self.assertEqual(item.quantity, Decimal("1.000"))
        r2 = self.co([{"product": str(p.id), "qty": "2.5", "price": "1.00"}], key=key)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.data["replayed"])

    def test_returnable_qty_is_fractional(self):
        p = self.mk(unit_code="m2")
        r = self.co([{"product": str(p.id), "qty": "1.44", "price": "10.00"}])
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.assertEqual(r.data["items"][0]["line_total"], "14.40")
        d = self.api.get(f"/api/main/pos/sales/{r.data['id']}/")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(Decimal(str(d.data["items"][0]["returnable_qty"])), Decimal("1.440"))


class ProductFieldsTests(HwBase):
    def test_create_manual_and_patch_fields(self):
        r = self.api.post("/api/main/products/create-manual/", {
            "name": "Кабель", "price": "5.00", "unit_code": "m", "quantity_step": "0.1",
            "min_sale_quantity": "0.5", "quantity_decimals": 1,
        }, format="json", HTTP_IDEMPOTENCY_KEY=f"k-{uuid.uuid4()}")
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        self.assertEqual(r.data["unit_code"], "m")
        self.assertEqual(Decimal(str(r.data["quantity_step"])), Decimal("0.1"))
        self.assertEqual(r.data["quantity_decimals"], 1)
        pid = r.data["id"]
        r = self.api.patch(f"/api/main/products/{pid}/", {"quantity_step": "0.25", "unit_code": "kg"}, format="json")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        self.assertEqual(r.data["unit_code"], "kg")
        self.assertEqual(Decimal(str(r.data["quantity_step"])), Decimal("0.25"))
        self.assertEqual(Decimal(str(r.data["min_sale_quantity"])), Decimal("0.5"))  # не тронут
        r = self.api.patch(f"/api/main/products/{pid}/", {"quantity_step": None}, format="json")
        self.assertIsNone(r.data["quantity_step"])

    def test_validation(self):
        p = self.mk()
        for body in ({"unit_code": "zzz"}, {"quantity_step": "0"}, {"min_sale_quantity": "-1"}, {"quantity_decimals": 5}):
            r = self.api.patch(f"/api/main/products/{p.id}/", body, format="json")
            self.assertEqual(r.status_code, 400, (body, getattr(r, "data", None)))


class StepAndMinTests(HwBase):
    def setUp(self):
        super().setUp()
        self.p = self.mk(unit_code="m", quantity_step=Decimal("0.1"), min_sale_quantity=Decimal("1"))

    def line(self, qty):
        return [{"product": str(self.p.id), "qty": qty, "price": "1.00"}]

    def test_without_flag_nothing_checked(self):
        r = self.co(self.line("2.55"), client=NEW)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))

    def test_flag_but_old_client_not_checked(self):
        self.flag()
        r = self.co(self.line("2.55"))
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))

    def test_flag_new_client_step_and_min(self):
        self.flag()
        r = self.co(self.line("2.55"), client=NEW)
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "quantity_step_mismatch")
        self.assertEqual(r.data["errors"][0]["index"], 0)
        self.assertEqual(r.data["data"]["step"], "0.1")
        r = self.co(self.line("0.5"), client=NEW)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "quantity_below_min")
        self.assertEqual(r.data["data"]["min"], "1.000")
        self.assertEqual(Sale.objects.count(), 0)
        r = self.co(self.line("2.5"), client=NEW)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))

    def test_add_item_and_patch(self):
        self.flag()
        cart = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift,
                                   status=Cart.Status.ACTIVE)
        h = {"HTTP_X_NURMARKET_CLIENT": NEW}
        r = self.api.post(f"/api/main/pos/sales/{cart.id}/add-item/", {"product_id": str(self.p.id), "quantity": "2.55"},
                          format="json", **h)
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertEqual(r.data["code"], "quantity_step_mismatch")
        r = self.api.post(f"/api/main/pos/sales/{cart.id}/add-item/", {"product_id": str(self.p.id), "quantity": "2.5"},
                          format="json", **h)
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        item = CartItem.objects.get(cart=cart)
        r = self.api.patch(f"/api/main/pos/carts/{cart.id}/items/{item.id}/", {"quantity": "0.3"}, format="json", **h)
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        r = self.api.patch(f"/api/main/pos/carts/{cart.id}/items/{item.id}/", {"quantity": "3.1"}, format="json", **h)
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))

    def test_decimals_acts_as_step(self):
        self.flag()
        q = self.mk(name="Целый", quantity_decimals=0)
        r = self.co([{"product": str(q.id), "qty": "1.5", "price": "1.00"}], client=NEW)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "quantity_step_mismatch")
