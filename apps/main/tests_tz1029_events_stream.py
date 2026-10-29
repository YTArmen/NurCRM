"""ТЗ 29.10 Б-102: поток событий ws/main/events/ и адрес events/stream/."""
import uuid

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.main.change_feed import record_change
from apps.main.consumers import MainEventsConsumer
from apps.main.events_stream import allowed_for, to_stream_event
from apps.main.models import ChangeEvent, Product
from apps.users.models import Company, CompanyAppSettings

User = get_user_model()


class MapperTests(TestCase):
    def test_mapping(self):
        base = {"id": 5, "at": "2026-10-29T15:15:03+06:00", "branch": None, "actor": None, "entity": "", "entity_id": ""}
        m = to_stream_event({**base, "type": "sale_paid", "data": {"sale": "S1", "shift": "SH", "cashbox": "CB"}})
        self.assertEqual((m["type"], m["sale_id"], m["cashbox"], m["shift"]), ("sale_created", "S1", "CB", "SH"))
        self.assertEqual(m["at"], "2026-10-29T09:15:03Z")
        m = to_stream_event({**base, "type": "shift_closed", "data": {"shift": "SH"}})
        self.assertEqual((m["type"], m["state"]), ("shift_changed", "closed"))
        m = to_stream_event({**base, "type": "product_changed", "entity_id": "P", "data": {"what": "price", "product": "P"}})
        self.assertEqual((m["type"], m["ids"]), ("price_changed", ["P"]))
        self.assertIsNone(to_stream_event({**base, "type": "stock_low", "data": {}}))
        m = to_stream_event({**base, "type": "stock_changed", "data": {"products": [str(i) for i in range(501)]}})
        self.assertIsNone(m["ids"])

    def test_permissions(self):
        s = {"type": "settings_changed", "app": "owner", "keys": ["x"], "branch": None, "ids": []}
        self.assertFalse(allowed_for(s, role="cashier", user_id="u", branch_id=None))
        self.assertTrue(allowed_for(s, role="owner", user_id="u", branch_id=None))
        st = {"type": "staff_changed", "ids": ["u2"], "branch": None}
        self.assertFalse(allowed_for(st, role="cashier", user_id="u", branch_id=None))
        self.assertTrue(allowed_for({**st, "ids": ["u"]}, role="cashier", user_id="u", branch_id=None))
        b = {"type": "stock_changed", "ids": [], "branch": "B2"}
        self.assertFalse(allowed_for(b, role="cashier", user_id="u", branch_id="B1"))


class HttpStreamTests(TestCase):
    def test_use_websocket(self):
        u = User.objects.create_user(email=f"h-{uuid.uuid4().hex}@t.com", password="x", role="owner")
        c = Company.objects.create(name="C", owner=u)
        u.company = c
        u.save()
        api = APIClient()
        api.force_authenticate(u)
        r = api.get("/api/main/events/stream/", HTTP_ACCEPT="text/event-stream")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Type"].startswith("text/event-stream"))
        body = r.content.decode()
        self.assertIn("event: use_websocket", body)
        self.assertIn("retry: 3000", body)
        self.assertIn("/ws/main/events/", body)
        self.assertEqual(APIClient().get("/api/main/events/stream/").status_code in (401, 403), True)

    def test_capabilities_feature(self):
        from core.capabilities import FEATURES
        self.assertIn("events_stream", FEATURES)


