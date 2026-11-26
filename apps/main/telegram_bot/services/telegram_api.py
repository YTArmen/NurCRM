import logging
import time

import httpx
from django.conf import settings as django_settings
from django.core.cache import cache

logger = logging.getLogger("telegram_bot.api")
TELEGRAM_API_BASE = "https://api.telegram.org"

# Лимиты Telegram (ТЗ-07 1.1.5): ~30 сообщений/с на бота и ~1/с в один чат.
BOT_MESSAGES_PER_SECOND = 30
CHAT_MIN_INTERVAL_SECONDS = 1.0
RATE_LIMIT_MAX_WAIT_SECONDS = 3.0


def build_webhook_url(bot_uuid) -> str:
    """Единый адрес вебхука — и для установки, и для ежечасной проверки."""
    base_url = getattr(django_settings, "TELEGRAM_WEBHOOK_BASE_URL", "") or "https://app.nurcrm.kg"
    return f"{base_url.rstrip('/')}/api/telegram/webhook/{bot_uuid}/"


def _bot_key(token: str) -> str:
    # Публичная часть токена (id бота) — секрет в ключи кэша не пишем.
    return (token or "").split(":", 1)[0]


def check_rate_limit(token: str, chat_id: str) -> float:
    """
    ТЗ Н-23 (3): без ожидания занимает «слот» отправки. 0 — можно слать сейчас,
    иначе число секунд (retry_after), через сколько повторить.
    Лимиты: 30 сообщений/с на бота и 1/с в один чат. Ошибки кэша не блокируют отправку.
    """
    bot = _bot_key(token)
    try:
        if not cache.add(f"tg_rl_chat:{bot}:{chat_id}", 1, timeout=CHAT_MIN_INTERVAL_SECONDS):
            return CHAT_MIN_INTERVAL_SECONDS
        second = int(time.time())
        key = f"tg_rl_bot:{bot}:{second}"
        cache.add(key, 0, timeout=5)
        if cache.incr(key) > BOT_MESSAGES_PER_SECOND:
            cache.delete(f"tg_rl_chat:{bot}:{chat_id}")
            return round(max(0.05, second + 1 - time.time()), 2)
    except Exception as exc:
        logger.debug("rate limit cache unavailable: %s", exc)
    return 0.0


def _wait_rate_limit(token: str, chat_id: str) -> None:
    """Ждёт, пока отправка уложится в лимиты Telegram (но не дольше RATE_LIMIT_MAX_WAIT_SECONDS)."""
    deadline = time.monotonic() + RATE_LIMIT_MAX_WAIT_SECONDS
    while True:
        retry_after = check_rate_limit(token, chat_id)
        if not retry_after or time.monotonic() >= deadline:
            return
        time.sleep(min(retry_after, 0.2 if retry_after >= 0.2 else retry_after))


def _post_with_retry(client, url: str, **kwargs):
    """POST с одним повтором на 429 (retry_after до 5 с)."""
    resp = client.post(url, **kwargs)
    if resp.status_code == 429:
        try:
            retry_after = int(((resp.json() or {}).get("parameters") or {}).get("retry_after") or 1)
        except Exception:
            retry_after = 1
        if retry_after <= 5:
            time.sleep(retry_after)
            resp = client.post(url, **kwargs)
    return resp


class TelegramAPIError(Exception):
    def __init__(self, message: str, status_code: int = None, description: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.description = description


def get_me(token: str) -> dict:
    """Проверяет токен бота через getMe."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/getMe"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)
            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "Unauthorized")
                raise TelegramAPIError(f"Telegram: {desc}", status_code=resp.status_code, description=desc)
            return data.get("result", {})
    except httpx.RequestError as exc:
        logger.error("getMe request failed: %s", exc)
        raise TelegramAPIError(f"Ошибка соединения с Telegram: {exc}")


def set_webhook(token: str, webhook_url: str, secret_token: str) -> dict:
    """Устанавливает вебхук на сервере с secret_token."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/setWebhook"
    payload = {
        "url": webhook_url,
        "secret_token": secret_token,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "Failed to set webhook")
                raise TelegramAPIError(f"Telegram: {desc}", status_code=resp.status_code, description=desc)
            return data
    except httpx.RequestError as exc:
        logger.error("setWebhook request failed: %s", exc)
        raise TelegramAPIError(f"Ошибка установки webhook: {exc}")


def delete_webhook(token: str) -> dict:
    """Удаляет вебхук."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/deleteWebhook"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json={"drop_pending_updates": False})
            return resp.json()
    except Exception as exc:
        logger.error("deleteWebhook error: %s", exc)
        return {"ok": False, "description": str(exc)}


def get_webhook_info(token: str) -> dict:
    """Получает информацию о вебхуке от Telegram Bot API."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/getWebhookInfo"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)
            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "Failed to get webhook info")
                raise TelegramAPIError(f"Telegram: {desc}", status_code=resp.status_code, description=desc)
            return data.get("result", {})
    except httpx.RequestError as exc:
        logger.error("getWebhookInfo request failed: %s", exc)
        raise TelegramAPIError(f"Ошибка соединения с Telegram: {exc}")


