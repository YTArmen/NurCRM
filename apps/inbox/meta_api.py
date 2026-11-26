"""Тонкий клиент Meta Graph API / Instagram API / 360dialog через requests.
В тестах мокается: apps.inbox.meta_api.requests. Секреты в тексты ошибок не попадают."""
import requests
from django.conf import settings

GRAPH = "https://graph.facebook.com"
IG_GRAPH = "https://graph.instagram.com"
IG_OAUTH = "https://api.instagram.com/oauth/access_token"
D360 = "https://waba-v2.360dialog.io"


def graph_version():
    return getattr(settings, "META_GRAPH_VERSION", "v21.0")


class MetaError(Exception):
    def __init__(self, message, code=None, status=None, permanent=False, retry_after=None, subcode=None):
        super().__init__(message)
        self.code, self.status, self.permanent = code, status, permanent
        self.retry_after, self.subcode = retry_after, subcode


def _request(method, url, *, token=None, headers=None, json=None, params=None, data=None, timeout=15):
    h = dict(headers or {})
    if token:
        h.setdefault("Authorization", f"Bearer {token}")
    try:
        resp = requests.request(method, url, headers=h, json=json, params=params, data=data, timeout=timeout)
    except Exception as exc:
        raise MetaError(f"Ошибка соединения с Meta: {type(exc).__name__}")
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
        err = (body.get("error") if isinstance(body, dict) else None) or {}
        code = err.get("code")
        msg = err.get("message") or f"HTTP {resp.status_code}"
        # 4xx (кроме 429 и лимитов 4/17/32/613/130429) — повтор бесполезен; 190 — токен отозван
        throttled = resp.status_code == 429 or code in (4, 17, 32, 613, 130429, 80007)
        permanent = 400 <= resp.status_code < 500 and not throttled
        raise MetaError(f"Meta: {msg}", code=code, status=resp.status_code, permanent=permanent,
                        retry_after=60 if throttled else None, subcode=err.get("error_subcode"))
    return body


# ------------------------------------------------------------------ WhatsApp Cloud API / шаблоны

def _d360_headers(api_key):
    return {"D360-API-KEY": api_key}


def list_templates(provider, waba_id, token):
    if provider == "360dialog":
        body = _request("GET", f"{D360}/configs/templates", headers=_d360_headers(token))
        return body.get("waba_templates") or body.get("data") or []
    out, url, params = [], f"{GRAPH}/{graph_version()}/{waba_id}/message_templates", {"limit": 100}
    for _ in range(10):
        body = _request("GET", url, token=token, params=params)
        out.extend(body.get("data") or [])
        nxt = ((body.get("paging") or {}).get("next"))
        if not nxt:
            break
        url, params = nxt, None
    return out


def create_template(provider, waba_id, token, payload):
    if provider == "360dialog":
        return _request("POST", f"{D360}/configs/templates", headers=_d360_headers(token), json=payload)
    return _request("POST", f"{GRAPH}/{graph_version()}/{waba_id}/message_templates", token=token, json=payload)


def send_whatsapp(provider, phone_number_id, token, payload):
    if provider == "360dialog":
        return _request("POST", f"{D360}/messages", headers=_d360_headers(token), json=payload)
    return _request("POST", f"{GRAPH}/{graph_version()}/{phone_number_id}/messages", token=token, json=payload)


# ------------------------------------------------------------------ Embedded Signup (Б-85)

def exchange_code(code):
    """Код Embedded Signup → токен бизнеса. app_id/secret — из окружения, не из базы."""
    return _request("GET", f"{GRAPH}/{graph_version()}/oauth/access_token", params={
        "client_id": settings.META_APP_ID, "client_secret": settings.META_APP_SECRET, "code": code})


def debug_token_waba(token):
    """granular_scopes токена → список waba_id."""
    body = _request("GET", f"{GRAPH}/{graph_version()}/debug_token", params={
        "input_token": token, "access_token": f"{settings.META_APP_ID}|{settings.META_APP_SECRET}"})
    scopes = (body.get("data") or {}).get("granular_scopes") or []
    for sc in scopes:
        if sc.get("scope") == "whatsapp_business_management" and sc.get("target_ids"):
            return list(sc["target_ids"])
    return []


def waba_phone_numbers(waba_id, token):
    return _request("GET", f"{GRAPH}/{graph_version()}/{waba_id}/phone_numbers", token=token,
                    params={"fields": "id,display_phone_number,verified_name,quality_rating,name_status,messaging_limit_tier"}
                    ).get("data") or []


def subscribe_app(waba_id, token):
    return _request("POST", f"{GRAPH}/{graph_version()}/{waba_id}/subscribed_apps", token=token)


def register_number(phone_number_id, token, pin):
    return _request("POST", f"{GRAPH}/{graph_version()}/{phone_number_id}/register", token=token,
                    json={"messaging_product": "whatsapp", "pin": pin})


def phone_number_info(phone_number_id, token):
    return _request("GET", f"{GRAPH}/{graph_version()}/{phone_number_id}", token=token,
                    params={"fields": "quality_rating,name_status,messaging_limit_tier,display_phone_number,verified_name"})


# ------------------------------------------------------------------ Instagram Login (Б-86)

def ig_exchange_code(code, redirect_uri):
    return _request("POST", IG_OAUTH, data={
        "client_id": settings.INSTAGRAM_APP_ID, "client_secret": settings.INSTAGRAM_APP_SECRET,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code})


def ig_long_lived(short_token):
    return _request("GET", f"{IG_GRAPH}/access_token", params={
        "grant_type": "ig_exchange_token", "client_secret": settings.INSTAGRAM_APP_SECRET,
        "access_token": short_token})


def ig_refresh(token):
    return _request("GET", f"{IG_GRAPH}/refresh_access_token", params={
        "grant_type": "ig_refresh_token", "access_token": token})


def ig_me(token):
    return _request("GET", f"{IG_GRAPH}/{graph_version()}/me", token=token,
                    params={"fields": "user_id,username,account_type"})


def ig_subscribe(token):
    return _request("POST", f"{IG_GRAPH}/{graph_version()}/me/subscribed_apps", token=token,
                    params={"subscribed_fields": "messages,messaging_postbacks,message_reactions"})


def ig_send(token, payload):
    return _request("POST", f"{IG_GRAPH}/{graph_version()}/me/messages", token=token, json=payload)
