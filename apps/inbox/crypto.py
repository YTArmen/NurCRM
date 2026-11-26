"""Шифрование секретов каналов: AES-GCM (cryptography), ключ вне базы (Б-73 п. 2).

Ключ — переменная окружения INBOX_SECRET_KEY (любая строка, из неё берётся SHA-256).
Нет переменной — производный от settings.SECRET_KEY.
"""
import base64
import hashlib
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

SECRET_FIELDS = ("token", "api_key", "access_token", "app_secret", "pin")
PREFIX = "v1:"


def _key() -> bytes:
    raw = os.environ.get("INBOX_SECRET_KEY") or getattr(settings, "INBOX_SECRET_KEY", "")
    if not raw:
        raw = "inbox:" + str(settings.SECRET_KEY)
    return hashlib.sha256(raw.encode("utf-8")).digest()


def encrypt_dict(data: dict) -> str:
    if not data:
        return ""
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, json.dumps(data, ensure_ascii=False).encode("utf-8"), b"inbox-channel")
    return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_dict(blob: str) -> dict:
    if not blob or not blob.startswith(PREFIX):
        return {}
    try:
        raw = base64.urlsafe_b64decode(blob[len(PREFIX):].encode("ascii"))
        return json.loads(AESGCM(_key()).decrypt(raw[:12], raw[12:], b"inbox-channel").decode("utf-8"))
    except (InvalidTag, ValueError, KeyError):
        return {}


def get_secrets(channel) -> dict:
    return decrypt_dict(channel.secrets_enc)


def set_secrets(channel, updates: dict) -> None:
    """Добавляет/заменяет секреты; пустое значение — удаляет."""
    cur = get_secrets(channel)
    for k, v in (updates or {}).items():
        if k not in SECRET_FIELDS:
            continue
        if v in (None, ""):
            cur.pop(k, None)
        else:
            cur[k] = str(v).strip()
    channel.secrets_enc = encrypt_dict(cur)


def channel_token(channel) -> str:
    """Токен Telegram: у серверного бота — из его настроек, иначе из секретов канала."""
    if channel.bot_settings_id:
        try:
            return channel.bot_settings.token
        except Exception:
            return ""
    return get_secrets(channel).get("token", "")


def secret_flags(channel) -> dict:
    """Что наружу: только признаки «задан»."""
    cur = get_secrets(channel)
    flags = {f"{k}_set": bool(cur.get(k)) for k in SECRET_FIELDS}
    if channel.bot_settings_id:
        flags["token_set"] = bool(channel.bot_settings.token_set)
    return flags
