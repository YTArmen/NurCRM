"""
API приложения клиентов: /api/v1/…  (camelCase, ошибки {"error", "message"}).
"""
import hmac
import logging
import secrets
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.response import Response

from apps.main.models import Product, PromoRule
from apps.users.models import Branch, Company

from . import services, telegram
from .api_base import (
    AppAPIError,
    AppAPIView,
    AuthStartIPThrottle,
    AuthStatusIPThrottle,
    AuthStatusNonceThrottle,
    PublicAppAPIView,
    QrTokenThrottle,
    ReferralApplyThrottle,
    WebhookThrottle,
    error_response,
)
from .models import AppPushToken, AppQrToken, AppToken, TelegramAuthNonce, hash_secret

logger = logging.getLogger("clientapp.views")

NONCE_TTL_SECONDS = 300
QR_TTL_SECONDS = 300
AUTH_STATUS_REPLAY_SECONDS = 60  # повторный status после выдачи токена отдаёт тот же ответ


def _client_ip(request):
    return request.META.get("REMOTE_ADDR") or None


def _parse_birth_date(value, field="birthDate"):
    if value in (None, ""):
        return None
    d = parse_date(str(value)) if not isinstance(value, date) else value
    if d is None:
        raise AppAPIError("validation_error", "Дата рождения в формате ГГГГ-ММ-ДД.", extra={"fields": {field: "invalid"}})
    today = timezone.localdate()
    if d > today or d.year < 1900:
        raise AppAPIError("validation_error", "Неверная дата рождения.", extra={"fields": {field: "invalid"}})
    return d


def _clean_name(value):
    name = " ".join(str(value or "").split())
    if len(name) > 255:
        raise AppAPIError("validation_error", "ФИО слишком длинное.", extra={"fields": {"fullName": "too_long"}})
    return name


def me_payload(customer):
    return {
        "clientId": str(customer.id),
        "fullName": customer.full_name,
        "phone": customer.phone,
        "birthDate": customer.birth_date.isoformat() if customer.birth_date else None,
        "lang": customer.lang,
        "referralCode": customer.referral_code,
        "createdAt": services.iso(customer.created_at),
    }


# ======================================================================
# Этап 1. Магазины
# ======================================================================


class ShopsView(PublicAppAPIView):
    """GET /api/v1/shops — магазины с «показывать в приложении». ETag / If-None-Match → 304."""

    def get(self, request):
        shops, etag = services.get_shops_cached()
        inm = request.headers.get("If-None-Match", "")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            resp = Response(status=status.HTTP_304_NOT_MODIFIED)
        else:
            resp = Response(shops)
        resp["ETag"] = etag
        resp["Cache-Control"] = "public, max-age=60"
        return resp


def _resolve_shop(shop_id):
    """(company, branch) магазина, видимого в приложении, или 404."""
    shops, _ = services.get_shops_cached()
    shop = next((s for s in shops if s["id"] == str(shop_id)), None)
    if shop is None:
        raise AppAPIError("not_found", "Магазин не найден.", http_status=404)
    company = Company.objects.get(pk=shop["companyId"])
    branch = Branch.objects.filter(pk=shop["branchId"]).first() if shop.get("branchId") else None
    return company, branch


def _promo_rule_text(rule):
    sign = "от" if rule.inclusive else "больше"
    scope = ""
    if rule.product_id:
        scope = f" «{rule.product.name}»"
    elif rule.brand_id:
        scope = f" бренда «{rule.brand.name}»"
    elif rule.category_id:
        scope = f" из категории «{rule.category.name}»"
    return f"При покупке {sign} {rule.min_qty} шт.{scope} — {rule.gift_qty} шт. в подарок."


def _fmt_money(v):
    d = Decimal(str(v or 0))
    return f"{int(d)}" if d == d.to_integral_value() else f"{d:.2f}"


