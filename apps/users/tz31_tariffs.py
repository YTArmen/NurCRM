"""
ТЗ кассы 05.11 п.32 / Б-39: тариф «Своё» — прайс, расчёт, заявка.

  GET  /api/public/tariffs/                  — прайс без входа (кэш 10 минут)
  POST /api/users/company/tariff/quote/      — расчёт {"packs": [...], "period": "month"}
  POST /api/users/company/tariff/custom/     — заявка (Idempotency-Key); только запись для менеджера

Цены пакетов лежат в таблице TariffPack (админка), цены start/standard — в SubscriptionPlan.
Заявка НЕ переводит компанию на план custom и НЕ включает пакеты (решение 08.11: до выхода кассы 1.17.61).
"""
from decimal import Decimal

from django.core.cache import cache
from django.db import transaction
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from apps.users.models import SubscriptionPlan, TariffCustomOrder, TariffPack
from apps.users.services_subscription import is_user_owner, resolve_plan_code
from core.exceptions import ApiError

CACHE_KEY = "public:tariffs:v4"  # pub2111: +mini, +trial_days
CACHE_TTL = 600
PERIODS = ("month", "year")  # trf2111: год = 10 месяцев
YEAR_PAID_MONTHS = 10
NEW_COMPANY_TRIAL_DAYS = 10  # pub2111: как в Company.save()
MINI_TRIAL_DAYS = 14  # trf2111: решение владельца 21.11
START_INCLUDES = ["pos", "stock"]

# trf2111b: имена тарифов на 5 языках (одно имя «Своё» везде — и в public/tariffs, и в capabilities)
PLAN_NAMES = {
    "mini": {"ru": "Мини", "ky": "Мини", "en": "Mini", "tr": "Mini", "uz": "Mini"},
    "start": {"ru": "Старт", "ky": "Старт", "en": "Start", "tr": "Başlangıç", "uz": "Start"},
    "standard": {"ru": "Стандарт", "ky": "Стандарт", "en": "Standard", "tr": "Standart", "uz": "Standart"},
    "custom": {"ru": "Своё", "ky": "Өзүңүздүкү", "en": "Custom", "tr": "Özel", "uz": "O'zingizniki"},
}


def _money(value) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _plan_price(code):
    """Цена плана из существующих записей SubscriptionPlan (None, если такого плана нет)."""
    for plan in SubscriptionPlan.objects.order_by("price"):
        if resolve_plan_code(plan) == code:
            return plan, _money(plan.price)
    return None, None


def build_public_tariffs() -> dict:
    start_plan, start_price = _plan_price("start")
    std_plan, std_price = _plan_price("standard")
    plans = [
        {"code": "start", "name": {"ru": start_plan.name if start_plan else "Старт"}, "price": start_price,
         "period": "month", "includes": START_INCLUDES},
        {"code": "standard", "name": {"ru": std_plan.name if std_plan else "Стандарт"}, "price": std_price,
         "period": "month", "includes": "all"},
        {"code": "custom", "base": "start", "name": {"ru": "Своё"}},
    ]
    # pub2111 (21.11.2026, просьба Бекназара): тариф «Мини» (только телефон) и пробный срок.
    # Пробный срок новой компании один для всех планов: Company.save() ставит end_date = start + 10 дней.
    mini_plan, mini_price = _plan_price("mini")
    if mini_plan is not None:
        plans.insert(0, {"code": "mini", "name": {"ru": mini_plan.name or "Мини"}, "price": mini_price,
                         "period": "month", "includes": ["pos"], "allowed_clients": ["mobile"],
                         "limits": {"max_staff": 1, "max_devices": 1, "max_cashboxes": 1}})
    for pl in plans:
        pl["name"] = dict(PLAN_NAMES.get(pl["code"], pl["name"]))  # trf2111b
        if pl["code"] != "custom":
            pl["trial_days"] = MINI_TRIAL_DAYS if pl["code"] == "mini" else NEW_COMPANY_TRIAL_DAYS
            pl["year"] = ({"months": 12, "paid_months": YEAR_PAID_MONTHS,
                           "price": _money(Decimal(pl["price"]) * YEAR_PAID_MONTHS)} if pl.get("price") else None)
    packs = [
        {
            "code": p.code, "name": p.name or {}, "description": p.description or {}, "group": p.group,
            "activation_fee": _money(p.activation_fee), "monthly_fee": _money(p.monthly_fee),
            "included_in": list(p.included_in or []), "trial_days": p.trial_days,
        }
        for p in TariffPack.objects.filter(is_active=True)
    ]
    return {"currency": "KGS", "plans": plans, "packs": packs}


class PublicTariffsAPIView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        data = None
        try:
            data = cache.get(CACHE_KEY)
        except Exception:
            pass
        if data is None:
            data = build_public_tariffs()
            try:
                cache.set(CACHE_KEY, data, CACHE_TTL)
            except Exception:
                pass
        resp = Response(data)
        resp["Cache-Control"] = f"public, max-age={CACHE_TTL}"
        return resp


# ---------------------------------------------------------------- расчёт

