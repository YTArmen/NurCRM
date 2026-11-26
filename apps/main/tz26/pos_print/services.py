"""Б-69: задания печати чека на другом устройстве."""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.main.tz26.pos_print.common import (
    DEVICE_KEY_RE, get_device_cashbox_id, iso,
)
from apps.main.tz26.pos_print.models import (
    LEASE_SECONDS, MAX_ATTEMPTS, PRINT_JOB_RETENTION_DAYS, PrintJob,
)

logger = logging.getLogger("nurcrm.pos_print")


def _emit_created(job, actor_id=None):
    """Событие print_job_created в ленте изменений (Б-46)."""
    try:
        from apps.main.change_feed import record_change

        record_change(
            job.company_id, "print_job_created", branch_id=job.branch_id, actor_id=actor_id,
            entity="print_job", entity_id=job.pk,
            data={
                "print_job": str(job.pk), "kind": job.kind,
                "sale": str(job.sale_id) if job.sale_id else None,
                "target_device_key": job.target_device_key or None,
                "target_cashbox": str(job.target_cashbox_id) if job.target_cashbox_id else None,
            },
        )
    except Exception:  # noqa: BLE001 — лента не должна ломать печать
        logger.exception("print_job_created: запись в ленту не удалась")


def create_print_job_from_checkout(sale, print_on, request=None):
    """
    Крючок из pos/checkout/: вызывается внутри транзакции продажи.
    print_on = {device_key?, cashbox?}. Ошибки в print_on продажу не ломают — задание просто не создаётся.
    Возвращает PrintJob или None.
    """
    if not isinstance(print_on, dict):
        return None
    device_key = str(print_on.get("device_key") or "").strip()
    cashbox_id = print_on.get("cashbox")
    if device_key and not DEVICE_KEY_RE.match(device_key):
        logger.warning("print_on.device_key неверного формата — задание не создано")
        return None
    if not device_key and not cashbox_id:
        return None
    try:
        from apps.construction.models import Cashbox

        cashbox = None
        if cashbox_id:
            cashbox = Cashbox.objects.filter(pk=cashbox_id, company_id=sale.company_id).first()
            if cashbox is None and not device_key:
                logger.warning("print_on.cashbox не найден в компании — задание не создано")
                return None
        try:
            copies = max(1, min(int(print_on.get("copies") or 1), 3))
        except (TypeError, ValueError):
            copies = 1
        user = getattr(request, "user", None)
        data = getattr(request, "data", None) or {}
        with transaction.atomic():  # savepoint: сбой здесь не отравляет транзакцию продажи
            job = PrintJob.objects.create(
                company_id=sale.company_id, branch_id=sale.branch_id,
                kind=PrintJob.Kind.SALE_RECEIPT, sale=sale,
                target_device_key=device_key, target_cashbox=cashbox, copies=copies,
                created_by=user if getattr(user, "is_authenticated", False) else None,
                source_device_key=str(data.get("device_key") or "")[:64] if hasattr(data, "get") else "",
            )
        _emit_created(job, actor_id=getattr(user, "pk", None))
        return job
    except Exception:  # noqa: BLE001
        logger.exception("create_print_job_from_checkout")
        return None


def requeue_expired(company_id):
    """Аренда истекла без done: снова pending, attempts+1; после MAX_ATTEMPTS — failed."""
    now = timezone.now()
    expired = PrintJob.objects.filter(
        company_id=company_id, status=PrintJob.Status.CLAIMED, lease_until__lt=now,
    )
    ids = list(expired.values_list("pk", flat=True))
    for pk in ids:
        with transaction.atomic():
            job = PrintJob.objects.select_for_update().filter(
                pk=pk, status=PrintJob.Status.CLAIMED, lease_until__lt=now).first()
            if job is None:
                continue
            job.attempts += 1
            job.claimed_by_device = ""
            job.lease_until = None
            if job.attempts >= MAX_ATTEMPTS:
                job.status = PrintJob.Status.FAILED
                job.error = job.error or "lease_expired"
            else:
                job.status = PrintJob.Status.PENDING
            job.save(update_fields=["attempts", "claimed_by_device", "lease_until", "status", "error", "updated_at"])


def jobs_for_device(company, device_key):
    """Задания для устройства: адресованные ему, а при пустом device-адресе — его кассе."""
    q = Q(target_device_key=device_key)
    cashbox_id = get_device_cashbox_id(company, device_key)
    if cashbox_id:
        q |= Q(target_device_key="", target_cashbox_id=cashbox_id)
    return PrintJob.objects.filter(company=company).filter(q)


def can_device_claim(job, company, device_key):
    if job.target_device_key:
        return job.target_device_key == device_key
    if job.target_cashbox_id:
        return str(get_device_cashbox_id(company, device_key) or "") == str(job.target_cashbox_id)
    return True


def claim(job_id, company, device_key):
    """Атомарно pending -> claimed. Возвращает (job, ok)."""
    now = timezone.now()
    n = PrintJob.objects.filter(pk=job_id, company=company, status=PrintJob.Status.PENDING).update(
        status=PrintJob.Status.CLAIMED, claimed_at=now, claimed_by_device=device_key,
        lease_until=now + timedelta(seconds=LEASE_SECONDS), updated_at=now,
    )
    return n == 1


def purge_old_jobs():
    """Хранение 7 дней (Celery-чистка)."""
    cutoff = timezone.now() - timedelta(days=PRINT_JOB_RETENTION_DAYS)
    deleted, _ = PrintJob.objects.filter(created_at__lt=cutoff).delete()
    return deleted


def serialize_job(job, request=None, with_sale=True):
    d = {
        "id": str(job.pk), "kind": job.kind, "status": job.status, "copies": job.copies,
        "sale_id": str(job.sale_id) if job.sale_id else None,
        "return_id": str(job.sale_return_id) if job.sale_return_id else None,
        "target_device_key": job.target_device_key or None,
        "target_cashbox": str(job.target_cashbox_id) if job.target_cashbox_id else None,
        "branch": str(job.branch_id) if job.branch_id else None,
        "created_by": ({"id": str(job.created_by_id),
                        "name": (job.created_by.get_full_name() or job.created_by.email) if job.created_by else ""}
                       if job.created_by_id else None),
        "source_device_key": job.source_device_key or None,
        "created_at": iso(job.created_at), "claimed_at": iso(job.claimed_at),
        "claimed_by_device": job.claimed_by_device or None,
        "lease_until": iso(job.lease_until), "printed_at": iso(job.printed_at),
        "error": job.error or "", "attempts": job.attempts,
    }
    if with_sale:
        d["sale"] = _sale_payload(job, request)
    return d


def _sale_payload(job, request):
    if not job.sale_id:
        return None
    from apps.main.pos_serializers import SaleDetailSerializer
    from apps.main.models import Sale

    sale = (Sale.objects.select_related("user").prefetch_related("items__product", "returns")
            .filter(pk=job.sale_id).first())
    if sale is None:
        return None
    return SaleDetailSerializer(sale, context={"request": request}).data
