"""?branch=<id>|all — единый фильтр филиала для всех списков и итогов."""
import uuid
from typing import Optional

from rest_framework.exceptions import ValidationError

from core.exceptions import ApiError
from apps.users.models import Branch


def branch_ids_from_request(ctx, request) -> Optional[list]:
    """None = без ограничения по филиалу (все филиалы компании). Иначе список id."""
    raw = request.query_params.get("branch")
    allowed = ctx.allowed_branch_ids()
    if not raw or raw == "all":
        return None if allowed is None else list(allowed)
    try:
        bid = uuid.UUID(raw)
    except ValueError:
        raise ValidationError({"branch": ["Некорректный идентификатор филиала."]})
    if allowed is not None and bid not in allowed:
        raise ApiError("permission_denied", "Нет доступа к филиалу.", status_code=403)
    if not Branch.objects.filter(id=bid, company=ctx.company).exists():
        raise ApiError("not_found", "Филиал не найден.", status_code=404)
    return [bid]


def apply_branch(qs, ids, field="branch_id"):
    if ids is None:
        return qs
    return qs.filter(**{f"{field}__in": ids})


def tenant_visible(ctx, tenant) -> bool:
    """Сотрудник с доступом только к части филиалов не видит арендаторов чужих филиалов (карточка, договоры, акты).
    Арендатор виден, если он заведён в доступном филиале или у него там договор; без филиала и договоров — виден всем."""
    allowed = ctx.allowed_branch_ids()
    if allowed is None:
        return True
    from .models import Lease
    if tenant.branch_id in allowed:
        return True
    leases = Lease.objects.filter(tenant=tenant, archived_at__isnull=True)
    if leases.filter(branch_id__in=allowed).exists():
        return True
    return tenant.branch_id is None and not leases.exists()
