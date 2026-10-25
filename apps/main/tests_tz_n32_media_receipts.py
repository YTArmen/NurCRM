"""ТЗ Н-32: медиа-хранилище (main/media/) и электронный чек (public-link, public/receipts)."""
import io
import tempfile
import uuid
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main import media_receipts as mr
from apps.main.models import Client, MainMedia, Sale, SaleItem, SalePayment
from apps.users.models import Company, SubscriptionPlan

User = get_user_model()
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "n32"}}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64


def _mk_company(name, slug):
    owner = User.objects.create_user(email=f"o_{uuid.uuid4().hex[:6]}@t.com", password="p", role="owner")
    plan = SubscriptionPlan.objects.create(name="Pro", price=Decimal("100.00"))
    company = Company.objects.create(name=name, slug=slug, owner=owner, subscription_plan=plan)
    owner.company = company
    owner.save()
    return owner, company


@override_settings(CACHES=LOCMEM, MEDIA_ROOT=tempfile.mkdtemp())
class MediaTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner, self.company = _mk_company("N32", "n32-shop")
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def up(self, data=PNG, name="a.png", purpose="product", ctype="image/png"):
        return self.api.post("/api/main/media/", {"file": SimpleUploadedFile(name, data, content_type=ctype), "purpose": purpose}, format="multipart")

    def test_upload_and_delete(self):
        r = self.up()
        self.assertEqual(r.status_code, 201, r.content)
        d = r.json()
        self.assertEqual(set(d), {"id", "url", "size", "content_type", "sha256", "created_at"})
        self.assertEqual(d["content_type"], "image/png")
        self.assertEqual(len(d["sha256"]), 64)
        self.assertEqual(self.api.delete(f"/api/main/media/{d['id']}/").status_code, 204)
        self.assertFalse(MainMedia.objects.exists())

    def test_too_large_413(self):
        r = self.up(PNG + b"0" * (6 * 1024 * 1024))
        self.assertEqual((r.status_code, r.json()["code"]), (413, "too_large"))

    def test_magic_bytes_not_extension(self):
        r = self.up(b"<?php echo 1;", name="a.png", ctype="image/png")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "invalid_type"))

    def test_pdf_only_for_invoice(self):
        self.assertEqual(self.up(PDF, "a.pdf", "product", "application/pdf").status_code, 400)
        self.assertEqual(self.up(PDF, "a.pdf", "invoice", "application/pdf").status_code, 201)

    def test_quota(self):
        with override_settings(COMPANY_MEDIA_QUOTA_MB=0):
            r = self.up()
        self.assertEqual((r.status_code, r.json()["code"]), (413, "quota_exceeded"))

    def test_other_company_cannot_delete(self):
        mid = self.up().json()["id"]
        o2, _ = _mk_company("Other", "other-shop")
        a2 = APIClient()
        a2.force_authenticate(o2)
        self.assertEqual(a2.delete(f"/api/main/media/{mid}/").status_code, 404)


@override_settings(CACHES=LOCMEM)
class ReceiptTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner, self.company = _mk_company("Магазин Н32", "n32-r")
        cb = Cashbox.objects.create(company=self.company, name="Касса")
        cl = Client.objects.create(company=self.company, full_name="Секретный Клиент", phone="+996555000111")
        self.sale = Sale.objects.create(company=self.company, cashbox=cb, user=self.owner, client=cl, status=Sale.Status.PAID,
                                        subtotal=Decimal("100.00"), total=Decimal("100.00"), paid_at=timezone.now(),
                                        payment_method="cash")
        SaleItem.objects.create(company=self.company, sale=self.sale, name_snapshot="Хлеб", unit_price=Decimal("50.00"), quantity=Decimal("2"))
        SalePayment.objects.create(company=self.company, sale=self.sale, method="cash", amount=Decimal("100.00"))
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.pub = APIClient()

    def link(self):
        return self.api.post(f"/api/main/pos/sales/{self.sale.id}/public-link/")

    def test_link_idempotent_and_long(self):
        a, b = self.link(), self.link()
        self.assertEqual(a.json()["token"], b.json()["token"])
        self.assertGreaterEqual(len(a.json()["token"]), 32)
        self.assertIn(a.json()["token"], a.json()["url"])

    def test_public_no_auth_no_customer_data(self):
        tok = self.link().json()["token"]
        r = self.pub.get(f"/api/public/receipts/{tok}/")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.content.decode()
        self.assertNotIn("Секретный", body)
        self.assertNotIn("996555000111", body)
        d = r.json()
        self.assertEqual(d["company"]["name"], "Магазин Н32")
        self.assertEqual(d["items"][0]["name"], "Хлеб")
        self.assertEqual(d["totals"]["total"], "100.00")
        self.assertEqual(d["payments"][0]["amount"], "100.00")
        self.assertTrue(d["number"])

    def test_unknown_token_404(self):
        self.assertEqual(self.pub.get("/api/public/receipts/" + "x" * 32 + "/").status_code, 404)

    def test_pdf(self):
        tok = self.link().json()["token"]
        r = self.pub.get(f"/api/public/receipts/{tok}/pdf/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_rating_once(self):
        tok = self.link().json()["token"]
        url = f"/api/public/receipts/{tok}/rating/"
        self.assertEqual(self.pub.post(url, {"score": 6}, format="json").status_code, 400)
        self.assertEqual(self.pub.post(url, {"score": 5, "comment": "ок"}, format="json").status_code, 201)
        r = self.pub.post(url, {"score": 4}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "already_rated"))
        self.assertEqual(self.pub.get(f"/api/public/receipts/{tok}/").json()["rating"]["score"], 5)

    def test_throttle_per_ip(self):
        tok = self.link().json()["token"]
        with mock.patch.object(mr.PublicReceiptThrottle, "rate", "2/minute"):
            codes = [self.pub.get(f"/api/public/receipts/{tok}/").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
