"""
Сброс пароля по ссылке из письма («Забыли пароль?» на экранах входа маркета и услуг).

POST /api/users/password-reset/request/   (без токена)  { "email" }
  → 200 {"ok": true} всегда — есть ли такой аккаунт, не сообщаем
  → 400 {"code": "validation_error"} — не похоже на почту
  → 429 {"code": "too_many_requests"} — больше 5 запросов с IP или 3 на одну почту за 15 минут
  Письмо со ссылкой <сайт>/reset-password?token=…; сайт — Origin запроса или поле "origin" тела (сайт услуг ходит через
  посредник), если адрес в PASSWORD_RESET_ORIGINS / списке ниже, иначе PASSWORD_RESET_DEFAULT_ORIGIN (market.nurcrm.kg).

POST /api/users/password-reset/check/     { "token" }             → 200 {"ok": true, "email": "ma***@nur.kg"} | 400 invalid_token
POST /api/users/password-reset/confirm/   { "token", "password" } → 200 {"ok": true} | 400 invalid_token | 400 validation_error

Ссылка живёт 15 минут и одноразовая: в подпись входит хэш текущего пароля и время последнего входа, поэтому
после смены пароля (или входа) та же ссылка больше не подходит. Отдельной таблицы не нужно.
"""
import hashlib
import os
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection
from django.utils.html import escape
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

import logging

log = logging.getLogger(__name__)

TTL = 15 * 60
SALT = "nurcrm.password-reset.v1"
IP_LIMIT, EMAIL_LIMIT = 5, 3
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEFAULT_ORIGINS = (
    "https://market.nurcrm.kg",
    "https://front-market.vercel.app",
    "https://app.nurcrm.kg",
    "https://stageapp.nurcrm.kg",
    # сфера услуг (front-usluga): ходит на сервер через свой посредник, поэтому присылает адрес в теле запроса
    "https://front-usluga.vercel.app",
    "https://stage.nurcrm.kg",
    "https://nurcrm.kg",
    "https://www.nurcrm.kg",
)


def _origins():
    extra = getattr(settings, "PASSWORD_RESET_ORIGINS", None)
    return tuple(extra) if extra else DEFAULT_ORIGINS


def _default_origin():
    return getattr(settings, "PASSWORD_RESET_DEFAULT_ORIGIN", "") or "https://market.nurcrm.kg"


def _client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", "")) or "?"


def _hit(key: str) -> int:
    try:
        n = (cache.get(key) or 0) + 1
        cache.set(key, n, TTL)
        return n
    except Exception:
        return 1


def _state(user) -> str:
    """Что меняется после использования ссылки: пароль и последний вход."""
    raw = f"{user.pk}:{user.password}:{user.last_login.isoformat() if user.last_login else ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def make_token(user) -> str:
    return signing.TimestampSigner(salt=SALT).sign_object({"u": str(user.pk), "s": _state(user)}, compress=True)


def read_token(token: str):
    """Пользователь по токену или None (подделка, срок вышел, ссылка уже использована, аккаунт выключен)."""
    if not token or len(token) > 512:
        return None
    try:
        data = signing.TimestampSigner(salt=SALT).unsign_object(token, max_age=TTL)
    except signing.BadSignature:  # SignatureExpired — её подкласс
        return None
    User = get_user_model()
    user = User.objects.filter(pk=data.get("u"), is_active=True).first()
    if user is None or data.get("s") != _state(user):
        return None
    return user


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return (name[:2] + "***" if len(name) > 2 else name[:1] + "***") + "@" + domain


