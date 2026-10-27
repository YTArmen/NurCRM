"""
PIN администратора (ТЗ «Админка» п.10).

  POST users/auth/pin/          {pin, current_pin?}  — задать/сменить PIN (4–6 цифр)
  POST users/auth/pin/verify/   {pin}                — токен подтверждения на 5 минут
  GET/PATCH users/auth/pin/settings/ {require_pin}   — настройка компании «Подтверждать PIN»

Опасные действия закрываются декоратором @pin_guard("DELETE", ...): если у компании включено
«Подтверждать PIN», нужен заголовок X-Confirm-Token, иначе 403 {"code": "pin_required"}.
"""
import re
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AdminPin, CompanyPinSetting

TOKEN_TTL = 300            # 5 минут
MAX_ATTEMPTS = 5
LOCK_MINUTES = 15
_SALT = "nurcrm.admin-pin-confirm"
_PIN_RE = re.compile(r"^\d{4,6}$")
HEADER = "HTTP_X_CONFIRM_TOKEN"


class PinRequired(APIException):
    status_code = 403
    default_code = "pin_required"


def _company(user):
    return getattr(user, "company", None) or getattr(user, "owned_company", None)


def _is_admin(user):
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "role", None) in ("owner", "admin")
        or getattr(user, "owned_company_id", None)
    )


def make_token(user):
    return signing.dumps({"u": str(user.pk), "c": str(getattr(_company(user), "pk", ""))}, salt=_SALT)


def token_is_valid(token, user):
    try:
        data = signing.loads(token, salt=_SALT, max_age=TOKEN_TTL)
    except signing.BadSignature:
        return False
    company = _company(user)
    return data.get("u") == str(user.pk) and data.get("c") == str(getattr(company, "pk", ""))


def _company_admin_hashes(user):
    company = _company(user)
    if company is None:
        return []
    return list(
        AdminPin.objects.filter(
            Q(user__company=company, user__role__in=("owner", "admin")) | Q(user_id=company.owner_id),
            user__is_active=True,
        ).exclude(pin_hash="").values_list("pin_hash", flat=True)[:20]
    )


def pin_required_for(user):
    company = _company(user)
    if company is None:
        return False
    return CompanyPinSetting.objects.filter(company=company, require_pin=True).exists()


def pin_permission(methods=None):
    methods = tuple(m.upper() for m in methods) if methods else None

    class _PinGuard(permissions.BasePermission):
        def has_permission(self, request, view):
            if methods is not None and request.method not in methods:
                return True
            if request.method in ("GET", "HEAD", "OPTIONS"):
                return True
            user = request.user
            if not getattr(user, "is_authenticated", False) or not pin_required_for(user):
                return True
            token = request.META.get(HEADER, "")
            if token and token_is_valid(token, user):
                return True
            raise PinRequired({"detail": "Нужно подтвердить действие PIN-кодом.", "code": "pin_required"})

    return _PinGuard


def pin_guard(*methods):
    """Декоратор класса view: добавляет проверку X-Confirm-Token (для указанных методов или для всех)."""

    def deco(view_cls):
        view_cls.permission_classes = [*list(view_cls.permission_classes), pin_permission(methods or None)]
        return view_cls

    return deco


def _clean_pin(value):
    value = str(value or "").strip()
    if not _PIN_RE.match(value):
        raise ValidationError({"pin": "PIN — от 4 до 6 цифр."})
    return value


def _lock_error(rec):
    left = max(int((rec.locked_until - timezone.now()).total_seconds()), 1)
    resp = Response(
        {"detail": f"Слишком много попыток. Повторите через {left // 60 + 1} мин.", "code": "pin_locked", "retry_after": left},
        status=429,
    )
    resp["Retry-After"] = str(left)
    return resp


