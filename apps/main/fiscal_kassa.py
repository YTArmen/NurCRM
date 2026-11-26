"""13.11.2026 П0-1 (ТЗ сервер — единое): фискальные реквизиты чека, которые касса получила от онлайн-ККМ (eKassa).

Касса сама пробивает чек и потом записывает реквизиты на сервер:
  POST pos/sales/<id>/fiscal/, POST pos/returns/<id>/fiscal/ — идемпотентно (повтор тех же реквизитов -> 200 replayed,
  другие реквизиты у принятого чека -> 409 fiscal_already_set).
Принятый чек (fiscal_status=accepted) нельзя удалить или поменять: 409 fiscal_locked; возврат — только возвратом.
GET/PUT settings/fiscal/ — налоги компании; GET reports/sales-without-fiscal/?from=&to= — чеки без принятого чека ККМ.
Режим: если у компании включена серверная eKassa (apps.ekassa), касса реквизиты не пишет (409 fiscal_mode_server).
"""
from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

STATUSES = ("none", "pending", "accepted", "failed")
TEXT_FIELDS = {  # поле -> max длина
    "fiscal_doc_number": 32, "fiscal_memory_number": 32, "kkm_reg_number": 32, "fiscal_sign": 64,
    "fiscal_qr_url": 512, "kkm_shift_id": 32, "fiscal_error": 255,
}
KEY_FIELDS = ("fiscal_doc_number", "fiscal_memory_number", "kkm_reg_number", "fiscal_sign")


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def fiscal_payload(obj) -> dict:
    at = getattr(obj, "fiscal_at", None)
    out = {"fiscal_status": getattr(obj, "fiscal_status", None) or "none",
           "fiscal_at": at.isoformat() if at else None}
    for f in TEXT_FIELDS:
        out[f] = getattr(obj, f, "") or None
    return out


def fiscal_mode(company) -> str:
    """server — пробивает сервер (apps.ekassa), kassa — касса (fiscal_enabled), off — выключено."""
    if company is None:
        return "off"
    try:
        from apps.ekassa.models import EkassaIntegration

        if EkassaIntegration.objects.filter(company=company, is_enabled=True).exists():
            return "server"
    except Exception:
        pass
    st = settings_of(company, create=False)
    return "kassa" if (st is not None and st.fiscal_enabled) else "off"


# ---------------------------------------------------------------- режимы налогообложения (14.11.2026, p0114)
# Закрытый список кодов. Режим задан и при регистрации ККМ в ГНС; в чек eKassa он не передаётся —
# в чек идут только ставки строки: vat (НДС, %) и st (НсП, %), а НсП разный для наличных и безнала.
# vat/nsp: "settings" — ставки из настроек компании, "zero" — всегда 0 (налог уже входит в режим).
TAX_REGIMES = {
    "general": {"vat": "settings", "nsp": "settings", "names": (
        "Общий налоговый режим", "Жалпы салык режими", "General tax regime", "Genel vergi rejimi", "Umumiy soliq rejimi")},
    "single_tax": {"vat": "zero", "nsp": "zero", "names": (
        "Единый налог (упрощённая система)", "Бирдиктүү салык (жөнөкөйлөтүлгөн система)", "Single tax (simplified system)",
        "Tek vergi (basitleştirilmiş sistem)", "Yagona soliq (soddalashtirilgan tizim)")},
    "patent": {"vat": "zero", "nsp": "zero", "names": (
        "Патент", "Патент", "Patent", "Patent", "Patent")},
    "custom": {"vat": "settings", "nsp": "settings", "names": (
        "Особый режим (ставки вручную)", "Өзгөчө режим (ставкалар кол менен)", "Special regime (manual rates)",
        "Özel rejim (oranlar elle)", "Maxsus rejim (stavkalar qo‘lda)")},
}
_LANGS = ("ru", "ky", "en", "tr", "uz")


def tax_regimes_list():
    return [{"code": c, "vat": r["vat"], "nsp": r["nsp"], "name": dict(zip(_LANGS, r["names"]))}
            for c, r in TAX_REGIMES.items()]


def _q(v):
    return format(Decimal(str(v)).quantize(Decimal("0.01")), "f")


