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
