"""
Тесты ТЗ-28 (apps.possync). Запуск:
    python manage.py test apps.possync --settings=core.settings_test_sqlite
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashShift
from apps.main.models import Client, ClientDeal, Product, ProductDeletion, ProductVariant, Sale
from apps.users.models import Company, SubscriptionPlan

from apps.integrations.models import IdempotencyRecord

from .models import StockAdjustment

User = get_user_model()


class PossyncBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.plan = SubscriptionPlan.objects.create(name="Стандарт", price=Decimal("1.00"))
        self.company = Company.objects.create(name="Sync Co", owner=self.owner, is_active=True, subscription_plan=self.plan)
        self.owner.company = self.company
        self.owner.save()
        self.cashbox = Cashbox.objects.create(name="Касса 1", company=self.company, role=Cashbox.CashboxRole.POS_MAIN)
        self.product = Product.objects.create(company=self.company, name="Хлеб", price=Decimal("100.00"), quantity=Decimal("50"))
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def open_shift(self):
        return CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=self.owner,
            status=CashShift.Status.OPEN, opening_cash=Decimal("1000.00"),
        )

    def movement(self, key=None, **body):
        payload = {
            "kind": "receive",
            "reason": "тест",
            "lines": [{"product_id": str(self.product.id), "delta": "10"}],
        }
        payload.update(body)
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.api.post("/api/main/stock/movements/", payload, format="json", **headers)


class ErrorFormatTests(PossyncBase):
    def test_validation_error_has_code_fields_detail(self):
        r = self.movement(kind="nope")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "validation_error")
        self.assertIn("kind", r.data["fields"])
        self.assertTrue(r.data["detail"])
        # совместимость: ошибка поля осталась и на верхнем уровне
        self.assertIn("kind", r.data)

    def test_not_found_code(self):
        r = self.api.get(f"/api/main/stock/movements/?product_id={uuid.uuid4()}")
        self.assertEqual(r.status_code, 200)
        r = self.api.get("/api/construction/shifts/current/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.data["code"], "shift_not_open")

    def test_request_id_and_server_time_headers(self):
        r = self.api.get("/api/health/", HTTP_X_REQUEST_ID="abc-123")
        self.assertEqual(r["X-Request-Id"], "abc-123")
        self.assertTrue(r["X-Server-Time"])
        r = self.api.get("/api/health/")
        self.assertEqual(len(r["X-Request-Id"]), 32)


class StockMovementTests(PossyncBase):
    def test_receive_adds_delta_and_writes_document(self):
        r = self.movement(key="k1")
        self.assertEqual(r.status_code, 201, r.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("60.000"))
        line = r.data["lines"][0]
        self.assertEqual(line["before"], "50.000")
        self.assertEqual(line["after"], "60.000")
        self.assertTrue(r.data["number"].startswith("ПР-"))
        self.assertIn("catalog_version", r.data)
        self.assertEqual(StockAdjustment.objects.count(), 1)

    def test_replay_same_key_same_body_does_not_apply_twice(self):
        r1 = self.movement(key="k2")
        r2 = self.movement(key="k2")
        self.assertEqual(r2.status_code, 201, r2.data)
        self.assertEqual(r2["Idempotent-Replayed"], "true")
        self.assertEqual(r1.data["id"], r2.data["id"])
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("60.000"))
        self.assertEqual(StockAdjustment.objects.count(), 1)

    def test_same_key_other_body_is_conflict(self):
        self.movement(key="k3")
        r = self.movement(key="k3", lines=[{"product_id": str(self.product.id), "delta": "99"}])
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["code"], "idempotency_conflict")
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("60.000"))

    def test_write_off_below_zero_is_insufficient_stock(self):
        r = self.movement(kind="write_off", lines=[{"product_id": str(self.product.id), "delta": "-70"}])
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["code"], "insufficient_stock")
        self.assertEqual(r.data["data"]["available"], "50.000")
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("50"))

    def test_count_fix_set_to(self):
        r = self.movement(kind="count_fix", lines=[{"product_id": str(self.product.id), "set_to": "42"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["lines"][0]["delta"], "-8.000")
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("42.000"))

    def test_bumps_updated_at_for_delta_sync(self):
        before = self.product.updated_at
        self.movement()
        self.product.refresh_from_db()
        self.assertGreater(self.product.updated_at, before)

    def test_list(self):
        self.movement(key="a")
        self.movement(key="b", kind="write_off", lines=[{"product_id": str(self.product.id), "delta": "-1"}])
        r = self.api.get("/api/main/stock/movements/?kind=write_off")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["kind"], "write_off")

    def test_other_company_product_not_found(self):
        other_owner = User.objects.create_user(email=f"x{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        other = Company.objects.create(name="Other", owner=other_owner, subscription_plan=self.plan)
        p = Product.objects.create(company=other, name="Чужой", price=Decimal("1"), quantity=Decimal("1"))
        r = self.movement(lines=[{"product_id": str(p.id), "delta": "1"}])
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.data["code"], "not_found")


class ProductsStockTests(PossyncBase):
    def test_stock_only_and_since(self):
        r = self.api.get("/api/main/products/stock/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["items"][0]["q"], "50.000")
        future = (timezone.now() + timedelta(minutes=1)).isoformat()
        r = self.api.get(f"/api/main/products/stock/?updated_since={future}")
        self.assertEqual(r.data["count"], 0)
        ms = int((timezone.now() - timedelta(days=1)).timestamp() * 1000)
        r = self.api.get(f"/api/main/products/stock/?updated_after={ms}")
        self.assertEqual(r.data["count"], 1)

    def test_deleted_ids_in_delta(self):
        gone = uuid.uuid4()
        ProductDeletion.objects.create(company=self.company, product_id=gone)
        since = (timezone.now() - timedelta(minutes=5)).isoformat()
        r = self.api.get(f"/api/main/products/stock/?updated_since={since}")
        self.assertIn(str(gone), r.data["deleted"])

    def test_products_list_updated_after_ms(self):
        ms = int((timezone.now() - timedelta(days=1)).timestamp() * 1000)
        r = self.api.get(f"/api/main/products/list/?updated_after={ms}")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("deleted", r.data)
        self.assertIn("server_time_ms", r.data)
        self.assertIn("catalog_version", r.data)


class ShiftCurrentTests(PossyncBase):
    def test_current_shift(self):
        shift = self.open_shift()
        r = self.api.get(f"/api/construction/shifts/current/?cashbox={self.cashbox.id}")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["id"], str(shift.id))
        self.assertEqual(r.data["opening_cash"], "1000.00")
        self.assertIn("expected_cash", r.data)
        self.assertIn("by_method", r.data)
        self.assertTrue(r.data["is_own"])

    def test_no_shift(self):
        r = self.api.get(f"/api/construction/shifts/current/?cashbox={self.cashbox.id}")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.data["code"], "shift_not_open")


class BootstrapTests(PossyncBase):
    def test_bootstrap_shape(self):
        shift = self.open_shift()
        r = self.api.get(f"/api/main/bootstrap/?cashbox_id={self.cashbox.id}&device_id=dev-1")
        self.assertEqual(r.status_code, 200, r.data)
        for key in ("server_time", "user", "company", "permissions", "features", "cashboxes", "shift",
                    "catalog_version", "catalog_cursor", "settings"):
            self.assertIn(key, r.data)
        self.assertEqual(r.data["user"]["id"], str(self.owner.id))
        self.assertTrue(r.data["user"]["is_owner"])
        self.assertEqual(r.data["company"]["id"], str(self.company.id))
        self.assertEqual(r.data["cashboxes"][0]["id"], str(self.cashbox.id))
        self.assertEqual(r.data["shift"]["id"], str(shift.id))
        self.assertEqual(r.data["device_id"], "dev-1")

    def test_bootstrap_without_shift(self):
        r = self.api.get("/api/main/bootstrap/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(r.data["shift"])


class ClientDebtsTests(PossyncBase):
    def test_debts_get_and_post_still_works(self):
        client = Client.objects.create(company=self.company, full_name="Айбек", phone="+996555000222")
        ClientDeal.objects.create(
            company=self.company, client=client, kind=ClientDeal.Kind.DEBT,
            amount=Decimal("1000.00"), prepayment=Decimal("200.00"), debt_days=1,
            first_due_date=timezone.localdate() + timedelta(days=10), title="Долг",
        )
        r = self.api.get(f"/api/main/clients/{client.id}/debts/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["total"], "800.00")
        self.assertEqual(len(r.data["items"]), 1)
        self.assertEqual(r.data["items"][0]["remaining"], "800.00")
        self.assertEqual(r.data["items"][0]["original"], "1000.00")

        # старый POST (прямой долг) работает как раньше
        r = self.api.post(f"/api/main/clients/{client.id}/debts/", {"amount": "300.00"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.api.get(f"/api/main/clients/{client.id}/debts/")
        self.assertEqual(r.data["total"], "1100.00")
        self.assertEqual(len(r.data["items"]), 2)

    def test_empty(self):
        client = Client.objects.create(company=self.company, full_name="Без долгов", phone="+996555000333")
        r = self.api.get(f"/api/main/clients/{client.id}/debts/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["total"], "0.00")
        self.assertEqual(r.data["items"], [])


class CheckoutIdempotencyTests(PossyncBase):
    def quick(self, key, **body):
        payload = {
            "items": [{"product": str(self.product.id), "qty": "2", "price": "100.00"}],
            "payment": {"method": "cash", "received": "500.00"},
        }
        payload.update(body)
        return self.api.post("/api/main/pos/checkout/", payload, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_replay_and_conflict(self):
        self.open_shift()
        r1 = self.quick("sale-1")
        self.assertEqual(r1.status_code, 201, r1.data)
        r2 = self.quick("sale-1")
        self.assertIn(r2.status_code, (200, 201), r2.data)
        self.assertTrue(r2.data.get("replayed"))
        self.assertEqual(r1.data["id"], r2.data["id"])
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 1)

        r3 = self.quick("sale-1", items=[{"product": str(self.product.id), "qty": "5", "price": "100.00"}])
        self.assertEqual(r3.status_code, 409, r3.data)
        self.assertEqual(r3.data["code"], "idempotency_conflict")
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 1)
        self.assertEqual(IdempotencyRecord.objects.filter(company=self.company, key="sale-1").count(), 1)

    def test_sale_decrement_bumps_updated_at(self):
        self.open_shift()
        before = self.product.updated_at
        self.quick("sale-2")
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("48.000"))
        self.assertGreater(self.product.updated_at, before)


class ClientCreateIdempotencyTests(PossyncBase):
    def test_double_click_creates_one_client(self):
        body = {"full_name": "Дубль", "phone": "+996700123456"}
        r1 = self.api.post("/api/main/clients/", body, format="json", HTTP_IDEMPOTENCY_KEY="c-1")
        self.assertEqual(r1.status_code, 201, r1.data)
        r2 = self.api.post("/api/main/clients/", body, format="json", HTTP_IDEMPOTENCY_KEY="c-1")
        self.assertEqual(r2.status_code, 201, r2.data)
        self.assertEqual(r2["Idempotent-Replayed"], "true")
        self.assertEqual(Client.objects.filter(company=self.company, full_name="Дубль").count(), 1)


class BarcodeVariantTests(PossyncBase):
    def test_variant_barcode_found(self):
        shirt = Product.objects.create(company=self.company, name="Футболка", price=Decimal("500"), quantity=Decimal("3"), barcode="4600000000001")
        v = ProductVariant.objects.create(company=self.company, product=shirt, size="M", color="белый", barcode="2000000000017", quantity=Decimal("3"))
        r = self.api.get("/api/main/products/barcode/2000000000017/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["id"], str(shirt.id))
        self.assertEqual(r.data["variant"]["id"], str(v.id))
        self.assertEqual(r.data["matched_by"], "variant_barcode")
        # основной штрихкод — как раньше, без variant
        r = self.api.get("/api/main/products/barcode/4600000000001/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("variant", r.data)
        r = self.api.get("/api/main/products/barcode/0000000000000/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.data["code"], "not_found")


class CheckoutEnrichTests(PossyncBase):
    def test_checkout_response_has_shift_and_stock(self):
        self.open_shift()
        payload = {
            "items": [{"product": str(self.product.id), "qty": "2", "price": "100.00"}],
            "payment": {"method": "cash", "received": "500.00"},
        }
        r = self.api.post("/api/main/pos/checkout/", payload, format="json", HTTP_IDEMPOTENCY_KEY="s-enrich")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["stock"][0]["quantity"], "48.000")
        self.assertEqual(r.data["shift_totals"]["sales_count"], 1)
        self.assertIn("catalog_version", r.data)
