"""Тесты ТЗ «Бухгалтер»: режим «только смотреть», закрытие периода, статьи, остатки, проверка."""
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.construction.models import Cashbox, CashFlow
from apps.users.models import Company

User = get_user_model()


class BuhBase(APITestCase):
    def setUp(self):
        sfx = uuid.uuid4().hex[:8]
        self.owner = User.objects.create_user(email=f"o_{sfx}@t.com", password="p", role="owner")
        self.company = Company.objects.create(name=f"C {sfx}", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        self.acc = User.objects.create_user(email=f"a_{sfx}@t.com", password="p", role="manager")
        self.acc.company = self.company
        self.acc.can_view_cashbox = True
        self.acc.save()
        self.box = Cashbox.objects.create(name="Касса", company=self.company, role=Cashbox.CashboxRole.POS_MAIN)

    def post_flow(self, amount="100.00", **extra):
        data = {"cashbox": str(self.box.id), "type": "expense", "amount": amount, "name": "Тест"}
        data.update(extra)
        return self.client.post("/api/construction/cashflows/", data, format="json")


class ReadOnlyTests(BuhBase):
    def test_null_works_as_before(self):
        self.client.force_authenticate(self.acc)
        self.assertEqual(self.post_flow().status_code, 201)

    def test_false_blocks_write_allows_read(self):
        self.acc.can_edit_cashbox = False
        self.acc.save()
        self.client.force_authenticate(self.acc)
        r = self.post_flow()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data.get("code"), "read_only")
        self.assertEqual(self.client.get("/api/construction/cashflows/").status_code, 200)
        self.assertEqual(self.client.get("/api/construction/cashboxes/").status_code, 200)
        r = self.client.post("/api/construction/cashboxes/", {"name": "Новая"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data.get("code"), "read_only")
        # снятие запрета -> снова можно
        self.acc.can_edit_cashbox = None
        self.acc.save()
        self.assertEqual(self.post_flow().status_code, 201)

    def test_owner_and_admin_always_can(self):
        self.owner.can_edit_cashbox = False
        self.owner.save()
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.post_flow().status_code, 201)
        adm = User.objects.create_user(email="adm@t.com", password="p", role="admin")
        adm.company = self.company
        adm.can_edit_cashbox = False
        adm.save()
        self.client.force_authenticate(adm)
        self.assertEqual(self.post_flow().status_code, 201)

    def test_products_guard(self):
        self.acc.can_edit_products = False
        self.acc.can_view_products = True
        self.acc.save()
        self.client.force_authenticate(self.acc)
        r = self.client.post("/api/main/products/create-manual/", {"name": "x"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data.get("code"), "read_only")

    def test_fields_in_employee_patch_and_profile(self):
        self.client.force_authenticate(self.owner)
        r = self.client.patch(f"/api/users/employees/{self.acc.id}/", {"can_edit_cashbox": False}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIs(r.data.get("can_edit_cashbox"), False)
        self.acc.refresh_from_db()
        self.assertIs(self.acc.can_edit_cashbox, False)
        r = self.client.get(f"/api/users/employees/{self.acc.id}/")
        self.assertIn("can_edit_salary", r.data)
        self.assertIsNone(r.data["can_edit_salary"])
        self.client.force_authenticate(self.acc)
        r = self.client.get("/api/users/profile/")
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.data.get("can_edit_cashbox"), False)


class PeriodCloseTests(BuhBase):
    def _close(self, date):
        from apps.warehouse.models import WarehouseAccountingSettings

        WarehouseAccountingSettings.objects.update_or_create(
            company=self.company, defaults={"closed_until": date}
        )

    def test_create_blocked_in_closed_period_for_non_owner(self):
        from django.utils import timezone

        self._close(timezone.localdate())
        self.client.force_authenticate(self.acc)
        r = self.post_flow()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "period_closed")
        self.assertEqual(r.data["closed_until"], timezone.localdate().isoformat())

    def test_owner_allowed_and_open_period_ok(self):
        from django.utils import timezone

        self._close(timezone.localdate())
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.post_flow().status_code, 201)
        self._close(timezone.localdate() - timezone.timedelta(days=5))
        self.client.force_authenticate(self.acc)
        self.assertEqual(self.post_flow().status_code, 201)

    def test_update_delete_blocked(self):
        from django.utils import timezone

        self.client.force_authenticate(self.acc)
        fid = self.post_flow().data["id"]
        self._close(timezone.localdate())
        r = self.client.patch(f"/api/construction/cashflows/{fid}/", {"name": "x"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "period_closed")
        r = self.client.delete(f"/api/construction/cashflows/{fid}/")
        self.assertEqual(r.data["code"], "period_closed")
        r = self.client.post(f"/api/construction/cashflows/{fid}/cancel-request/", {}, format="json")
        self.assertEqual(r.data["code"], "period_closed")
