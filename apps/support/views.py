import hmac
import logging
from collections import OrderedDict

from django.db import transaction
from rest_framework import exceptions, permissions, serializers, status
from rest_framework.authentication import BaseAuthentication
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from apps.support.bot import get_support_config, handle_support_bot_command, _send_raw
from apps.support.models import SupportErrorReport, SupportReportAttachment
from apps.support.sanitizer import MAX_MESSAGE, MAX_STACK, sanitize_context, sanitize_text

logger = logging.getLogger("support.views")

CATEGORIES = [
    "payment", "shift", "print", "drawer", "scales", "sync", "offline",
    "login", "bot", "update", "crash", "ui", "other",
]
MAX_BATCH = 50
LIMIT_ANON_PER_HOUR = 10
LIMIT_AUTH_PER_HOUR = 30
ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024
ATTACHMENT_LIMIT_PER_HOUR = 5
RATE_WINDOW = 3600


# ---------------------------------------------------------------- аутентификация

class TolerantAuthentication(BaseAuthentication):
    """
    Пробует стандартные классы аутентификации проекта (JWT, Api-Key), но
    недействительный/просроченный токен не даёт 401: запрос считается анонимным
    (тогда обязателен device_id и действует лимит 10/час). Программа должна
    уметь отправить отчёт именно тогда, когда вход сломан.
    """

    def authenticate(self, request):
        for auth_cls in api_settings.DEFAULT_AUTHENTICATION_CLASSES:
            try:
                result = auth_cls().authenticate(request)
            except exceptions.APIException:
                continue
            except Exception:
                logger.debug("Support auth backend %s failed", auth_cls, exc_info=True)
                continue
            if result is not None:
                return result
        return None


def _user_company(user):
    for attr in ("company", "owned_company"):
        try:
            comp = getattr(user, attr, None)
        except Exception:
            comp = None
        if comp is not None:
            return comp
    return None


def _user_login(user) -> str:
    try:
        return (user.get_username() or "")[:128]
    except Exception:
        return (getattr(user, "email", "") or "")[:128]


def _client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "")) or "unknown"


def _consume_quota(key: str, wanted: int, limit: int) -> int:
    """
    Атомарно резервирует до `wanted` единиц из часовой квоты `limit`.
    Возвращает, сколько удалось зарезервировать.
    """
    from django.core.cache import cache

    if wanted <= 0:
        return 0
    cache.add(key, 0, RATE_WINDOW)
    try:
        after = int(cache.incr(key, wanted))
    except ValueError:
        cache.set(key, wanted, RATE_WINDOW)
        after = wanted
    before = after - wanted
    allowed = max(0, min(wanted, limit - before))
    over = wanted - allowed
    if over:
        try:
            cache.decr(key, over)
        except ValueError:
            pass
    return allowed


def _retry_after(key: str) -> int:
    from django.core.cache import cache

    try:
        ttl = cache.ttl(key)  # django-redis
        if ttl and ttl > 0:
            return int(ttl)
    except Exception:
        pass
    return RATE_WINDOW


# ---------------------------------------------------------------- приём отчётов

class ErrorReportItemSerializer(serializers.Serializer):
    client_report_id = serializers.UUIDField()
    app = serializers.CharField(max_length=32)
    version = serializers.CharField(max_length=32)
    os = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    device_id = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    login = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    level = serializers.ChoiceField(choices=["critical", "error", "warning"], default="error")
    category = serializers.CharField(max_length=32, required=False, allow_blank=True, default="other")
    fingerprint = serializers.CharField(max_length=64)
    message = serializers.CharField(allow_blank=True, trim_whitespace=False)
    stack = serializers.CharField(required=False, allow_blank=True, allow_null=True, trim_whitespace=False, default="")
    context = serializers.ListField(
        child=serializers.CharField(allow_blank=True, trim_whitespace=False),
        required=False,
        allow_empty=True,
        default=list,
    )
    count = serializers.IntegerField(required=False, default=1, min_value=1, max_value=1_000_000)
    first_at = serializers.DateTimeField()
    last_at = serializers.DateTimeField()

    def validate_category(self, value):
        value = (value or "other").strip().lower()
        return value if value in CATEGORIES else "other"

    def validate(self, attrs):
        if attrs["last_at"] < attrs["first_at"]:
            attrs["first_at"], attrs["last_at"] = attrs["last_at"], attrs["first_at"]
        return attrs


class ErrorReportsBatchSerializer(serializers.Serializer):
    reports = serializers.ListField(
        child=ErrorReportItemSerializer(), max_length=MAX_BATCH, allow_empty=False
    )


def _clip_report(raw):
    """Обрезать слишком длинные строки до лимитов полей: отчёт о сбое лучше принять урезанным."""
    if not isinstance(raw, dict):
        return raw
    out = dict(raw)
    for name, field in ErrorReportItemSerializer().fields.items():
        limit = getattr(field, "max_length", None)
        if limit and isinstance(out.get(name), str) and len(out[name]) > limit:
            out[name] = out[name][:limit]
    return out


