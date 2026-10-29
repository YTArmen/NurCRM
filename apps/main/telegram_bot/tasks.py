import logging
import time
from decimal import Decimal
from celery import shared_task

from apps.main.telegram_bot.services import (
    telegram_api,
    ai_service,
    owner_handler,
    customer_handler,
    events_handler,
)

logger = logging.getLogger("telegram_bot.tasks")


QUEUE_ALERT_SECONDS = 30
QUEUE_METRICS_KEY = "tg_queue_metrics"
FALLBACK_REPLY = "Извините, сейчас не получилось ответить. Попробуйте, пожалуйста, ещё раз через минуту."


def _record_queue_latency(latency: float) -> None:
    """Возраст задачи в очереди (ТЗ-07 1.1.3): метрики + тревога, если ждала > 30 с."""
    from django.core.cache import cache

    try:
        now = time.time()
        metrics = cache.get(QUEUE_METRICS_KEY) or {}
        metrics["last_latency"] = round(latency, 3)
        metrics["last_at"] = now
        if now - metrics.get("window_start", 0) >= 300:
            metrics["window_start"] = now
            metrics["max_latency_5m"] = round(latency, 3)
        else:
            metrics["max_latency_5m"] = max(round(latency, 3), metrics.get("max_latency_5m", 0))
        cache.set(QUEUE_METRICS_KEY, metrics, timeout=3600)
    except Exception:
        pass
    if latency > QUEUE_ALERT_SECONDS:
        _queue_alert(f"⚠ Очередь ботов: задача ждала {latency:.0f} с (порог {QUEUE_ALERT_SECONDS} с). Проверьте воркеры celery.")


def _queue_alert(text: str) -> None:
    """Тревога команде через технический бот — не чаще раза в 10 минут."""
    from django.core.cache import cache

    try:
        if not cache.add("tg_queue_alert_lock", 1, timeout=600):
            return
    except Exception:
        pass
    logger.error(text)
    try:
        from apps.support.bot import send_team_alert
        send_team_alert(text)
    except Exception as exc:
        logger.warning("queue alert not delivered: %s", exc)


def _execute_scenario(settings, chat_id: str, scenario, from_user: dict, text: str, is_voice: bool = False):
    """Выполняет ответ по пользовательскому сценарию (ТЗ-11 п. 1.4)."""
    from apps.main.telegram_bot.models import TelegramInquiry
    from apps.main.telegram_bot.services.photo_service import send_single_product_photo
    from django.db.models import F

    token = settings.token
    scenario.hits = F("hits") + 1
    scenario.save(update_fields=["hits"])

    reply_markup = None
    if scenario.buttons:
        inline_keyboard = []
        row = []
        for b in scenario.buttons:
            btn_dict = {"text": b.get("text", "")}
            if b.get("url"):
                btn_dict["url"] = b["url"]
            elif b.get("command"):
                cmd = b["command"]
                if not cmd.startswith("/"):
                    cmd = "/" + cmd
                btn_dict["callback_data"] = f"sc_cmd:{cmd[:50]}"
            row.append(btn_dict)
            if len(row) == 2:
                inline_keyboard.append(row)
                row = []
        if row:
            inline_keyboard.append(row)
        if inline_keyboard:
            reply_markup = {"inline_keyboard": inline_keyboard}

    if scenario.photo_product_id:
        from apps.main.models import Product
        prod = Product.objects.filter(id=scenario.photo_product_id).first()
        if prod:
            send_single_product_photo(settings, chat_id, prod)

    telegram_api.send_message(token, chat_id, scenario.reply_text, parse_mode="HTML", reply_markup=reply_markup)

    cust_name = f"{(from_user.get('first_name') or '').strip()} {(from_user.get('last_name') or '').strip()}".strip()
    cust_username = from_user.get("username") or ""
    TelegramInquiry.objects.create(
        company=settings.company,
        chat_id=str(chat_id),
        name=cust_name,
        username=cust_username,
        text=text,
        reply=scenario.reply_text,
        is_voice=is_voice,
        scenario=scenario,
        scenario_title=scenario.title,
    )


