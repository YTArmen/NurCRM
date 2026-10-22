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


class AnalyticsTests(BuhBase):
    URL = "/api/main/analytics/market/"

    def test_sales_dynamics_has_transactions(self):
        from decimal import Decimal
        from django.utils import timezone
        from apps.main.models import Sale

        now = timezone.now()
        for _ in range(3):
            Sale.objects.create(company=self.company, status=Sale.Status.PAID, total=Decimal("100"), paid_at=now, cashbox=self.box)
        self.client.force_authenticate(self.owner)
        today = timezone.localdate().isoformat()
        r = self.client.get(self.URL, {"tab": "sales", "date_from": today, "date_to": today})
        self.assertEqual(r.status_code, 200, r.data)
        day = r.data["charts"]["sales_dynamics"][0]
        self.assertEqual(day["transactions"], 3)
        self.assertEqual(day["value"], "300.00")

    def test_stock_items_paginated_with_abc(self):
        from decimal import Decimal
        from apps.main.models import Product

        for i, (qty, cost) in enumerate([(7, 100), (2, 100), (7, 10), (6, 5)]):
            Product.objects.create(company=self.company, name=f"T{i}", price=Decimal("200"),
                                   purchase_price=Decimal(cost), quantity=Decimal(qty))
        self.client.force_authenticate(self.owner)
        r = self.client.get(self.URL, {"tab": "stock_items", "page_size": 2, "sort": "stock_value", "order": "desc", "abc": 1})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["count"], 4)
        self.assertEqual([i["name"] for i in r.data["items"]], ["T0", "T1"])
        self.assertEqual(r.data["items"][0]["stock_value"], "700.00")
        self.assertEqual(r.data["items"][0]["purchase_price"], "100.00")
        self.assertEqual(r.data["items"][0]["class"], "A")
        self.assertEqual(r.data["totals"]["stock_value"], "1000.00")
        self.assertEqual(sum(r.data["abc"]["counts"].values()), 4)
        r2 = self.client.get(self.URL, {"tab": "stock_items", "page": 2, "page_size": 3})
        self.assertEqual(len(r2.data["items"]), 1)
        r3 = self.client.get(self.URL, {"tab": "stock_items", "only_in_stock": 1, "search": "T1"})
        self.assertEqual(r3.data["count"], 1)


