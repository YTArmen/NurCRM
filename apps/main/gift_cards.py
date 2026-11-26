"""П3-1 (15.11.2026, p31_1511): подарочные сертификаты и сводка действующих акций.

POST gift-cards/ {nominal, code?, expires_at?, client?, note?} + Idempotency-Key — выпуск (владелец/админ).
     Продажа сертификата покупателю — обычной строкой чека (услуга «Подарочный сертификат»); sold_sale_id можно передать.
GET  gift-cards/?status=&page= ; GET gift-cards/<code>/ — баланс и движения; POST gift-cards/<code>/cancel/ {reason}.
Оплата сертификатом в pos/checkout/: часть оплаты {"method": "gift_card", "amount", "gift_card": "<code>"} — разбирает
крючок кассы (apps/repair/checkout_hook, общий с trade_in_credit): reserve() до продажи, settle() после; в продажу часть
идёт как offset (неденежный зачёт: выручка полная, в ящик не попадает).
Ошибки: 404 gift_card_not_found, 409 gift_card_unavailable {balance, status, expires_at}, 400 gift_card_required.
GET  promotions/active/ — действующие акции компании: скидки товаров, ступени скидок, «N+1» (PromoRule), промокоды витрины.
"""
import secrets
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

Q2 = Decimal("0.01")
_ALPH = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def _money(v):
    try:
        d = Decimal(str(v).replace(",", ".")).quantize(Q2)
        return d if d > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _norm(code):
    return "".join(str(code or "").split()).upper()[:32]


def card_payload(c, moves=False):
    out = {"id": str(c.id), "code": c.code, "nominal": format(c.nominal, "f"), "balance": format(c.balance, "f"),
           "available": format(max(c.balance - c.pending, Decimal("0")), "f"), "status": c.status,
           "expires_at": c.expires_at.isoformat() if c.expires_at else None,
           "client": str(c.client_id) if c.client_id else None, "note": c.note, "created_at": c.created_at.isoformat()}
    if moves:
        out["moves"] = [{"kind": m.kind, "amount": format(m.amount, "f"), "sale_id": str(m.sale_id) if m.sale_id else None,
                         "at": m.created_at.isoformat()} for m in c.moves.all()]
    return out


def _usable(c):
    return c.status == "active" and (c.expires_at is None or c.expires_at >= timezone.localdate())


# ---------------------------------------------------------------- крючок оплаты (вызывает checkout_hook)

def reserve(request, company, parts, offline=False):
    """parts — части оплаты method=gift_card. -> (error Response | None, held [(card_id, amount)], warnings)."""
    from apps.main.models import GiftCard

    held, warns = [], []
    with transaction.atomic():
        for i, p in enumerate(parts):
            code, amount = _norm(p.get("gift_card")), _money(p.get("amount"))
            if not code or amount is None:
                return _err("gift_card_required", "Для gift_card нужны gift_card (код) и amount > 0.", index=i), [], []
            c = GiftCard.objects.select_for_update().filter(company=company, code=code).first()
            if c is None:
                if offline:
                    warns.append({"code": "gift_card_unconfirmed", "index": i, "gift_card": code, "reason": "not_found"})
                    continue
                return _err("gift_card_not_found", "Сертификат не найден.", 404, index=i), [], []
            avail = c.balance - c.pending
            if not _usable(c) or amount > avail:
                if offline:
                    warns.append({"code": "gift_card_unconfirmed", "index": i, "gift_card": code,
                                  "reason": "unavailable" if _usable(c) else c.status, "balance": format(max(avail, Decimal("0")), "f"),
                                  "shortfall": format(max(amount - max(avail, Decimal("0")), Decimal("0")), "f")})
                    c.pending += amount
                    c.save(update_fields=["pending", "updated_at"])
                    held.append((c.pk, amount))
                    continue
                return _err("gift_card_unavailable", "Сертификатом нельзя оплатить эту сумму.", 409, index=i,
                            balance=format(max(avail, Decimal("0")), "f"), status=c.status,
                            expires_at=c.expires_at.isoformat() if c.expires_at else None), [], []
            c.pending += amount
            c.save(update_fields=["pending", "updated_at"])
            held.append((c.pk, amount))
    return None, held, warns


def settle(held, sale_id, ok, user=None):
    from apps.main.models import GiftCard, GiftCardMove

    with transaction.atomic():
        for cid, amount in held:
            c = GiftCard.objects.select_for_update().get(pk=cid)
            c.pending = max(Decimal("0"), c.pending - amount)
            if ok and sale_id and not GiftCardMove.objects.filter(card=c, kind="redeem", sale_id=sale_id).exists():
                take = min(amount, max(c.balance, Decimal("0")))  # офлайн-недостача — в warnings чека, баланс не в минус
                c.balance -= take
                GiftCardMove.objects.create(card=c, kind="redeem", amount=take, sale_id=sale_id, user=user)
                if c.balance <= 0 and c.status == "active":
                    c.status = "used"
            c.save()


