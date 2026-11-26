"""П2-5 (14.11.2026, p2514): промокоды витрины и этапы доставки заказа.

Владелец/админ или «Витрина» (can_view_showcase):
  GET/POST  showcase/promo-codes/            {code, kind: percent|fixed, value, min_total?, max_discount?, valid_from?, valid_to?,
                                              max_uses?, per_phone?, is_active?}
  PATCH/DELETE showcase/promo-codes/<id>/    (DELETE — выключить, если код уже применяли)
Покупатель (без входа):
  POST public/companies/<slug>/promo/validate/ {code, items_total, phone?}
       → {valid, code, discount, total_after, reason?: not_found|inactive|expired|not_started|min_total|used_up|phone_limit}
  Заказ: POST public/companies/<slug>/orders/ c полем promo_code — скидка вычитается из суммы заказа, применение записывается.
Доставка (кассир/владелец):
  POST showcase/orders/<id>/delivery/ {stage: assembled|with_courier|delivered|failed, courier?, note?}
  GET  showcase/orders/<id>/delivery/
  Событие ленты pos_request_changed с kind=showcase_order и stage (касса и программа владельца видят сразу).
"""
import contextvars
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import F
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

Q2 = Decimal("0.01")
STAGES = ("assembled", "with_courier", "delivered", "failed")
_PENDING = contextvars.ContextVar("showcase_promo_pending", default=None)


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def _can_manage(user):
    from apps.users.permissions_flags import has_any_flag, is_privileged

    return is_privileged(user) or has_any_flag(user, ("can_view_showcase",))


def _norm(code):
    return re.sub(r"\s+", "", str(code or "")).upper()[:32]


