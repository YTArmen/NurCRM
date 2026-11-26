"""rps2611 (26.11.2026): ТЗ Бекназара «статус ремонта на витрине».

  PUT/GET api/main/repair-orders/<external_id 32 hex>/   — касса шлёт ВСЁ состояние квитанции (upsert, без денег)
  POST    api/public/companies/<slug>/repair-status/      — клиент: {code, phone_last4} → статус без личных данных

Деньги ремонта (предоплата/оплата) касса проводит своими путями; здесь prepay — только цифра.
Признак кассе: capabilities.features → "repair_status_public" (когда у компании подключён «Ремонт»).
"""
import hashlib
import re
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.repair.models import RepairOrder
from apps.repair.perms import addon_on, company_of, err, guard
from apps.repair.services import Z, hist, lock_company, money, next_number, norm_serial

EXT_DEVICE = "kassa-ext"
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
TITLES = {
    "ru": {"accepted": "Принят", "in_work": "В работе", "ready": "Готов", "issued": "Выдан", "cancelled": "Отменён"},
    "ky": {"accepted": "Кабыл алынды", "in_work": "Иштелип жатат", "ready": "Даяр", "issued": "Берилди", "cancelled": "Жокко чыгарылды"},
    "en": {"accepted": "Accepted", "in_work": "In progress", "ready": "Ready", "issued": "Picked up", "cancelled": "Cancelled"},
    "uz": {"accepted": "Qabul qilindi", "in_work": "Ishlanmoqda", "ready": "Tayyor", "issued": "Berildi", "cancelled": "Bekor qilindi"},
    "kk": {"accepted": "Қабылданды", "in_work": "Жөнделуде", "ready": "Дайын", "issued": "Берілді", "cancelled": "Бас тартылды"},
}
# поле кассы → поле модели (строки)
TEXT_MAP = {"device": "device", "client_name": "customer_name", "client_phone": "customer_phone", "serial": "serial",
            "problem": "problem", "accessories": "accessories", "note": "condition_note"}


def public_enabled(company):
    return bool(company) and addon_on(company, "repair")


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def _ready_at(o):
    for h in reversed(o.history or []):
        if h.get("action") == "ready":
            return h.get("at")
    return None


def ext_payload(o):
    return {
        "id": str(o.id), "external_id": o.local_id, "number": o.number, "code": o.local_number or f"Р-{o.number}",
        "status": o.status, "device": o.device, "client_name": o.customer_name,
        "due_date": o.due_date.isoformat() if o.due_date else None, "ready_at": _ready_at(o),
        "estimate": format(o.estimated_price, "f"), "prepay": format(o.prepaid, "f"),
        "final_price": format(o.final_price, "f") if o.final_price is not None else None,
        "accepted_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
        "public_url": f"https://market.nurcrm.kg/catalog/{o.company.slug}/repair?code={int(_digits(o.local_number) or o.number)}"
        if getattr(o.company, "slug", None) else None,
    }


READY_TEXT = {
    "ru": "Здравствуйте{name}! Ваше устройство «{device}» ({code}) готово — можно забирать.{due} «{shop}»",
    "ky": "Саламатсызбы{name}! «{device}» ({code}) түзмөгүңүз даяр — алып кетсеңиз болот.{due} «{shop}»",
    "en": "Hello{name}! Your device {device} ({code}) is ready for pickup.{due} {shop}",
    "tr": "Merhaba{name}! {device} ({code}) cihazınız hazır, teslim alabilirsiniz.{due} {shop}",
    "uz": "Assalomu alaykum{name}! «{device}» ({code}) qurilmangiz tayyor — olib ketishingiz mumkin.{due} «{shop}»",
}
DUE_TEXT = {"ru": " К оплате: {a} сом.", "ky": " Төлөөгө: {a} сом.", "en": " To pay: {a} KGS.", "tr": " Ödenecek: {a} KGS.", "uz": " To'lov: {a} som."}


def _find_client(o):
    from apps.main.models import Client

    if o.client_id and getattr(o.client, "telegram_chat_id", None):
        return o.client
    tail = _digits(o.customer_phone)[-9:]
    if len(tail) < 9:
        return None
    qs = Client.objects.filter(company_id=o.company_id).filter(Q(phone_normalized__endswith=tail) | Q(phone__contains=tail[-6:])).exclude(telegram_chat_id__isnull=True).exclude(telegram_chat_id="")
    for c in qs[:50]:
        if _digits(c.phone)[-9:] == tail:
            return c
    return None


