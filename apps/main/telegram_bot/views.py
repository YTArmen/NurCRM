import logging
from decimal import Decimal
from typing import Optional
from django.utils.dateparse import parse_date
from django.db.models import Sum, Count, Q, Value, DecimalField
from django.db.models.functions import Coalesce, TruncDate
from rest_framework import permissions, status, generics
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError, NotFound
from rest_framework.pagination import PageNumberPagination

from apps.main.telegram_bot.models import (
    TelegramBotSettings,
    TelegramInquiry,
    TelegramCustomerProfile,
    TelegramMessageLog,
    TelegramBotScenario,
    TelegramBotAudit,
)
from apps.main.telegram_bot.serializers import (
    TelegramBotSettingsSerializer,
    TelegramInquirySerializer,
    TelegramCustomerSerializer,
    TelegramBotScenarioSerializer,
    TelegramBotAuditSerializer,
)
from apps.main.telegram_bot.services import telegram_api, ai_service, events_handler
from apps.main.models import Client

logger = logging.getLogger("telegram_bot.views")
ZERO_MONEY = Decimal("0.00")
MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)


def _get_company(request):
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        raise PermissionDenied("Требуется авторизация.")
    company = getattr(user, "owned_company", None) or getattr(user, "company", None)
    if not company:
        raise PermissionDenied("У пользователя не найдена компания.")
    return company


def _can_manage_bot_settings(user, company) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    if getattr(company, "owner_id", None) == user.id:
        return True
    if getattr(user, "role", "") in ("owner", "admin"):
        return True
    if getattr(user, "can_view_settings", False):
        return True
    return False


class TelegramInquiryPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


# =========================================================================
# 4.1 Настройки бота
# =========================================================================

class TelegramBotSettingsView(APIView):
    """
    GET /api/main/telegram-bot/settings/
    PATCH /api/main/telegram-bot/settings/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request)
        settings, _ = TelegramBotSettings.objects.get_or_create(company=company)
        serializer = TelegramBotSettingsSerializer(settings)
        return Response(serializer.data)

    def patch(self, request):
        company = _get_company(request)
        if not _can_manage_bot_settings(request.user, company):
            raise PermissionDenied("У вас нет прав на изменение настроек бота.")
        settings, _ = TelegramBotSettings.objects.get_or_create(company=company)
        old_data = {
            k: getattr(settings, k)
            for k in request.data.keys()
            if hasattr(settings, k)
        }
        serializer = TelegramBotSettingsSerializer(settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        changes = {}
        for k in request.data.keys():
            if hasattr(settings, k):
                new_v = getattr(settings, k)
                old_v = old_data.get(k)
                if old_v != new_v:
                    changes[k] = [_jsonable(old_v), _jsonable(new_v)]
        if changes:
            TelegramBotAudit.objects.create(
                company=company,
                user=request.user,
                user_name=getattr(request.user, "first_name", "") or getattr(request.user, "email", "owner"),
                action=TelegramBotAudit.Action.SETTINGS_UPDATE,
                object_title="Настройки бота",
                source=request.data.get("source") if request.data.get("source") in ("owner", "ai_advisor") else "owner",
                changes=changes,
            )
        if {"token", "owner_chat_id"} & set(changes):
            _sync_menu_quietly(company)  # ТЗ ч.12, 2.9: меню команд — сразу при подключении бота
        return Response(serializer.data)


BOT_CAPABILITIES = {
    "voice_in": True,
    "voice_out": True,
    "staff": True,
    "shift_archive": True,
    "product_actions": True,
    "invoice_photo": True,
    "debt_reminders": True,
}


class TelegramBotCapabilitiesView(APIView):
    """
    GET /api/main/telegram-bot/capabilities/ (ТЗ ч.15, п. 7) — что умеет бот на этом сервере.
    Касса по этому ответу показывает владельцу только рабочие переключатели.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(dict(BOT_CAPABILITIES))


def _jsonable(value):
    """Decimal/дата → строка, чтобы класть в JSONField журнала."""
    if isinstance(value, (Decimal,)) or hasattr(value, "isoformat"):
        return str(value)
    return value


