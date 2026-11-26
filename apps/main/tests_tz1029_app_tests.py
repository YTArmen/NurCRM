from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import Company

User = get_user_model()
URL = "/api/main/app-tests/checks/"


def mk(email, slug):
    owner = User.objects.create_user(email=email, password="pass12345")
    company = Company.objects.create(name=slug, slug=slug, owner=owner)
    staff = User.objects.create_user(email="s" + email, password="pass12345", company=company, role="cashier") \
        if "company" in [f.name for f in User._meta.fields] else owner
    return owner, staff


class AppTestChecksTests(TestCase):
    def cl(self, u):
        c = APIClient()
        c.force_authenticate(u)
        return c

    def post(self, c, **kw):
        body = {"version": "1.18.8", "item_id": "f1", "device_id": "aaaaaaa1", "status": "ok"}
        body.update(kw)
        return c.post(URL, body, format="json", secure=True)

    def test_flow(self):
        owner, staff = mk("o1@t.com", "ac1")
        c1, c2 = self.cl(owner), self.cl(staff)
        self.assertEqual(self.post(c1, name="Тел1").status_code, 200)
        self.assertEqual(self.post(c2, device_id="bbbbbbb2").status_code, 200)
        self.post(c2, device_id="bbbbbbb2")  # идемпотентно
        d = c2.get(URL + "?version=1.18.8", secure=True).json()
        self.assertEqual((d["f1"]["ok_count"], d["f1"]["issue_count"], len(d["f1"]["devices"])), (2, 0, 2))
        self.post(c2, device_id="bbbbbbb2", status="issue", comment="ломается")
        d = c1.get(URL + "?version=1.18.8", secure=True).json()
        self.assertEqual((d["f1"]["ok_count"], d["f1"]["issue_count"]), (1, 1))
        self.assertEqual(c1.get(URL + "?version=1.0", secure=True).json(), {})
        self.post(c2, device_id="bbbbbbb2", status="none")
        d = c1.get(URL + "?version=1.18.8", secure=True).json()
        self.assertEqual((d["f1"]["ok_count"], d["f1"]["issue_count"]), (1, 0))

    def test_company_isolation_and_validation(self):
        o1, _ = mk("o2@t.com", "ac2")
        o2, _ = mk("o3@t.com", "ac3")
        self.post(self.cl(o1))
        self.assertEqual(self.cl(o2).get(URL + "?version=1.18.8", secure=True).json(), {})
        self.assertEqual(self.post(self.cl(o1), device_id="short").status_code, 400)
        self.assertEqual(self.post(self.cl(o1), status="bad").status_code, 400)