def effective_taxes(st) -> dict:
    """Ставки, которыми касса и сервер считают чек: {vat_rate, nsp_cash, nsp_cashless, product_vat_allowed}.
    Правило строки: НДС = vat_rate товара (если product_vat_allowed и у товара задан), иначе vat_rate компании;
    НсП = nsp_cash, если весь чек оплачен наличными, иначе nsp_cashless (как флаг cash у eKassa)."""
    rule = TAX_REGIMES.get((getattr(st, "tax_regime", "") or "").strip()) if st is not None else None
    zero_vat = bool(rule and rule["vat"] == "zero")
    zero_nsp = bool(rule and rule["nsp"] == "zero")
    vat = Decimal(0)
    if st is not None and st.vat_payer and not zero_vat:
        rates = [Decimal(str(r)) for r in (st.vat_rates or []) if Decimal(str(r)) > 0]
        vat = rates[0] if rates else Decimal(12)
    cash = (st.nsp_rate if st is not None and st.nsp_rate is not None else Decimal(0))
    cashless = (st.nsp_rate_cashless if st is not None and getattr(st, "nsp_rate_cashless", None) is not None else cash)
    if zero_nsp:
        cash = cashless = Decimal(0)
    return {"vat_rate": _q(vat), "nsp_cash": _q(cash), "nsp_cashless": _q(cashless),
            "product_vat_allowed": bool(st is not None and st.vat_payer and not zero_vat)}


def settings_of(company, create=True):
    from apps.main.models import CompanyFiscalSettings

    st = CompanyFiscalSettings.objects.filter(company=company).first()
    if st is None and create:
        st = CompanyFiscalSettings(company=company)
    return st


def settings_payload(company) -> dict:
    st = settings_of(company)
    return {
        "mode": fiscal_mode(company),
        "fiscal_enabled": bool(st.fiscal_enabled),
        "tax_regime": st.tax_regime or "",
        "vat_payer": bool(st.vat_payer),
        "vat_rates": st.vat_rates or [],
        "nsp_rate": format(st.nsp_rate, "f") if st.nsp_rate is not None else None,
        # 14.11.2026 П0-1 (p0114)
        "tax_regime_name": dict(zip(_LANGS, TAX_REGIMES[st.tax_regime]["names"])) if st.tax_regime in TAX_REGIMES else None,
        "nsp_rate_cash": format(st.nsp_rate, "f") if st.nsp_rate is not None else None,
        "nsp_rate_cashless": format(st.nsp_rate_cashless, "f") if st.nsp_rate_cashless is not None else None,
        "effective": effective_taxes(st),
        "tax_regimes": tax_regimes_list(),
        "mode_server_by": "support",  # серверную eKassa включает поддержка, не владелец
    }


def capabilities_block(company) -> dict:
    try:
        return settings_payload(company) if company is not None else {"mode": "off"}
    except Exception:
        return {"mode": "off"}


def is_locked(obj) -> bool:
    return (getattr(obj, "fiscal_status", None) or "none") == "accepted"


def locked_response(obj, what="Чек"):
    return _err("fiscal_locked", f"{what} пробит на ККМ (ФД {obj.fiscal_doc_number}): менять и удалять нельзя, только возврат.",
                409, fiscal_doc_number=obj.fiscal_doc_number or None)


# ---------------------------------------------------------------- запись реквизитов

def _apply(request, obj):
    data = request.data if hasattr(request.data, "get") else {}
    status = str(data.get("fiscal_status") or "").strip().lower()
    if status not in ("pending", "accepted", "failed"):
        return _err("validation_error", "fiscal_status: pending, accepted или failed.",
                    fields={"fiscal_status": ["pending, accepted или failed."]})
    vals = {}
    for f, mx in TEXT_FIELDS.items():
        if f in data:
            v = "" if data.get(f) is None else str(data.get(f)).strip()
            if len(v) > mx:
                return _err("validation_error", f"{f}: не длиннее {mx} символов.", fields={f: [f"Не длиннее {mx}."]})
            vals[f] = v
    at = None
    if data.get("fiscal_at"):
        at = parse_datetime(str(data.get("fiscal_at")))
        if at is None:
            return _err("validation_error", "fiscal_at: ISO-8601 с часовым поясом.", fields={"fiscal_at": ["ISO-8601."]})
        if timezone.is_naive(at):
            at = timezone.make_aware(at)
    if status == "accepted" and not (vals.get("fiscal_doc_number") or obj.fiscal_doc_number):
        return _err("validation_error", "Для accepted нужен fiscal_doc_number.",
                    fields={"fiscal_doc_number": ["Обязательно для accepted."]})

    if is_locked(obj):
        same = status == "accepted" and all(
            (vals[f] if f in vals else obj_val) == obj_val
            for f in KEY_FIELDS for obj_val in [getattr(obj, f) or ""])
        if same:
            return Response({**fiscal_payload(obj), "replayed": True}, status=200)
        return _err("fiscal_already_set", "У чека уже записаны другие фискальные реквизиты.", 409, current=fiscal_payload(obj))

    obj.fiscal_status = status
    for f, v in vals.items():
        setattr(obj, f, v)
    if status == "accepted":
        obj.fiscal_at = at or obj.fiscal_at or timezone.now()
        obj.fiscal_error = vals.get("fiscal_error", "")
    elif at is not None:
        obj.fiscal_at = at
    obj.save(update_fields=["fiscal_status", "fiscal_at", *TEXT_FIELDS])
    return Response({**fiscal_payload(obj), "replayed": False}, status=200)


