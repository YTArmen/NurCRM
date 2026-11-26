"""П2-6 (14.11.2026, p2614): поддержка — часы работы, обращения со статусом, оценка ответа.

GET  api/public/support/info/              — без входа: {hours{ru,ky,en,tr,uz}, first_reply{…}, channels[{type,value,label}], updated_at}
POST api/support/requests/                 — {client_request_id, text, app?, version?, contact?} → 201 (повтор id → 200 replayed)
                                             сервер сам пересылает обращение в группу поддержки (Telegram) с номером #N
GET  api/support/requests/?client_request_id=…  → одно {id, number, status, answer, answered_at, …}; без параметра — мои последние 50
POST api/support/requests/<id>/rating/     — {stars: 1..5, comment?} (только автор, только после answered/closed)
Статус меняет команда в группе:  /req <N> work | answered <текст ответа> | closed
Смена статуса пишется в ленту main/changes/ типом support_request_changed (кассе не надо опрашивать).
Часы и срок ответа задаются командой: manage.py support_info set --hours-ru "Пн–Сб 9:00–21:00" --reply-ru "до 30 минут"
"""
import json
from datetime import timedelta
import os
from pathlib import Path

from django.db import transaction
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

INFO_FILE = Path(os.environ.get("NUR_SUPPORT_INFO_FILE", "/home/nur/var/support_info.json"))
LANGS = ("ru", "ky", "en", "tr", "uz")
DEFAULT_INFO = {
    "hours": {"ru": "Ежедневно 9:00–21:00", "ky": "Күн сайын 9:00–21:00", "en": "Daily 9:00–21:00",
              "tr": "Her gün 9:00–21:00", "uz": "Har kuni 9:00–21:00"},
    "first_reply": {"ru": "в течение 1 часа в рабочее время", "ky": "иш убактысында 1 сааттын ичинде",
                    "en": "within 1 hour during working hours", "tr": "mesai saatlerinde 1 saat içinde",
                    "uz": "ish vaqtida 1 soat ichida"},
    "channels": [],
}


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def read_info():
    try:
        data = json.loads(INFO_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        data = {}
    out = {k: data.get(k) or v for k, v in DEFAULT_INFO.items()}
    for k in ("hours", "first_reply"):
        ru = out[k].get("ru") or DEFAULT_INFO[k]["ru"]
        out[k] = {lang: out[k].get(lang) or ru for lang in LANGS}
    out["updated_at"] = data.get("updated_at")
    return out


def write_info(data):
    INFO_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = INFO_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, INFO_FILE)


class PublicSupportInfoAPIView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        resp = Response(read_info())
        resp["Cache-Control"] = "public, max-age=300"
        return resp


def payload(r):
    return {"id": r.pk, "number": r.pk, "client_request_id": r.client_request_id, "status": r.status,
            "answer": r.answer or None, "answered_at": r.answered_at.isoformat() if r.answered_at else None,
            "closed_at": r.closed_at.isoformat() if r.closed_at else None, "rating": r.rating,
            "created_at": r.created_at.isoformat() if r.created_at else None}


def _forward(r):
    from apps.support.bot import _send_raw, esc, get_support_config

    cfg = get_support_config()
    if not (cfg.token and cfg.chat_id):
        return False
    u = r.user
    who = (f"{getattr(u, 'first_name', '') or ''} {getattr(u, 'last_name', '') or ''}".strip() or getattr(u, "email", "") or "—") if u else "—"
    text = (f"🆘 <b>Обращение #{r.pk}</b>\nМагазин: {esc(getattr(r.company, 'name', ''))}\nКто: {esc(who)}"
            f"{(' · ' + esc(r.contact)) if r.contact else ''}\nПрограмма: {esc(r.app)} {esc(r.version)}\n\n{esc(r.text[:3000])}\n\n"
            f"Ответить: <code>/req {r.pk} answered текст</code> · в работу: <code>/req {r.pk} work</code> · закрыть: <code>/req {r.pk} closed</code>")
    return _send_raw(cfg.token, cfg.chat_id, text)


def _feed(r):
    try:
        from apps.main.change_feed import record_change

        record_change(r.company_id, "support_request_changed", actor_id=None, entity="support_request", entity_id=r.pk,
                      data={"request": r.pk, "client_request_id": r.client_request_id, "status": r.status,
                            "user": str(r.user_id) if r.user_id else None})
    except Exception:  # noqa: BLE001
        pass


class SupportRequestsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.support.models import SupportRequest

        qs = SupportRequest.objects.filter(company=_company(request.user), user=request.user)
        crid = (request.query_params.get("client_request_id") or "").strip()
        if crid:
            r = qs.filter(client_request_id=crid).first()
            if r is None:
                return _err("not_found", "Обращение не найдено.", 404)
            return Response(payload(r))
        return Response([payload(r) for r in qs[:50]])

    def post(self, request):
        from apps.support.models import SupportRequest

        d = request.data if hasattr(request.data, "get") else {}
        crid = str(d.get("client_request_id") or "").strip()
        text = str(d.get("text") or d.get("description") or "").strip()
        if not crid or len(crid) > 64:
            return _err("validation_error", "client_request_id: от 1 до 64 символов.", fields={"client_request_id": ["Обязательно."]})
        if not text or len(text) > 5000:
            return _err("validation_error", "text: от 1 до 5000 символов.", fields={"text": ["Обязательно."]})
        company = _company(request.user)
        old = SupportRequest.objects.filter(company=company, client_request_id=crid).first()
        if old is not None:
            return Response({**payload(old), "replayed": True}, status=200)
        recent = SupportRequest.objects.filter(user=request.user, created_at__gte=timezone.now() - timedelta(hours=1)).count()
        if recent >= 20:
            return _err("too_many_requests", "Не больше 20 обращений в час.", 429)
        with transaction.atomic():
            r = SupportRequest.objects.create(company=company, user=request.user, client_request_id=crid, text=text,
                                              app=str(d.get("app") or "")[:32], version=str(d.get("version") or "")[:32],
                                              contact=str(d.get("contact") or "")[:128])
        try:
            if _forward(r):
                SupportRequest.objects.filter(pk=r.pk).update(forwarded=True)
                r.forwarded = True
        except Exception:  # noqa: BLE001
            pass
        return Response({**payload(r), "forwarded": r.forwarded, "replayed": False}, status=201)


class SupportRequestRatingAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from apps.support.models import SupportRequest

        r = SupportRequest.objects.filter(pk=pk, company=_company(request.user), user=request.user).first()
        if r is None:
            return _err("not_found", "Обращение не найдено.", 404)
        if r.status not in ("answered", "closed"):
            return _err("not_answered", "Оценить можно после ответа поддержки.", 409)
        d = request.data if hasattr(request.data, "get") else {}
        try:
            stars = int(d.get("stars"))
        except (TypeError, ValueError):
            stars = 0
        if not 1 <= stars <= 5:
            return _err("validation_error", "stars: от 1 до 5.", fields={"stars": ["От 1 до 5."]})
        r.rating, r.rating_comment, r.rated_at = stars, str(d.get("comment") or "")[:500], timezone.now()
        r.save(update_fields=["rating", "rating_comment", "rated_at", "updated_at"])
        try:
            from apps.support.bot import _send_raw, get_support_config

            cfg = get_support_config()
            _send_raw(cfg.token, cfg.chat_id, f"{'⭐' * stars} оценка обращения #{r.pk}" + (f": {r.rating_comment}" if r.rating_comment else ""))
        except Exception:  # noqa: BLE001
            pass
        return Response(payload(r))


def handle_req_command(args):
    """/req <N> work | answered <текст> | closed — из группы поддержки."""
    from apps.support.models import SupportRequest

    if len(args) < 2 or not args[0].lstrip("#").isdigit():
        return "Формат: /req &lt;номер&gt; work | answered &lt;текст&gt; | closed"
    r = SupportRequest.objects.filter(pk=int(args[0].lstrip("#"))).first()
    if r is None:
        return "Нет такого обращения."
    act = args[1].lower()
    now = timezone.now()
    if act in ("work", "in_progress"):
        r.status = "in_progress"
    elif act in ("answered", "answer", "ответ"):
        r.status, r.answered_at = "answered", now
        if len(args) > 2:
            r.answer = " ".join(args[2:])[:4000]
    elif act in ("closed", "close"):
        r.status, r.closed_at = "closed", now
        r.answered_at = r.answered_at or now
    else:
        return "Действие: work, answered или closed."
    r.save()
    _feed(r)
    return f"Обращение #{r.pk}: {r.get_status_display()}"
