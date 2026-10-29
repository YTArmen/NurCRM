"""ТЗ Н-26: витрина одежды — цвет, порядок размеров, фото по цвету, фильтры, facets."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from apps.main.models import Product, ProductImage, ProductVariant
from apps.main.variant_utils import size_sort_key
from apps.users.models import Company

User = get_user_model()
def _png():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buf, "PNG")
    return buf.getvalue()


BASE = "/api/main/public/companies/n26-shop/showcase/"


class SizeOrderTests(TestCase):
    def test_default_order(self):
        raw = ["Z", "W30", "48", "XL", "M", "W26", "5XL", "XXS", "44", "S", "A", "10", "XXL", "4XL"]
        got = sorted(raw, key=size_sort_key)
        self.assertEqual(got, ["XXS", "S", "M", "XL", "XXL", "4XL", "5XL", "10", "44", "48", "W26", "W30", "A", "Z"])


class PublicN26Tests(TestCase):
    def setUp(self):
        self.client = APIClient()
        owner = User.objects.create_user(email="n26@test.com", password="pass12345")
        self.company = Company.objects.create(name="N26", slug="n26-shop", owner=owner)
        self.nike = Product.objects.create(company=self.company, name="Nike", price=Decimal("1000"), quantity=Decimal("0"))
        self.plain = Product.objects.create(company=self.company, name="Носки", price=Decimal("50"), quantity=Decimal("10"))
        def mk(size, color, qty, hex_="", price=None, **kw):
            return ProductVariant.objects.create(
                company=self.company, product=self.nike, size=size, color=color, color_hex=hex_,
                quantity=Decimal(qty), price=price, **kw)
        self.v46w = mk("46", "Белый", "3", "#ffffff")
        self.v48w = mk("48", "Белый", "20")
        self.vm_black = mk("M", "Чёрный", "7", "#000000", price=Decimal("800"))
        self.v44b = mk("44", "Чёрный", "0")
        self.vhidden = mk("S", "Чёрный", "5", is_active=False)
        self.img = ProductImage.objects.create(
            company=self.company, product=self.nike, color="Белый", is_primary=True,
            image=SimpleUploadedFile("a.png", _png(), content_type="image/png"))

    def get(self, path="", **params):
        r = self.client.get(BASE + path, params, secure=True)
        self.assertEqual(r.status_code, 200, r.content[:400])
        return r.json()

    def test_card(self):
        d = self.get(f"{self.nike.id}/")
        self.assertEqual(d["sizes"], ["M", "44", "46", "48"])
        self.assertEqual([v["size"] for v in d["variants"]], ["M", "44", "46", "48"])
        names = {c["name"]: c for c in d["colors"]}
        self.assertEqual(set(names), {"Белый", "Чёрный"})
        self.assertEqual(names["Белый"]["hex"], "#ffffff")
        self.assertEqual(len(names["Белый"]["images"]), 1)
        self.assertEqual(names["Чёрный"]["images"], [])
        by = {(v["size"], v["color"]): v for v in d["variants"]}
        self.assertEqual(by[("46", "Белый")]["available"], 3)
        self.assertIsNone(by[("48", "Белый")]["available"])
        self.assertIsNone(by[("M", "Чёрный")]["available"])
        self.assertEqual(by[("M", "Чёрный")]["old_price"], "1000.00")
        self.assertIsNone(by[("46", "Белый")]["old_price"])
        self.assertNotIn("S", d["sizes"])
        self.assertEqual(d["images"][0]["color"], "Белый")

    def test_sort_order_overrides(self):
        ProductVariant.objects.filter(pk=self.v48w.pk).update(sort_order=-1)
        d = self.get(f"{self.nike.id}/")
        self.assertEqual(d["variants"][0]["size"], "48")
        # ТЗ витрины одежды 26.10: sizes тоже по sort_order
        self.assertEqual(d["sizes"], ["48", "M", "44", "46"])

    def test_list_fields(self):
        rows = {r["name"]: r for r in self.get()["results"]}
        n = rows["Nike"]
        self.assertEqual(n["sizes_in_stock"], ["M", "46", "48"])
        self.assertEqual([c["name"] for c in n["colors"]], ["Чёрный", "Белый"])
        self.assertEqual(n["price_range"]["min"], "800.00")
        self.assertEqual(rows["Носки"]["colors"], [])

    def test_filters(self):
        names = lambda d: {r["name"] for r in d["results"]}
        self.assertEqual(names(self.get(size="46", color="Белый")), {"Nike"})
        self.assertEqual(names(self.get(size="M,46")), {"Nike"})
        self.assertEqual(names(self.get(size="44")), set())  # размер есть, но остаток 0
        self.assertEqual(names(self.get(size="S")), set())  # неактивный
        self.assertEqual(names(self.get(color="Чёрный")), {"Nike"})
        self.assertEqual(names(self.get(size="46", color="Чёрный")), set())
        self.assertEqual(names(self.get(in_stock="1")), {"Nike", "Носки"})
        # обнулили остаток «46 Белый» — товар пропал из фильтра
        ProductVariant.objects.filter(pk=self.v46w.pk).update(quantity=0)
        self.assertEqual(names(self.get(size="46", color="Белый")), set())
        ProductVariant.objects.filter(product=self.nike).update(quantity=0)
        self.assertEqual(names(self.get(in_stock="1")), {"Носки"})
        self.assertEqual(names(self.get(in_stock="0")), {"Nike"})

    def test_facets(self):
        d = self.get("facets/")
        self.assertEqual([s["value"] for s in d["sizes"]], ["M", "46", "48"])
        self.assertTrue(all(s["count"] == 1 for s in d["sizes"]))
        cols = {c["name"]: c for c in d["colors"]}
        self.assertEqual(cols["Белый"]["count"], 1)
        self.assertEqual(cols["Белый"]["hex"], "#ffffff")
        self.assertEqual(d["price"], {"min": "50.00", "max": "1000.00"})
        d = self.get("facets/", search="Nike")
        self.assertEqual(d["price"]["min"], "800.00")

    def test_variant_color_hex_validation(self):
        from apps.main.kassa_stage3 import ProductVariantSerializer
        ser = ProductVariantSerializer(data={"size": "L", "color_hex": "red"}, context={"product": self.nike})
        self.assertFalse(ser.is_valid())
        self.assertIn("color_hex", ser.errors)
        ser = ProductVariantSerializer(data={"size": "L", "color_hex": "#a1b2c3"}, context={"product": self.nike})
        self.assertTrue(ser.is_valid(), ser.errors)
        self.assertEqual(ser.validated_data["color_hex"], "#A1B2C3")