@shared_task(name="apps.main.telegram_bot.tasks.process_telegram_update")
def process_telegram_update(settings_id: str, update_data: dict, enqueued_at: float = None):
    """Фоновая обработка вебхука от Telegram."""
    from apps.main.telegram_bot.models import (
        TelegramBotSettings,
        TelegramProcessedUpdate,
        TelegramMessageLog,
    )

    if enqueued_at:
        _record_queue_latency(time.time() - float(enqueued_at))

    settings = None
    chat_id = None
    try:
        settings = (
            TelegramBotSettings.objects.select_related("company")
            .filter(id=settings_id)
            .first()
        )
        if not settings:
            logger.warning("TelegramBotSettings %s not found", settings_id)
            return

        if settings.mode != TelegramBotSettings.Mode.SERVER:
            logger.info("Bot %s is in local mode, skipping server processing", settings_id)
            return

        update_id = update_data.get("update_id")
        if not update_id:
            return

        # Идемпотентность на уровне БД
        _obj, created = TelegramProcessedUpdate.objects.get_or_create(
            bot_settings=settings,
            update_id=update_id,
        )
        if not created:
            logger.info("Duplicate update_id %s for bot %s, skipping", update_id, settings_id)
            return

        # Обработка нажатий inline-кнопок (callback_query, ТЗ-09, ТЗ-10)
        cq = update_data.get("callback_query")
        if cq:
            from apps.main.telegram_bot.services import callback_handler
            callback_handler.handle_callback_query(settings, cq)
            from django.utils import timezone
            TelegramBotSettings.objects.filter(id=settings.id).update(last_reply_at=timezone.now())
            return

        message = update_data.get("message")
        if not message:
            return

        # Проверка возраста сообщения: старше 15 минут не обрабатывать (TZ 5.1)
        msg_date = message.get("date")
        if msg_date:
            age = int(time.time()) - int(msg_date)
            if age > 900:  # 15 минут
                logger.info("Dropping stale message (age %ds > 900s) for bot %s", age, settings_id)
                return

        chat = message.get("chat", {})
        chat_id = str(chat.get("id"))
        from_user = message.get("from", {})
        chat_title = chat.get("title") or ""
        sender_name = f"{(from_user.get('first_name') or '').strip()} {(from_user.get('last_name') or '').strip()}".strip() or (from_user.get("username") or "")

        text = message.get("text") or message.get("caption") or ""
        # ТЗ ч.15, п. 2.1: voice, audio и video_note распознаются одинаково
        audio_part, audio_mime = None, "audio/ogg"
        if message.get("voice"):
            audio_part, audio_mime = message["voice"], message["voice"].get("mime_type") or "audio/ogg"
        elif message.get("audio"):
            audio_part, audio_mime = message["audio"], message["audio"].get("mime_type") or "audio/mpeg"
        elif message.get("video_note"):
            audio_part, audio_mime = message["video_note"], "video/mp4"
        is_voice = audio_part is not None

        # Проверяем: владелец или покупатель (TZ 5.1)
        is_owner = (settings.owner_chat_id and str(settings.owner_chat_id) == str(chat_id))
        audience = "owner" if is_owner else "customers"

        # ТЗ Н-23 (2): 6 цифр от не-владельца — возможно, код подтверждения владельца
        if not is_owner and text and not message.get("voice"):
            from apps.main.telegram_bot.services import owner_notify
            if owner_notify.try_bind_owner_from_message(settings, chat_id, from_user, chat_title, text):
                from django.utils import timezone
                TelegramBotSettings.objects.filter(id=settings.id).update(last_reply_at=timezone.now())
                return

        # Логируем для detect-owner-chat
        TelegramMessageLog.objects.create(
            bot_settings=settings,
            chat_id=chat_id,
            chat_title=chat_title,
            sender_name=sender_name,
            text=text if not is_voice else "[Голосовое сообщение]",
        )

        # Ящик «Чаты» (ТЗ 26.10 Б-76): входящее покупателя пишем в диалог; в паузе после ответа человека бот молчит
        if not is_owner:
            from apps.inbox import bot_hooks
            if bot_hooks.on_customer_message(settings, message):
                return

        # ТЗ ч.15, п. 5: фото накладной от владельца (photo или document-картинка)
        image_part, image_mime = None, "image/jpeg"
        if message.get("photo"):
            image_part = sorted(message["photo"], key=lambda ph: ph.get("file_size") or 0)[-1]
        elif (message.get("document") or {}).get("mime_type", "").startswith("image/"):
            image_part, image_mime = message["document"], message["document"]["mime_type"]
        if image_part is not None and is_owner:
            token = settings.token
            file_info = telegram_api.get_file(token, image_part.get("file_id")) if token else {}
            image_bytes = telegram_api.download_file(token, file_info["file_path"]) if file_info.get("file_path") else b""
            if image_bytes:
                owner_handler.handle_owner_photo(settings, chat_id, image_bytes, image_mime, caption=text)
            else:
                telegram_api.send_message(token, chat_id, "Не удалось скачать фото из Telegram, пришлите ещё раз.")
            from django.utils import timezone
            TelegramBotSettings.objects.filter(id=settings.id).update(last_reply_at=timezone.now())
            return

        # Если входящее голосовое сообщение
        if is_voice:
            file_id = audio_part.get("file_id")
            token = settings.token
            transcribed = ""
            if token and file_id:
                file_info = telegram_api.get_file(token, file_id)
                file_path = file_info.get("file_path")
                if file_path:
                    audio_bytes = telegram_api.download_file(token, file_path)
                    ai_key = ai_service.get_effective_ai_key(settings.ai_key)
                    if audio_bytes and ai_key:
                        transcribed = ai_service.transcribe_voice(ai_key, audio_bytes, mime_type=audio_mime)
            if transcribed:
                text = transcribed
            elif not text:
                if token:
                    telegram_api.send_message(token, chat_id, "Не расслышал, повторите или напишите текстом.", parse_mode=None)
                return

        if not text:
            return

        # 1. Проверяем кастомный сценарий-команду (ТЗ-11 п. 1.4)
        if text.strip().startswith("/"):
            from apps.main.telegram_bot.views import match_scenario
            from apps.main.telegram_bot.models import TelegramBotScenario
            sc = match_scenario(settings.company, text, audience=audience)
            if sc and sc.kind == TelegramBotScenario.Kind.COMMAND:
                _execute_scenario(settings, chat_id, sc, from_user, text, is_voice)
                from django.utils import timezone
                TelegramBotSettings.objects.filter(id=settings.id).update(last_reply_at=timezone.now())
                return

        if is_owner:
            owner_handler.handle_owner_message(
                settings=settings,
                chat_id=chat_id,
                text=text,
                is_voice=is_voice,
            )
        else:
            customer_handler.handle_customer_message(
                settings=settings,
                chat_id=chat_id,
                from_user=from_user,
                text=text,
                is_voice=is_voice,
            )

        from django.utils import timezone
        TelegramBotSettings.objects.filter(id=settings.id).update(last_reply_at=timezone.now())

    except Exception as exc:
        logger.exception("Error processing telegram update %s: %s", update_data.get("update_id"), exc)
        # Не молчим (ТЗ-07 1.1.6): короткий ответ вместо тишины.
        if settings is not None and chat_id and chat_id != "None":
            try:
                res = telegram_api.send_message(settings.token, chat_id, FALLBACK_REPLY, parse_mode=None)
                if res.get("ok"):
                    from django.utils import timezone
                    TelegramBotSettings.objects.filter(id=settings.id).update(last_reply_at=timezone.now())
            except Exception:
                logger.exception("Fallback reply failed for update %s", update_data.get("update_id"))