class SaleFiscalAPIView(APIView):
    """POST pos/sales/<id>/fiscal/ {fiscal_status, fiscal_doc_number, fiscal_memory_number, kkm_reg_number,
    fiscal_sign, fiscal_qr_url, fiscal_at, kkm_shift_id, fiscal_error}."""

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk):
        from apps.main.models import Sale

        company = _company(request.user)
        sale = get_object_or_404(Sale.objects.select_for_update(), pk=pk, company=company)
        if fiscal_mode(company) == "server" and (getattr(sale, "fiscal_status", None) or "none") != "pending":
            # p0114: чек, который касса начала пробивать до включения серверной eKassa (pending), касса дописывает
            return _err("fiscal_mode_server", "Чеки этой компании пробивает сервер (eKassa), касса их не пишет.", 409)
        if sale.status == Sale.Status.NEW:
            return _err("sale_not_completed", "Чек ещё не проведён.", 409)
        return _apply(request, sale)


class ReturnFiscalAPIView(APIView):
    """POST pos/returns/<id>/fiscal/ — то же для возврата."""

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk):
        from apps.main.models import SaleReturn

        company = _company(request.user)
        ret = get_object_or_404(SaleReturn.objects.select_for_update(), pk=pk, company=company)
        if fiscal_mode(company) == "server" and (getattr(ret, "fiscal_status", None) or "none") != "pending":
            # p0114: чек, который касса начала пробивать до включения серверной eKassa (pending), касса дописывает
            return _err("fiscal_mode_server", "Чеки этой компании пробивает сервер (eKassa), касса их не пишет.", 409)
        if getattr(ret, "status", "") == "voided":
            return _err("return_voided", "Возврат аннулирован.", 409)
        return _apply(request, ret)


def return_locked_response(pk):
    """Для правки/аннулирования возврата: 409 fiscal_locked, если возврат пробит на ККМ."""
    from apps.main.models import SaleReturn

    r = SaleReturn.objects.filter(pk=pk).only("fiscal_status", "fiscal_doc_number", "sale_id").first()
    if r is not None and is_locked(r):
        return locked_response(r, "Возврат")
    # ek1411: возврат, пробитый сервером через eKassa (Sale.ekassa_fiscal["returns"][<id>])
    if r is not None and r.sale_id:
        from apps.main.models import Sale

        s = Sale.objects.filter(pk=r.sale_id).only("ekassa_fiscal").first()
        em = (((s.ekassa_fiscal if s else None) or {}).get("returns") or {}).get(str(pk)) or {}
        if em.get("status") == "ok":
            fd = em.get("fd_number")
            return _err("fiscal_locked", f"Возврат пробит в eKassa (ФД {fd}): менять и аннулировать нельзя. "
                        "Если вернули не всё — оформите ещё один возврат.", 409, fiscal_doc_number=fd)
        if em.get("status") == "pending":
            return _err("fiscal_pending", "Чек возврата сейчас отправляется в eKassa. Подождите минуту и повторите.", 409)
    return None


# ---------------------------------------------------------------- налоги компании

def _is_owner_like(user):
    from apps.main.pos_serializers import _is_owner_like as f

    return f(user)


