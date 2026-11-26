"""Единое ТЗ 26.10.2026: Б-01 (capabilities, заголовок версии API, 404, журнал версий клиентов) и Б-11 (формат ошибок)."""
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.main.models import Client
from apps.users.models import ClientVersionSeen, Company, SubscriptionPlan

User = get_user_model()


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class B01B11ApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="NBS", owner=self.owner, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.api.force_authenticate(user=self.owner)

    def test_capabilities(self):
        r = self.api.get("/api/main/capabilities/", HTTP_X_NURMARKET_CLIENT="kassa/1.17.61")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["api"], "2026-10")
        self.assertIn("idempotency_lookup", r.data["features"])
        self.assertIn("api_strict_2026_10", r.data["company_flags"])
        self.assertFalse(r.data["company_flags"]["api_strict_2026_10"])
        self.assertTrue(r.data["client_is_new"])
        self.assertEqual(self.api.get("/api/main/capabilities/", HTTP_X_NURMARKET_CLIENT="kassa/1.17.60").data["client_is_new"], False)
        self.assertEqual(APIClient().get("/api/main/capabilities/").status_code, 401)

    def test_api_header_in_every_response(self):
        r = self.api.get("/api/main/products/catalog-version/")
        self.assertEqual(r["X-NurCRM-API"], "2026-10")
        self.assertEqual(self.api.get("/api/main/no-such-path/")["X-NurCRM-API"], "2026-10")

    def test_three_kinds_of_404(self):
        r = self.api.get("/api/main/no-such-path/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "route_not_found")
        r = self.api.get(f"/api/main/idempotency/{uuid.uuid4()}/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.data["code"], "idempotency_key_not_found")
        self.assertFalse(r.data["found"])

    def test_client_version_journal_and_report(self):
        self.api.get("/api/main/products/catalog-version/", HTTP_X_NURMARKET_CLIENT="kassa/1.17.61")
        self.assertTrue(ClientVersionSeen.objects.filter(company=self.company, client="kassa/1.17.61").exists())
        # Отчёт — только поддержка и суперпользователь.
        self.assertEqual(self.api.get("/api/main/client-versions/").status_code, 403)
        root = User.objects.create_user(email="root@t.com", password="p", is_superuser=True, is_staff=True)
        api = APIClient()
        api.force_authenticate(user=root)
        r = api.get("/api/main/client-versions/")
        self.assertEqual(r.status_code, 200, r.data)
        clients = [c["client"] for comp in r.data["companies"] for c in comp["clients"]]
        self.assertIn("kassa/1.17.61", clients)

    def test_b11_detail_first_key_and_invalid_param(self):
        supplier_url = None
        sup = Client.objects.create(company=self.company, full_name="Пост", type=Client.StatusClient.SUPPLIERS)
        supplier_url = f"/api/main/suppliers/{sup.id}/recommendations/?analysis_days=13"
        r = self.api.get(supplier_url)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(list(r.data.keys())[0], "detail")
        self.assertEqual(r.data["code"], "invalid_param")
        self.assertEqual(r.data["field"], "analysis_days")
        self.assertEqual(r.data["data"]["allowed"], [7, 14, 30])
        # Ошибка из исключения (ApiError/DRF) — тоже detail первым.
        r = self.api.post("/api/main/products/create-manual/", {}, format="json", HTTP_IDEMPOTENCY_KEY="k-" + uuid.uuid4().hex)
        self.assertGreaterEqual(r.status_code, 400)
        if isinstance(r.data, dict) and "detail" in r.data:
            self.assertEqual(list(r.data.keys())[0], "detail")


class B02VariantStockDiffTests(TestCase):
    """Б-02: variant-stock-diff — отчёт перед пересчётом остатков по вариантам (только чтение)."""

    def setUp(self):
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="NBS", owner=self.owner, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.api = APIClient()
        self.api.force_authenticate(user=self.owner)

    def test_report_lists_only_mismatched_products(self):
        from apps.main.models import Product, ProductVariant

        nike = Product.objects.create(company=self.company, name="Nike", price=Decimal("10"), quantity=Decimal("89"))
        ProductVariant.objects.create(company=self.company, product=nike, size="42", quantity=Decimal("30"))
        ProductVariant.objects.create(company=self.company, product=nike, size="43", quantity=Decimal("23"))
        ok = Product.objects.create(company=self.company, name="Adidas", price=Decimal("10"), quantity=Decimal("5"))
        ProductVariant.objects.create(company=self.company, product=ok, size="40", quantity=Decimal("5"))
        Product.objects.create(company=self.company, name="Без размеров", price=Decimal("1"), quantity=Decimal("7"))
        # сигнал уже выровнял остаток; возвращаем «старое» значение, как в боевой базе
        Product.objects.filter(pk=nike.pk).update(quantity=Decimal("89"))
        r = self.api.get("/api/main/products/variant-stock-diff/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data), 1)
        row = r.data[0]
        self.assertEqual((row["name"], Decimal(row["quantity_now"]), Decimal(row["quantity_after"]), Decimal(row["diff"])),
                         ("Nike", Decimal("89"), Decimal("53"), Decimal("-36")))
        # только чтение: остаток не пересчитан
        nike.refresh_from_db()
        self.assertEqual(nike.quantity, Decimal("89"))

    def test_cashier_forbidden(self):
        cashier = User.objects.create_user(email="c@t.com", password="p", company=self.company)
        api = APIClient()
        api.force_authenticate(user=cashier)
        self.assertEqual(api.get("/api/main/products/variant-stock-diff/").status_code, 403)
