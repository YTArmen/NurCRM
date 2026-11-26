"""
elqr1911 (19.11.2026, ТЗ Бекназара «Оплата подписки клиентами через ELQR»): оплата подписки NurCRM через
Dynamic QR Элдик Банка.

  GET  /api/users/billing/config/                    — можно ли платить, тарифы, комиссия, срок подписки
  GET  /api/users/billing/invoices/                  — история счетов компании
  POST /api/users/billing/invoices/                  — счёт + QR {plan_code?, months} (Idempotency-Key)
  GET  /api/users/billing/invoices/{id}/             — статус (сервер сам спрашивает банк, не чаще раза в 3 с)
  POST /api/users/billing/invoices/{id}/cancel/      — отменить неоплаченный счёт
  POST /api/users/billing/invoices/{id}/test-pay/    — «учебный банк»: {result: success|error|cancelled}
  POST /api/users/billing/eldik/callback/            — callback банка (x-api-key + HMAC-SHA256 сырого тела)
  GET  /api/users/billing/report/?month=YYYY-MM      — сводка для акта сверки (только staff)

Режим — переменная ELQR_MODE в .env: test (по умолчанию, «учебный банк», только компании из ELQR_TEST_EMAILS)
или live (настоящий банк, нужны ELQR_API_KEY и ELQR_BASE_URL). Ключ банка — только здесь, в клиентах нет.
Итог = цена × месяцы − скидка (её даёт только staff) + комиссия банка ELQR_FEE (30 сом, платит клиент).
Продление Company.end_date — только по SUCCESS, один раз на счёт, по цене тарифа.
"""
import base64
import hashlib
import hmac
import io
import json
import logging
import os
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from apps.users.models import BillingInvoice, Company, SubscriptionPlan, TariffPack
from apps.users.services_subscription import is_user_owner, resolve_plan_code
from core.exceptions import ApiError

log = logging.getLogger("billing.elqr")

MONTHS = (1, 3, 6, 12)
YEAR_PAID_MONTHS = 10  # trf2111
FINAL = {BillingInvoice.Status.PAID, BillingInvoice.Status.EXPIRED, BillingInvoice.Status.CANCELLED,
         BillingInvoice.Status.ERROR}
BANK_TO_STATUS = {
    "QR": "pending", "CREATED": "pending", "PROCESSING": "processing", "SUCCESS": "paid",
    "ERROR": "error", "CANCELLED": "cancelled", "CANCELED": "cancelled",
}


# ---------------------------------------------------------------- настройки

def _env(name, default=""):
    return (os.getenv(name) or default).strip()


def mode():
    return "live" if _env("ELQR_MODE").lower() == "live" and _env("ELQR_API_KEY") and _env("ELQR_BASE_URL") else "test"


