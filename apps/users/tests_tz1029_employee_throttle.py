"""ТЗ 29.10 Б-101 п.6: 429 на частые изменения сотрудников."""
from django.test import override_settings
from rest_framework.test import APIClient

from apps.users.models import Company, SubscriptionPlan, User
from apps.users.tests_tz_n16_permissions import LOCMEM, N16Base


@override_settings(CACHES=LOCMEM)
class EmployeeThrottleTests(N16Base):
    def test_31st_patch_is_429_get_ok_other_company_ok(self):
        url = f"/api/users/employees/{self.cashier.id}/"
        for i in range(30):
            r = self.api_owner.patch(url, {"comment": f"c{i}"}, format="json")
            self.assertEqual(r.status_code, 200, r.content)
        r = self.api_owner.patch(url, {"comment": "x"}, format="json")
        self.assertEqual(r.status_code, 429, r.content)
        body = r.json()
        self.assertEqual(body["code"], "too_many_requests")
        self.assertIn("detail", body)
        self.assertGreaterEqual(body["retry_after"], 1)
        self.assertIn("Retry-After", r)
        # чтение не ограничено
        self.assertEqual(self.api_owner.get("/api/users/employees/").status_code, 200)
        # админ той же компании делит лимит
        self.assertEqual(self.api_admin.patch(url, {"comment": "y"}, format="json").status_code, 429)
        # другая компания не затронута
        owner2 = User.objects.create_user(email="o2@t.kg", password="x", role="owner")
        plan = SubscriptionPlan.objects.create(name="П2", price=1)
        c2 = Company.objects.create(name="Co2", owner=owner2, is_active=True, subscription_plan=plan)
        owner2.company = c2
        owner2.save()
        emp2 = User.objects.create_user(email="e2@t.kg", password="x", company=c2, role="salesperson")
        api2 = APIClient()
        api2.force_authenticate(owner2)
        r = api2.patch(f"/api/users/employees/{emp2.id}/", {"comment": "z"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
