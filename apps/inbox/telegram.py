"""Тонкий клиент Telegram Bot API через requests (в тестах мокается: apps.inbox.telegram.requests)."""
import requests

API = "https://api.telegram.org"


class TelegramError(Exception):
    def __init__(self, message, code=None, retry_after=None, permanent=False):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.permanent = permanent


def _call(token: str, method: str, payload=None, http="post", timeout=10):
    url = f"{API}/bot{token}/{method}"
    try:
        if http == "get":
            resp = requests.get(url, timeout=timeout)
        else:
            resp = requests.post(url, json=payload or {}, timeout=timeout)
        data = resp.json()
    except Exception as exc:  # сеть/разбор — временная ошибка, текст без токена
        raise TelegramError(f"Ошибка соединения с Telegram: {type(exc).__name__}")
    if not data.get("ok"):
        desc = data.get("description") or "Unauthorized"
        code = data.get("error_code") or getattr(resp, "status_code", None)
        retry_after = (data.get("parameters") or {}).get("retry_after")
        permanent = code in (400, 401, 403, 404) and not retry_after
        raise TelegramError(f"Telegram: {desc}", code=code, retry_after=retry_after, permanent=permanent)
    return data.get("result")


def get_me(token):
    return _call(token, "getMe", http="get")


def get_webhook_info(token):
    return _call(token, "getWebhookInfo", http="get")


def send_message(token, chat_id, text, reply_to_message_id=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
    return _call(token, "sendMessage", payload)


def set_webhook(token, url, secret_token):
    return _call(token, "setWebhook", {
        "url": url, "secret_token": secret_token,
        "allowed_updates": ["message", "callback_query"],
    })


def send_document(token, chat_id, filename, content: bytes, caption=""):
    """sendDocument (multipart). Подпись ≤ 1024 символов."""
    url = f"{API}/bot{token}/sendDocument"
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "caption": (caption or "")[:1024]},
                             files={"document": (filename or "file", content)}, timeout=30)
        data = resp.json()
    except Exception as exc:
        raise TelegramError(f"Ошибка соединения с Telegram: {type(exc).__name__}")
    if not data.get("ok"):
        code = data.get("error_code") or getattr(resp, "status_code", None)
        retry_after = (data.get("parameters") or {}).get("retry_after")
        raise TelegramError(f"Telegram: {data.get('description') or 'error'}", code=code, retry_after=retry_after,
                            permanent=code in (400, 401, 403, 404) and not retry_after)
    return data.get("result")
