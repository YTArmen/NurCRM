"""Б-85 (WhatsApp Embedded Signup, качество номера) и Б-86 (Instagram Login, токены).
Выключено, пока в окружении не заданы META_APP_ID/META_APP_SECRET (WhatsApp) или
INSTAGRAM_APP_ID/INSTAGRAM_APP_SECRET (Instagram). Все вызовы Meta — через meta_api (мок в тестах)."""
import logging
import os
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.utils import timezone

from core.exceptions import ApiError

from . import ai, crypto, meta_api
from .models import Channel, ChannelContact, Conversation

logger = logging.getLogger("inbox")
STATE_SALT = "inbox-meta-state"
STATE_MAX_AGE = 900
IG_SCOPES = "instagram_business_basic,instagram_business_manage_messages"
IG_TOKEN_DAYS = 60
IG_REFRESH_BEFORE = timedelta(days=10)


def _env(name):
    return getattr(settings, name, "") or os.environ.get(name, "")


def meta_enabled() -> bool:
    return bool(_env("META_APP_ID") and _env("META_APP_SECRET"))


def instagram_enabled() -> bool:
    return bool(_env("INSTAGRAM_APP_ID") and _env("INSTAGRAM_APP_SECRET"))


def meta_app_secret() -> str:
    return _env("META_APP_SECRET")


def instagram_app_secret() -> str:
    return _env("INSTAGRAM_APP_SECRET")


def _need(enabled, what):
    if not enabled:
        raise ApiError("integration_disabled", f"Подключение {what} выключено: не заданы ключи приложения Meta в окружении.",
                       status_code=501)


def make_state(user, company, purpose):
    return signing.dumps({"u": str(user.id), "c": company.id if isinstance(company.id, int) else str(company.id),
                          "p": purpose, "n": secrets.token_hex(8)}, salt=STATE_SALT)


def read_state(state, user, company, purpose):
    try:
        d = signing.loads(state or "", salt=STATE_SALT, max_age=STATE_MAX_AGE)
    except signing.BadSignature:
        raise ApiError("invalid_state", "Параметр state недействителен или устарел.")
    if d.get("u") != str(user.id) or str(d.get("c")) != str(company.id) or d.get("p") != purpose:
        raise ApiError("invalid_state", "Параметр state не подходит к этому запросу.")
    return d


def redirect_uri(kind):
    base = getattr(settings, "TELEGRAM_WEBHOOK_BASE_URL", "") or "https://app.nurcrm.kg"
    return getattr(settings, f"META_{kind.upper()}_REDIRECT_URI", "") or f"{base.rstrip('/')}/chats/connect/{kind}"


# ------------------------------------------------------------------ WhatsApp Embedded Signup

def whatsapp_start(user, company) -> dict:
    _need(meta_enabled(), "WhatsApp напрямую")
    state = make_state(user, company, "wa")
    q = {"client_id": _env("META_APP_ID"), "redirect_uri": redirect_uri("whatsapp"), "state": state,
         "response_type": "code", "override_default_response_type": "true",
         "extras": '{"setup":{},"featureType":"","sessionInfoVersion":"3"}'}
    if _env("META_CONFIG_ID"):
        q["config_id"] = _env("META_CONFIG_ID")
    return {"url": f"https://www.facebook.com/{meta_api.graph_version()}/dialog/oauth?{urlencode(q)}", "state": state}


def _provider_err(exc, code="provider_error"):
    return ApiError(code, str(exc), status_code=502 if not exc.permanent else 400, data={"code": exc.code})