# ---------------------------------------------------------------- адреса

class GiftCardListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import GiftCard

        qs = GiftCard.objects.filter(company=_company(request.user))
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        try:
            page = max(1, int(request.query_params.get("page") or 1))
        except ValueError:
            page = 1
        return Response({"count": qs.count(), "page": page, "page_size": 50,
                         "results": [card_payload(c) for c in qs[(page - 1) * 50: page * 50]]})

    def post(self, request):
        from apps.main.models import Client, GiftCard, GiftCardMove
        from apps.main.pos_serializers import _is_owner_like

        if not _is_owner_like(request.user):
            return _err("permission_denied", "Выпускать сертификаты может владелец или админ.", 403, permission="owner")
        company = _company(request.user)
        data = request.data if hasattr(request.data, "get") else {}
        key = (request.headers.get("Idempotency-Key") or "").strip()[:128]
        if key:
            prior = GiftCard.objects.filter(company=company, idempotency_key=key).first()
            if prior is not None:
                return Response({**card_payload(prior), "replayed": True})
        nominal = _money(data.get("nominal"))
        if nominal is None:
            return _err("validation_error", "nominal — сумма больше нуля.", fields={"nominal": ["Больше нуля."]})
        exp = None
        if data.get("expires_at"):
            exp = parse_date(str(data["expires_at"]))
            if exp is None or exp < timezone.localdate():
                return _err("validation_error", "expires_at: дата не в прошлом (YYYY-MM-DD).", fields={"expires_at": ["Дата."]})
        client = None
        if data.get("client"):
            try:
                client = Client.objects.filter(company=company, pk=data["client"]).first()
            except Exception:  # noqa: BLE001
                client = None
            if client is None:
                return _err("validation_error", "Клиент не найден.", fields={"client": ["Не найден."]})
        code = _norm(data.get("code")) or None
        for _ in range(5):
            c = code or "".join(secrets.choice(_ALPH) for _ in range(10))
            try:
                with transaction.atomic():
                    card = GiftCard.objects.create(company=company, code=c, nominal=nominal, balance=nominal, expires_at=exp,
                                                   client=client, sold_sale_id=data.get("sold_sale_id") or None,
                                                   note=str(data.get("note") or "")[:255], idempotency_key=key,
                                                   created_by=request.user)
                    GiftCardMove.objects.create(card=card, kind="issue", amount=nominal, user=request.user)
                return Response({**card_payload(card), "replayed": False}, status=201)
            except IntegrityError:
                if code:
                    return _err("gift_card_code_taken", "Сертификат с таким кодом уже есть.", 409)
        return _err("validation_error", "Не удалось подобрать код, повторите.")


class GiftCardDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, code):
        from apps.main.models import GiftCard

        c = GiftCard.objects.filter(company=_company(request.user), code=_norm(code)).prefetch_related("moves").first()
        if c is None:
            return _err("gift_card_not_found", "Сертификат не найден.", 404)
        return Response(card_payload(c, moves=True))


