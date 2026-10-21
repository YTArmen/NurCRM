"""Журнал денежных операций: запись одной строкой INSERT, ошибки журнала не ломают операцию."""
import logging

CASHFLOW_FIELDS = ("amount", "type", "cashbox_id", "category_id", "name", "status", "created_at")


def cashflow_snapshot(cf):
    out = {}
    for f in CASHFLOW_FIELDS:
        v = getattr(cf, f, None)
        out[f] = str(v) if v is not None else None
    return out


def log_change(*, company, user, action, object_type, object_id="", object_name="", before=None, after=None, request=None):
    try:
        from apps.construction.models import AuditLog

        if before is not None and after is not None:
            keys = [k for k in after if before.get(k) != after.get(k)]
            if not keys and action == "update":
                return
            before = {k: before.get(k) for k in keys}
            after = {k: after[k] for k in keys}
        ip = None
        if request is not None:
            xff = request.META.get("HTTP_X_FORWARDED_FOR")
            ip = (xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")) or None
        AuditLog.objects.create(
            company=company, user=user if getattr(user, "pk", None) else None, action=action,
            object_type=object_type, object_id=str(object_id or ""), object_name=(object_name or "")[:255],
            before=before, after=after, ip=ip,
        )
    except Exception:
        logging.getLogger(__name__).warning("audit log failed", exc_info=True)


def log_cashflow(request, cf, action, before=None, after=None):
    log_change(
        company=cf.company, user=request.user, action=action, object_type="cashflow", object_id=cf.pk,
        object_name=cf.name, before=before, after=after, request=request,
    )
