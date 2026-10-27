"""Тесты куска cust_orders (Б-60).

python manage.py test apps.main.tz26.cust_orders --settings=core.settings_test_sqlite
"""
import uuid
from decimal import Decimal

from django.db.models import F

from apps.construction.models import CashFlow
from apps.main.models import ChangeEvent, Client, Product, PurchaseOrder, Sale
from apps.main.tests_kassa_api import KassaBase

from .models import CustomerOrder
from .services import create_draft_from_showcase

URL = "/api/main/customer-orders/"


def key():
    return uuid.uuid4().hex


class CustOrderBase(KassaBase):
    def setUp(self):
        super().setUp()
        # мешок: остаток 25, цена 100
        self.bag = Product.objects.create(company=self.company, name="Цемент", price=Decimal("100.00"),
                                          purchase_price=Decimal("60.00"), quantity=Decimal("25"))
        self.supplier = Client.objects.create(company=self.company, full_name="Завод", phone="+996555111000",
                                              type=Client.StatusClient.SUPPLIERS)

    def create(self, qty="40", price="100", status="draft", k=None, **extra):
        body = {"client": str(self.client_obj.id), "status": status,
                "items": [{"product": str(self.bag.id), "qty": qty, "price": price}], **extra}
        return self.api.post(URL, body, format="json", HTTP_IDEMPOTENCY_KEY=k or key())

    def act(self, oid, action, body=None, k=None):
        return self.api.post(f"{URL}{oid}/{action}/", body or {}, format="json", HTTP_IDEMPOTENCY_KEY=k or key())

    def expected_cash(self):
        return self.shift.calc_live_totals(refresh=True)["expected_cash"]


class CrudTests(CustOrderBase):
    def test_create_list_patch_and_replay(self):
        k = key()
        extra = {"delivery": {"type": "delivery", "address": "Бишкек", "floor": "3", "lift": False, "price": "50"},
                 "due_date": "2026-11-01"}
        r = self.create(k=k, **extra)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["number"], r.data["status"], r.data["source"]), (1, "draft", "kassa"))
        self.assertEqual((r.data["items_total"], r.data["total"]), ("4000.00", "4050.00"))
        self.assertEqual(r.data["delivery"]["address"], "Бишкек")
        again = self.create(k=k, **extra)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.data["replayed"])
        self.assertEqual(CustomerOrder.objects.count(), 1)

        lst = self.api.get(URL + "?status=draft")
        self.assertEqual((lst.status_code, lst.data["count"]), (200, 1))
        oid = r.data["id"]
        p = self.api.patch(f"{URL}{oid}/", {"status": "confirmed", "note": "позвонить"}, format="json")
        self.assertEqual((p.status_code, p.data["status"], p.data["version"]), (200, "confirmed", 2))
        bad = self.api.patch(f"{URL}{oid}/", {"status": "ready"}, format="json")
        self.assertEqual((bad.status_code, bad.data["code"]), (400, "status_transition_invalid"))
        conflict = self.api.patch(f"{URL}{oid}/", {"note": "x"}, format="json", HTTP_IF_MATCH="1")
        self.assertEqual((conflict.status_code, conflict.data["code"]), (409, "version_conflict"))
        no = self.api.patch(f"{URL}{oid}/", {"prepayment": {"amount": "1"}}, format="json")
        self.assertEqual(no.data["code"], "prepayment_via_action")

    def test_not_found(self):
        r = self.api.get(f"{URL}{uuid.uuid4()}/")
        self.assertEqual((r.status_code, r.data["code"]), (404, "not_found"))