class PinSetView(APIView):
    """POST users/auth/pin/ — задать или сменить PIN (владелец/администратор)."""

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        user = request.user
        if not _is_admin(user):
            raise PermissionDenied({"detail": "PIN может задать владелец или администратор.", "code": "permission_denied"})
        new_pin = _clean_pin(request.data.get("pin"))
        rec = AdminPin.objects.select_for_update().filter(user=user).first()
        if rec is not None and rec.pin_hash:
            if rec.locked_until and rec.locked_until > timezone.now():
                return _lock_error(rec)
            if not check_password(str(request.data.get("current_pin") or ""), rec.pin_hash):
                rec.failed_attempts += 1
                if rec.failed_attempts >= MAX_ATTEMPTS:
                    rec.failed_attempts = 0
                    rec.locked_until = timezone.now() + timedelta(minutes=LOCK_MINUTES)
                rec.save(update_fields=["failed_attempts", "locked_until", "updated_at"])
                raise ValidationError({"current_pin": "Текущий PIN неверный.", "code": "pin_invalid"})
            rec.pin_hash = make_password(new_pin)
            rec.failed_attempts, rec.locked_until = 0, None
            rec.save()
        elif rec is not None:
            rec.pin_hash = make_password(new_pin)
            rec.save()
        else:
            AdminPin.objects.create(user=user, pin_hash=make_password(new_pin))
        return Response({"has_pin": True})


class PinVerifyView(APIView):
    """POST users/auth/pin/verify/ {pin} -> {token, expires_in}. 5 ошибок подряд — блокировка на 15 минут."""

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        user = request.user
        rec = AdminPin.objects.select_for_update().filter(user=user).first()
        admin_user = _is_admin(user)
        if rec is None and admin_user:
            return Response({"detail": "PIN ещё не задан.", "code": "pin_not_set"}, status=status.HTTP_400_BAD_REQUEST)
        if rec is None:
            # сотрудник без своего PIN подтверждает PIN-ом владельца/админа компании;
            # строка с пустым хэшем нужна только для счётчика неверных попыток
            if not _company_admin_hashes(user):
                return Response({"detail": "PIN ещё не задан.", "code": "pin_not_set"}, status=status.HTTP_400_BAD_REQUEST)
            rec = AdminPin.objects.create(user=user, pin_hash="")
        if rec.locked_until and rec.locked_until > timezone.now():
            return _lock_error(rec)
        pin = str(request.data.get("pin") or "")
        ok = check_password(pin, rec.pin_hash) if rec.pin_hash else any(check_password(pin, h) for h in _company_admin_hashes(user))
        if ok:
            if rec.failed_attempts or rec.locked_until:
                rec.failed_attempts, rec.locked_until = 0, None
                rec.save(update_fields=["failed_attempts", "locked_until", "updated_at"])
            return Response({"token": make_token(user), "expires_in": TOKEN_TTL})
        rec.failed_attempts += 1
        left = MAX_ATTEMPTS - rec.failed_attempts
        if rec.failed_attempts >= MAX_ATTEMPTS:
            rec.failed_attempts = 0
            rec.locked_until = timezone.now() + timedelta(minutes=LOCK_MINUTES)
            rec.save(update_fields=["failed_attempts", "locked_until", "updated_at"])
            return _lock_error(rec)
        rec.save(update_fields=["failed_attempts", "updated_at"])
        return Response({"detail": "Неверный PIN.", "code": "pin_invalid", "attempts_left": left}, status=status.HTTP_400_BAD_REQUEST)


class PinSettingsView(APIView):
    """GET users/auth/pin/settings/ -> {require_pin, has_pin}; PATCH {require_pin} — только владелец/администратор."""

    permission_classes = [permissions.IsAuthenticated]

    def _payload(self, user):
        has = AdminPin.objects.filter(user=user).exclude(pin_hash="").exists() or (not _is_admin(user) and bool(_company_admin_hashes(user)))
        return {"require_pin": pin_required_for(user), "has_pin": has}

    def get(self, request):
        return Response(self._payload(request.user))

    def patch(self, request):
        user = request.user
        company = _company(user)
        if company is None or not _is_admin(user):
            raise PermissionDenied({"detail": "Менять настройку может владелец или администратор.", "code": "permission_denied"})
        raw = request.data.get("require_pin")
        if raw is None:
            raise ValidationError({"require_pin": "Обязательное поле."})
        value = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
        if value and not AdminPin.objects.filter(user=user).exclude(pin_hash="").exists():
            raise ValidationError({"require_pin": "Сначала задайте PIN.", "code": "pin_not_set"})
        CompanyPinSetting.objects.update_or_create(company=company, defaults={"require_pin": value})
        return Response(self._payload(user))