class ShopPromosView(PublicAppAPIView):
    """GET /api/v1/shops/{id}/promos — действующие акции кассы (подарки и акционные товары)."""

    def get(self, request, shop_id):
        company, branch = _resolve_shop(shop_id)
        today = timezone.localdate()
        branch_q = Q(branch__isnull=True) | (Q(branch=branch) if branch else Q(pk__isnull=True))
        promos = []
        rules = (
            PromoRule.objects.filter(company=company, is_active=True)
            .filter(branch_q)
            .filter(Q(active_from__isnull=True) | Q(active_from__lte=today))
            .filter(Q(active_to__isnull=True) | Q(active_to__gte=today))
            .select_related("product", "brand", "category")
            .order_by("-priority", "-min_qty")[:30]
        )
        for r in rules:
            promos.append({
                "id": f"rule-{r.id}",
                "title": r.title or "Подарок за покупку",
                "text": _promo_rule_text(r),
                "imageUrl": None,
                "validUntil": r.active_to.isoformat() if r.active_to else None,
            })
        products = (
            Product.objects.filter(company=company, stock=True, promotion_tiers__isnull=False)
            .filter(branch_q)
            .exclude(status=Product.Status.ARCHIVED)
            .prefetch_related("promotion_tiers", "images")
            .distinct()
            .order_by("name")[:50]
        )
        for p in products:
            tiers = list(p.promotion_tiers.all())
            if not tiers:
                continue
            best = max(t.discount_percent for t in tiers)
            parts = [f"от {_fmt_money(t.min_amount)} сом — {_fmt_money(t.discount_percent)} %" for t in tiers]
            image = next((i for i in p.images.all() if i.is_primary and i.image), None) or next(
                (i for i in p.images.all() if i.image), None
            )
            image_url = None
            if image is not None:
                try:
                    image_url = request.build_absolute_uri(image.image.url)
                except Exception:
                    image_url = None
            promos.append({
                "id": f"product-{p.id}",
                "title": f"Скидка до {_fmt_money(best)} % на «{p.name}»",
                "text": "Скидка в чеке: " + "; ".join(parts) + ".",
                "imageUrl": image_url,
                "validUntil": None,
                "productId": str(p.id),
            })
        return Response(promos)


# ======================================================================
# Этап 2. Вход через Telegram
# ======================================================================


class TelegramAuthStartView(PublicAppAPIView):
    """POST /api/v1/auth/telegram/start {fullName, birthDate, referralCode?}"""

    throttle_classes = [AuthStartIPThrottle]

    def post(self, request):
        if not telegram.is_configured():
            return error_response(
                "auth_unavailable", "Вход через Telegram временно недоступен.", status.HTTP_503_SERVICE_UNAVAILABLE
            )
        data = request.data if isinstance(request.data, dict) else {}
        full_name = _clean_name(data.get("fullName"))
        birth_date = _parse_birth_date(data.get("birthDate"))
        referral_code = str(data.get("referralCode") or "").strip().upper()[:16]
        nonce = secrets.token_urlsafe(24)
        TelegramAuthNonce.objects.create(
            nonce=nonce,
            full_name=full_name,
            birth_date=birth_date,
            referral_code=referral_code,
            expires_at=timezone.now() + timedelta(seconds=NONCE_TTL_SECONDS),
            created_ip=_client_ip(request),
        )
        return Response(
            {
                "nonce": nonce,
                "botUrl": f"https://t.me/{telegram.bot_username()}?start={nonce}",
                "expiresIn": NONCE_TTL_SECONDS,
            },
            status=status.HTTP_201_CREATED,
        )