def _sync_menu_quietly(company):
    try:
        sync_telegram_bot_menu(company)
    except Exception:
        logger.warning("sync_telegram_bot_menu failed for company %s", getattr(company, "id", None), exc_info=True)


class TelegramBotDetectOwnerChatView(APIView):
    """
    POST /api/main/telegram-bot/detect-owner-chat/
    Берёт последний чат, где написали /start, сохраняет как owner_chat_id.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _get_company(request)
        settings, _ = TelegramBotSettings.objects.get_or_create(company=company)

        # Ищем последнее сообщение со словом /start
        last_log = (
            TelegramMessageLog.objects.filter(bot_settings=settings, text__icontains="/start")
            .order_by("-created_at")
            .first()
        )
        if not last_log:
            # Попробуем самое последнее сообщение вообще, если /start не найден
            last_log = (
                TelegramMessageLog.objects.filter(bot_settings=settings)
                .order_by("-created_at")
                .first()
            )

        if not last_log:
            return Response(
                {"detail": "Напишите боту /start и повторите"},
                status=status.HTTP_404_NOT_FOUND,
            )

        settings.owner_chat_id = str(last_log.chat_id)
        settings.owner_chat_title = last_log.chat_title or last_log.sender_name or "Nur"
        settings.save(update_fields=["owner_chat_id", "owner_chat_title"])
        _sync_menu_quietly(company)

        return Response({
            "owner_chat_id": settings.owner_chat_id,
            "owner_chat_title": settings.owner_chat_title,
        })


class TelegramBotTestMessageView(APIView):
    """
    POST /api/main/telegram-bot/test-message/
    Пробное сообщение владельцу.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _get_company(request)
        settings, _ = TelegramBotSettings.objects.get_or_create(company=company)

        if not settings.token:
            return Response({"detail": "Токен бота не настроен"}, status=status.HTTP_400_BAD_REQUEST)

        if not settings.owner_chat_id:
            return Response({"detail": "Чат владельца (owner_chat_id) не настроен"}, status=status.HTTP_400_BAD_REQUEST)

        test_text = (
            "✅ <b>Тестовое сообщение от бота NurCRM!</b>\n"
            f"Связь с магазином «{getattr(company, 'name', '')}» успешно установлена.\n"
            "Вы будете получать сюда сводки смен, уведомления о заказах и заканчивающихся товарах."
        )

        res = telegram_api.send_message(settings.token, settings.owner_chat_id, test_text, parse_mode="HTML")
        if res.get("ok"):
            return Response({"ok": True})
        else:
            desc = res.get("description", "Не удалось отправить сообщение в Telegram")
            return Response({"ok": False, "detail": desc}, status=status.HTTP_400_BAD_REQUEST)


