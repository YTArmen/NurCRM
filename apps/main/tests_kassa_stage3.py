"""Касса NurMarket, этап 3: варианты, склады, обмен, мастер, запись, заказ-наряды."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.main.models import (
    Cart,
    MarketAppointment,
    Product,
    ProductStock,
    ProductVariant,
    Sale,
    SaleExchange,
    SaleItem,
    Warehouse,
    WorkOrder,
)
from apps.main.tests_kassa_api import KassaBase


class VariantTests(KassaBase):
    def setUp(self):
        super().setUp()
        self.shirt = Product.objects.create(company=self.company, name="Рубашка", price=Decimal("2500"), quantity=0)

    def _variant(self, size, qty, barcode=None, price=None):
        r = self.api.post(f"/api/main/products/{self.shirt.id}/variants/",
                          {"size": size, "color": "чёрный", "quantity": qty, "barcode": barcode, "price": price},
                          format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return ProductVariant.objects.get(pk=r.data["id"])

    def test_variants_sync_stock_scan_and_sell(self):
        m = self._variant("M", "3", barcode="2000000012345", price="2600.00")
        self._variant("L", "2")
        self.shirt.refresh_from_db()
        self.assertEqual(self.shirt.quantity, Decimal("5"))

        cart = Cart.objects.create(company=self.company, user=self.owner, shift=self.shift, status=Cart.Status.ACTIVE)
        r = self.api.post(f"/api/main/pos/sales/{cart.id}/scan/", {"barcode": "2000000012345"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        item = cart.items.get()
        self.assertEqual((item.variant_id, item.unit_price), (m.id, Decimal("2600.00")))

        r = self.api.post(f"/api/main/pos/sales/{cart.id}/checkout/", {"payment_method": "cash", "cash_received": "2600"},
                          format="json")
        self.assertEqual(r.status_code, 201, r.data)
        m.refresh_from_db()
        self.shirt.refresh_from_db()
        self.assertEqual((m.quantity, self.shirt.quantity), (Decimal("2"), Decimal("4")))
        detail = self.api.get(f"/api/main/pos/sales/{r.data['sale_id']}/").data
        self.assertEqual((detail["items"][0]["variant_size"], detail["items"][0]["variant_color"]), ("M", "чёрный"))

    def test_cannot_sell_more_than_variant_stock(self):
        m = self._variant("M", "1")
        self._variant("L", "5")
        r = self.quick(items=[{"product": str(self.shirt.id), "variant": str(m.id), "qty": "2"}],
                       payment={"method": "cash", "received": "10000"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Недостаточно остатка", r.data["detail"])

    def test_duplicate_barcode_rejected(self):
        self._variant("M", "1", barcode="111")
        r = self.api.post(f"/api/main/products/{self.shirt.id}/variants/", {"size": "L", "barcode": "111"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_exchange_size_with_difference(self):
        m = self._variant("M", "3")
        xl = self._variant("XL", "3", price="2800.00")
        sale_id = self.quick(items=[{"product": str(self.shirt.id), "variant": str(m.id), "qty": "1"}],
                             payment={"method": "cash", "received": "2500"}).data["id"]
        cash_before = self.shift.calc_live_totals(refresh=True)["expected_cash"]
        item = SaleItem.objects.get(sale_id=sale_id)

        r = self.api.post(f"/api/main/pos/sales/{sale_id}/exchange/", {
            "return_items": [{"item": str(item.id), "qty": "1"}],
            "new_items": [{"product": str(self.shirt.id), "variant": str(xl.id), "qty": "1"}],
            "payment": {"method": "cash", "received": "300"},
        }, format="json", HTTP_IDEMPOTENCY_KEY="ex-1")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["difference"], "300.00")
        m.refresh_from_db()
        xl.refresh_from_db()
        self.assertEqual((m.quantity, xl.quantity), (Decimal("3"), Decimal("2")))
        new_sale = Sale.objects.get(pk=r.data["new_sale"])
        self.assertEqual({p.method: p.amount for p in new_sale.payments.all()},
                         {"offset": Decimal("2500.00"), "cash": Decimal("300.00")})
        # в ящик пришла только доплата
        self.assertEqual(self.shift.calc_live_totals(refresh=True)["expected_cash"], cash_before + Decimal("300.00"))

        again = self.api.post(f"/api/main/pos/sales/{sale_id}/exchange/", {}, format="json", HTTP_IDEMPOTENCY_KEY="ex-1")
        self.assertEqual(again.data["exchange"], r.data["exchange"])
        self.assertEqual(SaleExchange.objects.count(), 1)

    def test_offset_not_allowed_from_kassa(self):
        r = self.quick(payment={"method": "cash", "payments": [{"method": "offset", "amount": "200.00"}]})
        self.assertEqual(r.status_code, 400)


class StockTransferTests(KassaBase):
    def test_transfer_between_warehouse_and_shop(self):
        wh = Warehouse.objects.create(company=self.company, name="Склад")
        r = self.api.post("/api/main/stock-transfers/", {
            "from": None, "to": str(wh.id), "items": [{"product": str(self.product.id), "qty": "20"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("30"))
        self.assertEqual(ProductStock.objects.get(warehouse=wh).quantity, Decimal("20"))

        r = self.api.post("/api/main/stock-transfers/", {
            "from": str(wh.id), "to": None, "items": [{"product": str(self.product.id), "qty": "25"}],
        }, format="json")
        self.assertEqual(r.status_code, 400)
        stocks = self.api.get(f"/api/main/products/{self.product.id}/stocks/").data
        self.assertEqual((stocks["shop"], stocks["total"]), ("30.000", "50.000"))


class ServiceTests(KassaBase):
    def setUp(self):
        super().setUp()
        self.haircut = Product.objects.create(
            company=self.company, name="Стрижка", price=Decimal("1000"), kind="service",
            duration_min=45, performer_commission_percent=Decimal("40"),
        )

    def test_appointment_to_sale_pays_master(self):
        start = timezone.now().replace(microsecond=0) + timedelta(hours=1)
        body = {"client": str(self.client_obj.id), "service": str(self.haircut.id),
                "performer": str(self.cashier.id), "start": start.isoformat()}
        r = self.api.post("/api/main/appointments/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        ap = MarketAppointment.objects.get(pk=r.data["id"])
        self.assertEqual(ap.end - ap.start, timedelta(minutes=45))
        clash = self.api.post("/api/main/appointments/", {**body, "start": (start + timedelta(minutes=30)).isoformat()},
                              format="json")
        self.assertEqual(clash.status_code, 400)

        day = self.api.get(f"/api/main/appointments/?date={timezone.localdate(start).isoformat()}&performer={self.cashier.id}")
        self.assertEqual(len(day.data), 1)

        r = self.api.post(f"/api/main/appointments/{ap.id}/to-sale/")
        self.assertEqual(r.status_code, 201, r.data)
        cart_id = r.data["sale"]
        r = self.api.post(f"/api/main/pos/sales/{cart_id}/checkout/", {"payment_method": "cash", "cash_received": "1000"},
                          format="json")
        self.assertEqual(r.status_code, 201, r.data)
        item = SaleItem.objects.get(sale_id=r.data["sale_id"])
        self.assertEqual((item.performer_id, item.performer_commission_amount), (self.cashier.id, Decimal("400.00")))

        salary = self.api.get("/api/main/analytics/market/?tab=salary").data
        row = next(x for x in salary["rows"] if x["user_id"] == str(self.cashier.id))
        self.assertEqual(row["performer_commission_period"], "400.00")

    def test_work_order_prepayment_and_issue(self):
        r = self.api.post("/api/main/work-orders/", {
            "client": str(self.client_obj.id),
            "items": [{"custom": True, "name": "Замена экрана", "price": "3000", "qty": "1", "performer": str(self.cashier.id)},
                      {"product": str(self.product.id), "qty": "1"}],
            "prepayment": "1000.00", "prepayment_method": "cash", "description": "iPhone 11",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["number"], r.data["total"], r.data["left_to_pay"]), (1, "3100.00", "2100.00"))
        self.assertEqual(self.shift.calc_live_totals(refresh=True)["expected_cash"], Decimal("2000.00"))

        oid = r.data["id"]
        self.assertEqual(self.api.patch(f"/api/main/work-orders/{oid}/", {"status": "ready"}, format="json").data["status"], "ready")
        r = self.api.post(f"/api/main/work-orders/{oid}/issue/", {"payment": {"method": "cash", "received": "2100"}}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "issued")
        sale = Sale.objects.get(pk=r.data["sale"])
        self.assertEqual(sale.total, Decimal("3100.00"))
        self.assertEqual({p.method: p.amount for p in sale.payments.all()},
                         {"offset": Decimal("1000.00"), "cash": Decimal("2100.00")})
        self.assertEqual(self.shift.calc_live_totals(refresh=True)["expected_cash"], Decimal("4100.00"))
        self.assertEqual(WorkOrder.objects.get(pk=oid).sale_id, sale.id)
        again = self.api.post(f"/api/main/work-orders/{oid}/issue/", {"payment": {"method": "cash"}}, format="json")
        self.assertTrue(again.data["replayed"])

    def test_cancel_work_order_refunds_prepayment(self):
        r = self.api.post("/api/main/work-orders/", {
            "items": [{"custom": True, "name": "Диагностика", "price": "500", "qty": "1"}],
            "prepayment": "500", "prepayment_method": "cash",
        }, format="json")
        self.api.patch(f"/api/main/work-orders/{r.data['id']}/", {"status": "canceled"}, format="json")
        self.assertEqual(self.shift.calc_live_totals(refresh=True)["expected_cash"], Decimal("1000.00"))


class RentalTests(KassaBase):
    def setUp(self):
        super().setUp()
        self.dress = Product.objects.create(company=self.company, name="Платье", price=Decimal("3000"), quantity=0)
        self.m = ProductVariant.objects.create(company=self.company, product=self.dress, size="M", quantity=Decimal("2"))
        self.dress.quantity = Decimal("2")
        self.dress.save()

    def _rent(self, **extra):
        today = timezone.localdate()
        body = {"client": str(self.client_obj.id), "items": [{"variant": str(self.m.id)}],
                "date_from": today.isoformat(), "date_to": (today + timedelta(days=2)).isoformat(),
                "tariff": "сутки", "deposit_type": "money", "deposit_amount": "5000.00"}
        body.update(extra)
        r = self.api.post("/api/rentals/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_rent_and_return_ok(self):
        rent = self._rent()
        self.m.refresh_from_db()
        self.assertEqual(self.m.quantity, Decimal("1"))
        self.assertEqual(self.shift.calc_live_totals(refresh=True)["expected_cash"], Decimal("6000.00"))
        self.assertEqual(self.api.get("/api/rentals/?status=active", HTTP_X_NURMARKET_CLIENT="kassa/1.17.61").data["count"], 1)  # ТЗ Н-21: count/results

        r = self.api.post(f"/api/rentals/{rent['id']}/return/", {"condition": "ok"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["status"], r.data["deposit_refunded"]), ("returned", "5000.00"))
        self.m.refresh_from_db()
        self.assertEqual(self.m.quantity, Decimal("2"))
        self.assertEqual(self.shift.calc_live_totals(refresh=True)["expected_cash"], Decimal("1000.00"))
        self.assertTrue(self.api.post(f"/api/rentals/{rent['id']}/return/", {}, format="json").data["replayed"])

    def test_damaged_penalty_withheld_from_deposit(self):
        rent = self._rent()
        r = self.api.post(f"/api/rentals/{rent['id']}/return/", {"condition": "damaged", "penalty": "1500"},
                          format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["deposit_withheld"], r.data["deposit_refunded"]), ("1500.00", "3500.00"))
        sale = Sale.objects.get(pk=r.data["penalty_sale"])
        self.assertEqual({p.method: p.amount for p in sale.payments.all()}, {"offset": Decimal("1500.00")})
        # в ящике остались 1000 + 1500 удержанного штрафа
        self.assertEqual(self.shift.calc_live_totals(refresh=True)["expected_cash"], Decimal("2500.00"))

    def test_overdue_filter_and_document_deposit(self):
        past = timezone.localdate() - timedelta(days=5)
        self._rent(deposit_type="document", deposit_document="паспорт AN123",
                   date_from=past.isoformat(), date_to=(past + timedelta(days=1)).isoformat())
        rows = self.api.get("/api/rentals/?status=overdue", HTTP_X_NURMARKET_CLIENT="kassa/1.17.61").data["results"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["overdue"])
        self.assertEqual(rows[0]["deposit_amount"], "0.00")
