# apps/cafe/permissions.py
from rest_framework import permissions
from rest_framework.exceptions import APIException
from django.contrib.auth import get_user_model

User = get_user_model()


def _company_id_of(obj):
    """Пытаемся извлечь company_id из любого объекта (через поле company_id или company.id)."""
    if obj is None:
        return None
    if hasattr(obj, "company_id"):
        return getattr(obj, "company_id")
    company = getattr(obj, "company", None)
    return getattr(company, "id", None)


class IsCompanyOwner(permissions.BasePermission):
    """
    Пускает владельца компании (или суперпользователя).
    На уровне объекта — владелец должен совпадать по компании с объектом.
    """

    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated):
            return False
        return bool(
            getattr(u, "is_superuser", False)
            or getattr(u, "role", None) == "owner"
            or getattr(u, "owned_company_id", None)
        )

    def has_object_permission(self, request, view, obj):
        u = request.user
        if getattr(u, "is_superuser", False):
            return True

        is_owner = (getattr(u, "role", None) == "owner") or bool(getattr(u, "owned_company_id", None))
        if not is_owner:
            return False

        obj_company_id = _company_id_of(obj)
        user_company_id = getattr(u, "company_id", None) or getattr(u, "owned_company_id", None)
        return obj_company_id in (None, user_company_id)


class IsCompanyOwnerOrAdmin(permissions.BasePermission):
    """
    Пускает владельца или администратора той же компании (суперпользователь — всегда).
    Для объектов User запрещает редактировать самого себя.
    """

    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated):
            return False
        if getattr(u, "is_superuser", False):
            return True
        return getattr(u, "role", None) in ("owner", "admin")

    def has_object_permission(self, request, view, obj):
        u = request.user

        # суперюзер — без ограничений
        if getattr(u, "is_superuser", False):
            return True

        # роль должна быть owner|admin
        if getattr(u, "role", None) not in ("owner", "admin"):
            return False

        # проверка компании
        obj_company_id = _company_id_of(obj)
        user_company_id = getattr(u, "company_id", None) or getattr(u, "owned_company_id", None)
        if obj_company_id not in (None, user_company_id):
            return False

        # запрет редактировать себя — только если объект это User
        if request.method not in permissions.SAFE_METHODS and isinstance(obj, User):
            if obj.pk == u.pk:
                return False

        return True


class IsCompanyOwnerOrAdminOrSupervisor(permissions.BasePermission):
    """
    Пускает владельца, администратора, РОП или руководителя региона (supervisor).
    Для объектов User запрещает редактировать самого себя.
    Для supervisor доступ разрешён только к сотрудникам своего региона.
    """

    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated):
            return False
        if getattr(u, "is_superuser", False) or getattr(u, "owned_company_id", None):
            return True
        return getattr(u, "role", None) in ("owner", "admin", "rop", "supervisor")

    def has_object_permission(self, request, view, obj):
        u = request.user
        if getattr(u, "is_superuser", False):
            return True

        obj_company_id = _company_id_of(obj)
        user_company_id = getattr(u, "company_id", None) or getattr(u, "owned_company_id", None)
        if obj_company_id not in (None, user_company_id):
            return False

        if request.method not in permissions.SAFE_METHODS and isinstance(obj, User):
            if obj.pk == u.pk:
                return False

        if getattr(u, "role", None) in ("owner", "admin", "rop") or bool(getattr(u, "owned_company_id", None)):
            return True

        if getattr(u, "role", None) == "supervisor":
            if isinstance(obj, User):
                my_regions = u.get_consulting_region_codes()
                return any(r in my_regions for r in obj.get_consulting_region_codes())
            return True

        return False


class IsPlatformAdmin(permissions.BasePermission):
    message = "Требуются права платформенного администратора."

    def has_permission(self, request, view):
        u = request.user
        return bool(
            u
            and u.is_authenticated
            and getattr(u, "is_platform_admin", False) is True
        )


# ─────────────────────────────────────────────────────────────
# «Только смотреть» (ТЗ «Бухгалтер», п. 1)
# ─────────────────────────────────────────────────────────────
READ_ONLY_FIELDS = (
    "can_edit_cashbox", "can_edit_products", "can_edit_procurement", "can_edit_documents",
    "can_edit_clients", "can_edit_salary", "can_edit_employees", "can_edit_settings",
)
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def is_read_only_for(user, field: str) -> bool:
    """True, если права «менять» раздела явно отключены (field=False). owner/admin/superuser — никогда."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "role", None) in ("owner", "admin"):
        return False
    if getattr(user, "owned_company_id", None):
        return False
    return getattr(user, field, None) is False


class ReadOnlyError(APIException):
    """403 {"detail", "code": "read_only"} — без зависимости от core.exceptions.ApiError (ТЗ-28)."""

    status_code = 403
    default_code = "read_only"


def read_only_permission(field: str):
    """Класс permission: при field=False запрещает POST/PATCH/PUT/DELETE (403 code=read_only)."""

    class _ReadOnlyGuard(permissions.BasePermission):
        def has_permission(self, request, view):
            if request.method in _SAFE_METHODS:
                return True
            if is_read_only_for(request.user, field):
                raise ReadOnlyError(
                    {"detail": "Недостаточно прав: у вас доступ к разделу только для просмотра.", "code": "read_only"}
                )
            return True

    _ReadOnlyGuard.__name__ = f"ReadOnlyGuard_{field}"
    return _ReadOnlyGuard


def read_only_guard(field: str):
    """Декоратор класса view: добавляет проверку «только смотреть» к permission_classes."""

    def deco(view_cls):
        view_cls.permission_classes = [*list(view_cls.permission_classes), read_only_permission(field)]
        return view_cls

    return deco
