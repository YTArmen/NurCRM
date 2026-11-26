"""
Модули Маркетплейса для любой сферы (mod2011, 20.11.2026, ТЗ Бекназара «функции-модули для любой сферы»).

Профиль магазина (сфера мастера кассы) задаёт набор модулей по умолчанию; клиент добавляет любой модуль
из другой сферы или убирает ненужный. market_sphere (grocery/clothing/services…) не трогаем — это база.

Хранение без миграции: CompanyAppSettings(company, branch=None, app="market-modules").settings =
    {"profile": "stroymag", "added": [...], "removed": [...]}, версия — CompanyAppSettings.version.

GET  /api/users/company/modules/   → {profile, profile_base, mode, version, modules: [id…], items: [...], catalog: [...], profiles: [...]}
PUT  /api/users/company/modules/   {profile?, mode?, enable?: [id], disable?: [id], modules?: [id] (полная замена)}
     владелец/админ; If-Match: <version> (по желанию) → 409 version_conflict. Ответ — как GET.
В capabilities: "modules": {profile, version, enabled: [...]}; событие ленты features_changed {modules_version, profile}.

Модули — это набор экранов и поведения касс/владельца/мобилки. Платные функции кассы (kassa-*)
и пакеты тарифа ими НЕ включаются — они по-прежнему через CompanyAddon (оплата/поддержка).
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

APP = "market-modules"

# id: (ru, en, группа)
MODULES: dict[str, tuple[str, str, str]] = {
    "scales_labels": ("Весы с этикетками", "Scales and labels", "grocery"),
    "expiry_batches": ("Сроки годности и партии", "Expiry dates and batches", "grocery"),
    "quick_items": ("Быстрые товары", "Quick items", "grocery"),
    "fractional_qty": ("Дробное количество (шаг, минимум)", "Fractional quantity (step, minimum)", "hozmag"),
    "packages": ("Упаковки", "Packages", "hozmag"),
    "client_price": ("Цена по клиенту", "Client price", "hozmag"),
    "price_levels": ("Уровни цен («прораб»)", "Price levels (foreman)", "stroymag"),
    "estimate": ("Смета", "Estimate", "stroymag"),
    "material_calc": ("Калькулятор материалов", "Material calculator", "stroymag"),
    "rental": ("Прокат", "Rental", "stroymag"),
    "variants": ("Размеры и цвета", "Sizes and colors", "clothing"),
    "consultant": ("Консультант", "Consultant", "clothing"),
    "exchange": ("Обмен", "Exchange", "clothing"),
    "serials": ("Серийные номера", "Serial numbers", "electronics"),
    "warranty": ("Гарантия", "Warranty", "electronics"),
    "repair": ("Ремонт", "Repair", "electronics"),
    "trade_in": ("Trade-in", "Trade-in", "electronics"),
    "part_numbers": ("Каталожный номер и OEM", "Part number and OEM", "autoparts"),
    "cross_analogs": ("Кроссы и аналоги", "Cross references and analogs", "autoparts"),
    "condition": ("Состояние (новое / б/у)", "Condition (new / used)", "autoparts"),
    "storage_cell": ("Ячейка хранения", "Storage cell", "autoparts"),
    "sto_price": ("Цена СТО", "Service-station price", "autoparts"),
    "return_rules": ("Правила возврата (электрика, установленные)", "Return rules", "autoparts"),
    "customer_orders": ("Заказ под клиента", "Customer orders", "autoparts"),
    "category_tiles": ("Быстрые плитки категорий", "Category tiles", "bazaar"),
    "volume_price": ("Цена по количеству", "Volume price", "bazaar"),
    "haggle": ("Торг", "Haggling", "bazaar"),
    "currencies": ("Курсы валют", "Currencies", "bazaar"),
    "invoice_cargo": ("Накладная и карго (опт)", "Invoice and cargo (wholesale)", "bazaar"),
}

PROFILES: dict[str, tuple[str, str, list[str]]] = {
    "grocery": ("Продуктовый", "Grocery", ["scales_labels", "expiry_batches", "quick_items"]),
    "hozmag": ("Хозмаг", "Household goods", ["fractional_qty", "packages", "client_price"]),
    "stroymag": ("Строймаг", "Building materials",
                 ["fractional_qty", "packages", "client_price", "price_levels", "estimate", "material_calc", "rental"]),
    "clothing": ("Магазин одежды", "Clothing", ["variants", "consultant", "exchange"]),
    "electronics": ("Магазин электроники", "Electronics", ["serials", "warranty", "repair", "trade_in"]),
    "bazaar_retail": ("Базар (розница)", "Bazaar (retail)", ["category_tiles", "volume_price", "haggle", "currencies"]),
    "bazaar_wholesale": ("Базар (опт)", "Bazaar (wholesale)",
                         ["category_tiles", "volume_price", "haggle", "currencies", "invoice_cargo"]),
    "autoparts": ("Автозапчасти", "Auto parts",
                  ["part_numbers", "cross_analogs", "condition", "storage_cell", "sto_price", "warranty",
                   "return_rules", "customer_orders"]),
    "stationery": ("Канцтовары", "Stationery", ["quick_items", "packages", "client_price"]),
    "services": ("Услуги", "Services", []),
}
ALIASES = {"hardware": "stroymag", "bazaar": "bazaar_retail"}
# Признаки компании, которые профиль включает сам (без ручного CompanyAddon)
PROFILE_FLAGS = {
    "hozmag": {"hardware_2026_11"},
    "stroymag": {"hardware_2026_11"},
    "electronics": {"electronics_2026_11"},
}


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _doc(company, lock=False):
    from apps.users.models import CompanyAppSettings

    qs = CompanyAppSettings.objects.filter(company=company, branch__isnull=True, app=APP)
    if lock:
        qs = qs.select_for_update()
    return qs.first()


def _default_profile(company) -> str:
    sphere = (getattr(company, "market_spheres", None) or [getattr(company, "market_sphere", None)] or [None])[0]
    if sphere in PROFILES:
        return sphere
    try:
        from core.api_compat import company_flag

        if company_flag(company, "electronics_2026_11"):
            return "electronics"
        if company_flag(company, "hardware_2026_11"):
            return "stroymag"
    except Exception:  # noqa: BLE001
        pass
    return "grocery"


def state(company) -> dict:
    """Действующее состояние: профиль, версия, список включённых модулей."""
    cache = getattr(company, "_nm_modules", None)
    if cache is not None:
        return cache
    doc = _doc(company) if company is not None else None
    s = dict(doc.settings or {}) if doc else {}
    profile = ALIASES.get(s.get("profile"), s.get("profile")) or (_default_profile(company) if company else "grocery")
    if profile not in PROFILES:
        profile = "grocery"
    added = [m for m in (s.get("added") or []) if m in MODULES]
    removed = {m for m in (s.get("removed") or []) if m in MODULES}
    enabled = [m for m in PROFILES[profile][2] if m not in removed] + [m for m in added if m not in PROFILES[profile][2]]
    # trf2111: пробные модули {code: "YYYY-MM-DD"} — включены до даты (включительно), в абонплату не входят
    from django.utils import timezone as _tz
    today = _tz.localdate().isoformat()
    trial = {m: d for m, d in (s.get("trials") or {}).items()
             if m in MODULES and m not in enabled and str(d) >= today}
    enabled = enabled + list(trial)
    res = {"profile": profile, "version": doc.version if doc else 0, "enabled": list(dict.fromkeys(enabled)),
           "added": added, "removed": sorted(removed), "trial": trial}
    try:
        company._nm_modules = res
    except AttributeError:
        pass
    return res


def profile_flags(company) -> set[str]:
    """Признаки от явно выбранного профиля (без профиля «по умолчанию» — иначе цикл с company_flag)."""
    try:
        doc = _doc(company)
        p = ALIASES.get((doc.settings or {}).get("profile"), (doc.settings or {}).get("profile")) if doc else None
        return set(PROFILE_FLAGS.get(p, ()))
    except Exception:  # noqa: BLE001
        return set()


def capabilities_block(company):
    if company is None:
        return None
    st = state(company)
    return {"profile": st["profile"], "version": st["version"], "enabled": st["enabled"]}


def catalog():
    return [{"id": k, "name": ru, "names": {"ru": ru, "en": en}, "group": g,
             "default_in": [p for p, v in PROFILES.items() if k in v[2]]} for k, (ru, en, g) in MODULES.items()]


def profiles():
    return [{"id": k, "name": ru, "names": {"ru": ru, "en": en}, "modules": mods,
             "base": "bazaar" if k.startswith("bazaar_") else k,
             "flags": sorted(PROFILE_FLAGS.get(k, ()))} for k, (ru, en, mods) in PROFILES.items()]


def payload(company):
    st = state(company)
    p = st["profile"]
    return {
        "profile": p, "profile_base": "bazaar" if p.startswith("bazaar_") else p,
        # trf2111b: owner — профиль выбрал владелец (PUT company/modules/), auto — сервер вывел сам (сфера/флаги)
        "profile_source": "owner" if (_doc(company) and (_doc(company).settings or {}).get("profile")) else "auto",
        "mode": p.split("_", 1)[1] if p.startswith("bazaar_") else None,
        "version": st["version"], "modules": st["enabled"], "added": st["added"], "removed": st["removed"],
        "items": [{"id": k, "enabled": k in st["enabled"],
                   "source": ("trial" if k in st.get("trial", {}) else
                              "default" if k in PROFILES[p][2] else "added") if k in st["enabled"]
                   else ("removed" if k in PROFILES[p][2] else "off"),
                   "trial_until": st.get("trial", {}).get(k)} for k in MODULES],
        "trial": st.get("trial", {}),  # trf2111
        "flags": sorted(PROFILE_FLAGS.get(p, ())),
        "catalog": catalog(), "profiles": profiles(),
    }


def _err(code, detail, status=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=status)


class CompanyModulesAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request.user)
        if company is None:
            return _err("no_company", "Пользователь не привязан к компании.", 403)
        return Response(payload(company))

    def put(self, request):
        from apps.users.models import CompanyAppSettings

        company = _company(request.user)
        if company is None:
            return _err("no_company", "Пользователь не привязан к компании.", 403)
        u = request.user
        if not (getattr(company, "owner_id", None) == u.id or getattr(u, "role", "") in ("owner", "admin")
                or getattr(u, "is_superuser", False)):
            return _err("permission_denied", "Модули меняет владелец или администратор.", 403)
        d = request.data if isinstance(request.data, dict) else {}
        profile = d.get("profile")
        if profile is not None:
            if profile == "bazaar":
                profile = "bazaar_wholesale" if d.get("mode") == "wholesale" else "bazaar_retail"
            profile = ALIASES.get(profile, profile)
            if profile not in PROFILES:
                return _err("profile_unknown", "Неизвестный профиль магазина.", fields={"profile": list(PROFILES)})
        lists = {}
        for key in ("enable", "disable", "modules"):
            v = d.get(key)
            if v is None:
                continue
            if not isinstance(v, list) or any(m not in MODULES for m in v):
                bad = [m for m in (v if isinstance(v, list) else [v]) if m not in MODULES]
                return _err("module_unknown", "Неизвестный модуль.", fields={key: bad})
            lists[key] = v
        with transaction.atomic():
            doc = _doc(company, lock=True)
            if doc is None:
                doc = CompanyAppSettings.objects.create(company=company, branch=None, app=APP, settings={}, version=0)
                doc = _doc(company, lock=True)
            if_match = (request.headers.get("If-Match") or "").strip().strip('"')
            if if_match and if_match != str(doc.version):
                return _err("version_conflict", "Модули изменили в другом месте. Обновите и повторите.", 409,
                            version=doc.version)
            try:
                del company._nm_modules
            except AttributeError:
                pass
            cur = state(company)
            p = profile or cur["profile"]
            defaults = PROFILES[p][2]
            if "modules" in lists:
                want = list(dict.fromkeys(lists["modules"]))
            else:
                want = [m for m in cur["enabled"] if m not in cur.get("trial", {})] if profile is None or profile == cur["profile"] else \
                    list(defaults) + [m for m in cur["added"] if m not in defaults]
                for m in lists.get("enable", []):
                    if m not in want:
                        want.append(m)
                want = [m for m in want if m not in set(lists.get("disable", []))]
            new = {"profile": p, "added": [m for m in want if m not in defaults],
                   "removed": [m for m in defaults if m not in want]}
            old = {k: (doc.settings or {}).get(k) for k in ("profile", "added", "removed")}
            if new != old:
                doc.settings = {**(doc.settings or {}), **new}
                doc.version = (doc.version or 0) + 1
                doc.updated_by = u
                doc.save(update_fields=["settings", "version", "updated_by", "updated_at"])
                for attr in ("_nm_modules", "_nm_flags"):
                    try:
                        delattr(company, attr)
                    except AttributeError:
                        pass
                _emit(company, doc.version, p, u)
        return Response(payload(company))

    patch = put


def _emit(company, version, profile, user):
    try:
        from apps.main.change_feed import record_change

        record_change(company.id, "features_changed", actor_id=getattr(user, "id", None), entity="modules",
                      entity_id="modules", data={"modules_version": version, "profile": profile,
                                                 "enabled": state(company)["enabled"]})
    except Exception:  # noqa: BLE001
        pass

def apply_change(company, user, *, profile=None, enable=(), disable=(), modules=None, if_match=None):
    """Общая правка модулей (для PUT и для «Своё»: tariff/modules/<code>/). Возвращает (payload, None) или (None, (code, detail, status, extra))."""
    from apps.users.models import CompanyAppSettings

    if profile is not None:
        profile = ALIASES.get(profile, profile)
        if profile not in PROFILES:
            return None, ("profile_unknown", "Неизвестный профиль магазина.", 400, {"fields": {"profile": list(PROFILES)}})
    for key, v in (("enable", enable), ("disable", disable), ("modules", modules)):
        if v is not None and any(m not in MODULES for m in v):
            return None, ("module_unknown", "Неизвестный модуль.", 400, {"fields": {key: [m for m in v if m not in MODULES]}})
    with transaction.atomic():
        doc = _doc(company, lock=True)
        if doc is None:
            CompanyAppSettings.objects.create(company=company, branch=None, app=APP, settings={}, version=0)
            doc = _doc(company, lock=True)
        if if_match and str(if_match) != str(doc.version):
            return None, ("version_conflict", "Модули изменили в другом месте. Обновите и повторите.", 409, {"version": doc.version})
        for attr in ("_nm_modules",):
            try:
                delattr(company, attr)
            except AttributeError:
                pass
        cur = state(company)
        p = profile or cur["profile"]
        defaults = PROFILES[p][2]
        if modules is not None:
            want = list(dict.fromkeys(modules))
        else:
            want = [m for m in cur["enabled"] if m not in cur.get("trial", {})] if profile is None or profile == cur["profile"] else \
                list(defaults) + [m for m in cur["added"] if m not in defaults]
            for m in enable or ():
                if m not in want:
                    want.append(m)
            want = [m for m in want if m not in set(disable or ())]
        new = {"profile": p, "added": [m for m in want if m not in defaults],
               "removed": [m for m in defaults if m not in want]}
        old = {k: (doc.settings or {}).get(k) for k in ("profile", "added", "removed")}
        if new != old:
            doc.settings = {**(doc.settings or {}), **new}
            doc.version = (doc.version or 0) + 1
            doc.updated_by = user
            doc.save(update_fields=["settings", "version", "updated_by", "updated_at"])
            for attr in ("_nm_modules", "_nm_flags"):
                try:
                    delattr(company, attr)
                except AttributeError:
                    pass
            _emit(company, doc.version, p, user)
    return payload(company), None


TRIAL_DAYS = 7  # trf2111: решение владельца 21.11 — 7 дней пробного у модулей


def start_trial(company, user, code, days=TRIAL_DAYS):
    """trf2111: пробный модуль на days дней, один раз на модуль для компании.
    Возвращает (body, None) или (None, (code, detail, status))."""
    from datetime import timedelta

    from django.utils import timezone as _tz

    from apps.users.models import CompanyAppSettings, FeatureTrial

    if code not in MODULES:
        return None, ("module_unknown", "Неизвестный модуль.", 400)
    with transaction.atomic():
        doc = _doc(company, lock=True)
        if doc is None:
            CompanyAppSettings.objects.create(company=company, branch=None, app=APP, settings={}, version=0)
            doc = _doc(company, lock=True)
        for attr in ("_nm_modules", "_nm_flags"):
            try:
                delattr(company, attr)
            except AttributeError:
                pass
        cur = state(company)
        if code in cur["enabled"]:
            return None, ("already_active", "Модуль уже включён.", 409)
        if FeatureTrial.objects.select_for_update().filter(company=company, code=f"module:{code}").exists():
            return None, ("trial_used", "Пробный период этого модуля уже использован.", 409)
        expires = _tz.localdate() + timedelta(days=days)
        st = dict(doc.settings or {})
        st["trials"] = {**(st.get("trials") or {}), code: expires.isoformat()}
        doc.settings = st
        doc.version = (doc.version or 0) + 1
        doc.updated_by = user
        doc.save(update_fields=["settings", "version", "updated_by", "updated_at"])
        FeatureTrial.objects.create(company=company, code=f"module:{code}"[:64], expires_at=expires)
        for attr in ("_nm_modules", "_nm_flags"):
            try:
                delattr(company, attr)
            except AttributeError:
                pass
        _emit(company, doc.version, state(company)["profile"], user)
    return {"code": code, "kind": "module", "expires_at": expires.isoformat(), "modules_version": doc.version}, None