class FiscalSettingsAPIView(APIView):
    """GET settings/fiscal/ — всем сотрудникам; PUT/PATCH — владелец/админ."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(settings_payload(_company(request.user)))

    def put(self, request):
        company = _company(request.user)
        if not _is_owner_like(request.user):
            return _err("permission_denied", "Налоги компании меняет владелец.", 403, permission="owner")
        data = request.data if hasattr(request.data, "get") else {}
        st = settings_of(company)
        if "fiscal_enabled" in data:
            st.fiscal_enabled = data.get("fiscal_enabled") in (True, "true", "True", "1", 1)
        if "vat_payer" in data:
            st.vat_payer = data.get("vat_payer") in (True, "true", "True", "1", 1)
        if "tax_regime" in data:
            v = str(data.get("tax_regime") or "").strip()
            if v and v not in TAX_REGIMES:
                return _err("validation_error", "tax_regime: один из кодов " + ", ".join(TAX_REGIMES) + " или пусто.",
                            fields={"tax_regime": [", ".join(TAX_REGIMES)]}, allowed=list(TAX_REGIMES))
            st.tax_regime = v
        if "nsp_rate_cash" in data and "nsp_rate" not in data:
            data = {**data, "nsp_rate": data.get("nsp_rate_cash")}
        for f in ("nsp_rate", "nsp_rate_cashless"):
            if f in data:
                v = data.get(f)
                if v in (None, ""):
                    st.nsp_rate = None
                else:
                    try:
                        d = Decimal(str(v).replace(",", "."))
                    except InvalidOperation:
                        return _err("validation_error", f"{f}: число.", fields={f: ["Число."]})
                    if not (Decimal(0) <= d <= Decimal(100)):
                        return _err("validation_error", f"{f}: от 0 до 100.", fields={f: ["От 0 до 100."]})
                    st.nsp_rate = d.quantize(Decimal("0.01"))
        if "vat_rates" in data:
            raw = data.get("vat_rates") or []
            try:
                rates = sorted({Decimal(str(x).replace(",", ".")).quantize(Decimal("0.01")) for x in raw})
            except (InvalidOperation, TypeError):
                return _err("validation_error", "vat_rates: список чисел.", fields={"vat_rates": ["Список чисел."]})
            if any(not (Decimal(0) <= r <= Decimal(100)) for r in rates):
                return _err("validation_error", "vat_rates: от 0 до 100.", fields={"vat_rates": ["От 0 до 100."]})
            st.vat_rates = [format(r, "f") for r in rates]
        rule = TAX_REGIMES.get(st.tax_regime)
        if rule and rule["vat"] == "zero" and st.vat_payer:
            return _err("tax_regime_conflict", "При этом режиме НДС не платят: снимите «Плательщик НДС» или выберите общий режим.",
                        fields={"vat_payer": ["Не для этого режима."]}, tax_regime=st.tax_regime)
        st.save()
        try:  # касса узнаёт о смене налогов из ленты main/changes/ (app_settings_changed, app=fiscal)
            from apps.main.change_feed import _emit
            _emit(company.pk, "app_settings_changed", actor_id=request.user.pk, entity="app_settings", entity_id="fiscal",
                  data={"app": "fiscal", "version": None, "keys": ["fiscal"]})
        except Exception:  # noqa: BLE001
            pass
        return Response(settings_payload(company))

    patch = put


# ---------------------------------------------------------------- отчёт

class SalesWithoutFiscalAPIView(APIView):
    """GET reports/sales-without-fiscal/?from=YYYY-MM-DD&to=YYYY-MM-DD[&page=&page_size=]
    Проведённые чеки, у которых нет принятого чека ККМ (fiscal_status != accepted)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import Sale
        from apps.users.permissions_flags import sees_all_sales

        company = _company(request.user)
        qp = request.query_params
        d_from, d_to = parse_date(qp.get("from") or ""), parse_date(qp.get("to") or "")
        if d_from is None or d_to is None:
            return _err("validation_error", "Укажите from и to (YYYY-MM-DD).", fields={"from": ["YYYY-MM-DD."], "to": ["YYYY-MM-DD."]})
        if (d_to - d_from).days > 92:
            return _err("validation_error", "Период не длиннее 92 дней.", fields={"to": ["Не длиннее 92 дней."]})
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(d_from, time.min), tz)
        end = timezone.make_aware(datetime.combine(d_to, time.max), tz)
        qs = (Sale.objects.filter(company=company, created_at__gte=start, created_at__lte=end)
              .exclude(status__in=[Sale.Status.NEW, Sale.Status.CANCELED])
              .exclude(fiscal_status="accepted"))
        if not sees_all_sales(request.user):
            qs = qs.filter(user_id=request.user.pk)
        try:
            page = max(1, int(qp.get("page") or 1))
            size = min(500, max(1, int(qp.get("page_size") or 100)))
        except ValueError:
            return _err("validation_error", "page и page_size — целые числа.")
        from django.db.models import Count, Sum

        agg = qs.aggregate(n=Count("id"), s=Sum("total"))
        rows = (qs.order_by("created_at", "id").prefetch_related("payments")[(page - 1) * size: page * size])
        results = [{
            "id": str(s.id), "number": s.doc_number, "created_at": s.created_at.isoformat(),
            "status": s.status, "total": format(s.total or 0, "f"), "payment_method": s.payment_method,
            "payments": [{"method": p.method, "amount": format(p.amount or 0, "f")} for p in s.payments.all()],
            **fiscal_payload(s),
        } for s in rows]
        return Response({"from": str(d_from), "to": str(d_to), "count": agg["n"] or 0,
                         "total": format(agg["s"] or Decimal("0.00"), "f"), "page": page, "page_size": size,
                         "results": results})