def _company_of(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _owner_company(request):
    company = _company_of(request.user)
    if company is None:
        raise ApiError("permission_denied", "Пользователь не привязан к компании.", status_code=403)
    if not is_user_owner(request.user, company):
        raise ApiError("permission_denied", "Тариф меняет только владелец.", status_code=403)
    return company


def _active_pack_codes(company) -> set:
    from apps.users.serializers import company_feature_codes

    return set(company_feature_codes(company))


def calc_quote(company, raw_packs, period, modules=None, sphere=None, mode=None, remove=None) -> dict:
    if period not in PERIODS:
        raise ApiError("invalid", "period — month или year.", fields={"period": ["Допустимо: month, year."]})
    if not isinstance(raw_packs, list) or not all(isinstance(c, str) for c in raw_packs):
        raise ApiError("invalid", "packs — список кодов пакетов.", fields={"packs": ["Список кодов."]})
    codes = list(dict.fromkeys(c.strip().lower() for c in raw_packs if c.strip()))
    plan_code = resolve_plan_code(getattr(company, "subscription_plan", None))
    # tariff8: на «Стандарте» больше не 409 — пакеты включены (already_active), считаем модули
    known = {p.code: p for p in TariffPack.objects.filter(is_active=True)}
    unknown = [c for c in codes if c not in known]
    if unknown:
        raise ApiError("pack_unknown", "Неизвестный пакет: " + ", ".join(unknown), data={"packs": unknown},
                       fields={"packs": ["Неизвестный пакет."]})
    active = _active_pack_codes(company)
    # trf2111b: на «Стандарте» пакеты included_in:[standard] уже входят в цену — already_active, без двойной оплаты
    if plan_code == "standard":
        active |= {c for c, p in known.items() if "standard" in (p.included_in or [])}
    # trf2111b: remove — снять пакеты «Стандарта». Тогда тариф становится «Своё»: «Старт» + оставшиеся пакеты
    # по их цене + модули. Решение владельца 21.11: «Своё» дороже «Стандарта».
    if remove is not None and (not isinstance(remove, list) or not all(isinstance(c, str) for c in remove)):
        raise ApiError("invalid", "remove — список кодов пакетов.", fields={"remove": ["Список кодов."]})
    removed = list(dict.fromkeys(c.strip().lower() for c in (remove or []) if c.strip()))
    bad = [c for c in removed if c not in known]
    if bad:
        raise ApiError("pack_unknown", "Неизвестный пакет: " + ", ".join(bad), data={"packs": bad},
                       fields={"remove": ["Неизвестный пакет."]})
    custom_from_standard = plan_code == "standard" and bool(removed)
    rows, monthly, once = [], Decimal("0"), Decimal("0")
    if custom_from_standard:
        keep = [c for c, p in known.items() if "standard" in (p.included_in or []) and c not in removed]
        codes = list(dict.fromkeys(keep + [c for c in codes if c not in removed]))
        active = set(_active_pack_codes(company)) - {c for c, p in known.items() if "standard" in (p.included_in or [])}
    for code in codes:
        p = known[code]
        is_active = code in active
        m = Decimal("0") if is_active else p.monthly_fee
        o = Decimal("0") if is_active else p.activation_fee
        monthly += m
        once += o
        rows.append({"code": code, "monthly": _money(m), "once": _money(o), "already_active": is_active})
    _, base_price = _plan_price("start")
    _, std_price = _plan_price("standard")
    cheaper = False
    if std_price is not None:
        cheaper = Decimal(base_price or 0) + monthly > Decimal(std_price)
    # tariff8: модули и итог (старые поля выше не меняем)
    from apps.users.tariff_modules import module_quote

    mq = module_quote(company, modules, sphere, mode)
    base_plan = "standard" if plan_code == "standard" and not custom_from_standard else "start"
    base_plan_price = std_price if base_plan == "standard" else base_price
    total_month = Decimal(base_plan_price or 0) + monthly + mq["extra_month"]
    return {
        "monthly": _money(monthly), "once": _money(once), "base": base_plan_price, "packs": rows,
        "start_price": base_price,  # trf2111b: base теперь — цена базового плана (раньше всегда «Старт»)
        "plan_after": "custom" if (custom_from_standard or (plan_code != "standard" and rows)) else plan_code,
        "removed": removed,
        "standard_cheaper": cheaper, "standard_price": std_price,
        "base_plan": base_plan, "base_price": base_plan_price, "sphere": mq["sphere"], "modules": mq["modules"],
        "included": mq["included"], "extra": mq["extra"], "extra_month": _money(mq["extra_month"]),
        "total_month": _money(total_month), "total_once": _money(once),
        # trf2111: год = 12 месяцев за цену YEAR_PAID_MONTHS
        "period": period,
        "total_year": _money(total_month * YEAR_PAID_MONTHS), "year_months": 12, "year_paid_months": YEAR_PAID_MONTHS,
        "year_discount": _money(total_month * (12 - YEAR_PAID_MONTHS)),
        "trial": mq.get("trial", []),
    }


class TariffQuoteAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _owner_company(request)
        data = request.data if isinstance(request.data, dict) else {}
        return Response(calc_quote(company, data.get("packs") or [], data.get("period") or "month",
                                   data.get("modules"), data.get("sphere"), data.get("mode"), data.get("remove")))


class TariffCustomAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @idempotent(body_key=True, replay_flag=False)
    def post(self, request):
        company = _owner_company(request)
        data = request.data if isinstance(request.data, dict) else {}
        period = data.get("period") or "month"
        quote = calc_quote(company, data.get("packs") or [], period, data.get("modules"), data.get("sphere"),
                           data.get("mode"), data.get("remove"))  # trf2111b: modules[] и remove[] тоже
        packs = [r["code"] for r in quote["packs"] if not r["already_active"]]
        if not packs:
            raise ApiError("invalid", "Выберите хотя бы один ещё не подключённый пакет.", fields={"packs": ["Пусто."]})
        with transaction.atomic():
            order = TariffCustomOrder.objects.create(
                company=company, created_by=request.user, packs=packs, period=period,
                amount_monthly=Decimal(quote["monthly"]), amount_once=Decimal(quote["once"]),
            )
        return Response(
            {"order_id": str(order.id), "status": order.status, "amount_monthly": quote["monthly"],
             "amount_once": quote["once"], "pay_url": None},
            status=201,
        )
