"""Тариф «Мини» (20.11.2026, ТЗ Бекназара): ~500 сом, только телефон.

- plan.code == "mini"; правила держим здесь (миграции нет).
- max_staff=1 — это ТОЛЬКО владелец (сотрудники считаются вместе с владельцем).
- Разрешён только клиент mobile. Касса (kassa/…, android-pos/…) и программа владельца (owner/…)
  получают 403 plan_client_not_allowed при входе, обновлении входа и в запросах.
  22.11 (mini2211, решение владельца): браузер (без заголовка или site/…) пускаем только к оплате подписки
  (WEB_ALLOWED); остальное — 403 plan_client_not_allowed с client="web". Клиент входа запоминается в токене
  (claim ncl = mobile/web), поэтому подменить заголовок у токена с сайта нельзя.
- Вход с телефона на «Мини» — только с device_key (одно устройство), иначе 400 device_key_required.
- Второй сотрудник / вторая касса — 403 plan_limit {limit, max, upgrade_plan}.
"""
from rest_framework.exceptions import APIException

MINI = "mini"
UPGRADE_PLAN = "start"
LIMITS = {"max_staff": 1, "max_devices": 1, "max_cashboxes": 1}
ALLOWED_CLIENTS = ["mobile"]
NAMES = {"ru": "Мини", "ky": "Мини", "en": "Mini", "tr": "Mini", "uz": "Mini"}
DESCRIPTIONS = {
    "ru": "Только телефон: 1 пользователь (владелец), 1 устройство, 1 касса.",
    "ky": "Телефон гана: 1 колдонуучу (ээси), 1 түзмөк, 1 касса.",
    "en": "Phone only: 1 user (owner), 1 device, 1 cash box.",
    "tr": "Yalnızca telefon: 1 kullanıcı (sahibi), 1 cihaz, 1 kasa.",
    "uz": "Faqat telefon: 1 foydalanuvchi (egasi), 1 qurilma, 1 kassa.",
}
PERIOD = "month"
PRICE = "500.00"

# заголовок X-NurMarket-Client -> клиент; client_type при входе -> клиент
_HEADER_CLIENT = {"kassa": "desktop_pos", "android-pos": "android_pos", "owner": "desktop_owner", "mobile": "mobile"}
_BLOCKED = {"desktop_pos", "android_pos", "desktop_owner"}
_TITLES = {"desktop_pos": "касса на ПК", "android_pos": "Android-касса", "desktop_owner": "программа владельца"}


class PlanError(APIException):
    status_code = 403

    def __init__(self, body):
        super().__init__(detail=body)
        self.detail = body


