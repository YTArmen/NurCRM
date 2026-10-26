import os
import tempfile
from datetime import timedelta
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.catalog_media import services
from apps.catalog_media.models import GlobalProductPhoto
from apps.main.models import GlobalProduct, Product, ProductImage
from apps.users.models import Company

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="gp-media-")


def touch(name):
    path = os.path.join(MEDIA, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x")


class FakeResp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@override_settings(MEDIA_ROOT=MEDIA, MEDIA_URL="/media/")
class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="gp@test.com", password="x", role="owner")
        self.company = Company.objects.create(name="GP Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()

    def gp(self, barcode, name="Товар"):
        return GlobalProduct.objects.create(name=name, barcode=barcode)

    def product(self, barcode, images, name="P", company=None):
        company = company or self.company
        p = Product.objects.create(company=company, name=name, barcode=barcode,
                                   purchase_price=1, price=2, quantity=1)
        ProductImage.objects.bulk_create([
            ProductImage(company=company, product=p, image=img, is_primary=prim)
            for img, prim in images
        ])
        return p


class SerializerTests(Base):
    def _get(self, barcode):
        api = APIClient()
        api.force_authenticate(self.owner)
        r = api.get(f"/api/main/products/global-barcode/{barcode}/", secure=True)
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        return r.data

    def test_no_photo(self):
        self.gp("4870000000017")
        d = self._get("4870000000017")
        self.assertIsNone(d["image_url"])
        self.assertIsNone(d["image_source"])

    def test_store_photo_absolute_url(self):
        g = self.gp("4870000000017")
        touch("products/a/1.webp")
        GlobalProductPhoto.objects.create(product=g, source="store", image_name="products/a/1.webp")
        d = self._get("4870000000017")
        self.assertEqual(d["image_url"], "https://testserver/media/products/a/1.webp")
        self.assertEqual(d["image_source"], "store")

    def test_store_photo_missing_file_hidden(self):
        g = self.gp("4870000000017")
        GlobalProductPhoto.objects.create(product=g, source="store", image_name="products/gone.webp")
        d = self._get("4870000000017")
        self.assertIsNone(d["image_url"])
        self.assertIsNone(d["image_source"])

    def test_off_photo_and_miss(self):
        g = self.gp("4870000000017")
        GlobalProductPhoto.objects.create(product=g, source="off", url="https://images.openfoodfacts.org/x.jpg")
        d = self._get("4870000000017")
        self.assertEqual(d["image_url"], "https://images.openfoodfacts.org/x.jpg")
        self.assertEqual(d["image_source"], "off")
        g2 = self.gp("4870000000024")
        GlobalProductPhoto.objects.create(product=g2, source="off")  # промах
        d = self._get("4870000000024")
        self.assertIsNone(d["image_url"])
        self.assertIsNone(d["image_source"])

    def test_photo_joined_in_one_query(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        g = self.gp("4870000000017")
        touch("products/a/1.webp")
        GlobalProductPhoto.objects.create(product=g, source="store", image_name="products/a/1.webp")
        api = APIClient()
        api.force_authenticate(self.owner)
        with CaptureQueriesContext(connection) as ctx:
            api.get("/api/main/products/global-barcode/4870000000017/", secure=True)
        photo_q = [q for q in ctx.captured_queries if "catalog_media_globalproductphoto" in q["sql"]]
        self.assertEqual(len(photo_q), 1, "фото должно идти join-ом, а не отдельным запросом")


class StoreFillTests(Base):
    def test_pick_most_recent_primary_and_existing(self):
        g = self.gp("4870000000017")
        touch("products/old/1.webp")
        touch("products/new/1.webp")
        touch("products/new/2.webp")
        owner2 = User.objects.create_user(email="gp2@test.com", password="x", role="owner")
        company2 = Company.objects.create(name="GP Co2", owner=owner2)
        old = self.product("4870000000017", [("products/old/1.webp", True)], "old")
        new = self.product("4870000000017", [("products/new/2.webp", False), ("products/new/1.webp", True)], "new", company=company2)
        Product.objects.filter(pk=old.pk).update(updated_at=timezone.now() - timedelta(days=5))
        out = services.fill_store()
        self.assertEqual(out["found"], 1)
        ph = GlobalProductPhoto.objects.get(product=g)
        self.assertEqual((ph.source, ph.image_name, ph.source_product_id), ("store", "products/new/1.webp", new.pk))

    def test_skips_missing_file_and_empty_image(self):
        g = self.gp("4870000000017")
        self.product("4870000000017", [("products/nofile.webp", True), ("", False)])
        self.assertEqual(services.fill_store()["found"], 0)
        self.assertFalse(GlobalProductPhoto.objects.filter(product=g).exists())

    def test_falls_through_to_next_existing_file(self):
        g = self.gp("4870000000017")
        touch("products/b/ok.webp")
        self.product("4870000000017", [("products/b/ok.webp", False), ("products/b/gone.webp", True)])
        services.fill_store()
        self.assertEqual(GlobalProductPhoto.objects.get(product=g).image_name, "products/b/ok.webp")

    def test_idempotent_and_dry_run(self):
        self.gp("4870000000017")
        touch("products/a/1.webp")
        self.product("4870000000017", [("products/a/1.webp", True)])
        self.assertEqual(services.fill_store(dry_run=True)["found"], 1)
        self.assertEqual(GlobalProductPhoto.objects.count(), 0)
        services.fill_store()
        self.assertEqual(GlobalProductPhoto.objects.count(), 1)
        again = services.fill_store()
        self.assertEqual(again["found"], 0)
        self.assertEqual(GlobalProductPhoto.objects.count(), 1)

    def test_store_replaces_off(self):
        g = self.gp("4870000000017")
        GlobalProductPhoto.objects.create(product=g, source="off", url="https://x/y.jpg")
        touch("products/a/1.webp")
        self.product("4870000000017", [("products/a/1.webp", True)])
        services.fill_store()
        ph = GlobalProductPhoto.objects.get(product=g)
        self.assertEqual((ph.source, ph.url), ("store", ""))

    def test_command_runs(self):
        self.gp("4870000000017")
        touch("products/a/1.webp")
        self.product("4870000000017", [("products/a/1.webp", True)])
        out = StringIO()
        call_command("fill_global_photos", "--source", "store", "--limit", "5", stdout=out)
        self.assertEqual(GlobalProductPhoto.objects.count(), 1)


class OffTests(Base):
    CODE = "4870000000012"  # валидный EAN-13

    def test_gtin(self):
        self.assertTrue(services.gtin_ok(self.CODE))
        self.assertFalse(services.gtin_ok("4870000000013"))
        self.assertTrue(services.gtin_ok("4870000000013", check_digit=False))
        self.assertFalse(services.gtin_ok("123"))
        self.assertFalse(services.gtin_ok("2000000000015"))  # внутренний код
        self.assertFalse(services.gtin_ok("48700000000AB"))

    def test_parse(self):
        p = services.parse_off_response
        self.assertEqual(p({"status": 1, "product": {"image_front_url": "https://a/f.jpg", "image_url": "https://a/i.jpg"}}), "https://a/f.jpg")
        self.assertEqual(p({"status": 1, "product": {"image_url": "https://a/i.jpg"}}), "https://a/i.jpg")
        self.assertIsNone(p({"status": 0, "status_verbose": "product not found"}))
        self.assertIsNone(p({"status": 1, "product": {}}))
        self.assertIsNone(p({"status": 1, "product": {"image_url": "javascript:x"}}))

    def _run(self, responses, **kw):
        sess = mock.Mock()
        sess.get.side_effect = responses
        return services.fill_off(session=sess, sleep=lambda s: None, **kw), sess

    def test_found_miss_and_headers(self):
        a = self.gp(self.CODE)
        b = self.gp("4870000000029")
        bad = self.gp("123")
        def by_url(url, **kw):
            if self.CODE in url:
                return FakeResp(200, {"status": 1, "product": {"image_front_url": "https://off/a.jpg"}})
            return FakeResp(404)
        stats, sess = self._run(by_url)
        self.assertEqual((stats["found"], stats["miss"], stats["skipped_invalid"]), (1, 1, 1))
        self.assertEqual(GlobalProductPhoto.objects.get(product=a).url, "https://off/a.jpg")
        self.assertEqual(GlobalProductPhoto.objects.get(product=b).url, "")
        self.assertFalse(GlobalProductPhoto.objects.filter(product=bad).exists())
        _, kwargs = sess.get.call_args
        self.assertEqual(kwargs["headers"]["User-Agent"], "NurCRM/1.0 (+https://nurcrm.kg)")
        self.assertEqual(kwargs["params"]["fields"], "code,image_front_url,image_url")
        self.assertNotIn("@", kwargs["headers"]["User-Agent"])

    def test_resume_skips_misses_and_rechecks_old(self):
        a = self.gp(self.CODE)
        self._run([FakeResp(404)])
        stats, sess = self._run([])  # ничего не должно запрашиваться
        self.assertEqual(stats["requests"], 0)
        GlobalProductPhoto.objects.filter(product=a).update(checked_at=timezone.now() - timedelta(days=40))
        stats, _ = self._run([FakeResp(200, {"status": 1, "product": {"image_url": "https://off/z.jpg"}})])
        self.assertEqual(stats["found"], 1)
        # и теперь с фото — больше не трогаем, даже с --recheck-days 0
        stats, _ = self._run([], recheck_days=0)
        self.assertEqual(stats["requests"], 0)

    def test_never_overwrites_store(self):
        g = self.gp(self.CODE)
        GlobalProductPhoto.objects.create(product=g, source="store", image_name="products/a/1.webp")
        stats, _ = self._run([])
        self.assertEqual(stats["requests"], 0)
        ph = GlobalProductPhoto.objects.get(product=g)
        self.assertEqual((ph.source, ph.image_name), ("store", "products/a/1.webp"))

    def test_rate_limit_and_network_error_not_recorded(self):
        self.gp(self.CODE)
        self.gp("4870000000029")
        stats, _ = self._run([FakeResp(429, headers={"Retry-After": "7"}), requests.ConnectionError("down")])
        self.assertEqual(stats["errors"], 2)
        self.assertEqual(GlobalProductPhoto.objects.count(), 0)

    def test_limit_and_dry_run(self):
        self.gp(self.CODE)
        self.gp("4870000000029")
        stats, sess = self._run([], dry_run=True)
        self.assertEqual((stats["candidates"], stats["requests"]), (2, 0))
        sess.get.assert_not_called()
        stats, _ = self._run([FakeResp(404)], limit=1)
        self.assertEqual(stats["requests"], 1)

    def test_pacing(self):
        self.gp(self.CODE)
        sleeps = []
        sess = mock.Mock()
        sess.get.return_value = FakeResp(404)
        services.fill_off(session=sess, sleep=sleeps.append, rate_per_min=60)
        self.assertTrue(sleeps and 0.5 < sleeps[0] <= 1.0)
