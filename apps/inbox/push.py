"""Push-уведомления ящика (Б-77 п. 3) через сменяемый провайдер.

Выбор: settings.INBOX_PUSH_PROVIDER ("log" по умолчанию | "fcm").
- log: только запись в журнал PushLog (ничего не уходит наружу);
- fcm: заготовка FCM HTTP v1 через requests; выключена, пока не задано INBOX_FCM_ENABLED=1,
  INBOX_FCM_PROJECT_ID и INBOX_FCM_ACCESS_TOKEN (OAuth-токен сервисного аккаунта; обновление токена —
  отдельная задача: нужна библиотека google-auth, её здесь нет).
Токены устройств хранит другой модуль (DevicePushToken); если его нет — получателей без токенов просто нет.
"""
import logging
import os
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("inbox.push")

THROTTLE = timedelta(seconds=30)
BODY_MAX = 60
_PHONE = re.compile(r"\+?\d[\d\s\-()]{5,}\d")


def safe_body(text: str) -> str:
    """Текст push: без телефона, до 60 символов."""
    t = _PHONE.sub("…", " ".join((text or "").split()))
    return t if len(t) <= BODY_MAX else t[: BODY_MAX - 1] + "…"


class PushProvider:
    name = "base"

    def send(self, tokens: list, title: str, body: str, data: dict) -> bool:
        raise NotImplementedError


class LogProvider(PushProvider):
    """Ничего не отправляет: запись в PushLog делает notify_new_inbound."""
    name = "log"

    def send(self, tokens, title, body, data):
        logger.info("push(log): %s токенов, %r", len(tokens), body)
        return True


class FcmProvider(PushProvider):
    name = "fcm"

    def enabled(self) -> bool:
        return (os.environ.get("INBOX_FCM_ENABLED") == "1" and bool(os.environ.get("INBOX_FCM_PROJECT_ID"))
                and bool(os.environ.get("INBOX_FCM_ACCESS_TOKEN")))

    def send(self, tokens, title, body, data):
        if not self.enabled():
            logger.info("push(fcm): выключен (нет INBOX_FCM_*), пропуск")
            return False
        import requests

        url = f"https://fcm.googleapis.com/v1/projects/{os.environ['INBOX_FCM_PROJECT_ID']}/messages:send"
        headers = {"Authorization": f"Bearer {os.environ['INBOX_FCM_ACCESS_TOKEN']}"}
        ok = True
        for t in tokens:
            payload = {"message": {"token": t["token"], "notification": {"title": title, "body": body},
                                   "data": {k: str(v) for k, v in (data or {}).items()}}}
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=5)
                ok = ok and resp.status_code < 300
            except Exception as exc:
                logger.warning("push(fcm) error: %s", type(exc).__name__)
                ok = False
        return ok


def get_provider() -> PushProvider:
    name = getattr(settings, "INBOX_PUSH_PROVIDER", "log")
    return FcmProvider() if name == "fcm" else LogProvider()


def get_device_tokens(user_ids) -> list:
    """[{user_id, device_key, platform, token}] — из DevicePushToken другого помощника (необязательно)."""
    try:
        from apps.main.tz26.pos_sync.models import DevicePushToken
    except ImportError:
        return []
    try:
        rows = DevicePushToken.objects.filter(user_id__in=list(user_ids))
        return [{"user_id": r.user_id, "device_key": r.device_key, "platform": r.platform, "token": r.token} for r in rows]
    except Exception:
        logger.exception("push: не удалось прочитать токены устройств")
        return []


def recipients(conv) -> list:
    """Ответственному; если нет — всем, у кого есть доступ к диалогу."""
    if conv.assignee_id:
        return [conv.assignee]
    from django.contrib.auth import get_user_model

    from . import access

    out = []
    for u in get_user_model().objects.filter(company_id=conv.company_id, is_active=True)[:300]:
        if not access.has(u, "can_view_inbox"):
            continue
        if access.has(u, "can_view_all_conversations") or conv.branch_id is None or conv.branch_id in access.user_branch_ids(u):
            out.append(u)
    owner = getattr(conv.company, "owner", None)
    if owner and owner not in out:
        out.append(owner)
    return out


def notify_new_inbound(conv, msg) -> int:
    """Push на новое входящее. Не чаще 1 раза в 30 с на диалог. Возвращает число получателей."""
    from .models import PushLog

    if PushLog.objects.filter(conversation=conv, created_at__gte=timezone.now() - THROTTLE).exists():
        return 0
    users = recipients(conv)
    if not users:
        return 0
    tokens = get_device_tokens([u.id for u in users])
    provider = get_provider()
    body = safe_body(msg.text or f"[{msg.kind}]")
    title = "Новое сообщение"
    ok = False
    try:
        ok = provider.send(tokens, title, body, {"conversation": conv.id, "type": "inbox_message"})
    except Exception:
        logger.exception("push provider failed")
    by_user = {}
    for t in tokens:
        by_user[t["user_id"]] = by_user.get(t["user_id"], 0) + 1
    PushLog.objects.bulk_create([
        PushLog(company_id=conv.company_id, conversation=conv, user=u, provider=provider.name, title=title,
                body=body, tokens=by_user.get(u.id, 0), ok=ok)
        for u in users
    ])
    return len(users)
