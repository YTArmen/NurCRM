"""
ТЗ ч.13: настройки программ на сервере и купленные функции компании.

  GET/PATCH /api/users/app-settings/{app}/        — личные настройки пользователя (1.1)
  GET/PATCH /api/main/app-settings/{app}/?branch= — общие настройки компании / филиала (1.2)
  POST      /api/users/company/features/activate/ — включить функцию компании ключом (2.3)

Тело PATCH — JSON-объект: верхний уровень сливается, null удаляет ключ. Секреты (ключи ИИ,
токен бота) кладутся в объект "secrets": хранятся зашифрованными, в ответе — только владельцу
и администратору (в личных настройках — самому пользователю). If-Match: <version> — при
устаревшей версии 412 и текущие настройки.
"""
import json
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.http_cache import ETagMixin
from apps.main.telegram_bot.crypto import decrypt_secret, encrypt_secret
from apps.users.models import (
    Branch,
    CompanyAddon,
    CompanyAppSettings,
    FeatureActivationKey,
    UserAppSettings,
)

APP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_BYTES = 256 * 1024
SECRETS_KEY = "secrets"
META_KEY = "_meta"  # ТЗ Н-17: служебные версии по ключам внутри JSON, клиенту не отдаются и не пишутся
EXPECTED_KEY = "_expected"
MAX_KEY_BYTES = 64 * 1024
SECRET_PREFIX = "secret_"


class TooLarge(APIException):
    """ТЗ Н-17: 413 too_large."""

    status_code = 413
    default_code = "too_large"


class KeyVersionConflict(Exception):
    def __init__(self, keys):
        self.keys = keys


def _check_app(app):
    if not APP_RE.match(app or ""):
        raise ValidationError({"app": "Имя программы: латиница, цифры, «-», до 64 символов."})