def fee():
    try:
        return Decimal(_env("ELQR_FEE", "30")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("30.00")


def ttl():
    try:
        return max(60, int(_env("ELQR_TTL", "1200")))
    except ValueError:
        return 1200


def callback_url():
    return _env("ELQR_CALLBACK_URL", "https://app.nurcrm.kg/api/users/billing/eldik/callback/")


def test_emails():
    return {e.strip().lower() for e in _env("ELQR_TEST_EMAILS", "market@nur.kg").split(",") if e.strip()}


def available_for(company):
    if company is None:
        return False
    if mode() == "live":
        return True
    owner_email = (getattr(getattr(company, "owner", None), "email", "") or "").lower()
    return owner_email in test_emails()


def _money(v):
    return str(Decimal(str(v or 0)).quantize(Decimal("0.01")))


# ---------------------------------------------------------------- банк

class BankError(Exception):
    pass


class TestBank:
    """«Учебный банк»: те же поля, что у Элдика, без денег. Оплата — кнопкой test-pay."""

    name = "test"

    def generate(self, inv):
        link = f"https://app.nurcrm.kg/elqr-test/{inv.id}"
        return {"qr_image": _qr_png_base64(link), "qr_link": link}

    def state(self, inv):
        return None  # статус меняет только test-pay


class EldikBank:
    """
    Dynamic QR Элдик Банка. Пути и поля — по документации v1.2/1.3 (generate / state); проверить на sandbox
    до включения ELQR_MODE=live. Все ошибки, кроме 401, приходят с HTTP 200 и телом {"err": {...}}.
    """

    name = "eldik"

    def __init__(self):
        self.base = _env("ELQR_BASE_URL").rstrip("/")
        self.key = _env("ELQR_API_KEY")

    def _call(self, method, path, body=None):
        import requests

        try:
            r = requests.request(method, self.base + path, json=body, timeout=15,
                                 headers={"x-api-key": self.key, "Content-Type": "application/json"})
        except requests.RequestException as e:
            raise BankError(f"network: {e.__class__.__name__}")
        if r.status_code == 401:
            raise BankError("unauthorized (401): неверный ключ банка")
        try:
            data = r.json()
        except ValueError:
            raise BankError(f"http {r.status_code}: не JSON")
        if isinstance(data, dict) and data.get("err"):
            err = data["err"] if isinstance(data["err"], dict) else {"error": str(data["err"])}
            raise BankError(f"{err.get('err_code', '')} {err.get('error', '')}".strip())
        if r.status_code >= 400:
            raise BankError(f"http {r.status_code}")
        return data

    def generate(self, inv, ttl_seconds=None):
        # elqr2611: по документации банка нужны psType и callbackUrl; ttl_seconds=0 — тест для банка (TTL=0)
        data = self._call("POST", "/api/v1/qr/generate", {
            "id": str(inv.id), "amount": float(inv.amount),
            "ttl": ttl() if ttl_seconds is None else int(ttl_seconds),
            "name": f"NurCRM · {inv.company.name}"[:64],
            "psType": "eldik", "callbackUrl": callback_url(),
        })
        link = data.get("qrLink") or data.get("qrIink") or ""  # у банка опечатка qrIink
        image = data.get("qrImage") or ""
        if not (link or image):
            raise BankError("банк не вернул QR")
        return {"qr_image": image, "qr_link": link}

    def state(self, inv):
        data = self._call("GET", f"/api/v1/qr/state/{inv.id}")
        return data if isinstance(data, dict) else None


def bank_for(inv):
    return EldikBank() if inv.provider == "eldik" else TestBank()


def _qr_png_base64(text):
    import qrcode

    img = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- цены

def _plan(code):
    for plan in SubscriptionPlan.objects.order_by("price"):
        if resolve_plan_code(plan) == code:
            return plan
    return None


def price_for(company, code):
    """Цена месяца: start/standard — SubscriptionPlan.price; custom — «Старт» + активные пакеты сверх него."""
    if code in ("start", "standard", "mini"):  # trf2111: «Мини» тоже оплачивается
        plan = _plan(code)
        if plan is None:
            raise ApiError("plan_unknown", "Такого тарифа нет.", fields={"plan_code": ["Неизвестный тариф."]})
        return plan, Decimal(plan.price)
    if code == "custom":
        from apps.users.serializers import company_feature_codes

        base = _plan("start")
        if base is None:
            raise ApiError("plan_unknown", "Нет тарифа «Старт».")
        active = set(company_feature_codes(company))
        extra = sum((p.monthly_fee for p in TariffPack.objects.filter(is_active=True, code__in=active)
                     if "start" not in (p.included_in or [])), Decimal("0"))
        return _plan("custom"), Decimal(base.price) + extra
    raise ApiError("plan_unknown", "Тариф: mini, start, standard или custom.", fields={"plan_code": ["Неизвестный тариф."]})


# ---------------------------------------------------------------- продление

def apply_success(inv_id, bank_amount=None, txn_id=""):
    """SUCCESS от банка → продлить подписку один раз. Возвращает счёт."""
    with transaction.atomic():
        inv = BillingInvoice.objects.select_for_update().get(pk=inv_id)
        if inv.extended_at:
            return inv
        now = timezone.now()
        if bank_amount is not None:
            try:
                if Decimal(str(bank_amount)).quantize(Decimal("0.01")) != inv.amount:
                    inv.status, inv.bank_error = BillingInvoice.Status.ERROR, f"amount_mismatch: {bank_amount}"
                    inv.save(update_fields=["status", "bank_error", "updated_at"])
                    log.error("ELQR amount mismatch invoice=%s bank=%s ours=%s", inv.id, bank_amount, inv.amount)
                    return inv
            except InvalidOperation:
                pass
        company = Company.objects.select_for_update().get(pk=inv.company_id)
        before = company.end_date
        base = max(now, before) if before else now
        after = base + relativedelta(months=inv.months)
        company.end_date = after
        fields = ["end_date"]
        if not company.start_date:
            company.start_date = now
            fields.append("start_date")
        if inv.plan_id and company.subscription_plan_id != inv.plan_id:
            company.subscription_plan_id = inv.plan_id
            fields.append("subscription_plan")
        company.save(update_fields=fields)
        inv.status, inv.bank_status = BillingInvoice.Status.PAID, "SUCCESS"
        inv.paid_at = inv.paid_at or now
        inv.extended_at = now
        inv.valid_until_before, inv.valid_until_after = before, after
        if txn_id:
            inv.bank_txn_id = str(txn_id)[:128]
        inv.save()
        log.info("ELQR paid invoice=%s company=%s %s -> %s", inv.id, company.id, before, after)
    try:
        from django.core.cache import cache

        cache.delete(f"subscription:{inv.company_id}")
    except Exception:
        pass
    return inv


def refresh(inv, force=False):
    """Довести статус счёта: спросить банк (живой режим) и отметить истёкшие."""
    if inv.status in FINAL and inv.status != BillingInvoice.Status.CANCELLED:
        return inv
    now = timezone.now()
    if not force and inv.last_checked_at and now - inv.last_checked_at < timedelta(seconds=3):
        return inv
    data = None
    try:
        data = bank_for(inv).state(inv)
    except BankError as e:
        log.warning("ELQR state invoice=%s: %s", inv.id, e)
    inv.last_checked_at = now
    if data:
        bank_status = str(data.get("status") or "").upper()
        if bank_status == "SUCCESS":
            return apply_success(inv.id, data.get("amount"), data.get("transactionId") or data.get("txnId") or "")
        if inv.status != BillingInvoice.Status.CANCELLED and bank_status in BANK_TO_STATUS:
            inv.status = BANK_TO_STATUS[bank_status]
        inv.bank_status = bank_status[:32]
    if inv.status == BillingInvoice.Status.PENDING and inv.expires_at and now > inv.expires_at:
        inv.status = BillingInvoice.Status.EXPIRED
    inv.save(update_fields=["status", "bank_status", "last_checked_at", "updated_at"])
    return inv


# ---------------------------------------------------------------- API

def _company(request):
    user = request.user
    company = getattr(user, "owned_company", None) or getattr(user, "company", None)
    if company is None:
        raise ApiError("permission_denied", "Пользователь не привязан к компании.", status_code=403)
    if not is_user_owner(user, company):
        raise ApiError("permission_denied", "Оплата подписки — только владелец или администратор.", status_code=403)
    return company


def _is_staff(user):
    return bool(getattr(user, "is_superuser", False) or getattr(user, "is_staff", False))


def ser(inv, with_qr=True):
    paid = inv.status == BillingInvoice.Status.PAID
    lines = [{"title": f"Подписка NurCRM, {inv.months} мес.", "amount": _money(inv.subtotal)}]
    if inv.discount:
        lines.append({"title": "Скидка (год = 10 месяцев)" if inv.months == 12 else "Скидка",
                      "amount": _money(-inv.discount)})
    if inv.bank_fee:
        lines.append({"title": "Комиссия банка", "amount": _money(inv.bank_fee)})
    out = {
        "invoice_id": str(inv.id), "status": inv.status, "provider": inv.provider, "test_mode": inv.provider == "test",
        "plan": inv.plan_code, "plan_name": inv.plan.name if inv.plan_id and inv.plan else inv.plan_code,
        "months": inv.months, "period": "year" if inv.months == 12 else "month", "price_month": _money(inv.price_month), "subtotal": _money(inv.subtotal),
        "discount": _money(inv.discount), "bank_fee": _money(inv.bank_fee), "amount": _money(inv.amount),
        "lines": lines, "qr_link": inv.qr_link or None,
        "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
        "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
        "valid_until": (inv.valid_until_after if paid else inv.company.end_date),
        "error": inv.bank_error or None, "created_at": inv.created_at.isoformat(),
    }
    out["valid_until"] = out["valid_until"].isoformat() if out["valid_until"] else None
    if with_qr:
        out["qr_image"] = inv.qr_image or None
    return out


class BillingConfigAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request)
        plans = []
        for code in ("mini", "start", "standard", "custom"):  # trf2111: +mini, цена года
            try:
                _, price = price_for(company, code)
                plans.append({"code": code, "price_month": _money(price),
                              "price_year": _money(price * YEAR_PAID_MONTHS)})
            except ApiError:
                pass
        return Response({
            "available": available_for(company), "mode": mode(), "bank_fee": _money(fee()), "months": list(MONTHS),
            "current_plan": resolve_plan_code(company.subscription_plan) if company.subscription_plan_id else None,
            "valid_until": company.end_date.isoformat() if company.end_date else None,
            "plans": plans, "qr_ttl_seconds": ttl(),
            "periods": ["month", "year"], "year_paid_months": YEAR_PAID_MONTHS,
        })


class BillingInvoiceListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request)
        qs = BillingInvoice.objects.filter(company=company).select_related("plan", "company")
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        return Response({"results": [ser(i, with_qr=False) for i in qs[:200]]})

    @idempotent(body_key=True, replay_flag=True)
    def post(self, request):
        company = _company(request)
        if not available_for(company):
            raise ApiError("billing_unavailable", "Оплата через ELQR пока не включена.", status_code=409)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            months = int(data.get("months") or (12 if str(data.get("period") or "") == "year" else 1))
        except (TypeError, ValueError):
            months = 0
        if str(data.get("period") or "") == "year" and months != 12:
            raise ApiError("invalid", "period=year — это 12 месяцев.", fields={"months": ["12."]})
        if months not in MONTHS:
            raise ApiError("invalid", "Срок: 1, 3, 6 или 12 месяцев.", fields={"months": ["1, 3, 6 или 12."]})
        code = str(data.get("plan_code") or "").strip().lower() or (
            resolve_plan_code(company.subscription_plan) if company.subscription_plan_id else "start")
        plan, price = price_for(company, code)
        if price > 0 and code != "mini":  # elqrmod2011: абонплата набора = тариф + модули других сфер (extra)
            try:
                from apps.users.tariff_modules import module_quote
                price = (price + Decimal(str(module_quote(company)["extra_month"] or 0))).quantize(Decimal("0.01"))
            except Exception:  # noqa: BLE001
                pass
        if price <= 0:
            raise ApiError("plan_free", "Этот тариф бесплатный, оплачивать нечего.", status_code=409)
        hour_ago = timezone.now() - timedelta(hours=1)
        if BillingInvoice.objects.filter(company=company, created_at__gte=hour_ago).count() >= 20:
            raise ApiError("too_many_invoices", "Слишком много счетов за час, попробуйте позже.", status_code=429)

        subtotal = (price * months).quantize(Decimal("0.01"))
        discount = Decimal("0")
        # trf2111: год (12 мес.) = цена 10 месяцев, скидка 2 месяца
        year_discount = (price * (12 - YEAR_PAID_MONTHS)).quantize(Decimal("0.01")) if months == 12 else Decimal("0")
        if _is_staff(request.user):  # скидку даёт только staff (админка платформы)
            try:
                if data.get("amount_override") not in (None, ""):
                    discount = subtotal - Decimal(str(data["amount_override"]))
                elif data.get("discount_percent") not in (None, ""):
                    pct = Decimal(str(data["discount_percent"]))
                    if not (0 <= pct <= 100):
                        raise InvalidOperation
                    discount = subtotal * pct / 100
            except InvalidOperation:
                raise ApiError("invalid", "Скидка указана неверно.")
            discount = min(max(discount, Decimal("0")), subtotal).quantize(Decimal("0.01"))
        if data.get("amount_override") in (None, ""):
            discount = min(discount + year_discount, subtotal)
        bank_fee = fee()
        amount = subtotal - discount + bank_fee
        provider = "eldik" if mode() == "live" else "test"
        inv = BillingInvoice.objects.create(
            company=company, created_by=request.user, provider=provider, plan=plan, plan_code=code, months=months,
            price_month=price, subtotal=subtotal, discount=discount, bank_fee=bank_fee, amount=amount,
            expires_at=timezone.now() + timedelta(seconds=ttl()),
        )
        inv.company = company
        try:
            qr = bank_for(inv).generate(inv)
        except BankError as e:
            inv.status, inv.bank_error = BillingInvoice.Status.ERROR, str(e)[:255]
            inv.save(update_fields=["status", "bank_error", "updated_at"])
            log.error("ELQR generate invoice=%s: %s", inv.id, e)
            raise ApiError("bank_unavailable", "Банк не ответил, попробуйте ещё раз.", status_code=502,
                           data={"invoice_id": str(inv.id)})
        inv.qr_image, inv.qr_link = qr["qr_image"], qr["qr_link"]
        inv.save(update_fields=["qr_image", "qr_link", "updated_at"])
        return Response(ser(inv), status=201)