class TelegramBotTestAIView(APIView):
    """
    POST /api/main/telegram-bot/test-ai/
    Короткий запрос к Google Gemini для проверки работоспособности ключа и вызова функций аналитики.
    Тело (необязательно): {"question": "какая прибыль за сентябрь?"}
    Ответ: {"ok": true, "answer": "...", "model": "...", "functions_called": [...]}
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _get_company(request)
        settings, _ = TelegramBotSettings.objects.get_or_create(company=company)

        ai_key = ai_service.get_effective_ai_key(settings.ai_key)
        if not ai_key:
            return Response(
                {"detail": "Ключ Google Gemini не указан (ни в компании, ни на сервере)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        question = (request.data.get("question") or "").strip()
        if question:
            try:
                answer, model, functions_called = ai_service.generate_owner_ai_response(
                    company=company,
                    settings=settings,
                    user_question=question,
                )
                if not (answer or "").strip():
                    return Response({"ok": False, "detail": "ИИ вернул пустой ответ"}, status=status.HTTP_400_BAD_REQUEST)
                return Response({
                    "ok": True,
                    "answer": answer,
                    "model": model,
                    "functions_called": functions_called,
                })
            except Exception as exc:
                logger.exception("test-ai question execution failed: %s", exc)
                return Response({"ok": False, "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        res = ai_service.test_ai(ai_key)
        if res.get("ok"):
            ans = (res.get("answer") or "").strip()
            if not ans:
                return Response({"ok": False, "detail": "ИИ вернул пустой ответ"}, status=status.HTTP_400_BAD_REQUEST)
            return Response({
                "ok": True,
                "answer": ans,
                "model": res.get("model"),
                "functions_called": [],
            })
        else:
            return Response({"ok": False, "detail": res.get("error") or "ИИ вернул пустой ответ"}, status=status.HTTP_400_BAD_REQUEST)


# =========================================================================
# 4.2 Аналитика бота
# =========================================================================

class TelegramBotStatsView(APIView):
    """
    GET /api/main/telegram-bot/stats/?date_from=2026-09-01&date_to=2026-09-30
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request)
        qs = TelegramInquiry.objects.filter(company=company)

        date_from_str = request.query_params.get("date_from")
        if date_from_str:
            df = parse_date(date_from_str)
            if df:
                qs = qs.filter(created_at__date__gte=df)

        date_to_str = request.query_params.get("date_to")
        if date_to_str:
            dt = parse_date(date_to_str)
            if dt:
                qs = qs.filter(created_at__date__lte=dt)

        # Общие агрегации за 1 запрос
        agg = qs.aggregate(
            total_messages=Count("id"),
            total_people=Count("chat_id", distinct=True),
            total_orders=Count("order_id", distinct=True, filter=Q(order__isnull=False)),
            total_orders_sum=Coalesce(
                Sum("order__total", filter=Q(order__isnull=False)),
                Value(ZERO_MONEY, output_field=MONEY_FIELD),
            ),
        )

        messages = agg["total_messages"] or 0
        people = agg["total_people"] or 0
        orders = agg["total_orders"] or 0
        orders_total = agg["total_orders_sum"] or ZERO_MONEY

        # Группировка по дням за 1 запрос
        daily = (
            qs.annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(
                d_messages=Count("id"),
                d_people=Count("chat_id", distinct=True),
                d_orders=Count("order_id", distinct=True, filter=Q(order__isnull=False)),
            )
            .order_by("day")
        )

        by_day = [
            {
                "date": d["day"].isoformat() if d["day"] else "",
                "messages": d["d_messages"],
                "people": d["d_people"],
                "orders": d["d_orders"],
            }
            for d in daily
            if d["day"]
        ]

        return Response({
            "messages": messages,
            "people": people,
            "orders": orders,
            "orders_total": f"{orders_total:.2f}",
            "by_day": by_day,
        })


class TelegramBotInquiriesView(generics.ListAPIView):
    """
    GET /api/main/telegram-bot/inquiries/?date_from=&date_to=&chat_id=&page=
    Лента обращений покупателей (новые сверху).
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = TelegramInquirySerializer
    pagination_class = TelegramInquiryPagination

    def get_queryset(self):
        company = _get_company(self.request)
        qs = TelegramInquiry.objects.filter(company=company).select_related("order")

        chat_id = self.request.query_params.get("chat_id")
        if chat_id:
            qs = qs.filter(chat_id=chat_id)

        date_from_str = self.request.query_params.get("date_from")
        if date_from_str:
            df = parse_date(date_from_str)
            if df:
                qs = qs.filter(created_at__date__gte=df)

        date_to_str = self.request.query_params.get("date_to")
        if date_to_str:
            dt = parse_date(date_to_str)
            if dt:
                qs = qs.filter(created_at__date__lte=dt)

        return qs.order_by("-created_at")


class TelegramBotCustomersView(APIView):
    """
    GET /api/main/telegram-bot/customers/?date_from=&date_to=
    Список покупателей бота: chat_id, name, username, messages, orders, last_at, client_id.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request)

        date_from_str = request.query_params.get("date_from")
        date_to_str = request.query_params.get("date_to")

        df = parse_date(date_from_str) if date_from_str else None
        dt = parse_date(date_to_str) if date_to_str else None

        # Если есть фильтрация по датам, считаем количество сообщений и заказов в рамках периода
        inq_filter = Q(company=company)
        if df:
            inq_filter &= Q(created_at__date__gte=df)
        if dt:
            inq_filter &= Q(created_at__date__lte=dt)

        profiles = (
            TelegramCustomerProfile.objects.filter(company=company)
            .select_related("client")
            .order_by("-last_at")
        )

        # Вычисляем периодные агрегаты по chat_id если задан фильтр дат
        period_stats = {}
        if df or dt:
            period_rows = (
                TelegramInquiry.objects.filter(inq_filter)
                .values("chat_id")
                .annotate(
                    msg_cnt=Count("id"),
                    ord_cnt=Count("order_id", distinct=True, filter=Q(order__isnull=False)),
                )
            )
            for r in period_rows:
                period_stats[r["chat_id"]] = (r["msg_cnt"], r["ord_cnt"])

        # Карта привязанных клиентов компании
        client_map = {
            c.telegram_chat_id: str(c.id)
            for c in Client.objects.filter(company=company, telegram_chat_id__isnull=False)
            if c.telegram_chat_id
        }

        results = []
        for p in profiles:
            if df or dt:
                if p.chat_id not in period_stats:
                    continue
                msgs, ords = period_stats[p.chat_id]
            else:
                msgs = p.messages_count
                ords = p.orders_count

            cid = str(p.client_id) if p.client_id else client_map.get(p.chat_id)

            results.append({
                "chat_id": p.chat_id,
                "name": p.name,
                "username": p.username,
                "messages": msgs,
                "orders": ords,
                "last_at": p.last_at.isoformat() if p.last_at else None,
                "client_id": cid,
            })

        return Response(results)


