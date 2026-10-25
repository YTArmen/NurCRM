"""ТЗ Н-16: права сотрудников на сервере, коды доступа, сброс пароля."""
import uuid
from datetime import date
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.construction.models import Cashbox, CashShift
from apps.main.models import Client, ClientDeal, Product, Sale
from apps.users.models import Company, SubscriptionPlan, User


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "n16"}}


@override_settings(CACHES=LOCMEM)
class N16Base(TestCase):
    """Магазин: владелец, админ, обычный кассир (роль salesperson, флаги как при создании)."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        plan = SubscriptionPlan.objects.create(name="Стандарт", price=Decimal("1.00"))
        self.company = Company.objects.create(name="N16 Co", owner=self.owner, is_active=True, subscription_plan=plan)
        self.owner.company = self.company
        self.owner.save()
        self.admin = User.objects.create_user(
            email=f"a{uuid.uuid4().hex[:6]}@t.kg", password="x", company=self.company, role="admin"
        )
        # как EmployeeCreateSerializer: флаги не заданы -> can_view_dashboard=True, остальное по умолчанию
        self.cashier = User.objects.create_user(
            email=f"c{uuid.uuid4().hex[:6]}@t.kg", password="x", company=self.company, role="salesperson",
            can_view_dashboard=True,
        )
        self.cashbox = Cashbox.objects.create(name="Касса", company=self.company, role=Cashbox.CashboxRole.POS_MAIN)
        self.product = Product.objects.create(
            company=self.company, name="Хлеб", price=Decimal("100.00"), quantity=Decimal("50"), barcode="4600000000017"
        )
        self.client_obj = Client.objects.create(company=self.company, full_name="Айбек", phone="+996555000222")
        self.api_owner = self.api_for(self.owner)
        self.api_admin = self.api_for(self.admin)
        self.api_cashier = self.api_for(self.cashier)

    @staticmethod
    def api_for(user):
        api = APIClient()
        api.force_authenticate(user)
        return api

    def open_shift(self, user=None):
        return CashShift.objects.create(
            company=self.company, cashbox=self.cashbox, cashier=user or self.cashier,
            status=CashShift.Status.OPEN, opening_cash=Decimal("100.00"),
        )

    def checkout(self, api, shift, **body):
        payload = {
            "shift": str(shift.id),
            "items": [{"product": str(self.product.id), "qty": "1", "price": "100.00"}],
            "payment": {"method": "cash", "received": "100.00"},
        }
        payload.update(body)
        return api.post("/api/main/pos/checkout/", payload, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))

    def assertDenied(self, resp, flag=None):
        self.assertEqual(resp.status_code, 403, getattr(resp, "content", b""))
        self.assertEqual(resp.json().get("code"), "permission_denied", resp.content)
        if flag:
            self.assertEqual(resp.json().get("required"), flag)


class CashierStillWorks(N16Base):
    """Обычный кассир не теряет работу на кассе (регрессия: писалась ДО ограничений)."""

    def test_catalogue_read(self):
        r = self.api_cashier.get("/api/main/products/list/")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.get("/api/main/products/compact-list/")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.get(f"/api/main/products/barcode/{self.product.barcode}/")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.get(f"/api/main/products/{self.product.id}/")
        self.assertEqual(r.status_code, 200, r.content)

    def test_clients_search_and_create_for_receipt(self):
        r = self.api_cashier.get("/api/main/clients/?search=Айбек")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.post(
            "/api/main/clients/", {"full_name": "Новый", "phone": "+996700111222"}, format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r.status_code, 201, r.content)

    def test_own_shift_open_report_close(self):
        r = self.api_cashier.post(
            "/api/construction/shifts/open/", {"cashbox": str(self.cashbox.id), "opening_cash": "100.00"},
            format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r.status_code, 201, r.content)
        sid = r.json()["id"]
        r = self.api_cashier.get(f"/api/construction/shifts/{sid}/report/")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.get(f"/api/construction/shifts/{sid}/")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.post(
            f"/api/construction/shifts/{sid}/close/", {"closing_cash": "100.00"}, format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r.status_code, 200, r.content)

    def test_checkout_sales_list_return(self):
        shift = self.open_shift()
        r = self.checkout(self.api_cashier, shift)
        self.assertEqual(r.status_code, 201, r.content)
        sale_id = r.json()["id"]
        r = self.api_cashier.get("/api/main/pos/sales/")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.get(f"/api/main/pos/sales/{sale_id}/")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.get(f"/api/construction/cash/shifts/{shift.id}/sales/")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.post(f"/api/main/pos/sales/{sale_id}/return/", {"reason": "брак"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.get("/api/main/pos/returns/")
        self.assertEqual(r.status_code, 200, r.content)

    def test_pay_debt_and_debtors(self):
        shift = self.open_shift()
        # существующее поведение: кассир (не owner-like) гасит долги только «своих» клиентов (salesperson)
        self.client_obj.salesperson = self.cashier
        self.client_obj.save()
        sale = Sale.objects.create(
            company=self.company, cashbox=self.cashbox, user=self.cashier, client=self.client_obj,
            payment_method="debt", status=Sale.Status.DEBT, subtotal=Decimal("100.00"), total=Decimal("100.00"),
            debt_initial=Decimal("100.00"), debt_remaining=Decimal("100.00"),
        )
        ClientDeal.objects.create(
            company=self.company, client=self.client_obj, sale=sale, kind=ClientDeal.Kind.DEBT, title="Долг",
            amount=Decimal("100.00"), debt_months=1, first_due_date=date.today(),
        )
        r = self.api_cashier.get("/api/main/clients/debtors/")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api_cashier.post(
            f"/api/main/clients/{self.client_obj.id}/pay-debt/", {"amount": "30.00", "shift": str(shift.id)},
            format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r.status_code, 200, r.content)

    def test_drawer_ops_in_own_shift_and_cashboxes_read(self):
        shift = self.open_shift()
        for kind, typ in (("shift_drawer_inflow", "income"), ("shift_drawer_outflow", "expense")):
            r = self.api_cashier.post("/api/construction/cashflows/", {
                "cashbox": str(self.cashbox.id), "shift": str(shift.id), "type": typ, "name": kind,
                "amount": "10.00", "source_kind": kind, "source_id": f"{kind}-1", "payment_method": "cash",
            }, format="json")
            self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.api_cashier.get("/api/construction/cashflows/").status_code, 200)
        self.assertEqual(self.api_cashier.get("/api/construction/cashboxes/").status_code, 200)


class RestrictionsTests(N16Base):
    def setUp(self):
        super().setUp()
        self.other = User.objects.create_user(
            email=f"e{uuid.uuid4().hex[:6]}@t.kg", password="x", company=self.company, role="salesperson"
        )

    # --- сотрудники ---
    def test_employees_list_and_detail(self):
        self.assertDenied(self.api_cashier.get("/api/users/employees/"), "can_view_employees")
        self.assertDenied(self.api_cashier.patch(f"/api/users/employees/{self.other.id}/", {"first_name": "X"}, format="json"))
        self.assertEqual(self.api_owner.get("/api/users/employees/").status_code, 200)
        self.assertEqual(self.api_admin.get("/api/users/employees/").status_code, 200)
        self.cashier.can_view_employees = True
        self.cashier.save()
        self.assertEqual(self.api_for(self.cashier).get("/api/users/employees/").status_code, 200)

    def test_employee_create_returns_id(self):
        r = self.api_owner.post("/api/users/employees/create/", {
            "email": f"n{uuid.uuid4().hex[:6]}@t.kg", "first_name": "Н", "last_name": "К", "role": "salesperson",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()["id"])
        self.assertTrue(r.json()["generated_password"])
        self.assertTrue(User.objects.get(pk=r.json()["id"]).can_use_pos)
        r = self.api_cashier.post("/api/users/employees/create/", {"email": "z@t.kg"}, format="json")
        self.assertEqual(r.status_code, 403)

    # --- сброс пароля ---
    def test_reset_password_owner_admin_only_and_audit(self):
        from apps.construction.models import AuditLog

        url = f"/api/users/employees/{self.other.id}/reset-password/"
        self.assertDenied(self.api_cashier.post(url))
        r = self.api_admin.post(url)
        self.assertEqual(r.status_code, 200, r.content)
        pw = r.json()["password"]
        self.other.refresh_from_db()
        self.assertTrue(self.other.check_password(pw))
        self.assertTrue(AuditLog.objects.filter(company=self.company, action="reset_password", object_id=str(self.other.id)).exists())
        self.assertNotIn(pw, str(list(AuditLog.objects.values("before", "after", "object_name"))))

    def test_reset_password_other_company_404(self):
        stranger = User.objects.create_user(email=f"s{uuid.uuid4().hex[:6]}@t.kg", password="x")
        r = self.api_owner.post(f"/api/users/employees/{stranger.id}/reset-password/")
        self.assertEqual(r.status_code, 404)

    # --- товары ---
    def test_products_write_denied_for_cashier(self):
        r = self.api_cashier.patch(f"/api/main/products/{self.product.id}/", {"price": "1.00"}, format="json")
        self.assertDenied(r, "can_view_products")
        r = self.api_cashier.delete(f"/api/main/products/{self.product.id}/")
        self.assertDenied(r, "can_view_products")
        # Создание товара со сканера кассиру оставлено (решение 25.10) — не 403.
        r = self.api_cashier.post("/api/main/products/create-manual/", {"name": "N", "price": "1"}, format="json")
        self.assertNotEqual(r.status_code, 403, r.content)
        r = self.api_cashier.post("/api/main/products/bulk-update/", {}, format="json")
        self.assertDenied(r, "can_view_products")
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("100.00"))

    def test_products_write_allowed_with_flag_and_for_owner_admin(self):
        for api in (self.api_owner, self.api_admin):
            r = api.patch(f"/api/main/products/{self.product.id}/", {"price": "101.00"}, format="json")
            self.assertEqual(r.status_code, 200, r.content)
        self.cashier.can_view_products = True
        self.cashier.save()
        r = self.api_for(self.cashier).patch(f"/api/main/products/{self.product.id}/", {"price": "102.00"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        # а явный запрет «менять товары» по-прежнему сильнее
        self.cashier.can_edit_products = False
        self.cashier.save()
        r = self.api_for(self.cashier).patch(f"/api/main/products/{self.product.id}/", {"price": "103.00"}, format="json")
        self.assertEqual(r.status_code, 403)

    # --- клиенты ---
    def test_client_update_delete_denied_but_create_and_read_ok(self):
        url = f"/api/main/clients/{self.client_obj.id}/"
        self.assertEqual(self.api_cashier.get(url).status_code, 200)
        self.assertDenied(self.api_cashier.patch(url, {"full_name": "Х"}, format="json"), "can_view_clients")
        self.assertDenied(self.api_cashier.delete(url), "can_view_clients")
        r = self.api_owner.patch(url, {"full_name": "Новое"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    # --- кассы ---
    def test_cashboxes_write_denied(self):
        r = self.api_cashier.post("/api/construction/cashboxes/", {"name": "Новая"}, format="json")
        self.assertDenied(r, "can_view_cashbox")
        r = self.api_cashier.patch(f"/api/construction/cashboxes/{self.cashbox.id}/", {"name": "Х"}, format="json")
        self.assertDenied(r, "can_view_cashbox")
        r = self.api_cashier.delete(f"/api/construction/cashboxes/{self.cashbox.id}/")
        self.assertDenied(r, "can_view_cashbox")
        r = self.api_owner.post("/api/construction/cashboxes/", {"name": "Новая"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_cashflows_write_outside_own_shift_denied(self):
        body = {"cashbox": str(self.cashbox.id), "type": "expense", "name": "Прочее", "amount": "5.00", "payment_method": "cash"}
        # без смены и без открытой смены — нельзя
        self.assertDenied(self.api_cashier.post("/api/construction/cashflows/", body, format="json"), "can_view_cashbox")
        # чужая смена
        foreign = self.open_shift(self.other)
        self.assertDenied(
            self.api_cashier.post("/api/construction/cashflows/", {**body, "shift": str(foreign.id)}, format="json"),
            "can_view_cashbox",
        )
        # своя смена — ок
        own = self.open_shift()
        r = self.api_cashier.post("/api/construction/cashflows/", {**body, "shift": str(own.id)}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        # правка/удаление чужой операции (без смены) — нельзя
        owner_flow = self.api_owner.post("/api/construction/cashflows/", body, format="json")
        self.assertEqual(owner_flow.status_code, 201, owner_flow.content)
        fid = owner_flow.json()["id"]
        self.assertDenied(self.api_cashier.patch(f"/api/construction/cashflows/{fid}/", {"name": "Х"}, format="json"))
        self.assertDenied(self.api_cashier.delete(f"/api/construction/cashflows/{fid}/"))

    def test_cashflow_request_mode_keeps_employee_requests(self):
        """Режим «заявки»: расход сотрудника уходит на подтверждение — не блокируем."""
        self.company.cashflow_requests_enabled = True
        self.company.save()
        r = self.api_cashier.post("/api/construction/cashflows/", {
            "cashbox": str(self.cashbox.id), "type": "expense", "name": "Заявка", "amount": "7.00",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["status"], "pending")

    # --- аналитика ---
    def test_analytics_requires_flag(self):
        # Вкладка «Продажи» открыта кассиру (решение 25.10), остальная аналитика — с правом.
        self.assertEqual(self.api_cashier.get("/api/main/analytics/market/?tab=sales").status_code, 200)
        self.assertEqual(self.api_cashier.get("/api/main/analytics/market/").status_code, 200)
        for url in (
            "/api/main/analytics/market/?tab=products", "/api/main/analytics/market/summary/",
            "/api/main/analytics/finance/", "/api/main/analytics/pnl/", "/api/main/analytics/market/hours-sources/",
        ):
            self.assertDenied(self.api_cashier.get(url), "can_view_analytics")
            r = self.api_owner.get(url)
            self.assertNotEqual(r.status_code, 403, url)
        self.cashier.can_view_analytics = True
        self.cashier.save()
        r = self.api_for(self.cashier).get("/api/main/analytics/market/?tab=sales")
        self.assertEqual(r.status_code, 200, r.content)

    def test_app_settings_write_owner_admin_only(self):
        r = self.api_cashier.put("/api/main/app-settings/market/", {"data": {"a": 1}}, format="json")
        self.assertEqual(r.status_code, 403, r.content)
        r = self.api_cashier.get("/api/main/app-settings/market/")
        self.assertEqual(r.status_code, 200, r.content)

    # --- can_use_pos ---
    def test_can_use_pos_default_true_and_blocks_when_false(self):
        self.assertTrue(User.objects.get(pk=self.cashier.pk).can_use_pos)
        self.cashier.can_use_pos = False
        self.cashier.save()
        api = self.api_for(self.cashier)
        r = api.post("/api/construction/shifts/open/", {"cashbox": str(self.cashbox.id), "opening_cash": "1"}, format="json")
        self.assertDenied(r, "can_use_pos")
        shift = self.open_shift(self.owner)
        self.assertDenied(self.checkout(api, shift), "can_use_pos")


class AccessCodesTests(N16Base):
    def put(self, api, emp, action="discount", code="1234"):
        return api.put(f"/api/users/employees/{emp.id}/access-codes/{action}/", {"code": code}, format="json")

    def test_set_list_delete_owner_only_no_codes_leaked(self):
        self.assertDenied(self.put(self.api_cashier, self.admin))
        r = self.put(self.api_owner, self.admin, code="4321")
        self.assertEqual(r.status_code, 200, r.content)
        from apps.users.models import EmployeeAccessCode

        row = EmployeeAccessCode.objects.get(employee=self.admin, action="discount")
        self.assertNotIn("4321", row.code_hash)
        r = self.api_owner.get(f"/api/users/employees/{self.admin.id}/access-codes/")
        self.assertEqual(r.json()["actions"], ["discount"])
        self.assertNotIn("4321", r.content.decode())
        self.assertDenied(self.api_cashier.get(f"/api/users/employees/{self.admin.id}/access-codes/"))
        r = self.api_owner.delete(f"/api/users/employees/{self.admin.id}/access-codes/discount/")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(EmployeeAccessCode.objects.exists())

    def test_verify_ok_and_invalid(self):
        self.put(self.api_owner, self.admin, code="4321")
        r = self.api_cashier.post("/api/users/access-codes/verify/", {"action": "discount", "code": "4321"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["ok"], True)
        self.assertEqual(r.json()["employee"]["id"], str(self.admin.id))
        r = self.api_cashier.post("/api/users/access-codes/verify/", {"action": "discount", "code": "0000"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "code_invalid")
        r = self.api_cashier.post("/api/users/access-codes/verify/", {"action": "other", "code": "4321"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_verify_rate_limit_429_on_sixth_wrong(self):
        self.put(self.api_owner, self.admin, code="4321")
        codes = []
        for _ in range(6):
            r = self.api_cashier.post("/api/users/access-codes/verify/", {"action": "discount", "code": "9999"}, format="json")
            codes.append(r.status_code)
        self.assertEqual(codes, [400, 400, 400, 400, 400, 429])
        # даже верный код после блокировки не пускает
        r = self.api_cashier.post("/api/users/access-codes/verify/", {"action": "discount", "code": "4321"}, format="json")
        self.assertEqual(r.status_code, 429)