def _get(request, pk):
    company = _company(request)
    inv = BillingInvoice.objects.filter(pk=pk, company=company).select_related("plan", "company").first()
    if inv is None:
        raise ApiError("not_found", "Счёт не найден.", status_code=404)
    return inv


class BillingInvoiceDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        inv = refresh(_get(request, pk))
        inv.refresh_from_db()
        return Response(ser(inv))


class BillingInvoiceCancelAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        inv = _get(request, pk)
        if inv.status in (BillingInvoice.Status.PENDING, BillingInvoice.Status.PROCESSING):
            inv.status = BillingInvoice.Status.CANCELLED
            inv.save(update_fields=["status", "updated_at"])
        return Response(ser(inv, with_qr=False))


class BillingInvoiceTestPayAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        inv = _get(request, pk)
        if inv.provider != "test" or not available_for(inv.company):
            raise ApiError("not_test_invoice", "Тестовая оплата только для «учебного банка».", status_code=409)
        result = str((request.data or {}).get("result") or "success").lower()
        if inv.status not in (BillingInvoice.Status.PENDING, BillingInvoice.Status.PROCESSING):
            raise ApiError("invoice_final", f"Счёт уже в статусе {inv.status}.", status_code=409)
        if result == "success":
            inv = apply_success(inv.id, inv.amount, f"test-{inv.id}")
        elif result in ("error", "cancelled", "processing"):
            inv.status, inv.bank_status = result, result.upper()
            inv.save(update_fields=["status", "bank_status", "updated_at"])
        else:
            raise ApiError("invalid", "result: success, processing, error или cancelled.")
        inv.refresh_from_db()
        return Response(ser(inv, with_qr=False))