def _dec(v):
    try:
        return Decimal(str(v).replace(",", ".")).quantize(Q2, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def promo_payload(p):
    return {"id": str(p.id), "code": p.code, "kind": p.kind, "value": format(p.value, "f"), "min_total": format(p.min_total, "f"),
            "max_discount": format(p.max_discount, "f") if p.max_discount is not None else None,
            "valid_from": p.valid_from.isoformat() if p.valid_from else None, "valid_to": p.valid_to.isoformat() if p.valid_to else None,
            "max_uses": p.max_uses, "per_phone": p.per_phone, "used_count": p.used_count, "is_active": p.is_active}


def check(company, code, items_total, phone=""):
    """-> (promo|None, discount: Decimal, reason|None)."""
    from apps.main.models import ShowcaseOrderPromo, ShowcasePromoCode

    p = ShowcasePromoCode.objects.filter(company=company, code=_norm(code)).first()
    if p is None:
        return None, Decimal("0"), "not_found"
    now = timezone.now()
    if not p.is_active:
        return p, Decimal("0"), "inactive"
    if p.valid_from and now < p.valid_from:
        return p, Decimal("0"), "not_started"
    if p.valid_to and now > p.valid_to:
        return p, Decimal("0"), "expired"
    if items_total < p.min_total:
        return p, Decimal("0"), "min_total"
    if p.max_uses is not None and p.used_count >= p.max_uses:
        return p, Decimal("0"), "used_up"
    ph = re.sub(r"\D", "", phone or "")
    if p.per_phone and ph and ShowcaseOrderPromo.objects.filter(promo=p, phone=ph).count() >= p.per_phone:
        return p, Decimal("0"), "phone_limit"
    disc = (items_total * p.value / 100) if p.kind == "percent" else p.value
    if p.max_discount is not None:
        disc = min(disc, p.max_discount)
    disc = max(Decimal("0"), min(disc, items_total)).quantize(Q2, rounding=ROUND_HALF_UP)
    return p, disc, None


def apply_to_order(request, company, items_total, total):
    """Крючок создания заказа: promo_code из тела -> новая сумма; применение запишется после сохранения заказа."""
    data = request.data if hasattr(request.data, "get") else {}
    code = data.get("promo_code") if hasattr(data, "get") else None
    if not code:
        return total
    from apps.main.showcase.design_schema import ShowcaseFieldError

    cust = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    phone = str(data.get("phone") or data.get("customer_phone") or cust.get("phone") or "")
    p, disc, reason = check(company, code, Decimal(str(items_total)), phone)
    if reason:
        raise ShowcaseFieldError("promo_code", "Промокод не подходит.", f"promo_{reason}")
    _PENDING.set({"company": company.pk, "promo": p.pk, "code": p.code, "discount": disc, "phone": re.sub(r"\D", "", phone or "")})
    return (Decimal(str(total)) - disc).quantize(Q2)


@receiver(post_save, sender="main.ShowcaseOrder")
def _order_saved(sender, instance, created, **kw):
    pend = _PENDING.get()
    if not created or not pend or pend["company"] != instance.company_id:
        return
    _PENDING.set(None)
    from apps.main.models import ShowcaseOrderPromo, ShowcasePromoCode

    ShowcaseOrderPromo.objects.create(order=instance, promo_id=pend["promo"], code=pend["code"], discount=pend["discount"], phone=pend["phone"])
    ShowcasePromoCode.objects.filter(pk=pend["promo"]).update(used_count=F("used_count") + 1)


class PromoCodesAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import ShowcasePromoCode

        if not _can_manage(request.user):
            return _err("permission_denied", "Нет права «Витрина».", 403, permission="can_view_showcase")
        return Response([promo_payload(p) for p in ShowcasePromoCode.objects.filter(company=_company(request.user)).order_by("-created_at")])

    def post(self, request):
        from apps.main.models import ShowcasePromoCode

        if not _can_manage(request.user):
            return _err("permission_denied", "Нет права «Витрина».", 403, permission="can_view_showcase")
        p = ShowcasePromoCode(company=_company(request.user), created_by=request.user)
        bad = _fill(p, request.data, create=True)
        if bad:
            return bad
        if ShowcasePromoCode.objects.filter(company=p.company, code=p.code).exists():
            return _err("promo_exists", "Такой промокод уже есть.", 409)
        p.save()
        return Response(promo_payload(p), status=201)


def _fill(p, d, create=False):
    d = d if hasattr(d, "get") else {}
    if create or "code" in d:
        code = _norm(d.get("code"))
        if not re.fullmatch(r"[A-Z0-9А-ЯЁ_-]{3,32}", code or ""):
            return _err("validation_error", "code: 3–32 буквы или цифры.", fields={"code": ["3–32 символа."]})
        p.code = code
    if create or "kind" in d:
        k = str(d.get("kind") or "percent")
        if k not in ("percent", "fixed"):
            return _err("validation_error", "kind: percent или fixed.", fields={"kind": ["percent, fixed"]})
        p.kind = k
    if create or "value" in d:
        v = _dec(d.get("value"))
        if v is None or v <= 0 or (p.kind == "percent" and v > 100):
            return _err("validation_error", "value: больше 0 (процент — до 100).", fields={"value": ["Неверно."]})
        p.value = v
    for f in ("min_total", "max_discount"):
        if f in d:
            v = None if d.get(f) in (None, "") else _dec(d.get(f))
            if d.get(f) not in (None, "") and (v is None or v < 0):
                return _err("validation_error", f"{f}: число ≥ 0.", fields={f: ["Неверно."]})
            setattr(p, f, v if v is not None else (Decimal("0") if f == "min_total" else None))
    for f in ("valid_from", "valid_to"):
        if f in d:
            v = parse_datetime(str(d.get(f))) if d.get(f) else None
            if d.get(f) and v is None:
                return _err("validation_error", f"{f}: ISO-время.", fields={f: ["ISO."]})
            setattr(p, f, v)
    for f in ("max_uses", "per_phone"):
        if f in d:
            try:
                setattr(p, f, None if d.get(f) in (None, "") else max(1, int(d.get(f))))
            except (TypeError, ValueError):
                return _err("validation_error", f"{f}: целое.", fields={f: ["Целое."]})
    if "is_active" in d:
        p.is_active = d.get("is_active") in (True, "true", "1", 1)
    return None


class PromoCodeDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, pk):
        from apps.main.models import ShowcasePromoCode

        return get_object_or_404(ShowcasePromoCode, pk=pk, company=_company(request.user))

    def patch(self, request, pk):
        if not _can_manage(request.user):
            return _err("permission_denied", "Нет права «Витрина».", 403, permission="can_view_showcase")
        p = self._get(request, pk)
        bad = _fill(p, request.data)
        if bad:
            return bad
        p.save()
        return Response(promo_payload(p))

    def delete(self, request, pk):
        if not _can_manage(request.user):
            return _err("permission_denied", "Нет права «Витрина».", 403, permission="can_view_showcase")
        p = self._get(request, pk)
        if p.used_count or p.orders.exists():
            p.is_active = False
            p.save(update_fields=["is_active"])
            return Response(promo_payload(p))
        p.delete()
        return Response(status=204)


