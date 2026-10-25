"""Сеансы, пароль и блокировка входа для пользователей модуля «Аренда» (ТЗ §3.7, §3.11).

Сеансы. В проекте нет JWT-blacklist, поэтому «завершить сеансы» = отметка `tokens_revoked_at` + `session_version`
в ArendaSecurity. В API аренды (get_ctx) токен принимается, только если
  - у него есть claim `sv` и он равен текущей версии (токены, выданные после смены пароля нашим ответом), или
  - claim нет, но `iat` строго позже отметки (обычный вход после смены).
Обновлённый через refresh access-токен наследует iat/sv от refresh, поэтому старый refresh тоже не обходит проверку.
Если пользователь создан из «Аренды» (created_by_arenda) и доступ закрыт — дополнительно User.is_active=False.

Блокировка входа: 5 неверных паролей подряд -> вход закрыт на 15 минут. Действует только для «арендных»
пользователей: у кого есть ArendaAccess или чья компания уже пользуется модулем (есть ArendaSettings).
Другие сферы не затрагиваются.
"""
import re
from datetime import timedelta

from django.utils import timezone

from core.exceptions import ApiError

MAX_FAILS = 5
LOCK_MINUTES = 15


def now():
    return timezone.now()


# ------------------------------------------------------------------ пароль


def validate_new_password(value: str):
    """>= 8 символов и хотя бы одна цифра. Возвращает список ошибок."""
    errs = []
    if len(value or "") < 8:
        errs.append("Не меньше 8 символов.")
    if not re.search(r"\d", value or ""):
        errs.append("Нужна хотя бы одна цифра.")
    return errs


def generate_temp_password(length=10):
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if not validate_new_password(pw) and re.search(r"[A-Za-z]", pw):
            return pw


# ------------------------------------------------------------------ запись безопасности


def get_sec(user, create=True):
    from .models_access import ArendaSecurity

    if not create:
        return ArendaSecurity.objects.filter(user=user).first()
    sec, _ = ArendaSecurity.objects.get_or_create(user=user)
    return sec


def revoke_sessions(user):
    """Завершить все выданные раньше токены пользователя (в API аренды)."""
    sec = get_sec(user)
    sec.tokens_revoked_at = now()
    sec.session_version += 1
    sec.save(update_fields=["tokens_revoked_at", "session_version", "updated_at"])
    return sec


def issue_tokens(user):
    """Новая пара токенов с текущей версией сеанса (после смены пароля текущий сеанс продолжается)."""
    from rest_framework_simplejwt.tokens import RefreshToken

    sec = get_sec(user)
    refresh = RefreshToken.for_user(user)
    refresh["sv"] = sec.session_version
    access = refresh.access_token
    access["sv"] = sec.session_version
    return {"access": str(access), "refresh": str(refresh)}


def stamp_token(token, user):
    """Проставить в токен текущую версию сеанса (если у пользователя есть запись безопасности)."""
    sec = get_sec(user, create=False)
    if sec is not None:
        token["sv"] = sec.session_version
    return token


def token_is_valid(user, token) -> bool:
    """token — request.auth (simplejwt) или None (force_authenticate / ключ API)."""
    if not getattr(user, "is_authenticated", False) or getattr(user, "pk", None) is None:
        return True
    sec = get_sec(user, create=False)
    if sec is None or sec.tokens_revoked_at is None or token is None:
        return True
    try:
        sv = token.get("sv")
        iat = token.get("iat")
    except Exception:
        return True
    if sv is not None:
        return int(sv) == sec.session_version
    if iat is None:
        return False
    return int(iat) > int(sec.tokens_revoked_at.timestamp())


def check_request_token(request):
    token = getattr(request, "_auth", None)  # не request.auth: не запускать повторную аутентификацию
    if not token_is_valid(request.user, token):
        raise ApiError("session_revoked", "Сеанс завершён. Войдите заново.", status_code=401)


# ------------------------------------------------------------------ блокировка входа


def is_arenda_user(user) -> bool:
    if user is None:
        return False
    if hasattr(user, "arenda_access") or get_sec(user, create=False) is not None:
        return True
    company = getattr(user, "company", None) or getattr(user, "owned_company", None)
    return bool(company and hasattr(company, "arenda_settings"))


def _find_user(identifier):
    from apps.users.models import User

    ident = (identifier or "").strip().lower()
    if not ident:
        return None
    return User.objects.filter(email__iexact=ident).first()


def locked_seconds(user) -> int:
    sec = get_sec(user, create=False)
    if sec and sec.locked_until and sec.locked_until > now():
        return int((sec.locked_until - now()).total_seconds()) + 1
    return 0


def guard_before_login(identifier):
    """Вызвать ДО проверки пароля. Бросает 429, если вход заблокирован; 403, если доступ закрыт."""
    user = _find_user(identifier)
    if not is_arenda_user(user):
        return None
    left = locked_seconds(user)
    if left:
        raise ApiError("account_locked", "Слишком много попыток входа. Подождите 15 минут.", status_code=429,
                       top={"retry_after": left})
    acc = getattr(user, "arenda_access", None)
    if acc is not None and not acc.is_active and not getattr(user, "owned_company", None):
        raise ApiError("access_closed", "Вход закрыт владельцем.", status_code=403)
    return user


def register_failure(identifier):
    user = _find_user(identifier)
    if not is_arenda_user(user):
        return
    sec = get_sec(user)
    sec.failed_attempts += 1
    if sec.failed_attempts >= MAX_FAILS:
        sec.locked_until = now() + timedelta(minutes=LOCK_MINUTES)
        sec.failed_attempts = 0
    sec.save(update_fields=["failed_attempts", "locked_until", "updated_at"])


def register_success(identifier):
    user = _find_user(identifier)
    sec = get_sec(user, create=False) if user else None
    if sec and (sec.failed_attempts or sec.locked_until):
        sec.failed_attempts, sec.locked_until = 0, None
        sec.save(update_fields=["failed_attempts", "locked_until", "updated_at"])


def sync_user_state(user, acc):
    """После изменения доступа: закрыт -> завершить сеансы (и заблокировать аккаунт, если он заведён из «Аренды»)."""
    sec = get_sec(user)
    if not acc.is_active:
        revoke_sessions(user)
        if sec.created_by_arenda and user.is_active:
            user.is_active = False
            user.save(update_fields=["is_active"])
    elif sec.created_by_arenda and not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])
