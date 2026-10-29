from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.main.models import Product, ProductCategory
from apps.users.models import Company

User = get_user_model()


class ShowcaseCategoryKeysTests(TestCase):
    """ТЗ 29.10 Ф-3: category и category_title есть у каждой позиции витрины."""

    def test_keys_always_present(self):
        owner = User.objects.create_user(email="f3@test.com", password="pass12345")
        company = Company.objects.create(name="F3", slug="f3-shop", owner=owner)
        cat = ProductCategory.objects.create(company=company, name="Обувь")
        Product.objects.create(company=company, name="С", price=Decimal("10"), quantity=Decimal("5"), category=cat)
        Product.objects.create(company=company, name="Без", price=Decimal("10"), quantity=Decimal("5"))
        resp = APIClient().get("/api/main/public/companies/f3-shop/showcase/", secure=True)
        self.assertEqual(resp.status_code, 200)
        rows = {r["name"]: r for r in resp.json()["results"]}
        self.assertEqual(rows["Без"]["category"], None)
        self.assertEqual(rows["Без"]["category_title"], "")
        self.assertEqual(rows["С"]["category_title"], "Обувь")
        self.assertEqual(rows["С"]["category"], str(cat.id))
