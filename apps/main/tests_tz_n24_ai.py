"""ТЗ Н-24: ключи с областями, поиск товара, клиент по телефону, черновик заказа, журнал звонков."""
import time
import uuid
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.integrations.models import ApiKey, ApiKeyCallLog, hash_api_key
from apps.main.ai_agent_api import skeleton
from apps.main.models import Client, Product, ShowcaseOrder
from apps.main.tests_tz_n14_debtors import TzN14DebtorsTests
from apps.main.telegram_bot.models import VoiceCall

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class N24Base(TestCase):
    _setup_n14 = TzN14DebtorsTests.setUp
    _debt_sale = TzN14DebtorsTests._debt_sale

    def setUp(self):
        self._setup_n14()
        cache.clear()
        self.cola = Product.objects.create(company=self.company, name="Кока-Кола 1л", price=Decimal("90"), quantity=Decimal("12"))
        Product.objects.create(company=self.company, name="Хлеб белый", price=Decimal("30"), quantity=Decimal("5"))
        Product.objects.create(company=self.company, name="Coca-Cola 0.5L", price=Decimal("60"), quantity=Decimal("0"))

    def key_client(self, scopes, limit=120):
        raw = ApiKey.generate()
        key = ApiKey.objects.create(
            company=self.company, name="t", scopes=scopes, prefix=raw[:16],
            key_hash=hash_api_key(raw), rate_limit_per_minute=limit,
        )
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Api-Key {raw}")
        return c, key

    def draft_body(self, **kw):
        body = {
            "customer": {"name": "Азат", "phone": "+996555111222"},
            "items": [{"product": str(self.cola.id), "qty": "2"}],
            "delivery": {"type": "pickup"},
        }
        body.update(kw)
        return body


