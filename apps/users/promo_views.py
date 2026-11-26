"""Промокоды / подарочные коды (09.11.2026).

POST /api/users/company/promo/redeem/ {code}  — владелец/админ вводит код в Маркетплейсе:
     включаются все функции кода (CompanyAddon, source=promo). Неверные коды считаются вместе с ключами
     (FeatureKeyAttempt, не больше KEY_ATTEMPTS_PER_HOUR в час).
GET  /api/users/platform-admin/promo-codes/         — список (поддержка)
     Доступ: платформенный админ ИЛИ заголовок X-Support-Key = SUPPORT_PROMO_KEY из .env.
     Ключ ставит nginx support.nurcrm.kg (/nurpromo/) только после проверки сессии оператора (auth_request /api/me).
POST /api/users/platform-admin/promo-codes/         {features:[...], days?, max_uses?, valid_until?, note?, code?}
PATCH /api/users/platform-admin/promo-codes/{id}/   {active?, note?, max_uses?, valid_until?}
"""
import hmac
import os
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.app_settings_views import KEY_ATTEMPTS_PER_HOUR, _client_ip, _company_of, _is_owner_or_admin
from apps.users.models import CompanyAddon, FeatureKeyAttempt, PromoCode, PromoRedemption
from apps.users.permissions import IsPlatformAdmin

class IsPlatformAdminOrSupportKey(IsPlatformAdmin):
    message = "Нужны права платформенного администратора или ключ поддержки."

    def has_permission(self, request, view):
        key = os.environ.get("SUPPORT_PROMO_KEY", "")
        got = request.headers.get("X-Support-Key", "")
        if key and len(key) >= 32 and got and hmac.compare_digest(key, got):
            return True
        return super().has_permission(request, view)


def _creator(request):
    u = getattr(request, "user", None)
    return u if u is not None and getattr(u, "is_authenticated", False) else None


CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _apply_addon(company, code, days, note):
    """Включает/продлевает функцию; бессрочную не укорачивает. Возвращает until (или None)."""
    today = timezone.localdate()
    addon = CompanyAddon.objects.select_for_update().filter(company=company, code=code).first()
    already_forever = addon is not None and addon.is_effective(today) and addon.until is None
    if days:
        start = addon.until if (addon and addon.is_effective(today) and addon.until and addon.until > today) else today
        until = start + timedelta(days=days)
    else:
        until = None
    if addon is None:
        addon = CompanyAddon(company=company, code=code)
    if not already_forever:
        addon.until = until
    addon.active = True
    addon.source = "promo"
    addon.note = note[:255]
    addon.save()
    return addon.until


def _promo_payload(p, with_usage=False):
    d = {
        "id": str(p.id), "code": p.code, "features": list(p.features or []), "days": p.days,
        "max_uses": p.max_uses, "used_count": p.used_count, "active": p.active,
        "valid_until": p.valid_until.isoformat() if p.valid_until else None,
        "note": p.note, "created_at": p.created_at.isoformat(),
    }
    if with_usage:
        d["companies"] = [
            {"company": r.company.name, "at": r.created_at.isoformat()}
            for r in p.redemptions.select_related("company").order_by("-created_at")[:50]
        ]
    return d


class PromoRedeemAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.users.serializers import company_feature_codes

        company = _company_of(request.user)
        if company is None:
            raise PermissionDenied("Пользователь не привязан к компании.")
        if not _is_owner_or_admin(request.user, company):
            raise PermissionDenied("Ввести промокод может владелец или администратор.")
        raw = re.sub(r"\s+", "", str(request.data.get("code") or "")).upper()
        if not raw:
            raise ValidationError({"code": "Введите промокод."})

        ip = _client_ip(request)
        recent = FeatureKeyAttempt.objects.filter(created_at__gte=timezone.now() - timedelta(hours=1))
        if max(recent.filter(company=company).count(), recent.filter(ip=ip).count() if ip else 0) >= KEY_ATTEMPTS_PER_HOUR:
            resp = Response({"detail": "Слишком много неверных кодов. Попробуйте через час.", "code": "too_many_attempts"},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
            resp["Retry-After"] = "3600"
            return resp

        def fail(msg, code, st=status.HTTP_400_BAD_REQUEST):
            FeatureKeyAttempt.objects.create(company=company, ip=ip)
            return Response({"detail": msg, "code": code}, status=st)

        with transaction.atomic():
            promo = PromoCode.objects.select_for_update().filter(code__iexact=raw).first()
            if promo is None or not promo.active:
                return fail("Промокод не найден.", "invalid_code")
            if PromoRedemption.objects.filter(promo=promo, company=company).exists():
                return Response({"detail": "Этот промокод вы уже вводили.", "code": "already_redeemed"}, status=status.HTTP_409_CONFLICT)
            if promo.valid_until and promo.valid_until < timezone.localdate():
                return fail("Срок промокода истёк.", "expired")
            if promo.used_count >= promo.max_uses:
                return fail("Промокод уже использован.", "used_up", status.HTTP_409_CONFLICT)
            enabled = []
            for code in promo.features or []:
                until = _apply_addon(company, code, promo.days, f"промокод {promo.code}")
                enabled.append({"code": code, "until": until.isoformat() if until else None})
            try:
                PromoRedemption.objects.create(promo=promo, company=company, user=request.user)
            except IntegrityError:
                return Response({"detail": "Этот промокод вы уже вводили.", "code": "already_redeemed"}, status=status.HTTP_409_CONFLICT)
            PromoCode.objects.filter(pk=promo.pk).update(used_count=F("used_count") + 1)
        return Response({"enabled": enabled, "features": company_feature_codes(company)})


def _clean(data, partial=False):
    out = {}
    if "features" in data or not partial:
        feats = data.get("features") or []
        if not isinstance(feats, list) or not feats:
            raise ValidationError({"features": "Выберите хотя бы одну функцию."})
        feats = [str(x).strip().lower() for x in feats]
        bad = [x for x in feats if not CODE_RE.match(x)]
        if bad:
            raise ValidationError({"features": f"Неверный код: {', '.join(bad)}"})
        out["features"] = sorted(set(feats))
    for k in ("days", "max_uses"):
        if k in data:
            v = data.get(k)
            if v in (None, ""):
                if k == "max_uses":
                    raise ValidationError({k: "Число от 1."})
                out[k] = None
            else:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ValidationError({k: "Целое число."})
                if v < 1 or v > 100000:
                    raise ValidationError({k: "От 1 до 100000."})
                out[k] = v
    if "valid_until" in data:
        v = data.get("valid_until")
        if v in (None, ""):
            out["valid_until"] = None
        else:
            from datetime import date
            try:
                out["valid_until"] = date.fromisoformat(str(v)[:10])
            except ValueError:
                raise ValidationError({"valid_until": "Дата ГГГГ-ММ-ДД."})
    if "note" in data:
        out["note"] = str(data.get("note") or "")[:255]
    if "active" in data:
        out["active"] = bool(data.get("active"))
    if "code" in data and not partial and data.get("code"):
        c = re.sub(r"\s+", "", str(data["code"])).upper()
        if not re.match(r"^[A-Z0-9][A-Z0-9-]{3,39}$", c):
            raise ValidationError({"code": "Латиница, цифры и «-», 4–40 символов."})
        out["code"] = c
    return out


class PlatformPromoListAPIView(APIView):
    permission_classes = [IsPlatformAdminOrSupportKey]

    def get(self, request):
        qs = PromoCode.objects.all()[:300]
        return Response({"results": [_promo_payload(p) for p in qs]})

    def post(self, request):
        data = _clean(request.data)
        if "code" in data and PromoCode.objects.filter(code__iexact=data["code"]).exists():
            raise ValidationError({"code": "Такой промокод уже есть."})
        from urllib.parse import unquote
        op = unquote(str(request.headers.get("X-Support-Operator") or ""))[:60]
        if op and not data.get("note"):
            data["note"] = f"support: {op}"
        p = PromoCode.objects.create(created_by=_creator(request), **data)
        return Response(_promo_payload(p), status=status.HTTP_201_CREATED)


class PlatformPromoDetailAPIView(APIView):
    permission_classes = [IsPlatformAdminOrSupportKey]

    def get(self, request, pk):
        return Response(_promo_payload(get_object_or_404(PromoCode, pk=pk), with_usage=True))

    def patch(self, request, pk):
        p = get_object_or_404(PromoCode, pk=pk)
        data = _clean(request.data, partial=True)
        data.pop("code", None)
        for k, v in data.items():
            setattr(p, k, v)
        p.save()
        return Response(_promo_payload(p, with_usage=True))
