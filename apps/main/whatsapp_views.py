"""WhatsApp компании через Evolution API (09.11.2026).

Номер магазина подключается по QR (как WhatsApp Web). Таблиц нет: инстанс Evolution
называется nur-<company_id>, состояние берём у самого Evolution.

GET  /api/main/whatsapp/          → {configured, state: none|connecting|open|close, phone}
POST /api/main/whatsapp/connect/  → {state, qr}   (только владелец/админ)
POST /api/main/whatsapp/logout/   → {state: none} (только владелец/админ)
POST /api/main/whatsapp/send/ {client, text} → {ok, id}
     Отправка только клиенту этой компании (телефон из карточки), не чаще раза в 3 с на компанию.
GET/PATCH /api/main/whatsapp/settings/ — шаблоны, подпись, скорость, «спасибо» (CompanyAppSettings app=whatsapp).

Платная функция: дополнение компании «whatsapp» (Маркетплейс, промокод). Доступ сотрудника:
can_view_whatsapp — отправка; can_edit_whatsapp=False — «только отправка» (шаблоны не менять).
Владелец/админ — всё; подключать/отключать номер — только владелец/админ.
"""
import logging
import re

import requests
from django.conf import settings
from django.core.cache import cache
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.kassa_views import _ClientScopedMixin, _company
from apps.main.pos_serializers import _is_owner_like
from apps.users.models import CompanyAddon, CompanyAppSettings

log = logging.getLogger(__name__)

MIN_INTERVAL_SEC = 3
MAX_TEXT = 1500
TIMEOUT = 20


def _cfg():
    url = (getattr(settings, "EVOLUTION_API_URL", "") or "").rstrip("/")
    key = getattr(settings, "EVOLUTION_API_KEY", "") or ""
    return url, key


ADDON = "whatsapp"
SETTINGS_KEYS = {"WaTemplates", "WaDefaultTemplate", "WaSignatureOn", "WaSignature", "WaSpeed", "WaThanksOn", "WaThanksText"}


def has_addon(company) -> bool:
    a = CompanyAddon.objects.filter(company=company, code=ADDON).first()
    return bool(a and a.is_effective())


def can_send(user) -> bool:
    return _is_owner_like(user) or bool(getattr(user, "can_view_whatsapp", False))


def can_edit(user) -> bool:
    return _is_owner_like(user) or (bool(getattr(user, "can_view_whatsapp", False)) and getattr(user, "can_edit_whatsapp", None) is not False)


def _instance(company) -> str:
    return f"nur-{company.id}"


def _evo(method, path, **kw):
    url, key = _cfg()
    r = requests.request(method, f"{url}{path}", headers={"apikey": key}, timeout=TIMEOUT, **kw)
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data


