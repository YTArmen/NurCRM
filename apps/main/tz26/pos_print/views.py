"""Б-69: задания печати чека на другом устройстве."""
import hashlib
from datetime import datetime, time as dtime

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from apps.main.tz26.pos_print import services
from apps.main.tz26.pos_print.common import (
    branch_ok, clean_device_key, company_of, iso, paginate, require_idem_key, user_branch_id,
)
from apps.main.tz26.pos_print.models import PrintJob
from apps.users.permissions_flags import PosAccess
from apps.utils import _is_owner_like
from core.exceptions import ApiError


def not_found(what="Объект"):
    return ApiError("not_found", f"{what} не найден.", status_code=404)


def _can_create_print(user, sale):
    if _is_owner_like(user) or getattr(user, "can_view_sale", False):
        return True
    return sale is not None and sale.user_id == user.pk


class PrintJobListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def get(self, request):
        company = company_of(request)
        services.requeue_expired(company.pk)
        device_key = clean_device_key(request.query_params.get("device_key"))
        if device_key:
            qs = services.jobs_for_device(company, device_key)
        else:
            qs = PrintJob.objects.filter(company=company)
            if not _is_owner_like(request.user):
                qs = qs.filter(created_by=request.user)
        if not _is_owner_like(request.user):
            mine = user_branch_id(request.user)
            if mine:
                qs = qs.filter(Q(branch__isnull=True) | Q(branch_id=mine))
        st = [s for s in (request.query_params.get("status") or "").split(",") if s]
        if st:
            qs = qs.filter(status__in=st)
        if request.query_params.get("cashbox"):
            qs = qs.filter(target_cashbox_id=request.query_params["cashbox"])
        tz = timezone.get_current_timezone()
        df = parse_date(request.query_params.get("date_from") or "")
        dt = parse_date(request.query_params.get("date_to") or "")
        if df:
            qs = qs.filter(created_at__gte=datetime.combine(df, dtime.min, tz))
        if dt:
            qs = qs.filter(created_at__lte=datetime.combine(dt, dtime.max, tz))
        qs = qs.select_related("created_by").order_by("created_at", "id")
        with_sale = bool(device_key) or request.query_params.get("include_sale") in ("1", "true")

        # ETag по составу и состоянию заданий: опрос раз в 3–5 с дешёвый
        sig = list(qs.values_list("pk", "status", "attempts", "updated_at")[:500])
        etag = '"%s"' % hashlib.md5(repr((sig, request.get_full_path())).encode()).hexdigest()
        if request.headers.get("If-None-Match") == etag:
            resp = HttpResponse(status=304)
            resp["ETag"] = etag
            return resp
        resp = paginate(request, qs, lambda j: services.serialize_job(j, request, with_sale=with_sale))
        resp["ETag"] = etag
        return resp

    @idempotent(body_key=True)
    def _create(self, request):
        from apps.construction.models import Cashbox
        from apps.main.models import Sale, SaleReturn

        company = company_of(request)
        d = request.data
        kind = d.get("kind") or PrintJob.Kind.SALE_RECEIPT
        if kind not in PrintJob.Kind.values:
            raise ApiError("validation_error", "Неверный kind.", fields={"kind": ["Неверное значение."]})
        sale = ret = None
        if d.get("sale"):
            sale = Sale.objects.filter(pk=d["sale"], company=company).first()
            if sale is None:
                raise not_found("Продажа")
        if d.get("return"):
            ret = SaleReturn.objects.filter(pk=d["return"], company=company).select_related("sale").first()
            if ret is None:
                raise not_found("Возврат")
            sale = sale or ret.sale
        if kind == "sale_receipt" and sale is None:
            raise ApiError("validation_error", "Для чека продажи нужен sale.", fields={"sale": ["Обязательное поле."]})
        if kind == "return_receipt" and ret is None:
            raise ApiError("validation_error", "Для чека возврата нужен return.", fields={"return": ["Обязательное поле."]})
        if kind != "test" and not _can_create_print(request.user, sale):
            raise ApiError("permission_denied", "Нет доступа: Продажи", status_code=403,
                           top={"required": "can_view_sale"})
        device_key = clean_device_key(d.get("target_device_key"), field="target_device_key")
        cashbox = None
        if d.get("target_cashbox"):
            cashbox = Cashbox.objects.filter(pk=d["target_cashbox"], company=company).first()
            if cashbox is None:
                raise ApiError("validation_error", "Касса не найдена.", fields={"target_cashbox": ["Не найдена."]})
        if not device_key and cashbox is None:
            raise ApiError("validation_error", "Укажите target_device_key или target_cashbox.",
                           fields={"target_device_key": ["Нужно устройство или касса."]})
        try:
            copies = int(d.get("copies") or 1)
        except (TypeError, ValueError):
            copies = 0
        if not 1 <= copies <= 3:
            raise ApiError("validation_error", "copies: от 1 до 3.", fields={"copies": ["От 1 до 3."]})
        with transaction.atomic():
            job = PrintJob.objects.create(
                company=company, branch_id=(sale.branch_id if sale else None), kind=kind, sale=sale,
                sale_return=ret, target_device_key=device_key, target_cashbox=cashbox, copies=copies,
                created_by=request.user,
                source_device_key=clean_device_key(d.get("source_device_key"), field="source_device_key"),
            )
        services._emit_created(job, actor_id=request.user.pk)
        return Response(services.serialize_job(job, request), status=status.HTTP_201_CREATED)

    def post(self, request):
        require_idem_key(request)
        return self._create(request)


