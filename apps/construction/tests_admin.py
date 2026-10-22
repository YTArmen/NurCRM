"""Тесты ТЗ «Админка»: права, журнал, корзина, PIN, аналитика."""
import importlib

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model

from apps.construction.tests_buhgalter import BuhBase

User = get_user_model()


class EditProductsRightTests(BuhBase):
    def _patch(self, user):
        self.client.force_authenticate(user)
        return self.client.patch("/api/main/products/00000000-0000-0000-0000-000000000000/", {"name": "x"}, format="json")

    def test_false_blocks_patch_delete_bulk(self):
        self.acc.can_view_products = True
        self.acc.can_edit_products = False
        self.acc.save()
        self.client.force_authenticate(self.acc)
        for method, url in (
            ("patch", "/api/main/products/00000000-0000-0000-0000-000000000000/"),
            ("delete", "/api/main/products/00000000-0000-0000-0000-000000000000/"),
            ("post", "/api/main/products/bulk-update/"),
            ("post", "/api/main/products/bulk-delete/"),
        ):
            r = getattr(self.client, method)(url, {}, format="json")
            self.assertEqual(r.status_code, 403, url)
            self.assertEqual(r.data.get("code"), "read_only")

    def test_owner_always_allowed(self):
        self.owner.can_edit_products = False
        self.owner.save()
        r = self._patch(self.owner)
        self.assertEqual(r.status_code, 404)  # дошёл до поиска товара, не 403

    def test_backfill_migration(self):
        mod = importlib.import_module("apps.users.migrations.0063_can_edit_products_backfill")
        self.acc.can_view_products = True
        self.acc.save()
        other = User.objects.create_user(email="n@t.com", password="p", role="manager")
        other.company = self.company
        other.can_view_products = False
        other.save()
        mod.backfill(django_apps, None)
        self.acc.refresh_from_db()
        other.refresh_from_db()
        self.assertIs(self.acc.can_edit_products, True)
        self.assertIsNone(other.can_edit_products)
