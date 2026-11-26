"""Продление входа (ТЗ Бекназара 06.11.2026, решение директора).

- Каждый успешный auth/refresh/ выдаёт новый refresh (ROTATE_REFRESH_TOKENS) на
  JWT_REFRESH_TOKEN_DAYS (30) дней: пока устройством пользуются, вход не кончается.
- Абсолютный предел без ввода пароля: JWT_REFRESH_MAX_AGE_DAYS (90) от входа по
  паролю (claim ``auth_time``). Срок нового refresh не выходит за этот предел.
- Чёрного списка нет: старый refresh действует до своего срока, поэтому повтор
  обновления (потерянный ответ, два запроса сразу) не выбрасывает кассира.
- 401 у refresh отдаёт ``code``: refresh_expired / user_disabled / token_invalid / session_revoked.

Слоты устройств (ТЗ «один вход на каждый ТИП устройства»):
- вход с ``client_type`` (или заголовком X-NurMarket-Client) и ``device_key`` 8–64 символа занимает слот
  mobile / android_pos / desktop_pos / desktop_owner; браузер и клиенты без device_key не ограничиваются;
- слот занят другим живым устройством -> 409 device_slot_taken; повтор с replace_device=true
  (или replace_device_id) отключает старое -> у него 401 session_revoked (reason device_replaced);
- флаги компании allow_multi_desktop_pos / allow_multi_android_pos снимают лимит для касс;
- 20.11.2026: роли owner и admin слотом не ограничиваются; слоты разных типов друг друга не трогают;
  вход без client_type/device_key слот не занимает и чужие входы не закрывает.
"""
import os
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

AUTH_TIME_CLAIM = "auth_time"


def refresh_max_age() -> timedelta:
    try:
        days = int(os.getenv("JWT_REFRESH_MAX_AGE_DAYS", "90"))
    except ValueError:
        days = 90
    return timedelta(days=max(days, 1))


def stamp_auth_time(token) -> None:
    """Метка «когда вводили пароль» — ставится при входе по паролю."""
    token[AUTH_TIME_CLAIM] = int(timezone.now().timestamp())