def _send(user, link: str) -> None:
    subject = "Сброс пароля NurCRM"
    text = (
        "Здравствуйте!\n\n"
        "Кто-то (возможно, вы) попросил сбросить пароль от аккаунта NurCRM.\n"
        f"Чтобы задать новый пароль, откройте ссылку — она действует 15 минут и только один раз:\n{link}\n\n"
        "Если вы не просили сброс, просто ничего не делайте: пароль останется прежним.\n\n"
        "Поддержка NurCRM: +996 559 900 556"
    )
    html = f"""<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.5;color:#111827;max-width:520px">
<p>Здравствуйте!</p>
<p>Кто-то (возможно, вы) попросил сбросить пароль от аккаунта NurCRM.</p>
<p><a href="{escape(link)}" style="display:inline-block;background:#ffd600;color:#0b2a3c;font-weight:700;padding:12px 22px;border-radius:12px;text-decoration:none">Задать новый пароль</a></p>
<p style="color:#667085;font-size:13px">Ссылка действует 15 минут и только один раз. Если кнопка не открывается, скопируйте адрес:<br>{escape(link)}</p>
<p style="color:#667085;font-size:13px">Если вы не просили сброс, ничего не делайте — пароль останется прежним.<br>Поддержка NurCRM: +996 559 900 556</p>
</div>"""
    # сначала основная почта (EMAIL_*), не ушло — запасная (EMAIL_FALLBACK_HOST_USER / _PASSWORD, тот же SMTP-сервер)
    last_error = None
    for conn, sender in _mail_routes():
        msg = EmailMultiAlternatives(subject, text, sender, [user.email], connection=conn)
        msg.attach_alternative(html, "text/html")
        try:
            msg.send(fail_silently=False)
            return
        except Exception as e:  # пробуем следующий маршрут
            log.warning("password reset email via %s failed: %s", sender, e)
            last_error = e
    raise last_error or RuntimeError("no mail route")


def _mail_routes():
    """(соединение, отправитель): основная почта, затем запасная, если задана."""
    routes = [(get_connection(fail_silently=False), getattr(settings, "DEFAULT_FROM_EMAIL", None))]
    user = os.getenv("EMAIL_FALLBACK_HOST_USER", "").strip()
    password = os.getenv("EMAIL_FALLBACK_HOST_PASSWORD", "").replace(" ", "")
    if user and password:
        routes.append((get_connection(
            "django.core.mail.backends.smtp.EmailBackend",
            fail_silently=False,
            host=os.getenv("EMAIL_FALLBACK_HOST", "") or getattr(settings, "EMAIL_HOST", "smtp.gmail.com"),
            port=int(os.getenv("EMAIL_FALLBACK_PORT", "") or getattr(settings, "EMAIL_PORT", 587)),
            username=user, password=password, use_tls=True, timeout=20,
        ), user))
    return routes


class PasswordResetRequestEmailAPIView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        email = str(request.data.get("email") or "").strip().lower()[:254]
        if not EMAIL_RE.match(email):
            return Response({"code": "validation_error", "detail": "Укажите почту, с которой входите.", "fields": {"email": ["Укажите почту."]}}, status=400)
        if _hit(f"pwlink:ip:{_client_ip(request)}") > IP_LIMIT or _hit(f"pwlink:em:{hashlib.sha1(email.encode()).hexdigest()}") > EMAIL_LIMIT:
            return Response({"code": "too_many_requests", "detail": "Слишком много запросов. Попробуйте через 15 минут."}, status=429)

        User = get_user_model()
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            # адрес сайта для ссылки: заголовок Origin (браузер) или поле "origin" (посредник сайта услуг) — только из списка
            allowed = _origins()
            header = (request.headers.get("Origin") or "").rstrip("/")
            body = str(request.data.get("origin") or "").strip().rstrip("/")
            base = header if header in allowed else body if body in allowed else _default_origin()
            link = f"{base}/reset-password?token={make_token(user)}"
            try:
                _send(user, link)
            except Exception:
                # почта не ушла: человеку отвечаем так же (не раскрываем аккаунт), в журнал — ошибку
                log.exception("password reset email failed for user %s", user.pk)
        return Response({"ok": True})


class PasswordResetCheckAPIView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        user = read_token(str(request.data.get("token") or ""))
        if user is None:
            return Response({"code": "invalid_token", "detail": "Ссылка устарела или уже использована. Запросите новую."}, status=400)
        return Response({"ok": True, "email": _mask(user.email)})


class PasswordResetConfirmAPIView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        user = read_token(str(request.data.get("token") or ""))
        if user is None:
            return Response({"code": "invalid_token", "detail": "Ссылка устарела или уже использована. Запросите новую."}, status=400)
        password = str(request.data.get("password") or "")
        errors = []
        if len(password) < 8:
            errors.append("Пароль — не короче 8 символов.")
        else:
            try:
                validate_password(password, user)
            except ValidationError as e:
                errors.extend(e.messages)
        if errors:
            return Response({"code": "validation_error", "detail": errors[0], "fields": {"password": errors}}, status=400)
        user.set_password(password)
        user.save(update_fields=["password"])
        return Response({"ok": True})
