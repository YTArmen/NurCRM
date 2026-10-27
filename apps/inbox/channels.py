"""Каналы компании: создание, проверка, серверный бот как канал (Б-73) и правила Б-87."""
import secrets as pysecrets

from django.conf import settings as dj_settings
from django.utils import timezone

from core.exceptions import ApiError

from . import crypto as sec
from . import telegram
from .models import Channel

ALL_KINDS = [k for k, _ in Channel.Kind.choices]
IMPLEMENTED_KINDS = ["telegram_bot"]  # этап 1; остальные виды — на следующих этапах
KIND_COMPANY_FLAG = {
    "telegram_bot": "can_view_telegram", "telegram_business": "can_view_telegram",
    "whatsapp_cloud": "can_view_whatsapp", "whatsapp_bsp": "can_view_whatsapp",
    "instagram": "can_view_instagram",
}
AI_MODES = ("off", "draft", "auto")
AUTO_ASSIGN = ("none", "round_robin", "branch")
DEFAULT_SETTINGS = {"ai_mode": "off", "working_hours": {}, "auto_assign": "none", "ai_pause_minutes": 30}
EXTERNAL_KEYS = ("bot_username", "phone", "display_name", "waba_id", "phone_number_id", "ig_username", "wazzup_channel_id")


def is_unofficial(channel) -> bool:
    return channel.provider == Channel.Provider.UNOFFICIAL


def allowed_kinds(company) -> list:
    out = []
    for k in ALL_KINDS:
        flag = KIND_COMPANY_FLAG.get(k)
        if flag is None or getattr(company, flag, False):
            out.append(k)
    return out


def webhook_url(channel) -> str:
    base = getattr(dj_settings, "TELEGRAM_WEBHOOK_BASE_URL", "") or "https://app.nurcrm.kg"
    return f"{base.rstrip('/')}/api/inbox/webhooks/{channel.id}/"


def validate_settings(data, provider, current=None) -> dict:
    """Сливает настройки канала с проверкой; Б-87: на неофициальном канале ai_mode=auto запрещён."""
    out = dict(DEFAULT_SETTINGS)
    out.update(current or {})
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ApiError("invalid", "settings должен быть объектом.", fields={"settings": ["Ожидается объект"]})
    out.update(data)
    if out["ai_mode"] not in AI_MODES:
        raise ApiError("invalid", "ai_mode: off, draft или auto.", fields={"settings.ai_mode": ["Недопустимое значение"]})
    if out["auto_assign"] not in AUTO_ASSIGN:
        raise ApiError("invalid", "auto_assign: none, round_robin или branch.", fields={"settings.auto_assign": ["Недопустимое значение"]})
    try:
        out["ai_pause_minutes"] = int(out["ai_pause_minutes"])
        if not 0 <= out["ai_pause_minutes"] <= 1440:
            raise ValueError
    except (TypeError, ValueError):
        raise ApiError("invalid", "ai_pause_minutes: число 0…1440.", fields={"settings.ai_pause_minutes": ["Недопустимое значение"]})
    from . import ai as ai_svc
    ai_svc.validate_ai_settings(out)
    if provider == Channel.Provider.UNOFFICIAL and out["ai_mode"] == "auto":
        raise ApiError("channel_not_allowed",
                       "На неофициальном канале ИИ в режиме auto запрещён: аккаунт могут заблокировать.",
                       data={"reason": "unofficial_ai_auto"})
    return out


def ensure_channel_allowed_for_broadcast(channel):
    """Б-87: рассылки на неофициальных каналах запрещены (вызывается модулем рассылок, Б-81)."""
    if is_unofficial(channel):
        raise ApiError("channel_not_allowed", "Рассылки на неофициальном канале запрещены.",
                       data={"reason": "unofficial_broadcast"})


def accept_risk(channel, user):
    channel.risk_accepted_at = timezone.now()
    channel.risk_accepted_by = user


