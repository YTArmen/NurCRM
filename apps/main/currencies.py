"""
cur2011 (20.11.2026, «Мини»/базар): курсы валют компании и оплата в валюте.

GET/PUT /api/main/currencies/   {base:"KGS", rates:{"USD":"87.50","RUB":"0.95","CNY":"12.10"}, enabled:["USD",…]}
        читают все, меняют владелец/админ; version + If-Match (409 version_conflict); событие app_settings_changed (app=currencies).
Оплата в валюте — в строке оплаты чека (pos/checkout/, pos/carts/<id>/checkout/):
        payments[] (pos/checkout/ — payment.payments[]): {method, amount, currency:"USD", amount_currency:"20", rate:"87.5"}
        amount — ВСЕГДА в сомах (как раньше: из него ящик, смена, долг, отчёты). currency/amount_currency/rate — запись, сколько
        и по какому курсу взяли. Проверка: |amount − amount_currency×rate| ≤ 1 сом, иначе 400 currency_amount_mismatch.
        Без rate берётся курс компании. Неизвестная валюта → 400 currency_unknown.
В оплатах чека (payments[]) — currency, amount_currency, rate (null — сомы).
В отчёте смены (shifts/<id>/report/) — cash_by_currency: {"USD": {"amount_currency": "40.00", "amount": "3500.00"}, …}.
"""
import contextvars
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

APP = "currencies"
BASE = "KGS"
KNOWN = ("USD", "EUR", "RUB", "CNY", "KZT", "UZS", "TRY")
_PENDING = contextvars.ContextVar("cur2011_pending", default=None)


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _dec(v):
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _doc(company, lock=False):
    from apps.users.models import CompanyAppSettings

    qs = CompanyAppSettings.objects.filter(company=company, branch__isnull=True, app=APP)
    return (qs.select_for_update() if lock else qs).first()


def settings_of(company):
    doc = _doc(company) if company else None
    s = dict(doc.settings or {}) if doc else {}
    rates = {k: str(v) for k, v in (s.get("rates") or {}).items() if k in KNOWN}
    return {"base": BASE, "rates": rates, "enabled": [c for c in (s.get("enabled") or list(rates)) if c in rates],
            "known": list(KNOWN), "version": doc.version if doc else 0,
            "updated_at": doc.updated_at.isoformat() if doc else None}


class CurrenciesAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        c = _company(request.user)
        if c is None:
            return Response({"code": "no_company", "detail": "Нет компании."}, status=403)
        r = Response(settings_of(c))
        r["ETag"] = f'"{r.data["version"]}"'
        return r

    def put(self, request):
        from apps.users.models import CompanyAppSettings

        c = _company(request.user)
        u = request.user
        if c is None:
            return Response({"code": "no_company", "detail": "Нет компании."}, status=403)
        if not (c.owner_id == u.id or getattr(u, "role", "") in ("owner", "admin") or u.is_superuser):
            return Response({"code": "permission_denied", "detail": "Курсы меняет владелец или администратор."}, status=403)
        d = request.data if isinstance(request.data, dict) else {}
        rates_in = d.get("rates")
        if rates_in is not None and not isinstance(rates_in, dict):
            return Response({"code": "validation_error", "detail": "rates — объект {\"USD\": \"87.5\"}."}, status=400)
        with transaction.atomic():
            doc = _doc(c, lock=True)
            if doc is None:
                CompanyAppSettings.objects.create(company=c, branch=None, app=APP, settings={}, version=0)
                doc = _doc(c, lock=True)
            im = (request.headers.get("If-Match") or "").strip().lstrip("W/").strip('"')
            if im and im != str(doc.version):
                return Response({"code": "version_conflict", "detail": "Курсы изменили в другом месте.",
                                 "version": doc.version}, status=409)
            s = dict(doc.settings or {})
            rates = dict(s.get("rates") or {})
            bad = {}
            for k, v in (rates_in or {}).items():
                k = str(k).upper()
                if k not in KNOWN:
                    bad[k] = "Неизвестная валюта."
                    continue
                if v in (None, ""):
                    rates.pop(k, None)
                    continue
                dv = _dec(v)
                if dv is None or dv <= 0 or dv > Decimal("1000000"):
                    bad[k] = "Курс — число больше 0."
                    continue
                rates[k] = format(dv.quantize(Decimal("0.000001")).normalize(), "f")
            if bad:
                return Response({"code": "validation_error", "detail": "Неверные курсы.", "fields": bad}, status=400)
            enabled = d.get("enabled")
            if enabled is not None:
                if not isinstance(enabled, list):
                    return Response({"code": "validation_error", "detail": "enabled — список валют."}, status=400)
                enabled = [str(x).upper() for x in enabled if str(x).upper() in rates]
            s.update({"rates": rates, "enabled": enabled if enabled is not None else [x for x in (s.get("enabled") or list(rates)) if x in rates]})
            doc.settings = s
            doc.version = (doc.version or 0) + 1
            doc.updated_by = u
            doc.save(update_fields=["settings", "version", "updated_by", "updated_at"])
        r = Response(settings_of(c))
        r["ETag"] = f'"{r.data["version"]}"'
        return r

    patch = put


