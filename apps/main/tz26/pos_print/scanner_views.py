"""Б-72: сеанс телефона-сканера для кассы."""
import random

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.tz26.pos_print.common import company_of, iso
from apps.main.tz26.pos_print.models import SCANNER_SESSION_TTL, ScannerEvent, ScannerSession
from apps.main.tz26.pos_print.views import not_found
from apps.users.permissions_flags import PosAccess
from apps.utils import _is_owner_like
from core.exceptions import ApiError

EVENTS_LIMIT = 500


def _new_code():
    """Код уникален среди живых сеансов всех компаний: по коду сеанс находится однозначно."""
    now = timezone.now()
    for _ in range(50):
        code = f"{random.SystemRandom().randrange(1_000_000):06d}"
        if not ScannerSession.objects.filter(code=code, closed_at__isnull=True, expires_at__gt=now).exists():
            return code
    raise ApiError("conflict", "Не удалось выдать код, повторите.", status_code=409)


def _session_for(request, pk):
    s = ScannerSession.objects.filter(pk=pk).first()
    if s is None:
        raise not_found("Сеанс сканера")
    if s.company_id != company_of(request).pk:
        raise ApiError("permission_denied", "Сеанс другой компании.", status_code=403)
    return s


class ScannerSessionCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def post(self, request):
        from apps.construction.models import Cashbox

        company = company_of(request)
        cb = request.data.get("cashbox")
        cashbox = Cashbox.objects.filter(pk=cb, company=company).first() if cb else None
        if cashbox is None:
            raise ApiError("validation_error", "Касса не найдена.", fields={"cashbox": ["Обязательное поле."]})
        s = ScannerSession.objects.create(
            company=company, branch_id=cashbox.branch_id, cashbox=cashbox, code=_new_code(),
            created_by=request.user, expires_at=timezone.now() + SCANNER_SESSION_TTL,
        )
        return Response({"id": str(s.pk), "code": s.code, "expires_at": iso(s.expires_at),
                         "cashbox": str(cashbox.pk)}, status=status.HTTP_201_CREATED)


class ScannerSessionCloseAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def post(self, request, pk):
        s = _session_for(request, pk)
        if s.closed_at is None:
            ScannerSession.objects.filter(pk=s.pk, closed_at__isnull=True).update(closed_at=timezone.now())
        return Response({"id": str(s.pk), "closed": True})


class ScannerScanAPIView(APIView):
    """Телефон: POST pos/scanner-sessions/{code}/scan/ {barcode, seq}. Повтор seq — без дубля."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, code):
        barcode = str(request.data.get("barcode") or "").strip()
        if not barcode or len(barcode) > 128:
            raise ApiError("validation_error", "barcode: от 1 до 128 символов.",
                           fields={"barcode": ["Обязательное поле."]})
        try:
            seq = int(request.data.get("seq"))
            if seq < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ApiError("validation_error", "seq: целое число ≥ 0.", fields={"seq": ["Обязательное поле."]})
        now = timezone.now()
        s = (ScannerSession.objects.filter(code=str(code), closed_at__isnull=True, expires_at__gt=now)
             .order_by("-created_at").first())
        if s is None:
            raise ApiError("session_expired", "Сеанс не найден или истёк.", status_code=410)
        if s.company_id != company_of(request).pk:
            raise ApiError("permission_denied", "Сеанс другой компании.", status_code=403)
        try:
            with transaction.atomic():
                ev, created = ScannerEvent.objects.get_or_create(
                    session=s, seq=seq, defaults={"barcode": barcode, "scanned_by": request.user})
        except IntegrityError:
            ev, created = ScannerEvent.objects.get(session=s, seq=seq), False
        if created:
            try:
                from apps.main.change_feed import record_change

                record_change(s.company_id, "scanner_scan", branch_id=s.branch_id, actor_id=request.user.pk,
                              entity="scanner_session", entity_id=s.pk,
                              data={"session": str(s.pk), "cashbox": str(s.cashbox_id), "seq": seq, "barcode": barcode})
            except Exception:  # noqa: BLE001
                pass
        return Response({"seq": ev.seq, "barcode": ev.barcode, "duplicate": not created},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ScannerEventsAPIView(APIView):
    """Касса: GET pos/scanner-sessions/{id}/events/?after_seq= — сканы по порядку seq."""
    permission_classes = [permissions.IsAuthenticated, PosAccess]

    def get(self, request, pk):
        s = _session_for(request, pk)
        qs = s.events.all()
        raw = request.query_params.get("after_seq")
        if raw not in (None, ""):
            try:
                qs = qs.filter(seq__gt=int(raw))
            except ValueError:
                raise ApiError("validation_error", "after_seq: целое число.", fields={"after_seq": ["Неверное значение."]})
        rows = list(qs.order_by("seq", "id")[: EVENTS_LIMIT + 1])
        has_more = len(rows) > EVENTS_LIMIT
        rows = rows[:EVENTS_LIMIT]
        return Response({
            "session": str(s.pk), "active": s.is_active(), "expires_at": iso(s.expires_at),
            "events": [{"seq": e.seq, "barcode": e.barcode, "at": iso(e.created_at)} for e in rows],
            "last_seq": rows[-1].seq if rows else (int(raw) if raw not in (None, "") else None),
            "has_more": has_more,
        })