def whatsapp_callback(user, company, code, state, waba_id="", phone_number_id=""):
    _need(meta_enabled(), "WhatsApp напрямую")
    read_state(state, user, company, "wa")
    if not code:
        raise ApiError("invalid", "Нужен code.", fields={"code": ["Обязательное поле"]})
    try:
        token = meta_api.exchange_code(code).get("access_token", "")
        if not token:
            raise ApiError("provider_error", "Meta не вернула токен.", status_code=502)
        waba = waba_id or (meta_api.debug_token_waba(token) or [""])[0]
        if not waba:
            raise ApiError("waba_not_found", "В выданном доступе нет аккаунта WhatsApp Business.")
        numbers = meta_api.waba_phone_numbers(waba, token)
        num = next((n for n in numbers if n.get("id") == phone_number_id), None) if phone_number_id else (numbers[0] if numbers else None)
        if not num:
            raise ApiError("phone_not_found", "В аккаунте нет номера телефона.")
        meta_api.subscribe_app(waba, token)
        pin = "".join(secrets.choice("0123456789") for _ in range(6))
        try:
            meta_api.register_number(num["id"], token, pin)
        except meta_api.MetaError as exc:  # номер уже зарегистрирован — не ошибка
            logger.info("register_number: %s", exc)
    except meta_api.MetaError as exc:
        raise _provider_err(exc)
    ext = {"waba_id": waba, "phone_number_id": num["id"], "phone": num.get("display_phone_number") or "",
           "display_name": num.get("verified_name") or ""}
    ch = Channel.objects.filter(company=company, kind=Channel.Kind.WHATSAPP_CLOUD, external__phone_number_id=num["id"]).first()
    if ch is None:
        ch = Channel(company=company, kind=Channel.Kind.WHATSAPP_CLOUD, provider=Channel.Provider.META,
                     webhook_secret=secrets.token_urlsafe(24),
                     name=f"WhatsApp {ext['phone']}".strip(), settings={"ai_mode": "off"})
    ch.external = {**(ch.external or {}), **ext}
    ch.status = Channel.Status.ACTIVE
    ch.health = {**(ch.health or {}), "quality_rating": (num.get("quality_rating") or "").lower() or None,
                 "messaging_tier": num.get("messaging_limit_tier"), "display_name_status": num.get("name_status"),
                 "last_error": None}
    crypto.set_secrets(ch, {"access_token": token, "pin": pin})
    ch.save()
    return ch


# ------------------------------------------------------------------ Instagram Login

def instagram_start(user, company) -> dict:
    _need(instagram_enabled(), "Instagram")
    state = make_state(user, company, "ig")
    q = {"client_id": _env("INSTAGRAM_APP_ID"), "redirect_uri": redirect_uri("instagram"), "response_type": "code",
         "scope": IG_SCOPES, "state": state, "force_reauth": "true"}
    return {"url": f"https://www.instagram.com/oauth/authorize?{urlencode(q)}", "state": state}


def instagram_callback(user, company, code, state):
    _need(instagram_enabled(), "Instagram")
    read_state(state, user, company, "ig")
    if not code:
        raise ApiError("invalid", "Нужен code.", fields={"code": ["Обязательное поле"]})
    try:
        short = meta_api.ig_exchange_code(code, redirect_uri("instagram"))
        long = meta_api.ig_long_lived(short.get("access_token", ""))
        token = long.get("access_token", "")
        me = meta_api.ig_me(token)
        if (me.get("account_type") or "").upper() not in ("BUSINESS", "MEDIA_CREATOR"):
            raise ApiError("ig_not_professional", "Нужен профессиональный аккаунт Instagram (бизнес или автор).")
        meta_api.ig_subscribe(token)
    except meta_api.MetaError as exc:
        raise _provider_err(exc)
    uid = str(me.get("user_id") or me.get("id") or short.get("user_id") or "")
    ch = Channel.objects.filter(company=company, kind=Channel.Kind.INSTAGRAM, external__ig_user_id=uid).first()
    if ch is None:
        ch = Channel(company=company, kind=Channel.Kind.INSTAGRAM, provider=Channel.Provider.META,
                     webhook_secret=secrets.token_urlsafe(24), name=f"Instagram @{me.get('username')}",
                     settings={"ai_mode": "off"})
    expires = timezone.now() + timedelta(seconds=int(long.get("expires_in") or IG_TOKEN_DAYS * 86400))
    ch.external = {**(ch.external or {}), "ig_user_id": uid, "ig_username": me.get("username") or ""}
    ch.status = Channel.Status.ACTIVE
    ch.health = {**(ch.health or {}), "token_expires_at": expires.isoformat(), "last_error": None}
    crypto.set_secrets(ch, {"access_token": token})
    ch.save()
    return ch


