"""Приложение клиентов: бесплатный период, защита карты, отчёт по неделям, ФИО на старых кассах."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient

from apps.clientapp.models import AppShopSettings, ClientAppConfig
from apps.clientapp.tests import AppTestMixin
from apps.main.models import Client
from apps.main.tests_kassa_api import KassaBase
from apps.users.models import CompanyAddon, User

SETTINGS = "/api/main/app-shop-settings/"


class FreePeriodTests(AppTestMixin, KassaBase):
    def enable(self, **extra):
        return self.api.patch(SETTINGS, {"show_in_app": True, "address": "Бишкек, Чуй 1",
                                         "latitude": "42.87", "longitude": "74.59", **extra}, format="json")

    def test_free_by_default_any_company_and_feature(self):
        r = self.enable()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["access"]["allowed"])
        self.assertTrue(r.data["visible_in_app"])
        self.assertEqual(len(self.app.get("/api/v1/shops").json()), 1)
        company = self.api.get("/api/users/company/").data
        self.assertIn("client_app", company["features"])

    def test_after_free_until_needs_feature(self):
        self.enable()
        ClientAppConfig.objects.update_or_create(pk=1, defaults={"free_until": timezone.localdate() - timedelta(days=1)})
        self.assertEqual(self.app.get("/api/v1/shops").json(), [])  # пропал с карты
        self.assertNotIn("client_app", self.api.get("/api/users/company/").data["features"])
        r = self.enable()
        self.assertEqual((r.status_code, r.data["code"]), (403, "client_app_payment_required"))

        CompanyAddon.objects.create(company=self.company, code="client_app")
        r = self.enable()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(self.app.get("/api/v1/shops").json()), 1)

    def test_free_until_shown_in_features_detail(self):
        until = timezone.localdate() + timedelta(days=60)
        ClientAppConfig.objects.update_or_create(pk=1, defaults={"free_until": until})
        detail = self.api.get("/api/users/company/").data["features_detail"]
        self.assertIn({"code": "client_app", "until": until.isoformat()}, detail)


class MapProtectionTests(AppTestMixin, KassaBase):
    def test_needs_coords_and_admin_can_hide(self):
        r = self.api.patch(SETTINGS, {"show_in_app": True, "address": "Бишкек, Чуй 1"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # ТЗ 14.10, п. 2.2: пока геокодер не прошёл, магазин в списке без координат
        listed = self.app.get("/api/v1/shops").json()
        self.assertEqual((len(listed), listed[0]["latitude"]), (1, None))
        # геокодер адрес не нашёл — магазин скрыт, владелец видит причину
        AppShopSettings.objects.filter(company=self.company, branch=None).update(geocode_status="not_found",
                                                                                 geocode_attempts=1)
        self.assertEqual(self.app.get("/api/v1/shops").json(), [])
        self.assertIn("no_coordinates", self.api.get(SETTINGS).data["not_visible_reasons"])

        self.api.patch(SETTINGS, {"latitude": "42.87", "longitude": "74.59"}, format="json")
        self.assertEqual(len(self.app.get("/api/v1/shops").json()), 1)

        admin = User.objects.create_user(email="pa@t.kg", password="x")
        User.objects.filter(pk=admin.pk).update(is_platform_admin=True)
        admin.refresh_from_db()
        pa = APIClient()
        pa.force_authenticate(admin)
        r = pa.patch(f"/api/platform-admin/client-app/shops/{self.company.id}/", {"hidden": True, "reason": "спам"},
                     format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.app.get("/api/v1/shops").json(), [])
        self.assertIn("hidden_by_admin", self.api.get(SETTINGS).data["not_visible_reasons"])
        # обычный владелец так не может
        self.assertEqual(self.api.patch(f"/api/platform-admin/client-app/shops/{self.company.id}/",
                                        {"hidden": False}, format="json").status_code, 403)


class WeeklyReportTests(AppTestMixin, KassaBase):
    def test_counts_shops_customers_and_app_sales(self):
        AppShopSettings.objects.create(company=self.company, show_in_app=True, address="a",
                                       latitude=Decimal("42.8"), longitude=Decimal("74.5"))
        self.quick(client=str(self.client_obj.id))  # до регистрации в приложении — не считается
        customer, _ = self.make_customer(phone="+996555000222")  # телефон клиента кассы
        self.quick(client=str(self.client_obj.id))
        from apps.clientapp.report import weekly_report

        r = weekly_report(2)
        last = r["weeks"][-1]
        self.assertEqual((last["shops_enabled"], last["customers_total"], last["customers_new"]), (1, 1, 1))
        self.assertEqual((last["app_sales_count"], last["app_sales_total"], last["app_buyers"]), (1, "200.00", 1))
        self.assertEqual(r["now"]["shops_on_map"], 1)


class OldKassaNameTests(AppTestMixin, KassaBase):
    def test_placeholder_client_gets_app_name(self):
        self.make_customer(phone="+996700111222", name="Айгерим Асанова")
        c = Client.objects.create(company=self.company, full_name="0700111222", phone="0700111222")
        c.refresh_from_db()
        self.assertEqual(c.full_name, "Айгерим Асанова")
        real = Client.objects.create(company=self.company, full_name="Айка", phone="0700111222")
        real.refresh_from_db()
        self.assertEqual(real.full_name, "Айка")  # настоящее имя от кассира не трогаем

    def test_customer_name_fills_existing_clients(self):
        c = Client.objects.create(company=self.company, full_name="", phone="+996700333444")
        with self.captureOnCommitCallbacks(execute=True):
            self.make_customer(phone="+996700333444", name="Бакыт")
        c.refresh_from_db()
        self.assertEqual(c.full_name, "Бакыт")
