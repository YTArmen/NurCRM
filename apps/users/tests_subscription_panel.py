import uuid
import zoneinfo
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.users.models import Company, SubscriptionPlan

User = get_user_model()
BISHKEK_TZ = zoneinfo.ZoneInfo("Asia/Bishkek")


class CompanySubscriptionPanelTests(APITestCase):
    def setUp(self):
        # 1. Тарифы
        self.plan_start = SubscriptionPlan.objects.create(
            name="Старт",
            price=Decimal("990.00"),
            description="Базовый тариф для магазина",
        )
        self.plan_standard = SubscriptionPlan.objects.create(
            name="Стандарт",
            price=Decimal("1990.00"),
            description="Полный доступ ко всем модулям",
        )

        # 2. Владелец
        owner_email = f"owner_{uuid.uuid4().hex[:8]}@test.com"
        self.owner = User.objects.create_user(email=owner_email, password="password", role="owner")

        # 3. Компания
        now = timezone.now()
        today_local = timezone.localtime(now, BISHKEK_TZ).date()

        self.company = Company.objects.create(
            name=f"Test Company {uuid.uuid4().hex[:6]}",
            owner=self.owner,
            subscription_plan=self.plan_standard,
            start_date=now - timedelta(days=30),
            end_date=now + timedelta(days=15),
            cashier_password="secret123",
        )
        self.owner.company = self.company
        self.owner.save()

        # 4. Обычный сотрудник (кассир)
        cashier_email = f"cashier_{uuid.uuid4().hex[:8]}@test.com"
        self.cashier = User.objects.create_user(email=cashier_email, password="password", role="cashier")
        self.cashier.company = self.company
        self.cashier.save()

    def test_owner_gets_full_subscription_in_company_endpoint(self):
        """GET /users/company/ для owner отдает все поля подписки и лимитов."""
        self.client.force_authenticate(user=self.owner)
        res = self.client.get("/api/users/company/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        data = res.data
        self.assertIn("created_at", data)
        self.assertIn("end_date", data)
        self.assertIn("subscription_end_date", data)
        self.assertEqual(data["end_date"], data["subscription_end_date"])

        # subscription
        sub = data["subscription"]
        self.assertIsNotNone(sub)
        self.assertEqual(sub["status"], "active")
        self.assertGreater(sub["days_left"], 7)
        self.assertFalse(sub["is_trial"])
        self.assertIsNone(sub["auto_renew"])
        self.assertEqual(sub["next_payment_at"], data["end_date"])
        self.assertIsNone(sub["last_payment"])

        # plan
        plan = sub["plan"]
        self.assertIsNotNone(plan)
        self.assertEqual(plan["code"], "standard")
        self.assertEqual(plan["name"], "Стандарт")
        self.assertEqual(plan["price"], "1990.00")
        self.assertEqual(plan["currency"], "KGS")
        self.assertEqual(plan["period"], "month")

        # limits
        limits = data["limits"]
        self.assertIn("employees", limits)
        self.assertEqual(limits["employees"]["used"], 2)  # owner + cashier
        self.assertEqual(limits["employees"]["max"], 10)  # «Стандарт» — до 10 сотрудников (26.10.2026)

    def test_non_owner_sees_redacted_financial_info(self):
        """§7: Не-владелец не видит цену, currency, last_payment, next_payment_at, auto_renew, cashier_password."""
        self.client.force_authenticate(user=self.cashier)
        res = self.client.get("/api/users/company/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        data = res.data
        self.assertNotIn("cashier_password", data)

        # Публичные поля подписки видны
        sub = data["subscription"]
        self.assertEqual(sub["status"], "active")
        self.assertIsNotNone(sub["days_left"])
        self.assertEqual(sub["end_date"], data["end_date"])

        # Финансовые поля скрыты
        self.assertNotIn("last_payment", sub)
        self.assertNotIn("next_payment_at", sub)
        self.assertNotIn("auto_renew", sub)

        plan = sub["plan"]
        self.assertEqual(plan["code"], "standard")
        self.assertEqual(plan["name"], "Стандарт")
        self.assertNotIn("price", plan)
        self.assertNotIn("currency", plan)

    def test_status_expiring_soon_and_expired(self):
        """Проверка статусов: expiring_soon (<=7 дней) и expired (<0)."""
        self.client.force_authenticate(user=self.owner)
        now = timezone.now()

        # 1. Срок через 3 дня -> expiring_soon
        self.company.end_date = now + timedelta(days=3)
        self.company.save()
        res = self.client.get("/api/users/company/")
        self.assertEqual(res.data["subscription"]["status"], "expiring_soon")
        self.assertLessEqual(res.data["subscription"]["days_left"], 3)

        # 2. Срок сегодня -> expiring_soon, days_left = 0
        today_local = timezone.localtime(now, BISHKEK_TZ).date()
        self.company.end_date = timezone.make_aware(
            timezone.datetime(today_local.year, today_local.month, today_local.day, 12, 0, 0),
            BISHKEK_TZ
        )
        self.company.save()
        res = self.client.get("/api/users/company/")
        self.assertEqual(res.data["subscription"]["status"], "expiring_soon")
        self.assertEqual(res.data["subscription"]["days_left"], 0)

        # 3. Срок вчера -> expired
        self.company.end_date = now - timedelta(days=2)
        self.company.save()
        res = self.client.get("/api/users/company/")
        self.assertEqual(res.data["subscription"]["status"], "expired")
        self.assertLess(res.data["subscription"]["days_left"], 0)

    def test_edge_case_no_end_date_returns_unknown(self):
        """§9: Если end_date не задан -> days_left: null, status: 'unknown'."""
        self.company.end_date = None
        self.company.subscription_plan = None
        self.company.save()

        self.client.force_authenticate(user=self.owner)
        res = self.client.get("/api/users/company/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        sub = res.data["subscription"]
        self.assertIsNone(sub["end_date"])
        self.assertIsNone(sub["days_left"])
        self.assertEqual(sub["status"], "unknown")
        self.assertIsNone(sub["plan"])

    def test_plan_start_limit_is_3_employees(self):
        """§6: Тариф 'Старт' отдает limit.employees.max = 3."""
        self.company.subscription_plan = self.plan_start
        self.company.save()

        self.client.force_authenticate(user=self.owner)
        res = self.client.get("/api/users/company/")
        limits = res.data["limits"]
        self.assertEqual(limits["employees"]["max"], 3)
        self.assertEqual(res.data["subscription"]["plan"]["code"], "start")

    def test_subscription_detail_endpoint_owner_only(self):
        """GET /users/company/subscription/ доступен только owner (403 для других)."""
        # 1. Owner -> 200 OK
        self.client.force_authenticate(user=self.owner)
        res_owner = self.client.get("/api/users/company/subscription/")
        self.assertEqual(res_owner.status_code, status.HTTP_200_OK)
        self.assertIn("company_created_at", res_owner.data)
        self.assertIn("status", res_owner.data)
        self.assertIn("days_left", res_owner.data)
        self.assertIn("payments", res_owner.data)
        self.assertEqual(res_owner.data["payments"], [])

        # 2. Cashier -> 403 Forbidden
        self.client.force_authenticate(user=self.cashier)
        res_cashier = self.client.get("/api/users/company/subscription/")
        self.assertEqual(res_cashier.status_code, status.HTTP_403_FORBIDDEN)

    def test_subscription_plans_endpoint_has_code(self):
        """GET /users/subscription-plans/ отдает поле code: start | standard."""
        self.client.force_authenticate(user=self.owner)
        res = self.client.get("/api/users/subscription-plans/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get("results", res.data)
        codes = [p.get("code") for p in results]
        self.assertIn("start", codes)
        self.assertIn("standard", codes)