class ScopeTests(N24Base):
    def test_catalog_only_key_cannot_create_order(self):
        c, _ = self.key_client(["catalog:read"])
        r = c.post("/api/main/orders/draft/", self.draft_body(), format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(c.get("/api/main/products/search/?q=cola").status_code, 200)

    def test_scoped_key_cannot_read_other_endpoints(self):
        c, _ = self.key_client(["catalog:read"])
        self.assertEqual(c.get("/api/main/clients/debtors/").status_code, 403)
        self.assertEqual(c.get("/api/main/clients/by-phone/?phone=0700000010").status_code, 403)

    def test_legacy_read_key_cannot_post_draft(self):
        c, _ = self.key_client(["read"])
        self.assertEqual(c.post("/api/main/orders/draft/", self.draft_body(), format="json").status_code, 403)

    def test_rate_limit_and_log(self):
        c, key = self.key_client(["catalog:read"], limit=2)
        codes = [c.get("/api/main/products/search/?q=cola").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
        self.assertEqual(ApiKeyCallLog.objects.filter(key=key, allowed=True).count(), 2)
        self.assertEqual(ApiKeyCallLog.objects.filter(key=key, allowed=False, reason="rate_limit").count(), 1)

    def test_stock_hidden_without_stock_scope(self):
        c, _ = self.key_client(["catalog:read"])
        row = c.get("/api/main/products/search/?q=хлеб").json()["results"][0]
        self.assertNotIn("stock", row)
        c2, _ = self.key_client(["catalog:read", "stock:read"])
        self.assertEqual(c2.get("/api/main/products/search/?q=хлеб").json()["results"][0]["stock"], "5.000")

    def test_create_key_with_new_scope_and_calls_log(self):
        r = self.api.post("/api/users/api-keys/", {"name": "bot", "scopes": ["order:draft"], "rate_limit_per_minute": 10}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["rate_limit_per_minute"], 10)
        self.assertEqual(self.api.get(f"/api/users/api-keys/{r.json()['id']}/calls/").status_code, 200)


class SearchTests(N24Base):
    def test_skeleton(self):
        self.assertEqual(skeleton("кока-кола"), skeleton("Coca Cola"))

    def test_queries(self):
        for q in ("кока кола", "кока-кола 1л", "cola", "кока колла", "кока кла", "coca cola"):
            res = self.api.get("/api/main/products/search/", {"q": q}).json()["results"]
            names = [r["name"] for r in res]
            self.assertTrue(any("Кок" in n or "Coca" in n for n in names), (q, names))
        top = self.api.get("/api/main/products/search/", {"q": "кока-кола 1л"}).json()["results"][0]
        self.assertEqual(top["name"], "Кока-Кола 1л")
        self.assertEqual(Decimal(top["price"]), Decimal("90"))

    def test_speed(self):
        Product.objects.bulk_create([
            Product(company=self.company, name=f"Товар номер {i} {w}", price=Decimal("10"), quantity=Decimal("1"))
            for i, w in enumerate(["молоко", "сыр", "чай", "кофе"] * 750)
        ])
        t = time.perf_counter()
        res = self.api.get("/api/main/products/search/", {"q": "кока клла"}).json()["results"]
        elapsed = time.perf_counter() - t
        self.assertTrue(res)
        self.assertLess(elapsed, 1.0, elapsed)  # на проде < 300 мс; в тестах с запасом


class ByPhoneTests(N24Base):
    def test_debt_matches_debtors(self):
        self._debt_sale(self.debtor, total="150.00", days=5)
        self.debtor.phone = "0700000010"
        self.debtor.save()
        key, _ = self.key_client(["client:read_self"])
        r = key.get("/api/main/clients/by-phone/?phone=+996 700 000 010")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        debtors = self.api.get("/api/main/clients/debtors/").json()
        self.assertEqual(d["debt_total"], debtors[0]["debt_total"])
        self.assertEqual(d["debt_total"], "150.00")
        self.assertEqual(d["client"]["full_name"], "Должник")
        self.assertTrue(d["since"] and d["next_due_date"])

    def test_not_found_has_zero_debt(self):
        r = self.api.get("/api/main/clients/by-phone/?phone=0777000999")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["client"])
        self.assertEqual(r.json()["debt_total"], "0.00")


class DraftOrderTests(N24Base):
    def test_draft_idempotent_and_status(self):
        c, _ = self.key_client(["order:draft"])
        k = {"HTTP_IDEMPOTENCY_KEY": "call-1"}
        r1 = c.post("/api/main/orders/draft/", self.draft_body(source="voice_ai"), format="json", **k)
        self.assertEqual(r1.status_code, 201, r1.content)
        r2 = c.post("/api/main/orders/draft/", self.draft_body(source="voice_ai"), format="json", **k)
        self.assertEqual(r1.json()["number"], r2.json()["number"])
        self.assertEqual(ShowcaseOrder.objects.count(), 1)
        o = ShowcaseOrder.objects.get()
        self.assertEqual((o.source, o.stock_reserved), ("voice_ai", True))
        n = r1.json()["number"]
        self.assertEqual(c.get(f"/api/main/orders/draft/status/?number={n}").json()["results"][0]["status"], "new")
        self.assertEqual(len(c.get("/api/main/orders/draft/status/?phone=0555111222").json()["results"]), 1)

    def test_bad_source(self):
        c, _ = self.key_client(["order:draft"])
        r = c.post("/api/main/orders/draft/", self.draft_body(source="x"), format="json")
        self.assertEqual(r.status_code, 400)


class VoiceCallTests(N24Base):
    def test_create_list_idempotent(self):
        c, _ = self.key_client(["order:draft"])
        body = {"phone": "0700000010", "duration_seconds": 42, "language": "ky", "outcome": "callback",
                "transcript": "Салам", "recording_url": "https://rec.example/1.mp3", "cost": "0.12",
                "callback_needed": True}
        r = c.post("/api/main/voice/calls/", body, format="json", HTTP_IDEMPOTENCY_KEY="v1")
        self.assertEqual(r.status_code, 201, r.content)
        again = c.post("/api/main/voice/calls/", body, format="json", HTTP_IDEMPOTENCY_KEY="v1")
        self.assertEqual(again.json()["id"], r.json()["id"])
        self.assertEqual(VoiceCall.objects.count(), 1)
        self.assertEqual(len(c.get("/api/main/voice/calls/?callback_needed=1").json()["results"]), 1)
        self.assertEqual(VoiceCall.objects.get().client_id, self.debtor.id)
