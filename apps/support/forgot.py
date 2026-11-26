"""
«Забыли пароль?» с экранов входа (маркет, услуги): заявка уходит в Telegram-группу поддержки.

POST /api/support/password-reset-requests/   (без токена)
{ "name", "phone", "login"?, "company"?, "program"?: "market|usluga|…", "comment"?, "website"?: "" }
→ 201 {"ok": true}
→ 400 {"code": "validation_error", "fields": {...}}       имя / телефон (не меньше 9 цифр)
→ 429 {"code": "too_many_requests"}                        больше 3 заявок с одного IP за 10 минут
→ 503 {"code": "not_delivered", "phone": "+996 559 900 556"} бот не настроен или Telegram не принял

Пароль здесь не сбрасывается: поддержка перезванивает, проверяет владельца и меняет пароль сама.
Бот — тот же, что у оповещений команды (SUPPORT_TELEGRAM_BOT_TOKEN); группа — PASSWORD_RESET_TELEGRAM_CHAT_ID,
если задана, иначе SUPPORT_TELEGRAM_CHAT_ID. Общий часовой лимит оповещений сюда не относится —
заявка человека не должна теряться из-за массового сбоя.
"""
import re

from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from django.core.cache import cache

from apps.support.bot import _send_raw, _setting, esc, get_support_config
from apps.support.views import _client_ip

SUPPORT_PHONE = "+996 559 900 556"
PROGRAMS = {"market": "Маркет", "usluga": "Сфера услуг", "cafe": "Кафе", "kassa": "Касса"}
IP_LIMIT = 3
IP_WINDOW = 600


def _hit(key: str) -> int:
    """Счётчик заявок с IP за окно; при сбое кэша не мешает отправке."""
    try:
        n = (cache.get(key) or 0) + 1
        cache.set(key, n, IP_WINDOW)
        return n
    except Exception:
        return 1


def _field(data, name: str, limit: int) -> str:
    v = data.get(name) if hasattr(data, "get") else None
    return ("" if v is None else str(v)).strip()[:limit]


class PasswordResetRequestAPIView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        data = request.data
        # ловушка для ботов: люди это поле не видят — отвечаем «принято», но ничего не шлём
        if _field(data, "website", 200):
            return Response({"ok": True}, status=201)

        name = _field(data, "name", 120)
        phone = _field(data, "phone", 40)
        errors = {}
        if len(name) < 2:
            errors["name"] = ["Укажите имя."]
        if len(re.sub(r"\D", "", phone)) < 9:
            errors["phone"] = ["Укажите телефон (не меньше 9 цифр)."]
        if errors:
            return Response({"code": "validation_error", "detail": "Проверьте поля.", "fields": errors}, status=400)

        ip = _client_ip(request)
        if _hit(f"pwreset:ip:{ip}") > IP_LIMIT:
            return Response({"code": "too_many_requests", "detail": "Слишком много заявок. Попробуйте через 10 минут или позвоните.", "phone": SUPPORT_PHONE}, status=429)

        program = _field(data, "program", 30).lower()
        lines = [
            "🔑 <b>Забыли пароль</b>" + (f" · {esc(PROGRAMS.get(program, program))}" if program else ""),
            f"Имя: <b>{esc(name)}</b>",
            f"Телефон: <b>{esc(phone)}</b>",
        ]
        for key, label, limit in (("login", "Логин", 120), ("company", "Компания", 120), ("comment", "Комментарий", 1000)):
            v = _field(data, key, limit)
            if v:
                lines.append(f"{label}: {esc(v)}")
        lines.append(f"IP: {esc(ip)}")
        lines.append("<i>Перед сбросом пароля перезвоните и проверьте, что это владелец аккаунта.</i>")

        cfg = get_support_config()
        chat_id = _setting("PASSWORD_RESET_TELEGRAM_CHAT_ID") or cfg.chat_id
        if not (cfg.token and chat_id and _send_raw(cfg.token, chat_id, "\n".join(lines))):
            return Response({"code": "not_delivered", "detail": f"Заявка не отправлена. Позвоните в поддержку: {SUPPORT_PHONE}", "phone": SUPPORT_PHONE}, status=503)
        return Response({"ok": True}, status=201)
