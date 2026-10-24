"""ТЗ Н-23: уведомления владельцу и одноразовые коды подтверждения владельца."""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from django.conf import settings as django_settings
from django.utils import timezone

from apps.main.telegram_bot.models import (
    TelegramBotSettings,
    TelegramOwnerCode,
    TelegramOwnerNotification,
)
from apps.main.telegram_bot.services import telegram_api

DEDUPE_TTL = timedelta(hours=24)
OWNER_CODE_TTL = timedelta(minutes=10)
OWNER_CODE_MAX_ATTEMPTS = 5
CODE_RE = re.compile(r"^\s*(?:/start\s+|/code\s+)?(\d{6})\s*$")


def build_reply_markup(buttons):
    """buttons: [{"text","url"|"callback_data"}] или список рядов таких кнопок."""
    if not buttons or not isinstance(buttons, (list, tuple)):
        return None
    rows = buttons if all(isinstance(r, (list, tuple)) for r in buttons) else [[b] for b in buttons]
    out = []
    for row in rows:
        r = []
        for b in row:
            if not isinstance(b, dict) or not b.get("text"):
                continue
            item = {"text": str(b["text"])[:64]}
            if b.get("url"):
                item["url"] = str(b["url"])
            elif b.get("callback_data"):
                item["callback_data"] = str(b["callback_data"])[:64]
            else:
                continue
            r.append(item)
        if r:
            out.append(r)
    return {"inline_keyboard": out} if out else None


def notify_owner(company, text, buttons=None, dedupe_key=""):
    """
    Шлёт текст в чат владельца серверным ботом компании.
    Возвращает (http_status, payload). Повтор с тем же dedupe_key за 24 ч — не отправляет.
    """
    settings = TelegramBotSettings.objects.filter(company=company).first()
    if not settings or not settings.token:
        return 400, {"ok": False, "detail": "Токен бота не настроен"}
    if not settings.owner_chat_id:
        return 400, {"ok": False, "detail": "Чат владельца не привязан"}
    dedupe_key = (dedupe_key or "").strip()[:128]
    now = timezone.now()
    if dedupe_key:
        dup = TelegramOwnerNotification.objects.filter(
            company=company, dedupe_key=dedupe_key, ok=True, created_at__gte=now - DEDUPE_TTL,
        ).exists()
        if dup:
            return 200, {"ok": True, "duplicate": True}
    res = telegram_api.send_message(
        settings.token, settings.owner_chat_id, text, parse_mode="HTML",
        reply_markup=build_reply_markup(buttons),
    )
    ok = bool(res.get("ok"))
    TelegramOwnerNotification.objects.create(
        company=company, dedupe_key=dedupe_key, text=text[:4000], ok=ok,
        error="" if ok else str(res.get("description") or "")[:255],
    )
    if ok:
        return 200, {"ok": True, "duplicate": False}
    payload = {"ok": False, "detail": res.get("description") or "Не удалось отправить сообщение в Telegram"}
    retry_after = (res.get("parameters") or {}).get("retry_after")
    if retry_after:
        payload["retry_after"] = retry_after
    return 502, payload


def _hash_code(company_id, code: str) -> str:
    key = (django_settings.SECRET_KEY + "|owner-code|" + str(company_id)).encode()
    return hmac.new(key, code.encode(), hashlib.sha256).hexdigest()


def issue_owner_code(company, user=None):
    """Выдаёт новый 6-значный код (старые неиспользованные гасятся). Код в БД — только хешем."""
    now = timezone.now()
    TelegramOwnerCode.objects.filter(company=company, used_at__isnull=True).update(used_at=now)
    code = f"{secrets.randbelow(10**6):06d}"
    TelegramOwnerCode.objects.create(
        company=company, code_hash=_hash_code(company.id, code),
        expires_at=now + OWNER_CODE_TTL, created_by=user,
    )
    return code, now + OWNER_CODE_TTL


def verify_owner_code(company, code, chat_id, from_id="", chat_title=""):
    """
    Проверяет код и привязывает чат и from.id как владельца компании.
    Возвращает (ok, причина). Код одноразовый; 5 неверных попыток гасят его.
    """
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code) or not chat_id:
        return False, "bad_request"
    now = timezone.now()
    row = (
        TelegramOwnerCode.objects.filter(company=company, used_at__isnull=True, expires_at__gt=now)
        .order_by("-created_at").first()
    )
    if not row:
        return False, "no_active_code"
    if not hmac.compare_digest(row.code_hash, _hash_code(company.id, code)):
        row.attempts += 1
        if row.attempts >= OWNER_CODE_MAX_ATTEMPTS:
            row.used_at = now
        row.save(update_fields=["attempts", "used_at"])
        return False, "wrong_code"
    row.used_at = now
    row.save(update_fields=["used_at"])
    settings, _ = TelegramBotSettings.objects.get_or_create(company=company)
    settings.owner_chat_id = str(chat_id)
    settings.owner_user_id = str(from_id or "")
    if chat_title:
        settings.owner_chat_title = chat_title
    settings.save(update_fields=["owner_chat_id", "owner_user_id", "owner_chat_title"])
    return True, "ok"


def try_bind_owner_from_message(settings, chat_id, from_user, chat_title, text):
    """Вызывается вебхуком: сообщение из 6 цифр от не-владельца → пробуем как код владельца."""
    m = CODE_RE.match(text or "")
    if not m:
        return False
    ok, _ = verify_owner_code(
        settings.company, m.group(1), chat_id, (from_user or {}).get("id") or "", chat_title,
    )
    if ok and settings.token:
        telegram_api.send_message(settings.token, chat_id, "Готово: этот чат теперь чат владельца.", parse_mode=None)
    return ok
