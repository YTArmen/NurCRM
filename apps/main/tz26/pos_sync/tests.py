# Тесты куска pos_sync: python manage.py test apps.main.tz26.pos_sync --settings=core.settings_test_sqlite
import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from apps.construction.models import CashShift
from apps.main.models import Product, Sale, SalePayment, SaleReturn
from apps.main.tz26.pos_sync.models import Device, DevicePushToken
from apps.possync.tests import PossyncBase
from apps.users.models import User

KEY8 = "abcd1234"


class SyncBase(PossyncBase):
    def setUp(self):
        super().setUp()
        self.shift = self.open_shift()
        self.prod = Product.objects.create(company=self.company, name="Т", price=Decimal("10.00"), quantity=Decimal("100"))

    def co(self, key=None, headers=None, **extra):
        body = {"items": [{"product": str(self.prod.id), "qty": "1", "price": "10.00"}],
                "payment": {"method": "cash", "received": "100.00"}}
        body.update(extra)
        return self.api.post("/api/main/pos/checkout/", body, format="json",
                             HTTP_IDEMPOTENCY_KEY=key or f"k-{uuid.uuid4()}", **(headers or {}))


class OriginTests(SyncBase):
    """Б-67."""

    def test_explicit_source_and_device(self):
        r = self.co(source="mobile", device_key=KEY8, device_name="Телефон Али")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["source"], r.data["device_key"]), ("mobile", KEY8))
        sale = Sale.objects.get(pk=r.data["id"])
        self.assertEqual((sale.source, sale.device_key, sale.device_name), ("mobile", KEY8, "Телефон Али"))
        d = self.api.get(f"/api/main/pos/sales/{sale.pk}/").data
        self.assertEqual(d["source"], "mobile")
        self.assertEqual(d["created_by"]["id"], str(self.owner.pk))
        lst = self.api.get("/api/main/pos/sales/").data["results"][0]
        self.assertEqual(lst["device_name"], "Телефон Али")
        self.assertEqual(lst["created_by"]["id"], str(self.owner.pk))

    def test_source_from_header(self):
        r = self.co(headers={"HTTP_X_NURMARKET_CLIENT": "kassa/1.17.61"})
        self.assertEqual(r.data["source"], "pos")
        r = self.co(headers={"HTTP_X_NURMARKET_CLIENT": "mobile/1.18.5"})
        self.assertEqual(r.data["source"], "mobile")
        r = self.co()
        self.assertEqual(r.data["source"], "web")

    def test_invalid_values_400(self):
        # горячая правка 29.10: 400 на неверные значения — только для новых клиентов (флаг компании)
        from apps.users.models import CompanyAddon
        CompanyAddon.objects.get_or_create(company=self.company, code="api_strict_2026_10", defaults={"active": True})
        self.user.company.__dict__.pop("_nm_flags", None) if hasattr(self, "user") else None
        for extra in ({"source": "fax"}, {"device_key": "short"}, {"device_key": "bad key!!"}, {"device_name": "x" * 65}):
            r = self.co(**extra)
            self.assertEqual(r.status_code, 400, (extra, getattr(r, "data", None)))
        self.assertEqual(Sale.objects.count(), 0)

    def test_replay_without_source_is_not_409(self):
        key = f"k-{uuid.uuid4()}"
        r1 = self.co(key=key, source="mobile", device_key=KEY8)
        self.assertEqual(r1.status_code, 201, r1.data)
        r2 = self.co(key=key)  # без source и device_key
        self.assertEqual(r2.status_code, 200, getattr(r2, "data", None))
        self.assertTrue(r2.data["replayed"])
        r3 = self.co(key=key, source="pos", device_key="zzzzzzzz9")
        self.assertEqual(r3.status_code, 200, getattr(r3, "data", None))
        self.assertEqual(Sale.objects.count(), 1)

    def test_idempotency_body_hash_ignores_origin(self):
        from types import SimpleNamespace
        from apps.integrations.idempotency import _body_hash

        def req(data, path="/api/main/pos/checkout/"):
            return SimpleNamespace(data=data, path=path)

        base = {"items": [1], "payment": {"method": "cash"}}
        self.assertEqual(_body_hash(req(base)), _body_hash(req({**base, "source": "mobile", "device_key": KEY8,
                                                               "device_name": "x"})))
        # на других адресах поле source по-прежнему входит в сравнение
        self.assertNotEqual(_body_hash(req(base, "/api/main/tasks/")), _body_hash(req({**base, "source": "m"}, "/api/main/tasks/")))

    def test_batch_passes_origin(self):
        body = {"items": [{"idempotency_key": f"b-{uuid.uuid4()}", "body": {
            "items": [{"product": str(self.prod.id), "qty": "1", "price": "10.00"}],
            "payment": {"method": "cash", "received": "100.00"},
            "source": "owner", "device_key": KEY8}}]}
        r = self.api.post("/api/main/pos/checkout/batch/", body, format="json",
                          HTTP_X_NURMARKET_CLIENT="kassa/1.17.61")
        self.assertEqual(r.data["results"][0]["status"], 201, r.data)
        self.assertEqual(Sale.objects.get().source, "owner")
        body["items"][0]["idempotency_key"] = f"b-{uuid.uuid4()}"
        body["items"][0]["body"].pop("source")
        r = self.api.post("/api/main/pos/checkout/batch/", body, format="json",
                          HTTP_X_NURMARKET_CLIENT="kassa/1.17.61")
        self.assertEqual(r.data["results"][0]["status"], 201, r.data)
        self.assertEqual(set(Sale.objects.values_list("source", flat=True)), {"owner", "pos"})

    def test_filters(self):
        self.co(source="mobile", device_key=KEY8)
        self.co(source="pos", device_key="kassa-001")
        other = User.objects.create_user(email="c@t.kg", password="x", role="cashier", company=self.company)
        s = Sale.objects.first()
        Sale.objects.filter(pk=s.pk).update(user=other)
        self.assertEqual(len(self.api.get("/api/main/pos/sales/?source=mobile").data["results"]), 1)
        self.assertEqual(len(self.api.get("/api/main/pos/sales/?source=mobile,pos").data["results"]), 2)
        self.assertEqual(len(self.api.get(f"/api/main/pos/sales/?device_key={KEY8}").data["results"]), 1)
        self.assertEqual(len(self.api.get(f"/api/main/pos/sales/?created_by={other.pk}").data["results"]), 1)
        self.assertEqual(self.api.get("/api/main/pos/sales/?source=zzz").status_code, 400)
        self.assertEqual(self.api.get("/api/main/pos/sales/?created_by=not-uuid").status_code, 400)

    def test_return_origin(self):
        r = self.co()
        sale_id = r.data["id"]
        rr = self.api.post(f"/api/main/pos/sales/{sale_id}/return/",
                           {"idempotency_key": "ret-1", "source": "mobile", "device_key": KEY8},
                           format="json")
        self.assertEqual(rr.status_code, 200, getattr(rr, "data", None))
        self.assertEqual(SaleReturn.objects.count(), 1)
        lst = self.api.get("/api/main/pos/returns/").data
        row = lst[0] if isinstance(lst, list) else lst["results"][0]
        self.assertEqual((row["source"], row["device_key"]), ("mobile", KEY8))
        self.assertEqual(row["created_by"]["id"], str(self.owner.pk))