class RefreshRejected(Exception):
    def __init__(self, code: str, detail: str, reason: str | None = None, extra: dict | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.reason = reason
        self.extra = extra or {}

    def as_payload(self) -> dict:
        data = {"detail": self.detail, "code": self.code}
        if self.reason:
            data["reason"] = self.reason
        data.update(self.extra)
        return data


EXPIRED_TEXT = "Срок входа истёк. Войдите заново."


def rotate_refresh(raw_token: str) -> dict:
    """Проверить refresh и выдать новую пару. Ошибки — RefreshRejected."""
    if not raw_token or not isinstance(raw_token, str):
        raise RefreshRejected("token_invalid", "Не передан refresh-токен.")
    try:
        refresh = RefreshToken(raw_token)
    except TokenError as exc:
        if "expired" in str(exc).lower():
            raise RefreshRejected("refresh_expired", EXPIRED_TEXT, reason="expired")
        raise RefreshRejected("token_invalid", "Вход недействителен. Войдите заново.")

    now = timezone.now()
    try:
        auth_time = int(refresh.payload.get(AUTH_TIME_CLAIM) or refresh.payload.get("iat") or 0)
    except (TypeError, ValueError):
        auth_time = 0
    if not auth_time:
        auth_time = int(now.timestamp())
    deadline = auth_time + int(refresh_max_age().total_seconds())
    if now.timestamp() >= deadline:
        raise RefreshRejected("refresh_expired", EXPIRED_TEXT, reason="max_age")

    user_id = refresh.payload.get(api_settings.USER_ID_CLAIM)
    user = get_user_model().objects.filter(**{api_settings.USER_ID_FIELD: user_id}).first() if user_id else None
    if user is None or not api_settings.USER_AUTHENTICATION_RULE(user):
        raise RefreshRejected("user_disabled", "Пользователь отключён. Обратитесь к владельцу.")

    session = None
    sid = refresh.payload.get(SID_CLAIM)
    if sid:
        from apps.users.models import AuthSession

        session = AuthSession.objects.filter(pk=sid, user=user).first()
        if session is None or session.revoked_at is not None:
            raise revoked_error(session)

    data = {"access": str(refresh.access_token)}
    if api_settings.ROTATE_REFRESH_TOKENS:
        refresh[AUTH_TIME_CLAIM] = auth_time
        lifetime = min(api_settings.REFRESH_TOKEN_LIFETIME, timedelta(seconds=deadline - now.timestamp()))
        refresh.set_jti()
        refresh.set_exp(from_time=now, lifetime=lifetime)
        refresh.set_iat(at_time=now)
        data["refresh"] = str(refresh)
    else:
        data["refresh"] = raw_token
    if session is not None:
        new_exp = datetime.fromtimestamp(int(RefreshToken(data["refresh"], verify=False)["exp"]), tz=dt_timezone.utc)
        type(session).objects.filter(pk=session.pk).update(last_seen_at=now, expires_at=max(new_exp, session.expires_at))
    return data


# ---------------------------------------------------------------- слоты устройств

SID_CLAIM = "sid"
SLOTS = ("mobile", "android_pos", "desktop_pos", "desktop_owner")
SLOT_TITLES = {
    "mobile": "Телефон", "android_pos": "Android-касса", "desktop_pos": "Касса на ПК",
    "desktop_owner": "Программа владельца",
}
_HEADER_SLOT = {"kassa": "desktop_pos", "owner": "desktop_owner", "mobile": "mobile", "android-pos": "android_pos"}
_MULTI_FLAG = {"desktop_pos": "allow_multi_desktop_pos", "android_pos": "allow_multi_android_pos"}
_REVOKED_CACHE = "authsess:rev:{}"
UNLIMITED_ROLES = ("owner", "admin")


def _header_kind(request) -> str:
    try:
        raw = (request.headers.get("X-NurMarket-Client") or "").strip().lower()
    except AttributeError:
        return ""
    return raw.split("/", 1)[0] if "/" in raw else ""


def detect_slot(request, data) -> str | None:
    ct = str((data or {}).get("client_type") or "").strip().lower().replace("-", "_")
    if ct in SLOTS:
        return ct
    if ct:  # web / site / неизвестное — без лимита
        return None
    return _HEADER_SLOT.get(_header_kind(request)) if request is not None else None


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


def _iso(dt):
    return dt.isoformat() if dt else None


def device_payload(sess) -> dict:
    return {
        "id": str(sess.id), "slot": sess.slot, "client_type": sess.slot,
        "slot_title": SLOT_TITLES.get(sess.slot, sess.slot),
        "name": sess.device_name, "platform": sess.platform, "client": sess.client,
        "last_seen_at": _iso(sess.last_seen_at), "created_at": _iso(sess.created_at),
    }


def revoked_error(sess) -> RefreshRejected:
    if sess is None:
        return RefreshRejected("session_revoked", "Вход на этом устройстве закрыт. Войдите заново.", reason="logged_out")
    reason = sess.revoked_reason or "logged_out"
    if reason == "device_replaced":
        who = sess.revoked_by_device_name or "другом устройстве"
        detail = f"Ваш вход закрыт: вы вошли на другом устройстве ({who})."
    else:
        detail = "Вход на этом устройстве закрыт. Войдите заново."
    return RefreshRejected("session_revoked", detail, reason=reason,
                           extra={"by_device_name": sess.revoked_by_device_name or None, "at": _iso(sess.revoked_at)})


def revoke_sessions(sessions, reason: str, by_device_name: str = "") -> int:
    from django.core.cache import cache

    now = timezone.now()
    n = 0
    ttl = int(api_settings.ACCESS_TOKEN_LIFETIME.total_seconds()) + 120
    for sess in sessions:
        sess.revoked_at = now
        sess.revoked_reason = reason
        sess.revoked_by_device_name = (by_device_name or "")[:64]
        sess.save(update_fields=["revoked_at", "revoked_reason", "revoked_by_device_name"])
        try:
            cache.set(_REVOKED_CACHE.format(sess.id), revoked_error(sess).as_payload(), timeout=ttl)
        except Exception:
            pass
        n += 1
    return n


class DeviceSlotTaken(Exception):
    def __init__(self, sess):
        self.sess = sess

    def as_payload(self) -> dict:
        s = self.sess
        title = SLOT_TITLES.get(s.slot, s.slot)
        seen = timezone.localtime(s.last_seen_at).strftime("%d.%m %H:%M") if s.last_seen_at else "—"
        name = s.device_name or "без названия"
        return {
            "code": "device_slot_taken", "slot": s.slot,
            "detail": f"Вы уже входили на другом устройстве такого типа ({title} «{name}», был в сети {seen}). "
                      f"Выйти там и войти здесь?",
            "device": {"id": str(s.id), "name": s.device_name, "platform": s.platform,
                       "last_seen_at": _iso(s.last_seen_at)},
        }


def open_session(request, user, data, token):
    """Вызывается при входе по паролю. Ставит sid в token или бросает DeviceSlotTaken.
    Без слота/device_key — ничего не делает (старые клиенты, браузер)."""
    from django.db import transaction

    from apps.users.models import AuthSession
    from core.api_compat import company_flag

    slot = detect_slot(request, data)
    device_key = str((data or {}).get("device_key") or "").strip()
    if not slot or not (8 <= len(device_key) <= 64):
        return None
    device_name = str(data.get("device_name") or "").strip()[:64]
    platform = str(data.get("platform") or "").strip()[:32]
    try:
        client = (request.headers.get("X-NurMarket-Client") or "")[:40]
    except AttributeError:
        client = ""
    now = timezone.now()
    expires = now + api_settings.REFRESH_TOKEN_LIFETIME
    company = getattr(user, "owned_company", None) or getattr(user, "company", None)
    multi = slot in _MULTI_FLAG and company_flag(company, _MULTI_FLAG[slot])
    # 20.11.2026 (ТЗ Бекназара «один аккаунт в кассе и программе владельца»): владелец и админ
    # могут входить с нескольких ПК/телефонов одного типа — слот их не ограничивает.
    if str(getattr(user, "role", "") or "").lower() in UNLIMITED_ROLES:
        from apps.users.plan_mini import is_mini  # mini2011: на «Мини» одно устройство даже у владельца

        multi = not is_mini(company)

    with transaction.atomic():
        others = list(
            AuthSession.objects.select_for_update()
            .filter(user=user, slot=slot, revoked_at__isnull=True, expires_at__gt=now)
            .exclude(device_key=device_key)
            .order_by("-last_seen_at")
        )
        if others and not multi:
            replace_id = str(data.get("replace_device_id") or "").strip()
            if not (_truthy(data.get("replace_device")) or (replace_id and any(str(o.id) == replace_id for o in others))):
                raise DeviceSlotTaken(others[0])
            revoke_sessions(others, "device_replaced", device_name or SLOT_TITLES.get(slot, ""))
        sess, _ = AuthSession.objects.update_or_create(
            user=user, slot=slot, device_key=device_key,
            defaults={"device_name": device_name, "platform": platform, "client": client, "last_seen_at": now,
                      "expires_at": expires, "revoked_at": None, "revoked_reason": "", "revoked_by_device_name": ""},
        )
    try:
        from django.core.cache import cache

        cache.delete(_REVOKED_CACHE.format(sess.id))
    except Exception:
        pass
    token[SID_CLAIM] = str(sess.id)
    return sess


try:
    from rest_framework.exceptions import AuthenticationFailed
    from rest_framework_simplejwt.authentication import JWTAuthentication

    class SessionAwareJWTAuthentication(JWTAuthentication):
        """JWT + проверка отключённого устройства (sid в кэше «отозван»): 401 session_revoked сразу,
        не дожидаясь конца 15-минутного access."""

        def get_validated_token(self, raw_token):
            token = super().get_validated_token(raw_token)
            sid = token.payload.get(SID_CLAIM) if hasattr(token, "payload") else None
            if sid:
                payload = None
                try:
                    from django.core.cache import cache

                    payload = cache.get(_REVOKED_CACHE.format(sid))
                except Exception:
                    payload = None
                if payload:
                    raise AuthenticationFailed(payload)
            return token

        def authenticate(self, request):
            res = super().authenticate(request)
            if res is not None:
                from apps.users import client_versions as _cv

                if not _cv.exempt(request):  # cver2011: 426 client_outdated, только если min задан
                    _cv.check(request)
                from apps.users.plan_mini import check_request

                # mini2011: касса/программа владельца на «Мини» — 403; capabilities отдаём, чтобы показать причину
                # mini2211: сайт на «Мини» — только оплата подписки
                check_request(request, res[0], res[1])
            return res
except Exception:  # pragma: no cover
    pass
