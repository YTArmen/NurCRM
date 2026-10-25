from .models import AuditLog


def _ip(request):
    if request is None:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return (xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")) or None


def log(request, company, action, object_type, obj=None, *, before=None, after=None, branch=None,
        object_id="", object_name="", user=None):
    """Запись в журнал действий. before/after — JSON-совместимые словари."""
    user = user or (request.user if request is not None and request.user.is_authenticated else None)
    return AuditLog.objects.create(
        company=company, user=user, branch=branch, action=action, object_type=object_type,
        object_id=str(getattr(obj, "pk", "") or object_id), object_name=str(object_name or obj or "")[:255],
        before=before, after=after, ip=_ip(request),
    )