def notify_ready(o):
    """rps2611: сообщение 'готово' клиенту в Telegram-бот магазина; один раз на заказ, только с согласием и чатом."""
    try:
        from apps.main.telegram_bot.services.customer_notify import send_to_customer

        client = _find_client(o)
        if client is None:
            return "no_chat"
        price = o.final_price if o.final_price is not None else o.estimated_price
        due = max((price or Z) - (o.prepaid or Z) + (o.refunded or Z), Z)
        name = (o.customer_name or "").strip().split(" ")[0]
        amount = f"{due:,.0f}".replace(",", " ")
        text = {lg: tpl.format(name=f", {name}" if name else "", device=o.device, code=o.local_number or f"Р-{o.number}",
                               due=DUE_TEXT[lg].format(a=amount) if due > 0 else "", shop=getattr(o.company, "name", "") or "")
                for lg, tpl in READY_TEXT.items()}
        row = send_to_customer(o.company, client, "other", text=text, dedupe_key=f"repair:{o.id}:ready")
        return str(getattr(row, "status", "") or "queued")
    except Exception:
        import logging
        logging.getLogger(__name__).exception("repair notify_ready")
        return "error"


def _snapshot(o):
    return (o.status, o.device, o.customer_name, o.customer_phone, o.serial, o.problem, o.accessories, o.condition_note,
            o.local_number, o.due_date, o.estimated_price, o.prepaid, o.final_price)


class RepairExternalAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _find(self, company, external_id):
        return RepairOrder.objects.filter(company=company, device_id=EXT_DEVICE, local_id=external_id.lower()).first()

    def get(self, request, external_id):
        bad = guard(request, "repair", "repair.view")
        if bad:
            return bad
        o = self._find(company_of(request.user), external_id)
        if o is None:
            return err("not_found", "Заказ не найден.", 404)
        return Response(ext_payload(o))

    def put(self, request, external_id):
        bad = guard(request, "repair", "repair.manage")
        if bad:
            return bad
        if not _HEX32.match(external_id or ""):
            return err("validation_error", "external_id — 32 hex.", fields={"external_id": ["32 hex."]})
        d = request.data if isinstance(request.data, dict) else {}
        body_ext = str(d.get("external_id") or external_id)
        if body_ext.lower() != external_id.lower():
            return err("external_id_mismatch", "external_id в теле и в адресе разные.")
        st = str(d.get("status") or "accepted")
        if st not in RepairOrder.STATUSES:
            return err("validation_error", "status: accepted|in_work|ready|issued|cancelled.", fields={"status": ["Неверный статус."]})
        company = company_of(request.user)
        with transaction.atomic():
            lock_company(company)
            o = self._find(company, external_id)
            created = o is None
            if created:
                if not str(d.get("device") or "").strip():
                    return err("validation_error", "Укажите устройство (device).", fields={"device": ["Обязательно."]})
                o = RepairOrder(company=company, created_by=request.user, device_id=EXT_DEVICE, local_id=external_id.lower(),
                                number=next_number(RepairOrder, company))
                before = None
            else:
                o = RepairOrder.objects.select_for_update().get(pk=o.pk)
                before = _snapshot(o)
            for src, dst in TEXT_MAP.items():
                if src in d:
                    setattr(o, dst, str(d.get(src) or "").strip()[:4000 if dst in ("problem", "condition_note") else 255])
            o.serial_norm = norm_serial(o.serial)
            code = str(d.get("code") or "").strip()[:32]
            if code:
                o.local_number = code
            elif d.get("number") not in (None, "") and not o.local_number:
                o.local_number = f"Р-{_digits(d.get('number')).zfill(4)}"
            if "due_date" in d:
                o.due_date = parse_date(str(d.get("due_date"))[:10]) if d.get("due_date") else None
            for src, dst in (("estimate", "estimated_price"), ("prepay", "prepaid")):
                if src in d:
                    v = money(d.get(src) or 0, allow_zero=True)
                    if v is None:
                        return err("validation_error", f"{src}: сумма ≥ 0.", fields={src: ["Сумма ≥ 0."]})
                    setattr(o, dst, v)
            if "final_price" in d:
                if d.get("final_price") in (None, ""):
                    o.final_price = None
                else:
                    v = money(d.get("final_price"), allow_zero=True)
                    if v is None:
                        return err("validation_error", "final_price: сумма ≥ 0.", fields={"final_price": ["Сумма ≥ 0."]})
                    o.final_price = v
            old_status = o.status if not created else None
            o.status = st
            now = timezone.now()
            if before is not None and _snapshot(o) == before:
                return Response({**ext_payload(o), "unchanged": True})
            if created:
                hist(o, request.user, "kassa_created", code=o.local_number)
            if old_status != st:
                hist(o, request.user, st, source="kassa")
                if st == "ready" and d.get("ready_at"):
                    o.history[-1]["at"] = str(d.get("ready_at"))
                if st == "issued":
                    o.issued_at = now
                if st == "cancelled":
                    o.cancelled_at = now
            elif not created:
                hist(o, request.user, "kassa_update")
            o.save()
            acc = parse_datetime(str(d.get("accepted_at") or "")) if created and d.get("accepted_at") else None
            if acc is not None:
                RepairOrder.objects.filter(pk=o.pk).update(created_at=acc)
                o.created_at = acc
        try:
            from apps.repair.views_repair import _feed

            _feed(o, request.user)
        except Exception:
            pass
        notified = notify_ready(o) if (st == "ready" and old_status != "ready") else None
        return Response({**ext_payload(o), "created": created, **({"notified": notified} if notified else {})}, status=201 if created else 200)