class PublicPromoValidateAPIView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request, slug):
        from apps.main.showcase.services import resolve_public_company

        company = resolve_public_company(slug, request)
        d = request.data if hasattr(request.data, "get") else {}
        total = _dec(d.get("items_total") or 0) or Decimal("0")
        p, disc, reason = check(company, d.get("code"), total, str(d.get("phone") or ""))
        return Response({"valid": reason is None, "code": p.code if p else _norm(d.get("code")), "discount": format(disc, "f"),
                         "total_after": format(max(Decimal("0"), total - disc), "f"), "reason": reason,
                         "min_total": format(p.min_total, "f") if p else None})


class ShowcaseOrderDeliveryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _order(self, request, pk):
        from apps.main.models import ShowcaseOrder

        return get_object_or_404(ShowcaseOrder, pk=pk, company=_company(request.user))

    def get(self, request, pk):
        o = self._order(request, pk)
        try:
            dv = o.delivery
        except Exception:  # noqa: BLE001
            dv = None
        return Response({"order": str(o.pk), "stage": dv.stage if dv else None, "courier": dv.courier if dv else "",
                         "history": dv.history if dv else []})

    def post(self, request, pk):
        from apps.main.models import ShowcaseOrderDelivery
        from apps.users.permissions_flags import has_any_flag, is_privileged

        u = request.user
        if not (is_privileged(u) or has_any_flag(u, ("can_view_showcase", "can_view_cashier", "can_view_sale"))):
            return _err("permission_denied", "Нет права «Витрина» или «Касса».", 403, permission="can_view_showcase")
        d = request.data if hasattr(request.data, "get") else {}
        stage = str(d.get("stage") or "")
        if stage not in STAGES:
            return _err("validation_error", "stage: " + ", ".join(STAGES) + ".", fields={"stage": list(STAGES)})
        with transaction.atomic():
            o = self._order(request, pk)
            if o.status == "canceled":
                return _err("order_canceled", "Заказ отменён.", 409)
            dv, _ = ShowcaseOrderDelivery.objects.select_for_update().get_or_create(order=o, defaults={"stage": stage})
            dv.stage = stage
            if "courier" in d:
                dv.courier = str(d.get("courier") or "")[:128]
            dv.note = str(d.get("note") or "")[:255]
            dv.history = (dv.history or []) + [{"stage": stage, "at": timezone.now().isoformat(), "by": str(u.pk), "note": dv.note}]
            dv.save()
        from apps.integrations.events import emit_event

        emit_event(o.company_id, "pos_request_changed", {"entity": "showcase_order", "entity_id": str(o.pk), "kind": "showcase_order",
                                                         "order": str(o.pk), "number": getattr(o, "number", None), "stage": stage,
                                                         "status": o.status, "actor": u.pk})
        return Response({"order": str(o.pk), "stage": dv.stage, "courier": dv.courier, "history": dv.history})