def company_of(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def plan_code(company):
    if company is None:
        return None
    from apps.users.services_subscription import resolve_plan_code

    return resolve_plan_code(getattr(company, "subscription_plan", None))


def is_mini(company) -> bool:
    return plan_code(company) == MINI


def request_client(request, data=None):
    """desktop_pos / android_pos / desktop_owner / mobile / None (браузер, неизвестно)."""
    ct = str((data or {}).get("client_type") or "").strip().lower().replace("-", "_")
    if ct:
        return ct
    try:
        raw = (request.headers.get("X-NurMarket-Client") or "").strip().lower()
    except AttributeError:
        return None
    return _HEADER_CLIENT.get(raw.split("/", 1)[0]) if "/" in raw else None


def _header_client(request):
    try:
        raw = (request.headers.get("X-NurMarket-Client") or "").strip().lower()
    except AttributeError:
        return None
    return _HEADER_CLIENT.get(raw.split("/", 1)[0]) if "/" in raw else None


def client_blocked(request, data=None):
    """Клиент, если он запрещён на «Мини» (без обращения к базе), иначе None.
    mini2211: смотрим и client_type, и заголовок — client_type=mobile при заголовке kassa/… не проходит."""
    for c in (request_client(request, data), _header_client(request)):
        if c in _BLOCKED:
            return c
    return None


def client_error(client) -> dict:
    title = _TITLES.get(client, client)
    return {
        "code": "plan_client_not_allowed", "plan": MINI, "client": client,
        "allowed_clients": ALLOWED_CLIENTS, "upgrade_plan": UPGRADE_PLAN,
        "detail": f"Тариф «Мини» работает только с телефона. Чтобы пользоваться ({title}), смените тариф на «Старт».",
    }


def check_client(request, user, data=None):
    """Бросает PlanError(403 plan_client_not_allowed) для кассы/программы владельца на «Мини»."""
    c = client_blocked(request, data)
    if c and is_mini(company_of(user)):
        raise PlanError(client_error(c))


def limit_error(limit: str, current=None) -> dict:
    names = {"max_staff": "пользователь (только владелец)", "max_devices": "устройство", "max_cashboxes": "касса"}
    body = {
        "code": "plan_limit", "plan": MINI, "limit": limit, "max": LIMITS[limit], "upgrade_plan": UPGRADE_PLAN,
        "detail": f"На тарифе «Мини» доступно: {LIMITS[limit]} {names[limit]}. Чтобы добавить ещё, смените тариф на «Старт».",
    }
    if current is not None:
        body["current"] = current
    return body


def plan_block(company) -> dict | None:
    """Объект plan для GET api/main/capabilities/."""
    plan = getattr(company, "subscription_plan", None) if company is not None else None
    code = plan_code(company)
    if code == MINI:
        return {"code": MINI, "name": getattr(plan, "name", None) or NAMES["ru"], "names": NAMES, "offline_max_days": 14,
                "descriptions": DESCRIPTIONS, "period": PERIOD, "limits": dict(LIMITS),
                "allowed_clients": list(ALLOWED_CLIENTS), "upgrade_plan": UPGRADE_PLAN}
    if code is None:
        return None
    try:
        from apps.users.services_subscription import employee_limit

        staff = employee_limit(company, code)
    except Exception:
        staff = None
    from apps.users.tz31_tariffs import PLAN_NAMES  # trf2111b: одно имя с public/tariffs
    names = PLAN_NAMES.get(code)
    return {"code": code, "name": names["ru"] if names else getattr(plan, "name", None), "names": names,
            "period": PERIOD,
            "limits": {"max_staff": staff, "max_devices": None, "max_cashboxes": None},
            "allowed_clients": None}


# ---------------------------------------------------------------- mini2211: сайт только для оплаты
CLIENT_CLAIM = "ncl"
WEB = "web"
# пути после /api/, доступные «Мини» с сайта: оплата, тариф, профиль, выход/устройства
WEB_ALLOWED = ("users/billing/", "users/company/subscription/", "users/subscription-plans/", "users/profile/",
               "users/auth/", "main/capabilities/", "public/")
_MINI_CACHE = "pm:mini:u:{}"


class DeviceKeyRequired(APIException):
    status_code = 400

    def __init__(self):
        body = {"code": "device_key_required", "plan": MINI,
                "detail": "На тарифе «Мини» вход только с одного телефона: приложение должно передать device_key."}
        super().__init__(detail=body)
        self.detail = body


def login_client(request, data=None) -> str:
    """mobile / desktop_pos / … / web — какой клиент входит (для claim ncl)."""
    c = client_blocked(request, data)
    if c:
        return c
    return "mobile" if request_client(request, data) == "mobile" else WEB


def user_is_mini(user) -> bool:
    """is_mini с кэшем на 2 минуты: проверка идёт в каждом запросе."""
    pk = getattr(user, "pk", None)
    if pk is None:
        return False
    from django.core.cache import cache

    key = _MINI_CACHE.format(pk)
    try:
        v = cache.get(key)
    except Exception:  # noqa: BLE001
        v = None
    if v is None:
        v = bool(is_mini(company_of(user)))
        try:
            cache.set(key, v, 120)
        except Exception:  # noqa: BLE001
            pass
    return v


def check_login(request, user, data, token) -> dict | None:
    """При входе по паролю: 403 для кассы/программы владельца, 400 без device_key у телефона,
    claim ncl в токене. Для сайта на «Мини» возвращает объект plan_restriction для ответа."""
    check_client(request, user, data)
    client = login_client(request, data)
    token[CLIENT_CLAIM] = client
    if not is_mini(company_of(user)):
        return None
    if client == "mobile":
        key = str((data or {}).get("device_key") or "").strip()
        if not (8 <= len(key) <= 64):
            raise DeviceKeyRequired()
        return None
    return web_restriction()


def web_restriction() -> dict:
    return {"plan": MINI, "client": WEB, "allowed": ["billing"], "allowed_clients": list(ALLOWED_CLIENTS),
            "upgrade_plan": UPGRADE_PLAN}


def web_error() -> dict:
    return {"code": "plan_client_not_allowed", "plan": MINI, "client": WEB,
            "allowed_clients": list(ALLOWED_CLIENTS), "upgrade_plan": UPGRADE_PLAN, "web_allowed": ["billing"],
            "detail": "Тариф «Мини» работает только в телефоне. На сайте доступна только оплата подписки. "
                      "Чтобы работать на компьютере, смените тариф на «Старт»."}


def _web_allowed(request) -> bool:
    path = request.path or ""
    i = path.find("/api/")
    rel = path[i + 5:] if i >= 0 else path.lstrip("/")
    if rel.startswith(WEB_ALLOWED):
        return True
    # карточка компании — только чтение (тариф, срок, сумма к оплате)
    return request.method in ("GET", "HEAD", "OPTIONS") and rel.rstrip("/") == "users/company"


def check_request(request, user, token=None):
    """Каждый запрос с JWT. Касса/программа владельца на «Мини» — 403 (кроме capabilities);
    сайт на «Мини» — только оплата."""
    if (request.path or "").rstrip("/").endswith("/capabilities"):
        return
    blocked = client_blocked(request)
    if blocked:
        check_client(request, user)
        return
    if not user_is_mini(user):
        return
    payload = getattr(token, "payload", None) or {}
    client = payload.get(CLIENT_CLAIM) or _header_client(request) or WEB
    if client == WEB and not _web_allowed(request):
        raise PlanError(web_error())