class BillingReportAPIView(APIView):
    """Сводка для акта сверки с банком (Приложение 5): по дням месяца — количество, сумма, комиссия."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not _is_staff(request.user):
            raise ApiError("permission_denied", "Только для администрации NurCRM.", status_code=403)
        m = (request.query_params.get("month") or timezone.localdate().strftime("%Y-%m")).strip()
        try:
            y, mm = int(m[:4]), int(m[5:7])
            assert len(m) == 7 and 1 <= mm <= 12
        except (ValueError, AssertionError):
            raise ApiError("period_invalid", "Месяц указан неверно (YYYY-MM).")
        qs = BillingInvoice.objects.filter(status=BillingInvoice.Status.PAID, paid_at__year=y, paid_at__month=mm)
        provider = request.query_params.get("provider") or "eldik"
        qs = qs.filter(provider=provider)
        tot = qs.aggregate(n=Count("id"), amount=Sum("amount"), fee=Sum("bank_fee"))
        return Response({"month": m, "provider": provider, "count": tot["n"], "amount": _money(tot["amount"]),
                         "bank_fee": _money(tot["fee"]),
                         "invoices": [{"invoice_id": str(i.id), "company": str(i.company_id), "amount": _money(i.amount),
                                       "paid_at": i.paid_at.isoformat(), "bank_txn_id": i.bank_txn_id}
                                      for i in qs.order_by("paid_at")[:5000]]})


def _client_ip(request):
    return (request.META.get("HTTP_X_REAL_IP") or request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0]
            or request.META.get("REMOTE_ADDR") or "").strip()


@method_decorator(csrf_exempt, name="dispatch")
class EldikCallbackView(View):
    """Callback банка. Подпись — HMAC-SHA256 тела как получено, ключ = api-key. Ответ 200 сразу."""

    def post(self, request):
        key = _env("ELQR_API_KEY")
        if not key:
            return JsonResponse({"ok": False, "error": "not_configured"}, status=503)
        ips = {i.strip() for i in _env("ELQR_CALLBACK_IPS").split(",") if i.strip()}
        if ips and _client_ip(request) not in ips:
            log.warning("ELQR callback from foreign ip %s", _client_ip(request))
            return JsonResponse({"ok": False}, status=403)
        raw = request.body
        sig = (request.headers.get("x-request-signature") or "").strip().lower()
        good = hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("x-api-key") or "", key) or not hmac.compare_digest(sig, good):
            log.warning("ELQR callback bad signature ip=%s", _client_ip(request))
            return JsonResponse({"ok": False, "error": "bad_signature"}, status=403)
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            return JsonResponse({"ok": False, "error": "bad_json"}, status=400)
        inv_id = body.get("id") or body.get("qrId") or body.get("orderId")
        inv = BillingInvoice.objects.filter(pk=inv_id).first() if inv_id else None
        if inv is None:
            log.warning("ELQR callback unknown invoice %s", inv_id)
            return JsonResponse({"ok": True})
        status = str(body.get("status") or "").upper()
        if status == "SUCCESS":
            # перепроверяем у банка: продление только по подтверждённому SUCCESS
            try:
                st = bank_for(inv).state(inv) or {}
            except BankError as e:
                log.error("ELQR callback state check invoice=%s: %s", inv.id, e)
                st = {}
            if str(st.get("status") or "").upper() == "SUCCESS":
                apply_success(inv.id, st.get("amount", body.get("amount")),
                              st.get("transactionId") or body.get("transactionId") or "")
        else:
            refresh(inv, force=True)
        return JsonResponse({"ok": True})
