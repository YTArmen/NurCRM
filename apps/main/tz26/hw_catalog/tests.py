# Тесты куска hw_catalog: python manage.py test apps.main.tz26.hw_catalog --settings=core.settings_test_sqlite
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.main.models import Product, ProductCategory
from apps.possync.tests import PossyncBase

from .models import CategoryRelation, ProductRelation


class SphereProfilesTests(TestCase):
    def test_hardware_profile_public(self):
        r = APIClient().get("/api/public/sphere-profiles/")
        self.assertEqual(r.status_code, 200)
        prof = {p["id"]: p for p in r.json()}["hardware"]
        self.assertEqual(prof["base"], "grocery")
        self.assertEqual(len(prof["category_hints"]), 15)
        for lang in ("ru", "ky", "en", "tr", "uz"):
            self.assertTrue(prof["title"][lang])
            self.assertTrue(all(h[lang] for h in prof["category_hints"]))
        self.assertEqual(prof["fractional_units"]["m2"], {"step": "0.01", "min": "0.5"})
        self.assertTrue(prof["features"]["storage_locations"])


class HwBase(PossyncBase):
    def mkp(self, name, qty="10", price="10.00", **kw):
        return Product.objects.create(company=self.company, name=name, price=Decimal(price), quantity=Decimal(qty), **kw)

    def patch(self, product, body):
        return self.api.patch(f"/api/main/products/{product.id}/", body, format="json")

    def ids(self, url):
        r = self.api.get(url)
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        data = r.data["results"] if isinstance(r.data, dict) and "results" in r.data else r.data
        return {str(p["id"]) for p in data}


