from unittest import mock

from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient

from apps.arenda.models import ArendaAccess, AuditLog, Employee, Reminder
from apps.users.models import Company, User

from .base import ArendaBase, make_user

LOGIN = "/api/users/auth/login/"


class AccessBase(ArendaBase):
    def employee(self, name="Айбек Кассиров", phone="0700 123 456"):
        return Employee.objects.create(company=self.company, branch=self.branch, full_name=name, phone=phone)

    def grant(self, emp, **body):
        return self.client.post(f"/api/arenda/employees/{emp.id}/access/", body, format="json")

    def login(self, email, password):
        c = APIClient()
        r = c.post(LOGIN, {"email": email, "password": password}, format="json")
        if r.status_code == 200:
            c.credentials(HTTP_AUTHORIZATION="Bearer " + r.data["access"])
        return c, r


class StaffAccessTests(AccessBase):
    def test_first_access_creates_user_with_temp_password(self):
        e = self.employee()
        r = self.grant(e, preset="cashier")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["sections"], ["tenants", "cash"])
        self.assertEqual(r.data["login"], "+996700123456")
        pw = r.data["temp_password"]
        self.assertGreaterEqual(len(pw), 8)
        self.assertTrue(any(ch.isdigit() for ch in pw))
        self.assertEqual(r.data["invite"]["status"], "not_sent")
        # заглушка SMS есть, но пароля в тексте нет
        rem = Reminder.objects.get(company=self.company, rule_kind="staff_invite")
        self.assertEqual(rem.status, "not_sent")
        self.assertNotIn(pw, rem.text)
        self.assertIn("+996700123456", rem.text)
        # пароль не лежит в журнале
        for a in AuditLog.objects.filter(company=self.company):
            self.assertNotIn(pw, str(a.before) + str(a.after))
        # повторное сохранение доступа пароль не меняет и не показывает
        r2 = self.grant(e, preset="cashier")
        self.assertNotIn("temp_password", r2.data)
        e.refresh_from_db()
        self.assertTrue(e.user.check_password(pw))

    def test_cashier_sees_only_tenants_and_cash_analytics_403(self):
        e = self.employee()
        pw = self.grant(e, preset="cashier").data["temp_password"]
        e.refresh_from_db()
        c, r = self.login(e.user.email, pw)
        self.assertEqual(r.status_code, 200)
        me = c.get("/api/arenda/me/").data
        self.assertEqual(me["sections"], ["tenants", "cash"])
        self.assertFalse(me["is_owner"])
        self.assertTrue(me["must_change_password"])
        r = c.get("/api/arenda/analytics/?tab=overview")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data["section"], "analytics")
        self.assertEqual(c.get("/api/arenda/tenants/").status_code, 200)
        self.assertEqual(c.get("/api/arenda/notifications/summary/").status_code, 403)
        # выдавать доступы сотрудник не может
        self.assertEqual(c.get("/api/arenda/audit/").status_code, 403)
        self.assertEqual(c.post(f"/api/arenda/employees/{e.id}/access/", {"preset": "manager"}, format="json").status_code, 403)

    def test_phone_login_endpoint(self):
        e = self.employee()
        pw = self.grant(e, preset="cashier").data["temp_password"]
        r = APIClient().post("/api/arenda/auth/login/", {"login": "+996 700 123 456", "password": pw}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("access", r.data)

    def test_no_access_for_employee_without_account_does_not_create_user(self):
        e = self.employee()
        r = self.grant(e, preset="none")
        self.assertEqual(r.status_code, 200)
        e.refresh_from_db()
        self.assertIsNone(e.user)

    def test_phone_required(self):
        e = self.employee(phone="")
        r = self.grant(e, preset="cashier")
        self.assertEqual(r.status_code, 400)

    def test_uncheck_all_closes_login_and_ends_sessions(self):
        e = self.employee()
        pw = self.grant(e, preset="cashier").data["temp_password"]
        e.refresh_from_db()
        c, _ = self.login(e.user.email, pw)
        self.assertEqual(c.get("/api/arenda/me/").status_code, 200)
        r = self.grant(e, sections=[])
        self.assertFalse(r.data["is_active"])
        self.assertEqual(c.get("/api/arenda/me/").status_code, 401)  # JWT уже недействителен (user.is_active=False)
        c2, r2 = self.login(e.user.email, pw)
        self.assertEqual(r2.status_code, 403)  # access_closed
        # снова включили: логин работает, старый токен мёртв
        r = self.grant(e, preset="admin")
        self.assertTrue(r.data["is_active"])
        self.assertEqual(c.get("/api/arenda/me/").status_code, 401)
        c3, r3 = self.login(e.user.email, pw)
        self.assertEqual(r3.status_code, 200)

    def test_old_put_access_also_revokes(self):
        e = self.employee()
        pw = self.grant(e, preset="cashier").data["temp_password"]
        e.refresh_from_db()
        c, _ = self.login(e.user.email, pw)
        r = self.client.put(f"/api/arenda/access/{e.user.id}/", {"preset": "none"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/arenda/me/").status_code, 401)

    def test_reset_password_new_temp(self):
        e = self.employee()
        pw = self.grant(e, preset="cashier").data["temp_password"]
        e.refresh_from_db()
        c, _ = self.login(e.user.email, pw)
        r = self.client.post(f"/api/arenda/access/{e.user.id}/invite/")
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.data["temp_password"], pw)
        self.assertEqual(c.get("/api/arenda/me/").status_code, 401)
        self.assertEqual(self.login(e.user.email, pw)[1].status_code, 401)
        self.assertEqual(self.login(e.user.email, r.data["temp_password"])[1].status_code, 200)

    def test_view_as_employee(self):
        e = self.employee()
        self.grant(e, preset="cashier")
        e.refresh_from_db()
        r = self.client.get(f"/api/arenda/me/?as_user={e.user.id}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["view_as"])
        self.assertEqual(r.data["sections"], ["tenants", "cash"])
        self.assertFalse(r.data["is_owner"])
        self.assertEqual(r.data["user"]["id"], str(e.user.id))
        # сам owner остаётся собой
        self.assertTrue(self.client.get("/api/arenda/me/").data["is_owner"])
        # не владелец — нельзя
        c = APIClient()
        c.force_authenticate(e.user)
        self.assertEqual(c.get(f"/api/arenda/me/?as_user={e.user.id}").status_code, 403)
        # чужой/несуществующий
        self.assertEqual(self.client.get(f"/api/arenda/me/?as_user={self.owner.id}").status_code, 404)

    def test_branch_restricted_view_as(self):
        e = self.employee()
        self.grant(e, preset="admin", all_branches=False, branches=[str(self.branch.id)])
        e.refresh_from_db()
        r = self.client.get(f"/api/arenda/me/?as_user={e.user.id}")
        self.assertFalse(r.data["all_branches"])
        self.assertEqual([b["id"] for b in r.data["branches"]], [str(self.branch.id)])

    def test_audit_log_owner_only_and_filters(self):
        e = self.employee()
        self.grant(e, preset="cashier")
        e.refresh_from_db()
        r = self.client.get("/api/arenda/audit/?entity=access")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)
        row = r.data["results"][0]
        self.assertEqual(row["action"], "access")
        self.assertEqual(row["after"]["sections"], ["tenants", "cash"])
        self.assertEqual(row["user"], str(self.owner.id))
        self.assertEqual(self.client.get("/api/arenda/audit/?entity=unit").data["count"], 0)
        self.assertEqual(self.client.get(f"/api/arenda/audit/?user={e.user.id}").data["count"], 0)
        today = timezone.localdate()
        self.assertEqual(self.client.get(f"/api/arenda/audit/?date_from={today}&date_to={today}").data["count"], 1)
        tomorrow = today + timedelta(days=1)
        self.assertEqual(self.client.get(f"/api/arenda/audit/?date_from={tomorrow}").data["count"], 0)
        self.assertEqual(self.client.get("/api/arenda/audit/?date_from=xx").status_code, 400)
        _, cl = self.staff("m@a.kg")
        self.assertEqual(cl.get("/api/arenda/audit/").status_code, 403)


class PasswordAndLockoutTests(AccessBase):
    def setUp(self):
        super().setUp()
        self.emp = self.employee()
        self.temp = self.grant(self.emp, preset="admin").data["temp_password"]
        self.emp.refresh_from_db()
        self.email = self.emp.user.email

    def test_password_change_ends_other_sessions(self):
        c1, _ = self.login(self.email, self.temp)  # сеанс 1 (меняет пароль)
        c2, r2 = self.login(self.email, self.temp)  # сеанс 2 (другое устройство)
        refresh2 = r2.data["refresh"]
        self.assertEqual(c2.get("/api/arenda/me/").status_code, 200)
        r = c1.post("/api/arenda/me/password/", {"current": self.temp, "new": "NewPass123", "repeat": "NewPass123"},
                    format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("password_changed_at", r.data)
        # старый и «другой» сеанс мертвы, в том числе через refresh
        self.assertEqual(c1.get("/api/arenda/me/").status_code, 401)
        self.assertEqual(c2.get("/api/arenda/me/").status_code, 401)
        rr = APIClient().post("/api/users/auth/refresh/", {"refresh": refresh2}, format="json")
        if rr.status_code == 200:
            c = APIClient()
            c.credentials(HTTP_AUTHORIZATION="Bearer " + rr.data["access"])
            self.assertEqual(c.get("/api/arenda/me/").status_code, 401)
        # токены из ответа работают у текущего сеанса
        cn = APIClient()
        cn.credentials(HTTP_AUTHORIZATION="Bearer " + r.data["access"])
        me = cn.get("/api/arenda/me/")
        self.assertEqual(me.status_code, 200)
        self.assertFalse(me.data["must_change_password"])
        self.assertIsNotNone(me.data["password_changed_at"])
        # refresh из ответа даёт рабочий access
        rr = APIClient().post("/api/users/auth/refresh/", {"refresh": r.data["refresh"]}, format="json")
        self.assertEqual(rr.status_code, 200)
        cr = APIClient()
        cr.credentials(HTTP_AUTHORIZATION="Bearer " + rr.data["access"])
        self.assertEqual(cr.get("/api/arenda/me/").status_code, 200)
        # старый пароль не подходит, новый подходит (новый вход после смены)
        self.assertEqual(self.login(self.email, self.temp)[1].status_code, 401)
        c3, r3 = self.login(self.email, "NewPass123")
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(c3.get("/api/arenda/me/").status_code, 200)
        self.assertEqual(AuditLog.objects.filter(object_type="user", action="password").count(), 1)

    def test_password_rules(self):
        c, _ = self.login(self.email, self.temp)
        post = lambda cur, new, **kw: c.post("/api/arenda/me/password/", {"current": cur, "new": new, **kw}, format="json")
        self.assertEqual(post("wrong", "NewPass123").status_code, 400)
        r = post(self.temp, "short1")
        self.assertEqual(r.status_code, 400)
        self.assertIn("new", r.data["fields"] if "fields" in r.data else r.data)
        self.assertEqual(post(self.temp, "nodigitshere").status_code, 400)
        self.assertEqual(post(self.temp, "NewPass123", repeat="Other1234").status_code, 400)
        self.assertEqual(post(self.temp, self.temp).status_code, 400)
        # ничего не изменилось
        self.assertEqual(self.login(self.email, self.temp)[1].status_code, 200)

    def test_lockout_after_five_failures_15_minutes(self):
        for _ in range(5):
            self.assertEqual(self.login(self.email, "bad")[1].status_code, 401)
        r = self.login(self.email, self.temp)[1]  # правильный пароль, но заблокирован
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.data["code"], "account_locked")
        self.assertGreater(r.data["retry_after"], 800)
        later = timezone.now() + timedelta(minutes=16)
        with mock.patch("apps.arenda.security.now", return_value=later):
            self.assertEqual(self.login(self.email, self.temp)[1].status_code, 200)

    def test_success_resets_counter(self):
        for _ in range(4):
            self.login(self.email, "bad")
        self.assertEqual(self.login(self.email, self.temp)[1].status_code, 200)
        for _ in range(4):
            self.login(self.email, "bad")
        self.assertEqual(self.login(self.email, self.temp)[1].status_code, 200)

    def test_lockout_not_applied_to_other_spheres(self):
        other_owner = make_user("o@other.kg")
        co = Company.objects.create(name="Магазин", owner=other_owner)
        other_owner.company = co
        other_owner.save()
        for _ in range(7):
            r = self.login("o@other.kg", "bad")[1]
            self.assertEqual(r.status_code, 401)
        self.assertEqual(self.login("o@other.kg", "pass12345")[1].status_code, 200)

    def test_lockout_applies_to_arenda_owner(self):
        self.client.get("/api/arenda/me/")  # компания начала пользоваться модулем (ArendaSettings создан)
        for _ in range(5):
            self.login("owner@a.kg", "bad")
        self.assertEqual(self.login("owner@a.kg", "pass12345")[1].status_code, 429)

    def test_arenda_login_endpoint_lockout(self):
        for _ in range(5):
            APIClient().post("/api/arenda/auth/login/", {"login": "+996700123456", "password": "bad"}, format="json")
        r = APIClient().post("/api/arenda/auth/login/", {"login": "+996700123456", "password": self.temp}, format="json")
        self.assertEqual(r.status_code, 429)