class GiftCardRefundAPIView(APIView):
    """POST gift-cards/<code>/refund/ {amount, sale_id} + Idempotency-Key — возврат товара, оплаченного сертификатом:
    сумма возвращается на сертификат (по умолчанию до решения владельца). Условия:
    - чек этой компании и по нему было списание с сертификата; не больше списанного минус уже возвращённое;
    - у чека есть проведённые возвраты, и все refund по чеку (с этим) ≤ сумме возвратов —
      иначе 409 gift_card_refund_without_return (владелец/админ может без возврата);
    - тот же Idempotency-Key -> тот же ответ (replayed=true)."""

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, code):
        from django.db.models import Sum

        from apps.main.models import GiftCard, GiftCardMove, Sale, SaleReturn
        from apps.main.pos_serializers import _is_owner_like

        company = _company(request.user)
        c = GiftCard.objects.select_for_update().filter(company=company, code=_norm(code)).first()
        if c is None:
            return _err("gift_card_not_found", "Сертификат не найден.", 404)
        data = request.data if hasattr(request.data, "get") else {}
        key = (request.headers.get("Idempotency-Key") or str(data.get("idempotency_key") or "")).strip()[:128]
        if key:
            prior = c.moves.filter(kind="refund", idempotency_key=key).first()
            if prior is not None:
                return Response({**card_payload(c), "refunded": format(prior.amount, "f"), "replayed": True})
        amount, sale_id = _money(data.get("amount")), data.get("sale_id")
        if amount is None or not sale_id:
            return _err("validation_error", "Нужны amount > 0 и sale_id.")
        try:
            sale = Sale.objects.filter(company=company, pk=sale_id).only("id").first()
        except Exception:  # noqa: BLE001 — неверный uuid
            sale = None
        if sale is None:
            return _err("validation_error", "Чек не найден в этой компании.", fields={"sale_id": ["Не найден."]})
        spent = c.moves.filter(kind="redeem", sale_id=sale.pk).aggregate(s=Sum("amount"))["s"] or Decimal("0")
        back = c.moves.filter(kind="refund", sale_id=sale.pk).aggregate(s=Sum("amount"))["s"] or Decimal("0")
        if amount > spent - back:
            return _err("gift_card_refund_exceeds", "Вернуть можно не больше, чем списано по этому чеку.", 409,
                        available=format(max(spent - back, Decimal("0")), "f"))
        if not _is_owner_like(request.user):
            returned = (SaleReturn.objects.filter(company=company, sale_id=sale.pk).exclude(status="voided")
                        .aggregate(s=Sum("returned_amount"))["s"] or Decimal("0"))
            all_back = (GiftCardMove.objects.filter(card__company=company, kind="refund", sale_id=sale.pk)
                        .aggregate(s=Sum("amount"))["s"] or Decimal("0"))
            if all_back + amount > returned:
                return _err("gift_card_refund_without_return", "Сначала оформите возврат товара по этому чеку.", 409,
                            returned_total=format(returned, "f"), refunded_total=format(all_back, "f"))
        GiftCardMove.objects.create(card=c, kind="refund", amount=amount, sale_id=sale.pk, user=request.user,
                                    idempotency_key=key)
        c.balance += amount
        if c.status == "used":
            c.status = "active"
        c.save(update_fields=["balance", "status", "updated_at"])
        return Response({**card_payload(c), "refunded": format(amount, "f"), "replayed": False})


class GiftCardCancelAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, code):
        from apps.main.models import GiftCard, GiftCardMove
        from apps.main.pos_serializers import _is_owner_like

        if not _is_owner_like(request.user):
            return _err("permission_denied", "Аннулировать может владелец или админ.", 403, permission="owner")
        c = GiftCard.objects.select_for_update().filter(company=_company(request.user), code=_norm(code)).first()
        if c is None:
            return _err("gift_card_not_found", "Сертификат не найден.", 404)
        if c.status == "cancelled":
            return Response({**card_payload(c), "replayed": True})
        if c.pending > 0:
            return _err("gift_card_busy", "По сертификату идёт оплата, повторите позже.", 409)
        GiftCardMove.objects.create(card=c, kind="cancel", amount=c.balance, user=request.user)
        c.status = "cancelled"
        c.note = (c.note + " | " if c.note else "") + str((request.data or {}).get("reason") or "")[:120]
        c.save(update_fields=["status", "note", "updated_at"])
        return Response({**card_payload(c), "replayed": False})


class ActivePromotionsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from django.db.models import Q

        from apps.main.models import Product, ProductPromotionTier, PromoRule

        company = _company(request.user)
        today = timezone.localdate()
        out = []
        for p in Product.objects.filter(company=company, discount_percent__gt=0).only("id", "name", "discount_percent")[:2000]:
            out.append({"type": "product_discount", "product_id": str(p.id), "name": p.name,
                        "discount_percent": format(p.discount_percent, "f")})
        for t in ProductPromotionTier.objects.filter(product__company=company).select_related("product")[:2000]:
            out.append({"type": "tier", "product_id": str(t.product_id), "name": t.product.name,
                        "min_amount": format(t.min_amount, "f") if t.min_amount is not None else None,
                        "promo_quantity": t.promo_quantity, "discount_percent": format(t.discount_percent, "f")})
        rules = PromoRule.objects.filter(company=company, is_active=True).filter(
            Q(active_from__isnull=True) | Q(active_from__lte=today)).filter(Q(active_to__isnull=True) | Q(active_to__gte=today))
        for r in rules[:500]:
            out.append({"type": "gift_n_plus", "id": str(r.id), "title": r.title, "min_qty": r.min_qty, "gift_qty": r.gift_qty,
                        "inclusive": r.inclusive, "product_id": str(r.product_id) if r.product_id else None,
                        "brand_id": str(r.brand_id) if r.brand_id else None,
                        "category_id": str(r.category_id) if r.category_id else None,
                        "active_to": r.active_to.isoformat() if r.active_to else None})
        try:
            from apps.main.models import ShowcasePromoCode  # витрина (П2-5)

            for pc in ShowcasePromoCode.objects.filter(company=company, is_active=True)[:500]:
                out.append({"type": "promo_code", "id": str(pc.id), "code": getattr(pc, "code", None)})
        except Exception:  # noqa: BLE001 — нет модели / другие поля — сводка без промокодов
            pass
        return Response({"date": today.isoformat(), "count": len(out), "results": out})
