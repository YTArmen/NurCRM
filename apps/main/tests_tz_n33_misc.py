"""ТЗ Н-33: мелочи (pickup_code, роли касс safe/bank, таблицы размеров, дизайн-ключи, скидка прихода и т.д.)."""
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main.models import Client, Product, ProductVariant, ShowcaseOrder
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "n33"}}


@override_settings(CACHES=LOCMEM)
class N33Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
        plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
        self.company = Company.objects.create(name="N33", slug="n33-shop", owner=self.owner, subscription_plan=plan, can_view_showcase=True)
        self.owner.company = self.company
        self.owner.save()
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.pub = APIClient()


class PickupCodeTests(N33Base):
    def test_generated_and_writable(self):
        o = ShowcaseOrder.objects.create(company=self.company, number=1, customer_name="A", customer_phone="1")
        self.assertTrue(o.pickup_code.isdigit() and 4 <= len(o.pickup_code) <= 6)
        r = self.api.get(f"/api/main/showcase/orders/{o.id}/")
        self.assertEqual(r.json()["pickup_code"], o.pickup_code)
        r = self.api.patch(f"/api/main/showcase/orders/{o.id}/", {"pickup_code": "7777"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        o.refresh_from_db()
        self.assertEqual(o.pickup_code, "7777")
        r = self.api.patch(f"/api/main/showcase/orders/{o.id}/", {"pickup_code": "ab"}, format="json")
        self.assertEqual(r.status_code, 400)


class CashboxRoleTests(N33Base):
    def test_safe_bank_roles_not_drawers(self):
        r = self.api.post("/api/construction/cashboxes/", {"name": "Сейф", "role": "safe"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["role"], "safe")
        r = self.api.post("/api/construction/cashboxes/", {"name": "Р/с", "role": "bank"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        for cb in Cashbox.objects.filter(company=self.company, role__in=("safe", "bank")):
            self.assertIsNone(cb.drawer_cash_info())


class SizeChartTests(N33Base):
    def test_crud_and_public_card(self):
        r = self.api.post("/api/main/showcase/size-charts/", {
            "name": "Кроссовки", "columns": ["EU", "см"], "rows": [["40", "25"], ["41", "26"]],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        cid = r.json()["id"]
        bad = self.api.post("/api/main/showcase/size-charts/", {"name": "x", "columns": ["a"], "rows": [["1", "2"]]}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(len(self.api.get("/api/main/showcase/size-charts/").json()), 1)
        self.assertEqual(self.api.patch(f"/api/main/showcase/size-charts/{cid}/", {"note": "по стельке"}, format="json").json()["note"], "по стельке")
        p = Product.objects.create(company=self.company, name="Nike", price=Decimal("10"), quantity=Decimal("1"))
        r = self.api.patch(f"/api/main/products/{p.id}/", {"size_chart": cid}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.pub.get(f"/api/main/public/companies/n33-shop/showcase/{p.id}/", secure=True)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["size_chart"]["columns"], ["EU", "см"])
        p2 = Product.objects.create(company=self.company, name="Без", price=Decimal("10"), quantity=Decimal("1"))
        r = self.pub.get(f"/api/main/public/companies/n33-shop/showcase/{p2.id}/", secure=True)
        self.assertIsNone(r.json()["size_chart"])
        self.assertEqual(self.api.delete(f"/api/main/showcase/size-charts/{cid}/").status_code, 204)


class DesignKeysTests(N33Base):
    def test_defaults_options_and_patch(self):
        r = self.api.get("/api/main/showcase/editor/options/")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()["defaults"]
        self.assertEqual(d["product_page"]["variant_picker"], "buttons")
        self.assertIs(d["card"]["show"]["swatches"], False)
        self.assertIn("swatches", r.json()["variant_pickers"])
        r = self.api.patch("/api/main/showcase/design/draft/", {
            "product_page": {"variant_picker": "swatches"}, "card": {"show": {"swatches": True}},
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        draft = r.json()["draft"]
        self.assertEqual(draft["product_page"]["variant_picker"], "swatches")
        self.assertIs(draft["card"]["show"]["swatches"], True)
        r = self.api.patch("/api/main/showcase/design/draft/", {"product_page": {"variant_picker": "nope"}}, format="json")
        self.assertEqual(r.status_code, 400)


class ReceiptDiscountTests(N33Base):
    def setUp(self):
        super().setUp()
        self.supplier = Client.objects.create(full_name="Пост", phone="+996700111222",
                                              type=Client.StatusClient.SUPPLIERS, company=self.company)
        self.a = Product.objects.create(name="A", company=self.company, quantity=Decimal("0"), purchase_price=Decimal("1.00"), price=Decimal("5.00"))
        self.b = Product.objects.create(name="B", company=self.company, quantity=Decimal("0"), purchase_price=Decimal("1.00"), price=Decimal("5.00"))

    def post(self, extra):
        body = {"items": [{"product": str(self.a.id), "qty": 10, "purchase_price": "10.00"},
                          {"product": str(self.b.id), "qty": 5, "purchase_price": "20.00"}], **extra}
        return self.api.post(f"/api/main/suppliers/{self.supplier.id}/receipt/", body, format="json")

    def test_without_new_fields_unchanged(self):
        r = self.post({})
        self.assertEqual(r.status_code, 200, r.content)
        self.a.refresh_from_db()
        self.assertEqual(self.a.purchase_price, Decimal("10.000"))

    def test_discount_and_extra_distributed(self):
        # строки: 100 и 100, итого 200. Скидка 10% = 20, доп. расходы 30 → +10 пропорционально: +5 и +5
        r = self.post({"discount": "10%", "extra_costs": [{"name": "Доставка", "amount": "30"}]})
        self.assertEqual(r.status_code, 200, r.content)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.a.purchase_price, Decimal("10.500"))  # 105 / 10
        self.assertEqual(self.b.purchase_price, Decimal("21.000"))  # 105 / 5
        d = self.api.get(f"/api/main/suppliers/receipts/{r.json()['id']}/").json()
        self.assertEqual(d["discount"], "20.00")
        self.assertEqual(d["extra_costs"][0]["name"], "Доставка")

    def test_discount_amount_exceeds_total(self):
        r = self.post({"discount": "500"})
        self.assertEqual(r.status_code, 400)

    def test_pdf(self):
        rid = self.post({"discount": "5"}).json()["id"]
        r = self.api.get(f"/api/main/suppliers/receipts/{rid}/pdf/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertEqual(self.api.get(f"/api/main/suppliers/receipts/{uuid.uuid4()}/pdf/").status_code, 404)


class VariantsCatalogTests(N33Base):
    def setUp(self):
        super().setUp()
        self.p = Product.objects.create(company=self.company, name="Nike", price=Decimal("10"), quantity=Decimal("0"), barcode="2000000000011")
        self.v = ProductVariant.objects.create(company=self.company, product=self.p, size="42", color="Чёрный",
                                               quantity=Decimal("2"), barcode="2000000000042")

    def test_include_variants(self):
        r = self.api.get("/api/main/products/list/", {"include": "variants"})
        self.assertEqual(r.status_code, 200, r.content)
        row = r.json()["results"][0]
        self.assertEqual(row["variants"][0]["size"], "42")
        row = self.api.get("/api/main/products/list/").json()["results"][0]
        self.assertNotIn("variants", row)

    def test_barcode_variant_and_product(self):
        r = self.api.get("/api/main/products/barcode/2000000000042/")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual(d["matched_by"], "variant_barcode")
        self.assertEqual(d["variant"]["size"], "42")
        self.assertEqual(d["product"]["id"], str(self.p.id))
        self.assertEqual(d["id"], str(self.p.id))
        r = self.api.get("/api/main/products/barcode/2000000000011/")
        self.assertEqual(r.json()["matched_by"], "barcode")
        self.assertNotIn("variant", r.json())