class AttributesTests(HwBase):
    def test_keys_dictionary(self):
        r = self.api.post("/api/main/attribute-keys/", {
            "key": "diameter_mm", "name": {"ru": "Диаметр", "en": "Diameter"}, "type": "number", "unit": "мм", "filterable": True,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(set(r.data["name"]), {"ru", "ky", "en", "tr", "uz"})
        self.assertEqual(r.data["name"]["ky"], "Диаметр")  # нет перевода — берём ru
        r2 = self.api.post("/api/main/attribute-keys/", {"key": "diameter_mm", "name": "x"}, format="json")
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.data["code"], "attribute_key_exists")
        bad = self.api.post("/api/main/attribute-keys/", {"key": "Bad Key", "name": "x"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.data["code"], "invalid_attribute_key")
        self.assertEqual(len(self.api.get("/api/main/attribute-keys/").data), 1)
        d = self.api.post("/api/main/attribute-keys/defaults/", {}, format="json")
        self.assertEqual(d.status_code, 200)
        self.assertNotIn("diameter_mm", d.data["created"])
        self.assertIn("length_m", d.data["created"])

    def test_write_filter_and_search(self):
        pipe20 = self.mkp("Труба ПП")
        pipe25 = self.mkp("Труба ПВХ")
        paint = self.mkp("Краска")
        r = self.patch(pipe20, {"attributes": [
            {"key": "diameter_mm", "value": 20, "unit": "мм"}, {"key": "color_ral", "value": "9016"}]})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["attributes"][0], {"key": "diameter_mm", "value": 20, "unit": "мм"})
        self.patch(pipe25, {"attributes": [{"key": "diameter_mm", "value": "25.0"}, {"key": "color_ral", "value": "9010"}]})
        self.patch(paint, {"attributes": [{"key": "color_ral", "value": "9016"}]})

        base = "/api/main/products/list/"
        self.assertEqual(self.ids(f"{base}?attr.diameter_mm=20"), {str(pipe20.id)})
        self.assertEqual(self.ids(f"{base}?attr.diameter_mm=25"), {str(pipe25.id)})  # 25.0 == 25
        self.assertEqual(self.ids(f"{base}?attr.color_ral=9016"), {str(pipe20.id), str(paint.id)})
        self.assertEqual(self.ids(f"{base}?attr.color_ral=9016&attr.diameter_mm=20"), {str(pipe20.id)})
        self.assertEqual(self.ids(f"{base}?attr.color_ral=9016,9010"), {str(pipe20.id), str(pipe25.id), str(paint.id)})
        self.assertEqual(self.ids(f"{base}?search=9010"), {str(pipe25.id)})
        self.assertIn(str(pipe20.id), self.ids(f"{base}?search=20"))
        r = self.api.get(f"{base}?attr.BAD=1")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "invalid_attribute_key")

    def test_replace_and_validation(self):
        p = self.mkp("Кабель")
        self.patch(p, {"attributes": [{"key": "cores", "value": 3}]})
        r = self.patch(p, {"attributes": [{"key": "section_mm2", "value": "2.5"}]})
        self.assertEqual([a["key"] for a in r.data["attributes"]], ["section_mm2"])
        self.assertEqual(self.ids("/api/main/products/list/?attr.cores=3"), set())  # индекс обновился
        for bad in (
            [{"key": "A", "value": 1}],
            [{"key": "x", "value": "y" * 65}],
            [{"key": "x", "value": True}],
            [{"key": "x", "value": 1}, {"key": "x", "value": 2}],
            [{"key": f"k{i}", "value": i} for i in range(31)],
            "строка",
        ):
            r = self.patch(p, {"attributes": bad})
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.data["code"], "invalid_attributes")
        self.api.post("/api/main/attribute-keys/", {
            "key": "grade", "name": "Марка", "type": "enum", "options": ["M100", "M200"]}, format="json")
        r = self.patch(p, {"attributes": [{"key": "grade", "value": "M999"}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.patch(p, {"attributes": [{"key": "grade", "value": "M100"}]}).status_code, 200)

    def test_create_manual_with_attributes(self):
        r = self.api.post("/api/main/products/create-manual/", {
            "name": "Саморез", "price": "1", "quantity": "5",
            "attributes": [{"key": "length_mm", "value": 35}], "storage_location": "А-01-1",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.ids("/api/main/products/list/?attr.length_mm=35"), {str(Product.objects.get(name="Саморез").id)})
        p = Product.objects.get(name="Саморез")
        self.assertEqual(p.attributes[0]["value"], 35)

    def test_public_card_and_facets(self):
        p1 = self.mkp("Труба 20")
        p2 = self.mkp("Труба 25")
        p3 = self.mkp("Труба 20 синяя")
        self.api.post("/api/main/attribute-keys/", {
            "key": "diameter_mm", "name": {"ru": "Диаметр", "en": "Diameter"}, "type": "number", "unit": "мм",
            "filterable": True}, format="json")
        self.patch(p1, {"attributes": [{"key": "diameter_mm", "value": 20}]})
        self.patch(p2, {"attributes": [{"key": "diameter_mm", "value": 25}]})
        self.patch(p3, {"attributes": [{"key": "diameter_mm", "value": 20}]})
        slug = self.company.slug
        pub = APIClient()
        r = pub.get(f"/api/main/public/companies/{slug}/showcase/{p1.id}/")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        self.assertEqual(r.data["attributes"], [{"key": "diameter_mm", "name": "Диаметр", "value": 20, "unit": "мм"}])
        r = pub.get(f"/api/main/public/companies/{slug}/showcase/facets/")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        fac = r.data["attributes"][0]
        self.assertEqual((fac["key"], fac["name"]), ("diameter_mm", "Диаметр"))
        self.assertEqual(fac["values"], [{"value": "20", "count": 2}, {"value": "25", "count": 1}])
        r = pub.get(f"/api/main/public/companies/{slug}/showcase/?attr.diameter_mm=25")
        self.assertEqual({x["id"] for x in r.data["results"]}, {str(p2.id)})
        r = pub.get(f"/api/main/public/companies/{slug}/showcase/", {"lang": "en"})
        self.assertEqual(r.status_code, 200)


class StorageLocationTests(HwBase):
    def setUp(self):
        super().setUp()
        from apps.users.models import Branch

        self.branch = Branch.objects.create(company=self.company, name="Филиал 1")

    def test_location_per_branch_filter_and_card(self):
        a = self.mkp("Гвозди")
        b = self.mkp("Шурупы")
        c = self.mkp("Краска")
        self.assertEqual(self.patch(a, {"storage_location": "Б-04-2"}).data["storage_location"], "Б-04-2")
        self.patch(b, {"storage_location": "Б-04-3", "branch": "main"})
        self.patch(c, {"storage_location": "В-01"})
        r = self.patch(a, {"storage_location": "Ф-9", "branch": str(self.branch.id)})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["storage_location"], "Ф-9")  # ответ показывает филиал из тела запроса

        base = "/api/main/products/list/"
        self.assertEqual(self.ids(f"{base}?location_prefix=Б-04"), {str(a.id), str(b.id)})  # главный склад
        self.assertEqual(self.ids(f"{base}?location_prefix=Б-04-3"), {str(b.id)})
        d = self.mkp("Дюбель", branch=self.branch)  # товар филиала
        self.patch(d, {"storage_location": "Ф-1", "branch": str(self.branch.id)})
        self.assertEqual(self.ids(f"{base}?location_prefix=Ф&branch={self.branch.id}"), {str(d.id)})
        rows = self.api.get(f"{base}?branch=main").data["results"]
        self.assertEqual({x["name"]: x["storage_location"] for x in rows}["Гвозди"], "Б-04-2")
        card = self.api.get(f"/api/main/products/{a.id}/").data
        self.assertEqual(card["storage_location"], "Б-04-2")
        self.assertEqual({x["branch"]: x["location"] for x in card["storage_locations"]},
                         {"main": "Б-04-2", str(self.branch.id): "Ф-9"})
        # очистка и проверка длины
        self.assertEqual(self.patch(c, {"storage_location": ""}).data["storage_location"], "")
        r = self.patch(c, {"storage_location": "x" * 33})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "invalid_storage_location")
        r = self.patch(c, {"storage_location": "A", "branch": "00000000-0000-0000-0000-000000000001"})
        self.assertEqual(r.data["code"], "branch_not_found")

    def test_inventory_by_location_prefix(self):
        a = self.mkp("Гвозди")
        b = self.mkp("Шурупы")
        c = self.mkp("Краска")
        self.patch(a, {"storage_location": "Б-04-2"})
        self.patch(b, {"storage_location": "Б-04-3"})
        self.patch(c, {"storage_location": "В-01"})
        url = "/api/main/inventory/sessions/"
        r = self.api.post(url, {"location_prefix": "Б-04", "items": [
            {"product_id": str(a.id), "quantity_fact": "7"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["location_prefix"], r.data["expected_count"], r.data["uncounted_count"]), ("Б-04", 2, 1))
        self.assertEqual([str(ln["product"]) for ln in r.data["lines"]], [str(a.id)])
        r = self.api.post(url, {"location_prefix": "Б-04", "items": [
            {"product_id": str(c.id), "quantity_fact": "1"}]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "product_not_in_location")
        # без префикса — как раньше
        r = self.api.post(url, {"items": [{"product_id": str(c.id), "quantity_fact": "1"}]}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertNotIn("location_prefix", r.data)


class RelatedTests(HwBase):
    def setUp(self):
        super().setUp()
        self.tile = self.mkp("Плитка")
        self.glue = self.mkp("Клей", qty="50")
        self.tile2 = self.mkp("Плитка-аналог", qty="0")
        self.tile3 = self.mkp("Плитка-аналог 2", qty="4")

    def test_related_input_and_card(self):
        r = self.patch(self.tile, {"related_input": [
            {"product": str(self.glue.id), "kind": "accessory", "ratio": 5, "ratio_unit": "kg", "sort_order": 1},
            {"product": str(self.tile2.id), "kind": "analog", "sort_order": 0},
        ]})
        self.assertEqual(r.status_code, 200, r.data)
        rel = r.data["related"]
        self.assertEqual([(x["product"], x["kind"], x["ratio"], x["ratio_unit"]) for x in rel],
                         [(str(self.tile2.id), "analog", None, ""), (str(self.glue.id), "accessory", "5", "kg")])
        # замена целиком, пустой список очищает
        r = self.patch(self.tile, {"related_input": [{"product": str(self.glue.id), "ratio": "2.5", "ratio_unit": "kg"}]})
        self.assertEqual(len(r.data["related"]), 1)
        self.assertEqual(r.data["related"][0]["ratio"], "2.5")
        self.assertEqual(self.patch(self.tile, {"related_input": []}).data["related"], [])
        self.assertEqual(ProductRelation.objects.count(), 0)

    def test_related_validation(self):
        for bad in (
            [{"product": str(self.tile.id)}],
            [{"product": "nope"}],
            [{"product": str(uuid_other())}],
            [{"product": str(self.glue.id), "kind": "x"}],
            [{"product": str(self.glue.id), "ratio": -1}],
            [{"product": str(self.glue.id)}, {"product": str(self.glue.id)}],
        ):
            r = self.patch(self.tile, {"related_input": bad})
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.data["code"], "invalid_related")

    def test_public_card_related_only_in_stock(self):
        self.patch(self.tile, {"related_input": [
            {"product": str(self.glue.id), "kind": "accessory", "ratio": 5, "ratio_unit": "kg"},
            {"product": str(self.tile2.id), "kind": "analog"},
            {"product": str(self.tile3.id), "kind": "analog"},
        ]})
        r = APIClient().get(f"/api/main/public/companies/{self.company.slug}/showcase/{self.tile.id}/")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        got = {x["id"]: x for x in r.data["related"]}
        self.assertEqual(set(got), {str(self.glue.id), str(self.tile3.id)})  # без остатка — нет
        self.assertEqual((got[str(self.glue.id)]["ratio"], got[str(self.glue.id)]["ratio_unit"]), ("5", "kg"))
        self.assertEqual(got[str(self.glue.id)]["kind"], "accessory")
        lst = APIClient().get(f"/api/main/public/companies/{self.company.slug}/showcase/")
        self.assertNotIn("related", lst.data["results"][0])

    def test_category_relations_and_public_fallback(self):
        cat_tile = ProductCategory.objects.create(company=self.company, name="Плитка")
        cat_glue = ProductCategory.objects.create(company=self.company, name="Клей")
        Product.objects.filter(pk=self.tile.pk).update(category=cat_tile)
        Product.objects.filter(pk=self.glue.pk).update(category=cat_glue)
        r = self.api.post("/api/main/category-relations/", {
            "category": str(cat_tile.id), "related_category": str(cat_glue.id), "kind": "accessory"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        again = self.api.post("/api/main/category-relations/", {
            "category": str(cat_tile.id), "related_category": str(cat_glue.id)}, format="json")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(CategoryRelation.objects.count(), 1)
        self.assertEqual(len(self.api.get("/api/main/category-relations/").data), 1)
        bad = self.api.post("/api/main/category-relations/", {
            "category": str(cat_tile.id), "related_category": str(uuid_other())}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.data["code"], "category_not_found")
        pub = APIClient().get(f"/api/main/public/companies/{self.company.slug}/showcase/{self.tile.id}/")
        self.assertEqual([(x["id"], x["source"]) for x in pub.data["related"]], [(str(self.glue.id), "category")])
        self.assertEqual(self.api.delete(f"/api/main/category-relations/{r.data['id']}/").status_code, 204)


def uuid_other():
    import uuid

    return uuid.uuid4()
