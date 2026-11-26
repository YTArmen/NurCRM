"""
Совместимость со старыми кассами (единое ТЗ бэкенду 26.10.2026, Б-01 и Б-02).

Касса и мобилка шлют `X-NurMarket-Client: kassa|owner|mobile/<версия>`, новый сайт витрины — `site/<версия>`.
«Новый» клиент — kassa/owner ≥ 1.17.61, mobile ≥ 1.18.5, site ≥ 1.0. Запрос без заголовка (старая касса,
старый сайт market.nurcrm.kg) — «старый».

Меняющее поведение (шаг 3 ТЗ) включается так:

    from core.api_compat import is_strict
    if is_strict(request, company):
        raise ApiError("negative_price", ...)          # новое поведение
    else:
        ...                                             # переходный режим для старых касс

is_strict = новый клиент ИЛИ у компании флаг `api_strict_2026_10`.
Флаг компании — платная функция (CompanyAddon) с тем же кодом: включают поддержка или
суперпользователь (админка «Платные функции компаний»), без миграций.
Авторы: Умар и Claude (Anthropic), 26.10.2026.
"""
import re

API_VERSION = "2026-10"
STRICT_FLAG = "api_strict_2026_10"
HARDWARE_FLAG = "hardware_2026_11"
# 06.11.2026: несколько касс одного типа на одном логине (иначе — слот «одна касса этого типа»)
MULTI_DESKTOP_POS_FLAG = "allow_multi_desktop_pos"
MULTI_ANDROID_POS_FLAG = "allow_multi_android_pos"
ELECTRONICS_FLAG = "electronics_2026_11"  # 13.11.2026: проверки серийников
COMPANY_FLAGS = (STRICT_FLAG, HARDWARE_FLAG, MULTI_DESKTOP_POS_FLAG, MULTI_ANDROID_POS_FLAG, ELECTRONICS_FLAG)

MIN_NEW = {"kassa": (1, 17, 61), "owner": (1, 17, 61), "mobile": (1, 18, 5), "site": (1, 0, 0)}
# 06.11.2026: android-pos/<версия> — Android-касса; для всей старой логики это та же касса (kind "kassa")
_CLIENT_RE = re.compile(r"^\s*(kassa|owner|mobile|site|android-pos)/(\d+(?:\.\d+){0,3})\b", re.I)


def parse_client(request):
    """('kassa', (1, 17, 61)) или (None, None), если заголовка нет или он не по формату."""
    cached = getattr(request, "_nm_client", None)
    if cached is not None:
        return cached
    raw = ""
    try:
        raw = request.headers.get("X-NurMarket-Client") or ""
    except AttributeError:
        raw = (getattr(request, "META", {}) or {}).get("HTTP_X_NURMARKET_CLIENT", "")
    m = _CLIENT_RE.match(raw)
    result = (None, None)
    if m:
        kind = m.group(1).lower()
        result = ("kassa" if kind == "android-pos" else kind, tuple(int(x) for x in m.group(2).split(".")))
    try:
        request._nm_client = result
    except AttributeError:
        pass
    return result


def is_kassa(request):
    """Запрос с кассы (X-NurMarket-Client: kassa/…)."""
    return parse_client(request)[0] == "kassa"


def may_use_foreign_shift(request, user, owner_like):
    """ТЗ 31.10 п.7: чужая смена — только владельцу/админу из веб-админки; с кассы — никому (shift_not_owned)."""
    return bool(owner_like) and not is_kassa(request)


def client_label(request):
    kind, ver = parse_client(request)
    return f"{kind}/{'.'.join(map(str, ver))}" if kind else ""


MIN_NEW_ANDROID_POS = (1, 0, 0)  # qf1311


def _raw_kind(request):
    try:
        raw = request.headers.get("X-NurMarket-Client") or ""
    except AttributeError:
        raw = (getattr(request, "META", {}) or {}).get("HTTP_X_NURMARKET_CLIENT", "")
    m = _CLIENT_RE.match(raw)
    return m.group(1).lower() if m else ""


def is_new_client(request):
    kind, ver = parse_client(request)
    if not kind:
        return False
    need = MIN_NEW[kind]
    if kind == "kassa" and _raw_kind(request) == "android-pos":  # qf1311: своя нумерация Android-кассы
        need = MIN_NEW_ANDROID_POS
    return tuple(ver) + (0,) * (3 - len(ver)) >= need


# П1-7 (14.11.2026, p1714): запросы из веб-админки маркета (market.nurcrm.kg и её превью).
# Для них права сотрудника (can_*) проверяются так же строго, как для новой кассы.
# Узнаём по Origin/Referer (браузер подставляет сам) или по заголовку X-NurMarket-Client: web/<версия>.
_WEB_ADMIN_HOSTS = {"market.nurcrm.kg", "stagemarket.nurcrm.kg"}


def _host_of(url):
    m = re.match(r"^\s*https?://([^/:]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def is_web_admin(request):
    if request is None:
        return False
    cached = getattr(request, "_nm_web_admin", None)
    if cached is not None:
        return cached
    try:
        h = request.headers
        client = h.get("X-NurMarket-Client") or ""
        origin = h.get("Origin") or h.get("Referer") or ""
    except AttributeError:
        meta = getattr(request, "META", {}) or {}
        client = meta.get("HTTP_X_NURMARKET_CLIENT", "")
        origin = meta.get("HTTP_ORIGIN", "") or meta.get("HTTP_REFERER", "")
    host = _host_of(origin)
    result = bool(
        re.match(r"^\s*web/", client, re.I)
        or host in _WEB_ADMIN_HOSTS
        or (host.endswith(".vercel.app") and host.startswith("front-market"))
    )
    try:
        request._nm_web_admin = result
    except AttributeError:
        pass
    return result


def strict_rights(request, company=None):
    """Права сотрудника проверять строго: новая касса, флаг компании или веб-админка маркета."""
    return is_web_admin(request) or is_strict(request, company)


def company_flag(company, code):
    """Флаг компании — действующая платная функция с этим кодом."""
    if company is None:
        return False
    cache = getattr(company, "_nm_flags", None)
    if cache is None:
        from apps.users.models import CompanyAddon

        cache = {
            a.code: a.is_effective()
            for a in CompanyAddon.objects.filter(company=company, code__in=COMPANY_FLAGS)
        }
        try:  # mod2011: профиль магазина (hozmag/stroymag/electronics) включает признаки сам
            from apps.users.market_modules import profile_flags
            for _f in profile_flags(company):
                cache[_f] = True
        except Exception:
            pass
        try:
            company._nm_flags = cache
        except AttributeError:
            pass
    return bool(cache.get(code))


def company_flags(company):
    return {code: company_flag(company, code) for code in COMPANY_FLAGS}


def _company_of(request):
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def is_strict(request, company=None):
    """True — новое поведение (шаг 3): новый клиент или флаг компании api_strict_2026_10."""
    if request is not None and is_new_client(request):
        return True
    if company is None and request is not None:
        company = _company_of(request)
    return company_flag(company, STRICT_FLAG)


def is_hardware_strict(request, company=None):
    """Проверки хозмага (Б-52, Б-55, Б-56): флаг hardware_2026_11 и только новый клиент."""
    if company is None and request is not None:
        company = _company_of(request)
    return company_flag(company, HARDWARE_FLAG) and request is not None and is_new_client(request)