class TelegramAuthStatusView(PublicAppAPIView):
    """GET /api/v1/auth/telegram/status?nonce=… → pending | ok (+token, один раз) | expired."""

    throttle_classes = [AuthStatusNonceThrottle, AuthStatusIPThrottle]

    def get(self, request):
        nonce = (request.query_params.get("nonce") or "").strip()
        if not nonce or len(nonce) > 64:
            raise AppAPIError("validation_error", "Укажите nonce.")
        replay_key = "capp:auth_status_ok:" + hash_secret(nonce)
        with transaction.atomic():
            obj = TelegramAuthNonce.objects.select_for_update(of=("self",)).select_related("customer").filter(nonce=nonce).first()
            if obj is not None and obj.status == TelegramAuthNonce.Status.CONSUMED:
                # iPhone может «заморозить» первый ответ в фоне — тот же ответ ещё минуту
                cached = cache.get(replay_key)
                if cached:
                    return Response(cached)
            if obj is None:
                raise AppAPIError("not_found", "Вход не найден. Начните заново.", http_status=404)
            if obj.status == TelegramAuthNonce.Status.PENDING:
                if obj.is_expired:
                    obj.status = TelegramAuthNonce.Status.EXPIRED
                    obj.save(update_fields=["status"])
                    return Response({"status": "expired"})
                return Response({"status": "pending"})
            if obj.status == TelegramAuthNonce.Status.OK and obj.customer and obj.customer.deleted_at is None:
                if obj.is_expired:
                    obj.status = TelegramAuthNonce.Status.EXPIRED
                    obj.save(update_fields=["status"])
                    return Response({"status": "expired"})
                customer = obj.customer
                _, raw = AppToken.issue(customer, request.headers.get("User-Agent", ""))
                obj.status = TelegramAuthNonce.Status.CONSUMED
                obj.full_name = ""
                obj.birth_date = None
                obj.save(update_fields=["status", "full_name", "birth_date"])
                referral_code = obj.referral_code
            else:
                # токен уже выдан (nonce одноразовый) или вход истёк
                return Response({"status": "expired"})
        if referral_code:
            try:
                services.apply_referral_code(customer, referral_code)
            except services.ReferralError as exc:
                logger.info("referral at login not applied: %s", exc.code)
        payload = {"status": "ok", "token": raw, "clientId": str(customer.id), "profile": me_payload(customer)}
        try:
            cache.set(replay_key, payload, AUTH_STATUS_REPLAY_SECONDS)
        except Exception:
            logger.debug("auth status replay cache failed", exc_info=True)
        return Response(payload)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class TelegramWebhookView(PublicAppAPIView):
    """POST /api/v1/auth/telegram/webhook/ — обновления бота входа (заголовок секрета обязателен)."""

    throttle_classes = [WebhookThrottle]

    def post(self, request):
        secret = telegram.webhook_secret()
        if not secret or not telegram.bot_token():
            return error_response("auth_unavailable", "Бот не настроен.", status.HTTP_503_SERVICE_UNAVAILABLE)
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(got.encode(), secret.encode()):
            return error_response("forbidden", "Неверный секрет.", status.HTTP_403_FORBIDDEN)
        update = request.data if isinstance(request.data, dict) else {}
        try:
            result = telegram.handle_update(update)
        except Exception:
            logger.exception("client app webhook failed")
            result = "error"
        # Telegram всегда получает 200 — иначе будет повторять обновление
        return Response({"ok": True, "result": result})


class LogoutView(AppAPIView):
    """
    POST /api/v1/auth/logout {pushToken?} — отзывает текущий токен и push-токены (ТЗ 14.10, п. 2.4:
    после выхода уведомления не приходят). Если приложение прислало pushToken — удаляется только
    токен этого устройства, иначе все push-токены клиента.
    """

    def post(self, request):
        token = request.auth
        if token is not None:
            AppToken.objects.filter(pk=token.pk).update(revoked_at=timezone.now())
        data = request.data if isinstance(request.data, dict) else {}
        push = str(data.get("pushToken") or data.get("token") or "").strip()
        qs = AppPushToken.objects.filter(customer=request.user)
        if push:
            qs = qs.filter(token=push)
        qs.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ======================================================================
# Профиль
# ======================================================================


