"""П2-1 (15.11.2026): способ оплаты trade_in_credit в pos/checkout/ (решение владельца, п.3).

Касса шлёт часть оплаты: {"method": "trade_in_credit", "amount": "5000", "trade_in": "<id скупки>"}.
До продажи: проверяем скупку (своя компания, открыта, хватает остатка) и резервируем (credit_pending), а в саму продажу
часть уходит как "offset" — неденежный зачёт: выручка чека полная, в ящик смены не попадает.
После продажи: резерв -> credit_confirmed + запись TradeInCredit(sale). Ошибка продажи — резерв снимается.
Ошибки (до продажи): 400 trade_in_required, 404 trade_in_not_found, 409 trade_in_unavailable (available).
"""
import logging
from decimal import Decimal

from django.db import transaction
from rest_framework.response import Response

logger = logging.getLogger("nurcrm.repair")
_INSTALLED = False


def _parts(request):
    data = request.data if isinstance(request.data, dict) else None
    pay = (data or {}).get("payment")
    if not isinstance(pay, dict) or not isinstance(pay.get("payments"), list):
        return None, []
    return pay, [p for p in pay["payments"] if isinstance(p, dict) and str(p.get("method") or "").lower() == "trade_in_credit"]


def _gift_parts(request):
    """П3-1 (p31_1511): части оплаты подарочным сертификатом {"method": "gift_card", "amount", "gift_card": "<код>"}."""
    data = request.data if isinstance(request.data, dict) else None
    pay = (data or {}).get("payment")
    if not isinstance(pay, dict) or not isinstance(pay.get("payments"), list):
        return []
    return [p for p in pay["payments"] if isinstance(p, dict) and str(p.get("method") or "").lower() == "gift_card"]


def _reserve(request, parts, offline=False):
    """Онлайн — строгая проверка (4xx). Офлайн-досылка (продажа уже была) — не отказываем: что можно — резервируем,
    остальное — warnings trade_in_unconfirmed (проверка соседней сессии 15.11)."""
    from apps.repair.models import TradeIn
    from apps.repair.perms import company_of, err
    from apps.repair.services import money

    company = company_of(request.user)
    held, warns = [], []
    with transaction.atomic():
        for i, p in enumerate(parts):
            tid = p.get("trade_in")
            amount = money(p.get("amount"))
            if amount is None:
                return err("trade_in_required", "Для trade_in_credit нужен amount > 0.", 400, index=i), [], []
            t = TradeIn.objects.select_for_update().filter(company=company, pk=tid).first() if tid else None
            if t is None:
                if offline:
                    warns.append({"code": "trade_in_unconfirmed", "index": i, "trade_in": str(tid or ""), "reason": "not_found"})
                    continue
                if not tid:
                    return err("trade_in_required", "Для trade_in_credit нужен trade_in.", 400, index=i), [], []
                return err("trade_in_not_found", "Скупка не найдена.", 404, index=i), [], []
            if t.status != "open" or amount > t.available:
                if offline and t.status != "cancelled":
                    warns.append({"code": "trade_in_unconfirmed", "index": i, "trade_in": str(t.pk), "reason": "exceeds_available",
                                  "available": format(max(t.available, Decimal("0")), "f")})
                elif offline:
                    warns.append({"code": "trade_in_unconfirmed", "index": i, "trade_in": str(t.pk), "reason": "cancelled"})
                    continue
                else:
                    return err("trade_in_unavailable", "По скупке нельзя зачесть эту сумму.", 409, index=i,
                               available=format(max(t.available, Decimal("0")), "f"), status=t.status), [], []
            t.credit_pending += amount
            t.save(update_fields=["credit_pending", "updated_at"])
            held.append((t.pk, amount))
    return None, held, warns