def _validate_reports_tolerant(data):
    """30.10.2026: раньше один плохой отчёт отклонял всю пачку (400), касса слала её снова и снова.
    Теперь: длинное обрезаем, плохие отчёты пропускаем, хорошие принимаем. Все плохие — 400."""
    import logging

    reports = data.get("reports") if isinstance(data, dict) else None
    if not isinstance(reports, list) or not reports:
        raise exceptions.ValidationError({"reports": "Нужен непустой список отчётов."})
    if len(reports) > MAX_BATCH:
        raise exceptions.ValidationError({"reports": f"Не больше {MAX_BATCH} отчётов в пачке."})
    items, rejected = [], []
    for i, raw in enumerate(reports):
        ser = ErrorReportItemSerializer(data=_clip_report(raw))
        if ser.is_valid():
            items.append(ser.validated_data)
        else:
            rejected.append({"index": i, "errors": ser.errors})
    if rejected:
        logging.getLogger(__name__).warning("error-reports: пропущено %s из %s: %s",
                                            len(rejected), len(reports), rejected[:3])
    if not items:
        raise exceptions.ValidationError({"reports": rejected[:10]})
    return items, rejected


class ErrorReportListCreateAPIView(APIView):
    """
    POST /api/support/error-reports/ — пачка до 50 отчётов.
    Ответ: {"accepted": N, "duplicates": M}; при превышении лимита — 429
    с теми же полями и "rejected" (принятое сохранено, повтор безопасен).
    Группировка и оповещения — в воркере.
    """

    authentication_classes = [TolerantAuthentication]
    permission_classes = [permissions.AllowAny]
    parser_classes = [JSONParser]

    def post(self, request):
        items, invalid = _validate_reports_tolerant(request.data)

        user = request.user
        is_auth = bool(user and user.is_authenticated)
        company = _user_company(user) if is_auth else None
        token_login = _user_login(user) if is_auth else ""

        if not is_auth and any(not (r.get("device_id") or "").strip() for r in items):
            raise exceptions.ValidationError(
                {"device_id": "Без токена авторизации device_id обязателен в каждом отчёте."}
            )

        # 1. Дедупликация: внутри пачки и с уже сохранёнными
        unique = OrderedDict()
        for r in items:
            unique.setdefault(r["client_report_id"], r)
        existing = set(
            SupportErrorReport.objects.filter(client_report_id__in=list(unique.keys()))
            .values_list("client_report_id", flat=True)
        )
        fresh = [r for cid, r in unique.items() if cid not in existing]
        duplicates = len(items) - len(fresh)

        # 2. Лимит по устройству — только для новых отчётов
        limit = LIMIT_AUTH_PER_HOUR if is_auth else LIMIT_ANON_PER_HOUR
        by_key = OrderedDict()
        for r in fresh:
            dev = (r.get("device_id") or "").strip()
            key = f"support:rate:dev:{dev}" if dev else f"support:rate:user:{getattr(user, 'pk', '')}"
            by_key.setdefault(key, []).append(r)

        accepted_items = []
        rejected = 0
        retry_after = 0
        for key, group in by_key.items():
            allowed = _consume_quota(key, len(group), limit)
            accepted_items.extend(group[:allowed])
            if allowed < len(group):
                rejected += len(group) - allowed
                retry_after = max(retry_after, _retry_after(key))

        # 3. Чистка секретов и сохранение
        objs = []
        for r in accepted_items:
            objs.append(SupportErrorReport(
                client_report_id=r["client_report_id"],
                company=company,
                app=r["app"][:32],
                version=r["version"][:32],
                os=sanitize_text(r.get("os", ""), 128),
                device_id=(r.get("device_id") or "")[:128],
                login=token_login or sanitize_text(r.get("login", ""), 128),
                level=r.get("level") or "error",
                category=r.get("category") or "other",
                fingerprint=r["fingerprint"],
                message=sanitize_text(r.get("message", ""), MAX_MESSAGE),
                stack=sanitize_text(r.get("stack") or "", MAX_STACK),
                context=sanitize_context(r.get("context") or []),
                count=r.get("count") or 1,
                first_at=r["first_at"],
                last_at=r["last_at"],
            ))

        inserted_ids = []
        if objs:
            SupportErrorReport.objects.bulk_create(objs, ignore_conflicts=True, batch_size=MAX_BATCH)
            # ignore_conflicts не сообщает, что вставлено, — проверяем по PK
            inserted_ids = [
                str(pk) for pk in SupportErrorReport.objects.filter(id__in=[o.id for o in objs])
                .values_list("id", flat=True)
            ]
            duplicates += len(objs) - len(inserted_ids)

        if inserted_ids:
            transaction.on_commit(lambda ids=inserted_ids: _enqueue(ids))

        body = {"accepted": len(inserted_ids), "duplicates": duplicates, "invalid": len(invalid)}
        if rejected:
            body["rejected"] = rejected
            body["detail"] = f"Превышен лимит отчётов ({limit}/час на устройство)."
            resp = Response(body, status=status.HTTP_429_TOO_MANY_REQUESTS)
            resp["Retry-After"] = str(retry_after or RATE_WINDOW)
            return resp
        return Response(body, status=status.HTTP_200_OK)