class SalesSyncTests(SyncBase):
    """Б-68."""

    def test_created_after_shift_cashbox_page_size(self):
        ids = [self.co().data["id"] for _ in range(3)]
        t0 = timezone.now() - timedelta(hours=1)
        for i, sid in enumerate(ids):
            Sale.objects.filter(pk=sid).update(created_at=t0 + timedelta(minutes=10 * i))
        after = (t0 + timedelta(minutes=5)).isoformat()
        r = self.api.get("/api/main/pos/sales/", {"created_after": after, "ordering": "created_at", "page_size": 100})
        self.assertEqual([x["id"] for x in r.data["results"]], ids[1:])
        r = self.api.get("/api/main/pos/sales/", {"shift": str(self.shift.pk), "page_size": 2})
        self.assertEqual(len(r.data["results"]), 2)
        self.assertEqual(self.api.get("/api/main/pos/sales/", {"shift": str(uuid.uuid4())}).data["results"], [])
        r = self.api.get("/api/main/pos/sales/", {"cashbox": str(self.cashbox.pk)})
        self.assertEqual(len(r.data["results"]), 3)

    def test_secondary_sort_by_id(self):
        ids = [self.co().data["id"] for _ in range(3)]
        same = timezone.now() - timedelta(hours=2)
        Sale.objects.filter(pk__in=ids).update(created_at=same)
        asc = [x["id"] for x in self.api.get("/api/main/pos/sales/?ordering=created_at").data["results"]]
        desc = [x["id"] for x in self.api.get("/api/main/pos/sales/?ordering=-created_at").data["results"]]
        self.assertEqual(asc, sorted(ids))
        self.assertEqual(desc, sorted(ids, reverse=True))

    def test_updated_after_catches_late_offline_and_return(self):
        a = self.co().data["id"]
        old = timezone.now() - timedelta(hours=3)
        Sale.objects.filter(pk=a).update(created_at=old, paid_at=old, received_at=old)
        SalePayment.objects.filter(sale_id=a).update(created_at=old)
        cursor = (timezone.now() - timedelta(minutes=1)).isoformat()
        self.assertEqual(self.api.get("/api/main/pos/sales/", {"updated_after": cursor}).data["results"], [])
        # досланная офлайн-продажа: время продажи в прошлом, принята только что
        Sale.objects.filter(pk=a).update(received_at=timezone.now())
        r = self.api.get("/api/main/pos/sales/", {"updated_after": cursor}).data["results"]
        self.assertEqual([x["id"] for x in r], [a])
        # а created_after её не покажет (created_at старый)
        self.assertEqual(self.api.get("/api/main/pos/sales/", {"created_after": cursor}).data["results"], [])
        self.assertEqual(self.api.get("/api/main/pos/sales/?updated_after=xx").status_code, 400)

    def test_sold_at_for_offline_sale(self):
        sold = (timezone.now() - timedelta(hours=1)).replace(microsecond=0)
        r = self.co(offline=True, sold_at=sold.isoformat(), offline_created_at=sold.isoformat())
        self.assertEqual(r.status_code, 201, getattr(r, "data", None))
        row = self.api.get("/api/main/pos/sales/").data["results"][0]
        self.assertEqual(row["sold_at"], row["created_at"])
        self.assertLess(abs((parse_datetime(row["sold_at"]) - sold).total_seconds()), 1)

    def test_etag_304(self):
        self.co()
        r1 = self.api.get("/api/main/pos/sales/?ordering=created_at")
        etag = r1["ETag"]
        self.assertTrue(etag)
        r2 = self.api.get("/api/main/pos/sales/?ordering=created_at", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r2.status_code, 304)
        self.assertEqual(r2["ETag"], etag)
        self.co()  # новая продажа - ETag меняется
        r3 = self.api.get("/api/main/pos/sales/?ordering=created_at", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r3.status_code, 200)
        self.assertNotEqual(r3["ETag"], etag)
        # разные параметры - разные ETag
        self.assertNotEqual(self.api.get("/api/main/pos/sales/?page_size=1")["ETag"], r3["ETag"])

    def test_etag_changes_on_return(self):
        sid = self.co().data["id"]
        etag = self.api.get("/api/main/pos/sales/")["ETag"]
        self.api.post(f"/api/main/pos/sales/{sid}/return/", {"idempotency_key": "r1"}, format="json")
        self.assertEqual(self.api.get("/api/main/pos/sales/", HTTP_IF_NONE_MATCH=etag).status_code, 200)