# ------------------------------------------------------------------ оплата в валюте

def _parse_lines(request, company):
    """Строки оплаты с валютой из тела запроса → [(method, amount, currency, amount_currency, rate)] или Response-ошибка."""
    d = request.data if isinstance(getattr(request, "data", None), dict) else {}
    pays = d.get("payments")
    if not isinstance(pays, list) and isinstance(d.get("payment"), dict):  # pos/checkout/: payment.payments[]
        pays = d["payment"].get("payments")
    if not isinstance(pays, list) or not any(isinstance(p, dict) and p.get("currency") for p in pays):
        return None
    rates = settings_of(company)["rates"]
    out = []
    for i, p in enumerate(pays):
        if not isinstance(p, dict):
            continue
        cur = str(p.get("currency") or "").upper()
        if not cur or cur == BASE:
            continue
        if cur not in KNOWN:
            return Response({"code": "currency_unknown", "detail": f"Неизвестная валюта {cur}.",
                             "fields": {f"payments[{i}].currency": ["Неизвестная валюта."]}}, status=400)
        amt, ac = _dec(p.get("amount")), _dec(p.get("amount_currency"))
        rate = _dec(p.get("rate")) if p.get("rate") not in (None, "") else _dec(rates.get(cur))
        if ac is None or ac <= 0 or rate is None or rate <= 0:
            return Response({"code": "currency_rate_required",
                             "detail": "Для оплаты в валюте нужны amount_currency и курс (rate или курс компании).",
                             "fields": {f"payments[{i}]": ["amount_currency и rate."]}}, status=400)
        if amt is None:
            return Response({"code": "validation_error", "detail": "amount (в сомах) обязателен.",
                             "fields": {f"payments[{i}].amount": ["Обязательное поле."]}}, status=400)
        if abs(amt - ac * rate) > Decimal("1"):
            return Response({"code": "currency_amount_mismatch",
                             "detail": "amount (сом) не сходится с amount_currency × rate.",
                             "expected_amount": str((ac * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                             "fields": {f"payments[{i}].amount": ["Не сходится с курсом."]}}, status=400)
        out.append((str(p.get("method") or ""), amt.quantize(Decimal("0.01")), cur, ac.quantize(Decimal("0.01")), rate))
    return out


def _apply(sale_id, lines):
    from apps.main.models import SalePayment

    pays = list(SalePayment.objects.filter(sale_id=sale_id).order_by("created_at", "id"))
    used = set()
    for method, amt, cur, ac, rate in lines:
        for p in pays:
            if p.id in used or p.method != method or Decimal(p.amount) != amt:
                continue
            used.add(p.id)
            SalePayment.objects.filter(pk=p.pk).update(currency=cur, amount_currency=ac, rate=rate)
            break


def wrap_checkout(cls):
    orig = cls.post

    def post(self, request, *a, **kw):
        company = _company(request.user)
        lines = _parse_lines(request, company) if company is not None else None
        if isinstance(lines, Response):
            return lines
        resp = orig(self, request, *a, **kw)
        if lines and resp.status_code < 300 and isinstance(resp.data, dict):
            sid = resp.data.get("sale_id") or resp.data.get("id")
            if sid:
                try:
                    _apply(sid, lines)
                except Exception:  # noqa: BLE001
                    pass
        return resp

    cls.post = post


def cash_by_currency(shift):
    from django.db.models import Sum

    from apps.main.models import Sale, SalePayment

    rows = (SalePayment.objects.filter(sale__shift_id=shift.id, currency__isnull=False,
                                       sale__status__in=[Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED, Sale.Status.DEBT],
                                       method=Sale.PaymentMethod.CASH)
            .exclude(currency="").values("currency").annotate(ac=Sum("amount_currency"), a=Sum("amount")))
    return {r["currency"]: {"amount_currency": f"{r['ac'] or 0:.2f}", "amount": f"{r['a'] or 0:.2f}"} for r in rows}


def wrap_shift_report():
    import apps.construction.views as v

    orig = v.build_shift_report

    def build(shift):
        data = orig(shift)
        try:
            data["cash_by_currency"] = cash_by_currency(shift)
        except Exception:  # noqa: BLE001
            data["cash_by_currency"] = {}
        return data

    v.build_shift_report = build


def install():
    from apps.main.kassa_views import PosQuickCheckoutAPIView
    from apps.main.pos_views import SaleCheckoutAPIView

    wrap_checkout(PosQuickCheckoutAPIView)
    wrap_checkout(SaleCheckoutAPIView)
    wrap_shift_report()
