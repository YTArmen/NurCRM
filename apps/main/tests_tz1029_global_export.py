import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.main.models import GlobalBrand, GlobalCategory, GlobalProduct
from apps.users.models import Company

User = get_user_model()


class GlobalExportTests(TestCase):
    def setUp(self):
        u = User.objects.create_user(email="gx@test.com", password="pass12345")
        Company.objects.create(name="GX", slug="gx", owner=u)
        self.c = APIClient()
        self.c.force_authenticate(u)
        b = GlobalBrand.objects.create(name="Бренд")
        cat = GlobalCategory.objects.create(name="Кат")
        self.g1 = GlobalProduct.objects.create(name="Один", barcode="111", brand=b, category=cat)
        self.g2 = GlobalProduct.objects.create(name="Два", barcode="222")

    def test_all_paged(self):
        r = self.c.get("/api/main/global-products/export/?page_size=1", secure=True)
        self.assertEqual(r.status_code, 200, r.content[:300])
        d = r.json()
        self.assertEqual(d["count"], 2)
        self.assertTrue(d["next"])
        self.assertEqual(set(d["results"][0]), {"barcode", "name", "brand", "category", "updated_at"})

    def test_since(self):
        before = (timezone.now() - datetime.timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        GlobalProduct.objects.filter(pk=self.g1.pk).update(updated_at=timezone.now() - datetime.timedelta(days=5))
        GlobalProduct.objects.filter(pk=self.g2.pk).update(updated_at=timezone.now() - datetime.timedelta(days=5))
        g = GlobalProduct.objects.get(pk=self.g1.pk)
        g.name = "Новое"
        g.save()
        r = self.c.get("/api/main/global-products/export/", {"since": before}, secure=True)
        self.assertEqual([x["name"] for x in r.json()["results"]], ["Новое"])
        r = self.c.get("/api/main/global-products/export/", {"since": timezone.now().isoformat()}, secure=True)
        self.assertEqual(r.json()["count"], 0)

    def test_auth_required(self):
        self.assertIn(APIClient().get("/api/main/global-products/export/", secure=True).status_code, (401, 403))
