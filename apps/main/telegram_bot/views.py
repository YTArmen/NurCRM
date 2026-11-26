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
        if question and request.data.get("as") == "customer":
            # bot2611: «Проверить бота» — ответ как покупателю (тот же промпт, заказ не создаётся, продавца не зовёт)
            from apps.main.telegram_bot.services import seller
            from apps.main.telegram_bot.services.customer_handler import build_customer_system_instruction, extract_order_json
            try:
                scfg = seller.get_config(settings)
                system_instruction, matched = build_customer_system_instruction(settings, company, question, [], [], scfg)
                with ai_service.CompanyAiConcurrencyLimit(company.id):
                    raw, model = ai_service.generate_chat_response(
                        api_key=ai_key, system_instruction=system_instruction,
                        contents=[{"role": "user", "parts": [{"text": question}]}], temperature=0.3, max_tokens=600)
                reason, raw = seller.extract_handoff_tag(raw) if seller.is_on(scfg) else (None, raw)
                from apps.main.telegram_bot.services import bargain
                bg_tag, raw = bargain.extract_tag(raw) if bargain.is_enabled(scfg) else (None, raw)
                order_data, clean = extract_order_json(raw)
                text_out = bargain.filter_prices(company, f"test:{request.user.id}", clean or raw) if seller.is_on(scfg) else (clean or raw)
                bg_res = None
                if bg_tag:  # bot2711: показать, что ответит движок торга (тест-чат владельца, в отчёт не идёт)
                    bg_text, _mk = bargain.run_tag(settings, f"test:{request.user.id}", bg_tag, "ru")
                    if bg_text:
                        text_out = bg_text
                    bg_res = {"tag": bg_tag, "text": bg_text}
                answer = ai_service.normalize_ai_output_for_telegram(seller.postprocess(scfg, text_out, True))
                return Response({"ok": True, "answer": answer, "model": model, "functions_called": [], "bargain": bg_res,
                                 "handoff": reason, "order_draft": order_data or None,
                                 "catalog_matches": [m.get("id") for m in matched][:10]})
            except Exception as exc:
                logger.exception("test-ai customer failed: %s", exc)
                return Response({"ok": False, "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
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

class TelegramBotOutboxView(APIView):
    """
    GET /api/main/telegram-bot/outbox/?client=<uuid>&status=&kind=&page_size=≤100&page=
    ТЗ кассы 05.11 п.31 / Б-30: журнал сообщений покупателям (отправлено, отложено, пропущено и почему).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.telegram_bot.models import TelegramCustomerOutbox
        company = _get_company(request)
        if not _can_manage_bot_settings(request.user, company):
            raise PermissionDenied("Недостаточно прав.")
        qs = TelegramCustomerOutbox.objects.filter(company=company).select_related("client")
        p = request.query_params
        if p.get("client"):
            try:
                import uuid as _uuid
                qs = qs.filter(client_id=_uuid.UUID(p["client"]))
            except ValueError:
                raise ValidationError({"client": "Недопустимый id."})
        if p.get("status"):
            qs = qs.filter(status=p["status"])
        if p.get("kind"):
            qs = qs.filter(kind=p["kind"])
        try:
            size = max(1, min(int(p.get("page_size", 50)), 100))
            page = max(1, int(p.get("page", 1)))
        except ValueError:
            raise ValidationError({"page": "Недопустимое значение."})
        total = qs.count()
        rows = qs.order_by("-created_at")[(page - 1) * size: page * size]
        return Response({"count": total, "page": page, "page_size": size, "results": [{
            "id": str(r.id), "client": {"id": str(r.client_id), "full_name": r.client.full_name} if r.client_id else None,
            "kind": r.kind, "category": r.category, "language": r.language, "text": r.text, "status": r.status,
            "reason": r.reason or None, "error": r.error or None, "dedupe_key": r.dedupe_key or None,
            "send_after": r.send_after.isoformat() if r.send_after else None,
            "sent_at": r.sent_at.isoformat() if r.sent_at else None, "created_at": r.created_at.isoformat(),
        } for r in rows]})


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


# =========================================================================
# bot2611: бот-продавец Ф1 — диалоги, передачи продавцу, отчёт
# =========================================================================

BOT_CAPABILITIES.update({"bot_seller": True, "bot_handoff": True, "bot_sales_stats": True})
BOT_ORDER_SOURCES = ("telegram", "telegram_bot")


def _period(request):
    """?from=YYYY-MM-DD&to=YYYY-MM-DD (или date_from/date_to); по умолчанию последние 30 дней."""
    from datetime import timedelta
    from django.utils import timezone
    qp = request.query_params
    today = timezone.localdate()
    df = parse_date(qp.get("from") or qp.get("date_from") or "") or (today - timedelta(days=29))
    dt = parse_date(qp.get("to") or qp.get("date_to") or "") or today
    if df > dt:
        raise ValidationError({"from": "Дата «с» позже даты «по»."})
    if (dt - df).days > 366:
        raise ValidationError({"from": "Период не больше года."})
    return df, dt


def _unknown_rows(company, unknown):
    top = sorted(unknown, key=lambda x: x.created_at, reverse=True)[:30]
    profs = {p.chat_id: p for p in TelegramCustomerProfile.objects.filter(
        company=company, chat_id__in={h.chat_id for h in top}).only("chat_id", "name", "client_id")}
    out = []
    for h in top:
        pr = profs.get(h.chat_id)
        out.append({"at": h.created_at, "chat_id": h.chat_id, "text": h.question[:300],
                    "name": (h.customer_name or (pr.name if pr else "") or ""),
                    "client_id": str(pr.client_id) if pr and pr.client_id else None})
    return out


def _dialog_status(h):
    if h is None:
        return "bot"
    from django.utils import timezone
    if h.status in ("waiting", "taken") and h.until and h.until > timezone.now():
        return "needs_human" if h.status == "waiting" else "human"
    if h.status == "callback" and not h.closed_at and h.created_at > timezone.now() - __import__("datetime").timedelta(days=3):
        return "callback"
    return "bot"


def _handoff_json(h):
    return {
        "id": str(h.id), "reason": h.reason, "status": h.status, "question": h.question,
        "phone": h.phone, "note": h.note, "taken_by": h.taken_by,
        "created_at": h.created_at, "taken_at": h.taken_at, "first_answer_at": h.first_answer_at,
        "until": h.until, "closed_at": h.closed_at,
        "order": ({"id": str(h.order_id), "number": h.order.number, "total": str(h.order.total)} if h.order_id else None),
    }


class TelegramBotDialogsView(APIView):
    """GET /api/main/telegram-bot/dialogs/?status=bot|needs_human|human|callback&from=&to=&page=&page_size=
    Диалоги покупателей с ботом (по профилю чата): последняя активность, сообщения, заказы, статус передачи продавцу."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from django.db.models import OuterRef, Subquery
        from apps.main.telegram_bot.models import TelegramHandoff
        company = _get_company(request)
        df, dt = _period(request)
        want = request.query_params.get("status") or ""
        if want and want not in ("bot", "needs_human", "human", "callback", "closed"):
            raise ValidationError({"status": "bot, needs_human, human, callback или closed"})
        last_text = TelegramInquiry.objects.filter(company=company, chat_id=OuterRef("chat_id")).order_by("-created_at")
        qs = (TelegramCustomerProfile.objects.filter(company=company, last_at__date__gte=df, last_at__date__lte=dt)
              .annotate(last_text=Subquery(last_text.values("text")[:1]))
              .order_by("-last_at"))
        if want:
            from django.utils import timezone
            now = timezone.now()
            live = TelegramHandoff.objects.filter(company=company)
            if want == "needs_human":
                chats = live.filter(status="waiting", until__gt=now)
            elif want == "human":
                chats = live.filter(status="taken", until__gt=now)
            elif want == "callback":
                chats = live.filter(status="callback", closed_at__isnull=True, created_at__gt=now - __import__("datetime").timedelta(days=3))
            elif want == "closed":  # botr2611: заявки, вернувшиеся боту (returned/expired)
                busy = live.filter(Q(status__in=("waiting", "taken"), until__gt=now) | Q(status="callback", closed_at__isnull=True))
                chats = live.filter(status__in=("returned", "expired")).exclude(chat_id__in=busy.values("chat_id"))
            else:
                chats = None
            if chats is not None:
                qs = qs.filter(chat_id__in=chats.values("chat_id"))
            else:
                busy = live.filter(Q(status__in=("waiting", "taken"), until__gt=now) | Q(status="callback", closed_at__isnull=True))
                qs = qs.exclude(chat_id__in=busy.values("chat_id"))
        pager = TelegramInquiryPagination()
        page = pager.paginate_queryset(qs, request, view=self)
        chat_ids = [p.chat_id for p in page]
        latest = {}
        for h in TelegramHandoff.objects.filter(company=company, chat_id__in=chat_ids).exclude(status="logged").order_by("chat_id", "-created_at"):
            latest.setdefault(h.chat_id, h)
        rows = [{
            "chat_id": p.chat_id, "name": p.name, "username": p.username,
            "client_id": str(p.client_id) if p.client_id else None,
            "messages_count": p.messages_count, "orders_count": p.orders_count,
            "last_at": p.last_at, "last_text": (p.last_text or "")[:200],
            "status": _dialog_status(latest.get(p.chat_id)),
            "handoff": _handoff_json(latest[p.chat_id]) if p.chat_id in latest else None,
        } for p in page]
        return pager.get_paginated_response(rows)


class TelegramBotDialogDetailView(APIView):
    """GET /api/main/telegram-bot/dialogs/<chat_id>/ — переписка (последние 200), заказы и передачи продавцу.
    POST {"action": "take"|"return"} — взять диалог / вернуть боту из программы владельца."""
    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, chat_id):
        company = _get_company(request)
        profile = TelegramCustomerProfile.objects.filter(company=company, chat_id=str(chat_id)).first()
        if profile is None:
            raise NotFound("Диалог не найден.")
        return company, profile

    def get(self, request, chat_id):
        from apps.main.telegram_bot.models import TelegramHandoff
        from apps.main.models import ShowcaseOrder
        company, p = self._get(request, chat_id)
        msgs = list(TelegramInquiry.objects.filter(company=company, chat_id=p.chat_id)
                    .select_related("order").order_by("-created_at")[:200])
        msgs.reverse()
        hs = list(TelegramHandoff.objects.filter(company=company, chat_id=p.chat_id).select_related("order")[:50])
        orders = ShowcaseOrder.objects.filter(company=company, telegram_chat_id=p.chat_id).order_by("-created_at")[:20]
        live = next((h for h in hs if h.status != "logged"), None)
        return Response({
            "chat_id": p.chat_id, "name": p.name, "username": p.username,
            "client_id": str(p.client_id) if p.client_id else None,
            "messages_count": p.messages_count, "orders_count": p.orders_count, "last_at": p.last_at,
            "status": _dialog_status(live),
            "messages": [{"at": m.created_at, "text": m.text, "reply": m.reply, "is_voice": m.is_voice,
                          "order_number": m.order.number if m.order_id else None} for m in msgs],
            "handoffs": [_handoff_json(h) for h in hs],
            "orders": [{"id": str(o.id), "number": o.number, "total": str(o.total), "status": o.status,
                        "created_at": o.created_at} for o in orders],
        })

    def post(self, request, chat_id):
        from apps.main.telegram_bot.models import TelegramHandoff
        from apps.main.telegram_bot.services import seller
        company, p = self._get(request, chat_id)
        if not _can_manage_bot_settings(request.user, company):
            raise PermissionDenied("Недостаточно прав.")
        action = request.data.get("action")
        if action not in ("take", "return", "reply"):
            raise ValidationError({"action": "take, return или reply"})
        bs = TelegramBotSettings.objects.select_related("company").filter(company=company).first()
        if bs is None:
            raise NotFound("Бот не подключён.")
        if action == "reply":  # botr2611: ответ покупателю из программы владельца
            text = str(request.data.get("text") or "").strip()
            if not text:
                raise ValidationError({"text": "Пустой ответ."})
            if len(text) > 4000:
                raise ValidationError({"text": "Не больше 4000 знаков."})
            if not bs.token:
                raise ValidationError({"detail": "У бота нет токена."})
            by = getattr(request.user, "first_name", "") or getattr(request.user, "email", "") or "владелец"
            h, err = seller.reply_from_app(bs, p.chat_id, text, by)
            if err:
                return Response({"detail": "Не доставлено: " + err, "code": "telegram_error"}, status=502)
            return Response({"status": _dialog_status(h), "sent": True, "handoff": _handoff_json(h)})
        h = seller.active_handoff(company, p.chat_id)
        if action == "take":
            if h is None:
                h = TelegramHandoff.objects.create(company=company, chat_id=p.chat_id, customer_name=p.name[:255],
                                                   reason="human", status="waiting")
            by = getattr(request.user, "first_name", "") or getattr(request.user, "email", "") or "владелец"
            seller.take(bs, h, by)
        else:
            from django.utils import timezone
            n_cb = TelegramHandoff.objects.filter(company=company, chat_id=p.chat_id, status="callback",
                                                  closed_at__isnull=True).update(closed_at=timezone.now())
            if h is None:
                return Response({"status": "bot", "changed": bool(n_cb)})
            seller.give_back(bs, h)
        return Response({"status": _dialog_status(h if action == "take" else None), "changed": True,
                         "handoff": _handoff_json(h)})


class TelegramBotSalesStatsView(APIView):
    """GET /api/main/telegram-bot/sales-stats/?from=&to= — отчёт бота-продавца (Ф1: диалоги, заказы, передачи продавцу)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.telegram_bot.models import TelegramHandoff
        from apps.main.models import ShowcaseOrder
        company = _get_company(request)
        df, dt = _period(request)
        inq = TelegramInquiry.objects.filter(company=company, created_at__date__gte=df, created_at__date__lte=dt)
        a = inq.aggregate(messages=Count("id"), people=Count("chat_id", distinct=True),
                          ordered_people=Count("chat_id", distinct=True, filter=Q(order__isnull=False)))
        orders = ShowcaseOrder.objects.filter(company=company, source__in=BOT_ORDER_SOURCES,
                                              created_at__date__gte=df, created_at__date__lte=dt)
        o = orders.aggregate(
            n=Count("id"), sum_total=Coalesce(Sum("total"), Value(ZERO_MONEY, output_field=MONEY_FIELD)),
            done=Count("id", filter=Q(status="done")),
            done_total=Coalesce(Sum("total", filter=Q(status="done")), Value(ZERO_MONEY, output_field=MONEY_FIELD)),
            canceled=Count("id", filter=Q(status="canceled")),
        )
        hs = list(TelegramHandoff.objects.filter(company=company, created_at__date__gte=df, created_at__date__lte=dt)
                  .only("reason", "status", "created_at", "first_answer_at", "taken_at", "question", "chat_id", "customer_name"))
        real = [h for h in hs if h.status != "logged"]
        by_reason = {}
        for h in real:
            by_reason[h.reason] = by_reason.get(h.reason, 0) + 1
        live_hours = [h for h in real if h.status != "callback"]
        answered = [h for h in live_hours if h.first_answer_at or h.taken_at]
        waits = sorted(((h.first_answer_at or h.taken_at) - h.created_at).total_seconds() for h in answered)
        unknown = [h for h in hs if h.reason == "unknown"]
        people = a["people"] or 0
        n_orders = o["n"] or 0
        return Response({
            "from": df, "to": dt,
            "dialogs": people, "messages": a["messages"] or 0, "people": people,
            "orders": {"count": n_orders, "total": str(o["sum_total"]), "done": o["done"], "done_total": str(o["done_total"]),
                       "canceled": o["canceled"],
                       "avg_check": str((o["sum_total"] / n_orders).quantize(Decimal("0.01"))) if n_orders else "0.00"},
            "conversion_percent": round(100.0 * (a["ordered_people"] or 0) / people, 1) if people else 0.0,
            "funnel": {"dialogs": people, "handoffs": len(real), "orders": n_orders, "done": o["done"]},
            "handoffs": {
                "total": len(real), "by_reason": by_reason,
                "callbacks": sum(1 for h in real if h.status == "callback"),
                "answered": len(answered),
                "unanswered": sum(1 for h in live_hours if not (h.first_answer_at or h.taken_at) and h.status in ("expired", "returned")),
                "waiting_now": sum(1 for h in live_hours if h.status == "waiting"),
                "avg_answer_seconds": int(sum(waits) / len(waits)) if waits else None,
                "median_answer_seconds": int(waits[len(waits) // 2]) if waits else None,
            },
            "unknown_questions": _unknown_rows(company, unknown),  # botr2611: + name, client_id
            "bargain": bargain_stats(company, df, dt),  # bot2711
        })


# =========================================================================
# bot2711: бот-продавец Ф2 — торг (правила товаров, журнал, симулятор, отчёт)
# =========================================================================

BOT_CAPABILITIES.update({"bot_bargain": True})


def _require_manager(request):
    company = _get_company(request)
    if not _can_manage_bot_settings(request.user, company):
        raise PermissionDenied("Правила торга видит и меняет только владелец/админ.")
    return company


def _rule_json(p, rule, bcfg):
    from apps.main.telegram_bot.services import bargain
    lp = Decimal(str(p.price or 0))
    return {
        "product_id": str(p.id), "name": p.name, "price": str(lp),
        "purchase_price": str(p.purchase_price or 0),
        "category_id": str(p.category_id) if getattr(p, "category_id", None) else None,
        "allowed": rule.allowed if rule else None,
        "min_price": str(rule.min_price) if rule and rule.min_price is not None else None,
        "max_discount_percent": str(rule.max_discount_percent) if rule and rule.max_discount_percent is not None else None,
        "effective_allowed": _eff_allowed(p, bcfg, rule),
        "floor": str(bargain.floor_price(p, lp, bcfg, rule)) if lp > 0 else "0.00",
    }


def _eff_allowed(p, bcfg, rule):
    from apps.main.telegram_bot.services import bargain
    if rule is not None and rule.allowed is not None:
        return bool(rule.allowed)
    cats = [str(c) for c in (bcfg.get("allowed_categories") or [])]
    if cats and str(getattr(p, "category_id", "") or "") in cats:
        return True
    return not bcfg.get("only_marked", True)


class TelegramBargainProductsView(APIView):
    """GET /api/main/telegram-bot/bargain/products/?search=&only_rules=1&page=&page_size=
    Товары с правилами торга и итоговым полом цены (только владельцу/админу; покупателю и ИИ — никогда)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import Product
        from apps.main.telegram_bot.models import ProductBargain
        from apps.main.telegram_bot.services import bargain
        company = _require_manager(request)
        bs = TelegramBotSettings.objects.filter(company=company).first()
        _cfg, bcfg = bargain.cfg_bargain(bs) if bs else ({}, {})
        qs = Product.objects.filter(company=company).exclude(status=Product.Status.ARCHIVED) \
            .only("id", "name", "price", "purchase_price", "category_id").order_by("name")
        s = (request.query_params.get("search") or "").strip()
        if s:
            qs = qs.filter(name__icontains=s)
        if request.query_params.get("only_rules") in ("1", "true"):
            qs = qs.filter(bargain_rule__isnull=False)
        cat = request.query_params.get("category")
        if cat:
            qs = qs.filter(category_id=cat)
        pager = TelegramInquiryPagination()
        page = pager.paginate_queryset(qs, request, view=self)
        rules = {r.product_id: r for r in ProductBargain.objects.filter(product_id__in=[p.id for p in page])}
        return pager.get_paginated_response([_rule_json(p, rules.get(p.id), bcfg) for p in page])


class TelegramBargainProductDetailView(APIView):
    """PUT /api/main/telegram-bot/bargain/products/<uuid>/ {allowed: true|false|null, min_price, max_discount_percent}
    DELETE — сбросить правила товара (как в общих настройках)."""
    permission_classes = [permissions.IsAuthenticated]

    def _product(self, company, pk):
        from apps.main.models import Product
        p = Product.objects.filter(company=company, pk=pk).first()
        if p is None:
            raise NotFound("Товар не найден.")
        return p

    def put(self, request, pk):
        from apps.main.telegram_bot.models import ProductBargain
        from apps.main.telegram_bot.services import bargain
        company = _require_manager(request)
        p = self._product(company, pk)
        d = request.data
        errors = {}
        allowed = d.get("allowed", None)
        if allowed not in (True, False, None):
            errors["allowed"] = "true, false или null"

        def dec(name, lo, hi):
            v = d.get(name)
            if v in (None, ""):
                return None
            try:
                v = Decimal(str(v))
            except Exception:
                errors[name] = "число"
                return None
            if not lo <= v <= hi:
                errors[name] = f"{lo}…{hi}"
            return v
        min_price = dec("min_price", Decimal("0"), Decimal("100000000"))
        max_disc = dec("max_discount_percent", Decimal("0"), Decimal("50"))
        if min_price is not None and p.price and min_price > Decimal(str(p.price)):
            errors["min_price"] = "Не выше цены товара."
        if errors:
            raise ValidationError(errors)
        rule, _ = ProductBargain.objects.update_or_create(
            product=p, defaults={"company": company, "allowed": allowed, "min_price": min_price, "max_discount_percent": max_disc})
        bs = TelegramBotSettings.objects.filter(company=company).first()
        _cfg, bcfg = bargain.cfg_bargain(bs) if bs else ({}, {})
        TelegramBotAudit.objects.create(
            company=company, user=request.user,
            user_name=getattr(request.user, "first_name", "") or getattr(request.user, "email", "owner"),
            action=TelegramBotAudit.Action.SETTINGS_UPDATE, object_title=f"Торг: {p.name}"[:255], source="owner",
            changes={"allowed": allowed, "min_price": str(min_price) if min_price is not None else None,
                     "max_discount_percent": str(max_disc) if max_disc is not None else None},
        )
        return Response(_rule_json(p, rule, bcfg))

    def delete(self, request, pk):
        from apps.main.telegram_bot.models import ProductBargain
        company = _require_manager(request)
        p = self._product(company, pk)
        ProductBargain.objects.filter(product=p).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TelegramBargainOffersView(APIView):
    """GET /api/main/telegram-bot/bargain/offers/?from=&to=&chat_id=&decision= — журнал решений движка (владельцу)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.telegram_bot.models import TelegramBargainOffer
        company = _require_manager(request)
        df, dt = _period(request)
        qs = TelegramBargainOffer.objects.filter(company=company, created_at__date__gte=df, created_at__date__lte=dt) \
            .select_related("product", "variant", "used_order")
        if request.query_params.get("chat_id"):
            qs = qs.filter(chat_id=request.query_params["chat_id"])
        if request.query_params.get("decision"):
            qs = qs.filter(decision=request.query_params["decision"])
        pager = TelegramInquiryPagination()
        page = pager.paginate_queryset(qs, request, view=self)
        return pager.get_paginated_response([{
            "id": str(o.id), "at": o.created_at, "chat_id": o.chat_id, "is_test": o.chat_id.startswith("test:"),
            "product_id": str(o.product_id), "product_name": o.product.name if o.product_id else "",
            "variant_id": str(o.variant_id) if o.variant_id else None, "qty": str(o.qty),
            "list_price": str(o.list_price), "client_offer": str(o.client_offer) if o.client_offer is not None else None,
            "decision": o.decision, "price": str(o.price), "step": o.step, "floor": str(o.floor),
            "limit_percent": str(o.limit_percent), "expires_at": o.expires_at, "accepted_at": o.accepted_at,
            "order": {"id": str(o.used_order_id), "number": o.used_order.number} if o.used_order_id else None,
        } for o in page])


class TelegramBargainTestView(APIView):
    """POST /api/main/telegram-bot/bargain/test/ {product_id, variant_id?, offer?, reset?}
    Симулятор движка для владельца (отдельный тест-чат, в отчёт не идёт). Ответ — как увидит покупатель + пол."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from django.core.cache import cache
        from apps.main.models import Product, ProductVariant
        from apps.main.telegram_bot.services import bargain
        from apps.main.variant_utils import name_with_variant
        company = _require_manager(request)
        bs = TelegramBotSettings.objects.select_related("company").filter(company=company).first()
        if bs is None:
            raise NotFound("Бот не подключён.")
        p = Product.objects.filter(company=company, pk=request.data.get("product_id")).first() if request.data.get("product_id") else None
        if p is None:
            raise ValidationError({"product_id": "Товар не найден."})
        v = None
        if request.data.get("variant_id"):
            v = ProductVariant.objects.filter(company=company, product=p, pk=request.data["variant_id"]).first()
        chat = f"test:{request.user.id}"
        if request.data.get("reset"):
            cache.delete(bargain._state_key(company.id, chat, p.id, getattr(v, "id", None)))
            cache.delete(f"tgbg:rate:{company.id}:{chat}")
        res = bargain.propose_price(bs, chat, p, v, request.data.get("qty") or 1, bargain.parse_offer(request.data.get("offer")))
        text, markup = bargain.render(res, name_with_variant(p.name, v), request.data.get("lang") or "ru")
        _cfg, bcfg = bargain.cfg_bargain(bs)
        return Response({
            "decision": res["decision"], "price": str(res["price"]), "list_price": str(res["list_price"]),
            "expires_at": res["expires_at"], "text": text, "button": bool(markup),
            "floor": str(bargain.floor_price(p, res["list_price"], bcfg)) if res["list_price"] else "0.00",
        })


def bargain_stats(company, df, dt):
    """Блок «торг» для sales-stats."""
    from django.db.models import F, ExpressionWrapper
    from apps.main.telegram_bot.models import TelegramBargainOffer
    qs = TelegramBargainOffer.objects.filter(company=company, created_at__date__gte=df, created_at__date__lte=dt) \
        .exclude(chat_id__startswith="test:")
    a = qs.aggregate(proposals=Count("id"), dialogs=Count("chat_id", distinct=True),
                     declines=Count("id", filter=Q(decision="decline")),
                     accepted=Count("id", filter=Q(accepted_at__isnull=False)))
    used = qs.filter(used_order__isnull=False)
    concession = ExpressionWrapper((F("list_price") - F("price")) * F("qty"), output_field=MONEY_FIELD)
    u = used.aggregate(n=Count("id"), lost=Coalesce(Sum(concession), Value(ZERO_MONEY, output_field=MONEY_FIELD)),
                       listed=Coalesce(Sum(ExpressionWrapper(F("list_price") * F("qty"), output_field=MONEY_FIELD)),
                                       Value(ZERO_MONEY, output_field=MONEY_FIELD)))
    top = list(qs.values("product_id", "product__name").annotate(n=Count("id"), orders=Count("used_order", distinct=True))
               .order_by("-n")[:10])
    return {
        "dialogs": a["dialogs"], "proposals": a["proposals"], "accepted_by_customer": a["accepted"],
        "orders": u["n"], "concessions_total": str(u["lost"]),
        "avg_discount_percent": round(float(100 * u["lost"] / u["listed"]), 1) if u["listed"] else 0.0,
        "decline_share_percent": round(100.0 * a["declines"] / a["proposals"], 1) if a["proposals"] else 0.0,
        "top_products": [{"product_id": str(t["product_id"]), "name": t["product__name"], "proposals": t["n"],
                          "orders": t["orders"]} for t in top],
    }

