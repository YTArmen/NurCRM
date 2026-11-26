"""ТЗ «Админка»: корзина удалённого (п.9)."""
from datetime import timedelta

from django.utils import timezone
from rest_framework import permissions
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.construction import trash
from apps.construction.audit import log_request
from apps.construction.models import TrashEntry
from apps.construction.views import CompanyBranchScopedMixin

# какое право нужно, чтобы видеть/возвращать тип (владелец и админ — всегда)
_VIEW_RIGHT = {"product": "can_view_products", "client": "can_view_clients",
               "category": "can_view_brand_category", "brand": "can_view_brand_category"}
_EDIT_RIGHT = {"product": "can_edit_products", "client": "can_edit_clients",
               "category": "can_edit_products", "brand": "can_edit_products"}


def _owner_like(user):
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "role", None) in ("owner", "admin")
        or getattr(user, "owned_company_id", None)
    )


def _can(user, type_, write):
    if _owner_like(user):
        return True
    if not getattr(user, _VIEW_RIGHT[type_], False):
        return False
    return not (write and getattr(user, _EDIT_RIGHT[type_], None) is False)


def _row(e):
    now = timezone.now()
    return {
        "id": str(e.id), "type": e.type, "object_id": e.object_id, "name": e.object_name,
        "deleted_at": e.deleted_at,
        "deleted_by": str(e.deleted_by_id) if e.deleted_by_id else None,
        "expires_at": e.deleted_at + timedelta(days=trash.RETENTION_DAYS),
        "days_left": max((e.deleted_at + timedelta(days=trash.RETENTION_DAYS) - now).days, 0),
    }


class TrashListView(CompanyBranchScopedMixin, APIView):
    """GET /api/main/trash/?type=product|client|category|brand&page= — удалённое за 30 дней."""

    permission_classes = [permissions.IsAuthenticated]
    PAGE = 50

    def get(self, request):
        company = self._company()
        if company is None:
            raise PermissionDenied("У пользователя не настроена компания.")
        trash.purge_expired(company)
        type_ = request.query_params.get("type")
        if type_ and type_ not in trash.TYPES:
            raise ValidationError({"type": "Используйте product|client|category|brand."})
        allowed = [t for t in trash.TYPES if _can(request.user, t, False)]
        types = [type_] if type_ else allowed
        if type_ and type_ not in allowed:
            raise PermissionDenied({"detail": "Нет доступа к корзине этого раздела.", "code": "permission_denied"})
        qs = TrashEntry.objects.filter(company=company, type__in=types)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(object_name__icontains=search)
        try:
            page = max(int(request.query_params.get("page") or 1), 1)
        except ValueError:
            page = 1
        total = qs.count()
        rows = qs.defer("payload")[(page - 1) * self.PAGE: page * self.PAGE]
        return Response({"count": total, "page": page, "page_size": self.PAGE, "retention_days": trash.RETENTION_DAYS,
                         "results": [_row(e) for e in rows]})


class TrashRestoreView(CompanyBranchScopedMixin, APIView):
    """POST /api/main/trash/{type}/{object_id}/restore/ — вернуть удалённое (object_id — id удалённого объекта)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, type, object_id):
        if type not in trash.TYPES:
            raise NotFound()
        company = self._company()
        if company is None:
            raise PermissionDenied("У пользователя не настроена компания.")
        if not _can(request.user, type, True):
            raise PermissionDenied({"detail": "Нет прав возвращать удалённое.", "code": "permission_denied"})
        entry = TrashEntry.objects.filter(company=company, type=type, object_id=str(object_id)).order_by("-deleted_at").first()
        if entry is None or entry.deleted_at < timezone.now() - timedelta(days=trash.RETENTION_DAYS):
            raise NotFound({"detail": "В корзине нет такой записи (или срок хранения 30 дней истёк)."})
        try:
            obj, notes = trash.restore(entry)
        except ValueError as exc:
            return Response({"detail": str(exc), "code": "restore_failed"}, status=409)
        log_request(request, "restore", type, obj.pk, trash._object_name(obj), company=company)
        return Response({"restored": True, "type": type, "id": str(obj.pk), "name": trash._object_name(obj), "notes": notes})
