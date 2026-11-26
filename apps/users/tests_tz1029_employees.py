"""ТЗ 29.10 Б-101: сотрудники и роли."""
from decimal import Decimal

from django.test import override_settings

from apps.construction.models import Cashbox, CashShift
from apps.main.models import ChangeEvent
from apps.users.models import Company, SubscriptionPlan, User
from apps.users.tests_tz_n16_permissions import LOCMEM, N16Base

NEW = {"HTTP_X_NURMARKET_CLIENT": "kassa/1.17.61"}


@override_settings(CACHES=LOCMEM)
class B101Tests(N16Base):
    def url(self, emp):
        return f"/api/users/employees/{emp.id}/"

    # 1. PATCH только присланных полей
    def test_patch_only_phone(self):
        self.cashier.can_view_clients = True
        self.cashier.phone_number = "+996555111222"
        self.cashier.track_number = "AB123"
        self.cashier.save()
        r = self.api_owner.patch(self.url(self.cashier), {"phone_number": "+996700123456"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        u = User.objects.get(pk=self.cashier.pk)
        self.assertEqual(u.phone_number, "+996700123456")
        self.assertEqual(u.track_number, "AB123")
        self.assertTrue(u.can_view_clients)
        self.assertTrue(u.can_view_dashboard)
        self.assertEqual(u.role, "salesperson")

    def test_empty_string_clears(self):
        self.cashier.phone_number = "+996555111222"
        self.cashier.track_number = "AB123"
        self.cashier.save()
        r = self.api_owner.patch(self.url(self.cashier), {"phone_number": "", "track_number": ""}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        u = User.objects.get(pk=self.cashier.pk)
        self.assertFalse(u.phone_number)
        self.assertFalse(u.track_number)

    def test_phone_validation_only_for_new_values(self):
        r = self.api_owner.patch(self.url(self.cashier), {"phone_number": "12ab"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        r = self.api_owner.patch(self.url(self.cashier), {"phone_number": "123"}, format="json")
        self.assertEqual(r.status_code, 400)
        # существующий плохой телефон остаётся, если его не меняют
        User.objects.filter(pk=self.cashier.pk).update(phone_number="12")
        r = self.api_owner.patch(self.url(self.cashier), {"phone_number": "12", "comment": "ok"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    # 2. can_use_pos
    def test_can_use_pos_in_employees_and_profile(self):
        r = self.api_owner.patch(self.url(self.cashier), {"can_use_pos": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIs(r.json()["can_use_pos"], False)
        rows = self.api_owner.get("/api/users/employees/").json()["results"]
        row = [x for x in rows if x["id"] == str(self.cashier.id)][0]
        self.assertIs(row["can_use_pos"], False)
        self.assertIs(self.api_for(User.objects.get(pk=self.cashier.pk)).get("/api/users/profile/").json()["can_use_pos"], False)
        api = self.api_for(User.objects.get(pk=self.cashier.pk))
        r = api.post("/api/construction/shifts/open/", {"cashbox": str(self.cashbox.id), "opening_cash": "1"}, format="json")
        self.assertDenied(r, "can_use_pos")
        self.assertDenied(self.checkout(api, self.open_shift(self.owner)), "can_use_pos")

    # 3. увольнение
    def test_dismiss_and_filter(self):
        r = self.api_owner.patch(self.url(self.cashier), {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        u = User.objects.get(pk=self.cashier.pk)
        self.assertFalse(u.is_active)
        self.assertIsNone(u.deleted_at)
        self.assertIsNotNone(u.fired_at)
        ids = lambda q: {x["id"] for x in self.api_owner.get("/api/users/employees/" + q).json()["results"]}
        cid = str(self.cashier.id)
        self.assertNotIn(cid, ids(""))
        self.assertNotIn(cid, ids("?is_active=true"))
        self.assertIn(cid, ids("?is_active=all"))
        self.assertEqual(ids("?is_active=false"), {cid})
        # вход закрыт
        r = self.client.post("/api/users/login/", {"email": self.cashier.email, "password": "x"}, content_type="application/json")
        self.assertNotEqual(r.status_code, 200)
        # возврат
        r = self.api_owner.patch(self.url(self.cashier), {"is_active": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        u.refresh_from_db()
        self.assertTrue(u.is_active)
        self.assertIsNone(u.fired_at)

    def test_dismiss_open_shift_409_strict_and_autoclose_old(self):
        self.open_shift(self.cashier)
        r = self.api_owner.patch(self.url(self.cashier), {"is_active": False}, format="json", **NEW)
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json()["code"], "open_shift")
        self.assertTrue(User.objects.get(pk=self.cashier.pk).is_active)
        r = self.api_owner.delete(self.url(self.cashier), **NEW)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "open_shift"))
        # старая касса — прежнее поведение: смена закрывается сама
        r = self.api_owner.patch(self.url(self.cashier), {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(CashShift.objects.filter(cashier=self.cashier, status=CashShift.Status.OPEN).exists())

    def test_last_admin_409(self):
        # владелец (owner) — второй админ; убираем сначала admin: есть владелец -> можно
        r = self.api_owner.patch(self.url(self.admin), {"role": "admin"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        # если владельцев/админов больше нет, кроме цели — 409
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        r = self.api_admin.get("/api/users/employees/lite/")  # админ жив
        self.assertEqual(r.status_code, 200)
        owner2 = User.objects.create_user(email="o2@t.kg", password="x", company=self.company, role="owner")
        User.objects.filter(pk=owner2.pk).update(is_active=False)
        api = self.api_for(User.objects.get(pk=self.owner.pk))
        User.objects.filter(pk=self.owner.pk).update(is_active=True)
        su = User.objects.create_user(email="su@t.kg", password="x", company=self.company, role="owner", is_superuser=True)
        r = self.api_for(su).patch(self.url(self.admin), {"is_active": False}, format="json", **NEW)
        # есть владелец self.owner -> разрешено
        self.assertEqual(r.status_code, 200, r.content)
        User.objects.filter(pk=self.admin.pk).update(is_active=True)
        User.objects.filter(pk__in=[self.owner.pk, su.pk]).update(is_active=False)
        r = self.api_for(su).patch(self.url(self.admin), {"is_active": False}, format="json", **NEW)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "last_admin"), r.content)

    # 4. доп. поля
    def test_card_fields_roundtrip(self):
        cb = Cashbox.objects.create(name="Вторая", company=self.company, role=Cashbox.CashboxRole.POS_BRANCH)
        body = {
            "hired_at": "2026-01-05", "fired_at": "2026-10-01", "birth_date": "1990-02-03", "address": "Бишкек",
            "emergency_contact": "Жена 0555", "comment": "ок", "language": "ky", "default_cashbox": str(cb.id),
        }
        r = self.api_owner.patch(self.url(self.cashier), body, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        for k, v in body.items():
            self.assertEqual(r.json()[k], v, k)
        r = self.api_owner.patch(self.url(self.cashier), {"comment": "x" * 501}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.api_owner.patch(self.url(self.cashier), {"language": "de"}, format="json")
        self.assertEqual(r.status_code, 400)
        # чужая касса
        other_owner = User.objects.create_user(email="oo@t.kg", password="x", role="owner")
        other = Company.objects.create(name="Other", owner=other_owner, is_active=True)
        foreign = Cashbox.objects.create(name="Чужая", company=other, role=Cashbox.CashboxRole.POS_MAIN)
        r = self.api_owner.patch(self.url(self.cashier), {"default_cashbox": str(foreign.id)}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        # null очищает
        r = self.api_owner.patch(self.url(self.cashier), {"default_cashbox": None, "language": None}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_profile_exposes_keys_and_language_only_writable(self):
        api = self.api_cashier
        r = api.patch("/api/users/profile/", {"language": "tr", "comment": "hack", "is_active": False, "hired_at": "2020-01-01"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        u = User.objects.get(pk=self.cashier.pk)
        self.assertEqual(u.language, "tr")
        self.assertTrue(u.is_active)
        self.assertFalse(u.comment)
        self.assertIsNone(u.hired_at)
        for k in ("is_active", "hired_at", "fired_at", "birth_date", "address", "emergency_contact", "comment", "default_cashbox"):
            self.assertIn(k, r.json())

    # 5. lite
    def test_lite(self):
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        r = self.api_cashier.get("/api/users/employees/lite/")
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.json()
        self.assertEqual({x["id"] for x in rows}, {str(self.owner.id), str(self.cashier.id)})
        for x in rows:
            self.assertEqual(set(x), {"id", "name", "role"})
        self.assertEqual(self.client.get("/api/users/employees/lite/").status_code, 401)

    # 6. лимит
    def test_limit_reached(self):
        plan = SubscriptionPlan.objects.create(name="Старт", price=Decimal("0"))
        self.company.subscription_plan = plan
        self.company.save()
        # owner + admin + cashier = 3 = лимит «Старт»
        body = {"email": "new@t.kg", "first_name": "Н", "last_name": "С", "role": "salesperson", "phone_number": "+996700000001"}
        r = self.api_owner.post("/api/users/employees/create/", body, format="json", **NEW)
        self.assertEqual(r.status_code, 403, r.content)
        j = r.json()
        self.assertEqual((j["code"], j["limit"], j["current"]), ("employee_limit_reached", 3, 3))
        # старая касса: прежний 400
        r = self.api_owner.post("/api/users/employees/create/", body, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        # возврат уволенного сверх лимита
        User.objects.filter(pk=self.cashier.pk).update(is_active=False)
        r = self.api_owner.post("/api/users/employees/create/", body, format="json", **NEW)
        self.assertEqual(r.status_code, 201, r.content)
        r = self.api_owner.patch(self.url(self.cashier), {"is_active": True}, format="json")
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(r.json()["code"], "employee_limit_reached")

    def test_page_size_200(self):
        User.objects.bulk_create([
            User(email=f"e{i}@t.kg", company=self.company, role="salesperson", first_name=f"E{i}") for i in range(150)
        ])
        r = self.api_owner.get("/api/users/employees/?page_size=200").json()
        self.assertEqual(r["count"], 153)
        self.assertEqual(len(r["results"]), 153)

    # 6. ошибки
    def test_permission_denied_required(self):
        r = self.api_cashier.patch(self.url(self.admin), {"comment": "x"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual((r.json()["code"], r.json()["required"]), ("permission_denied", "can_edit_employees"))
        r = self.api_cashier.post("/api/users/employees/create/", {"email": "z@t.kg"}, format="json")
        self.assertDenied(r, "can_edit_employees")
        r = self.api_cashier.delete(f"/api/users/employees/{self.admin.id}/delete/")
        self.assertDenied(r, "can_edit_employees")

    def test_foreign_id_404(self):
        oo = User.objects.create_user(email="oo2@t.kg", password="x", role="owner")
        oc = Company.objects.create(name="O2", owner=oo, is_active=True)
        stranger = User.objects.create_user(email="st@t.kg", password="x", company=oc, role="salesperson")
        r = self.api_owner.patch(self.url(stranger), {"comment": "x"}, format="json")
        self.assertEqual(r.status_code, 404)

    def test_create_email_unique_and_phone(self):
        body = {"email": self.cashier.email.upper(), "first_name": "A", "role": "salesperson"}
        r = self.api_owner.post("/api/users/employees/create/", body, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("email", r.json())
        r = self.api_owner.post("/api/users/employees/create/", {"email": "p@t.kg", "first_name": "A", "role": "salesperson", "phone_number": "12"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("phone_number", r.json())
        # уволенный сохраняет email и тоже блокирует повтор
        User.objects.filter(pk=self.cashier.pk).update(is_active=False)
        body["email"] = self.cashier.email
        r = self.api_owner.post("/api/users/employees/create/", body, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    # 7. лента
    def test_employee_changed_events(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.api_owner.patch(self.url(self.cashier), {"comment": "a"}, format="json")
            self.api_owner.patch(self.url(self.cashier), {"is_active": False}, format="json")
            self.api_owner.patch(self.url(self.cashier), {"is_active": True}, format="json")
            r = self.api_owner.post("/api/users/employees/create/", {"email": "ev@t.kg", "first_name": "E", "last_name": "L", "role": "salesperson"}, format="json")
            self.assertEqual(r.status_code, 201, r.content)
        acts = [e.data["action"] for e in ChangeEvent.objects.filter(company=self.company, type="employee_changed").order_by("id")]
        self.assertEqual(acts, ["updated", "dismissed", "restored", "created"])