# =========================================================================
# 4.3 Уведомление о закрытии смены из кассы
# =========================================================================

class TelegramBotNotifyShiftClosedView(APIView):
    """
    POST /api/main/telegram-bot/notify/shift-closed/
    Тело: {"shift_id": "..."}
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _get_company(request)
        shift_id = request.data.get("shift_id")
        if not shift_id:
            raise ValidationError({"shift_id": "Обязательное поле."})

        ok = events_handler.send_shift_closed_notification(company.id, shift_id)
        return Response({"ok": ok})


# =========================================================================
# ТЗ-07 1.1.3 Мониторинг очереди ботов (для команды, не для клиентов)
# =========================================================================

class TelegramBotQueueHealthView(APIView):
    """
    GET /api/main/telegram-bot/queue-health/
    Длина очереди, задержка последней пробы и последнего обновления, боты с ошибками вебхука.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        import time
        from django.core.cache import cache
        from apps.main.telegram_bot.tasks import QUEUE_METRICS_KEY, QUEUE_ALERT_SECONDS

        # ТЗ Н-23 (6): доступно владельцу/админу компании (раньше только is_staff → 403)
        company = _get_company(request)
        if not _can_manage_bot_settings(request.user, company):
            raise PermissionDenied("Недостаточно прав.")
        metrics = cache.get(QUEUE_METRICS_KEY) or {}
        server_bots = TelegramBotSettings.objects.filter(mode=TelegramBotSettings.Mode.SERVER)
        probe_sent = metrics.get("probe_sent_at") or 0
        probe_done = metrics.get("probe_done_at") or 0
        stuck_for = time.time() - probe_sent if probe_sent and probe_done < probe_sent else 0
        return Response({
            "queue_length": metrics.get("queue_length"),
            "probe_latency_seconds": metrics.get("probe_latency"),
            "probe_pending_seconds": round(stuck_for, 1),
            "last_update_latency_seconds": metrics.get("last_latency"),
            "max_update_latency_5m_seconds": metrics.get("max_latency_5m"),
            "alert_threshold_seconds": QUEUE_ALERT_SECONDS,
            "healthy": stuck_for < 120 and (metrics.get("probe_latency") or 0) <= QUEUE_ALERT_SECONDS,
            "server_bots": server_bots.count(),
            "server_bots_webhook_failed": server_bots.filter(webhook_ok=False).count(),
        })


# =========================================================================
# ТЗ-11 Свои сценарии, команды, аудит и меню Telegram-бота
# =========================================================================