class WsTests(TransactionTestCase):
    def _setup(self, role="owner"):
        owner = User.objects.create_user(email=f"o-{uuid.uuid4().hex}@t.com", password="x", role="owner")
        company = Company.objects.create(name="WS", owner=owner)
        owner.company = company
        owner.save()
        user = owner
        if role != "owner":
            user = User.objects.create_user(email=f"c-{uuid.uuid4().hex}@t.com", password="x", role=role,
                                            company=company)
        return owner, user, company

    async def _connect(self, user, path="/ws/main/events/"):
        token = str(RefreshToken.for_user(user).access_token)
        comm = WebsocketCommunicator(MainEventsConsumer.as_asgi(), path)
        ok, _ = await comm.connect()
        self.assertTrue(ok)
        await comm.send_json_to({"token": token})
        hello = await comm.receive_json_from()
        self.assertEqual(hello["type"], "connection_established")
        return comm

    async def test_push_and_cashier_filter(self):
        owner, cashier, company = await database_sync_to_async(self._setup)("cashier")
        comm = await self._connect(cashier)
        # owner-настройки кассиру не приходят, обычные — приходят
        await database_sync_to_async(record_change)(company.id, "app_settings_changed", entity="app_settings",
                                                    entity_id="owner", data={"app": "owner", "keys": ["pin"]})
        await database_sync_to_async(record_change)(company.id, "app_settings_changed", entity="app_settings",
                                                    entity_id="kassa", data={"app": "kassa", "keys": ["shop_name"]})
        msg = await comm.receive_json_from()
        self.assertEqual((msg["type"], msg["app"], msg["keys"]), ("settings_changed", "kassa", ["shop_name"]))
        self.assertTrue(await comm.receive_nothing())
        await comm.disconnect()

    async def test_replay_and_resync(self):
        owner, user, company = await database_sync_to_async(self._setup)()

        def mk():
            ids = []
            for i in range(3):
                ev = ChangeEvent.objects.create(company=company, type="client_changed", entity="client",
                                                entity_id=str(i), data={})
                ids.append(ev.id)
            return ids
        ids = await database_sync_to_async(mk)()
        comm = await self._connect(user, f"/ws/main/events/?since={ids[0]}")
        got = [await comm.receive_json_from(), await comm.receive_json_from()]
        self.assertEqual([m["id"] for m in got], ids[1:])
        self.assertEqual(got[0]["type"], "client_changed")
        await comm.disconnect()
        comm = await self._connect(user, "/ws/main/events/?since=abc")
        self.assertEqual((await comm.receive_json_from())["type"], "resync")
        await comm.disconnect()

    async def test_expired_token_closes_on_heartbeat(self):
        owner, user, company = await database_sync_to_async(self._setup)()
        MainEventsConsumer.PING_EVERY = 0.05
        try:
            comm = await self._connect(user)
            out = await comm.receive_json_from()
            self.assertEqual(out["type"], "ping")
            await comm.disconnect()
            # токен уже истёк: следующий heartbeat закрывает соединение кодом 4401
            import time
            from unittest import mock
            with mock.patch.object(MainEventsConsumer, "_token_exp", staticmethod(lambda t: time.time() - 1)):
                comm = await self._connect(user)
                out = await comm.receive_output()
                self.assertEqual((out["type"], out["code"]), ("websocket.close", 4401))
        finally:
            MainEventsConsumer.PING_EVERY = 20

    async def test_bad_token(self):
        comm = WebsocketCommunicator(MainEventsConsumer.as_asgi(), "/ws/main/events/")
        await comm.connect()
        await comm.send_json_to({"token": "bad"})
        out = await comm.receive_output()
        self.assertEqual((out["type"], out["code"]), ("websocket.close", 4401))



class SettingsKeysTests(TestCase):
    def _mk(self):
        owner = User.objects.create_user(email=f"s-{uuid.uuid4().hex}@t.com", password="x", role="owner")
        company = Company.objects.create(name="S", owner=owner)
        owner.company = company
        owner.save()
        return owner, owner, company

    def test_settings_keys_signal(self):
        owner, user, company = self._mk()
        obj = CompanyAppSettings.objects.create(company=company, app="kassa", settings={"a": 1})
        obj = CompanyAppSettings.objects.get(pk=obj.pk)
        obj.settings = {"a": 1, "shop_name": "X"}
        with self.captureOnCommitCallbacks(execute=True):
            obj.save()
        ev = ChangeEvent.objects.filter(type="app_settings_changed").order_by("-id").first()
        self.assertIn("shop_name", ev.data["keys"])
