# Тесты куска staff: python manage.py test apps.main.tz26.staff --settings=core.settings_test_sqlite
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.main.models import Task
from apps.users.models import Company

User = get_user_model()
NEW = {"HTTP_X_NURMARKET_CLIENT": "mobile/1.18.5"}


def make_company():
    s = uuid.uuid4().hex[:6]
    owner = User.objects.create_user(email=f"o{s}@t.kg", password="p12345678", role="owner")
    company = Company.objects.create(name=f"ST {s}", owner=owner)
    owner.company = company
    owner.save()
    cashier = User.objects.create_user(email=f"c{s}@t.kg", password="p12345678", role="cashier", company=company)
    return owner, company, cashier


def client_for(user, **extra):
    c = APIClient(**extra)
    c.force_authenticate(user)
    return c


class TaskTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner, self.company, self.cashier = make_company()
        self.other = User.objects.create_user(email=f"x{uuid.uuid4().hex[:6]}@t.kg", password="p12345678", role="cashier", company=self.company)
        self.api = client_for(self.owner, **NEW)
        self.cash_api = client_for(self.cashier, **NEW)

    def _create(self, **over):
        data = {"title": "Пересчитать полку", "due_date": (timezone.now() + timedelta(days=1)).isoformat(),
                "assigned_to": str(self.cashier.id), "priority": "high"}
        data.update(over)
        return self.api.post("/main/tasks/", data, format="json")

    @mock.patch("apps.main.tz26.staff.notify.send_telegram", return_value=True)
    def test_create_fields_and_notify(self, send):
        remind = timezone.now() + timedelta(hours=2)
        with self.captureOnCommitCallbacks(execute=True):
            r = self._create(remind_at=remind.isoformat())
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["priority"], "high")
        self.assertEqual(r.data["created_by"]["id"], str(self.owner.id))
        self.assertEqual(r.data["source"], "mobile")
        self.assertIsNone(r.data["completed_at"])
        self.assertEqual(r.data["report_files"], [])
        self.assertEqual(send.call_count, 1)  # при создании — сразу; напоминание ещё не наступило
        self.assertEqual(Task.objects.get(pk=r.data["id"]).created_by_id, self.owner.id)

    @mock.patch("apps.main.tz26.staff.notify.send_telegram", return_value=True)
    def test_reminder_task(self, send):
        from apps.main.tz26.staff.tasks import send_due_task_reminders

        r = self._create(remind_at=(timezone.now() - timedelta(minutes=1)).isoformat())
        self.assertEqual(r.status_code, 201, r.data)
        send.reset_mock()
        self.assertEqual(send_due_task_reminders()["reminded"], 1)
        self.assertEqual(send.call_count, 1)
        self.assertIsNotNone(Task.objects.get(pk=r.data["id"]).reminded_at)
        self.assertEqual(send_due_task_reminders()["reminded"], 0)  # повторно не шлём
        self.assertEqual(send.call_count, 1)

    def test_idempotency(self):
        h = {"HTTP_IDEMPOTENCY_KEY": "a" * 32}
        body = {"title": "Т", "due_date": (timezone.now() + timedelta(days=1)).isoformat(), "assigned_to": str(self.cashier.id)}
        r1 = self.api.post("/main/tasks/", body, format="json", **h)
        r2 = self.api.post("/main/tasks/", body, format="json", **h)
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertEqual(r2.data["id"], r1.data["id"])
        self.assertTrue(r2.data["replayed"])
        self.assertEqual(Task.objects.count(), 1)

    def test_filters_and_me(self):
        past = (timezone.now() - timedelta(days=3)).isoformat()
        a = self._create(title="просроч", due_date=past).data["id"]
        b = self._create(title="будущая", priority="low").data["id"]
        c = self._create(title="чужая", assigned_to=str(self.other.id)).data["id"]
        ids = lambda r: {x["id"] for x in r.data["results"]}
        self.assertEqual(ids(self.cash_api.get("/main/tasks/?assigned_to=me")), {a, b})
        self.assertEqual(ids(self.api.get(f"/main/tasks/?assigned_to={self.other.id}")), {c})
        self.assertEqual(ids(self.api.get("/main/tasks/?overdue=true")), {a})
        self.assertEqual(ids(self.api.get("/main/tasks/?priority=low")), {b})
        self.assertEqual(ids(self.api.get("/main/tasks/?status=done")), set())
        today = timezone.localdate().isoformat()
        self.assertEqual(ids(self.api.get(f"/main/tasks/?due_from={today}&due_to={today}")), set())
        r = self.cash_api.get("/main/tasks/?assigned_to=me&ordering=due_date")
        self.assertEqual([x["id"] for x in r.data["results"]], [a, b])

    def test_assignee_rights_and_done(self):
        tid = self._create().data["id"]
        r = self.cash_api.patch(f"/main/tasks/{tid}/", {"due_date": (timezone.now() + timedelta(days=9)).isoformat()}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (403, "only_status_for_assignee"))
        r = self.cash_api.patch(f"/main/tasks/{tid}/", {"status": "done", "report": "Готово"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["report"], "Готово")
        self.assertIsNotNone(r.data["completed_at"])
        r = self.cash_api.patch(f"/main/tasks/{tid}/", {"status": "pending"}, format="json")
        self.assertIsNone(r.data["completed_at"])
        self.assertEqual(client_for(self.other, **NEW).patch(f"/main/tasks/{tid}/", {"status": "done"}, format="json").status_code, 403)
        r = self.cash_api.post("/main/tasks/", {"title": "x", "due_date": timezone.now().isoformat()}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (403, "permission_denied"))
        self.assertEqual(self.cash_api.delete(f"/main/tasks/{tid}/").status_code, 403)

    def test_old_client_not_restricted(self):
        tid = self._create().data["id"]
        r = client_for(self.cashier).patch(f"/main/tasks/{tid}/", {"title": "Новое"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_manager_by_flag(self):
        self.cashier.can_view_employees = True
        self.cashier.save()
        r = self.cash_api.post("/main/tasks/", {"title": "x", "due_date": timezone.now().isoformat(),
                                                "assigned_to": str(self.other.id)}, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_report_files(self):
        tid = self._create().data["id"]
        f = SimpleUploadedFile("p.jpg", b"\xff\xd8\xff", content_type="image/jpeg")
        r = self.cash_api.post(f"/main/tasks/{tid}/files/", {"file": f}, format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(r.data), 1)
        self.assertEqual(len(self.api.get(f"/main/tasks/{tid}/").data["report_files"]), 1)
        bad = SimpleUploadedFile("x.exe", b"MZ")
        self.assertEqual(self.cash_api.post(f"/main/tasks/{tid}/files/", {"file": bad}, format="multipart").data["code"], "file_type_invalid")
        fid = r.data[0]["id"]
        self.assertEqual(self.cash_api.delete(f"/main/tasks/{tid}/files/{fid}/").status_code, 204)

    def test_summary(self):
        now = timezone.now()
        mk = lambda who, st, due, comp=None: Task.objects.create(
            company=self.company, title="t", due_date=due, status=st, assigned_to=who, completed_at=comp)
        mk(self.cashier, "done", now - timedelta(days=2), now - timedelta(days=3))   # вовремя
        mk(self.cashier, "done", now - timedelta(days=2), now - timedelta(days=1))   # с опозданием
        mk(self.cashier, "pending", now - timedelta(days=2))                           # просрочена
        mk(self.cashier, "in_progress", now + timedelta(days=2))
        mk(self.other, "pending", now + timedelta(days=1))
        r = self.api.get("/main/tasks/summary/")
        self.assertEqual(r.status_code, 200, r.data)
        row = next(x for x in r.data["results"] if x["assigned_to"] == str(self.cashier.id))
        self.assertEqual({k: row[k] for k in ("total", "done", "in_progress", "pending", "overdue", "done_on_time")},
                         {"total": 4, "done": 2, "in_progress": 1, "pending": 1, "overdue": 1, "done_on_time": 1})
        self.assertEqual(r.data["totals"]["total"], 5)
        r = self.cash_api.get("/main/tasks/summary/")
        self.assertEqual([x["assigned_to"] for x in r.data["results"]], [str(self.cashier.id)])