@shared_task(name="apps.main.telegram_bot.tasks.send_telegram_shift_summary")
def send_telegram_shift_summary(company_id: str, shift_id: str):
    """Отправка сводки смены владельцу."""
    try:
        events_handler.send_shift_closed_notification(company_id, shift_id)
    except Exception as exc:
        logger.error("send_telegram_shift_summary task failed for shift %s: %s", shift_id, exc)


@shared_task(name="apps.main.telegram_bot.tasks.send_telegram_debt_reminders")
def send_telegram_debt_reminders(company_id: str):
    """Рассылка напоминаний о долге подписанным покупателям."""
    from apps.main.telegram_bot.models import TelegramBotSettings
    from apps.main.models import Client, Sale
    from django.db.models import Sum
    from apps.main.telegram_bot.services.photo_service import format_amount

    try:
        settings = TelegramBotSettings.objects.filter(company_id=company_id).first()
        if not settings or not settings.token:
            return

        clients = Client.objects.filter(
            company_id=company_id,
            telegram_chat_id__isnull=False,
        ).exclude(telegram_chat_id="")

        sent_count = 0
        total_amount = Decimal("0.00")
        company_name = getattr(settings.company, "name", "наш магазин")

        for client in clients:
            chat_id = client.telegram_chat_id
            debt = (
                Sale.objects.filter(
                    company_id=company_id,
                    client=client,
                ).aggregate(s=Sum("debt_remaining"))["s"]
                or Decimal("0.00")
            )

            if debt > Decimal("0.00"):
                msg = (
                    f"Здравствуйте, {client.full_name}!\n"
                    f"Напоминаем, что в магазине «{company_name}» у вас есть задолженность: "
                    f"<b>{format_amount(debt)} сом</b>.\n"
                    f"Будем рады вашему визиту!"
                )
                res = telegram_api.send_message(settings.token, chat_id, msg, parse_mode="HTML")
                if res.get("ok"):
                    sent_count += 1
                    total_amount += debt

        if settings.owner_chat_id:
            summary = (
                f"✅ <b>Рассылка напоминаний о долгах завершена!</b>\n"
                f"Отправлено сообщений: <b>{sent_count}</b>\n"
                f"На общую сумму: <b>{format_amount(total_amount)} сом</b>."
            )
            telegram_api.send_message(settings.token, settings.owner_chat_id, summary, parse_mode="HTML")

    except Exception as exc:
        logger.exception("send_telegram_debt_reminders failed for company %s: %s", company_id, exc)


