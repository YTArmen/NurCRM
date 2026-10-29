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
COMPANY_FLAGS = (STRICT_FLAG, HARDWARE_FLAG)

MIN_NEW = {"kassa": (1, 17, 61), "owner": (1, 17, 61), "mobile": (1, 18, 5), "site": (1, 0, 0)}
_CLIENT_RE = re.compile(r"^\s*(kassa|owner|mobile|site)/(\d+(?:\.\d+){0,3})\b", re.I)


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
        result = (m.group(1).lower(), tuple(int(x) for x in m.group(2).split(".")))
    try:
        request._nm_client = result
    except AttributeError:
        pass
    return result


def client_label(request):
    kind, ver = parse_client(request)
    return f"{kind}/{'.'.join(map(str, ver))}" if kind else ""


def is_new_client(request):
    kind, ver = parse_client(request)
    if not kind:
        return False
    need = MIN_NEW[kind]
    return tuple(ver) + (0,) * (3 - len(ver)) >= need


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