def sync_telegram_bot_menu(company):
    """
    Пересобирает setMyCommands для Telegram-бота компании:
    - default (клиенты, ru & ky): встроенные команды + сценарии command с show_in_menu=True
    - chat (владелец): команды владельца + сценарии владельца
    """
    settings = TelegramBotSettings.objects.filter(company=company).first()
    if not settings or not settings.token:
        return False

    token = settings.token

    # 1. Default customer commands (ru)
    base_commands_ru = [
        {"command": "start", "description": "Главное меню"},
        {"command": "catalog", "description": "Каталог товаров"},
        {"command": "cart", "description": "Корзина"},
        {"command": "orders", "description": "Мои заказы"},
        {"command": "help", "description": "Помощь"},
    ]
    base_commands_ky = [
        {"command": "start", "description": "Башкы меню"},
        {"command": "catalog", "description": "Каталог"},
        {"command": "cart", "description": "Себет"},
        {"command": "orders", "description": "Менин буйрутмаларым"},
        {"command": "help", "description": "Жардам"},
    ]

    custom_cust_scenarios = TelegramBotScenario.objects.filter(
        company=company,
        is_active=True,
        kind=TelegramBotScenario.Kind.COMMAND,
        show_in_menu=True,
        audience__in=[TelegramBotScenario.Audience.CUSTOMERS, TelegramBotScenario.Audience.ALL],
    ).order_by("-priority", "title")

    for sc in custom_cust_scenarios:
        cmd_dict = {"command": sc.command, "description": sc.title[:256]}
        if not any(c["command"] == sc.command for c in base_commands_ru):
            base_commands_ru.append(cmd_dict)
            base_commands_ky.append(cmd_dict)

    telegram_api.set_my_commands(token, base_commands_ru, scope={"type": "default"})
    telegram_api.set_my_commands(token, base_commands_ky, scope={"type": "default"}, language_code="ky")

    # 2. Owner chat commands
    if settings.owner_chat_id:
        try:
            owner_chat_int = int(settings.owner_chat_id)
            owner_commands = [
                {"command": "segodnya", "description": "Сводка за сегодня"},
                {"command": "dolgi", "description": "Список должников"},
                {"command": "ostatki", "description": "Остатки товаров"},
                {"command": "zakaz", "description": "Заказы товаров"},
                {"command": "prokat", "description": "Прокат и бронь"},
            ]
            custom_owner_scenarios = TelegramBotScenario.objects.filter(
                company=company,
                is_active=True,
                kind=TelegramBotScenario.Kind.COMMAND,
                show_in_menu=True,
                audience__in=[TelegramBotScenario.Audience.OWNER, TelegramBotScenario.Audience.ALL],
            ).order_by("-priority", "title")
            for sc in custom_owner_scenarios:
                cmd_dict = {"command": sc.command, "description": sc.title[:256]}
                if not any(c["command"] == sc.command for c in owner_commands):
                    owner_commands.append(cmd_dict)
            telegram_api.set_my_commands(token, owner_commands, scope={"type": "chat", "chat_id": owner_chat_int})
        except (ValueError, TypeError):
            pass
    return True


def match_scenario(company, text: str, audience: str = "customers") -> Optional[TelegramBotScenario]:
    """
    Ищет подходящий сценарий по правилам ТЗ-11:
    1) Если текст - команда /cmd -> поиск kind='command'
    2) Поиск kind='keywords' по границам слов (ё=е, регистронезависимо)
    Сортировка по -priority, title
    """
    import re

    clean_text = (text or "").strip()
    if not clean_text:
        return None

    qs = TelegramBotScenario.objects.filter(
        company=company,
        is_active=True,
    ).filter(
        Q(audience=audience) | Q(audience=TelegramBotScenario.Audience.ALL)
    ).order_by("-priority", "title")

    # 1. Если команда
    if clean_text.startswith("/"):
        cmd = clean_text.lstrip("/").split()[0].lower()
        matched = qs.filter(kind=TelegramBotScenario.Kind.COMMAND, command=cmd).first()
        if matched:
            return matched

    # 2. Если ключевые слова
    norm_text = clean_text.lower().replace("ё", "е")
    for sc in qs.filter(kind=TelegramBotScenario.Kind.KEYWORDS):
        for kw in (sc.keywords or []):
            kw_norm = str(kw).strip().lower().replace("ё", "е")
            if not kw_norm:
                continue
            pattern = r'(?:\b|^|\s)' + re.escape(kw_norm) + r'(?:\b|$|\s)'
            if re.search(pattern, norm_text):
                return sc
    return None


