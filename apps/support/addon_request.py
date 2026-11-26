"""
Маркетплейс функций (веб-админка маркета): заявка «Подключить» уходит в Telegram-группу поддержки.
Оплаты онлайн пока нет — поддержка перезванивает, принимает оплату и включает функцию одной кнопкой из сообщения.

POST /api/support/addon-requests/   (с токеном)
{ "item": "kassa-loyalty", "title": "Программа лояльности", "price": "1 500 сом", "phone": "+996…", "comment"? }
→ 201 {"ok": true}
→ 400 {"code": "validation_error", "fields": {...}}       нет функции / телефон короче 9 цифр
→ 429 {"code": "too_many_requests"}                        больше 5 заявок от пользователя за 10 минут
→ 503 {"code": "not_delivered", "phone": "+996 559 900 556"} бот не настроен или Telegram не принял

GET  /api/support/addon-requests/   (с токеном) → {"requests": [{"item", "status": "sent"|"enabled", "at"}]} — заявки компании,
     чтобы маркетплейс показывал «Заявка отправлена».

GET  /api/support/addon-requests/approve/?t=<подпись>  — страница для поддержки: компания, функция, срок; кнопка «Включить».
POST /api/support/addon-requests/approve/ (t, period=month|year|forever) — включает платную функцию компании (CompanyAddon,
     source="support") и отмечает заявку «enabled». Ссылка подписана, живёт 14 дней; GET ничего не меняет (Telegram и
     браузеры открывают ссылки для предпросмотра).

Бот и группа — ADDON_REQUEST_TELEGRAM_BOT_TOKEN / ADDON_REQUEST_TELEGRAM_CHAT_ID, если заданы, иначе SUPPORT_TELEGRAM_*.
"""
import datetime
import re
import uuid

from django.core import signing
from django.http import HttpResponse
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.support.bot import _send_raw, _setting, esc, get_support_config
from apps.support.forgot import SUPPORT_PHONE, _field, _hit
from apps.support.views import _client_ip, _user_company, _user_login

USER_LIMIT = 5
APP = "nur-marketplace"
SALT = "nurcrm.addon-approve.v1"
LINK_DAYS = 14
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,63}$")
API_BASE = "https://app.nurcrm.kg/api"


def _store(company, create=False):
    from apps.users.models import CompanyAppSettings
    obj = CompanyAppSettings.objects.filter(company=company, branch__isnull=True, app=APP).first()
    if obj is None and create:
        obj = CompanyAppSettings(company=company, app=APP, settings={}, secrets={})
    return obj


def _requests(company):
    obj = _store(company)
    data = (obj.settings or {}).get("requests") if obj is not None else None
    return data if isinstance(data, list) else []


def _save_requests(company, items, user=None):
    obj = _store(company, create=True)
    obj.settings = {**(obj.settings or {}), "requests": items[-50:]}
    obj.version = (obj.version or 0) + 1
    if user is not None:
        obj.updated_by = user
    obj.save()


class AddonRequestAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = _user_company(request.user)
        if company is None:
            return Response({"requests": []})
        return Response({"requests": [{"item": r.get("item"), "status": r.get("status", "sent"), "at": r.get("at")}
                                      for r in _requests(company)]})

    def post(self, request, *args, **kwargs):
        data = request.data
        item = _field(data, "item", 64)
        title = _field(data, "title", 160) or item
        phone = _field(data, "phone", 40)
        errors = {}
        if not item:
            errors["item"] = ["Не выбрана функция."]
        if len(re.sub(r"\D", "", phone)) < 9:
            errors["phone"] = ["Укажите телефон (не меньше 9 цифр)."]
        if errors:
            return Response({"code": "validation_error", "detail": "Проверьте поля.", "fields": errors}, status=400)

        user = request.user
        if _hit(f"addonreq:user:{user.pk}") > USER_LIMIT:
            return Response({"code": "too_many_requests", "detail": "Слишком много заявок. Попробуйте через 10 минут или позвоните.", "phone": SUPPORT_PHONE}, status=429)

        company = _user_company(user)
        rid = uuid.uuid4().hex[:12]
        lines = [
            f"🛒 <b>Заявка из маркетплейса</b> · {esc(title)}",
            f"Функция: <code>{esc(item)}</code>",
        ]
        price = _field(data, "price", 80)
        if price:
            lines.append(f"Цена: {esc(price)}")
        if company is not None:
            lines.append(f"Компания: <b>{esc(getattr(company, 'name', '') or company)}</b> (id {esc(company.pk)})")
        lines.append(f"Логин: {esc(_user_login(user))}")
        lines.append(f"Телефон: <b>{esc(phone)}</b>")
        comment = _field(data, "comment", 1000)
        if comment:
            lines.append(f"Комментарий: {esc(comment)}")
        lines.append(f"IP: {esc(_client_ip(request))}")
        enable = company is not None and CODE_RE.match(item) and item != "constructor"
        if enable:
            token = signing.dumps({"c": str(company.pk), "i": item, "r": rid}, salt=SALT, compress=True)
            lines.append(f'<i>Перезвоните, примите оплату, затем:</i> <a href="{API_BASE}/support/addon-requests/approve/?t={token}">✅ Включить функцию</a>')
        else:
            lines.append("<i>Перезвоните, примите оплату и включите функцию.</i>")

        cfg = get_support_config()
        token_bot = _setting("ADDON_REQUEST_TELEGRAM_BOT_TOKEN") or cfg.token
        chat_id = _setting("ADDON_REQUEST_TELEGRAM_CHAT_ID") or cfg.chat_id
        if not (token_bot and chat_id and _send_raw(token_bot, chat_id, "\n".join(lines))):
            return Response({"code": "not_delivered", "detail": f"Заявка не отправлена. Позвоните в поддержку: {SUPPORT_PHONE}", "phone": SUPPORT_PHONE}, status=503)
        if company is not None:
            items = [r for r in _requests(company) if not (r.get("item") == item and r.get("status", "sent") == "sent")]
            items.append({"id": rid, "item": item, "title": title, "status": "sent", "at": timezone.now().isoformat(), "by": str(user.pk)})
            _save_requests(company, items, user)
        return Response({"ok": True}, status=201)


