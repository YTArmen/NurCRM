"""20.11.2026 fis2011: сведения о ККМ для кассы и программы владельца (любой сотрудник компании, только чтение).

GET /api/ekassa/status/            — режим, ККМ (ЗН/РН/ФМ/срок ФМ/ОФД), смена ККМ, последний пробитый чек, проблемные чеки.
GET /api/ekassa/status/?x_report=1 — плюс X-отчёт текстом (только при открытой смене ККМ; иначе x_report=null).
    &length=25..50 — ширина текста X-отчёта (по умолчанию 32).
Сведения ККМ кэшируются на 60 с (?refresh=1 — сразу из eKassa).
"""
import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ekassa.exceptions import EkassaAPIError, EkassaConfigurationError
from apps.ekassa.services import client_for, get_integration

logger = logging.getLogger(__name__)
CACHE_TTL = 60


def _company(user):
    c = getattr(user, "owned_company", None) or getattr(user, "company", None)
    if c:
        return c
    br = getattr(user, "branch", None)
    return getattr(br, "company", None) if br is not None else None


def _pos_info(company, cfg, refresh=False):
    key = f"ekassa:status_pos:{company.pk}:{cfg.fiscal_number.strip()}"
    if not refresh:
        hit = cache.get(key)
        if hit is not None:
            return hit, None
    try:
        r = client_for(company).request_json(
            "POST", "/api/get_pos_by_fiscal_number", json_body={"fiscal_number": cfg.fiscal_number.strip()})
    except EkassaConfigurationError as e:
        return None, {"code": "ekassa_disabled", "detail": str(e) or "Интеграция eKassa не настроена."}
    except EkassaAPIError as e:
        return None, {"code": "ekassa_upstream", "detail": str(e), "ekassa": e.payload}
    except Exception as e:  # сеть и т.п.
        logger.warning("ekassa status get_pos failed company=%s: %s", company.pk, e)
        return None, {"code": "ekassa_unreachable", "detail": "Не удалось связаться с eKassa."}
    data = (r or {}).get("data") or {}
    cache.set(key, data, CACHE_TTL)
    return data, None


def _kkm(d):
    addr = ", ".join(x for x in (d.get("administrativearea"), d.get("locality"), d.get("streetnumber"), d.get("route")) if x)
    return {
        "serial_number": d.get("kkmserialnumber"),            # ЗН
        "reg_number": d.get("kkmfiscalnumber"),               # РН ККМ
        "fm_number": d.get("fnregistrationnumber"),           # ФМ
        "fm_registered_at": d.get("fnregistrationdate"),
        "fm_expires_at": d.get("fnexpiresat"),                # срок ФМ
        "fm_status": d.get("fnregistrationprogress"),         # READY
        "kkm_registered_at": d.get("kkmregistrationdate"),
        "kkm_status": d.get("kkmregistrationprogress"),       # DONE
        "kkm_fail_reason": d.get("kkmfailreason"),
        "ofd_status": d.get("kkmofdstatus"),                  # DONE
        "model": d.get("ccrmodel"), "model_version": d.get("ccrversion"),
        "blocked": bool(d.get("block_status")),
        "unregistered": bool(d.get("kkmunreg")),
        "service_title": d.get("service_title"),
        "service_suspended_at": d.get("service_suspended_at"),  # до какого числа оплачен тариф eKassa
        "org_name": d.get("name"), "tin": d.get("tin"), "store_name": d.get("storename"),
        "cashier_name": d.get("cashiername"), "address": addr or None,
        "tax_system": d.get("taxsystems"), "is_vat": bool(d.get("is_vat")), "max_sum": d.get("maxsum"),
    }


def _shift(d):
    is_open = str(d.get("shift_state")) == "1"
    return {
        "open": is_open,
        "number": d.get("shift"),
        "opened_at": d.get("shift_date"),
        "receipts_income": d.get("shift_reciept_prihod"),
        "cash_income": d.get("shift_cash_prihod"),        # в тыйынах, как в eKassa
        "bank_income": d.get("shift_bank_prihod"),
        "receipts_income_return": d.get("shift_reciept_prihod_vozvrat"),
        "cash_income_return": d.get("shift_cash_prihod_vozvrat"),
        "bank_income_return": d.get("shift_bank_prihod_vozvrat"),
    }


def _receipts(company):
    from apps.main.models import Sale

    since = timezone.now() - timedelta(days=60)
    base = Sale.objects.filter(company=company, created_at__gte=since)
    last = (base.filter(ekassa_fiscal__status="ok").order_by("-created_at")
            .only("id", "created_at", "ekassa_fiscal").first())
    last_out = None
    if last:
        m = last.ekassa_fiscal or {}
        last_out = {"sale_id": str(last.id), "created_at": last.created_at.isoformat(), "fd_number": m.get("fd_number"),
                    "fpd": m.get("fpd"), "link": m.get("link")}
    bad = list(base.filter(ekassa_fiscal__status__in=["error", "pending"]).order_by("-created_at")
               .only("id", "created_at", "ekassa_fiscal")[:20])
    return last_out, {
        "count": base.filter(ekassa_fiscal__status__in=["error", "pending"]).count(),
        "items": [{"sale_id": str(s.id), "created_at": s.created_at.isoformat(),
                   "status": (s.ekassa_fiscal or {}).get("status"), "message": (s.ekassa_fiscal or {}).get("message")}
                  for s in bad],
        "retry": "POST /api/ekassa/retry/ (владелец)",
    }


class EkassaStatusAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.main.fiscal_kassa import fiscal_mode

        company = _company(request.user)
        if company is None:
            return Response({"detail": "Компания для текущего пользователя не найдена."}, status=404)
        mode = fiscal_mode(company)
        cfg = get_integration(company)
        out = {"mode": mode, "connected": False, "configured": bool(cfg and cfg.is_ready()),
               "kkm": None, "shift": None, "last_receipt": None, "problems": None, "x_report": None, "error": None,
               "checked_at": timezone.now().isoformat()}
        if mode != "server" or not out["configured"]:
            return Response(out)
        qp = request.query_params
        d, err = _pos_info(company, cfg, refresh=qp.get("refresh") in ("1", "true"))
        out["last_receipt"], out["problems"] = _receipts(company)
        if err:
            out["error"] = err
            return Response(out)
        out["connected"] = True
        out["kkm"] = _kkm(d)
        out["shift"] = _shift(d)
        if qp.get("x_report") in ("1", "true") and out["shift"]["open"]:
            try:
                length = max(25, min(int(qp.get("length") or 32), 50))
            except (TypeError, ValueError):
                length = 32
            try:
                r = client_for(company).request_json("POST", "/api/shift_state_by_fiscal_number", json_body={
                    "fiscal_number": cfg.fiscal_number.strip(), "txt": True, "length": length})
                rd = (r or {}).get("data") or {}
                out["x_report"] = {"txt": rd.get("txt"), "fields": rd.get("fields")}
            except EkassaAPIError as e:
                out["x_report"] = {"error": str(e), "ekassa": e.payload}
        return Response(out)