class AuditAndTrashTests(BuhBase):
    def _product(self, **kw):
        from decimal import Decimal
        from apps.main.models import Product

        d = dict(company=self.company, name="Мыло", price=Decimal("100"), purchase_price=Decimal("60"),
                 quantity=Decimal("5"), barcode="4600000000011")
        d.update(kw)
        return Product.objects.create(**d)

    def _log(self, **params):
        r = self.client.get("/api/main/audit-log/", params)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data["results"]

    def test_price_and_stock_change_logged(self):
        p = self._product()
        self.client.force_authenticate(self.owner)
        r = self.client.patch(f"/api/main/products/{p.id}/", {"price": "120", "quantity": "9"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        price = self._log(action="price_change", object_id=str(p.id))
        self.assertEqual(len(price), 1)
        self.assertEqual(price[0]["before"]["price"], "100")
        self.assertEqual(price[0]["after"]["price"], "120")
        stock = self._log(action="stock_change", object_type="product")
        self.assertEqual(stock[0]["before"], {"quantity": "5"})
        self.assertEqual(stock[0]["after"], {"quantity": "9"})
        # правка названия без цены — записей нет
        self.client.patch(f"/api/main/products/{p.id}/", {"name": "Мыло 2"}, format="json")
        self.assertEqual(len(self._log(object_id=str(p.id))), 2)

    def test_rights_change_and_login_logged(self):
        self.client.force_authenticate(self.owner)
        r = self.client.patch(f"/api/users/employees/{self.acc.id}/", {"can_edit_cashbox": False}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        rows = self._log(action="rights_change", user=str(self.owner.id))
        self.assertEqual(rows[0]["after"], {"can_edit_cashbox": False})
        self.assertEqual(rows[0]["object_id"], str(self.acc.id))
        self.client.force_authenticate(None)
        r = self.client.post("/api/users/auth/login/", {"email": self.owner.email, "password": "p"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.client.force_authenticate(self.owner)
        self.assertEqual(len(self._log(action="login")), 1)

    def test_filters_dates_and_cashbox_viewer_limit(self):
        from datetime import date

        p = self._product()
        self.client.force_authenticate(self.owner)
        self.client.patch(f"/api/main/products/{p.id}/", {"price": "130"}, format="json")
        today = date.today().isoformat()
        self.assertEqual(len(self._log(date_from=today, date_to=today)), 1)
        self.assertEqual(len(self._log(date_from="2000-01-01", date_to="2000-01-02")), 0)
        # бухгалтер с can_view_cashbox не видит записи про товары
        self.client.force_authenticate(self.acc)
        self.assertEqual(self._log(), [])

    def test_delete_product_client_sale_logged_and_trash_restore(self):
        from apps.main.models import Client, Product

        p = self._product()
        c = Client.objects.create(company=self.company, full_name="Иван", phone="+996700000001")
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.delete(f"/api/main/products/{p.id}/").status_code, 204)
        self.assertEqual(self.client.delete(f"/api/main/clients/{c.id}/").status_code, 204)
        self.assertFalse(Product.objects.filter(pk=p.pk).exists())
        # штрихкод свободен для нового товара
        self._product(name="Новый", barcode="4600000000011")
        dels = self._log(action="delete")
        self.assertEqual({d["object_type"] for d in dels}, {"product", "client"})
        tr = self.client.get("/api/main/trash/", {"type": "product"})
        self.assertEqual(tr.status_code, 200, tr.data)
        self.assertEqual(tr.data["count"], 1)
        self.assertEqual(tr.data["results"][0]["name"], "Мыло")
        # вернуть клиента
        r = self.client.post(f"/api/main/trash/client/{c.id}/restore/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(Client.objects.filter(pk=c.pk, full_name="Иван").exists())
        self.assertEqual(self.client.get("/api/main/trash/", {"type": "client"}).data["count"], 0)
        # товар: штрихкод занят новым -> вернётся без штрихкода, с примечанием
        r = self.client.post(f"/api/main/trash/product/{p.id}/restore/")
        self.assertEqual(r.status_code, 200, r.data)
        back = Product.objects.get(pk=p.pk)
        self.assertEqual(back.name, "Мыло")
        self.assertIsNone(back.barcode)
        self.assertTrue(r.data["notes"])
        # повторно — 404
        self.assertEqual(self.client.post(f"/api/main/trash/product/{p.id}/restore/").status_code, 404)

    def test_trash_expiry_30_days(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.construction.models import TrashEntry

        p = self._product()
        self.client.force_authenticate(self.owner)
        self.client.delete(f"/api/main/products/{p.id}/")
        TrashEntry.objects.update(deleted_at=timezone.now() - timedelta(days=31))
        self.assertEqual(self.client.post(f"/api/main/trash/product/{p.id}/restore/").status_code, 404)
        self.assertEqual(self.client.get("/api/main/trash/").data["count"], 0)

    def test_category_and_brand_trash(self):
        from apps.main.models import ProductBrand, ProductCategory

        cat = ProductCategory.objects.create(company=self.company, name="Бытовая")
        p = self._product(category=cat)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.delete(f"/api/main/categories/{cat.id}/").status_code, 204)
        p.refresh_from_db()
        self.assertIsNone(p.category_id)
        r = self.client.post(f"/api/main/trash/category/{cat.id}/restore/")
        self.assertEqual(r.status_code, 200, r.data)
        p.refresh_from_db()
        self.assertEqual(p.category_id, cat.id)  # связь вернулась
        br = ProductBrand.objects.create(company=self.company, name="Бренд")
        self.assertEqual(self.client.delete(f"/api/main/brands/{br.id}/").status_code, 204)
        self.assertEqual(self.client.get("/api/main/trash/", {"type": "brand"}).data["count"], 1)
        self.assertEqual(self.client.post(f"/api/main/trash/brand/{br.id}/restore/").status_code, 200)

    def test_archived_product_barcode_released(self):
        from decimal import Decimal
        from apps.main.models import Product, Sale, SaleItem

        p = self._product()
        sale = Sale.objects.create(company=self.company, status=Sale.Status.PAID, total=Decimal("100"), cashbox=self.box)
        SaleItem.objects.create(sale=sale, product=p, name_snapshot="Мыло", quantity=Decimal("1"), unit_price=Decimal("100"))
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.delete(f"/api/main/products/{p.id}/").status_code, 204)
        p.refresh_from_db()
        self.assertEqual(p.status, Product.Status.ARCHIVED)
        self.assertIsNone(p.barcode)
        self._product(name="Другой", barcode="4600000000011")  # не падает на уникальности
        r = self.client.post(f"/api/main/trash/product/{p.id}/restore/")
        self.assertEqual(r.status_code, 200, r.data)
        p.refresh_from_db()
        self.assertNotEqual(p.status, Product.Status.ARCHIVED)


class PinTests(BuhBase):
    P = "/api/users/auth/pin/"

    def _enable(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.post(self.P, {"pin": "1234"}, format="json").status_code, 200)
        r = self.client.patch(self.P + "settings/", {"require_pin": True}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data, {"require_pin": True, "has_pin": True})

    def _product(self):
        from decimal import Decimal
        from apps.main.models import Product

        return Product.objects.create(company=self.company, name="П", price=Decimal("1"), quantity=Decimal("1"))

    def test_pin_validation_and_change(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.post(self.P, {"pin": "12"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.P, {"pin": "12ab"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.P, {"pin": "1234"}, format="json").status_code, 200)
        # смена требует текущий
        self.assertEqual(self.client.post(self.P, {"pin": "654321"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.P, {"pin": "654321", "current_pin": "1234"}, format="json").status_code, 200)
        self.assertEqual(self.client.post(self.P + "verify/", {"pin": "1234"}, format="json").status_code, 400)
        r = self.client.post(self.P + "verify/", {"pin": "654321"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["expires_in"], 300)
        # хэш, не открытый текст
        from apps.users.models import AdminPin
        self.assertNotIn("654321", AdminPin.objects.get(user=self.owner).pin_hash)

    def test_lock_after_five_wrong(self):
        self.client.force_authenticate(self.owner)
        self.client.post(self.P, {"pin": "1234"}, format="json")
        for i in range(4):
            r = self.client.post(self.P + "verify/", {"pin": "0000"}, format="json")
            self.assertEqual((r.status_code, r.data["code"], r.data["attempts_left"]), (400, "pin_invalid", 4 - i))
        r = self.client.post(self.P + "verify/", {"pin": "0000"}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (429, "pin_locked"))
        # даже верный PIN заблокирован
        r = self.client.post(self.P + "verify/", {"pin": "1234"}, format="json")
        self.assertEqual(r.status_code, 429)
        from datetime import timedelta
        from django.utils import timezone
        from apps.users.models import AdminPin
        AdminPin.objects.update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post(self.P + "verify/", {"pin": "1234"}, format="json").status_code, 200)

    def test_dangerous_actions_need_token(self):
        p = self._product()
        self._enable()
        r = self.client.delete(f"/api/main/products/{p.id}/")
        self.assertEqual((r.status_code, r.data["code"]), (403, "pin_required"))
        r = self.client.post("/api/main/products/bulk-delete/", {"ids": [str(p.id)]}, format="json")
        self.assertEqual(r.status_code, 403)
        # PATCH не затронут
        self.assertEqual(self.client.patch(f"/api/main/products/{p.id}/", {"name": "Н"}, format="json").status_code, 200)
        tok = self.client.post(self.P + "verify/", {"pin": "1234"}, format="json").data["token"]
        self.assertEqual(self.client.delete(f"/api/main/products/{p.id}/", HTTP_X_CONFIRM_TOKEN="bad").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/main/products/{p.id}/", HTTP_X_CONFIRM_TOKEN=tok).status_code, 204)

    def test_token_expires_and_other_user(self):
        from unittest import mock
        import time
        from apps.users import pin as pinmod

        self._enable()
        tok = self.client.post(self.P + "verify/", {"pin": "1234"}, format="json").data["token"]
        self.assertTrue(pinmod.token_is_valid(tok, self.owner))
        self.assertFalse(pinmod.token_is_valid(tok, self.acc))
        with mock.patch("django.core.signing.time.time", return_value=time.time() + 301):
            self.assertFalse(pinmod.token_is_valid(tok, self.owner))

    def test_off_by_default_and_cancel_and_return_guarded(self):
        p = self._product()
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.delete(f"/api/main/products/{p.id}/").status_code, 204)  # настройка выключена
        self._enable()
        r = self.client.post("/api/construction/cashflows/00000000-0000-0000-0000-000000000001/cancel-request/", {}, format="json")
        self.assertEqual((r.status_code, r.data.get("code")), (403, "pin_required"))
        r = self.client.post("/api/main/pos/sales/00000000-0000-0000-0000-000000000001/return/", {}, format="json")
        self.assertEqual((r.status_code, r.data.get("code")), (403, "pin_required"))

    def test_employee_confirms_with_admin_pin(self):
        self._enable()
        self.client.force_authenticate(self.acc)
        self.assertEqual(self.client.post(self.P, {"pin": "1111"}, format="json").status_code, 403)
        self.assertEqual(self.client.post(self.P + "verify/", {"pin": "9999"}, format="json").status_code, 400)
        r = self.client.post(self.P + "verify/", {"pin": "1234"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.client.get(self.P + "settings/").data, {"require_pin": True, "has_pin": True})
        self.assertEqual(self.client.patch(self.P + "settings/", {"require_pin": False}, format="json").status_code, 403)
