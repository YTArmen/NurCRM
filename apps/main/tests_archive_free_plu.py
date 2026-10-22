"""
Удаление товара с историей переводит его в архив и освобождает PLU:
номер сразу можно отдать другому товару.
"""
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.main.models import Product
from apps.users.models import Company

User = get_user_model()

_HAS_HISTORY = property(lambda self: SimpleNamespace(exists=lambda: True))


class ArchiveFreesPluTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="plu-owner@test.com", password="x", role="owner")
        self.company = Company.objects.create(name="PLU Co", owner=self.owner)

    def test_archived_product_releases_plu(self):
        old = Product.objects.create(company=self.company, name="Тоок эти", is_weight=True, plu=2)
        with mock.patch.object(Product, "sale_items", _HAS_HISTORY):
            old.delete()

        old.refresh_from_db()
        self.assertEqual(old.status, Product.Status.ARCHIVED)
        self.assertIsNone(old.plu)

        new = Product.objects.create(company=self.company, name="Бука", is_weight=True, plu=2)
        self.assertEqual(new.plu, 2)

    def test_product_without_history_is_deleted(self):
        p = Product.objects.create(company=self.company, name="Баранина", is_weight=True, plu=5)
        p.delete()
        self.assertFalse(Product.objects.filter(pk=p.pk).exists())
