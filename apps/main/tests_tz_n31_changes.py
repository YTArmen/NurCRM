"""ТЗ Н-31: лента изменений, вебхук-типы, WebSocket, запросы «на кассу»."""
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashShift
from apps.main.change_views import decode_cursor, encode_cursor
from apps.main.models import ChangeEvent, Client, ClientDeal, DealPayment, Product
from apps.users.models import Company

User = get_user_model()
URL = "/api/main/changes/"


class FeedBase(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = User.objects.create_user(email=f"o-{uuid.uuid4().hex}@test.com", password="x",
                                             first_name="Айбек", role="owner")
        self.company = Company.objects.create(name="Co", owner=self.user)
        self.user.company = self.company
        self.user.save()
        self.api.force_authenticate(self.user)

    def run_commit(self, fn):
        with self.captureOnCommitCallbacks(execute=True):
            return fn()


class ChangeFeedTests(FeedBase):
    def test_start_cursor_then_events(self):
        r0 = self.api.get(URL)
        self.assertEqual(r0.status_code, 200)
        self.assertEqual(r0.data["events"], [])
        cursor = r0.data["cursor"]
        p = self.run_commit(lambda: Product.objects.create(name="T", company=self.company, quantity=1,
                                                           purchase_price=1, price=2))
        # правка цены → product_changed(price)
        p = Product.objects.get(pk=p.pk)
        p.price = Decimal("5.00")
        self.run_commit(lambda: p.save())
        r = self.api.get(URL, {"since": cursor})
        self.assertEqual(r.status_code, 200)
        kinds = [(e["type"], e["data"].get("what")) for e in r.data["events"]]
        self.assertIn(("product_changed", "created"), kinds)
        self.assertIn(("product_changed", "price"), kinds)
        self.assertFalse(r.data["has_more"])
        # фильтр по типам и пустой ответ по новому курсору
        r2 = self.api.get(URL, {"since": r.data["cursor"], "types": "debt_paid"})
        self.assertEqual(r2.data["events"], [])

    def test_quantity_change_gives_stock_changed(self):
        p = self.run_commit(lambda: Product.objects.create(name="T", company=self.company, quantity=1,
                                                           purchase_price=1, price=2))
        cursor = self.api.get(URL).data["cursor"]
        p = Product.objects.get(pk=p.pk)
        p.quantity = Decimal("7")
        self.run_commit(lambda: p.save())
        types = {e["type"] for e in self.api.get(URL, {"since": cursor}).data["events"]}
        self.assertEqual(types, {"product_changed", "stock_changed"})

    def test_debt_paid_and_created(self):
        cl = Client.objects.create(company=self.company, full_name="Клиент")
        cursor = self.api.get(URL).data["cursor"]

        def go():
            d = ClientDeal.objects.create(company=self.company, client=cl, title="d", kind="debt",
                                          amount=Decimal("10.00"), debt_months=1, first_due_date=date.today())
            DealPayment.objects.create(company=self.company, deal=d, kind="pay", amount=Decimal("5.00"),
                                       paid_date=date.today())
        self.run_commit(go)
        types = [e["type"] for e in self.api.get(URL, {"since": cursor}).data["events"]]
        self.assertIn("debt_created", types)
        self.assertIn("debt_paid", types)

    def test_shift_event_from_emit_event(self):
        cursor = self.api.get(URL).data["cursor"]
        cb = Cashbox.objects.create(name="К1", company=self.company)
        self.run_commit(lambda: CashShift.objects.create(company=self.company, cashbox=cb, cashier=self.user,
                                                         status=CashShift.Status.OPEN))
        ev = [e for e in self.api.get(URL, {"since": cursor}).data["events"] if e["type"] == "shift_opened"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["entity"], "shift")

    def test_has_more_and_limit(self):
        cursor = self.api.get(URL).data["cursor"]
        for i in range(3):
            ChangeEvent.objects.create(company=self.company, type="debt_paid", data={"i": i})
        r = self.api.get(URL, {"since": cursor, "limit": 2})
        self.assertEqual(len(r.data["events"]), 2)
        self.assertTrue(r.data["has_more"])
        r2 = self.api.get(URL, {"since": r.data["cursor"], "limit": 2})
        self.assertEqual(len(r2.data["events"]), 1)
        self.assertFalse(r2.data["has_more"])

    def test_expired_cursor_410_and_bad_400(self):
        old = encode_cursor(1, time.time() - 8 * 86400)
        r = self.api.get(URL, {"since": old})
        self.assertEqual(r.status_code, 410)
        self.assertEqual(r.data["code"], "cursor_expired")
        self.assertEqual(self.api.get(URL, {"since": "zzz"}).status_code, 400)

    def test_etag_304(self):
        cursor = self.api.get(URL).data["cursor"]
        r = self.api.get(URL, {"since": cursor})
        etag = r["ETag"]
        r2 = self.api.get(URL, {"since": cursor}, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r2.status_code, 304)
        ChangeEvent.objects.create(company=self.company, type="debt_paid")
        r3 = self.api.get(URL, {"since": cursor}, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r3.status_code, 200)

    def test_empty_response_single_query(self):
        cursor = self.api.get(URL).data["cursor"]
        self.api.get(URL, {"since": cursor})  # прогрев (сессии/кэш)
        with self.assertNumQueries(1):
            self.api.get(URL, {"since": cursor})

    def test_other_company_isolated(self):
        other = Company.objects.create(name="Other", owner=User.objects.create_user(
            email=f"x-{uuid.uuid4().hex}@t.com", password="x", role="owner"))
        ChangeEvent.objects.create(company=other, type="debt_paid")
        r = self.api.get(URL, {"since": encode_cursor(0)})
        self.assertEqual(r.data["events"], [])

    def test_purge_command(self):
        old = ChangeEvent.objects.create(company=self.company, type="debt_paid")
        ChangeEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=8))
        new = ChangeEvent.objects.create(company=self.company, type="debt_paid")
        call_command("purge_change_events", stdout=StringIO())
        self.assertEqual(list(ChangeEvent.objects.values_list("pk", flat=True)), [new.pk])

    def test_cursor_roundtrip(self):
        self.assertEqual(decode_cursor(encode_cursor(42, 1000))[0], 42)


