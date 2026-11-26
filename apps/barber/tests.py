from django.test import TestCase

# Create your tests here.


class ClientListAllAndQueriesTests(TestCase):
    """30.10.2026: page_size>=1000 отдаёт всех клиентов; нет запроса компании на каждую строку."""

    def test_all_clients_and_constant_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from rest_framework.test import APIClient
        from apps.barber.models import Client as BarberClient
        from apps.users.models import Company, User

        owner = User.objects.create_user(email="o_all@t.kg", password="p", role="owner")
        company = Company.objects.create(name="C", owner=owner)
        owner.company = company
        owner.save()
        BarberClient.objects.bulk_create([
            BarberClient(company=company, full_name=f"К{i:04d}", phone=f"+99670{i:07d}") for i in range(1203)
        ])
        api = APIClient()
        api.force_authenticate(owner)
        with CaptureQueriesContext(connection) as c:
            r = api.get("/api/barbershop/clients/", {"page_size": 1000, "ordering": "full_name"})
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.json()["results"] if isinstance(r.json(), dict) else r.json()
        self.assertEqual(len(rows), 1203)
        self.assertLess(len(c.captured_queries), 15)
        r = api.get("/api/barbershop/clients/", {"page_size": 50})
        self.assertEqual(len(r.json()["results"]), 50)
