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

    def test_bulk_status_blocked(self):
        from django.utils import timezone

        self.client.force_authenticate(self.acc)
        fid = self.post_flow().data["id"]
        self._close(timezone.localdate())
        r = self.client.patch(
            "/api/construction/cashflows/bulk/status/",
            {"items": [{"id": fid, "status": "approved"}]}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "period_closed")


class CategoryTests(BuhBase):
    def test_default_categories_lazy_and_once(self):
        from apps.construction.models import CashFlowCategory

        self.assertEqual(CashFlowCategory.objects.filter(company=self.company).count(), 0)
        self.client.force_authenticate(self.acc)
        r = self.client.get("/api/construction/cashflow-categories/")
        self.assertEqual(r.status_code, 200)
        names = {c["name"] for c in r.data}
        self.assertEqual(len(names), 11)
        self.assertIn("Аренда", names)
        self.assertIn("archived", r.data[0])
        self.assertIn("type", r.data[0])
        self.client.get("/api/construction/cashflow-categories/")
        self.assertEqual(CashFlowCategory.objects.filter(company=self.company).count(), 11)
        r = self.client.get("/api/construction/cashflow-categories/?type=income")
        self.assertTrue(all(c["type"] in ("income", "both") for c in r.data))

    def test_create_with_name_and_category_default_other(self):
        self.client.force_authenticate(self.owner)
        r = self.client.post(
            "/api/construction/cashflow-categories/", {"name": "Охрана", "type": "expense"}, format="json"
        )
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["name"], "Охрана")
        cat_id = r.data["id"]
        r = self.post_flow(category_id=cat_id)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(str(r.data["category_id"]), cat_id)
        r = self.post_flow()
        self.assertEqual(r.data["category_title"], "Прочие")

    def test_archived_category_rejected(self):
        self.client.force_authenticate(self.owner)
        r = self.client.post("/api/construction/cashflow-categories/", {"name": "Старая"}, format="json")
        cid = r.data["id"]
        self.client.patch(f"/api/construction/cashflow-categories/{cid}/", {"archived": True}, format="json")
        self.assertEqual(self.post_flow(category_id=cid).status_code, 400)

    def test_auto_cashflow_gets_category(self):
        from apps.construction.auto_cashflow import create_auto_cashflow

        cf = create_auto_cashflow(
            company=self.company, cashbox=self.box, type="income", amount="50",
            source_kind=CashFlow.SourceKind.DEBT_REPAYMENT, source_id=str(uuid.uuid4()),
        )
        self.assertEqual(cf.category.title, "Оплата долга покупателем")

    def test_finance_analytics_by_category(self):
        self.client.force_authenticate(self.owner)
        r = self.client.get("/api/construction/cashflow-categories/")
        rent = next(c["id"] for c in r.data if c["name"] == "Аренда")
        r = self.post_flow(amount="30.00", category_id=rent, status="approved")
        self.assertEqual(r.status_code, 201, r.data)
        CashFlow.objects.filter(company=self.company).update(status=CashFlow.Status.APPROVED)
        r = self.client.get("/api/main/analytics/market/?tab=finance&period=month")
        self.assertEqual(r.status_code, 200, r.data)
        tables = r.data["tables"]
        self.assertIn("expense_breakdown", tables)  # старое поле на месте
        row = next(x for x in tables["expense_by_category"] if x["name"] == "Аренда")
        self.assertEqual(row["total"], "30.00")
        self.assertEqual(row["count"], 1)


class BalancesTests(BuhBase):
    def _flow(self, type_, amount, when):
        cf = CashFlow.objects.create(
            company=self.company, cashbox=self.box, type=type_, amount=Decimal(amount),
            status=CashFlow.Status.APPROVED, name="t",
        )
        CashFlow.objects.filter(pk=cf.pk).update(created_at=when)

    def test_balances(self):
        from datetime import datetime, time

        from django.utils import timezone

        def at(d, h=12):
            return timezone.make_aware(datetime.combine(d, time(h)))

        today = timezone.localdate()
        d1 = today - timezone.timedelta(days=10)
        self._flow("income", "1000", at(d1 - timezone.timedelta(days=5)))  # до периода
        self._flow("expense", "100", at(d1 - timezone.timedelta(days=1)))
        self._flow("income", "300", at(d1))
        self._flow("expense", "50", at(today))
        self.client.force_authenticate(self.acc)
        r = self.client.get(f"/api/construction/cashboxes/balances/?date_from={d1}&date_to={today}")
        self.assertEqual(r.status_code, 200, r.data)
        row = next(x for x in r.data if x["cashbox_id"] == str(self.box.id))
        self.assertEqual(
            (row["opening"], row["income"], row["expense"], row["closing"]),
            ("900.00", "300.00", "50.00", "1150.00"),
        )
        # на 1-е число = только остаток до
        r = self.client.get(f"/api/construction/cashboxes/balances/?date_from={d1}&date_to={d1}")
        row = next(x for x in r.data if x["cashbox_id"] == str(self.box.id))
        self.assertEqual(row["closing"], "1200.00")
        self.assertEqual(self.client.get("/api/construction/cashboxes/balances/").status_code, 400)