class WebhookEventsListTests(FeedBase):
    def test_new_types_in_list(self):
        r = self.api.get("/api/users/webhooks/events/")
        self.assertEqual(r.status_code, 200, r.data)
        for t in ("debt_paid", "product_changed", "features_changed", "supplier_debt_changed", "sale.paid"):
            self.assertIn(t, r.data["events"])


from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.main.change_feed import record_change
from apps.main.consumers import CompanyChangesConsumer


class CompanyWebSocketTests(TransactionTestCase):
    def _setup(self):
        user = User.objects.create_user(email=f"w-{uuid.uuid4().hex}@test.com", password="x", role="owner")
        company = Company.objects.create(name="WS", owner=user)
        user.company = company
        user.save()
        return user, company

    async def test_token_first_message_and_push(self):
        user, company = await database_sync_to_async(self._setup)()
        token = str(RefreshToken.for_user(user).access_token)
        comm = WebsocketCommunicator(CompanyChangesConsumer.as_asgi(), "/ws/company/")
        ok, _ = await comm.connect()
        self.assertTrue(ok)
        await comm.send_json_to({"token": token})
        hello = await comm.receive_json_from()
        self.assertEqual(hello["type"], "connection_established")
        await database_sync_to_async(record_change)(company.id, "debt_paid", entity="deal", entity_id="1",
                                                    data={"amount": "5"})
        msg = await comm.receive_json_from()
        self.assertEqual(msg["type"], "change")
        self.assertEqual(msg["event"]["type"], "debt_paid")
        await comm.disconnect()

    async def test_bad_token_closes(self):
        comm = WebsocketCommunicator(CompanyChangesConsumer.as_asgi(), "/ws/company/")
        await comm.connect()
        await comm.send_json_to({"token": "bad"})
        out = await comm.receive_output()
        self.assertEqual(out["type"], "websocket.close")
        self.assertEqual(out["code"], 4401)


from apps.construction.models import CashFlow
from apps.main.models import PosRequest

RQ = "/api/main/pos/requests/"


