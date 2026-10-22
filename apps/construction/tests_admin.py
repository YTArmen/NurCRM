"""Тесты ТЗ «Админка»: права, журнал, корзина, PIN, аналитика."""
import importlib

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model

from apps.construction.tests_buhgalter import BuhBase
from apps.users.models import Company

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


class CashboxBranchTests(BuhBase):
    def test_cashboxes_return_branch(self):
        from apps.construction.models import Cashbox
        from apps.users.models import Branch

        br = Branch.objects.create(company=self.company, name="Фил")
        bx = Cashbox.objects.create(name="Моя касса фил", company=self.company, branch=br, role=Cashbox.CashboxRole.POS_BRANCH)
        self.client.force_authenticate(self.owner)
        r = self.client.get("/api/construction/cashboxes/")
        self.assertEqual(r.status_code, 200)
        rows = r.data["results"] if isinstance(r.data, dict) else r.data
        by_name = {x["name"]: x for x in rows}
        self.assertEqual(str(by_name["Моя касса фил"]["branch"]), str(br.id))
        self.assertIn("branch", by_name["Касса"])
        self.assertIsNone(by_name["Касса"]["branch"])


class PayProfileBranchTests(BuhBase):
    def test_branch_writable_and_returned(self):
        from apps.users.models import Branch

        br = Branch.objects.create(company=self.company, name="Ф1")
        o2 = User.objects.create_user(email="z@t.com", password="p", role="owner")
        c2 = Company.objects.create(name="Чужая", owner=o2)
        other = Branch.objects.create(company=c2, name="Ф2")
        self.client.force_authenticate(self.owner)
        url = "/api/main/market-sale-employee-pay-profiles/"
        r = self.client.post(url, {"user": str(self.acc.id), "pay_scheme": "percent", "sales_percent": "5", "branch": str(br.id)}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(str(r.data["branch"]), str(br.id))
        pid = r.data["id"]
        # глобальная схема того же сотрудника
        r2 = self.client.post(url, {"user": str(self.acc.id), "pay_scheme": "percent", "sales_percent": "3", "branch": None}, format="json")
        self.assertEqual(r2.status_code, 201, r2.data)
        self.assertIsNone(r2.data["branch"])
        # PATCH без branch ветку не трогает
        r3 = self.client.patch(f"{url}{pid}/", {"sales_percent": "7"}, format="json")
        self.assertEqual(r3.status_code, 200, r3.data)
        self.assertEqual(str(r3.data["branch"]), str(br.id))
        # список без активного филиала отдаёт обе
        r4 = self.client.get(url)
        rows = r4.data["results"] if isinstance(r4.data, dict) else r4.data
        self.assertEqual(len(rows), 2)
        # чужой филиал нельзя
        r5 = self.client.patch(f"{url}{pid}/", {"branch": str(other.id)}, format="json")
        self.assertEqual(r5.status_code, 400)