def refresh_instagram_tokens(now=None) -> dict:
    """Автообновление токена за 10 дней до срока; сбой → status:error и уведомление владельцу."""
    now = now or timezone.now()
    done = failed = 0
    for ch in Channel.objects.filter(kind=Channel.Kind.INSTAGRAM, status__in=("active", "connecting")):
        raw = (ch.health or {}).get("token_expires_at")
        try:
            exp = timezone.datetime.fromisoformat(raw) if raw else None
        except ValueError:
            exp = None
        if exp is None or exp - now > IG_REFRESH_BEFORE:
            continue
        token = crypto.get_secrets(ch).get("access_token", "")
        try:
            res = meta_api.ig_refresh(token)
            crypto.set_secrets(ch, {"access_token": res.get("access_token") or token})
            new_exp = now + timedelta(seconds=int(res.get("expires_in") or IG_TOKEN_DAYS * 86400))
            ch.health = {**(ch.health or {}), "token_expires_at": new_exp.isoformat(), "last_error": None}
            ch.save(update_fields=["secrets_enc", "health", "updated_at"])
            done += 1
        except meta_api.MetaError as exc:
            mark_channel_error(ch, f"Не удалось обновить токен Instagram: {exc}")
            failed += 1
    return {"refreshed": done, "failed": failed}


def mark_channel_error(channel, detail):
    h = dict(channel.health or {})
    h["last_error"], h["last_error_at"] = str(detail)[:300], timezone.now().isoformat()
    channel.health, channel.status = h, Channel.Status.ERROR
    channel.save(update_fields=["health", "status", "updated_at"])
    ai.notify_owner(channel.company_id, f"⚠️ Канал «{channel.name or channel.kind}» в «Чатах» отключился: {str(detail)[:150]}")


# ------------------------------------------------------------------ входящие события Meta

def route_channel(payload, kind):
    """Канал по телефону/WABA (WhatsApp) или id аккаунта (Instagram) из app-level вебхука."""
    out = []
    for entry in (payload.get("entry") or []):
        if kind == "instagram":
            ch = Channel.objects.filter(kind=Channel.Kind.INSTAGRAM, external__ig_user_id=str(entry.get("id"))).first()
            if ch:
                out.append((ch, {"object": "instagram", "entry": [entry]}))
            continue
        for chg in entry.get("changes") or []:
            val = chg.get("value") or {}
            pn = (val.get("metadata") or {}).get("phone_number_id")
            ch = (Channel.objects.filter(kind__in=("whatsapp_cloud", "whatsapp_bsp"), external__phone_number_id=str(pn)).first()
                  if pn else Channel.objects.filter(kind__in=("whatsapp_cloud", "whatsapp_bsp"),
                                                    external__waba_id=str(entry.get("id"))).first())
            if ch:
                out.append((ch, {"object": "whatsapp_business_account", "entry": [{"id": entry.get("id"), "changes": [chg]}]}))
    return out


def process_whatsapp(channel, payload) -> str:
    from . import services
    n = 0
    for entry in payload.get("entry") or []:
        for chg in entry.get("changes") or []:
            field, val = chg.get("field"), chg.get("value") or {}
            if field == "messages":
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in val.get("contacts") or []}
                for m in val.get("messages") or []:
                    kind, text, atts = _wa_content(m)
                    services.ingest_inbound(channel, m.get("from"), message_external_id=m.get("id") or "", text=text,
                                            kind=kind, attachments=atts, display_name=names.get(m.get("from"), ""),
                                            phone=m.get("from") or "")
                    n += 1
                for st in val.get("statuses") or []:
                    err = None
                    if st.get("status") == "failed":
                        e = (st.get("errors") or [{}])[0]
                        err = {"code": "channel_error", "detail": str(e.get("title") or e.get("message") or "Не доставлено")[:300]}
                    services.apply_status(channel, st.get("id"), st.get("status"), err)
                    n += 1
            elif field in ("phone_number_quality_update", "account_update", "phone_number_name_update"):
                handle_quality(channel, field, val)
                n += 1
    return f"processed:{n}"


