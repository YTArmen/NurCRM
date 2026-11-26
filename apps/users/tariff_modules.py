"""
tariff8 (20.11.2026, задача 8 от главного окна): «Своё» на «Стандарте» — модули в расчёте тарифа.

Каталог модулей и профили — apps/users/market_modules.py (главное окно, не менять здесь).
Здесь только цены и расчёт:
  * модули профиля выбранной сферы входят в тариф бесплатно (included);
  * модули из других сфер — extra, цена в месяц из MODULE_PRICES; разовой платы за модуль нет;
  * пакеты TariffPack считаются по-старому (tz31_tariffs.calc_quote).

GET    /api/public/modules/                       — каталог модулей с ценами (без входа)
DELETE /api/users/company/tariff/modules/{code}/  — владелец/админ убирает модуль (та же запись, что PUT company/modules/)
"""
from decimal import Decimal

from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ApiError

CURRENCY = "KGS"
_P100 = ("quick_items", "fractional_qty", "packages", "client_price", "condition", "storage_cell", "sto_price",
         "return_rules", "category_tiles", "volume_price", "haggle", "consultant", "exchange")
_P200 = ("scales_labels", "expiry_batches", "price_levels", "material_calc", "variants", "serials", "warranty",
         "part_numbers", "cross_analogs", "currencies", "customer_orders")
_P300 = ("estimate", "rental", "repair", "trade_in", "invoice_cargo")
MODULE_PRICES = {**{m: Decimal("100") for m in _P100}, **{m: Decimal("200") for m in _P200},
                 **{m: Decimal("300") for m in _P300}}
DEFAULT_PRICE = Decimal("200")  # модуль, который главное окно добавит в каталог раньше, чем сюда цену


def _money(v) -> str:
    return str(Decimal(str(v or 0)).quantize(Decimal("0.01")))


def price_of(module_id) -> Decimal:
    return MODULE_PRICES.get(module_id, DEFAULT_PRICE)


def _mm():
    from apps.users import market_modules as mm
    return mm


def resolve_profile(sphere, mode=None):
    mm = _mm()
    if sphere in (None, ""):
        return None
    p = str(sphere).strip().lower()
    if p == "bazaar":
        p = "bazaar_wholesale" if mode == "wholesale" else "bazaar_retail"
    p = mm.ALIASES.get(p, p)
    if p not in mm.PROFILES:
        raise ApiError("profile_unknown", "Неизвестная сфера (профиль магазина).", fields={"sphere": list(mm.PROFILES)})
    return p


def module_quote(company, raw_modules=None, sphere=None, mode=None) -> dict:
    """{sphere, modules, included, extra, extra_month}; модули по умолчанию — текущие у компании."""
    mm = _mm()
    st = mm.state(company)
    profile = resolve_profile(sphere, mode) or st["profile"]
    if raw_modules is None:
        mods = list(st["enabled"]) if profile == st["profile"] else list(mm.PROFILES[profile][2])
    else:
        if not isinstance(raw_modules, list) or not all(isinstance(m, str) for m in raw_modules):
            raise ApiError("invalid", "modules — список кодов модулей.", fields={"modules": ["Список кодов."]})
        mods = list(dict.fromkeys(m.strip().lower() for m in raw_modules if m.strip()))
        bad = [m for m in mods if m not in mm.MODULES]
        if bad:
            raise ApiError("module_unknown", "Неизвестный модуль: " + ", ".join(bad), data={"modules": bad},
                           fields={"modules": ["Неизвестный модуль."]})
    defaults = set(mm.PROFILES[profile][2])
    trial_now = st.get("trial", {}) if profile == st["profile"] else {}  # trf2111
    included, extra, total = [], [], Decimal("0")
    trial = []
    for m in mods:
        name = mm.MODULES[m][0]
        if m in trial_now and raw_modules is None:
            trial.append({"id": m, "name": name, "until": trial_now[m], "monthly": _money(price_of(m))})
            continue
        if m in defaults:
            included.append({"id": m, "name": name})
        else:
            p = price_of(m)
            total += p
            extra.append({"id": m, "name": name, "monthly": _money(p)})
    return {"sphere": profile, "modules": mods, "included": included, "extra": extra, "extra_month": total,
            "trial": trial}


def public_modules() -> dict:
    mm = _mm()
    return {
        "currency": CURRENCY,
        "modules": [{"id": k, "name": {"ru": ru, "en": en}, "group": g, "monthly": _money(price_of(k)), "once": "0.00",
                     "activation_fee": "0.00", "trial_days": 7,  # trf2111  # pub2111: разовой платы нет, пробного у модулей пока нет
                     "default_in": [p for p, v in mm.PROFILES.items() if k in v[2]]}
                    for k, (ru, en, g) in mm.MODULES.items()],
        "profiles": [{"id": k, "name": {"ru": ru, "en": en}, "modules": list(mods)}
                     for k, (ru, en, mods) in mm.PROFILES.items()],
    }


class PublicModulesAPIView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        resp = Response(public_modules())
        resp["Cache-Control"] = "public, max-age=300"
        return resp


class TariffModuleDeleteAPIView(APIView):
    """DELETE users/company/tariff/modules/{code}/ — то же, что PUT company/modules/ {"disable": [code]}."""
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, code):
        mm = _mm()
        code = (code or "").strip().lower()
        if code not in mm.MODULES:
            raise ApiError("module_unknown", "Неизвестный модуль.", status_code=404, data={"module": code})
        company = mm._company(request.user)
        if company is None:
            raise ApiError("no_company", "Пользователь не привязан к компании.", status_code=403)
        u = request.user
        if not (getattr(company, "owner_id", None) == u.id or getattr(u, "role", "") in ("owner", "admin")
                or getattr(u, "is_superuser", False)):
            raise ApiError("permission_denied", "Модули меняет владелец или администратор.", status_code=403)
        if code not in mm.state(company)["enabled"]:
            raise ApiError("module_not_enabled", "Этот модуль и так выключен.", status_code=409, data={"module": code})
        if_match = (request.headers.get("If-Match") or "").strip().strip('"') or None
        body, err = mm.apply_change(company, u, disable=[code], if_match=if_match)
        if err:
            ecode, detail, status, extra = err
            return Response({"code": ecode, "detail": detail, **(extra or {})}, status=status)
        return Response(body)