class MeView(AppAPIView):
    def get(self, request):
        return Response(me_payload(request.user))

    def patch(self, request):
        customer = request.user
        data = request.data if isinstance(request.data, dict) else {}
        changed = []
        if "fullName" in data:
            customer.full_name = _clean_name(data.get("fullName"))
            changed.append("full_name")
        if "birthDate" in data:
            customer.birth_date = _parse_birth_date(data.get("birthDate"))
            changed.append("birth_date")
        if "lang" in data:
            lang = str(data.get("lang") or "").lower()
            if lang not in ("ru", "ky"):
                raise AppAPIError("validation_error", "lang: ru или ky.", extra={"fields": {"lang": "invalid"}})
            customer.lang = lang
            changed.append("lang")
        if "phone" in data:
            raise AppAPIError("phone_change_requires_telegram", "Телефон меняется только повторным входом через Telegram.")
        if changed:
            customer.save(update_fields=changed + ["updated_at"])
        return Response(me_payload(customer))

    def delete(self, request):
        """
        Удаление аккаунта: профиль обезличивается, токены/push/QR удаляются.
        Записи клиентов в магазинах (main.Client, продажи, бонусы) — учёт магазина, их не трогаем.
        """
        customer = request.user
        with transaction.atomic():
            p_hash = customer.phone_hash or services.phone_hash(customer.phone or "")
            customer.tokens.all().delete()
            customer.push_tokens.all().delete()
            customer.qr_tokens.all().delete()
            TelegramAuthNonce.objects.filter(customer=customer).delete()
            type(customer).objects.filter(pk=customer.pk).update(
                phone=None,
                phone_hash=p_hash,
                full_name="",
                birth_date=None,
                telegram_user_id=None,
                referral_code=None,
                deleted_at=timezone.now(),
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


# ======================================================================
# Этап 3. Баланс и история
# ======================================================================


class BalanceView(AppAPIView):
    def get(self, request):
        return Response(services.customer_balances(request.user))


class PurchasesView(AppAPIView):
    def get(self, request):
        try:
            limit = int(request.query_params.get("limit") or 20)
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 50))
        cursor = request.query_params.get("cursor") or None
        if cursor and services.decode_cursor(cursor) is None:
            raise AppAPIError("validation_error", "Неверный cursor.")
        items, next_cursor = services.customer_purchases(request.user, cursor=cursor, limit=limit)
        return Response({"items": items, "nextCursor": next_cursor})


class PurchaseDetailView(AppAPIView):
    def get(self, request, purchase_id):
        data = services.customer_purchase_detail(request.user, purchase_id)
        if data is None:
            raise AppAPIError("not_found", "Покупка не найдена.", http_status=404)
        return Response(data)


# ======================================================================
# Этап 4. Push
# ======================================================================


class PushTokenView(AppAPIView):
    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        token = str(data.get("token") or "").strip()
        platform = str(data.get("platform") or "android").lower()
        if not token or len(token) > 255 or not (
            token.startswith("ExponentPushToken[") or token.startswith("ExpoPushToken[")
        ):
            raise AppAPIError("validation_error", "Нужен Expo push token.", extra={"fields": {"token": "invalid"}})
        if platform not in AppPushToken.Platform.values:
            raise AppAPIError("validation_error", "platform: android | ios.", extra={"fields": {"platform": "invalid"}})
        obj, created = AppPushToken.objects.update_or_create(
            token=token, defaults={"customer": request.user, "platform": platform}
        )
        # не больше 10 устройств на клиента
        extra = list(
            AppPushToken.objects.filter(customer=request.user).order_by("-updated_at").values_list("pk", flat=True)[10:]
        )
        if extra:
            AppPushToken.objects.filter(pk__in=extra).delete()
        return Response({"ok": True}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def delete(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        token = str(data.get("token") or request.query_params.get("token") or "").strip()
        qs = AppPushToken.objects.filter(customer=request.user)
        if token:
            qs = qs.filter(token=token)
        qs.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ======================================================================
# Этап 5. Приглашения и QR
# ======================================================================


class ReferralView(AppAPIView):
    def get(self, request):
        return Response(services.referral_summary(request.user))


class ReferralApplyView(AppAPIView):
    throttle_classes = [ReferralApplyThrottle]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            services.apply_referral_code(request.user, data.get("code"))
        except services.ReferralError as exc:
            # коды по ТЗ 14.10, раздел 3: referral_self | referral_already_applied | referral_not_found | referral_too_late
            http = {
                "referral_not_found": status.HTTP_404_NOT_FOUND,
                "referral_self": status.HTTP_400_BAD_REQUEST,
            }.get(exc.code, status.HTTP_409_CONFLICT)
            raise AppAPIError(exc.code, exc.message, http_status=http)
        return Response(services.referral_summary(request.user))


class QrTokenView(AppAPIView):
    throttle_classes = [QrTokenThrottle]

    def get(self, request):
        ttl = int(getattr(settings, "CLIENT_APP_QR_TTL_SECONDS", QR_TTL_SECONDS) or QR_TTL_SECONDS)
        raw = secrets.token_urlsafe(18)
        expires = timezone.now() + timedelta(seconds=ttl)
        AppQrToken.objects.create(customer=request.user, token_hash=hash_secret(raw), expires_at=expires)
        return Response({
            "token": raw,
            "qr": f"NURCRMT{raw}",
            "expiresAt": services.iso(expires),
            "expiresIn": ttl,
        })