def _enqueue(ids):
    from apps.support.tasks import process_incoming_reports

    try:
        process_incoming_reports.delay(ids)
    except Exception as exc:
        # Отчёты сохранены; их подберёт periodic process_pending_reports
        logger.error("Failed to enqueue process_incoming_reports: %s", exc)


# ---------------------------------------------------------------- вложения

class AttachmentSerializer(serializers.Serializer):
    client_report_id = serializers.UUIDField()
    device_id = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    file = serializers.FileField(required=False)
    attachment = serializers.FileField(required=False)

    def validate(self, attrs):
        f = attrs.get("file") or attrs.get("attachment")
        if not f:
            raise serializers.ValidationError({"file": "Файл обязателен."})
        if f.size > ATTACHMENT_MAX_BYTES:
            raise serializers.ValidationError({"file": "Размер файла не должен превышать 5 МБ."})
        head = f.read(4)
        f.seek(0)
        if head[:2] != b"PK":
            raise serializers.ValidationError({"file": "Ожидается zip-архив."})
        attrs["upload"] = f
        return attrs


class ErrorReportAttachmentAPIView(APIView):
    """
    POST /api/support/error-reports/attachment/ — multipart: client_report_id, file (zip ≤ 5 МБ).
    Вложение хранится отдельно 30 дней; может прийти раньше отчёта.
    """

    authentication_classes = [TolerantAuthentication]
    permission_classes = [permissions.AllowAny]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        ser = AttachmentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        user = request.user
        is_auth = bool(user and user.is_authenticated)
        device_id = (data.get("device_id") or "").strip()
        if not is_auth and not device_id:
            raise exceptions.ValidationError({"device_id": "Без токена авторизации device_id обязателен."})

        key = (
            f"support:rate:att:user:{user.pk}" if is_auth
            else f"support:rate:att:dev:{device_id}"
        )
        ip_key = f"support:rate:att:ip:{_client_ip(request)}"
        if not _consume_quota(key, 1, ATTACHMENT_LIMIT_PER_HOUR) or not _consume_quota(
            ip_key, 1, ATTACHMENT_LIMIT_PER_HOUR * 20
        ):
            resp = Response(
                {"detail": f"Превышен лимит вложений ({ATTACHMENT_LIMIT_PER_HOUR}/час)."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            resp["Retry-After"] = str(_retry_after(key))
            return resp

        cid = data["client_report_id"]
        upload = data["upload"]
        company = _user_company(user) if is_auth else None

        att = SupportReportAttachment.objects.filter(client_report_id=cid).first()
        if att:
            if att.company_id and (company is None or att.company_id != company.pk):
                raise exceptions.PermissionDenied("Вложение принадлежит другой компании.")
            old = att.file
            if old:
                try:
                    old.delete(save=False)
                except Exception:
                    logger.warning("Failed to delete old support attachment %s", att.pk, exc_info=True)
        else:
            att = SupportReportAttachment(client_report_id=cid)
        att.file = upload
        att.size = upload.size
        att.device_id = device_id[:128]
        if company is not None:
            att.company = company
        att.save()

        return Response(
            {"ok": True, "client_report_id": str(cid), "size": att.size},
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------- вебхук бота

class SupportBotWebhookAPIView(APIView):
    """
    Вебхук технического бота (регистрация: manage.py setup_support_webhook --url https://host).
    Проверяет X-Telegram-Bot-Api-Secret-Token и принимает команды только из группы команды.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    parser_classes = [JSONParser]

    def post(self, request):
        cfg = get_support_config()
        secret = cfg.webhook_secret
        got = request.META.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
        if not secret or not hmac.compare_digest(str(got), str(secret)):
            return Response({"ok": False}, status=status.HTTP_403_FORBIDDEN)

        data = request.data if isinstance(request.data, dict) else {}
        message = data.get("message") or {}
        if not isinstance(message, dict):
            return Response({"ok": True})
        text = (message.get("text") or "").strip()
        chat_id = str((message.get("chat") or {}).get("id") or "")

        # Только группа команды NurMarket
        if not text or not chat_id or not cfg.chat_id or chat_id != str(cfg.chat_id):
            return Response({"ok": True})

        try:
            reply = handle_support_bot_command(text, chat_id=chat_id)
        except Exception:
            logger.exception("Support bot command failed: %s", text[:100])
            reply = "Ошибка при выполнении команды."
        if reply and cfg.token:
            _send_raw(cfg.token, chat_id, reply)
        return Response({"ok": True})
