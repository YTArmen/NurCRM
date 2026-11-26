"""Б-65: POST/GET api/main/ai-actions/, настройки api/main/ai-actions/settings/."""
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ApiError

from apps.main.tz26.ai_stock.models import AiAction, AiCompanySettings
from apps.main.tz26.ai_stock.serializers import AiActionSerializer
from apps.main.tz26.ai_stock.services import store_utterances_enabled, utterance_hash


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def is_reader(user) -> bool:
    """Владелец / администратор / суперпользователь видят все записи компании."""
    if getattr(user, "is_superuser", False) or getattr(user, "owned_company", None):
        return True
    return getattr(user, "role", None) in ("owner", "admin", "OWNER", "ADMIN", "Владелец", "Администратор")


def _bool(raw):
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _dt(raw, end=False):
    """-> (datetime, только_дата). Для date_to-даты возвращает начало следующего дня."""
    d = parse_datetime(raw)
    if d is None:
        day = parse_date(raw)
        if day is None:
            raise ApiError("validation_error", f"Неверная дата: {raw}", fields={"date": ["Неверный формат"]})
        d = timezone.make_aware(datetime.combine(day, time.min))
        if end:
            d += timedelta(days=1)
        return d, True
    if timezone.is_naive(d):
        d = timezone.make_aware(d)
    return d, False


def _need_company(user):
    company = _company(user)
    if company is None:
        raise ApiError("permission_denied", "Пользователь не привязан к компании.", status_code=403)
    return company


class AiActionsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _need_company(request.user)
        data = request.data if isinstance(request.data, dict) else {}
        call_id = (request.headers.get("Idempotency-Key") or data.get("callId") or data.get("call_id") or "").strip()
        if not call_id:
            raise ApiError("validation_error", "Нужен заголовок Idempotency-Key (callId вызова).",
                           fields={"Idempotency-Key": ["Обязательное поле"]})
        if len(call_id) > 255:
            raise ApiError("validation_error", "Idempotency-Key не длиннее 255 символов.")

        existing = AiAction.objects.filter(company=company, call_id=call_id).select_related("user").first()
        if existing is not None:
            return self._replay(existing)

        client = str(data.get("client") or "mobile").lower()
        if client not in AiAction.ClientKind.values:
            raise ApiError("validation_error", "client: mobile|pos|owner|telegram.",
                           fields={"client": ["Недопустимое значение"]})
        at = timezone.now()
        if data.get("at"):
            at, _ = _dt(str(data["at"]))
        amount = None
        if data.get("amount") not in (None, ""):
            try:
                amount = Decimal(str(data["amount"])).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                raise ApiError("validation_error", "amount: неверное число.", fields={"amount": ["Неверное число"]})
        args = data.get("args")
        if args is None:
            args = {}
        if not isinstance(args, (dict, list)):
            raise ApiError("validation_error", "args: объект или список.", fields={"args": ["Неверный тип"]})

        text = data.get("utterance_text")
        text = str(text) if text not in (None, "") else None
        uhash = str(data.get("utterance_hash") or "")[:64]
        if not uhash and text is not None:
            uhash = utterance_hash(text)
        # Текст фразы — персональные данные: хранится только если включено у компании.
        if text is not None and not store_utterances_enabled(company):
            text = None

        branch = getattr(request, "branch", None)
        if getattr(branch, "company_id", None) != company.id:
            branch = None
        try:
            with transaction.atomic():
                obj = AiAction.objects.create(
                    company=company, branch=branch, call_id=call_id, user=request.user,
                    device=str(data.get("device") or "")[:128], client=client, at=at,
                    utterance_hash=uhash, utterance_text=text,
                    tool=str(data.get("tool") or "")[:128], args=args,
                    result_status=str(data.get("result_status") or "")[:32],
                    say=str(data.get("say") or ""),
                    object_type=str(data.get("object_type") or "")[:32],
                    object_id=str(data.get("object_id") or "")[:64],
                    amount=amount, reversible=_bool(data.get("reversible", False)),
                    compensation_of=(str(data["compensation_of"])[:255] if data.get("compensation_of") else None),
                    model=str(data.get("model") or "")[:64],
                )
        except IntegrityError:  # гонка двух одинаковых запросов
            return self._replay(AiAction.objects.get(company=company, call_id=call_id))
        return Response(AiActionSerializer(obj).data, status=status.HTTP_201_CREATED)

    @staticmethod
    def _replay(obj):
        resp = Response(AiActionSerializer(obj).data, status=status.HTTP_200_OK)
        resp["Idempotent-Replayed"] = "true"
        return resp

    def get(self, request):
        company = _need_company(request.user)
        qs = AiAction.objects.filter(company=company).select_related("user")
        qp = request.query_params
        if not is_reader(request.user):
            qs = qs.filter(user=request.user)  # сотрудник — только свои
        elif qp.get("user"):
            try:
                qs = qs.filter(user_id=UUID(qp["user"]))
            except ValueError:
                qs = qs.none()
        if qp.get("date_from"):
            qs = qs.filter(at__gte=_dt(qp["date_from"])[0])
        if qp.get("date_to"):
            d, date_only = _dt(qp["date_to"], end=True)
            qs = qs.filter(at__lt=d) if date_only else qs.filter(at__lte=d)
        if qp.get("object_type"):
            qs = qs.filter(object_type=qp["object_type"])
        if qp.get("object_id"):
            qs = qs.filter(object_id=qp["object_id"])
        if qp.get("reversible") not in (None, ""):
            qs = qs.filter(reversible=_bool(qp["reversible"]))
        total = qs.count()
        try:
            limit = max(1, min(int(qp.get("limit") or 50), 500))
            offset = max(0, int(qp.get("offset") or 0))
        except ValueError:
            limit, offset = 50, 0
        rows = AiActionSerializer(qs[offset:offset + limit], many=True).data
        return Response({"count": total, "limit": limit, "offset": offset, "results": rows})


class AiSettingsView(APIView):
    """GET — все видят, PATCH {store_ai_utterances} — только владелец/админ."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _need_company(request.user)
        return Response({"store_ai_utterances": store_utterances_enabled(company)})

    def patch(self, request):
        company = _need_company(request.user)
        if not is_reader(request.user):
            raise ApiError("permission_denied", "Только владелец или администратор.", status_code=403)
        if "store_ai_utterances" not in request.data:
            raise ApiError("validation_error", "Нужно поле store_ai_utterances.",
                           fields={"store_ai_utterances": ["Обязательное поле"]})
        val = request.data["store_ai_utterances"]
        val = val if isinstance(val, bool) else _bool(val)
        obj, _ = AiCompanySettings.objects.update_or_create(company=company, defaults={"store_ai_utterances": val})
        return Response({"store_ai_utterances": obj.store_ai_utterances})