def _client_ip(request):
    xf = request.META.get("HTTP_CF_CONNECTING_IP") or (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0]
    return (xf or request.META.get("REMOTE_ADDR") or "").strip()


def _hit(key, limit, seconds):
    n = cache.get(key) or 0
    if n >= limit:
        return False
    if n == 0:
        cache.set(key, 1, seconds)
    else:
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, n + 1, seconds)
    return True


def _lang(request):
    al = (request.headers.get("Accept-Language") or "ru").lower()
    for part in al.split(","):
        code = part.strip()[:2]
        if code in TITLES:
            return code
    return "ru"


class PublicRepairStatusAPIView(APIView):
    """Одинаковый 404 {code: not_found} на любую ошибку: нет кода, не те цифры, старше 90 дней после выдачи/отмены,
    функция у компании выключена. Лимиты: 20 попыток / 10 мин с IP, 5 / 10 мин на один код."""
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    NF = {"code": "not_found", "detail": "Заказ не найден."}

    def post(self, request, slug):
        from apps.main.showcase.services import resolve_public_company

        d = request.data if isinstance(request.data, dict) else {}
        code_n = _digits(d.get("code"))[:9]
        last4 = _digits(d.get("phone_last4"))
        ip = hashlib.sha1(_client_ip(request).encode()).hexdigest()[:16]
        if not _hit(f"rps:ip:{ip}", 20, 600) or (code_n and not _hit(f"rps:code:{slug}:{code_n}", 5, 600)):
            r = Response({"code": "rate_limited", "detail": "Слишком много попыток. Попробуйте через 10 минут."}, status=429)
            r["Retry-After"] = "600"
            return r
        try:
            company = resolve_public_company(slug, request, redirect_get=False)
        except Exception:
            return Response(self.NF, status=404)
        if not public_enabled(company) or not code_n or len(last4) != 4:
            return Response(self.NF, status=404)
        n = int(code_n)
        qs = RepairOrder.objects.filter(company=company).filter(
            Q(device_id=EXT_DEVICE, local_number__endswith=str(n)) | Q(number=n)).order_by("-created_at")[:20]
        old = timezone.now() - timedelta(days=90)
        o = None
        for c in qs:
            num_ok = (int(_digits(c.local_number) or 0) == n) if c.device_id == EXT_DEVICE else c.number == n
            if num_ok and _digits(c.customer_phone)[-4:] == last4 and len(_digits(c.customer_phone)) >= 4:
                o = c
                break
        if o is None:
            return Response(self.NF, status=404)
        if o.status in ("issued", "cancelled"):
            done = o.issued_at or o.cancelled_at or o.updated_at
            if done and done < old:
                return Response(self.NF, status=404)
        lang = _lang(request)
        due = None
        if o.status == "ready":
            price = o.final_price if o.final_price is not None else o.estimated_price
            due = max((price or Z) - (o.prepaid or Z) + (o.refunded or Z), Z)
        return Response({
            "code": o.local_number or f"Р-{o.number}", "status": o.status, "status_title": TITLES[lang].get(o.status, o.status),
            "device": o.device, "due_date": o.due_date.isoformat() if o.due_date else None, "ready_at": _ready_at(o),
            "amount_due": format(due, "f") if due is not None else None, "currency": "KGS",
            "shop_name": company.name, "shop_phone": company.phones_howcase or company.phone or None,
            "shop_whatsapp": company.whatsapp_phone or None,
            "updated_at": timezone.localtime(o.updated_at).isoformat() if o.updated_at else None,
        })
