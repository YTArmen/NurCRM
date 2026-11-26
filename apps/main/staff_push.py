"""П2-2 (14.11.2026, p2214): push сотрудникам «Требует внимания».

POST   devices/push-token/ {token, platform: android|ios|web|desktop, lang?}  — регистрация (повтор — обновление)
DELETE devices/push-token/ {token}                                        — снять (выход из приложения)
События (кому — владельцу/админам компании):
  return_without_sale — возврат без чека (сразу);
  shift_open_long     — смена открыта больше 23 часов (команда staff_attention, раз в час);
  low_stock           — товары ниже минимального остатка (команда staff_attention, раз в день в 09:00 по Бишкеку).
Доставка — Expo Push (как у приложения покупателей). Токен Expo вида ExponentPushToken[…]; иные токены хранятся,
но не отправляются (для них сервер-отправитель пока не подключён) — в ответе delivery: "expo" | "stored".
Каждое событие ещё пишется в ленту main/changes/ типом attention (data.kind) — касса без push видит его там.
"""
import logging
import re

from django.db import transaction
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger("nurcrm.staff_push")
EXPO_RE = re.compile(r"^(ExponentPushToken|ExpoPushToken)\[[^\]]{8,200}\]$")
PLATFORMS = ("android", "ios", "web", "desktop")
TEXTS = {
    "return_without_sale": {"ru": ("Возврат без чека", "{who}: {amount} сом, причина — {reason}"),
                            "ky": ("Чексиз кайтаруу", "{who}: {amount} сом, себеби — {reason}")},
    "shift_open_long": {"ru": ("Смена открыта больше суток", "{who}, касса «{cashbox}», с {since}"),
                        "ky": ("Смена бир суткадан ашык ачык", "{who}, «{cashbox}» кассасы, {since} бери")},
    "low_stock": {"ru": ("Заканчиваются товары", "Ниже минимума: {n}. Например: {names}"),
                  "ky": ("Товарлар түгөнүп баратат", "Минимумдан аз: {n}. Мисалы: {names}")},
}


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


class StaffPushTokenAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from apps.main.models import StaffPushToken

        d = request.data if hasattr(request.data, "get") else {}
        token = str(d.get("token") or "").strip()
        platform = str(d.get("platform") or "android").strip().lower()
        lang = str(d.get("lang") or "ru").strip().lower()[:4]
        if not token or len(token) > 255:
            return _err("validation_error", "token: от 1 до 255 символов.", fields={"token": ["Обязательно."]})
        if platform not in PLATFORMS:
            return _err("validation_error", "platform: " + ", ".join(PLATFORMS) + ".", fields={"platform": list(PLATFORMS)})
        company = _company(request.user)
        with transaction.atomic():
            obj, created = StaffPushToken.objects.update_or_create(
                token=token, defaults={"company": company, "user": request.user, "platform": platform, "lang": lang})
        return Response({"token": token, "platform": platform, "lang": lang,
                         "delivery": "expo" if EXPO_RE.match(token) else "stored"}, status=201 if created else 200)

    def delete(self, request):
        from apps.main.models import StaffPushToken

        d = request.data if hasattr(request.data, "get") else {}
        token = str(d.get("token") or request.query_params.get("token") or "").strip()
        StaffPushToken.objects.filter(token=token, user=request.user).delete()
        return Response(status=204)


def _recipients(company):
    from apps.main.models import StaffPushToken
    from django.db.models import Q

    return list(StaffPushToken.objects.filter(company=company).filter(
        Q(user__role__in=("owner", "admin")) | Q(user_id=getattr(company, "owner_id", None))).select_related("user"))


def notify(company, kind, fmt, data=None):
    """Пишет событие attention в ленту и шлёт push владельцу/админам. Никогда не бросает исключений."""
    try:
        from apps.main.change_feed import record_change

        record_change(company.pk, "attention", entity="attention", entity_id=kind, data={"kind": kind, **(fmt or {}), **(data or {})})
    except Exception:  # noqa: BLE001
        logger.exception("attention feed failed")
    try:
        tokens = [t for t in _recipients(company) if EXPO_RE.match(t.token)]
        if not tokens:
            return 0
        msgs = []
        for t in tokens:
            title, body = TEXTS[kind].get(t.lang) or TEXTS[kind]["ru"]
            try:
                body = body.format(**fmt)
            except (KeyError, IndexError):
                pass
            msgs.append({"to": t.token, "title": title, "body": body, "sound": "default",
                         "data": {"type": "attention", "kind": kind, **(data or {})}})

        def _send():
            try:
                import httpx

                from apps.clientapp.push import EXPO_CHUNK, EXPO_PUSH_URL

                with httpx.Client(timeout=10) as cli:
                    for i in range(0, len(msgs), EXPO_CHUNK):
                        cli.post(EXPO_PUSH_URL, json=msgs[i:i + EXPO_CHUNK])
            except Exception:  # noqa: BLE001
                logger.exception("staff push send failed")

        transaction.on_commit(_send)
        return len(msgs)
    except Exception:  # noqa: BLE001
        logger.exception("staff push failed")
        return 0
