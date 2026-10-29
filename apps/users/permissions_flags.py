"""ТЗ Н-16: единая проверка флагов доступа сотрудников (can_view_*) на сервере.

Правила:
- владелец, админ, суперпользователь и владелец компании (owned_company) флагами НЕ ограничиваются;
- безопасные методы (GET/HEAD/OPTIONS) пропускаются, если не указан read=True;
- иначе нужен хотя бы один из указанных флагов (== True);
- отказ: 403 {"code": "permission_denied", "detail": "...", "required": "<флаг>"}.

Использование (как read_only_guard):
    @flag_guard("can_view_products")                  # только запись (POST/PATCH/PUT/DELETE)
    @flag_guard("can_view_analytics", read=True)      # и чтение
"""
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied

SAFE = permissions.SAFE_METHODS


def is_privileged(user) -> bool:
    """owner/admin/superuser/владелец компании — без ограничений флагами."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):  # как apps.utils.is_owner_like
        return True
    if getattr(user, "role", None) in ("owner", "admin"):
        return True
    try:
        return getattr(user, "owned_company", None) is not None
    except Exception:
        return False


def has_any_flag(user, flags) -> bool:
    return any(getattr(user, f, None) is True for f in flags)


def denied(flags, detail=None):
    """403 с телом {code, detail, required}."""
    flags = [flags] if isinstance(flags, str) else list(flags)
    body = {
        "code": "permission_denied",
        "detail": detail or "Недостаточно прав для этого действия.",
        "required": flags[0],
    }
    if len(flags) > 1:
        body["required_any"] = flags
    return PermissionDenied(detail=body)


def _legacy_client(request) -> bool:
    """Выкатка 29.10 (горячая правка): права Н-16 по флагам действуют только для новых клиентов
    (заголовок X-NurMarket-Client нужной версии) или при флаге компании api_strict_2026_10.
    Веб-касса и старые кассы получают прежнее поведение — как до 29.10, без новых 403."""
    try:
        from core.api_compat import is_strict

        return not is_strict(request)
    except Exception:  # noqa: BLE001
        return True


def flag_permission(*flags, read=False, detail=None, allow=None):
    """Класс permission: нужен любой из флагов (владелец/админ — всегда)."""

    class FlagPermission(permissions.BasePermission):
        required_flags = flags

        def has_permission(self, request, view):
            user = request.user
            if not (user and user.is_authenticated):
                return False  # аутентификацию проверяет IsAuthenticated
            if is_privileged(user):
                return True
            if _legacy_client(request):
                return True
            if not read and request.method in SAFE:
                return True
            if allow is not None and allow(request):
                return True
            if has_any_flag(user, flags):
                return True
            raise denied(flags, detail)

    FlagPermission.__name__ = "FlagPermission_" + "_".join(flags)
    return FlagPermission


def flag_guard(*flags, read=False, detail=None, allow=None):
    """Декоратор класса view: добавляет flag_permission к permission_classes.
    allow(request) -> True пропускает запрос без флага (исключение для кассы)."""

    def deco(view_cls):
        view_cls.permission_classes = [*list(view_cls.permission_classes), flag_permission(*flags, read=read, detail=detail, allow=allow)]
        return view_cls

    return deco


class EmployeesListAccess(permissions.BasePermission):
    """ТЗ Н-16: список сотрудников — owner/admin/суперпользователь, РОП и руководитель региона,
    консалтинг-«продавец» с регионом (видит только себя), либо флаг can_view_employees (building — can_view_building_employess)."""

    FLAGS = ("can_view_employees", "can_view_building_employess")

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if is_privileged(user) or getattr(user, "role", None) in ("rop", "supervisor"):
            return True
        if _legacy_client(request):
            return True  # до 29.10 список отдавался любому сотруднику — старые кассы и сайт так и работают
        # role=salesperson — это и кассир магазина; консалтинг-продавца отличает привязка к региону
        if has_any_flag(user, self.FLAGS):
            return True
        if getattr(user, "role", None) == "salesperson" and user.get_consulting_region_codes():
            return True
        raise denied(self.FLAGS[0], "Список сотрудников недоступен.")


class PosAccess(permissions.BasePermission):
    """ТЗ Н-16: «Работа на кассе» (can_use_pos). Отказывает только при явном False;
    у всех существующих сотрудников после миграции True (db_default), owner/admin — всегда пускаются."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated) or is_privileged(user):
            return True
        if getattr(user, "can_use_pos", True) is False:
            raise denied("can_use_pos", "Работа на кассе отключена для вашей учётной записи.")
        return True
