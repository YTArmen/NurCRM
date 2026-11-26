"""Приложение клиентов (/api/v1/): этапы 1–5 ТЗ «API NurCRM для приложения клиентов»."""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.main.models import Client, ClientBonusTransaction, Product, ProductPromotionTier, PromoRule
from apps.main.phone_utils import normalize_phone_e164
from apps.main.tests_kassa_api import KassaBase
from apps.users.models import Branch, Company, User

from .models import (
    AppCustomer,
    AppPushToken,
    AppQrToken,
    AppShopSettings,
    AppToken,
    Referral,
    TelegramAuthNonce,
)
from .push import bonus_message, format_points, push_for_sale, sale_message

BOT = dict(
    CLIENT_APP_TELEGRAM_BOT_TOKEN="123:abc",
    CLIENT_APP_TELEGRAM_WEBHOOK_SECRET="hook-secret",
    CLIENT_APP_BOT_USERNAME="nurcrm_login_bot",
)
WEBHOOK = "/api/v1/auth/telegram/webhook/"


def run_now(task, *args):
    return task(*args)


def run_later_now(task, countdown, *args):
    return task(*args)


class AppTestMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.app = APIClient()
        self.tg = mock.patch("apps.clientapp.telegram.api_call", return_value={"ok": True})
        self.tg_mock = self.tg.start()
        self.addCleanup(self.tg.stop)

    def make_customer(self, phone="+996555000222", name="Айбек"):
        c = AppCustomer.objects.create(phone=phone, full_name=name)
        c.ensure_referral_code()
        _, raw = AppToken.issue(c)
        return c, raw

    def authed(self, raw):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        return api

    def hook(self, update, secret="hook-secret"):
        return self.app.post(WEBHOOK, update, format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret)

    @staticmethod
    def msg(from_id, text=None, contact=None):
        m = {"message_id": 1, "from": {"id": from_id, "is_bot": False}, "chat": {"id": from_id, "type": "private"}}
        if text is not None:
            m["text"] = text
        if contact is not None:
            m["contact"] = contact
        return {"update_id": 1, "message": m}


# ======================================================================
# Нормализация телефонов и поиск кассой (вопрос 6)
# ======================================================================


