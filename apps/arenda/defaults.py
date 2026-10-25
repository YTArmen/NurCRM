"""Ленивое наполнение справочников компании (не в миграции)."""
from .constants import DEFAULT_EXPENSE_CATEGORIES, DEFAULT_UNIT_TYPES, REMINDER_RULE_KINDS


def ensure_company_defaults(company):
    from .models import ArendaSettings, ExpenseCategory, ReminderRule, UnitType

    settings_obj, created = ArendaSettings.objects.get_or_create(
        company=company,
        defaults={
            "org_name": company.llc or company.name or "",
            "inn": (company.inn or "") if (company.inn or "").isdigit() and len(company.inn) == 14 else "",
            "okpo": company.okpo or "",
            "legal_address": company.address or "",
            "phone": company.phone or "",
        },
    )
    if created:
        for name in DEFAULT_UNIT_TYPES:
            UnitType.objects.get_or_create(company=company, name=name, defaults={"is_system": True})
        for name in DEFAULT_EXPENSE_CATEGORIES:
            ExpenseCategory.objects.get_or_create(company=company, name=name, defaults={"is_system": True})
        for kind in REMINDER_RULE_KINDS:
            ReminderRule.objects.get_or_create(company=company, kind=kind)
    return settings_obj
