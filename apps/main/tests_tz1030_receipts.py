"""ТЗ 30.10 «Поступления»: фильтр приходов по товару, номер и накладная, партия строки, файл накладной."""
import tempfile
import uuid
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.main.models import Client, Product, SupplierReceipt, SupplierReceiptItem
from apps.main.tests_tz_n33_misc import N33Base
from apps.main.tz26.hw_batches.models import ProductBatch, ProductBatchReceiptLine

PDF = b"%PDF-1.4\n" + b"0" * 64
XLSX = b"PK\x03\x04" + b"0" * 64


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ReceiptsTabTests(N33Base):
    def setUp(self):
        super().setUp()
        self.supplier = Client.objects.create(full_name="Пост", phone="+996700111222",
                                              type=Client.StatusClient.SUPPLIERS, company=self.company)
        self.a = Product.objects.create(name="A", company=self.company, quantity=Decimal("0"),
                                        purchase_price=Decimal("1.00"), price=Decimal("5.00"))
        self.b = Product.objects.create(name="B", company=self.company, quantity=Decimal("0"),
                                        purchase_price=Decimal("1.00"), price=Decimal("5.00"))

    def receive(self, products, **extra):
        body = {"items": [{"product": str(p.id), "qty": 2, "purchase_price": "10.00"} for p in products], **extra}
        r = self.api.post(f"/api/main/suppliers/{self.supplier.id}/receipt/", body, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def rows(self, url):
        d = self.api.get(url).json()
        return d.get("results", d) if isinstance(d, dict) else d

    def test_number_and_document_fields(self):
        r1 = self.receive([self.a], document_number="НК-77", document_date="2026-10-29")
        r2 = self.receive([self.b])
        self.assertEqual((r1["number"], r2["number"]), (1, 2))
        self.assertEqual((r1["document_number"], r1["document_date"]), ("НК-77", "2026-10-29"))
        d = self.api.get(f"/api/main/suppliers/receipts/{r1['id']}/").json()
        self.assertEqual((d["number"], d["document_number"], d["document_date"]), (1, "НК-77", "2026-10-29"))
        self.assertEqual(d["created_by"], str(self.owner.id))
        self.assertRegex(d["created_at"], r"[+-]\d\d:\d\d$|Z$")  # время с часовым поясом

    def test_bad_document_date_400_nothing_written(self):
        body = {"items": [{"product": str(self.a.id), "qty": 2, "purchase_price": "10.00"}], "document_date": "29.10"}
        r = self.api.post(f"/api/main/suppliers/{self.supplier.id}/receipt/", body, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(SupplierReceipt.objects.exists())

    def test_filter_by_product_only_its_lines(self):
        self.receive([self.a, self.b])
        self.receive([self.b])
        self.receive([self.a])
        rows = self.rows(f"/api/main/suppliers/receipts/?product_id={self.a.id}")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual({it["product_id"] for it in row["items"]}, {str(self.a.id)})
        self.assertEqual(len(self.rows("/api/main/suppliers/receipts/")), 3)
        self.assertEqual(self.rows(f"/api/main/suppliers/receipts/?product_id={uuid.uuid4()}"), [])
        self.assertEqual(self.rows("/api/main/suppliers/receipts/?product_id=bad"), [])

    def test_batch_fields(self):
        rid = self.receive([self.a])["id"]
        ri = SupplierReceiptItem.objects.get(receipt_id=rid)
        batch = ProductBatch.objects.create(company=self.company, product=self.a, label="Л-5",
                                            received_total=Decimal("2"), remaining=Decimal("1.5"))
        ProductBatchReceiptLine.objects.create(batch=batch, receipt_item=ri, quantity=Decimal("2"))
        it = self.api.get(f"/api/main/suppliers/receipts/{rid}/").json()["items"][0]
        self.assertEqual((it["batch_id"], it["batch_number"], it["batch_remaining"]), (str(batch.id), "Л-5", "1.500"))
        rid2 = self.receive([self.b])["id"]
        it2 = self.api.get(f"/api/main/suppliers/receipts/{rid2}/").json()["items"][0]
        self.assertIsNone(it2["batch_id"])

    def test_invoice_file_upload_and_delete(self):
        rid = self.receive([self.a])["id"]
        url = f"/api/main/suppliers/receipts/{rid}/file/"
        r = self.api.post(url, {"file": SimpleUploadedFile("n.xlsx", XLSX)}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["attachments"][0]["content_type"],
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.api.post(url, {"file": SimpleUploadedFile("n.pdf", PDF)}, format="multipart")
        d = self.api.get(f"/api/main/suppliers/receipts/{rid}/").json()
        self.assertEqual(len(d["attachments"]), 2)
        self.assertEqual(d["file"], d["attachments"][0]["url"])
        bad = self.api.post(url, {"file": SimpleUploadedFile("x.exe", b"MZ" + b"0" * 30)}, format="multipart")
        self.assertEqual((bad.status_code, bad.json()["code"]), (400, "invalid_type"))
        mid = d["attachments"][0]["id"]
        r = self.api.delete(f"{url}{mid}/")
        self.assertEqual(len(r.json()["attachments"]), 1)

    def test_other_company_cannot_touch_file(self):
        rid = self.receive([self.a])["id"]
        from apps.main.tests_tz_n32_media_receipts import _mk_company
        from rest_framework.test import APIClient

        o2, _ = _mk_company("Чужой", "tz1030-rc-other")
        a2 = APIClient()
        a2.force_authenticate(o2)
        r = a2.post(f"/api/main/suppliers/receipts/{rid}/file/", {"file": SimpleUploadedFile("n.pdf", PDF)},
                    format="multipart")
        self.assertEqual(r.status_code, 404)