# ----------------------------------------------------------------------------- «Включить» для поддержки

def _page(title, body, status=200):
    html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{escape(title)}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f5f7;font:15px/1.5 system-ui,sans-serif;color:#111827}}
.c{{width:min(440px,92vw);background:#fff;border-radius:20px;padding:28px;box-shadow:0 20px 50px rgba(11,42,60,.12)}}
h1{{font-size:20px;margin:0 0 6px}}.m{{color:#667085;font-size:13px}}label{{display:flex;gap:8px;align-items:center;padding:10px 12px;border:1px solid #e5e7eb;border-radius:12px;margin:6px 0;cursor:pointer}}
button{{width:100%;margin-top:14px;min-height:48px;border:0;border-radius:12px;background:#ffd600;color:#0b2a3c;font-weight:800;font-size:15px;cursor:pointer}}
.ok{{color:#0e7a3a;font-weight:700}}.bad{{color:#b42318;font-weight:700}}</style></head><body><div class="c">{body}</div></body></html>"""
    return HttpResponse(html, status=status, content_type="text/html; charset=utf-8")


def _read(token):
    try:
        data = signing.loads(token or "", salt=SALT, max_age=LINK_DAYS * 86400)
    except signing.BadSignature:
        return None, None
    from apps.users.models import Company
    company = Company.objects.filter(pk=data.get("c")).first()
    if company is None or not CODE_RE.match(str(data.get("i") or "")):
        return None, None
    return company, data


@method_decorator(csrf_exempt, name="dispatch")
class AddonApproveView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        company, data = _read(request.query_params.get("t"))
        if company is None:
            return _page("Ссылка недействительна", "<h1>Ссылка недействительна</h1><p class='m'>Срок ссылки 14 дней или она повреждена. Включите функцию в админке.</p>", 400)
        from apps.users.models import CompanyAddon
        cur = CompanyAddon.objects.filter(company=company, code=data["i"]).first()
        state = ("<p class='ok'>Уже включена" + (f" до {cur.until:%d.%m.%Y}" if cur.until else " бессрочно") + "</p>") if cur and cur.is_effective() else ""
        body = (f"<h1>Включить функцию</h1><p class='m'>Компания: <b>{escape(company.name)}</b><br>Функция: <b>{escape(data['i'])}</b></p>{state}"
                f"<form method='post'><input type='hidden' name='t' value='{escape(request.query_params.get('t'))}'>"
                "<label><input type='radio' name='period' value='month' checked> На месяц</label>"
                "<label><input type='radio' name='period' value='year'> На год</label>"
                "<label><input type='radio' name='period' value='forever'> Бессрочно (разовая покупка)</label>"
                "<button type='submit'>Включить</button></form><p class='m'>Включайте только после оплаты.</p>")
        return _page("Включить функцию", body)

    def post(self, request):
        token = request.data.get("t") or request.POST.get("t")
        company, data = _read(token)
        if company is None:
            return _page("Ссылка недействительна", "<h1>Ссылка недействительна</h1>", 400)
        period = request.data.get("period") or request.POST.get("period") or "month"
        today = timezone.localdate()
        until = None if period == "forever" else today + datetime.timedelta(days=366 if period == "year" else 31)
        from apps.users.models import CompanyAddon
        has_source = any(f.name == "source" for f in CompanyAddon._meta.get_fields())
        addon, _ = CompanyAddon.objects.get_or_create(company=company, code=data["i"])
        addon.active = True
        if addon.until and until and addon.until > today:
            until = addon.until + (until - today)  # продление от текущего конца
        addon.until = until
        if has_source:
            addon.source = "support"  # поле есть на боевом сервере (ТЗ Н-32), в старых копиях — нет
        addon.note = (f"маркетплейс: заявка {data.get('r', '')}")[:255]
        addon.save()
        items = _requests(company)
        for r in items:
            if r.get("item") == data["i"] and r.get("status", "sent") == "sent":
                r["status"] = "enabled"
                r["enabled_at"] = timezone.now().isoformat()
        _save_requests(company, items)
        when = f"до {until:%d.%m.%Y}" if until else "бессрочно"
        return _page("Включено", f"<h1>Готово</h1><p class='ok'>Функция «{escape(data['i'])}» включена {when}.</p><p class='m'>Компания: {escape(company.name)}. В маркетплейсе клиента она сразу станет «Подключено».</p>")