class TelegramBotScenarioListCreateView(APIView):
    """
    GET /api/main/telegram-bot/scenarios/
    POST /api/main/telegram-bot/scenarios/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request)
        qs = TelegramBotScenario.objects.filter(company=company).order_by("-priority", "title")
        serializer = TelegramBotScenarioSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        company = _get_company(request)
        if not _can_manage_bot_settings(request.user, company):
            raise PermissionDenied("У вас нет права на управление настройками.")

        if TelegramBotScenario.objects.filter(company=company).count() >= 100:
            raise ValidationError({"detail": "Достигнут лимит 100 сценариев на компанию."})

        serializer = TelegramBotScenarioSerializer(data=request.data, context={"company": company, "request": request})
        serializer.is_valid(raise_exception=True)
        source = request.data.get("source") or TelegramBotScenario.Source.OWNER
        if source not in (TelegramBotScenario.Source.OWNER, TelegramBotScenario.Source.AI_ADVISOR):
            source = TelegramBotScenario.Source.OWNER

        scenario = serializer.save(
            company=company,
            created_by=request.user,
            updated_by=request.user,
            source=source,
        )

        TelegramBotAudit.objects.create(
            company=company,
            user=request.user,
            user_name=getattr(request.user, "first_name", "") or getattr(request.user, "email", "owner"),
            action=TelegramBotAudit.Action.SCENARIO_CREATE,
            object_title=scenario.title,
            source=source,
            changes={f: [None, getattr(scenario, f)] for f in ("kind", "command", "keywords", "title", "reply_text", "is_active", "show_in_menu") if getattr(scenario, f)},
        )

        if scenario.kind == TelegramBotScenario.Kind.COMMAND and scenario.show_in_menu:
            sync_telegram_bot_menu(company)

        return Response(TelegramBotScenarioSerializer(scenario).data, status=status.HTTP_201_CREATED)


class TelegramBotScenarioDetailView(APIView):
    """
    GET /api/main/telegram-bot/scenarios/{id}/
    PATCH /api/main/telegram-bot/scenarios/{id}/
    DELETE /api/main/telegram-bot/scenarios/{id}/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        company = _get_company(request)
        scenario = generics.get_object_or_404(TelegramBotScenario, id=pk, company=company)
        return Response(TelegramBotScenarioSerializer(scenario).data)

    def patch(self, request, pk):
        company = _get_company(request)
        if not _can_manage_bot_settings(request.user, company):
            raise PermissionDenied("У вас нет права на управление настройками.")
        scenario = generics.get_object_or_404(TelegramBotScenario, id=pk, company=company)

        old_vals = {
            f: getattr(scenario, f)
            for f in ("kind", "command", "keywords", "title", "reply_text", "is_active", "show_in_menu", "priority", "audience")
        }

        serializer = TelegramBotScenarioSerializer(scenario, data=request.data, partial=True, context={"company": company, "request": request})
        serializer.is_valid(raise_exception=True)
        scenario = serializer.save(updated_by=request.user)

        changes = {}
        for f, old_v in old_vals.items():
            new_v = getattr(scenario, f)
            if old_v != new_v:
                changes[f] = [old_v, new_v]

        if changes:
            TelegramBotAudit.objects.create(
                company=company,
                user=request.user,
                user_name=getattr(request.user, "first_name", "") or getattr(request.user, "email", "owner"),
                action=TelegramBotAudit.Action.SCENARIO_UPDATE,
                object_title=scenario.title,
                source=request.data.get("source") if request.data.get("source") in ("owner", "ai_advisor") else "owner",
                changes=changes,
            )

        if "command" in changes or "show_in_menu" in changes or "is_active" in changes:
            sync_telegram_bot_menu(company)

        return Response(TelegramBotScenarioSerializer(scenario).data)

    def delete(self, request, pk):
        company = _get_company(request)
        if not _can_manage_bot_settings(request.user, company):
            raise PermissionDenied("У вас нет права на управление настройками.")
        scenario = generics.get_object_or_404(TelegramBotScenario, id=pk, company=company)
        title = scenario.title
        was_in_menu = scenario.show_in_menu and scenario.kind == TelegramBotScenario.Kind.COMMAND

        TelegramBotAudit.objects.create(
            company=company,
            user=request.user,
            user_name=getattr(request.user, "first_name", "") or getattr(request.user, "email", "owner"),
            action=TelegramBotAudit.Action.SCENARIO_DELETE,
            object_title=title,
            source=request.data.get("source") if request.data.get("source") in ("owner", "ai_advisor") else "owner",
            changes={},
        )

        scenario.delete()
        if was_in_menu:
            sync_telegram_bot_menu(company)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TelegramBotScenarioTestView(APIView):
    """
    POST /api/main/telegram-bot/scenarios/test/
    Тело: {"text": "а доставка есть?", "audience": "customers"}
    Ответ: {"matched": {id, title, kind} | null, "reply_text": "...", "buttons": [...]}
    Ничего не отправляет в Telegram — проверка "что ответит бот".
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _get_company(request)
        text = (request.data.get("text") or "").strip()
        audience = request.data.get("audience") or "customers"

        matched = match_scenario(company, text, audience=audience)
        if matched:
            return Response({
                "matched": {
                    "id": str(matched.id),
                    "title": matched.title,
                    "kind": matched.kind,
                },
                "reply_text": matched.reply_text,
                "buttons": matched.buttons,
                "photo_product": str(matched.photo_product_id) if matched.photo_product_id else None,
            })
        return Response({
            "matched": None,
            "reply_text": None,
            "buttons": [],
        })


class TelegramBotScenarioSyncMenuView(APIView):
    """
    POST /api/main/telegram-bot/scenarios/sync-menu/
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _get_company(request)
        if not _can_manage_bot_settings(request.user, company):
            raise PermissionDenied("У вас нет права на управление настройками.")
        ok = sync_telegram_bot_menu(company)
        return Response({"ok": ok})


