"""Б-62: уведомление исполнителю задачи в Telegram-бот компании (внешний вызов — только здесь, в тестах мокается)."""
import html
import logging

logger = logging.getLogger(__name__)

PRIORITY_LABEL = {"low": "низкий", "normal": "обычный", "high": "срочно"}


def employee_chat_id(company_id, user):
    """
    Чат сотрудника в боте компании. Отдельной привязки сотрудников к боту в проекте пока нет:
    берём telegram_chat_id пользователя, если такое поле появится; владельцу — чат владельца бота.
    """
    chat = getattr(user, "telegram_chat_id", None)
    if chat:
        return str(chat)
    from apps.main.telegram_bot.models import TelegramBotSettings

    st = TelegramBotSettings.objects.filter(company_id=company_id).first()
    if st and st.owner_chat_id and getattr(user, "owned_company", None) is not None:
        return str(st.owner_chat_id)
    return None


def send_telegram(company_id, user, text) -> bool:
    """Отправить сообщение сотруднику ботом компании. True — отправлено. Исключений наружу не бросает."""
    try:
        from apps.main.telegram_bot.models import TelegramBotSettings
        from apps.main.telegram_bot.services import telegram_api

        chat = employee_chat_id(company_id, user)
        st = TelegramBotSettings.objects.filter(company_id=company_id).first()
        if not chat or not st or not st.token:
            return False
        res = telegram_api.send_message(st.token, chat, text, parse_mode="HTML")
        return bool(isinstance(res, dict) and res.get("ok"))
    except Exception:
        logger.exception("task telegram notify failed")
        return False


def task_text(task, kind: str) -> str:
    head = "Новая задача" if kind == "assigned" else "Напоминание о задаче"
    due = task.due_date.astimezone().strftime("%d.%m.%Y %H:%M") if task.due_date else ""
    lines = [f"<b>{head}</b>", html.escape(task.title or "")]
    if task.description:
        lines.append(html.escape(task.description[:500]))
    if due:
        lines.append(f"Срок: {due}")
    lines.append(f"Приоритет: {PRIORITY_LABEL.get(task.priority, task.priority)}")
    return "\n".join(lines)


def emit_task_event(task, event: str):
    """Событие для будущих пушей/вебхуков (если тип зарегистрирован в apps.integrations.events.EVENTS)."""
    try:
        from apps.integrations.events import emit_event

        emit_event(task.company_id, event, {
            "task_id": str(task.id), "assigned_to": str(task.assigned_to_id) if task.assigned_to_id else None,
            "title": task.title, "priority": task.priority,
        })
    except Exception:
        logger.exception("task event failed")