def normalize_phone(raw: str) -> str:
    """+996 700 12-34-56 / 0700123456 / 700123456 → 996700123456; пусто, если не похоже на номер."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 10 and d.startswith("0"):
        d = "996" + d[1:]
    elif len(d) == 9:
        d = "996" + d
    return d if 10 <= len(d) <= 15 else ""


def _state(company):
    code, data = _evo("GET", f"/instance/connectionState/{_instance(company)}")
    if code == 404:
        return "none", ""
    state = ((data or {}).get("instance") or {}).get("state") or "close"
    phone = ""
    if state == "open":
        c2, items = _evo("GET", "/instance/fetchInstances", params={"instanceName": _instance(company)})
        if c2 == 200 and isinstance(items, list) and items:
            phone = (items[0].get("ownerJid") or "").split("@")[0]
    return state, phone


class _WaBase(_ClientScopedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        url, key = _cfg()
        self.configured = bool(url and key)

    def _down(self, exc=None):
        if exc:
            log.warning("evolution api error: %s", exc)
        return Response({"detail": "WhatsApp-сервер недоступен, попробуйте позже."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    def _not_configured(self):
        return Response({"configured": False, "state": "none", "phone": ""})

    def _no_addon(self):
        return Response({"detail": "WhatsApp не подключён в тарифе. Включите в Маркетплейсе.", "code": "addon_required"}, status=status.HTTP_402_PAYMENT_REQUIRED)


class WhatsAppStatusAPIView(_WaBase):
    def get(self, request, *args, **kwargs):
        company = _company(request)
        perms = {"addon": has_addon(company), "can_manage": _is_owner_like(request.user), "can_send": can_send(request.user), "can_edit": can_edit(request.user)}
        if not self.configured:
            return Response({"configured": False, "state": "none", "phone": "", **perms})
        if not perms["addon"]:
            return Response({"configured": True, "state": "none", "phone": "", **perms})
        try:
            state, phone = _state(company)
        except requests.RequestException as e:
            return self._down(e)
        return Response({"configured": True, "state": state, "phone": phone, **perms})


class WhatsAppConnectAPIView(_WaBase):
    def post(self, request, *args, **kwargs):
        if not self.configured:
            return self._not_configured()
        if not _is_owner_like(request.user):
            return Response({"detail": "Подключать WhatsApp может только владелец."}, status=status.HTTP_403_FORBIDDEN)
        company = _company(request)
        if not has_addon(company):
            return self._no_addon()
        name = _instance(company)
        try:
            state, phone = _state(company)
            if state == "open":
                return Response({"state": "open", "phone": phone, "qr": ""})
            if state == "none":
                code, data = _evo("POST", "/instance/create", json={"instanceName": name, "integration": "WHATSAPP-BAILEYS", "qrcode": True})
                if code >= 400:
                    log.warning("evolution create %s: %s %s", name, code, data)
                    return self._down()
                qr = ((data or {}).get("qrcode") or {}).get("base64") or ""
            else:
                code, data = _evo("GET", f"/instance/connect/{name}")
                if code >= 400:
                    return self._down()
                qr = (data or {}).get("base64") or ""
        except requests.RequestException as e:
            return self._down(e)
        return Response({"state": "connecting", "phone": "", "qr": qr})


class WhatsAppLogoutAPIView(_WaBase):
    def post(self, request, *args, **kwargs):
        if not self.configured:
            return self._not_configured()
        if not _is_owner_like(request.user):
            return Response({"detail": "Отключать WhatsApp может только владелец."}, status=status.HTTP_403_FORBIDDEN)
        name = _instance(_company(request))
        try:
            _evo("DELETE", f"/instance/logout/{name}")
            _evo("DELETE", f"/instance/delete/{name}")
        except requests.RequestException as e:
            return self._down(e)
        return Response({"state": "none", "phone": ""})


class WhatsAppSendAPIView(_WaBase):
    def post(self, request, *args, **kwargs):
        if not self.configured:
            return Response({"detail": "WhatsApp не настроен на сервере."}, status=status.HTTP_400_BAD_REQUEST)
        text = str(request.data.get("text") or "").strip()
        client_id = request.data.get("client")
        if not client_id or not text:
            return Response({"detail": "Нужны client и text."}, status=status.HTTP_400_BAD_REQUEST)
        if len(text) > MAX_TEXT:
            return Response({"detail": f"Текст длиннее {MAX_TEXT} символов."}, status=status.HTTP_400_BAD_REQUEST)
        if not can_send(request.user):
            return Response({"detail": "Нет доступа к отправке WhatsApp. Попросите владельца.", "code": "no_access"}, status=status.HTTP_403_FORBIDDEN)
        if not has_addon(_company(request)):
            return self._no_addon()
        client = self._client(request, client_id)
        number = normalize_phone(client.phone or "")
        if not number:
            return Response({"detail": "У клиента нет правильного номера телефона."}, status=status.HTTP_400_BAD_REQUEST)
        company = _company(request)
        lock = f"wa-send-{company.id}"
        if not cache.add(lock, 1, MIN_INTERVAL_SEC):
            return Response({"detail": "Слишком часто, подождите пару секунд."}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        try:
            code, data = _evo("POST", f"/message/sendText/{_instance(company)}", json={"number": number, "text": text, "delay": 1200})
        except requests.RequestException as e:
            return self._down(e)
        if code == 404:
            return Response({"detail": "WhatsApp не подключён. Подключите номер в настройках."}, status=status.HTTP_409_CONFLICT)
        if code >= 400:
            msg = str((data or {}).get("response") or data)[:300]
            if "exists" in msg and "false" in msg:
                return Response({"detail": "Этот номер не зарегистрирован в WhatsApp."}, status=status.HTTP_400_BAD_REQUEST)
            log.warning("evolution send %s: %s %s", company.id, code, msg)
            return Response({"detail": "Не удалось отправить."}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"ok": True, "id": ((data or {}).get("key") or {}).get("id") or ""})


class WhatsAppSettingsAPIView(_WaBase):
    """Шаблоны и прочее: читать — все сотрудники компании, менять — владелец/админ или «полный доступ»."""

    def _row(self, company, lock=False):
        qs = CompanyAppSettings.objects.filter(company=company, branch__isnull=True, app="whatsapp")
        if lock:
            qs = qs.select_for_update()
        return qs.first()

    def get(self, request, *args, **kwargs):
        row = self._row(_company(request))
        data = {k: v for k, v in ((row.settings if row else {}) or {}).items() if k in SETTINGS_KEYS}
        return Response({"settings": data, "can_edit": can_edit(request.user)})

    def patch(self, request, *args, **kwargs):
        if not can_edit(request.user):
            return Response({"detail": "Менять шаблоны WhatsApp может владелец или сотрудник с полным доступом.", "code": "no_access"}, status=status.HTTP_403_FORBIDDEN)
        patch = {k: v for k, v in (request.data or {}).items() if k in SETTINGS_KEYS}
        import json
        if len(json.dumps(patch, ensure_ascii=False)) > 60000:
            return Response({"detail": "Слишком большие настройки."}, status=status.HTTP_400_BAD_REQUEST)
        from django.db import transaction
        company = _company(request)
        with transaction.atomic():
            row = self._row(company, lock=True) or CompanyAppSettings(company=company, branch=None, app="whatsapp", settings={})
            cur = dict(row.settings or {})
            cur.update(patch)
            row.settings = cur
            row.version = (row.version or 0) + 1
            row.updated_by = request.user
            row.save()
        return Response({"settings": {k: v for k, v in cur.items() if k in SETTINGS_KEYS}, "can_edit": True})