class TelegramBotAuditListView(APIView):
    """
    GET /api/main/telegram-bot/audit/?limit=50
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _get_company(request)
        try:
            limit = min(max(1, int(request.query_params.get("limit") or 50)), 200)
        except (ValueError, TypeError):
            limit = 50

        qs = TelegramBotAudit.objects.filter(company=company).order_by("-created_at")[:limit]
        serializer = TelegramBotAuditSerializer(qs, many=True)
        return Response(serializer.data)


# =========================================================================
# ТЗ Н-23 (1), (2): уведомление владельцу и коды подтверждения владельца
# =========================================================================

class BotNotifyOwnerView(APIView):
    """POST /api/main/bot/notify-owner/ {text, buttons?, dedupe_key?}"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from apps.main.telegram_bot.services import owner_notify
        company = _get_company(request)
        text = str(request.data.get("text") or "").strip()
        if not text:
            raise ValidationError({"text": "Обязательное поле."})
        code, payload = owner_notify.notify_owner(
            company, text, buttons=request.data.get("buttons"),
            dedupe_key=request.data.get("dedupe_key") or "",
        )
        return Response(payload, status=code)


class TelegramBotOwnerCodeView(APIView):
    """POST /api/main/telegram-bot/owner-code/ — владелец/админ получает одноразовый код (10 мин)."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from apps.main.telegram_bot.services import owner_notify
        company = _get_company(request)
        if not _can_manage_bot_settings(request.user, company):
            raise PermissionDenied("Недостаточно прав.")
        code, expires_at = owner_notify.issue_owner_code(company, request.user)
        return Response({"code": code, "expires_at": expires_at, "ttl_seconds": 600}, status=status.HTTP_201_CREATED)


class TelegramBotOwnerCodeVerifyView(APIView):
    """POST /api/main/telegram-bot/owner-code/verify/ {code, chat_id, from_id?, chat_title?}"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from apps.main.telegram_bot.services import owner_notify
        company = _get_company(request)
        ok, reason = owner_notify.verify_owner_code(
            company, request.data.get("code"), str(request.data.get("chat_id") or ""),
            str(request.data.get("from_id") or ""), str(request.data.get("chat_title") or ""),
        )
        if ok:
            _sync_menu_quietly(company)
            return Response({"ok": True})
        return Response({"ok": False, "reason": reason}, status=status.HTTP_400_BAD_REQUEST)
