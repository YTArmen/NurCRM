"""Б-46 (27.10.2026): GET products/list/?ordering=-updated_at — избранные не мешают сортировке."""
from decimal import Decimal
import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from apps.main.models import Product, ProductFavorite
from apps.users.models import Company, User


class ProductOrderingUpdatedAtTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.company = Company.objects.create(name="Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        self.fav = Product.objects.create(company=self.company, name="Aos", price=Decimal("10"))
        self.other = Product.objects.create(company=self.company, name="Тест", price=Decimal("5"))
        ProductFavorite.objects.create(company=self.company, product=self.fav)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def first(self, ordering):
        r = self.api.get("/api/main/products/list/", {"ordering": ordering, "page_size": 1})
        self.assertEqual(r.status_code, 200, getattr(r, "data", r.content))
        return r.data["results"][0]["id"]

    def test_price_change_moves_product_first(self):
        self.other.price = Decimal("4")
        self.other.save(update_fields=["price"])
        self.assertEqual(self.first("-updated_at"), str(self.other.id))
        self.fav.price = Decimal("11")
        self.fav.save(update_fields=["price"])
        self.assertEqual(self.first("-updated_at"), str(self.fav.id))
        self.assertEqual(self.first("updated_at"), str(self.other.id))