def _wa_content(m):
    t = m.get("type") or "text"
    if t == "text":
        return "text", (m.get("text") or {}).get("body", ""), []
    if t == "button":
        return "text", (m.get("button") or {}).get("text", ""), []
    if t == "interactive":
        it = m.get("interactive") or {}
        r = it.get("button_reply") or it.get("list_reply") or {}
        return "text", r.get("title", ""), []
    if t == "location":
        loc = m.get("location") or {}
        return "location", "", [{"lat": loc.get("latitude"), "lon": loc.get("longitude")}]
    part = m.get(t) or {}
    kind = {"image": "image", "audio": "audio", "video": "video", "document": "file", "sticker": "sticker"}.get(t, "file")
    return kind, part.get("caption", ""), [{"media_id": None, "file_id": part.get("id"), "mime": part.get("mime_type") or "",
                                            "name": part.get("filename") or ""}]


def handle_quality(channel, field, val):
    """Статус номера: quality_rating, messaging_tier, display_name_status — по вебхукам Meta."""
    h = dict(channel.health or {})
    if field == "phone_number_quality_update":
        if val.get("current_limit"):
            h["messaging_tier"] = val["current_limit"]
        if val.get("quality_rating") or val.get("event") in ("FLAGGED", "DOWNGRADE", "UPGRADE", "ONBOARDING"):
            pass
    if field == "phone_number_name_update":
        h["display_name_status"] = val.get("decision") or val.get("name_status") or h.get("display_name_status")
    if field == "account_update" and str(val.get("event") or "").upper() in (
            "PARTNER_REMOVED", "ACCOUNT_DELETED", "DISABLED_UPDATE", "ACCOUNT_VIOLATION", "PARTNER_APP_UNINSTALLED"):
        channel.health = h
        mark_channel_error(channel, f"Meta: {val.get('event')}")
        return
    token = crypto.get_secrets(channel).get("access_token", "")
    pn = (channel.external or {}).get("phone_number_id")
    if token and pn:  # актуальные значения у Meta
        try:
            info = meta_api.phone_number_info(pn, token)
            h["quality_rating"] = (info.get("quality_rating") or "").lower() or h.get("quality_rating")
            h["messaging_tier"] = info.get("messaging_limit_tier") or h.get("messaging_tier")
            h["display_name_status"] = info.get("name_status") or h.get("display_name_status")
        except meta_api.MetaError as exc:
            logger.info("phone_number_info: %s", exc)
    channel.health = h
    channel.save(update_fields=["health", "updated_at"])
    if (h.get("quality_rating") or "") == "red":
        ai.notify_owner(channel.company_id, "⚠️ Качество номера WhatsApp упало до «красного»: сократите рассылки.")


def process_instagram(channel, payload) -> str:
    from . import services
    n = 0
    for entry in payload.get("entry") or []:
        for ev in entry.get("messaging") or []:
            msg = ev.get("message")
            sender = (ev.get("sender") or {}).get("id")
            if msg and not msg.get("is_echo") and sender and sender != str(entry.get("id")):
                atts = [{"media_id": None, "url": (a.get("payload") or {}).get("url"), "mime": a.get("type") or ""}
                        for a in msg.get("attachments") or []]
                services.ingest_inbound(channel, sender, message_external_id=msg.get("mid") or "", text=msg.get("text") or "",
                                        kind="image" if atts else "text", attachments=atts)
                n += 1
            elif ev.get("postback") and sender:
                pb = ev["postback"]
                services.ingest_inbound(channel, sender, message_external_id=pb.get("mid") or "", text=pb.get("title") or pb.get("payload") or "")
                n += 1
    return f"processed:{n}"