class FlowTests(CustOrderBase):
    def test_x8_reserve_prepay_supply_deliver(self):
        # Заказ 40 мешков при остатке 25
        r = self.create(qty="40", status="confirmed")
        oid = r.data["id"]
        res = self.act(oid, "reserve", {"create_supplier_order": True, "supplier": str(self.supplier.id)})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], "waiting_supply")
        item = res.data["items"][0]
        self.assertEqual((item["reserved_qty"], item["waiting_qty"], item["status"]), ("25.000", "15.000", "waiting_supply"))
        po = PurchaseOrder.objects.get(pk=res.data["supplier_order"])
        self.assertEqual(po.status, "draft")
        self.assertEqual(po.lines.get().qty, Decimal("15.000"))
        self.assertEqual(res.data["supplier_orders"], [str(po.pk)])

        # повтор reserve тем же ключом ничего не плодит
        k = key()
        self.act(oid, "reserve", {}, k=k)
        again = self.act(oid, "reserve", {}, k=k)
        self.assertTrue(again.data["replayed"])
        self.assertEqual(PurchaseOrder.objects.count(), 1)

        # предоплата 1 сом: в ящике +1, выручки нет
        cash0 = self.expected_cash()
        sales0 = Sale.objects.count()
        pr = self.act(oid, "prepay", {"amount": "1", "method": "cash"})
        self.assertEqual(pr.status_code, 201, pr.data)
        self.assertEqual(self.expected_cash(), cash0 + Decimal("1"))
        self.assertEqual(Sale.objects.count(), sales0)
        self.assertEqual(pr.data["prepayment"]["amount"], "1.00")
        cf = CashFlow.objects.get(source_kind=CashFlow.SourceKind.WORK_ORDER_PREPAYMENT)
        self.assertEqual((cf.amount, cf.type, cf.shift_id), (Decimal("1.00"), "income", self.shift.id))

        # выдача до прихода нельзя
        early = self.act(oid, "deliver", {"payment": {"method": "cash", "received": "5000"}})
        self.assertEqual((early.status_code, early.data["code"]), (409, "order_waiting_supply"))

        # приход 15 по заказу поставщику: строки ready + событие
        from apps.main.purchase_orders import apply_receipt_to_order
        Product.objects.filter(pk=self.bag.pk).update(quantity=F("quantity") + 15)
        po.status = "sent"
        po.save()
        with self.captureOnCommitCallbacks(execute=True):
            apply_receipt_to_order(po, [(self.bag.id, 15)])
        o = self.api.get(f"{URL}{oid}/").data
        self.assertEqual((o["status"], o["items"][0]["status"], o["items"][0]["reserved_qty"]), ("ready", "ready", "40.000"))
        self.assertEqual(ChangeEvent.objects.filter(type="customer_order_ready", entity_id=oid).count(), 1)

        # выдача: продажа на остаток суммы, предоплата зачтена
        k = key()
        d = self.act(oid, "deliver", {"payment": {"method": "cash", "received": "4000"}}, k=k)
        self.assertEqual(d.status_code, 201, d.data)
        self.assertEqual(d.data["status"], "delivered")
        sale = Sale.objects.get(pk=d.data["sale"])
        self.assertEqual(sale.total, Decimal("4000.00"))
        self.assertEqual({p.method: p.amount for p in sale.payments.all()},
                         {"offset": Decimal("1.00"), "cash": Decimal("3999.00")})
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.quantity, Decimal("0"))
        self.assertEqual(self.expected_cash(), cash0 + Decimal("1") + Decimal("3999"))
        rep = self.act(oid, "deliver", {"payment": {"method": "cash", "received": "4000"}}, k=k)
        self.assertTrue(rep.data["replayed"])
        again = self.act(oid, "deliver", {"payment": {"method": "cash"}})
        self.assertTrue(again.data["replayed"])
        self.assertEqual(Sale.objects.count(), sales0 + 1)

    def test_reserve_blocks_other_orders_stock(self):
        a = self.create(qty="20", status="confirmed").data["id"]
        b = self.create(qty="20", status="confirmed").data["id"]
        self.act(a, "reserve")
        rb = self.act(b, "reserve")
        self.assertEqual(rb.data["items"][0]["reserved_qty"], "5.000")
        self.assertEqual(rb.data["status"], "waiting_supply")

    def test_reserve_needs_confirmed(self):
        oid = self.create().data["id"]
        r = self.act(oid, "reserve")
        self.assertEqual((r.status_code, r.data["code"]), (409, "order_not_confirmed"))

    def test_reserve_supplier_required(self):
        oid = self.create(qty="40", status="confirmed").data["id"]
        r = self.act(oid, "reserve", {"create_supplier_order": True})
        self.assertEqual((r.status_code, r.data["code"]), (400, "supplier_required"))
        self.assertEqual(self.api.get(f"{URL}{oid}/").data["items"][0]["reserved_qty"], "0.000")

    def test_prepay_without_shift_and_over_total(self):
        oid = self.create(qty="1").data["id"]
        over = self.act(oid, "prepay", {"amount": "500"})
        self.assertEqual((over.status_code, over.data["code"]), (409, "prepayment_exceeds_total"))
        self.shift.status = "closed"
        self.shift.save()
        r = self.act(oid, "prepay", {"amount": "10"})
        self.assertEqual((r.status_code, r.data["code"]), (409, "shift_not_open"))

    def test_prepay_at_create(self):
        r = self.create(qty="10", prepayment={"amount": "300", "method": "cash"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["prepayment"]["amount"], r.data["left_to_pay"]), ("300.00", "700.00"))

    def test_cancel_refunds_prepayment(self):
        oid = self.create(qty="10", status="confirmed").data["id"]
        self.act(oid, "reserve")
        cash0 = self.expected_cash()
        self.act(oid, "prepay", {"amount": "400", "method": "cash"})
        self.assertEqual(self.expected_cash(), cash0 + 400)
        k = key()
        c = self.act(oid, "cancel", k=k)
        self.assertEqual((c.status_code, c.data["status"]), (200, "canceled"))
        self.assertEqual(self.expected_cash(), cash0)
        self.assertEqual(c.data["prepayment"]["amount"], "0.00")
        self.assertEqual(CashFlow.objects.filter(type="expense", source_kind="work_order_prepayment").count(), 1)
        self.assertTrue(self.act(oid, "cancel", k=k).data["replayed"])
        self.assertTrue(self.act(oid, "cancel").data["replayed"])
        # резерв снят: другой заказ получает весь остаток
        o2 = self.create(qty="25", status="confirmed").data["id"]
        self.assertEqual(self.act(o2, "reserve").data["status"], "reserved")

    def test_cancel_delivered_refused(self):
        oid = self.create(qty="2", status="confirmed").data["id"]
        self.act(oid, "reserve")
        self.act(oid, "deliver", {"payment": {"method": "cash", "received": "200"}})
        c = self.act(oid, "cancel")
        self.assertEqual((c.status_code, c.data["code"]), (409, "order_already_delivered"))

    def test_cancel_refund_needs_right(self):
        from rest_framework.test import APIClient
        oid = self.create(qty="10", status="confirmed").data["id"]
        self.act(oid, "prepay", {"amount": "100"})
        self.cashier.can_view_cashier = False
        self.cashier.save()
        c = APIClient()
        c.force_authenticate(self.cashier)
        r = c.post(f"{URL}{oid}/cancel/", {}, format="json", HTTP_IDEMPOTENCY_KEY=key())
        self.assertEqual((r.status_code, r.data["code"]), (403, "permission_denied"))
        self.assertEqual(self.api.get(f"{URL}{oid}/").data["status"], "confirmed")


class ShowcaseTests(CustOrderBase):
    def test_needs_quote_becomes_draft(self):
        o = create_draft_from_showcase(
            company=self.company, customer_name="Покупатель", customer_phone="+996700000001",
            items=[{"product": self.bag, "qty": Decimal("5")}], showcase_order_id=uuid.uuid4())
        self.assertEqual((o.status, o.source, o.needs_quote), ("draft", "showcase", True))
        r = self.api.get(f"{URL}{o.id}/")
        self.assertEqual((r.data["source"], r.data["items"][0]["price"], r.data["customer_name"]),
                         ("showcase", "100.00", "Покупатель"))