def _get_job(request, pk):
    company = company_of(request)
    services.requeue_expired(company.pk)
    job = PrintJob.objects.filter(pk=pk, company=company).select_related("created_by").first()
    if job is None:
        raise not_found("Задание печати")
    return company, job


class PrintJobDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def get(self, request, pk):
        _, job = _get_job(request, pk)
        if not branch_ok(request.user, job.branch_id):
            raise ApiError("permission_denied", "Задание другого филиала.", status_code=403)
        return Response(services.serialize_job(job, request))


class PrintJobClaimAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def post(self, request, pk):
        company, job = _get_job(request, pk)
        device_key = clean_device_key(request.data.get("device_key"), required=True)
        if not branch_ok(request.user, job.branch_id) or not services.can_device_claim(job, company, device_key):
            raise ApiError("permission_denied", "Это задание адресовано другому устройству.", status_code=403)
        if services.claim(job.pk, company, device_key):
            job.refresh_from_db()
            return Response(services.serialize_job(job, request))
        job.refresh_from_db()
        if job.status == PrintJob.Status.CLAIMED:
            raise ApiError("already_claimed", "Задание уже взято другим устройством.", status_code=409,
                           data={"claimed_by_device": job.claimed_by_device, "lease_until": iso(job.lease_until)})
        raise ApiError("invalid_state", f"Задание в статусе «{job.status}».", status_code=409,
                       data={"status": job.status})


class PrintJobDoneAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def post(self, request, pk):
        company, job = _get_job(request, pk)
        if job.status == PrintJob.Status.PRINTED:
            return Response(services.serialize_job(job, request))  # повтор done — 200 без изменений
        if job.status == PrintJob.Status.CANCELED:
            raise ApiError("invalid_state", "Задание отменено.", status_code=409, data={"status": job.status})
        device_key = clean_device_key(request.data.get("device_key"))
        if job.status == PrintJob.Status.CLAIMED and device_key and job.claimed_by_device != device_key:
            raise ApiError("already_claimed", "Задание взято другим устройством.", status_code=409)
        # done допустим и из pending/failed: чек уже напечатан, это главное
        now = timezone.now()
        PrintJob.objects.filter(pk=job.pk).exclude(status=PrintJob.Status.CANCELED).update(
            status=PrintJob.Status.PRINTED, printed_at=now, lease_until=None, error="", updated_at=now,
            claimed_by_device=device_key or job.claimed_by_device,
        )
        job.refresh_from_db()
        return Response(services.serialize_job(job, request))


class PrintJobFailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def post(self, request, pk):
        company, job = _get_job(request, pk)
        if job.status in (PrintJob.Status.PRINTED, PrintJob.Status.CANCELED):
            raise ApiError("invalid_state", f"Задание в статусе «{job.status}».", status_code=409,
                           data={"status": job.status})
        if job.status != PrintJob.Status.FAILED:
            PrintJob.objects.filter(pk=job.pk).update(
                status=PrintJob.Status.FAILED, error=str(request.data.get("error") or "")[:500],
                lease_until=None, updated_at=timezone.now())
            job.refresh_from_db()
        return Response(services.serialize_job(job, request))


class PrintJobCancelAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def post(self, request, pk):
        company, job = _get_job(request, pk)
        if not _is_owner_like(request.user):
            raise ApiError("permission_denied", "Отменять задания может владелец или администратор.", status_code=403)
        if job.status == PrintJob.Status.PRINTED:
            raise ApiError("invalid_state", "Уже напечатано.", status_code=409, data={"status": job.status})
        PrintJob.objects.filter(pk=job.pk).update(
            status=PrintJob.Status.CANCELED, lease_until=None, updated_at=timezone.now())
        job.refresh_from_db()
        return Response(services.serialize_job(job, request))
