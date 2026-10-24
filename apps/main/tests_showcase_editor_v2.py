"""
ТЗ-BE-2026-05 «редактор онлайн-витрины»: критерии приёмки 9.1–9.13 и эндпоинты п. 6.
"""
import io
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from apps.main.models import (
    Product,
    ProductCategory,
    ProductPromotionTier,
    ShowcaseBanner,
    ShowcaseDesign,
    ShowcaseDesignVersion,
    ShowcaseMedia,
    ShowcaseOrder,
    ShowcasePreviewToken,
    ShowcaseProductSettings,
    _cart_item_promotion,
    _money,
)
from apps.main.showcase import design_schema as ds
from apps.users.models import Company

User = get_user_model()

D = "/api/main/showcase/design/"
DRAFT = "/api/main/showcase/design/draft/"
PUBLISH = "/api/main/showcase/design/publish/"


def _img(width=800, height=600, fmt="JPEG", color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format=fmt)
    return buf.getvalue()


class ShowcaseEditorV2Base(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.pub = APIClient()
        self.owner = User.objects.create_user(email="o2@nur.test", password="x12345678", role="owner")
        self.company = Company.objects.create(
            name="NBS", slug="nbs-3", owner=self.owner, can_view_showcase=True, phones_howcase="996771830438",
        )
        self.owner.company = self.company
        self.owner.save()
        self.admin = User.objects.create_user(email="a2@nur.test", password="x12345678", role="admin", company=self.company)
        self.cashier = User.objects.create_user(email="c2@nur.test", password="x12345678", role="salesperson", company=self.company)
        self.cat = ProductCategory.objects.create(company=self.company, name="Фрукты")
        self.cat2 = ProductCategory.objects.create(company=self.company, name="Овощи")
        self.apple = Product.objects.create(company=self.company, category=self.cat, name="Яблоко", price=Decimal("100.00"))
        self.pear = Product.objects.create(company=self.company, category=self.cat, name="Груша", price=Decimal("80.00"))
        self.carrot = Product.objects.create(company=self.company, category=self.cat2, name="Морковь", price=Decimal("40.00"))
        self.api.force_authenticate(self.owner)

    def public_url(self, tail=""):
        return f"/api/main/public/companies/{self.company.slug}/{tail}"

    def public_design(self, **kw):
        return self.pub.get(self.public_url("showcase/design/"), **kw)

    def public_ids(self, params=None):
        res = self.pub.get(self.public_url("showcase/"), params or {})
        self.assertEqual(res.status_code, 200, res.content)
        return [p["id"] for p in res.data["results"]]

    def publish(self, key=None):
        kw = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        res = self.api.post(PUBLISH, **kw)
        self.assertEqual(res.status_code, 200, res.content)
        return res.data

    def upload(self, data=None, name="b.jpg", kind="banner", ctype="image/jpeg", key=None):
        f = SimpleUploadedFile(name, data if data is not None else _img(), content_type=ctype)
        kw = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.api.post("/api/main/showcase/media/", {"file": f, "kind": kind}, format="multipart", **kw)


class AcceptanceTests(ShowcaseEditorV2Base):
    # 9.1 Компания без настроек — витрина как сейчас
    def test_9_1_defaults_reproduce_current_look(self):
        res = self.public_design()
        self.assertEqual(res.status_code, 200)
        d = res.data
        c = d["theme"]["colors"]
        self.assertEqual((c["background"], c["header_bg"], c["accent"], c["accent_text"], c["surface"]),
                         ("#F5F6F8", "#FFFFFF", "#F7D74F", "#181F2B", "#FFFFFF"))
        self.assertEqual(c["badge_new_bg"], "#22C55E")
        self.assertEqual(d["hero"]["style"], "card")
        self.assertEqual(d["hero"]["title"], {"ru": "NBS"})
        self.assertEqual(d["header"]["name"], {"ru": "NBS"})
        self.assertEqual(d["categories"]["style"], "chips")
        self.assertEqual(d["products"]["columns"]["desktop"], 3)
        self.assertEqual(d["products"]["sort_options"], list(ds.SORT_OPTIONS))
        self.assertEqual(d["card"]["template"], "standard")
        self.assertEqual(d["card"]["price_position"], "photo_corner")
        self.assertEqual(d["card"]["new_badge_text"], {"ru": "НОВИНКА"})
        self.assertEqual(d["cart"]["style"], "drawer")
        self.assertEqual(d["cart"]["fields"]["phone"], "required")
        self.assertEqual(d["cart"]["order_channel"], "whatsapp")
        self.assertEqual(d["cart"]["whatsapp_phone"], "996771830438")
        self.assertFalse(d["footer"]["enabled"])
        self.assertEqual(d["banners"], [])
        self.assertEqual([s["type"] for s in d["sections"] if s["enabled"]], ["hero", "categories", "products"])
        # Редактор: черновик = значения по умолчанию, без предупреждений о контрасте
        res = self.api.get(D)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["draft"]["theme"], ds.default_theme())
        self.assertFalse(res.data["has_unpublished_changes"])
        self.assertEqual(ds.contrast_warnings(res.data["draft"]), [])
        # Публичный список товаров без настроек — как раньше (новые первыми)
        self.assertEqual(self.public_ids()[0], str(self.carrot.id))

    # 9.2 PATCH не меняет витрину до publish; publish — сразу
    def test_9_2_draft_invisible_until_publish(self):
        before = self.public_design()
        res = self.api.patch(DRAFT, {"theme": {"colors": {"accent": "#c8102e"}}, "card": {"template": "large"}}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["draft"]["theme"]["colors"]["accent"], "#C8102E")
        self.assertEqual(res.data["draft"]["theme"]["colors"]["background"], "#F5F6F8")  # слияние
        self.assertTrue(res.data["has_unpublished_changes"])
        self.assertEqual(self.public_design().data["theme"]["colors"]["accent"], "#F7D74F")
        self.publish()
        after = self.public_design()
        self.assertEqual(after.data["theme"]["colors"]["accent"], "#C8102E")
        self.assertEqual(after.data["card"]["template"], "large")
        self.assertNotEqual(before["ETag"], after["ETag"])
        self.assertFalse(self.api.get(D).data["has_unpublished_changes"])

    def test_patch_arrays_replaced_put_and_reset(self):
        self.api.patch(DRAFT, {"footer": {"phones": ["+996 555 111 222", "+996 700 000 000"]}}, format="json")
        res = self.api.patch(DRAFT, {"footer": {"phones": ["+996 555 999 999"]}}, format="json")
        self.assertEqual(res.data["draft"]["footer"]["phones"], ["+996 555 999 999"])
        res = self.api.patch(DRAFT, {"sections": [{"id": "a", "type": "products", "enabled": True},
                                                  {"id": "b", "type": "banners", "enabled": True, "place": "hero"}]}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([s["id"] for s in res.data["draft"]["sections"]], ["a", "b"])
        self.assertEqual(res.data["draft"]["sections"][1]["carousel"], ds.default_banners_carousel())
        # i18n сливается по языкам
        res = self.api.patch(DRAFT, {"hero": {"title": {"ky": "Дүкөн"}}}, format="json")
        self.assertEqual(res.data["draft"]["hero"]["title"], {"ru": "NBS", "ky": "Дүкөн"})
        # PUT — весь документ; не переданное — по умолчанию
        res = self.api.put(DRAFT, {"theme": {"radius": 4}}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["draft"]["theme"]["radius"], 4)
        self.assertEqual(res.data["draft"]["footer"]["phones"], [])
        self.assertEqual(res.data["draft"]["sections"], ds.default_document()["sections"])
        # reset раздела / всего
        self.api.patch(DRAFT, {"card": {"template": "list"}}, format="json")
        res = self.api.post("/api/main/showcase/design/draft/reset/", {"section": "theme"}, format="json")
        self.assertEqual(res.data["draft"]["theme"]["radius"], 14)
        self.assertEqual(res.data["draft"]["card"]["template"], "list")
        res = self.api.post("/api/main/showcase/design/draft/reset/", {"section": "all"}, format="json")
        self.assertEqual(res.data["draft"]["card"]["template"], "standard")
        res = self.api.post("/api/main/showcase/design/draft/reset/", {"section": "bogus"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["field"], "section")

    def test_apply_preset_and_contrast_warning(self):
        self.api.patch(DRAFT, {"card": {"template": "compact"}}, format="json")
        res = self.api.post("/api/main/showcase/design/draft/apply-preset/", {"preset": "kyrgyz"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["draft"]["theme"]["preset"], "kyrgyz")
        self.assertEqual(res.data["draft"]["theme"]["font"]["family"], "Noto Sans")
        self.assertEqual(res.data["draft"]["card"]["template"], "compact")  # только theme
        res = self.api.post("/api/main/showcase/design/draft/apply-preset/", {"preset": "neon"}, format="json")
        self.assertEqual((res.status_code, res.data["field"]), (400, "preset"))
        res = self.api.patch(DRAFT, {"theme": {"colors": {"text": "#AAAAAA"}}}, format="json")
        self.assertEqual(res.status_code, 200)
        w = [x for x in res.data["warnings"] if x["field"] == "theme.colors.text"]
        self.assertTrue(w and w[0]["code"] == "low_contrast" and w[0]["ratio"] < 4.5)
        self.assertEqual(res.data["draft"]["theme"]["colors"]["text"], "#AAAAAA")  # сохранено

    def test_editor_options(self):
        res = self.api.get("/api/main/showcase/editor/options/")
        self.assertEqual(res.status_code, 200)
        codes = [p["code"] for p in res.data["presets"]]
        for code in ("classic", "dark", "minimal", "fresh", "kyrgyz", "premium"):
            self.assertIn(code, codes)
        self.assertIn("Noto Sans", [f["family"] for f in res.data["fonts"]])
        self.assertIn("promo_blocks", res.data["section_types"])
        self.assertEqual(res.data["limits"]["banners"], 10)
        self.assertEqual(res.data["limits"]["pinned_products"], 24)

    # 9.3 Предпросмотр: без входа 24 ч, потом 410
    def test_9_3_preview_link(self):
        self.api.patch(DRAFT, {"theme": {"colors": {"accent": "#123456"}}}, format="json")
        res = self.api.post("/api/main/showcase/design/preview-link/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["url"].startswith("https://market.nurcrm.kg/catalog/nbs-3?preview="))
        token = res.data["url"].split("preview=")[1]
        prev = self.public_design(data={"preview": token})
        self.assertEqual(prev.status_code, 200)
        self.assertTrue(prev.data["preview"])
        self.assertEqual(prev.data["theme"]["colors"]["accent"], "#123456")
        self.assertEqual(self.public_design().data["theme"]["colors"]["accent"], "#F7D74F")
        ShowcasePreviewToken.objects.filter(token=token).update(expires_at=timezone.now() - timedelta(seconds=1))
        gone = self.public_design(data={"preview": token})
        self.assertEqual(gone.status_code, 410)
        self.assertEqual(gone.data["code"], "preview_expired")
        self.assertEqual(self.public_design(data={"preview": "nope"}).status_code, 404)

    # 9.4 restore версии n → publish → витрина как в версии n
    def test_9_4_restore_version(self):
        self.api.patch(DRAFT, {"theme": {"colors": {"accent": "#111111"}}}, format="json")
        b = self.api.post("/api/main/showcase/banners/", {"title": {"ru": "V2"}, "place": "hero"}, format="json")
        self.assertEqual(b.status_code, 201, b.content)
        v2 = self.publish()["version"]
        self.api.patch(DRAFT, {"theme": {"colors": {"accent": "#222222"}}}, format="json")
        self.api.delete(f"/api/main/showcase/banners/{b.data['id']}/")
        self.api.patch(f"/api/main/showcase/products/{self.apple.id}/", {"hidden": True}, format="json")
        self.publish()
        self.assertEqual(self.public_design().data["banners"], [])
        self.assertNotIn(str(self.apple.id), self.public_ids())
        res = self.api.post(f"/api/main/showcase/design/versions/{v2}/restore/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.public_design().data["theme"]["colors"]["accent"], "#222222")  # ещё не опубликовано
        self.publish()
        d = self.public_design().data
        self.assertEqual(d["theme"]["colors"]["accent"], "#111111")
        self.assertEqual([x["title"] for x in d["banners"]], [{"ru": "V2"}])
        self.assertIn(str(self.apple.id), self.public_ids())
        self.assertEqual(self.api.post("/api/main/showcase/design/versions/999/restore/").status_code, 404)

    def test_versions_keep_last_20_and_discard(self):
        for i in range(22):
            self.api.patch(DRAFT, {"theme": {"radius": i}}, format="json")
            self.publish()
        res = self.api.get("/api/main/showcase/design/versions/")
        self.assertEqual(len(res.data), 20)
        self.assertEqual(ShowcaseDesignVersion.objects.filter(company=self.company).count(), 20)
        self.assertEqual(res.data[0]["version"], 23)
        self.api.patch(DRAFT, {"theme": {"radius": 27}}, format="json")
        self.api.post("/api/main/showcase/banners/", {"title": "x"}, format="json")
        res = self.api.post("/api/main/showcase/design/discard/")
        self.assertEqual(res.data["draft"]["theme"]["radius"], 21)
        self.assertEqual(ShowcaseBanner.objects.filter(company=self.company).count(), 0)
        self.assertFalse(res.data["has_unpublished_changes"])

    # 9.5 Неверные значения — 400 с именем поля, документ не портится
    def test_9_5_validation_errors(self):
        cases = [
            ({"theme": {"colors": {"accent": "red"}}}, "theme.colors.accent"),
            ({"card": {"template": "mega"}}, "card.template"),
            ({"sections": [{"id": "x", "type": "slider"}]}, "sections[0].type"),
            ({"header": {"announcement": {"link": {"type": "url", "url": "javascript:alert(1)"}}}}, "header.announcement.link.url"),
            ({"footer": {"socials": {"instagram": "javascript:alert(1)"}}}, "footer.socials.instagram"),
            ({"theme": {"font": {"family": "Comic Sans"}}}, "theme.font.family"),
            ({"products": {"columns": {"desktop": 9}}}, "products.columns.desktop"),
            ({"theme": {"css": "body{}"}}, "theme.css"),
            ({"header": {"logo": "11111111-1111-1111-1111-111111111111"}}, "header.logo"),
            ({"hero": {"title": {"de": "x"}}}, "hero.title.de"),
            ({"cart": {"min_order_total": -1}}, "cart.min_order_total"),
        ]
        before = self.api.get(D).data["draft"]
        for payload, field in cases:
            res = self.api.patch(DRAFT, payload, format="json")
            self.assertEqual(res.status_code, 400, (payload, res.content))
            self.assertEqual(res.data["field"], field, payload)
            self.assertIn("detail", res.data)
            self.assertIn("code", res.data)
        self.assertEqual(self.api.get(D).data["draft"], before)
        ok = self.api.patch(DRAFT, {"header": {"announcement": {"enabled": True, "link": {"type": "url", "url": "https://wa.me/996771830438"}}}}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        ok = self.api.patch(DRAFT, {"hero": {"button": {"link": {"type": "category", "id": str(self.cat.id)}}}}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        bad = self.api.post("/api/main/showcase/banners/", {"link": {"type": "url", "url": "javascript:x"}}, format="json")
        self.assertEqual((bad.status_code, bad.data["field"]), (400, "link.url"))

    # 9.6 Права и тариф
    def test_9_6_rights_and_feature_gate(self):
        self.api.force_authenticate(self.cashier)
        for method, url, body in [
            ("patch", DRAFT, {"theme": {"radius": 1}}),
            ("post", PUBLISH, {}),
            ("post", "/api/main/showcase/banners/", {"title": "x"}),
            ("post", "/api/main/showcase/pages/", {"slug": "a", "title": "A"}),
            ("patch", f"/api/main/showcase/products/{self.apple.id}/", {"hidden": True}),
            ("post", "/api/main/showcase/design/draft/apply-preset/", {"preset": "dark"}),
        ]:
            res = getattr(self.api, method)(url, body, format="json")
            self.assertEqual(res.status_code, 403, url)
            self.assertEqual(res.data["code"], "permission_denied")
            self.assertTrue(res.data["detail"])
        self.api.force_authenticate(self.admin)
        self.assertEqual(self.api.patch(DRAFT, {"theme": {"radius": 1}}, format="json").status_code, 200)
        self.company.can_view_showcase = False
        self.company.save()
        self.api.force_authenticate(self.owner)
        res = self.api.patch(DRAFT, {"theme": {"radius": 2}}, format="json")
        self.assertEqual((res.status_code, res.data["code"]), (403, "feature_disabled"))
        self.assertEqual(self.api.get("/api/users/company/").data.get("features", []).count("showcase_editor"), 0)
        self.company.can_view_showcase = True
        self.company.save()
        self.assertIn("showcase_editor", self.api.get("/api/users/company/").data["features"])

    # 9.7 Скрытый товар не виден на витрине, но продаётся в кассе
    def test_9_7_hidden_product(self):
        res = self.api.patch(f"/api/main/showcase/products/{self.apple.id}/", {"hidden": True, "badge": "hit"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn(str(self.apple.id), self.public_ids())  # до publish витрина прежняя
        self.publish()
        self.assertNotIn(str(self.apple.id), self.public_ids())
        self.assertEqual(self.pub.get(self.public_url(f"showcase/{self.apple.id}/")).status_code, 404)
        # касса: товар не изменён, промо-расчёт кассы работает как обычно
        self.apple.refresh_from_db()
        self.assertEqual(self.apple.price, Decimal("100.00"))
        self.assertEqual(Product.objects.filter(company=self.company).count(), 3)
        # в заказ с витрины скрытый товар не попадает
        res = self.pub.post(self.public_url("orders/"), {
            "customer": {"phone": "+996555000111"}, "items": [{"product": str(self.apple.id), "qty": "1"}]}, format="json")
        self.assertEqual((res.status_code, res.data["code"]), (400, "product_hidden"))

    # 9.8 Баннер вне периода публично не отдаётся
    def test_9_8_banner_period(self):
        now = timezone.now()
        media = self.upload().data["id"]
        live = self.api.post("/api/main/showcase/banners/", {
            "title": {"ru": "Скидки"}, "image": media, "place": "inline", "inline_after_row": 2,
            "starts_at": (now - timedelta(days=1)).isoformat(), "ends_at": (now + timedelta(days=1)).isoformat()}, format="json")
        self.assertEqual(live.status_code, 201, live.content)
        self.api.post("/api/main/showcase/banners/", {"title": "Прошёл", "ends_at": (now - timedelta(hours=1)).isoformat()}, format="json")
        self.api.post("/api/main/showcase/banners/", {"title": "Будет", "starts_at": (now + timedelta(hours=1)).isoformat()}, format="json")
        self.api.post("/api/main/showcase/banners/", {"title": "Выключен", "active": False}, format="json")
        self.assertEqual(self.public_design().data["banners"], [])  # через publish
        self.publish()
        banners = self.public_design().data["banners"]
        self.assertEqual([b["title"] for b in banners], [{"ru": "Скидки"}])
        self.assertEqual(banners[0]["inline_after_row"], 2)
        self.assertEqual(banners[0]["image_mobile_urls"], banners[0]["image_urls"])  # без image_mobile — image
        self.assertIn(media, self.public_design().data["media"])
        bad = self.api.post("/api/main/showcase/banners/", {"starts_at": now.isoformat(), "ends_at": (now - timedelta(days=1)).isoformat()}, format="json")
        self.assertEqual((bad.status_code, bad.data["field"]), (400, "ends_at"))
        bad = self.api.post("/api/main/showcase/banners/", {"place": "footer"}, format="json")
        self.assertEqual((bad.status_code, bad.data["field"]), (400, "place"))

    @override_settings(SHOWCASE_LIMITS={"banners": 2})
    def test_banner_limit(self):
        for i in range(2):
            self.assertEqual(self.api.post("/api/main/showcase/banners/", {"title": str(i)}, format="json").status_code, 201)
        res = self.api.post("/api/main/showcase/banners/", {"title": "3"}, format="json")
        self.assertEqual((res.status_code, res.data["code"]), (400, "limit_exceeded"))

    # 9.9 Цена в блоке акции и в заказе = расчёт кассы
    def test_9_9_promo_prices_equal_kassa(self):
        self.apple.stock = True
        self.apple.save()
        ProductPromotionTier.objects.create(product=self.apple, min_amount=Decimal("0"), discount_percent=Decimal("15"))
        ProductPromotionTier.objects.create(product=self.apple, min_amount=Decimal("300"), discount_percent=Decimal("25"), position=1)
        res = self.api.post("/api/main/showcase/promo-blocks/", {
            "title": {"ru": "Акция недели"}, "source": {"type": "promotions", "ids": []}, "style": "carousel"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.api.post("/api/main/showcase/promo-blocks/", {"title": "Пусто", "source": {"type": "products", "ids": [str(self.carrot.id)]},
                                                          "active": False}, format="json")
        self.publish()
        blocks = self.public_design().data["promo_blocks"]
        self.assertEqual(len(blocks), 1)
        items = blocks[0]["items"]
        self.assertEqual([i["id"] for i in items], [str(self.apple.id)])
        kassa_disc, _ = _cart_item_promotion(self.apple, Decimal("100.00"), Decimal("1"))
        self.assertEqual(Decimal(items[0]["final_price"]), _money(Decimal("100.00") - kassa_disc))
        self.assertEqual(Decimal(items[0]["final_price"]), Decimal("85.00"))
        self.assertEqual(Decimal(items[0]["old_price"]), Decimal("100.00"))
        self.assertEqual(len(items[0]["tiers"]), 2)
        # заказ: 4 шт × 100 = 400 → ступень 25 % (как касса)
        kassa_disc4, _ = _cart_item_promotion(self.apple, Decimal("100.00"), Decimal("4"))
        res = self.pub.post(self.public_url("orders/"), {
            "customer": {"phone": "+996555000111"}, "items": [{"product": str(self.apple.id), "qty": "4"}]}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Decimal(res.data["total"]), _money(Decimal("400") - kassa_disc4))
        self.assertEqual(Decimal(res.data["total"]), Decimal("300.00"))
        quote = self.pub.post(self.public_url("showcase/cart/quote/"), {"items": [{"product": str(self.apple.id), "qty": "4"}]}, format="json")
        self.assertEqual(Decimal(quote.data["total"]), Decimal("300.00"))
        # Закончилась акция (сняли галочку) — блок скрывается сам
        self.apple.stock = False
        self.apple.save()
        cache.clear()
        self.assertEqual(self.public_design().data["promo_blocks"], [])

    # 9.10 Повтор publish / загрузки с тем же Idempotency-Key — без дублей
    def test_9_10_idempotency(self):
        a = self.publish("pub-1")
        b = self.publish("pub-1")
        self.assertEqual(a, b)
        self.assertEqual(ShowcaseDesignVersion.objects.filter(company=self.company).count(), 1)
        r1 = self.upload(key="up-1")
        r2 = self.upload(key="up-1")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.data["id"], r2.data["id"])
        self.assertEqual(ShowcaseMedia.objects.filter(company=self.company).count(), 1)

    # 9.11 ETag / If-None-Match → 304
    def test_9_11_etag_304(self):
        r = self.public_design()
        etag = r["ETag"]
        self.assertTrue(etag.startswith('"v1'))
        self.assertEqual(self.public_design(HTTP_IF_NONE_MATCH=etag).status_code, 304)
        self.api.patch(DRAFT, {"theme": {"radius": 3}}, format="json")
        self.assertEqual(self.public_design(HTTP_IF_NONE_MATCH=etag).status_code, 304)  # черновик не влияет
        self.publish()
        r2 = self.public_design(HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2["ETag"].startswith('"v2'))

    # 9.12 Картинки: лимит, не картинка, 409 при использовании
    def test_9_12_media(self):
        r = self.upload(_img(2400, 800), kind="banner")
        self.assertEqual(r.status_code, 201, r.content)
        for w in ("1920", "1080", "640", "320"):
            self.assertIn(w, r.data["urls"])
        self.assertEqual((r.data["width"], r.data["height"]), (2400, 800))
        logo = self.upload(_img(300, 100, "PNG"), name="l.png", kind="logo", ctype="image/png")
        self.assertEqual(logo.status_code, 201)
        for s in ("square_512", "square_192", "square_32"):
            self.assertIn(s, logo.data["urls"])
        bad = self.upload(b"not an image at all", name="x.jpg")
        self.assertEqual((bad.status_code, bad.data["code"], bad.data["field"]), (400, "invalid_image", "file"))
        gif = io.BytesIO()
        Image.new("RGB", (10, 10)).save(gif, format="GIF")
        bad = self.upload(gif.getvalue(), name="x.gif", ctype="image/gif")
        self.assertEqual((bad.status_code, bad.data["code"]), (400, "invalid_image_format"))
        with override_settings(SHOWCASE_LIMITS={"media_mb": 0}):
            big = self.upload()
        self.assertEqual((big.status_code, big.data["code"]), (400, "file_too_large"))
        self.assertIn("МБ", big.data["detail"])
        # SVG — только логотип, после очистки
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" onload="alert(1)"><script>alert(1)</script><rect width="10" height="10" fill="#f00"/></svg>'
        self.assertEqual(self.upload(svg, name="b.svg", kind="banner", ctype="image/svg+xml").status_code, 400)
        s = self.upload(svg, name="l.svg", kind="logo", ctype="image/svg+xml")
        self.assertEqual(s.status_code, 201, s.content)
        stored = ShowcaseMedia.objects.get(id=s.data["id"]).file.read()
        self.assertNotIn(b"script", stored)
        self.assertNotIn(b"onload", stored)
        self.assertIn(b"rect", stored)
        # список по kind
        kinds = {m["kind"] for m in self.api.get("/api/main/showcase/media/", {"kind": "logo"}).data}
        self.assertEqual(kinds, {"logo"})
        # используется в опубликованном виде → 409
        self.api.patch(DRAFT, {"header": {"logo": logo.data["id"]}}, format="json")
        self.publish()
        self.api.patch(DRAFT, {"header": {"logo": None}}, format="json")
        res = self.api.delete(f"/api/main/showcase/media/{logo.data['id']}/")
        self.assertEqual((res.status_code, res.data["code"], res.data["used_in"]), (409, "media_in_use", "published"))
        self.publish()
        self.assertEqual(self.api.delete(f"/api/main/showcase/media/{logo.data['id']}/").status_code, 204)
        self.assertEqual(self.api.delete(f"/api/main/showcase/media/{r.data['id']}/").status_code, 204)

    # 9.13 Тексты с <script> показываются как текст
    def test_9_13_texts_escaped(self):
        evil = '<script>alert(1)</script>Акция'
        res = self.api.patch(DRAFT, {"hero": {"title": {"ru": evil}}}, format="json")
        self.assertEqual(res.status_code, 200)
        self.publish()
        self.assertEqual(self.public_design().data["hero"]["title"]["ru"], evil)  # JSON-текст, не разметка
        page = self.api.post("/api/main/showcase/pages/", {
            "slug": "delivery", "title": {"ru": "Доставка"},
            "body": {"ru": '<p>Привет <b>мир</b><script>alert(1)</script><a href="javascript:x">ссылка</a>'
                           '<a href="https://wa.me/996">wa</a><img src=x onerror=alert(1)></p>'}}, format="json")
        self.assertEqual(page.status_code, 201, page.content)
        body = page.data["body"]["ru"]
        self.assertNotIn("<script", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("javascript:", body)
        self.assertNotIn("<img", body)
        self.assertIn("<b>мир</b>", body)
        self.assertIn('href="https://wa.me/996"', body)


class CatalogTests(ShowcaseEditorV2Base):
    def test_products_editor_list_bulk_order_and_public_ordering(self):
        res = self.api.get("/api/main/showcase/products/", {"search": "Яб"})
        self.assertEqual([r["product"] for r in res.data["results"]], [str(self.apple.id)])
        res = self.api.post("/api/main/showcase/products/bulk/", {"ids": [str(self.pear.id), str(self.carrot.id)], "hidden": True}, format="json")
        self.assertEqual((res.status_code, res.data["updated"]), (200, 2))
        res = self.api.get("/api/main/showcase/products/", {"hidden": "true"})
        self.assertEqual({r["product"] for r in res.data["results"]}, {str(self.pear.id), str(self.carrot.id)})
        self.api.post("/api/main/showcase/products/bulk/", {"ids": [str(self.pear.id), str(self.carrot.id)], "hidden": False}, format="json")
        # ручной порядок + закреплённый первым + бейдж
        self.api.post("/api/main/showcase/products/order/", {"order": [str(self.pear.id), str(self.apple.id), str(self.carrot.id)]}, format="json")
        self.api.patch(f"/api/main/showcase/products/{self.carrot.id}/", {"pinned": True, "badge": "sale"}, format="json")
        self.api.patch(DRAFT, {"products": {"default_sort": "manual"}}, format="json")
        self.publish()
        new = Product.objects.create(company=self.company, name="Новый", price=Decimal("1"))
        ids = self.public_ids()
        self.assertEqual(ids, [str(self.carrot.id), str(self.pear.id), str(self.apple.id), str(new.id)])  # новые — в конце
        self.assertEqual(self.public_ids({"ordering": "manual"}), ids)
        res = self.pub.get(self.public_url("showcase/"))
        first = res.data["results"][0]
        self.assertEqual((first["badge"], first["pinned"]), ("sale", True))
        self.assertEqual(self.public_ids({"ordering": "price_asc"})[0], str(new.id))  # коды сортировки документа
        self.assertEqual(self.public_ids({"pinned": "true"}), [str(self.carrot.id)])

    @override_settings(SHOWCASE_LIMITS={"pinned_products": 1})
    def test_pinned_limit(self):
        self.assertEqual(self.api.patch(f"/api/main/showcase/products/{self.apple.id}/", {"pinned": True}, format="json").status_code, 200)
        res = self.api.patch(f"/api/main/showcase/products/{self.pear.id}/", {"pinned": True}, format="json")
        self.assertEqual((res.status_code, res.data["code"]), (400, "limit_exceeded"))
        res = self.api.patch(f"/api/main/showcase/products/{self.pear.id}/", {"badge": "mega"}, format="json")
        self.assertEqual((res.status_code, res.data["field"]), (400, "badge"))

    def test_public_filters_is_new_on_sale_category(self):
        Product.objects.filter(id=self.pear.id).update(created_at=timezone.now() - timedelta(days=30))
        Product.objects.filter(id=self.apple.id).update(discount_percent=Decimal("10"))
        ids = self.public_ids({"is_new": "true"})
        self.assertNotIn(str(self.pear.id), ids)
        self.assertIn(str(self.apple.id), ids)
        self.assertEqual(self.public_ids({"on_sale": "true"}), [str(self.apple.id)])
        self.assertEqual(set(self.public_ids({"category": str(self.cat2.id)})), {str(self.carrot.id)})
        self.api.patch(DRAFT, {"card": {"new_badge_days": 60}}, format="json")
        self.publish()
        res = self.pub.get(self.public_url(f"showcase/{self.pear.id}/"))
        self.assertTrue(res.data["is_new"])
        self.assertIn(str(self.pear.id), self.public_ids({"is_new": "true"}))

    def test_categories_hidden_order_override(self):
        media = self.upload(kind="category").data["id"]
        res = self.api.patch(f"/api/main/showcase/categories/{self.cat2.id}/",
                             {"title_override": {"ru": "Свежие овощи"}, "image": media}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["products_count"], 1)
        self.api.post("/api/main/showcase/categories/order/", {"order": [str(self.cat2.id), str(self.cat.id)]}, format="json")
        rows = self.api.get("/api/main/showcase/categories/").data
        self.assertEqual([r["category"] for r in rows], [str(self.cat2.id), str(self.cat.id)])
        self.assertEqual(rows[1]["products_count"], 2)
        self.api.patch(f"/api/main/showcase/categories/{self.cat.id}/", {"hidden": True}, format="json")
        self.assertEqual(self.api.get(D).data["draft"]["categories"]["hidden"], [str(self.cat.id)])
        self.publish()
        self.assertEqual(self.public_ids(), [str(self.carrot.id)])
        pubcats = self.pub.get(self.public_url("showcase/categories/")).data
        self.assertEqual([c["id"] for c in pubcats], [str(self.cat2.id)])
        self.assertEqual(pubcats[0]["title"], {"ru": "Свежие овощи"})
        self.assertTrue(pubcats[0]["image_urls"])
        d = self.public_design().data
        self.assertEqual(d["categories"]["hidden"], [str(self.cat.id)])
        self.assertEqual(d["categories"]["order"], [str(self.cat2.id), str(self.cat.id)])

    def test_pages_crud_and_public(self):
        r = self.api.post("/api/main/showcase/pages/", {"slug": "about", "title": "О нас", "body": "Строка 1\nСтрока 2"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["body"]["ru"], "<p>Строка 1<br>Строка 2</p>")
        dup = self.api.post("/api/main/showcase/pages/", {"slug": "about", "title": "x"}, format="json")
        self.assertEqual((dup.status_code, dup.data["field"]), (400, "slug"))
        bad = self.api.post("/api/main/showcase/pages/", {"slug": "О нас", "title": "x"}, format="json")
        self.assertEqual((bad.status_code, bad.data["field"]), (400, "slug"))
        self.assertEqual(self.pub.get(self.public_url("showcase/pages/about/")).status_code, 404)
        self.publish()
        self.assertEqual(self.pub.get(self.public_url("showcase/pages/about/")).data["title"], {"ru": "О нас"})
        self.assertEqual(self.public_design().data["pages"][0]["slug"], "about")
        pid = r.data["id"]
        self.assertEqual(self.api.patch(f"/api/main/showcase/pages/{pid}/", {"show_in_footer": False}, format="json").data["show_in_footer"], False)
        self.assertEqual(self.api.delete(f"/api/main/showcase/pages/{pid}/").status_code, 204)

    def test_slug_check_and_redirect(self):
        other_owner = User.objects.create_user(email="x3@nur.test", password="x12345678")
        Company.objects.create(name="Other", slug="taken-shop", owner=other_owner)
        g = lambda s: self.api.get("/api/main/showcase/slug-check/", {"slug": s}).data
        self.assertTrue(g("free-shop")["available"])
        self.assertFalse(g("taken-shop")["available"])
        self.assertTrue(g("nbs-3")["available"])
        self.assertEqual(g("A b")["reason"], "invalid_format")
        self.company.slug = "nbs-new"
        self.company.save()
        res = self.pub.get("/api/main/public/companies/nbs-3/showcase/design/")
        self.assertEqual(res.status_code, 301)
        self.assertEqual(res.data["code"], "slug_moved")
        self.assertEqual(res.data["slug"], "nbs-new")
        self.assertIn("/companies/nbs-new/showcase/design/", res["Location"])
        self.assertEqual(self.pub.get("/api/main/public/companies/nbs-3/showcase/").status_code, 301)
        self.assertEqual(self.pub.get("/api/main/public/companies/nbs-3/").status_code, 301)
        order = self.pub.post("/api/main/public/companies/nbs-3/orders/", {
            "customer": {"phone": "+996555000111"}, "items": [{"product": str(self.apple.id), "qty": "1"}]}, format="json")
        self.assertEqual(order.status_code, 201, order.content)
        self.assertTrue(g("nbs-3")["available"])  # свой старый slug можно вернуть
        other_admin = User.objects.create_user(email="x4@nur.test", password="x12345678", role="owner")
        other = Company.objects.create(name="Third", slug="third-shop", owner=other_admin, can_view_showcase=True)
        other_admin.company = other
        other_admin.save()
        self.api.force_authenticate(other_admin)
        self.assertEqual(g("nbs-3")["reason"], "reserved")  # чужой — занят редиректом 90 дней
        self.assertEqual(self.pub.get("/api/main/public/companies/unknown-slug/showcase/design/").status_code, 404)


class OrdersEventsTests(ShowcaseEditorV2Base):
    def order(self, body, key=None):
        kw = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.pub.post(self.public_url("orders/"), body, format="json", **kw)

    def test_order_idempotency_whatsapp_webhook(self):
        body = {"customer": {"name": "Айбек", "phone": "+996555123456"}, "items": [{"product": str(self.apple.id), "qty": "2"}]}
        with mock.patch("apps.main.showcase.views_public_design.emit_event") as emit:
            r1 = self.order(body, key="o-1")
            r2 = self.order(body, key="o-1")
        self.assertEqual(r1.status_code, 201, r1.content)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.data["id"], r2.data["id"])
        self.assertEqual(ShowcaseOrder.objects.filter(company=self.company).count(), 1)
        self.assertEqual(r1.data["status"], "new")
        self.assertEqual(r1.data["total"], "200.00")
        self.assertTrue(r1.data["whatsapp_url"].startswith("https://wa.me/996771830438?text="))
        emit.assert_called_once()
        self.assertEqual(emit.call_args[0][1], "order.created")

    def test_order_rules_min_total_delivery_spam(self):
        self.api.patch(DRAFT, {"cart": {"min_order_total": 150, "fields": {"name": "required"},
                                        "delivery": {"delivery": True, "delivery_fee": 50, "free_from": 300}}}, format="json")
        self.publish()
        r = self.order({"customer": {"phone": "+996555123456"}, "items": [{"product": str(self.apple.id), "qty": "2"}]})
        self.assertEqual((r.status_code, r.data["field"]), (400, "customer.name"))
        r = self.order({"customer": {"name": "A", "phone": "+996555123456"}, "items": [{"product": str(self.apple.id), "qty": "1"}]})
        self.assertEqual((r.status_code, r.data["code"]), (400, "min_order_total"))
        r = self.order({"customer": {"name": "A", "phone": "+996555123456"}, "items": [{"product": str(self.apple.id), "qty": "2"}],
                        "delivery": {"type": "delivery"}})
        self.assertEqual((r.status_code, r.data["field"]), (400, "delivery.address"))
        r = self.order({"customer": {"name": "A", "phone": "+996555123456"}, "items": [{"product": str(self.apple.id), "qty": "2"}],
                        "delivery": {"type": "delivery", "address": "Бишкек"}})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.data["total"], r.data["delivery_fee"]), ("250.00", "50.00"))
        r = self.order({"customer": {"name": "A", "phone": "+996555123456"}, "items": [{"product": str(self.apple.id), "qty": "3"}],
                        "delivery": {"type": "delivery", "address": "Бишкек"}})
        self.assertEqual(r.data["delivery_fee"], "0.00")
        r = self.order({"customer": {"name": "A", "phone": "+996555123456"}, "website": "http://spam",
                        "items": [{"product": str(self.apple.id), "qty": "2"}]})
        self.assertEqual((r.status_code, r.data["code"]), (400, "spam"))

    @override_settings(SHOWCASE_ORDER_RATE=(2, 600))
    def test_order_rate_limit(self):
        body = {"customer": {"phone": "+996555123456"}, "items": [{"product": str(self.apple.id), "qty": "1"}]}
        self.assertEqual(self.order(body).status_code, 201)
        self.assertEqual(self.order(body).status_code, 201)
        r = self.order(body)
        self.assertEqual((r.status_code, r.data["code"]), (429, "throttled"))

    def test_variant_order_reserves_stock(self):
        from apps.main.models import ProductVariant

        v = ProductVariant.objects.create(company=self.company, product=self.pear, size="M", color="red",
                                          quantity=Decimal("3"), price=Decimal("90.00"))
        r = self.order({"customer": {"phone": "+996555123456"}, "items": [{"product": str(self.pear.id), "qty": "1"}]})
        self.assertEqual((r.status_code, r.data["code"]), (400, "variant_required"))
        r = self.order({"customer": {"phone": "+996555123456"}, "items": [{"product": str(self.pear.id), "variant": str(v.id), "qty": "5"}]})
        self.assertEqual((r.status_code, r.data["code"]), (409, "variant_out_of_stock"))
        self.assertEqual(ShowcaseOrder.objects.filter(company=self.company).count(), 0)
        r = self.order({"customer": {"phone": "+996555123456"}, "items": [{"product": str(self.pear.id), "variant": str(v.id), "qty": "2"}]})
        self.assertEqual(r.status_code, 201, r.content)
        v.refresh_from_db()
        self.assertEqual(v.quantity, Decimal("1"))
        self.api.patch(f"/api/main/showcase/orders/{r.data['id']}/", {"status": "canceled"}, format="json")
        v.refresh_from_db()
        self.assertEqual(v.quantity, Decimal("3"))

    def test_orders_list_and_status(self):
        r = self.order({"customer": {"phone": "+996555123456"}, "items": [{"product": str(self.apple.id), "qty": "1"}]})
        self.api.force_authenticate(self.cashier)
        lst = self.api.get("/api/main/showcase/orders/", {"status": "new", "date_from": timezone.localdate().isoformat()})
        self.assertEqual(len(lst.data), 1)
        for st in ("accepted", "ready", "done"):
            res = self.api.patch(f"/api/main/showcase/orders/{r.data['id']}/", {"status": st}, format="json")
            self.assertEqual(res.data["status"], st)
        res = self.api.patch(f"/api/main/showcase/orders/{r.data['id']}/", {"status": "new"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.api.get("/api/main/showcase/orders/", {"status": "lost"}).status_code, 400)

    def test_events_and_stats(self):
        banner = ShowcaseBanner.objects.create(company=self.company, title="B")
        url = self.public_url("showcase/events/")
        self.assertEqual(self.pub.post(url, {"type": "view"}, format="json").data["status"], "ok")
        self.pub.post(url, {"type": "product_view", "id": str(self.apple.id)}, format="json")
        self.pub.post(url, {"type": "add_to_cart", "id": str(self.apple.id)}, format="json")
        self.assertEqual(self.pub.post(url, {"type": "banner_click", "id": str(banner.id), "session": "s1"}, format="json").data["status"], "ok")
        self.assertEqual(self.pub.post(url, {"type": "banner_click", "id": str(banner.id), "session": "s1"}, format="json").data["status"], "already_tracked")
        self.pub.post(url, {"type": "banner_click", "id": str(banner.id), "session": "s2"}, format="json")
        bad = self.pub.post(url, {"type": "purchase"}, format="json")
        self.assertEqual((bad.status_code, bad.data["field"]), (400, "type"))
        self.order({"customer": {"phone": "+996555123456"}, "items": [{"product": str(self.apple.id), "qty": "1"}]})
        st = self.api.get("/api/main/showcase/stats/", {"date_from": timezone.localdate().isoformat()}).data
        self.assertEqual((st["views"], st["add_to_cart"], st["orders"]), (1, 1, 1))
        self.assertEqual(st["product_views"][0]["views"], 1)
        self.assertEqual(st["banner_clicks"], [{"id": str(banner.id), "title": {"ru": "B"}, "clicks": 2}])


class LegacyCompatTests(ShowcaseEditorV2Base):
    def test_legacy_document_upgraded(self):
        from apps.main.models import get_legacy_default_showcase_design

        legacy = get_legacy_default_showcase_design()
        legacy["layout"]["hidden_products"] = [str(self.apple.id)]
        legacy["theme"]["accent"] = "#00AA00"
        ShowcaseDesign.objects.create(company=self.company, draft=legacy, published=legacy, version=3)
        self.assertNotIn(str(self.apple.id), self.public_ids())
        d = self.public_design().data
        self.assertEqual(d["theme"]["colors"]["accent"], "#00AA00")
        self.assertEqual(d["theme"]["colors"]["background"], "#F5F6F8")  # старые дефолты → текущий вид
        self.assertEqual(d["card"]["template"], "standard")
        draft = self.api.get(D).data["draft"]
        self.assertIn("colors", draft["theme"])
        self.assertNotIn("layout", draft)
        self.assertTrue(ShowcaseProductSettings.objects.get(product=self.apple).hidden)

    def test_legacy_patch_payload_still_accepted(self):
        res = self.api.patch(DRAFT, {"layout": {"columns": {"desktop": 4, "mobile": 2}, "hidden_products": [str(self.pear.id)]},
                                     "cards": {"template": "compact"}, "carousel": {"interval_s": 7}}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["draft"]["products"]["columns"]["desktop"], 4)
        self.assertEqual(res.data["draft"]["card"]["template"], "compact")
        self.publish()
        self.assertNotIn(str(self.pear.id), self.public_ids())
        old = self.pub.post(self.public_url("showcase/track/"), {"event": "view"}, format="json")
        self.assertEqual(old.data["status"], "ok")
