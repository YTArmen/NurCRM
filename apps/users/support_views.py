"""
Конструктор тарифа «По договору» для support.nurcrm.kg (26.10.2026).

POST /api/users/support/company-features/
Заголовок X-Support-Token — общий секрет NURCRM_SUPPORT_TOKEN из .env (пусто — адрес выключен, 404).
{"email": "<владелец>", "codes": ["branches", ...], "until": "2027-10-26" | null,
 "max_employees": 5 | null, "price": 2500 | null}

Только для компаний на тарифе «По договору» (resolve_plan_code == "custom"). Выбранные функции
становятся CompanyAddon, снятые галочки выключаются; прочие платные функции компании не трогаются.
Отметка «constructor» говорит фронту и кассе: набор функций собран вручную, остальное закрыто.
"""
import hmac
import logging
import re

from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.utils.dateparse import parse_date
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CompanyAddon, User
from .services_subscription import resolve_plan_code

logger = logging.getLogger(__name__)

CONSTRUCTOR_CODE = "constructor"
# Функции, которые оператор отмечает галочками. Касса, склад, деньги и долги входят всегда.
CONSTRUCTOR_FEATURES = (
    "branches",
    "procurement",
    "clients",
    "analytics",
    "inventory-documents",
    "double-warehouse",
    "online-showcase",
    "barcode-print",
)
MAX_EMPLOYEES_LIMIT = 1000
_NOTE_MAX = re.compile(r"max_employees=(\d+)")


def constructor_max_employees(company):
    """Лимит сотрудников из конструктора; нет конструктора или лимита — None."""
    addon = CompanyAddon.objects.filter(company=company, code=CONSTRUCTOR_CODE).first()
    if addon is None or not addon.is_effective():
        return None
    m = _NOTE_MAX.search(addon.note or "")
    return int(m.group(1)) if m else None


def _token_ok(request):
    expected = getattr(settings, "NURCRM_SUPPORT_TOKEN", "") or ""
    if len(expected) < 32:
        return None  # адрес выключен
    given = request.headers.get("X-Support-Token") or ""
    return hmac.compare_digest(given.encode(), expected.encode())


class SupportCompanyFeaturesAPIView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        ok = _token_ok(request)
        if ok is None:
            raise Http404
        if not ok:
            return Response({"detail": "Неверный ключ поддержки."}, status=403)

        data = request.data if isinstance(request.data, dict) else {}
        errors = {}

        email = str(data.get("email") or "").strip()
        owner = User.objects.filter(email__iexact=email).select_related("owned_company__subscription_plan").first() if email else None
        company = getattr(owner, "owned_company", None) if owner else None
        if company is None:
            errors["email"] = "Владелец с таким email не найден."

        codes = data.get("codes")
        if not isinstance(codes, list) or any(not isinstance(c, str) for c in codes):
            errors["codes"] = "Нужен список кодов."
            codes = []
        unknown = sorted(set(codes) - set(CONSTRUCTOR_FEATURES))
        if unknown:
            errors["codes"] = "Неизвестные функции: " + ", ".join(unknown)

        until = None
        if data.get("until"):
            until = parse_date(str(data["until"]))
            if until is None:
                errors["until"] = "Дата в формате ГГГГ-ММ-ДД."

        max_employees = data.get("max_employees")
        if max_employees in ("", None):
            max_employees = None
        else:
            try:
                max_employees = int(max_employees)
            except (TypeError, ValueError):
                max_employees = -1
            if not 1 <= max_employees <= MAX_EMPLOYEES_LIMIT:
                errors["max_employees"] = f"Число от 1 до {MAX_EMPLOYEES_LIMIT}."

        price = data.get("price")
        if price in ("", None):
            price = None
        else:
            try:
                price = round(float(price), 2)
            except (TypeError, ValueError):
                price = -1
            if not 0 <= price <= 10_000_000:
                errors["price"] = "Цена — число от 0."

        if company is not None and resolve_plan_code(company.subscription_plan) != "custom":
            errors["email"] = "Конструктор только для тарифа «По договору»."

        if errors:
            return Response(errors, status=400)

        chosen = set(codes)
        note_parts = ["конструктор support"]
        if max_employees is not None:
            note_parts.append(f"max_employees={max_employees}")
        if price is not None:
            note_parts.append(f"price={price:g}")
        marker_note = "; ".join(note_parts)[:255]

        with transaction.atomic():
            existing = {
                a.code: a
                for a in CompanyAddon.objects.select_for_update().filter(
                    company=company, code__in=(*CONSTRUCTOR_FEATURES, CONSTRUCTOR_CODE)
                )
            }
            for code in (*CONSTRUCTOR_FEATURES, CONSTRUCTOR_CODE):
                on = code == CONSTRUCTOR_CODE or code in chosen
                addon = existing.get(code)
                if addon is None:
                    if not on:
                        continue
                    addon = CompanyAddon(company=company, code=code)
                addon.active = on
                addon.until = until if on else addon.until
                addon.note = marker_note if code == CONSTRUCTOR_CODE else "конструктор support"
                addon.save()

        logger.info(
            "support: конструктор компании %s: %s, до %s, сотрудников %s",
            company.id, sorted(chosen), until, max_employees,
        )
        return Response({
            "company": str(company.id),
            "codes": sorted(chosen),
            "until": until.isoformat() if until else None,
            "max_employees": max_employees,
            "price": price,
        })