def ensure_server_bot_channel(company):
    """Серверный Telegram-бот компании виден как один канал telegram_bot (Б-73 п. 3)."""
    from apps.main.telegram_bot.models import TelegramBotSettings

    bs = TelegramBotSettings.objects.filter(company=company).first()
    if not bs or not (bs.token_set or bs.bot_username):
        return None
    if bs.mode != TelegramBotSettings.Mode.SERVER:
        status = Channel.Status.DISABLED
    elif bs.token_set and bs.webhook_ok:
        status = Channel.Status.ACTIVE
    elif bs.webhook_error:
        status = Channel.Status.ERROR
    else:
        status = Channel.Status.CONNECTING
    ch = Channel.objects.filter(bot_settings=bs).first()
    ext = {"bot_username": bs.bot_username} if bs.bot_username else {}
    if ch is None:
        return Channel.objects.create(
            company=company, bot_settings=bs, kind=Channel.Kind.TELEGRAM_BOT, provider=Channel.Provider.TELEGRAM,
            name=f"Telegram-бот @{bs.bot_username}" if bs.bot_username else "Telegram-бот",
            status=status, external=ext, webhook_secret=bs.secret_token or "",
            settings=dict(DEFAULT_SETTINGS),
            health={"last_error": bs.webhook_error or None},
        )
    changed = []
    if ch.status != status:
        ch.status = status
        changed.append("status")
    if ext and ch.external.get("bot_username") != ext["bot_username"]:
        ch.external = {**ch.external, **ext}
        changed.append("external")
    if ch.webhook_secret != (bs.secret_token or ""):
        ch.webhook_secret = bs.secret_token or ""
        changed.append("webhook_secret")
    if changed:
        ch.save(update_fields=changed + ["updated_at"])
    return ch


def touch_health(channel_id, *, inbound=False, outbound=False, error=None):
    """Обновляет health канала (последнее входящее/исходящее, последняя ошибка)."""
    ch = Channel.objects.filter(id=channel_id).only("id", "health").first()
    if not ch:
        return
    now = timezone.now().isoformat()
    h = dict(ch.health or {})
    if inbound:
        h["last_inbound_at"] = now
    if outbound:
        h["last_outbound_at"] = now
    if error:
        h["last_error"], h["last_error_at"] = str(error)[:300], now
    Channel.objects.filter(id=channel_id).update(health=h)


def serialize(channel) -> dict:
    h = channel.health or {}
    health = {k: h.get(k) for k in ("last_inbound_at", "last_outbound_at", "last_error", "last_error_at")}
    health["quality_rating"] = h.get("quality_rating")
    health["messaging_tier"] = h.get("messaging_tier")
    for k in ("display_name_status", "token_expires_at", "coexistence"):
        if h.get(k) is not None:
            health[k] = h[k]
    ext = channel.external or {}
    external = {k: ext[k] for k in EXTERNAL_KEYS if ext.get(k) is not None}
    return {
        "id": str(channel.id),
        "kind": channel.kind,
        "provider": channel.provider,
        "name": channel.name,
        "branch": str(channel.branch_id) if channel.branch_id else None,
        "status": channel.status,
        "external": external,
        "health": health,
        "settings": {**DEFAULT_SETTINGS, **(channel.settings or {})},
        "ai_warning": _ai_warning(channel),
        **sec.secret_flags(channel),
        "unofficial": is_unofficial(channel),
        "risk_accepted_at": channel.risk_accepted_at.isoformat() if channel.risk_accepted_at else None,
        "risk_accepted_by": ({"id": str(channel.risk_accepted_by_id)} if channel.risk_accepted_by_id else None),
        "managed_by_bot": bool(channel.bot_settings_id),
        "webhook_url": webhook_url(channel),
        "created_at": channel.created_at.isoformat(),
    }


