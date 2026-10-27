"""Права ящика (Б-75). Флагов can_*_inbox у User нет, поэтому своя модель InboxAccess.

Правило по умолчанию (если записи InboxAccess нет):
- владелец компании, роль owner/admin, staff, суперпользователь — все права ящика;
- остальные: can_view_inbox и can_reply_inbox = User.can_view_clients, остальное запрещено.
Если запись InboxAccess есть — берутся её флаги.
"""
from rest_framework.permissions import BasePermission

from core.exceptions import ApiError

FLAGS = (
    "can_view_inbox", "can_reply_inbox", "can_view_all_conversations",
    "can_manage_channels", "can_broadcast", "can_use_ai_inbox",
)


def company_of(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def is_owner_like(user) -> bool:
    from apps.utils import _is_owner_like
    try:
        return bool(_is_owner_like(user))
    except Exception:
        return False


def perms_of(user) -> dict:
    if not getattr(user, "is_authenticated", False):
        return {f: False for f in FLAGS}
    if is_owner_like(user):
        return {f: True for f in FLAGS}
    row = getattr(user, "_inbox_access_cache", None)
    if row is None:
        from apps.inbox.models import InboxAccess
        row = InboxAccess.objects.filter(user=user).first() or False
        user._inbox_access_cache = row
    if row:
        return {f: bool(getattr(row, f)) for f in FLAGS}
    base = bool(getattr(user, "can_view_clients", False))
    return {**{f: False for f in FLAGS}, "can_view_inbox": base, "can_reply_inbox": base}


def has(user, flag: str) -> bool:
    return perms_of(user).get(flag, False)


def require(user, flag: str):
    if not company_of(user):
        raise ApiError("permission_denied", "Нет компании.", status_code=403, top={"required": flag})
    if not has(user, flag):
        raise ApiError("permission_denied", f"Нет доступа: {flag}", status_code=403, top={"required": flag})
    return company_of(user)


def can_see_phone(user) -> bool:
    return is_owner_like(user) or bool(getattr(user, "can_view_clients", False))


def user_branch_ids(user) -> list:
    try:
        return list(user.allowed_branch_ids)
    except Exception:
        return []


class InboxPermission(BasePermission):
    """Вход обязателен; конкретный флаг проверяется во вьюхе через require()."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)