class CheckedTests(BuhBase):
    def test_check_and_filter(self):
        from django.utils import timezone

        self.client.force_authenticate(self.owner)
        fid = self.post_flow(amount="40.00").data["id"]
        self.post_flow(amount="60.00")
        # бухгалтер только смотрит (can_edit_cashbox=False), но отмечать может
        self.acc.can_edit_cashbox = False
        self.acc.save()
        self.client.force_authenticate(self.acc)
        r = self.client.post(f"/api/construction/cashflows/{fid}/check/", {"checked": True, "note": "ок"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["checked"])
        self.assertEqual(r.data["accountant_note"], "ок")
        f = CashFlow.objects.get(pk=fid)
        self.assertEqual(f.amount, Decimal("40.00"))
        self.assertEqual(f.checked_by_id, self.acc.id)
        ids = lambda q: {x["id"] for x in self.client.get(f"/api/construction/cashflows/?checked={q}").data["results"]}
        self.assertEqual(ids("true"), {fid})
        self.assertNotIn(fid, ids("false"))
        # в закрытом периоде можно
        from apps.warehouse.models import WarehouseAccountingSettings

        WarehouseAccountingSettings.objects.create(company=self.company, closed_until=timezone.localdate())
        r = self.client.post(f"/api/construction/cashflows/{fid}/check/", {"checked": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["checked"])
        self.assertIsNone(CashFlow.objects.get(pk=fid).checked_at)

    def test_no_view_access_forbidden(self):
        self.client.force_authenticate(self.owner)
        fid = self.post_flow().data["id"]
        self.acc.can_view_cashbox = False
        self.acc.save()
        self.client.force_authenticate(self.acc)
        r = self.client.post(f"/api/construction/cashflows/{fid}/check/", {"checked": True}, format="json")
        self.assertEqual(r.status_code, 403)


class SalaryTests(BuhBase):
    def test_edit_salary_false_blocks_rates_and_payouts(self):
        self.acc.can_edit_salary = False
        self.acc.save()
        self.client.force_authenticate(self.acc)
        r = self.client.post("/api/warehouse/salary/payouts/", {}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data.get("code"), "read_only")


class AuditLogTests(BuhBase):
    def test_create_update_delete_logged_and_filter(self):
        self.client.force_authenticate(self.owner)
        fid = self.post_flow("100.00").data["id"]
        self.client.patch(f"/api/construction/cashflows/{fid}/", {"amount": "250.00"}, format="json")
        self.client.delete(f"/api/construction/cashflows/{fid}/")
        r = self.client.get(f"/api/construction/audit-log/?object_type=cashflow&object_id={fid}")
        self.assertEqual(r.status_code, 200)
        acts = [x["action"] for x in r.data["results"]]
        self.assertEqual(sorted(acts), ["create", "delete", "update"])
        upd = next(x for x in r.data["results"] if x["action"] == "update")
        self.assertEqual(upd["before"]["amount"], "100.00")
        self.assertEqual(upd["after"]["amount"], "250.00")
        self.assertEqual(self.client.get("/api/construction/audit-log/?object_id=nope").data["count"], 0)

    def test_closed_until_logged_and_no_access(self):
        self.client.force_authenticate(self.owner)
        self.client.patch("/api/warehouse/settings/period-close/", {"closed_until": "2026-09-30"}, format="json")
        r = self.client.get("/api/construction/audit-log/?action=closed_until")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["after"]["closed_until"], "2026-09-30")
        other = User.objects.create_user(email="x@t.com", password="p", role="manager")
        other.company = self.company
        other.save()
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get("/api/construction/audit-log/").status_code, 403)