@shared_task(name="apps.main.telegram_bot.tasks.send_telegram_notification")
def send_telegram_notification(token: str, chat_id: str, text: str, parse_mode: str = "HTML"):
    """Асинхронная отправка произвольного сообщения."""
    try:
        telegram_api.send_message(token, chat_id, text, parse_mode=parse_mode)
    except Exception as exc:
        logger.error("send_telegram_notification failed: %s", exc)


WEBHOOK_CHECK_BATCH_SIZE = 50
WEBHOOK_CHECK_SPREAD_SECONDS = 50 * 60
WEBHOOK_TOKEN_INVALID_ERROR = "Неверный токен Telegram (401). Проверьте токен в @BotFather."


@shared_task(name="apps.main.telegram_bot.tasks.check_server_bots_webhooks")
def check_server_bots_webhooks():
    """
    Раз в час: ставит проверку getWebhookInfo всех ботов в режиме server
    пачками по 50, равномерно распределёнными по 50 минутам (ТЗ-07 1.1.7).
    Боты с неверным токеном (401) не опрашиваются, пока владелец не сменит токен.
    """
    from apps.main.telegram_bot.models import TelegramBotSettings

    ids = list(
        TelegramBotSettings.objects.filter(mode=TelegramBotSettings.Mode.SERVER)
        .exclude(encrypted_token="")
        .exclude(webhook_error=WEBHOOK_TOKEN_INVALID_ERROR)
        .order_by("id")
        .values_list("id", flat=True)
    )
    batches = [ids[i:i + WEBHOOK_CHECK_BATCH_SIZE] for i in range(0, len(ids), WEBHOOK_CHECK_BATCH_SIZE)]
    step = WEBHOOK_CHECK_SPREAD_SECONDS / max(len(batches), 1)
    for n, batch in enumerate(batches):
        check_bot_webhooks_batch.apply_async(args=[[str(x) for x in batch]], countdown=int(n * step))
    return {"bots": len(ids), "batches": len(batches)}