class DeviceTests(SyncBase):
    """Б-70, Б-77 п.3."""
    HB = "/api/users/company/devices/heartbeat/"
    LIST = "/api/users/company/devices/"

    def hb(self, api=None, **kw):
        body = {"device_key": "kassa-main-1", "kind": "kassa", "name": "Касса", "app_version": "1.17.61",
                "cashbox": str(self.cashbox.pk), "can_print": True}
        body.update(kw)
        return (api or self.api).post(self.HB, body, format="json")

    def test_heartbeat_list_online_offline(self):
        r = self.hb()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("device_id", r.data)
        self.assertIn("server_time", r.data)
        rows = self.api.get(self.LIST).data
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["online"])
        self.assertEqual(rows[0]["cashbox_name"], "Касса 1")
        self.assertEqual(rows[0]["user"]["id"], str(self.owner.pk))
        Device.objects.update(last_seen_at=timezone.now() - timedelta(seconds=121))
        self.assertFalse(self.api.get(self.LIST).data[0]["online"])
        Device.objects.update(last_seen_at=timezone.now() - timedelta(seconds=100))
        self.assertTrue(self.api.get(self.LIST).data[0]["online"])
        self.hb(app_version="1.17.62")  # повтор - обновление, не дубль
        self.assertEqual(Device.objects.count(), 1)
        self.assertEqual(Device.objects.get().app_version, "1.17.62")

    def test_heartbeat_validation(self):
        self.assertEqual(self.hb(device_key="x").status_code, 400)
        self.assertEqual(self.hb(kind="tv").status_code, 400)
        self.assertEqual(self.hb(cashbox=str(uuid.uuid4())).status_code, 400)

    def test_cashier_sees_only_own_cashbox_printers(self):
        cashier = User.objects.create_user(email="k@t.kg", password="x", role="cashier", company=self.company)
        CashShift.objects.create(company=self.company, cashbox=self.cashbox, cashier=cashier,
                                 status=CashShift.Status.OPEN, opening_cash=Decimal("0"))
        self.hb()
        self.hb(device_key="kassa-noprint", can_print=False)
        from apps.construction.models import Cashbox
        other_cb = Cashbox.objects.create(name="Другая", company=self.company)
        self.hb(device_key="kassa-other-9", cashbox=str(other_cb.pk))
        api = APIClient()
        api.force_authenticate(cashier)
        keys = [d["device_key"] for d in api.get(self.LIST).data]
        self.assertEqual(keys, ["kassa-main-1"])
        self.assertEqual(len(self.api.get(self.LIST).data), 3)
        # чужая компания не видна
        stranger = User.objects.create_user(email="s@t.kg", password="x", role="owner")
        from apps.users.models import Company
        co2 = Company.objects.create(name="Co2", owner=stranger, is_active=True)
        stranger.company = co2
        stranger.save()
        api2 = APIClient()
        api2.force_authenticate(stranger)
        self.assertEqual(api2.get(self.LIST).data, [])

    def test_delete_owner_only(self):
        self.hb()
        cashier = User.objects.create_user(email="k@t.kg", password="x", role="cashier", company=self.company)
        api = APIClient()
        api.force_authenticate(cashier)
        self.assertEqual(api.delete(self.LIST + "kassa-main-1/").status_code, 403)
        self.assertEqual(self.api.delete(self.LIST + "kassa-main-1/").status_code, 204)
        self.assertEqual(Device.objects.count(), 0)
        self.assertEqual(self.api.delete(self.LIST + "kassa-main-1/").status_code, 404)

    def test_paths_do_not_shadow_company_detail(self):
        self.assertNotEqual(self.api.get("/api/users/company/").status_code, 404)

    def test_push_token(self):
        body = {"device_key": KEY8, "platform": "android", "token": "fcm-token-AAA"}
        r = self.api.post("/api/users/devices/push-token/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.api.post("/api/users/devices/push-token/", {**body, "token": "fcm-token-BBB"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(DevicePushToken.objects.get().token, "fcm-token-BBB")
        self.assertEqual(self.api.post("/api/users/devices/push-token/", {**body, "platform": "win"},
                                       format="json").status_code, 400)
        # тот же токен у другого сотрудника - привязка переезжает
        cashier = User.objects.create_user(email="k@t.kg", password="x", role="cashier", company=self.company)
        api = APIClient()
        api.force_authenticate(cashier)
        api.post("/api/users/devices/push-token/", {**body, "token": "fcm-token-BBB"}, format="json")
        self.assertEqual(DevicePushToken.objects.count(), 1)
        self.assertEqual(DevicePushToken.objects.get().user_id, cashier.pk)