class PhoneTests(AppTestMixin, KassaBase):
    def test_normalizer(self):
        for raw in ("+996 555 000 222", "0555000222", "996555000222", "555000222", "+996(555)00-02-22", "00996555000222"):
            self.assertEqual(normalize_phone_e164(raw), "+996555000222", raw)
        self.assertEqual(normalize_phone_e164("8 701 123 45 67"), "+77011234567")
        self.assertEqual(normalize_phone_e164("123"), "")

    def test_client_phone_normalized_on_save_and_kassa_search(self):
        c = Client.objects.create(company=self.company, full_name="Нурлан", phone="0700 12-34-56")
        self.assertEqual(c.phone_normalized, "+996700123456")
        r = self.api.get("/api/main/clients/", {"search": "+996700123456"})
        ids = [x["id"] for x in (r.data["results"] if isinstance(r.data, dict) else r.data)]
        self.assertIn(str(c.id), ids)

    def test_by_phone_endpoint_company_scoped(self):
        other_owner = User.objects.create_user(email=f"x{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        other = Company.objects.create(name="Other", owner=other_owner, subscription_plan=self.plan)
        Client.objects.create(company=other, full_name="Чужой", phone="+996555000222")
        r = self.api.get("/api/main/clients/by-phone/", {"phone": "0555 000 222"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["client"]["id"], str(self.client_obj.id))
        self.assertEqual(len(r.data["results"]), 1)
        self.assertEqual(self.api.get("/api/main/clients/by-phone/", {"phone": "12"}).status_code, 400)
        self.assertEqual(APIClient().get("/api/main/clients/by-phone/", {"phone": "0555000222"}).status_code, 401)

    def test_by_phone_creates_client_from_app_profile(self):
        """ТЗ 14.10, п. 2.3: клиента нет, но номер подтверждён в приложении → клиент из профиля, без дублей."""
        self.assertIsNone(self.api.get("/api/main/clients/by-phone/", {"phone": "0700 999 999"}).data["client"])
        AppCustomer.objects.create(phone="+996700999999", full_name="Гүлнара Асанова", birth_date="1991-03-04")
        r = self.api.get("/api/main/clients/by-phone/", {"phone": "0700 999 999"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["client"]["full_name"], r.data["client"]["created"], r.data["has_app"]),
                         ("Гүлнара Асанова", True, True))
        r2 = self.api.get("/api/main/clients/by-phone/", {"phone": "+996700999999"})
        self.assertEqual((r2.data["client"]["id"], r2.data["client"]["created"]), (r.data["client"]["id"], False))
        self.assertEqual(Client.objects.filter(company=self.company, phone_normalized="+996700999999").count(), 1)


# ======================================================================
# Этап 1. Магазины
# ======================================================================


class ShopsTests(AppTestMixin, KassaBase):
    def test_only_enabled_shops_with_etag(self):
        self.assertEqual(self.app.get("/api/v1/shops").data, [])
        self.company.slug = "nbs-3"
        self.company.can_view_showcase = True
        self.company.save()
        AppShopSettings.objects.create(
            company=self.company, show_in_app=True, address="Бишкек, Чуй 100", phone="+996 555 000 000",
            hours="08:00–22:00", latitude=Decimal("42.874600"), longitude=Decimal("74.569800"),
            points_enabled=True, points_percent=Decimal("5"),
        )
        r = self.app.get("/api/v1/shops")
        self.assertEqual(r.status_code, 200)
        shop = r.data[0]
        self.assertEqual(shop["id"], str(self.company.id))
        self.assertEqual(shop["companyId"], str(self.company.id))
        self.assertEqual(shop["address"], "Бишкек, Чуй 100")
        self.assertEqual((shop["latitude"], shop["longitude"]), (42.8746, 74.5698))
        self.assertEqual((shop["pointsEnabled"], shop["pointsPercent"], shop["catalogSlug"]), (True, 5, "nbs-3"))
        etag = r["ETag"]
        r2 = self.app.get("/api/v1/shops/", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r2.status_code, 304)
        # изменение настроек сбрасывает кэш и ETag
        AppShopSettings.objects.filter(company=self.company).update(hours="Круглосуточно")
        AppShopSettings.objects.get(company=self.company).save()
        r3 = self.app.get("/api/v1/shops", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r3.status_code, 200)
        self.assertNotEqual(r3["ETag"], etag)

    def test_hidden_company_and_catalog_slug(self):
        AppShopSettings.objects.create(company=self.company, show_in_app=False, address="Ош", latitude=Decimal("42.87"), longitude=Decimal("74.59"))
        self.assertEqual(self.app.get("/api/v1/shops").data, [])
        AppShopSettings.objects.filter(company=self.company).update(show_in_app=True)
        cache.clear()
        shop = self.app.get("/api/v1/shops").data[0]
        # ТЗ 14.10, п. 2.1: витрина открывается по slug без включения в CRM → catalogSlug есть всегда
        self.assertEqual(shop["catalogSlug"], self.company.slug)
        self.assertTrue(shop["catalogSlug"])
        self.assertEqual(self.app.get(f"/api/main/public/companies/{shop['catalogSlug']}/showcase/").status_code, 200)
        self.assertNotIn("stock", shop)
        Company.objects.filter(pk=self.company.pk).update(slug="")
        cache.clear()
        self.assertIsNone(self.app.get("/api/v1/shops").data[0]["catalogSlug"])

    def test_shops_follow_company_and_branch_changes(self):
        """ТЗ 14.10, п. 2.2: имя/адрес из компании и филиала, ETag меняется даже без сигналов (update())."""
        AppShopSettings.objects.create(company=self.company, show_in_app=True,
                                       latitude=Decimal("42.87"), longitude=Decimal("74.59"))
        Company.objects.filter(pk=self.company.pk).update(address="Бишкек, Чуй 1", phone="+996555111111")
        r = self.app.get("/api/v1/shops")
        shop = r.data[0]
        self.assertEqual((shop["name"], shop["address"], shop["phone"]), (self.company.name, "Бишкек, Чуй 1", "+996555111111"))
        etag = r["ETag"]
        self.assertEqual(self.app.get("/api/v1/shops", HTTP_IF_NONE_MATCH=etag).status_code, 304)
        # смена названия компании в CRM запросом update() — сигналов нет, но отпечаток источников другой
        Company.objects.filter(pk=self.company.pk).update(name="Новое имя")
        r2 = self.app.get("/api/v1/shops", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual((r2.status_code, r2.data[0]["name"]), (200, "Новое имя"))
        self.assertNotEqual(r2["ETag"], etag)
        # app-shop-settings важнее компании
        r3 = self.api.patch("/api/main/app-shop-settings/", {"display_name": "Витрина"}, format="json")
        self.assertEqual(r3.status_code, 200, r3.data)
        r4 = self.app.get("/api/v1/shops", HTTP_IF_NONE_MATCH=r2["ETag"])
        self.assertEqual((r4.status_code, r4.data[0]["name"]), (200, "Витрина"))
        # филиал: переименование через save() → новый ETag и новое имя
        b = Branch.objects.create(company=self.company, name="Центр", address="Чуй 1")
        AppShopSettings.objects.create(company=self.company, branch=b, show_in_app=True,
                                       latitude=Decimal("42.87"), longitude=Decimal("74.59"))
        r5 = self.app.get("/api/v1/shops")
        self.assertEqual([x["name"] for x in r5.data], ["Витрина — Центр"])
        Branch.objects.filter(pk=b.pk).update(name="Север")
        r6 = self.app.get("/api/v1/shops", HTTP_IF_NONE_MATCH=r5["ETag"])
        self.assertEqual((r6.status_code, r6.data[0]["name"]), (200, "Витрина — Север"))

    def test_address_change_resets_coords_but_keeps_shop_listed(self):
        """ТЗ 14.10, п. 2.2: после смены адреса координаты сброшены, магазин остаётся в списке без них."""
        AppShopSettings.objects.create(company=self.company, show_in_app=True, address="Ош, Ленина 1",
                                       latitude=Decimal("40.51"), longitude=Decimal("72.80"), geocode_status="ok",
                                       geocoded_address="Ош, Ленина 1")
        self.assertEqual(self.app.get("/api/v1/shops").data[0]["latitude"], 40.51)
        with mock.patch("apps.clientapp.tasks.enqueue"):
            r = self.api.patch("/api/main/app-shop-settings/", {"address": "Ош, Курманжан Датки 5"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        shops = self.app.get("/api/v1/shops").data
        self.assertEqual(len(shops), 1)
        self.assertEqual((shops[0]["address"], shops[0]["latitude"], shops[0]["longitude"]),
                         ("Ош, Курманжан Датки 5", None, None))
        # геокодер не нашёл адрес — магазин скрыт до исправления адреса
        AppShopSettings.objects.filter(company=self.company, branch=None).update(geocode_status="not_found",
                                                                                 geocode_attempts=1)
        cache.clear()
        self.assertEqual(self.app.get("/api/v1/shops").data, [])

    def test_branches_are_shops(self):
        b1 = Branch.objects.create(company=self.company, name="Центр", address="Чуй 1")
        b2 = Branch.objects.create(company=self.company, name="Юг", address="Ахунбаева 5")
        Branch.objects.create(company=self.company, name="Склад")  # без адреса — не магазин
        r = self.api.patch("/api/main/app-shop-settings/", {
            "show_in_app": True, "points_enabled": True, "points_percent": "3",
            "branches": [{"branch_id": str(b2.id), "show_in_app": False}],
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # на карте — только с координатами (их ставит геокодер; здесь вручную)
        r = self.api.patch("/api/main/app-shop-settings/", {"branches": [
            {"branch_id": str(b1.id), "latitude": "42.87", "longitude": "74.59"},
        ]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        shops = self.app.get("/api/v1/shops").data
        self.assertEqual([s["id"] for s in shops], [str(b1.id)])
        self.assertEqual(shops[0]["companyId"], str(self.company.id))
        self.assertEqual(shops[0]["pointsPercent"], 3)

    def test_owner_settings_permissions_and_geocode(self):
        cashier_api = APIClient()
        cashier_api.force_authenticate(self.cashier)
        self.assertEqual(cashier_api.get("/api/main/app-shop-settings/").status_code, 403)
        fake = mock.MagicMock()
        fake.__enter__.return_value.get.return_value = mock.MagicMock(
            status_code=200, json=lambda: [{"lat": "42.87", "lon": "74.59"}], raise_for_status=lambda: None
        )
        with mock.patch("apps.clientapp.tasks.enqueue", side_effect=run_now), \
                mock.patch("apps.clientapp.geocode.httpx.Client", return_value=fake) as client_cls, \
                self.captureOnCommitCallbacks(execute=True):
            r = self.api.patch("/api/main/app-shop-settings/", {"show_in_app": True, "address": "Бишкек, Чуй 100"},
                               format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("User-Agent", client_cls.call_args.kwargs["headers"])
        row = AppShopSettings.objects.get(company=self.company, branch=None)
        self.assertEqual((row.latitude, row.geocode_status), (Decimal("42.870000"), "ok"))
        shop = self.app.get("/api/v1/shops").data[0]
        self.assertEqual(shop["latitude"], 42.87)

    def test_geocode_failure_tolerated(self):
        with mock.patch("apps.clientapp.tasks.enqueue", side_effect=run_now), \
                mock.patch("apps.clientapp.geocode.httpx.Client", side_effect=OSError("down")), \
                self.captureOnCommitCallbacks(execute=True):
            r = self.api.patch("/api/main/app-shop-settings/", {"show_in_app": True, "address": "Ош"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(AppShopSettings.objects.get(branch=None).geocode_status, "error")
        # геокодер не справился — магазина нет на карте (защита от мусора), владелец видит причину
        self.assertEqual(self.app.get("/api/v1/shops").data, [])
        self.assertIn("no_coordinates", self.api.get("/api/main/app-shop-settings/").data["not_visible_reasons"])


class PromosTests(AppTestMixin, KassaBase):
    def test_active_promos_only(self):
        AppShopSettings.objects.create(company=self.company, show_in_app=True, address="Чуй 1", latitude=Decimal("42.87"), longitude=Decimal("74.59"))
        today = timezone.localdate()
        PromoRule.objects.create(company=self.company, title="2+1", min_qty=2, gift_qty=1, inclusive=True,
                                 active_to=today + timedelta(days=5))
        PromoRule.objects.create(company=self.company, title="Старая", min_qty=2, gift_qty=1,
                                 active_to=today - timedelta(days=1))
        PromoRule.objects.create(company=self.company, title="Выкл", min_qty=2, gift_qty=1, is_active=False)
        p = Product.objects.create(company=self.company, name="Молоко", price=Decimal("80"), stock=True,
                                   purchase_price=Decimal("37.77"))
        ProductPromotionTier.objects.create(product=p, min_amount=Decimal("500"), discount_percent=Decimal("10"))
        r = self.app.get(f"/api/v1/shops/{self.company.id}/promos")
        self.assertEqual(r.status_code, 200)
        titles = [x["title"] for x in r.data]
        self.assertEqual(titles[0], "2+1")
        self.assertEqual(r.data[0]["validUntil"], (today + timedelta(days=5)).isoformat())
        self.assertIn("Скидка до 10 % на «Молоко»", titles)
        self.assertEqual(len(r.data), 2)
        self.assertNotIn("37.77", str(r.data))  # закупочная цена не уходит
        r404 = self.app.get(f"/api/v1/shops/{uuid.uuid4()}/promos")
        self.assertEqual((r404.status_code, r404.data["error"]), (404, "not_found"))


# ======================================================================
# Этап 2. Вход через Telegram
# ======================================================================


@override_settings(**BOT)
class TelegramAuthTests(AppTestMixin, TestCase):
    def start(self, **body):
        return self.app.post("/api/v1/auth/telegram/start", body or {"fullName": "Иванов Иван", "birthDate": "1995-04-12"},
                             format="json")

    def test_full_flow(self):
        r = self.start()
        self.assertEqual(r.status_code, 201, r.data)
        nonce = r.data["nonce"]
        self.assertEqual(r.data["botUrl"], f"https://t.me/nurcrm_login_bot?start={nonce}")
        self.assertEqual(r.data["expiresIn"], 300)
        self.assertEqual(self.app.get("/api/v1/auth/telegram/status", {"nonce": nonce}).data, {"status": "pending"})

        self.assertEqual(self.hook(self.msg(777, text=f"/start {nonce}")).data["result"], "ask_contact")
        kb = self.tg_mock.call_args.args[1]["reply_markup"]
        self.assertTrue(kb["keyboard"][0][0]["request_contact"])
        res = self.hook(self.msg(777, contact={"phone_number": "996700123456", "user_id": 777}))
        self.assertEqual(res.data["result"], "ok")

        r = self.app.get("/api/v1/auth/telegram/status", {"nonce": nonce})
        self.assertEqual(r.data["status"], "ok")
        token = r.data["token"]
        cust = AppCustomer.objects.get(phone="+996700123456")
        self.assertEqual(r.data["clientId"], str(cust.id))
        self.assertEqual((cust.full_name, str(cust.birth_date), cust.telegram_user_id),
                         ("Иванов Иван", "1995-04-12", 777))
        me = self.authed(token).get("/api/v1/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["phone"], "+996700123456")
        # повтор в течение минуты (iPhone мог «заморозить» ответ) — тот же токен, новый не выпускается
        again = self.app.get("/api/v1/auth/telegram/status", {"nonce": nonce}).data
        self.assertEqual((again["status"], again["token"]), ("ok", token))
        self.assertEqual(AppToken.objects.filter(customer=cust).count(), 1)
        # после минуты — уже «expired»
        cache.clear()
        self.assertEqual(self.app.get("/api/v1/auth/telegram/status", {"nonce": nonce}).data, {"status": "expired"})

    def test_foreign_contact_rejected(self):
        nonce = self.start().data["nonce"]
        self.hook(self.msg(777, text=f"/start {nonce}"))
        res = self.hook(self.msg(777, contact={"phone_number": "+996700999999", "user_id": 888}))
        self.assertEqual(res.data["result"], "foreign_contact")
        res = self.hook(self.msg(777, contact={"phone_number": "+996700999999"}))  # без user_id
        self.assertEqual(res.data["result"], "foreign_contact")
        self.assertFalse(AppCustomer.objects.exists())
        self.assertEqual(self.app.get("/api/v1/auth/telegram/status", {"nonce": nonce}).data["status"], "pending")

    def test_nonce_bound_to_first_telegram_account(self):
        nonce = self.start().data["nonce"]
        self.hook(self.msg(777, text=f"/start {nonce}"))
        self.assertEqual(self.hook(self.msg(999, text=f"/start {nonce}")).data["result"], "foreign_nonce")
        # аккаунт 999 со своим контактом не может завершить чужой вход
        res = self.hook(self.msg(999, contact={"phone_number": "+996700999999", "user_id": 999}))
        self.assertEqual(res.data["result"], "expired")

    def test_nonce_expires(self):
        nonce = self.start().data["nonce"]
        TelegramAuthNonce.objects.filter(nonce=nonce).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.hook(self.msg(777, text=f"/start {nonce}")).data["result"], "expired")
        self.assertEqual(self.app.get("/api/v1/auth/telegram/status", {"nonce": nonce}).data["status"], "expired")

    def test_webhook_secret_required(self):
        self.assertEqual(self.hook(self.msg(1, text="/start"), secret="wrong").status_code, 403)
        self.assertEqual(self.app.post(WEBHOOK, {}, format="json").status_code, 403)

    def test_existing_customer_logs_in_again(self):
        cust, _ = self.make_customer(phone="+996700123456", name="Старое Имя")
        nonce = self.start().data["nonce"]
        self.hook(self.msg(5, text=f"/start {nonce}"))
        self.hook(self.msg(5, contact={"phone_number": "+996 700 123 456", "user_id": 5}))
        r = self.app.get("/api/v1/auth/telegram/status", {"nonce": nonce})
        self.assertEqual(r.data["clientId"], str(cust.id))
        self.assertEqual(AppCustomer.objects.count(), 1)
        cust.refresh_from_db()
        self.assertEqual(cust.full_name, "Старое Имя")

    def test_start_rate_limited_per_ip(self):
        codes = [self.start().status_code for _ in range(11)]
        self.assertEqual(codes[:10], [201] * 10)
        self.assertEqual(codes[10], 429)
        r = self.start()
        self.assertEqual(r.data["error"], "rate_limited")

    def test_status_polling_rate_limited_per_nonce(self):
        nonce = self.start().data["nonce"]
        codes = [self.app.get("/api/v1/auth/telegram/status", {"nonce": nonce}).status_code for _ in range(91)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(set(codes[:90]), {200})

    def test_validation_errors_format(self):
        r = self.start(fullName="Иван", birthDate="12.04.1995")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["error"], "validation_error")
        self.assertIn("message", r.data)

    @override_settings(CLIENT_APP_TELEGRAM_BOT_TOKEN="", CLIENT_APP_BOT_USERNAME="")
    def test_unconfigured_bot_returns_503(self):
        r = self.start()
        self.assertEqual((r.status_code, r.data["error"]), (503, "auth_unavailable"))


class ProfileTests(AppTestMixin, KassaBase):
    def test_auth_required_and_staff_jwt_not_accepted(self):
        r = self.app.get("/api/v1/me")
        self.assertEqual((r.status_code, r.data["error"]), (401, "unauthorized"))
        bad = self.authed("nca_nonsense").get("/api/v1/me")
        self.assertEqual(bad.status_code, 401)
        jwt_like = self.authed("eyJhbGciOiJIUzI1NiJ9.e30.x").get("/api/v1/me")
        self.assertEqual(jwt_like.status_code, 401)

    def test_patch_profile(self):
        _, raw = self.make_customer()
        api = self.authed(raw)
        r = api.patch("/api/v1/me", {"fullName": "Айбек Асанов", "birthDate": "1990-01-02", "lang": "ky"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["fullName"], r.data["birthDate"], r.data["lang"]), ("Айбек Асанов", "1990-01-02", "ky"))
        self.assertEqual(api.patch("/api/v1/me", {"lang": "en"}, format="json").status_code, 400)
        self.assertEqual(api.patch("/api/v1/me", {"phone": "+996700000000"}, format="json").data["error"],
                         "phone_change_requires_telegram")

    def test_logout_revokes_token(self):
        cust, raw = self.make_customer()
        AppPushToken.objects.create(customer=cust, token="ExponentPushToken[a]")
        AppPushToken.objects.create(customer=cust, token="ExponentPushToken[b]")
        api = self.authed(raw)
        self.assertEqual(api.post("/api/v1/auth/logout").status_code, 204)
        self.assertEqual(api.get("/api/v1/me").status_code, 401)
        self.assertFalse(AppPushToken.objects.filter(customer=cust).exists())  # ТЗ 14.10, п. 2.4

    def test_logout_with_push_token_removes_only_that_device(self):
        cust, raw = self.make_customer()
        AppPushToken.objects.create(customer=cust, token="ExponentPushToken[a]")
        AppPushToken.objects.create(customer=cust, token="ExponentPushToken[b]")
        self.assertEqual(self.authed(raw).post("/api/v1/auth/logout", {"pushToken": "ExponentPushToken[a]"},
                                               format="json").status_code, 204)
        self.assertEqual(list(AppPushToken.objects.values_list("token", flat=True)), ["ExponentPushToken[b]"])

    def test_delete_me_removes_customer_keeps_shop_records(self):
        cust, raw = self.make_customer()
        AppPushToken.objects.create(customer=cust, token="ExponentPushToken[x]")
        api = self.authed(raw)
        self.assertEqual(api.delete("/api/v1/me").status_code, 204)
        cust.refresh_from_db()
        self.assertIsNotNone(cust.deleted_at)
        self.assertEqual((cust.phone, cust.full_name, cust.birth_date), (None, "", None))
        self.assertFalse(AppToken.objects.filter(customer=cust).exists())
        self.assertFalse(AppPushToken.objects.exists())
        self.assertEqual(api.get("/api/v1/me").status_code, 401)
        self.assertTrue(Client.objects.filter(pk=self.client_obj.pk).exists())  # учёт магазина не трогаем


# ======================================================================
# Этап 3. Баланс и история
# ======================================================================


class BalanceTests(AppTestMixin, KassaBase):
    def setUp(self):
        super().setUp()
        self.cust, raw = self.make_customer(phone="+996555000222")
        self.me = self.authed(raw)
        AppShopSettings.objects.create(company=self.company, show_in_app=True, address="Чуй 1",
                                       points_enabled=True, points_percent=Decimal("5"))

    def pay_with_client(self, earn="10.00", redeem=None):
        body = {"client": str(self.client_obj.id)}
        if redeem:
            body.update(bonus_redeemed=redeem, payment={"method": "cash", "received": "500"})
        r = self.quick(**body)
        self.assertEqual(r.status_code, 201, r.data)
        sale_id = r.data["id"]
        if earn:
            e = self.api.post(f"/api/main/clients/{self.client_obj.id}/bonus/",
                              {"delta": earn, "reason": "earn", "sale": sale_id}, format="json")
            self.assertEqual(e.status_code, 201, e.data)
        return sale_id

    def test_balance_follows_kassa(self):
        self.assertEqual(self.me.get("/api/v1/me/balance").data, [])
        self.pay_with_client(earn="10.00")
        bal = self.me.get("/api/v1/me/balance").data
        self.assertEqual(len(bal), 1)
        self.assertEqual((bal[0]["companyId"], bal[0]["points"], bal[0]["pointsPercent"]),
                         (str(self.company.id), 10, 5))
        self.client_obj.refresh_from_db()
        self.assertEqual(Decimal(str(bal[0]["points"])), self.client_obj.bonus_balance)
        self.pay_with_client(earn="5.50", redeem="4.00")
        bal = self.me.get("/api/v1/me/balance").data
        self.assertEqual(bal[0]["points"], 11.5)

    def test_other_company_balance_separate(self):
        o = User.objects.create_user(email=f"z{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        other = Company.objects.create(name="Второй", owner=o, subscription_plan=self.plan)
        Client.objects.create(company=other, full_name="Айбек", phone="0555 000 222", bonus_balance=Decimal("7"))
        Client.objects.create(company=other, full_name="Поставщик", phone="0555000222",
                              type=Client.StatusClient.SUPPLIERS, bonus_balance=Decimal("99"))
        self.pay_with_client(earn="10")
        bal = {b["companyId"]: b["points"] for b in self.me.get("/api/v1/me/balance").data}
        self.assertEqual(bal, {str(self.company.id): 10, str(other.id): 7})

    def test_purchases_history_and_detail(self):
        s1 = self.pay_with_client(earn="10")
        s2 = self.pay_with_client(earn="5", redeem="3")
        r = self.me.get("/api/v1/me/purchases", {"limit": 1})
        self.assertEqual(r.status_code, 200)
        first = r.data["items"][0]
        self.assertEqual(first["id"], s2)
        self.assertEqual((first["pointsEarned"], first["pointsSpent"], first["total"]), (5, 3, 197))
        self.assertEqual(first["items"][0], {"name": "Хлеб", "qty": 2, "price": 100, "sum": 200})
        self.assertIsNotNone(r.data["nextCursor"])
        r2 = self.me.get("/api/v1/me/purchases", {"limit": 1, "cursor": r.data["nextCursor"]})
        self.assertEqual(r2.data["items"][0]["id"], s1)
        self.assertIsNone(r2.data["nextCursor"])
        d = self.me.get(f"/api/v1/me/purchases/{s1}")
        self.assertEqual((d.status_code, d.data["pointsEarned"]), (200, 10))
        self.assertNotIn("purchase_price", str(d.data))

    def test_full_return_flagged(self):
        sale_id = self.pay_with_client(earn=None)
        r = self.api.post(f"/api/main/pos/sales/{sale_id}/return/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        item = self.me.get(f"/api/v1/me/purchases/{sale_id}").data
        self.assertTrue(item["returned"])
        self.assertEqual(item["status"], "returned")

    def test_other_customers_purchase_is_404(self):
        foreign = Client.objects.create(company=self.company, full_name="Другой", phone="+996700777777")
        sale_id = self.quick(client=str(foreign.id)).data["id"]
        r = self.me.get(f"/api/v1/me/purchases/{sale_id}")
        self.assertEqual((r.status_code, r.data["error"]), (404, "not_found"))
        self.assertEqual(self.me.get("/api/v1/me/purchases").data["items"], [])
        self.assertEqual(self.me.get("/api/v1/me/purchases/garbage").status_code, 404)

    def test_bad_cursor(self):
        self.assertEqual(self.me.get("/api/v1/me/purchases", {"cursor": "!!!"}).status_code, 400)


class BonusImportTests(AppTestMixin, KassaBase):
    def test_import_idempotent_and_conflicts(self):
        other = Client.objects.create(company=self.company, full_name="Есть движения", phone="+996700111111")
        self.api.post(f"/api/main/clients/{other.id}/bonus/", {"delta": "5", "reason": "earn"}, format="json")
        body = {"items": [
            {"phone": "0555 000 222", "balance": "300.50"},
            {"client_id": str(other.id), "balance": "100"},
            {"phone": "0700 222 333", "balance": "40", "full_name": "Новый"},
            {"phone": "12", "balance": "1"},
        ]}
        r = self.api.post("/api/main/clients/bonus/import/", body, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([x["status"] for x in r.data["results"]], ["imported", "conflict", "imported", "invalid"])
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.bonus_balance, Decimal("300.50"))
        r2 = self.api.post("/api/main/clients/bonus/import/", body, format="json")
        self.assertEqual([x["status"] for x in r2.data["results"]],
                         ["already_imported", "conflict", "already_imported", "invalid"])
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.bonus_balance, Decimal("300.50"))
        new = Client.objects.get(phone_normalized="+996700222333")
        self.assertEqual(new.bonus_balance, Decimal("40.00"))
        tx = ClientBonusTransaction.objects.get(client=self.client_obj)
        self.assertEqual((tx.reason, tx.idempotency_key), ("manual", f"bonus-import:{self.client_obj.id}"))

    def test_imported_points_visible_in_app(self):
        _, raw = self.make_customer()
        self.api.post("/api/main/clients/bonus/import/", {"items": [{"phone": "+996555000222", "balance": "77"}]},
                      format="json")
        bal = self.authed(raw).get("/api/v1/me/balance").data
        self.assertEqual(bal[0]["points"], 77)
        hist = self.authed(raw).get("/api/v1/me/purchases").data["items"]
        self.assertEqual((hist[0]["kind"], hist[0]["pointsEarned"]), ("bonus", 77))


# ======================================================================
# Этап 4. Push
# ======================================================================


def _expo_response(tickets):
    fake = mock.MagicMock()
    fake.__enter__.return_value.post.return_value = mock.MagicMock(json=lambda: {"data": tickets})
    return fake


class PushTests(AppTestMixin, KassaBase):
    def setUp(self):
        super().setUp()
        self.cust, raw = self.make_customer()
        self.me = self.authed(raw)

    def test_register_and_delete_push_token(self):
        r = self.me.post("/api/v1/me/push-token", {"token": "ExponentPushToken[abc]", "platform": "android"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.me.post("/api/v1/me/push-token", {"token": "bad"}, format="json").status_code, 400)
        self.assertEqual(self.me.delete("/api/v1/me/push-token", {"token": "ExponentPushToken[abc]"},
                                        format="json").status_code, 204)
        self.assertFalse(AppPushToken.objects.exists())

    def test_push_sent_on_earn_and_dead_token_removed(self):
        AppPushToken.objects.create(customer=self.cust, token="ExponentPushToken[good]")
        AppPushToken.objects.create(customer=self.cust, token="ExponentPushToken[dead]")
        sale_id = self.quick(client=str(self.client_obj.id)).data["id"]
        fake = _expo_response([{"status": "ok"}, {"status": "error", "details": {"error": "DeviceNotRegistered"}}])
        with mock.patch("apps.clientapp.tasks.enqueue", side_effect=run_now), \
                mock.patch("apps.clientapp.tasks.enqueue_later", side_effect=run_later_now), \
                mock.patch("apps.clientapp.push.httpx.Client", return_value=fake), \
                self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(f"/api/main/clients/{self.client_obj.id}/bonus/",
                              {"delta": "42.5", "reason": "earn", "sale": sale_id}, format="json")
        self.assertEqual(r.status_code, 201)
        sent = fake.__enter__.return_value.post.call_args
        self.assertTrue(sent.args[0].startswith("https://exp.host/--/api/v2/push/send"))
        msgs = sent.kwargs["json"]
        self.assertEqual((msgs[0]["title"], msgs[0]["body"]), ("Kassa Co", "Начислено 42,5 балла"))
        self.assertEqual(msgs[0]["data"], {"type": "purchase", "companyId": str(self.company.id),
                                           "shopId": str(self.company.id), "purchaseId": sale_id})
        self.assertEqual(set(AppPushToken.objects.values_list("token", flat=True)), {"ExponentPushToken[good]"})

    def test_one_push_per_sale_with_earn_and_redeem(self):
        """ТЗ 14.10, п. 2.4: начисление и списание одним уведомлением; повтор чека второго не даёт."""
        AppPushToken.objects.create(customer=self.cust, token="ExponentPushToken[good]")
        self.cust.lang = "ky"
        self.cust.save(update_fields=["lang"])
        ClientBonusTransaction.objects.create(company=self.company, client=self.client_obj, delta=Decimal("100"),
                                              balance_after=Decimal("100"), reason="manual", note="старт")
        Client.objects.filter(pk=self.client_obj.pk).update(bonus_balance=Decimal("100"))
        sale_id = self.quick(client=str(self.client_obj.id)).data["id"]
        queued = []
        with mock.patch("apps.clientapp.tasks.enqueue_later", side_effect=lambda t, c, *a: queued.append(a)), \
                self.captureOnCommitCallbacks(execute=True):
            self.api.post(f"/api/main/clients/{self.client_obj.id}/bonus/",
                          {"delta": "45", "reason": "earn", "sale": sale_id}, format="json")
            self.api.post(f"/api/main/clients/{self.client_obj.id}/bonus/",
                          {"delta": "-50", "reason": "redeem", "sale": sale_id}, format="json")
        self.assertEqual(queued, [(sale_id,)])  # задача на продажу поставлена один раз
        fake = _expo_response([{"status": "ok"}])
        with mock.patch("apps.clientapp.push.httpx.Client", return_value=fake):
            self.assertEqual(push_for_sale(sale_id), 1)
            self.assertEqual(push_for_sale(sale_id), 0)  # повторная отправка чека — без второго уведомления
        msgs = fake.__enter__.return_value.post.call_args.kwargs["json"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual((msgs[0]["title"], msgs[0]["body"]), ("Kassa Co", "45 балл кошулду, 50 балл алынды"))

    def test_no_push_without_sale_and_push_errors_do_not_break(self):
        AppPushToken.objects.create(customer=self.cust, token="ExponentPushToken[good]")
        with mock.patch("apps.clientapp.tasks.enqueue", side_effect=run_now), \
                mock.patch("apps.clientapp.tasks.enqueue_later", side_effect=run_later_now), \
                mock.patch("apps.clientapp.push.httpx.Client", side_effect=OSError("expo down")) as cl, \
                self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(f"/api/main/clients/{self.client_obj.id}/bonus/", {"delta": "5", "reason": "earn"},
                              format="json")
            sale_id = self.quick(client=str(self.client_obj.id)).data["id"]
            r2 = self.api.post(f"/api/main/clients/{self.client_obj.id}/bonus/",
                               {"delta": "5", "reason": "earn", "sale": sale_id}, format="json")
        self.assertEqual((r.status_code, r2.status_code), (201, 201))
        self.assertEqual(cl.call_count, 1)  # только для продажи

    def test_messages_ru_ky(self):
        self.assertEqual(format_points(Decimal("1"), "ru"), "1 балл")
        self.assertEqual(format_points(Decimal("3"), "ru"), "3 балла")
        self.assertEqual(format_points(Decimal("11"), "ru"), "11 баллов")
        self.assertEqual(bonus_message("Нур", Decimal("-50"), "redeem", "ru"), ("Нур", "Списано 50 баллов"))
        self.assertEqual(bonus_message("Нур", Decimal("10"), "earn", "ky"), ("Нур", "10 балл кошулду"))
        self.assertEqual(sale_message("Нур", Decimal("45"), Decimal("50"), "ru")[1], "Начислено 45, списано 50 баллов")
        self.assertEqual(sale_message("Нур", Decimal("45"), Decimal("0"), "ru")[1], "Начислено 45 баллов")
        self.assertEqual(sale_message("Нур", Decimal("0"), Decimal("50"), "ky")[1], "50 балл алынды")
        self.assertEqual(sale_message("Нур", Decimal("45"), Decimal("50"), "ky")[1], "45 балл кошулду, 50 балл алынды")


# ======================================================================
# Этап 5. Приглашения и QR
# ======================================================================


class ReferralTests(AppTestMixin, KassaBase):
    def setUp(self):
        super().setUp()
        self.inviter, self.inviter_raw = self.make_customer(phone="+996700100100", name="Умар")
        self.invitee, raw = self.make_customer(phone="+996555000222", name="Айбек")
        self.me = self.authed(raw)

    def test_referral_reward_once(self):
        r = self.api.patch("/api/main/referral-rules/", {"enabled": True, "inviter_points": "100",
                                                          "invitee_points": "50"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        info = self.authed(self.inviter_raw).get("/api/v1/me/referral").data
        self.assertEqual(info["code"], self.inviter.referral_code)
        r = self.me.post("/api/v1/me/referral/apply", {"code": info["code"].lower()}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.me.post("/api/v1/me/referral/apply", {"code": info["code"]}, format="json").data["error"],
                         "referral_already_applied")
        with mock.patch("apps.clientapp.tasks.enqueue", side_effect=run_now), \
                self.captureOnCommitCallbacks(execute=True):
            self.quick(client=str(self.client_obj.id))
        with mock.patch("apps.clientapp.tasks.enqueue", side_effect=run_now), \
                self.captureOnCommitCallbacks(execute=True):
            self.quick(client=str(self.client_obj.id))
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.bonus_balance, Decimal("50.00"))
        inviter_client = Client.objects.get(company=self.company, phone_normalized="+996700100100")
        self.assertEqual(inviter_client.bonus_balance, Decimal("100.00"))
        ref = Referral.objects.get()
        self.assertIsNotNone(ref.rewarded_at)
        self.assertEqual(ClientBonusTransaction.objects.filter(idempotency_key__startswith="referral:").count(), 2)
        info = self.authed(self.inviter_raw).get("/api/v1/me/referral").data
        self.assertEqual((info["invited"], info["earned"]), (1, 100))

    def test_apply_rules(self):
        self.assertEqual(self.me.post("/api/v1/me/referral/apply", {"code": "NOPE123"}, format="json").status_code, 404)
        self.assertEqual(self.me.post("/api/v1/me/referral/apply", {"code": self.invitee.referral_code},
                                      format="json").data["error"], "referral_self")
        self.assertEqual(self.me.post("/api/v1/me/referral/apply", {"code": "NOPE123"}, format="json").data["error"],
                         "referral_not_found")
        self.quick(client=str(self.client_obj.id))
        r = self.me.post("/api/v1/me/referral/apply", {"code": self.inviter.referral_code}, format="json")
        self.assertEqual((r.status_code, r.data["error"]), (409, "referral_too_late"))

    def test_inviter_gets_push_after_friend_first_purchase(self):
        self.api.patch("/api/main/referral-rules/", {"enabled": True, "inviter_points": "100", "invitee_points": "0"},
                       format="json")
        AppPushToken.objects.create(customer=self.inviter, token="ExponentPushToken[inv]")
        self.me.post("/api/v1/me/referral/apply", {"code": self.inviter.referral_code}, format="json")
        fake = _expo_response([{"status": "ok"}])
        with mock.patch("apps.clientapp.tasks.enqueue", side_effect=run_now), \
                mock.patch("apps.clientapp.push.httpx.Client", return_value=fake), \
                self.captureOnCommitCallbacks(execute=True):
            self.quick(client=str(self.client_obj.id))
        msgs = fake.__enter__.return_value.post.call_args.kwargs["json"]
        self.assertEqual((msgs[0]["to"], msgs[0]["body"]), ("ExponentPushToken[inv]", "Друг сделал первую покупку: +100 баллов"))

    def test_landing_page(self):
        code = self.inviter.referral_code
        r = self.app.get(f"/r/{code.lower()}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn(code, html)
        self.assertIn(f"nurcrmapp://invite/{code}", html)
        self.assertIn("play.google.com", html)
        self.assertEqual(self.app.get("/r/<script>/").status_code, 404)

    def test_apply_rate_limited(self):
        codes = [self.me.post("/api/v1/me/referral/apply", {"code": "WRONG1"}, format="json").status_code
                 for _ in range(6)]
        self.assertEqual(codes[-1], 429)


class QrTests(AppTestMixin, KassaBase):
    def setUp(self):
        super().setUp()
        self.cust, raw = self.make_customer(phone="+996700555555", name="Гуля")
        self.me = self.authed(raw)

    def test_qr_resolve_creates_client_once(self):
        r = self.me.get("/api/v1/me/qr-token")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["qr"].startswith("NURCRMT"))
        res = self.api.post("/api/main/clients/resolve-qr/", {"token": r.data["qr"]}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["client"]["created"])
        self.assertEqual(res.data["client"]["phone_normalized"], "+996700555555")
        res2 = self.api.post("/api/main/clients/resolve-qr/", {"token": r.data["token"]}, format="json")
        self.assertEqual((res2.status_code, res2.data["client"]["id"]), (200, res.data["client"]["id"]))
        self.assertEqual(Client.objects.filter(phone_normalized="+996700555555").count(), 1)

    def test_qr_expired_or_unknown(self):
        raw = self.me.get("/api/v1/me/qr-token").data["token"]
        AppQrToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.api.post("/api/main/clients/resolve-qr/", {"token": raw}, format="json").status_code, 404)
        self.assertEqual(self.api.post("/api/main/clients/resolve-qr/", {"token": "NURCRMTxxxx"},
                                       format="json").status_code, 404)
        self.assertEqual(APIClient().post("/api/main/clients/resolve-qr/", {"token": raw}, format="json").status_code, 401)

    def test_phone_qr_transitional(self):
        res = self.api.post("/api/main/clients/resolve-qr/", {"token": "NURCRM996555000222"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.data["format"], res.data["client"]["id"]), ("phone", str(self.client_obj.id)))

    def test_phone_qr_creates_client_from_app_profile(self):
        """ТЗ 14.10, п. 2.3: старый QR с номером подтверждённого профиля → клиент из профиля, один раз."""
        self.assertIsNone(self.api.post("/api/main/clients/resolve-qr/", {"token": "NURCRM996700111222"},
                                        format="json").data["client"])  # профиля нет — как раньше
        res = self.api.post("/api/main/clients/resolve-qr/", {"token": "NURCRM996700555555"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data["format"], res.data["verified"], res.data["client"]["full_name"],
                          res.data["client"]["created"]), ("phone", False, "Гуля", True))
        res2 = self.api.post("/api/main/clients/resolve-qr/", {"token": "NURCRM996700555555"}, format="json")
        self.assertEqual((res2.status_code, res2.data["client"]["id"]), (200, res.data["client"]["id"]))
        self.assertEqual(Client.objects.filter(phone_normalized="+996700555555").count(), 1)

    def test_qr_token_rate_limited(self):
        codes = [self.me.get("/api/v1/me/qr-token").status_code for _ in range(31)]
        self.assertEqual(codes[-1], 429)