def _company_of(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _is_owner_or_admin(user, company):
    if getattr(user, "is_superuser", False):
        return True
    if company is not None and getattr(company, "owner_id", None) == user.id:
        return True
    return getattr(user, "role", None) in ("owner", "admin")


def _decrypt_all(enc):
    out = {}
    for k, v in (enc or {}).items():
        raw = decrypt_secret(v)
        try:
            out[k] = json.loads(raw)
        except (TypeError, ValueError):
            out[k] = raw
    return out


def _payload(obj, *, with_secrets):
    settings = dict(obj.settings or {}) if obj else {}
    settings.pop(META_KEY, None)  # ТЗ Н-17: служебное
    if not with_secrets:  # ТЗ Н-17: secret_* только владельцу/админу
        settings = {k: v for k, v in settings.items() if not str(k).startswith(SECRET_PREFIX)}
    if obj and obj.secrets and with_secrets:
        settings[SECRETS_KEY] = _decrypt_all(obj.secrets)
    kv = {k: int((v or {}).get("version") or 0) for k, v in _key_versions(obj.settings or {}).items()} if obj else {}
    if not with_secrets:
        kv = {k: v for k, v in kv.items() if not str(k).startswith(SECRET_PREFIX)}
    return {
        "settings": settings,
        "key_versions": kv,  # ТЗ Н-17: версии по ключам для _expected
        "version": obj.version if obj else 0,
        "updated_at": obj.updated_at.isoformat() if obj and obj.updated_at else None,
    }


def _response(payload, code=status.HTTP_200_OK):
    resp = Response(payload, status=code)
    resp["ETag"] = f'"{payload["version"]}"'
    return resp


def _if_match(request):
    raw = (request.headers.get("If-Match") or "").strip()
    if not raw or raw == "*":
        return None
    raw = raw.removeprefix("W/").strip('"')
    try:
        return int(raw)
    except ValueError:
        raise ValidationError({"If-Match": "Ожидается номер версии."})


def _key_versions(settings):
    meta = settings.get(META_KEY)
    keys = meta.get("keys") if isinstance(meta, dict) else None
    return keys if isinstance(keys, dict) else {}


def _apply_patch(obj, body, user=None, expected_keys=None):
    """Сливает тело в obj.settings / obj.secrets. Возвращает True, если что-то изменилось."""
    if not isinstance(body, dict):
        raise ValidationError({"detail": "Тело — JSON-объект с настройками."})
    body = dict(body)
    if META_KEY in body:
        raise ValidationError({"detail": "Ключ _meta зарезервирован.", "code": "reserved_key"})
    exp = body.pop(EXPECTED_KEY, None)
    if exp is not None:
        if not isinstance(exp, dict) or any(isinstance(v, bool) or not isinstance(v, int) for v in exp.values()):
            raise ValidationError({EXPECTED_KEY: "Ожидается объект {ключ: версия}."})
        expected_keys = exp
    for k, v in body.items():  # ТЗ Н-17: лимит на ключ
        if k != SECRETS_KEY and len(json.dumps(v, ensure_ascii=False).encode("utf-8")) > MAX_KEY_BYTES:
            raise TooLarge({"detail": f"Ключ «{k}» больше 64 КБ.", "code": "too_large"})
    settings = dict(obj.settings or {})
    secrets = dict(obj.secrets or {})
    before = (json.dumps(settings, sort_keys=True), dict(secrets))
    plain_secrets = None
    if expected_keys:  # ТЗ Н-17: условная запись по ключам
        cur = _key_versions(settings)
        bad = [k for k, n in expected_keys.items() if int((cur.get(k) or {}).get("version") or 0) != n]
        if bad:
            raise KeyVersionConflict(bad)
    touched = []

    for key, value in body.items():
        if key == SECRETS_KEY:
            if value is None:
                secrets = {}
                continue
            if not isinstance(value, dict):
                raise ValidationError({SECRETS_KEY: "Ожидается объект {имя: значение}."})
            plain_secrets = plain_secrets if plain_secrets is not None else _decrypt_all(secrets)
            for sk, sv in value.items():
                if sv is None:
                    secrets.pop(sk, None)
                    plain_secrets.pop(sk, None)
                elif plain_secrets.get(sk) != sv:
                    secrets[sk] = encrypt_secret(json.dumps(sv, ensure_ascii=False))
                    plain_secrets[sk] = sv
        elif value is None:
            if key in settings:
                touched.append(key)
            settings.pop(key, None)
        else:
            if settings.get(key) != value or key not in settings:
                touched.append(key)
            settings[key] = value

    if touched:
        meta = dict(settings.get(META_KEY) or {})
        keys = dict(meta.get("keys") or {})
        now = timezone.now().isoformat()
        for k in touched:
            keys[k] = {
                "version": int((keys.get(k) or {}).get("version") or 0) + 1,
                "updated_at": now,
                "updated_by": str(getattr(user, "id", "") or "") or None,
            }
        meta["keys"] = keys
        settings[META_KEY] = meta

    size = len(json.dumps(settings, ensure_ascii=False).encode("utf-8")) + len(json.dumps(secrets).encode("utf-8"))
    if size > MAX_BYTES:
        raise TooLarge({"detail": "Настройки больше 256 КБ.", "code": "too_large"})

    changed = (json.dumps(settings, sort_keys=True), secrets) != before
    if changed:
        obj.settings = settings
        obj.secrets = secrets
        obj.version = (obj.version or 0) + 1
    return changed


class _AppSettingsMixin(ETagMixin):
    """get/patch для записи настроек (личной или компании)."""

    model = None

    def _lookup(self, request, app):  # -> dict фильтра записи
        raise NotImplementedError

    def _can_write(self, request):
        return True

    def _with_secrets(self, request):
        return True

    def _extra(self, request, app, obj):
        return {}

    def get(self, request, app):
        _check_app(app)
        obj = self.model.objects.filter(**self._lookup(request, app)).first()
        payload = _payload(obj, with_secrets=self._with_secrets(request))
        payload.update(self._extra(request, app, obj))
        return _response(payload)

    def patch(self, request, app):
        _check_app(app)
        if not self._can_write(request):
            raise PermissionDenied("Менять общие настройки может владелец или администратор.")
        lookup = self._lookup(request, app)
        expected = _if_match(request)
        for _attempt in range(2):
            try:
                with transaction.atomic():
                    obj = self.model.objects.select_for_update().filter(**lookup).first()
                    if obj is None:
                        obj = self.model(**lookup)
                    if expected is not None and expected != (obj.version or 0):
                        payload = _payload(obj if obj.pk else None, with_secrets=self._with_secrets(request))
                        payload["detail"] = "Настройки изменились на другом устройстве — перечитайте и слейте."
                        payload["code"] = "version_conflict"
                        return _response(payload, status.HTTP_412_PRECONDITION_FAILED)
                    try:
                        changed = _apply_patch(obj, request.data, request.user)
                    except KeyVersionConflict as e:  # ТЗ Н-17: 409 с текущими значениями этих ключей
                        payload = _payload(obj if obj.pk else None, with_secrets=self._with_secrets(request))
                        cur_keys = _key_versions(obj.settings or {})
                        payload["conflict_keys"] = {
                            k: {"value": payload["settings"].get(k), "version": int((cur_keys.get(k) or {}).get("version") or 0)}
                            for k in e.keys
                        }
                        payload["detail"] = "Ключи изменились на другом устройстве — перечитайте и слейте."
                        payload["code"] = "version_conflict"
                        return _response(payload, status.HTTP_409_CONFLICT)
                    if changed or not obj.pk:
                        if hasattr(obj, "updated_by_id"):
                            obj.updated_by = request.user
                        obj.save()
                break
            except IntegrityError:
                continue  # запись создали параллельно — перечитываем и сливаем ещё раз
        payload = _payload(obj, with_secrets=self._with_secrets(request))
        payload.update(self._extra(request, app, obj))
        return _response(payload)

    def put(self, request, app):
        return self.patch(request, app)


class UserAppSettingsAPIView(_AppSettingsMixin, APIView):
    """GET/PATCH /api/users/app-settings/{app}/ — только сам пользователь (ТЗ ч.13, 1.1)."""

    permission_classes = [IsAuthenticated]
    model = UserAppSettings

    def _lookup(self, request, app):
        return {"user": request.user, "app": app}


class CompanyAppSettingsAPIView(_AppSettingsMixin, APIView):
    """
    GET/PATCH /api/main/app-settings/{app}/?branch=<id> (ТЗ ч.13, 1.2).
    Читают все сотрудники компании, меняют владелец и администратор.
    С ?branch= — настройки филиала; в ответе ещё effective = настройки компании + филиала.
    """

    permission_classes = [IsAuthenticated]
    model = CompanyAppSettings

    def _company(self, request):
        company = _company_of(request.user)
        if company is None:
            raise PermissionDenied("Пользователь не привязан к компании.")
        return company

    def _branch(self, request, company):
        raw = request.query_params.get("branch")
        if not raw:
            return None
        branch = Branch.objects.filter(id=raw, company=company).first() if _is_uuid(raw) else None
        if branch is None:
            raise NotFound("Филиал не найден.")
        return branch

    def _lookup(self, request, app):
        company = self._company(request)
        return {"company": company, "branch": self._branch(request, company), "app": app}

    def _can_write(self, request):
        return _is_owner_or_admin(request.user, self._company(request))

    def _with_secrets(self, request):
        return _is_owner_or_admin(request.user, self._company(request))

    def _extra(self, request, app, obj):
        lookup = self._lookup(request, app)
        if lookup["branch"] is None:
            return {}
        base = CompanyAppSettings.objects.filter(company=lookup["company"], branch__isnull=True, app=app).first()
        with_secrets = self._with_secrets(request)
        eff = dict(_payload(base, with_secrets=with_secrets)["settings"])
        own = _payload(obj, with_secrets=with_secrets)["settings"]
        if SECRETS_KEY in eff or SECRETS_KEY in own:
            own = dict(own)
            eff[SECRETS_KEY] = {**eff.get(SECRETS_KEY, {}), **own.pop(SECRETS_KEY, {})}
        eff.update(own)
        return {"effective": eff, "company_version": base.version if base else 0}


def _is_uuid(raw):
    import uuid

    try:
        uuid.UUID(str(raw))
        return True
    except ValueError:
        return False


def _client_ip(request):
    fwd = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return fwd or request.META.get("REMOTE_ADDR") or None


from core.api_compat import is_strict  # noqa: E402

KEY_ATTEMPTS_PER_HOUR = 10


class CompanyFeatureActivateAPIView(APIView):
    """
    POST /api/users/company/features/activate/  {"key": "ABCD-…"}   (ТЗ ч.13, 2.3)
    Ключ включает функцию компании (CompanyAddon) на свой срок и запоминается как использованный.
    → 200 {"code": "ai", "until": "2027-10-06" | null, "features": [...]}

    ТЗ Н-32 (3): ключ одноразовый — привязан к первой компании. Повтор той же компанией →
    200 + replayed:true (срок не продлевается); другая компания → 409 key_already_used.
    Больше 10 неверных ключей за час с компании или IP → 429 too_many_attempts.
    """

    permission_classes = [IsAuthenticated]
    force_strict = False  # ТЗ 29.10 Б-96: purchases/activate всегда отвечает по-новому (409/429)

    def post(self, request):
        from datetime import timedelta as _td

        from apps.users.models import FeatureKeyAttempt
        from apps.users.serializers import company_feature_codes

        company = _company_of(request.user)
        if company is None:
            raise PermissionDenied("Пользователь не привязан к компании.")
        if not _is_owner_or_admin(request.user, company):
            raise PermissionDenied("Включить функцию может владелец или администратор.")
        raw = str(request.data.get("key") or "").strip().upper()
        if not raw:
            raise ValidationError({"key": "Введите ключ."})

        ip = _client_ip(request)
        since = timezone.now() - _td(hours=1)
        recent = FeatureKeyAttempt.objects.filter(created_at__gte=since)
        by_company = recent.filter(company=company).count()
        by_ip = recent.filter(ip=ip).count() if ip else 0
        # Б-02/Б-36: старый клиент (без флага api_strict_2026_10) ждёт прежний 400 key_used, новый — 409/429
        strict = self.force_strict or is_strict(request, company)
        if max(by_company, by_ip) >= KEY_ATTEMPTS_PER_HOUR:
            if not strict:
                return Response({"detail": "Слишком много неверных ключей. Попробуйте через час.", "code": "key_used"},
                                status=status.HTTP_400_BAD_REQUEST)
            resp = Response(
                {"detail": "Слишком много неверных ключей. Попробуйте через час.", "code": "too_many_attempts"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            resp["Retry-After"] = "3600"
            return resp

        with transaction.atomic():
            key = FeatureActivationKey.objects.select_for_update().filter(key__iexact=raw).first()
            if key is None:
                FeatureKeyAttempt.objects.create(company=company, ip=ip)
                return Response({"detail": "Ключ не найден.", "code": "invalid_key"}, status=status.HTTP_400_BAD_REQUEST)
            if key.used_by_company_id and key.used_by_company_id != company.id:
                FeatureKeyAttempt.objects.create(company=company, ip=ip)
                if not strict:
                    return Response({"detail": "Ключ уже использован другой компанией.", "code": "key_used"},
                                    status=status.HTTP_400_BAD_REQUEST)
                return Response({"detail": "Ключ уже использован другой компанией.", "code": "key_already_used"},
                                status=status.HTTP_409_CONFLICT)

            addon = CompanyAddon.objects.select_for_update().filter(company=company, code=key.code).first()
            replayed = key.used_by_company_id is not None
            if not replayed:
                today = timezone.localdate()
                if key.days:
                    start = today
                    if addon and addon.is_effective(today) and addon.until and addon.until > today:
                        start = addon.until  # продление: срок добавляется к текущему
                    until = start + timedelta(days=key.days)
                else:
                    until = None
                already_forever = addon is not None and addon.is_effective(today) and addon.until is None
                if addon is None:
                    addon = CompanyAddon(company=company, code=key.code)
                if not already_forever:  # бессрочную функцию ключ со сроком не укорачивает
                    addon.until = until
                addon.active = True
                addon.source = "key"
                addon.note = (f"ключ {key.key}")[:255]
                addon.save()
                key.used_by_company = company
                key.used_by_user = request.user
                key.used_at = timezone.now()
                key.save(update_fields=["used_by_company", "used_by_user", "used_at"])

        data = {
            "code": key.code,
            "until": addon.until.isoformat() if addon and addon.until else None,
            "features": company_feature_codes(company),
        }
        if replayed:
            data["replayed"] = True
        return Response(data)


class CompanyFeatureTrialAPIView(APIView):
    """
    POST /api/users/company/features/trial/  {"code": "ai"}   (ТЗ Н-32, 3)
    Пробный доступ на settings.FEATURE_TRIAL_DAYS дней (по умолчанию 7), один раз на функцию
    для компании. → 200 {code, expires_at}; повтор → 409 trial_used; функция уже действует → 409 already_active.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.conf import settings as dj_settings

        from apps.users.models import Feature, FeatureTrial

        company = _company_of(request.user)
        if company is None:
            raise PermissionDenied("Пользователь не привязан к компании.")
        if not _is_owner_or_admin(request.user, company):
            raise PermissionDenied("Включить пробный доступ может владелец или администратор.")
        code = str(request.data.get("code") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", code):
            raise ValidationError({"code": "Укажите код функции."})
        known = (
            Feature.objects.filter(name=code).exists()
            or FeatureActivationKey.objects.filter(code=code).exists()
            or CompanyAddon.objects.filter(code=code).exists()
        )
        if not known:
            return Response({"detail": "Неизвестная функция.", "code": "unknown_feature"}, status=status.HTTP_400_BAD_REQUEST)
        today = timezone.localdate()
        with transaction.atomic():
            if FeatureTrial.objects.select_for_update().filter(company=company, code=code).exists():
                return Response({"detail": "Пробный доступ к этой функции уже использован.", "code": "trial_used"},
                                status=status.HTTP_409_CONFLICT)
            addon = CompanyAddon.objects.select_for_update().filter(company=company, code=code).first()
            if addon is not None and addon.is_effective(today):
                return Response({"detail": "Функция уже включена.", "code": "already_active"}, status=status.HTTP_409_CONFLICT)
            expires = today + timedelta(days=int(getattr(dj_settings, "FEATURE_TRIAL_DAYS", 7)))
            if addon is None:
                addon = CompanyAddon(company=company, code=code)
            addon.active = True
            addon.until = expires
            addon.source = "trial"
            addon.note = "пробный доступ"
            addon.save()
            FeatureTrial.objects.create(company=company, code=code, expires_at=expires)
        return Response({"code": code, "expires_at": expires.isoformat()})


class CompanyFeaturesListAPIView(APIView):
    """GET /api/users/company/features/ → [{code, until, source: tariff|key|trial|support}] (ТЗ Н-32, 3)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.serializers import company_features_with_source

        company = _company_of(request.user)
        if company is None:
            raise PermissionDenied("Пользователь не привязан к компании.")
        end = getattr(company, "end_date", None)
        return Response(company_features_with_source(company, plan_until=end.isoformat() if end else None))
