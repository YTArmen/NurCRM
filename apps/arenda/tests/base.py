from datetime import date

from rest_framework.test import APIClient, APITestCase

from apps.users.models import Branch, Company, User


def make_user(email, company=None, **kw):
    return User.objects.create_user(email=email, password="pass12345", first_name="Имя", last_name="Фам",
                                    company=company, **kw)


class ArendaBase(APITestCase):
    def setUp(self):
        self.owner = make_user("owner@a.kg")
        self.company = Company.objects.create(name="ТЦ Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        self.branch = Branch.objects.create(company=self.company, name="ТЦ Центр")
        self.branch2 = Branch.objects.create(company=self.company, name="ТЦ Запад")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def staff(self, email="s@a.kg"):
        u = make_user(email, self.company)
        c = APIClient()
        c.force_authenticate(u)
        return u, c

    def unit(self, number="101", floor=1, area="10.00", rate=100000, branch=None, client=None):
        r = (client or self.client).post("/api/arenda/units/", {
            "branch": str((branch or self.branch).id), "floor": floor, "number": number, "area": area, "rate": rate,
        }, format="json")
        assert r.status_code == 201, r.data
        return r.data

    def tenant(self, name="ИП Асанов"):
        r = self.client.post("/api/arenda/tenants/", {"name": name, "phone": "+996700111222"}, format="json")
        assert r.status_code == 201, r.data
        return r.data

    def lease(self, tenant, units, start="2026-01-01", end=None, **kw):
        body = {"tenant": tenant["id"], "units": [u["id"] for u in units], "start_date": start, "end_date": end,
                "pay_day": 10, **kw}
        return self.client.post("/api/arenda/leases/", body, format="json")