def _mirror_to_inbox(chat_id, text, data) -> None:
    try:
        from apps.inbox import bot_hooks
        bot_hooks.on_bot_send(chat_id, text, data)
    except Exception:
        pass


def send_message(token: str, chat_id: str, text: str, parse_mode: str = "HTML", reply_markup: dict = None) -> dict:
    """Отправляет текстовое сообщение в чат."""
    if not token or not chat_id or not text:
        return {"ok": False, "description": "Empty token, chat_id or text"}

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    _wait_rate_limit(token, chat_id)
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = _post_with_retry(client, url, json=payload)
            data = resp.json()
            if not data.get("ok") and parse_mode and resp.status_code == 400:
                # Попробуем без parse_mode, если ошибка парсинга HTML/Markdown
                payload.pop("parse_mode", None)
                resp2 = _post_with_retry(client, url, json=payload)
                data = resp2.json()
            if not data.get("ok"):
                data.setdefault("error_code", resp.status_code)
            else:
                _mirror_to_inbox(chat_id, text, data)  # ящик «Чаты»: ответ бота виден в диалоге
            return data
    except Exception as exc:
        logger.error("sendMessage error for chat %s: %s", chat_id, exc)
        return {"ok": False, "description": str(exc)}


def send_chat_action(token: str, chat_id: str, action: str = "typing") -> None:
    """«Печатает…» / «записывает голосовое…» (record_voice), пока бот думает. Ошибки не мешают ответу."""
    if not token or not chat_id:
        return
    try:
        with httpx.Client(timeout=5.0) as client:
            client.post(f"{TELEGRAM_API_BASE}/bot{token}/sendChatAction", json={"chat_id": str(chat_id), "action": action})
    except Exception as exc:
        logger.debug("sendChatAction error for chat %s: %s", chat_id, exc)


def send_voice(token: str, chat_id: str, voice_bytes: bytes, caption: str = None) -> dict:
    """Отправляет голосовое сообщение (OGG/Opus)."""
    if not token or not chat_id or not voice_bytes:
        return {"ok": False, "description": "Empty token, chat_id or voice_bytes"}

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendVoice"
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption[:1024]

    files = {"voice": ("voice.ogg", voice_bytes, "audio/ogg")}
    _wait_rate_limit(token, chat_id)
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = _post_with_retry(client, url, data=data, files=files)
            return resp.json()
    except Exception as exc:
        logger.error("sendVoice error for chat %s: %s", chat_id, exc)
        return {"ok": False, "description": str(exc)}


def send_photo(token: str, chat_id: str, photo, caption: str = None, parse_mode: str = "HTML", reply_markup: dict = None) -> dict:
    """Отправляет фото в чат (URL, file_id или bytes)."""
    import json
    if not token or not chat_id or not photo:
        return {"ok": False, "description": "Empty token, chat_id or photo"}
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendPhoto"
    _wait_rate_limit(token, chat_id)
    try:
        with httpx.Client(timeout=15.0) as client:
            if isinstance(photo, bytes):
                data = {"chat_id": str(chat_id)}
                if caption:
                    data["caption"] = caption[:1024]
                if parse_mode:
                    data["parse_mode"] = parse_mode
                if reply_markup:
                    data["reply_markup"] = json.dumps(reply_markup) if isinstance(reply_markup, dict) else reply_markup
                files = {"photo": ("photo.jpg", photo, "image/jpeg")}
                resp = _post_with_retry(client, url, data=data, files=files)
            else:
                payload = {
                    "chat_id": str(chat_id),
                    "photo": str(photo),
                }
                if caption:
                    payload["caption"] = caption[:1024]
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                resp = _post_with_retry(client, url, json=payload)
            data = resp.json()
            if not data.get("ok"):
                logger.warning("sendPhoto telegram error: %s", data)
            return data
    except Exception as exc:
        logger.error("sendPhoto error for chat %s: %s", chat_id, exc)
        return {"ok": False, "description": str(exc)}


def send_media_group(token: str, chat_id: str, media: list) -> dict:
    """
    Отправляет альбом из 2–10 фотографий.
    media: [{"type": "photo", "media": url_or_file_id, "caption": caption, "parse_mode": "HTML"}, ...]
    """
    if not token or not chat_id or not media:
        return {"ok": False, "description": "Empty token, chat_id or media"}
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMediaGroup"
    _wait_rate_limit(token, chat_id)
    try:
        with httpx.Client(timeout=20.0) as client:
            payload = {
                "chat_id": str(chat_id),
                "media": media[:10],
            }
            resp = _post_with_retry(client, url, json=payload)
            return resp.json()
    except Exception as exc:
        logger.error("sendMediaGroup error for chat %s: %s", chat_id, exc)
        return {"ok": False, "description": str(exc)}