def apply_fields(channel, data: dict, user, creating=False):
    """Применяет поля из тела запроса к каналу (без сохранения)."""
    if creating:
        kind = data.get("kind")
        if kind not in ALL_KINDS:
            raise ApiError("invalid", "kind: недопустимое значение.", fields={"kind": ["Недопустимое значение"]})
        channel.kind = kind
        provider = data.get("provider") or (
            "telegram" if kind.startswith("telegram") else "meta" if kind in ("whatsapp_cloud", "instagram")
            else "own" if kind == "web_widget" else "wazzup")
        if provider not in [p for p, _ in Channel.Provider.choices]:
            raise ApiError("invalid", "provider: недопустимое значение.", fields={"provider": ["Недопустимое значение"]})
        channel.provider = provider
        flag = KIND_COMPANY_FLAG.get(kind)
        if flag and not getattr(channel.company, flag, False):
            raise ApiError("channel_not_allowed", "Этот вид канала не подключён для вашей компании.",
                           status_code=403, data={"kind": kind})
        channel.webhook_secret = pysecrets.token_urlsafe(24)
    # Б-87: неофициальный канал — только с подтверждением риска
    if channel.provider == Channel.Provider.UNOFFICIAL and not channel.risk_accepted_at:
        if data.get("risk_accepted") is True:
            accept_risk(channel, user)
        else:
            raise ApiError(
                "risk_not_accepted",
                "Неофициальный шлюз нарушает правила WhatsApp/Instagram: номер или аккаунт могут заблокировать. "
                "Подтвердите риск (risk_accepted: true).")
    if "name" in data:
        channel.name = str(data["name"] or "")[:128]
    if "branch" in data:
        b = data["branch"]
        if b in (None, ""):
            channel.branch = None
        else:
            from apps.users.models import Branch
            try:
                br = Branch.objects.filter(id=b, company=channel.company).first()
            except Exception:
                br = None
            if not br:
                raise ApiError("invalid", "Филиал не найден.", fields={"branch": ["Нет такого филиала"]})
            channel.branch = br
    if "status" in data:
        if data["status"] not in ("disabled", "connecting", "active", "error"):
            raise ApiError("invalid", "status: недопустимое значение.", fields={"status": ["Недопустимое значение"]})
        if not creating and data["status"] in ("disabled", "connecting"):
            channel.status = data["status"]
    if isinstance(data.get("external"), dict):
        channel.external = {**(channel.external or {}), **{k: v for k, v in data["external"].items() if k in EXTERNAL_KEYS}}
    channel.settings = validate_settings(data.get("settings"), channel.provider, channel.settings)
    updates = {k: data[k] for k in sec.SECRET_FIELDS if k in data}
    if channel.bot_settings_id and "token" in updates:
        raise ApiError("channel_managed_by_bot", "Токен серверного бота меняется в настройках бота (telegram-bot/settings).")
    if updates:
        sec.set_secrets(channel, updates)


def check(channel) -> dict:
    """POST …/check/: проверка канала. Ответ {ok, status, detail}."""
    if channel.kind not in IMPLEMENTED_KINDS:
        return {"ok": False, "status": channel.status, "detail": "Проверка этого вида канала появится на следующем этапе."}
    token = sec.channel_token(channel)
    if not token:
        return _check_fail(channel, "Токен не задан")
    try:
        me = telegram.get_me(token)
    except telegram.TelegramError as exc:
        return _check_fail(channel, str(exc))
    detail = f"Telegram: @{me.get('username')}"
    ext = dict(channel.external or {})
    ext["bot_username"] = me.get("username")
    ext["display_name"] = me.get("first_name")
    health = dict(channel.health or {})
    try:
        info = telegram.get_webhook_info(token)
        if info.get("last_error_message"):
            health["last_error"] = str(info["last_error_message"])[:300]
            health["last_error_at"] = timezone.now().isoformat()
        health["pending_update_count"] = info.get("pending_update_count", 0)
        if not (info.get("url") or ""):
            detail += "; вебхук не установлен"
    except telegram.TelegramError as exc:
        detail += f"; getWebhookInfo: {exc}"
    channel.external, channel.health = ext, health
    if channel.status != Channel.Status.DISABLED:
        channel.status = Channel.Status.ACTIVE
    channel.save(update_fields=["external", "health", "status", "updated_at"])
    return {"ok": True, "status": channel.status, "detail": detail}


def _check_fail(channel, detail):
    h = dict(channel.health or {})
    h["last_error"], h["last_error_at"] = detail[:300], timezone.now().isoformat()
    channel.health = h
    if channel.status != Channel.Status.DISABLED:
        channel.status = Channel.Status.ERROR
    channel.save(update_fields=["health", "status", "updated_at"])
    return {"ok": False, "status": channel.status, "detail": detail}


def install_webhook(channel) -> dict:
    token = sec.channel_token(channel)
    try:
        telegram.set_webhook(token, webhook_url(channel), channel.webhook_secret)
        return {"ok": True}
    except telegram.TelegramError as exc:
        return {"ok": False, "detail": str(exc)}


def _ai_warning(channel):
    from . import ai as ai_svc
    return ai_svc.key_warning(channel)
