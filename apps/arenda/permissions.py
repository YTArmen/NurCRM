"""Доступ по разделам (ТЗ §2.2): проверка на сервере для каждого запроса."""
import uuid
from dataclasses import dataclass
from typing import Optional

from rest_framework.permissions import BasePermission

from core.exceptions import ApiError

from .constants import SECTIONS
from .defaults import ensure_company_defaults


@dataclass
class Ctx:
    user: object
    company: object
    is_owner: bool
    access: object = None  # ArendaAccess | None

    @property
    def sections(self):
        if self.is_owner:
            return list(SECTIONS)
        if self.access and self.access.is_active:
            return [s for s in (self.access.sections or []) if s in SECTIONS]
        return []

    def allowed_branch_ids(self) -> Optional[set]:
        """None = все филиалы компании."""
        if self.is_owner or (self.access and self.access.all_branches):
            return None
        if not self.access:
            return set()
        return set(self.access.branches.values_list("id", flat=True))


def _company_of(user):
    c = getattr(user, "owned_company", None)
    if c is not None:
        return c, True
    return getattr(user, "company", None), False


def get_ctx(request) -> Ctx:
    ctx = getattr(request, "_arenda_ctx", None)
    if ctx is not None:
        return ctx
    user = request.user
    company, owns = _company_of(user)
    if company is None:
        raise ApiError("permission_denied", "У пользователя нет компании.", status_code=403)
    role = str(getattr(user, "role", "") or "").lower()
    is_owner = owns or role in ("owner", "admin") or bool(getattr(user, "is_superuser", False))
    ensure_company_defaults(company)
    access = None
    if not is_owner:
        from .models import ArendaAccess
        access = ArendaAccess.objects.filter(user=user, company=company).first()
    ctx = Ctx(user=user, company=company, is_owner=is_owner, access=access)
    request._arenda_ctx = ctx
    return ctx


def _targeted_branch(request):
    raw = request.query_params.get("branch")
    if raw is None and hasattr(request, "data") and hasattr(request.data, "get"):
        raw = request.data.get("branch")
    if not raw or raw == "all":
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError):
        return None


def deny(section):
    return ApiError("permission_denied", "Нет доступа к разделу.", status_code=403, top={"section": section})


def ArendaSection(*sections: str):
    """Фабрика permission-класса: `permission_classes = [IsAuthenticated, ArendaSection("tenants")]`.
    Несколько разделов = достаточно любого из них."""
    assert sections and all(s in SECTIONS for s in sections), sections
    section = sections[0]

    class _ArendaSection(BasePermission):
        def has_permission(self, request, view):
            if not request.user or not request.user.is_authenticated:
                return False
            ctx = get_ctx(request)
            if not any(s in ctx.sections for s in sections):
                raise deny(section)
            allowed = ctx.allowed_branch_ids()
            if allowed is not None:
                b = _targeted_branch(request)
                if b is not None and b not in allowed:
                    raise deny(section)
            return True

    _ArendaSection.section = section
    _ArendaSection.sections = sections
    _ArendaSection.__name__ = "ArendaSection_" + "_".join(sections)
    return _ArendaSection