@shared_task(name="apps.main.telegram_bot.tasks.check_bot_webhooks_batch")
def check_bot_webhooks_batch(bot_ids: list):
    """Проверка вебхуков одной пачки ботов. Снятый/ошибочный/забитый вебхук ставится заново."""
    import uuid
    from apps.main.telegram_bot.models import TelegramBotSettings

    checked = reinstalled = 0
    for bot in TelegramBotSettings.objects.filter(id__in=bot_ids, mode=TelegramBotSettings.Mode.SERVER):
        token = bot.token
        if not token:
            continue
        checked += 1
        try:
            info = telegram_api.get_webhook_info(token)
            url = info.get("url") or ""
            pending = info.get("pending_update_count") or 0
            last_error_message = info.get("last_error_message") or ""
            expected_url = telegram_api.build_webhook_url(bot.bot_uuid)

            if not url or url != expected_url or pending > 50 or last_error_message:
                reason = last_error_message or (f"в очереди Telegram {pending} обновлений" if pending > 50 else "вебхук был снят")
                bot.secret_token = bot.secret_token or uuid.uuid4().hex
                telegram_api.set_webhook(token, expected_url, bot.secret_token)
                bot.webhook_ok = True
                bot.webhook_error = f"Вебхук переустановлен автоматически: {reason}"[:500]
                bot.save(update_fields=["webhook_ok", "webhook_error", "secret_token"])
                reinstalled += 1
            elif not bot.webhook_ok or bot.webhook_error:
                bot.webhook_ok = True
                bot.webhook_error = None
                bot.save(update_fields=["webhook_ok", "webhook_error"])
        except telegram_api.TelegramAPIError as exc:
            bot.webhook_ok = False
            if exc.status_code in (401, 404) or "Unauthorized" in str(exc):
                bot.webhook_error = WEBHOOK_TOKEN_INVALID_ERROR
            else:
                bot.webhook_error = f"Ошибка проверки вебхука: {exc}"[:500]
            bot.save(update_fields=["webhook_ok", "webhook_error"])
        except Exception as exc:
            logger.error("check_bot_webhooks_batch error for bot %s: %s", bot.id, exc)
    return {"checked": checked, "reinstalled": reinstalled}


@shared_task(name="apps.main.telegram_bot.tasks.monitor_bot_queue")
def monitor_bot_queue():
    """
    Каждую минуту (ТЗ-07 1.1.3): длина очереди celery и проба —
    задача с отметкой времени; её задержка = возраст самой старой задачи.
    """
    from django.conf import settings as dj_settings
    from django.core.cache import cache

    length = None
    try:
        import redis
        conn = redis.Redis.from_url(dj_settings.CELERY_BROKER_URL, socket_timeout=3)
        length = conn.llen(getattr(dj_settings, "CELERY_TASK_DEFAULT_QUEUE", "celery"))
    except Exception as exc:
        logger.warning("monitor_bot_queue: cannot read queue length: %s", exc)

    try:
        metrics = cache.get(QUEUE_METRICS_KEY) or {}
        metrics["queue_length"] = length
        metrics["queue_checked_at"] = time.time()
        # Проба не вернулась за 2 минуты — воркер стоит или очередь забита.
        probe_sent = metrics.get("probe_sent_at")
        probe_done = metrics.get("probe_done_at") or 0
        if probe_sent and probe_done < probe_sent and time.time() - probe_sent > 120:
            _queue_alert(f"⚠ Очередь ботов: проба не выполнена {time.time() - probe_sent:.0f} с, в очереди {length}. Воркер celery не работает?")
        else:
            metrics["probe_sent_at"] = time.time()
            queue_probe.delay(time.time())
        cache.set(QUEUE_METRICS_KEY, metrics, timeout=3600)
    except Exception as exc:
        logger.warning("monitor_bot_queue failed: %s", exc)
    return {"queue_length": length}


@shared_task(name="apps.main.telegram_bot.tasks.queue_probe")
def queue_probe(enqueued_at: float):
    from django.core.cache import cache

    latency = time.time() - float(enqueued_at)
    try:
        metrics = cache.get(QUEUE_METRICS_KEY) or {}
        metrics["probe_latency"] = round(latency, 3)
        metrics["probe_done_at"] = time.time()
        cache.set(QUEUE_METRICS_KEY, metrics, timeout=3600)
    except Exception:
        pass
    if latency > QUEUE_ALERT_SECONDS:
        _queue_alert(f"⚠ Очередь ботов: задача ждала {latency:.0f} с (порог {QUEUE_ALERT_SECONDS} с).")
    return round(latency, 3)


@shared_task(name="apps.main.telegram_bot.tasks.send_order_notification")
def send_order_notification(order_id: str):
    """ТЗ Б-22: уведомление о новом заказе витрины."""
    from apps.main.telegram_bot.services import order_notify

    order_notify.notify_new_order(order_id)


@shared_task(name="apps.main.telegram_bot.tasks.retry_order_notification")
def retry_order_notification(order_id: str, attempt: int):
    """Повтор после ошибки отправки: через 1, 5, 30 минут."""
    from apps.main.telegram_bot.services import order_notify

    order_notify.notify_new_order(order_id, attempt=int(attempt))