def _settle(held, sale_id, ok):
    from apps.repair.models import TradeIn, TradeInCredit
    from apps.repair.services import hist

    with transaction.atomic():
        for tid, amount in held:
            t = TradeIn.objects.select_for_update().get(pk=tid)
            t.credit_pending = max(Decimal("0"), t.credit_pending - amount)
            if ok and sale_id and not TradeInCredit.objects.filter(trade_in=t, sale_id=sale_id).exists():
                TradeInCredit.objects.create(trade_in=t, sale_id=sale_id, amount=amount)
                t.credit_confirmed += amount
                hist(t, None, "credit", sale=str(sale_id), amount=format(amount, "f"))
            if t.status == "open" and t.available <= 0 and t.credit_pending <= 0:
                t.status = "settled"
            t.save()


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        from apps.main.kassa_views import PosQuickCheckoutAPIView
        from apps.main.pos_views import SaleCheckoutAPIView
    except Exception:  # noqa: BLE001
        logger.exception("trade-in checkout hook not installed")
        return
    orig = PosQuickCheckoutAPIView.post
    orig_checkout = SaleCheckoutAPIView.checkout

    def checkout(self, request, pk, data, *a, **kw):
        # «зачёт» (offset) разрешаем только когда его подставил наш крючок trade-in в этом запросе
        if getattr(request, "_nur_trade_in_offset", False):
            kw["allow_offset"] = True
        return orig_checkout(self, request, pk, data, *a, **kw)

    SaleCheckoutAPIView.checkout = checkout

    def post(self, request, *args, **kwargs):
        pay, parts = _parts(request)
        gparts = _gift_parts(request)  # П3-1: сертификаты
        if not parts and not gparts:
            return orig(self, request, *args, **kwargs)
        from apps.repair.perms import company_of, addon_on, err

        company = company_of(request.user)
        key = (request.headers.get("Idempotency-Key") or "").strip()
        if key:
            from apps.main.models import Sale

            if Sale.objects.filter(company=company, idempotency_key=key).exists():
                # повтор уже проведённой продажи: без нового резерва — исходный post отдаст replayed
                for p in parts + gparts:
                    p["method"] = "offset"
                    p.pop("trade_in", None)
                    p.pop("gift_card", None)
                request._nur_trade_in_offset = True
                return orig(self, request, *args, **kwargs)
        data = request.data if isinstance(request.data, dict) else {}
        offline = bool(data.get("offline") and (data.get("sold_at") or data.get("offline_created_at")))
        if parts and not offline and not addon_on(company, "trade_in"):
            return err("feature_off", "Trade-in не подключён.", 403, feature="trade_in", addon="kassa-trade_in")
        held, warns, gheld = [], [], []
        if parts:
            bad, held, warns = _reserve(request, parts, offline=offline)
            if bad is not None:
                return bad
        if gparts:
            from apps.main.gift_cards import reserve as _g_reserve

            gbad, gheld, gwarns = _g_reserve(request, company, gparts, offline=offline)
            if gbad is not None:
                if held:
                    try:
                        _settle(held, None, False)
                    except Exception:  # noqa: BLE001
                        logger.exception("trade-in release failed")
                return gbad
            warns = list(warns) + list(gwarns)
        for p in parts + gparts:
            p["method"] = "offset"
            p.pop("trade_in", None)
            p.pop("gift_card", None)
        request._nur_trade_in_offset = True
        ok, sale_id = False, None
        try:
            resp = orig(self, request, *args, **kwargs)
            if isinstance(resp, Response) and resp.status_code in (200, 201) and isinstance(resp.data, dict):
                if warns:
                    resp.data["warnings"] = list(resp.data.get("warnings") or []) + warns
                sale_id = resp.data.get("id")
                if not sale_id and isinstance(resp.data.get("sale"), dict):
                    sale_id = resp.data["sale"].get("id")
                ok = bool(sale_id)
            return resp
        finally:
            if held:
                try:
                    _settle(held, sale_id, ok)
                except Exception:  # noqa: BLE001
                    logger.exception("trade-in settle failed")
            if gheld:
                try:
                    from apps.main.gift_cards import settle as _g_settle

                    _g_settle(gheld, sale_id, ok, getattr(request, "user", None))
                except Exception:  # noqa: BLE001
                    logger.exception("gift card settle failed")

    PosQuickCheckoutAPIView.post = post
    _INSTALLED = True
