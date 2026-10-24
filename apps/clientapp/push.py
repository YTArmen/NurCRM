"""
Уведомления через Expo Push (https://exp.host/--/api/v2/push/send, без ключей).

ТЗ 14.10 (п. 2.4): одна продажа = одно уведомление, заголовок — название магазина, текст на языке клиента:
  ru: «Начислено 45 баллов» / «Списано 50 баллов» / «Начислено 45, списано 50 баллов»
  ky: «45 балл кошулду» / «50 балл алынды» / «45 балл кошулду, 50 балл алынды»
  data: {type: "purchase", companyId, shopId, purchaseId}
"""
import logging
from decimal import Decimal

import httpx
from django.core.cache import cache

from apps.main.models import ClientBonusTransaction, Sale
from apps.main.phone_utils import normalize_phone_e164

from .models import AppCustomer, AppPushToken

logger = logging.getLogger("clientapp.push")
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_CHUNK = 100
ZERO = Decimal("0.00")
SALE_PUSH_SENT_SECONDS = 30 * 24 * 3600  # отметка «по этой продаже уведомление уже ушло»


def _num(value) -> str:
    d = abs(Decimal(str(value))).quantize(Decimal("0.01")).normalize()
    if d == d.to_integral_value():
        return f"{int(d)}"
    return f"{d:f}".replace(".", ",")


def _ru_points_word(n: Decimal) -> str:
    if n != n.to_integral_value():
        return "балла"
    n = int(n)
    if n % 10 == 1 and n % 100 != 11:
        return "балл"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "балла"
    return "баллов"


def format_points(value: Decimal, lang: str) -> str:
    """«45 баллов» / «45 балл» (ky)."""
    d = abs(Decimal(str(value))).quantize(Decimal("0.01"))
    if lang == "ky":
        return f"{_num(d)} балл"
    return f"{_num(d)} {_ru_points_word(d)}"


def sale_message(shop_name: str, earned: Decimal, spent: Decimal, lang: str):
    """(title, body) для продажи с начислением и/или списанием."""
    earned = abs(Decimal(str(earned or 0)))
    spent = abs(Decimal(str(spent or 0)))
    title = shop_name
    if lang == "ky":
        if earned > 0 and spent > 0:
            body = f"{_num(earned)} балл кошулду, {_num(spent)} балл алынды"
        elif spent > 0:
            body = f"{_num(spent)} балл алынды"
        else:
            body = f"{_num(earned)} балл кошулду"
        return title, body
    if earned > 0 and spent > 0:
        body = f"Начислено {_num(earned)}, списано {format_points(spent, lang)}"
    elif spent > 0:
        body = f"Списано {format_points(spent, lang)}"
    else:
        body = f"Начислено {format_points(earned, lang)}"
    return title, body


def bonus_message(shop_name: str, delta: Decimal, reason: str, lang: str):
    """Совместимость: одно движение (начисление или списание) → (title, body)."""
    d = Decimal(str(delta))
    return sale_message(shop_name, d if d > 0 else ZERO, -d if d < 0 else ZERO, lang)


def referral_message(points: Decimal, lang: str):
    pts = _num(points)
    if lang == "ky":
        return "Досуңуз биринчи сатып алуу жасады", f"Досуңуз биринчи сатып алуу жасады: +{pts} балл"
    return "Друг сделал первую покупку", f"Друг сделал первую покупку: +{pts} {_ru_points_word(Decimal(str(points)))}"


def send_expo(messages):
    """Отправляет сообщения, удаляет токены с DeviceNotRegistered. Возвращает число успешных."""
    ok = 0
    for i in range(0, len(messages), EXPO_CHUNK):
        chunk = messages[i:i + EXPO_CHUNK]
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    EXPO_PUSH_URL,
                    json=chunk,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
            data = resp.json().get("data") or []
        except Exception as exc:
            logger.warning("Expo push failed: %s", exc)
            continue
        if isinstance(data, dict):
            data = [data]
        dead = []
        for msg, ticket in zip(chunk, data):
            if (ticket or {}).get("status") == "ok":
                ok += 1
                continue
            details = (ticket or {}).get("details") or {}
            if details.get("error") == "DeviceNotRegistered":
                dead.append(msg["to"])
            else:
                logger.info("Expo push error: %s", ticket)
        if dead:
            AppPushToken.objects.filter(token__in=dead).delete()
    return ok


def push_to_customer(customer: AppCustomer, title: str, body: str, data: dict = None):
    tokens = list(customer.push_tokens.values_list("token", flat=True))
    if not tokens:
        return 0
    messages = [
        {"to": t, "title": title, "body": body, "sound": "default", "data": data or {}, "priority": "high"}
        for t in tokens
    ]
    return send_expo(messages)


def _shop_ref(company, branch):
    if branch is not None:
        return str(branch.id), f"{company.name} — {branch.name}"
    return str(company.id), company.name


def push_for_sale(sale_id, force=False):
    """
    Одно уведомление по продаже: суммирует начисления (EARN) и списания (REDEEM) по чеку.
    Повторная отправка чека кассой (офлайн-очередь) второго уведомления не даёт: отметка в кэше на 30 дней,
    а сами движения бонусов идемпотентны по Idempotency-Key, так что новых сигналов не будет.
    """
    sale = Sale.objects.select_related("client", "company", "branch").filter(pk=sale_id).first()
    if sale is None or sale.client_id is None:
        return 0
    txs = list(
        ClientBonusTransaction.objects.filter(
            sale_id=sale.pk,
            reason__in=(ClientBonusTransaction.Reason.EARN, ClientBonusTransaction.Reason.REDEEM),
        )
    )
    if not txs:
        return 0
    phone = normalize_phone_e164(sale.client.phone)
    if not phone:
        return 0
    customer = AppCustomer.objects.filter(phone=phone, deleted_at__isnull=True).first()
    if customer is None:
        return 0
    earned = sum((t.delta for t in txs if t.reason == ClientBonusTransaction.Reason.EARN), ZERO)
    spent = sum((-t.delta for t in txs if t.reason == ClientBonusTransaction.Reason.REDEEM), ZERO)
    if earned <= 0 and spent <= 0:
        return 0
    if not force:
        try:
            if not cache.add(f"capp:sale_push_sent:{sale.pk}", 1, SALE_PUSH_SENT_SECONDS):
                return 0
        except Exception:
            logger.debug("sale push dedupe cache unavailable", exc_info=True)
    shop_id, shop_name = _shop_ref(sale.company, sale.branch)
    title, body = sale_message(shop_name, earned, spent, customer.lang)
    data = {
        "type": "purchase",
        "companyId": str(sale.company_id),
        "shopId": shop_id,
        "purchaseId": str(sale.pk),
    }
    return push_to_customer(customer, title, body, data)


def push_for_bonus_tx(tx_id):
    """Совместимость со старым вызовом: уведомление по продаже этого движения."""
    sale_id = ClientBonusTransaction.objects.filter(pk=tx_id).values_list("sale_id", flat=True).first()
    return push_for_sale(sale_id) if sale_id else 0


def push_referral_reward(inviter_pk, points, company_id):
    """Пригласившему: «Друг сделал первую покупку: +100 баллов»."""
    inviter = AppCustomer.objects.filter(pk=inviter_pk, deleted_at__isnull=True).first()
    if inviter is None:
        return 0
    title, body = referral_message(Decimal(str(points)), inviter.lang)
    return push_to_customer(inviter, title, body, {"type": "referral", "companyId": str(company_id)})
