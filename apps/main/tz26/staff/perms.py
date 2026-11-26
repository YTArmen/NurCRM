"""Права куска staff (Б-62…Б-64)."""
from apps.utils import _is_owner_like


def company_of(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _flag(user, name) -> bool:
    return bool(getattr(user, name, False))


def can_manage_tasks(user) -> bool:
    """Ставить, назначать и удалять задачи: владелец/админ или can_view_employees (решение по вопросу ТЗ №2)."""
    return _is_owner_like(user) or _flag(user, "can_view_employees")


def can_view_salary(user) -> bool:
    return _is_owner_like(user) or _flag(user, "can_view_salary") or _flag(user, "can_edit_salary")


def can_edit_salary(user) -> bool:
    return _is_owner_like(user) or _flag(user, "can_edit_salary")


def can_edit_schedule(user) -> bool:
    return _is_owner_like(user) or _flag(user, "can_edit_employees")


def can_view_staff(user) -> bool:
    return _is_owner_like(user) or _flag(user, "can_view_employees") or _flag(user, "can_view_salary") \
        or _flag(user, "can_edit_employees")
