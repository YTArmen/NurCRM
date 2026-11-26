"""ТЗ 30.10 №9: публичная ссылка на электронный чек (share-link, /r/<token>)."""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.models import Cashbox
from apps.main.models import Client, Sale, SaleItem, SalePayment, SaleReceiptLink
from apps.main.tests_tz_n32_media_receipts import LOCMEM, _mk_company


@override_settings(CACHES=LOCMEM, RECEIPT_SHARE_URL_TEMPLATE="https://app.nurcrm.kg/r/{token}")
class ShareLinkTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner, self.company = _mk_company("Магазин 30.10", "tz1030-r")
        self.company.address = "Бишкек, Чуй 1"
        self.company.phone = "+996700000001"
        self.company.save()
        self.owner.first_name = "Айгуль"
        self.owner.save()
        cb = Cashbox.objects.create(company=self.company, name="Касса")
        cl = Client.objects.create(company=self.company, full_name="Секретный Клиент", phone="+996555000111")
        self.sale = Sale.objects.create(company=self.company, cashbox=cb, user=self.owner, client=cl,
                                        status=Sale.Status.PAID, subtotal=Decimal("100.00"),
                                        total=Decimal("100.00"), paid_at=timezone.now(), payment_method="cash")
        SaleItem.objects.create(company=self.company, sale=self.sale, name_snapshot="Хлеб",
                                unit_price=Decimal("50.00"), quantity=Decimal("2"))
        SalePayment.objects.create(company=self.company, sale=self.sale, method="cash", amount=Decimal("100.00"))
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.pub = APIClient()

    def share(self):
        return self.api.post(f"/api/main/pos/sales/{self.sale.id}/share-link/")

    def test_acceptance_create_open_repeat_revoke(self):
        a = self.share()
        self.assertEqual(a.status_code, 200, a.content)
        d = a.json()
        self.assertEqual(set(d), {"url", "token", "expires_at", "pdf_url", "created"})
        self.assertTrue(d["created"])
        self.assertEqual(d["url"], f"https://app.nurcrm.kg/r/{d['token']}")
        self.assertEqual(d["pdf_url"], d["url"] + ".pdf")
        self.assertGreaterEqual(len(d["token"]), 22)  # ≥128 бит
        exp = timezone.datetime.fromisoformat(d["expires_at"])
        self.assertAlmostEqual((exp - timezone.now()).days, 89, delta=1)

        page = self.pub.get(f"/r/{d['token']}", HTTP_ACCEPT_LANGUAGE="ru")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        for s in ("Магазин 30.10", "Бишкек, Чуй 1", "+996700000001", "Хлеб", "100.00", "Айгуль", "Оплачен",
                  "Скачать PDF", "window.print()"):
            self.assertIn(s, html)
        self.assertNotIn("Секретный", html)
        self.assertNotIn("996555000111", html)
        self.assertIn("noindex", page["X-Robots-Tag"])

        b = self.share().json()
        self.assertEqual((b["token"], b["created"]), (d["token"], False))

        self.assertEqual(self.api.delete(f"/api/main/pos/sales/{self.sale.id}/share-link/").json(), {"revoked": True})
        self.assertEqual(self.pub.get(f"/r/{d['token']}").status_code, 404)
        self.assertEqual(self.pub.get(f"/api/public/receipts/{d['token']}/").status_code, 404)

        c = self.share().json()  # после отзыва — новая ссылка, старая не оживает
        self.assertTrue(c["created"])
        self.assertNotEqual(c["token"], d["token"])
        self.assertEqual(self.pub.get(f"/r/{d['token']}").status_code, 404)

    def test_pdf_and_json(self):
        tok = self.share().json()["token"]
        r = self.pub.get(f"/r/{tok}.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"%PDF"))
        j = self.pub.get(f"/api/public/receipts/{tok}/").json()
        self.assertEqual(j["status"], "paid")
        self.assertEqual(j["cashier"], "Айгуль")
        self.assertEqual(j["client"], "С.К.")
        self.assertEqual(j["company"]["address"], "Бишкек, Чуй 1")

    def test_languages(self):
        tok = self.share().json()["token"]
        for lang, word in (("ky", "Жыйынтык"), ("uz", "Jami"), ("tr", "Toplam"), ("en", "Total")):
            r = self.pub.get(f"/r/{tok}", HTTP_ACCEPT_LANGUAGE=f"{lang}-XX,{lang};q=0.9")
            self.assertIn(word, r.content.decode(), lang)
            self.assertEqual(r["Content-Language"], lang)
        self.assertIn("Итого", self.pub.get(f"/r/{tok}", HTTP_ACCEPT_LANGUAGE="de").content.decode())

    def test_expired_404(self):
        tok = self.share().json()["token"]
        SaleReceiptLink.objects.filter(token=tok).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.pub.get(f"/r/{tok}").status_code, 404)

    def test_unknown_token_and_other_company(self):
        self.assertEqual(self.pub.get("/r/" + "x" * 32).status_code, 404)
        o2, _ = _mk_company("Чужой", "tz1030-other")
        a2 = APIClient()
        a2.force_authenticate(o2)
        self.assertEqual(a2.post(f"/api/main/pos/sales/{self.sale.id}/share-link/").status_code, 404)
        self.assertEqual(a2.delete(f"/api/main/pos/sales/{self.sale.id}/share-link/").status_code, 404)

    def test_share_url_in_sale(self):
        self.assertIsNone(self.api.get(f"/api/main/pos/sales/{self.sale.id}/").json()["share_url"])
        url = self.share().json()["url"]
        self.assertEqual(self.api.get(f"/api/main/pos/sales/{self.sale.id}/").json()["share_url"], url)
        rows = self.api.get("/api/main/pos/sales/").json()
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        self.assertEqual([r["share_url"] for r in rows if r["id"] == str(self.sale.id)], [url])

    def test_partial_return_status(self):
        tok = self.share().json()["token"]
        self.sale.status = Sale.Status.PARTIALLY_RETURNED
        self.sale.save(update_fields=["status"])
        self.assertIn("Частично возвращён", self.pub.get(f"/r/{tok}").content.decode())

    def test_robots(self):
        r = self.pub.get("/robots.txt")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Disallow: /r/", r.content.decode())