class PosRequestTests(FeedBase):
    def setUp(self):
        super().setUp()
        self.cashier = User.objects.create_user(email=f"c-{uuid.uuid4().hex}@test.com", password="x",
                                                role="cashier", company=self.company)
        self.cashbox = Cashbox.objects.create(name="Касса 1", company=self.company)
        self.cap = APIClient()
        self.cap.force_authenticate(self.cashier)
        self.client_obj = Client.objects.create(company=self.company, full_name="Должник")
        self.deal = ClientDeal.objects.create(company=self.company, client=self.client_obj, title="d",
                                              kind="debt", amount=Decimal("1000.00"), debt_months=1,
                                              first_due_date=date.today())

    def open_shift(self):
        return CashShift.objects.create(company=self.company, cashbox=self.cashbox, cashier=self.cashier,
                                        status=CashShift.Status.OPEN)

    def create(self, **kw):
        body = {"kind": "debt_payment", "cashbox": str(self.cashbox.id), "amount": "500.00",
                "payload": {"client": str(self.client_obj.id)}}
        body.update(kw)
        return self.api.post(RQ, body, format="json")

    def test_create_only_owner(self):
        r = self.cap.post(RQ, {"kind": "cash_out", "cashbox": str(self.cashbox.id), "amount": "1"}, format="json")
        self.assertEqual(r.status_code, 403)
        r = self.create()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "pending")
        self.assertEqual(self.create(payload={}).status_code, 400)

    def test_accept_debt_payment_and_replay(self):
        rid = self.create().data["id"]
        # без смены — нельзя
        self.assertEqual(self.cap.post(f"{RQ}{rid}/accept/").status_code, 409)
        shift = self.open_shift()
        before = shift.expected_cash
        r = self.cap.post(f"{RQ}{rid}/accept/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(r.data["replayed"])
        self.assertEqual(r.data["status"], "accepted")
        self.assertTrue(r.data["result"]["executed"])
        shift = CashShift.objects.get(pk=shift.pk)
        self.assertEqual(shift.expected_cash - before, Decimal("500.00"))
        r2 = self.cap.post(f"{RQ}{rid}/accept/")
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.data["replayed"])
        self.assertEqual(CashFlow.objects.filter(source_kind="debt_repayment").count(), 1)
        self.assertEqual(CashShift.objects.get(pk=shift.pk).expected_cash - before, Decimal("500.00"))

    def test_accept_cash_out(self):
        shift = self.open_shift()
        before = shift.expected_cash
        rid = self.create(kind="cash_out", payload={}, amount="200.00").data["id"]
        r = self.cap.post(f"{RQ}{rid}/accept/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(before - CashShift.objects.get(pk=shift.pk).expected_cash, Decimal("200.00"))
        self.assertEqual(CashFlow.objects.filter(source_kind="shift_drawer_outflow").count(), 1)
        self.cap.post(f"{RQ}{rid}/accept/")
        self.assertEqual(CashFlow.objects.filter(source_kind="shift_drawer_outflow").count(), 1)

    def test_other_cashier_cannot_accept(self):
        rid = self.create(kind="cash_out", payload={}).data["id"]
        other = User.objects.create_user(email=f"d-{uuid.uuid4().hex}@test.com", password="x",
                                         role="cashier", company=self.company)
        CashShift.objects.create(company=self.company, cashbox=Cashbox.objects.create(name="К2", company=self.company),
                                 cashier=other, status=CashShift.Status.OPEN)
        a = APIClient()
        a.force_authenticate(other)
        self.assertEqual(a.post(f"{RQ}{rid}/accept/").status_code, 409)

    def test_decline_cancel_flow(self):
        rid = self.create(kind="salary_payout", payload={}).data["id"]
        self.open_shift()
        r = self.cap.post(f"{RQ}{rid}/decline/", {"reason": "нет денег"}, format="json")
        self.assertEqual(r.data["status"], "declined")
        self.assertEqual(self.api.post(f"{RQ}{rid}/cancel/").status_code, 409)
        rid2 = self.create(kind="supplier_payment", payload={}).data["id"]
        self.assertEqual(self.api.post(f"{RQ}{rid2}/cancel/").data["status"], "canceled")
        self.assertEqual(self.cap.post(f"{RQ}{rid2}/accept/").status_code, 409)

    def test_other_kind_accept_status_only(self):
        self.open_shift()
        rid = self.create(kind="rental_payment", payload={}).data["id"]
        r = self.cap.post(f"{RQ}{rid}/accept/")
        self.assertEqual(r.data["status"], "accepted")
        self.assertFalse(r.data["result"]["executed"])

    def test_expired(self):
        rid = self.create(kind="cash_out", payload={}).data["id"]
        PosRequest.objects.filter(pk=rid).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.open_shift()
        r = self.cap.post(f"{RQ}{rid}/accept/")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(PosRequest.objects.get(pk=rid).status, "expired")

    def test_list_and_feed_event(self):
        cursor = self.api.get(URL).data["cursor"]
        with self.captureOnCommitCallbacks(execute=True):
            self.create(kind="cash_out", payload={})
        self.assertEqual(len(self.api.get(RQ).data["results"]), 1)
        self.assertEqual(self.cap.get(RQ).data["results"], [])  # нет смены — не видит
        types = [e["type"] for e in self.api.get(URL, {"since": cursor}).data["events"]]
        self.assertIn("pos_request_changed", types)