def edit_message_text(token: str, chat_id: str, message_id: int, text: str, parse_mode: str = "HTML", reply_markup: dict = None) -> dict:
    if not token or not chat_id or not message_id or not text:
        return {"ok": False, "description": "Missing parameters"}
    url = f"{TELEGRAM_API_BASE}/bot{token}/editMessageText"
    payload = {
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = _post_with_retry(client, url, json=payload)
            return resp.json()
    except Exception as exc:
        logger.error("editMessageText error for chat %s msg %s: %s", chat_id, message_id, exc)
        return {"ok": False, "description": str(exc)}


def edit_message_reply_markup(token: str, chat_id: str, message_id: int, reply_markup: dict = None) -> dict:
    if not token or not chat_id or not message_id:
        return {"ok": False, "description": "Missing parameters"}
    url = f"{TELEGRAM_API_BASE}/bot{token}/editMessageReplyMarkup"
    payload = {
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "reply_markup": reply_markup or {"inline_keyboard": []},
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = _post_with_retry(client, url, json=payload)
            return resp.json()
    except Exception as exc:
        logger.error("editMessageReplyMarkup error for chat %s msg %s: %s", chat_id, message_id, exc)
        return {"ok": False, "description": str(exc)}


def set_my_commands(token: str, commands: list, scope=None, language_code: str = None) -> dict:
    """Ставить меню команд Telegram (setMyCommands)."""
    if not token:
        return {"ok": False, "description": "Empty token"}
    url = f"{TELEGRAM_API_BASE}/bot{token}/setMyCommands"
    payload = {"commands": commands}
    if isinstance(scope, dict):
        payload["scope"] = scope
    elif scope == "chat":
        payload["scope"] = {"type": "all_private_chats"}
    elif scope == "default":
        payload["scope"] = {"type": "default"}
    if language_code:
        payload["language_code"] = language_code
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            return resp.json()
    except Exception as exc:
        logger.error("setMyCommands error: %s", exc)
        return {"ok": False, "description": str(exc)}


def answer_callback_query(token: str, callback_query_id: str, text: str = None, show_alert: bool = False) -> dict:
    """Ответ на callback_query, чтобы убрать крутящиеся часы Telegram."""
    if not token or not callback_query_id:
        return {"ok": False, "description": "Empty token or callback_query_id"}
    url = f"{TELEGRAM_API_BASE}/bot{token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]
    payload["show_alert"] = bool(show_alert)
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            return resp.json()
    except Exception as exc:
        logger.error("answerCallbackQuery error: %s", exc)
        return {"ok": False, "description": str(exc)}


def get_file(token: str, file_id: str) -> dict:
    """Получает информацию о файле по file_id."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/getFile"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params={"file_id": file_id})
            data = resp.json()
            if data.get("ok"):
                return data.get("result", {})
            return {}
    except Exception as exc:
        logger.error("getFile error: %s", exc)
        return {}


def download_file(token: str, file_path: str) -> bytes:
    """Скачивает файл из Telegram по file_path."""
    url = f"{TELEGRAM_API_BASE}/file/bot{token}/{file_path}"
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                return resp.content
            return b""
    except Exception as exc:
        logger.error("download_file error: %s", exc)
        return b""


def prepare_photo_for_telegram(photo_ref: str):
    """
    Принимает photo_ref:
    - Telegram file_id (возвращает как есть)
    - URL (http/https) или путь в /media/
    Если фото > 5MB или размеры > 1280px, сжимает через PIL до <= 1280px и возвращает bytes.
    Иначе возвращает URL или bytes.
    """
    import io
    import os
    from PIL import Image

    if not photo_ref:
        return None
    ref = str(photo_ref).strip()
    if not ref:
        return None

    # Если это file_id Telegram (нет слэшей, нет протокола)
    if "://" not in ref and not ref.startswith("/") and not ref.startswith("."):
        return ref

    raw_bytes = None
    # Проверяем локальный файл
    if ref.startswith("/media/") or ref.startswith("media/"):
        media_root = getattr(django_settings, "MEDIA_ROOT", "")
        rel_path = ref[7:] if ref.startswith("/media/") else ref[6:]
        local_path = os.path.join(media_root, rel_path)
        if os.path.exists(local_path):
            try:
                with open(local_path, "rb") as f:
                    raw_bytes = f.read()
            except Exception as exc:
                logger.warning("Failed to read local media file %s: %s", local_path, exc)

    if raw_bytes is None and (ref.startswith("http://") or ref.startswith("https://")):
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                resp = client.get(ref)
                if resp.status_code == 200:
                    raw_bytes = resp.content
        except Exception as exc:
            logger.warning("Failed to fetch image %s: %s", ref, exc)
            return ref

    if not raw_bytes:
        return ref

    # Проверяем размер и пропорции через PIL
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        max_side = max(img.width, img.height)
        if max_side > 1280 or len(raw_bytes) > 4 * 1024 * 1024 or img.format not in ("JPEG", "PNG"):
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if max_side > 1280:
                scale = 1280 / float(max_side)
                new_w = int(img.width * scale)
                new_h = int(img.height * scale)
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            out_buf = io.BytesIO()
            img.save(out_buf, format="JPEG", quality=85, optimize=True)
            return out_buf.getvalue()
        return raw_bytes
    except Exception as exc:
        logger.warning("Image processing failed for %s: %s", ref, exc)
        return raw_bytes or ref
