"""ТЗ Н-09 (23.10.2026): closed_by / close_reason / auto_close_at, настройка автозакрытия, код shift_closed."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashShift
from apps.construction.services_shifts import close_stale_shifts
from apps.users.models import Company, CompanyAppSettings, User

D = Decimal


class ShiftAutoCloseTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.company = Company.objects.create(name="Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        self.cb = Cashbox.objects.create(company=self.company, name="Касса", role=Cashbox.CashboxRole.POS_MAIN)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def _shift(self, opened_at):
        sh = CashShift.objects.create(company=self.company, cashbox=self.cb, cashier=self.owner,
                                      status=CashShift.Status.OPEN, opening_cash=D("10.00"))
        CashShift.objects.filter(pk=sh.pk).update(opened_at=opened_at)  # auto_now_add
        sh.refresh_from_db()
        return sh

    def _detail(self, sh):
        r = self.api.get(f"/api/construction/shifts/{sh.id}/")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_auto_close_at_and_system_close(self):
        opened = timezone.now().replace(minute=2, second=5, microsecond=0) - timedelta(hours=30)
        sh = self._shift(opened)
        d = self._detail(sh)
        expected = (opened + timedelta(hours=24)).replace(minute=0, second=0) + timedelta(hours=1)
        self.assertEqual(d["auto_close_at"], expected.isoformat())
        self.assertIsNone(d["closed_by"])
        self.assertIsNone(d["close_reason"])
        res = close_stale_shifts()
        self.assertEqual(res["closed"], [sh.pk])
        d = self._detail(sh)
        self.assertEqual(d["closed_by"], "system")
        self.assertEqual(d["close_reason"], "auto_24h")
        self.assertIsNone(d["auto_close_at"])

    def test_manual_close(self):
        sh = self._shift(timezone.now())
        sh.close(D("10.00"))
        d = self._detail(sh)
        self.assertEqual(d["closed_by"], "user")
        self.assertEqual(d["close_reason"], "manual")

    def test_setting_off_keeps_shift_open(self):
        sh = self._shift(timezone.now() - timedelta(hours=40))
        CompanyAppSettings.objects.create(company=self.company, app="nurmarket-mobile",
                                          settings={"ShiftAutoClose": {"enabled": False}})
        self.assertIsNone(self._detail(sh)["auto_close_at"])
        self.assertEqual(close_stale_shifts()["closed"], [])
        sh.refresh_from_db()
        self.assertEqual(sh.status, CashShift.Status.OPEN)

    def test_setting_custom_hours(self):
        sh = self._shift(timezone.now() - timedelta(hours=13))
        CompanyAppSettings.objects.create(company=self.company, app="nurmarket-mobile",
                                          settings={"ShiftAutoClose": {"enabled": True, "hours": 12}})
        self.assertIsNotNone(self._detail(sh)["auto_close_at"])
        self.assertEqual(close_stale_shifts()["closed"], [sh.pk])

    def test_sale_into_closed_shift_code(self):
        sh = self._shift(timezone.now())
        sh.close(D("10.00"))
        r = self.api.post("/api/main/pos/sales/start/", {"shift_id": str(sh.id)}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data.get("code"), "shift_closed", r.data)
        self.assertIn("Смена не открыта", str(r.data.get("detail")))
