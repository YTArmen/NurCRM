"""Разделы, наборы прав и справочники по умолчанию (ТЗ §2.2, §3.1, §3.6)."""

SECTIONS = [
    "analytics", "branches", "chess", "tenants", "leases",
    "notifications", "cash", "expenses", "staff", "settings",
]

SECTION_TITLES = {
    "analytics": "Аналитика",
    "branches": "Филиалы",
    "chess": "Шахматка",
    "tenants": "Арендаторы",
    "leases": "Договоры",
    "notifications": "Уведомления",
    "cash": "Касса",
    "expenses": "Расходы и налоги",
    "staff": "Сотрудники",
    "settings": "Настройки",
}

PRESETS = {
    "manager": {"title": "Управляющий", "sections": [s for s in SECTIONS if s != "settings"]},
    "accountant": {"title": "Бухгалтер", "sections": ["analytics", "leases", "cash", "expenses", "staff"]},
    "admin": {"title": "Администратор", "sections": ["chess", "tenants", "leases", "notifications"]},
    "cashier": {"title": "Кассир", "sections": ["tenants", "cash"]},
    "chess_only": {"title": "Только шахматка", "sections": ["chess"]},
    "none": {"title": "Без доступа", "sections": []},
}

DEFAULT_UNIT_TYPES = ["Бутик", "Островок", "Офис", "Склад", "Фуд-корт", "Рекламное место"]
DEFAULT_EXPENSE_CATEGORIES = ["Налоги", "Коммунальные", "Зарплата", "Непредвиденные", "Обслуживание"]

# Виды правил автонапоминаний (ТЗ §3.9)
REMINDER_RULE_KINDS = [
    "before_3_days",        # за 3 дня до срока
    "due_day",              # в день оплаты
    "overdue_every_3_days", # каждые 3 дня просрочки + копия владельцу в Telegram
    "penalty_from_day_1",   # начисление пени с 1-го дня просрочки
    "lease_end_45_days",    # за 45 дней до конца договора владельцу
]

DOC_KEYS = ["lease", "employment", "receipt", "act"]

# Статусы
SOON_DAYS = 7
LEASE_EXPIRING_DAYS = 45
