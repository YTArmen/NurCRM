"""
Разделы веба, которые 18.11.2026 убраны из меню в Маркетплейс (mm1811).
«Стандарт» и остальные тарифы ставят их сами бесплатно («Установить»), «Старт» — 500 сом в месяц
(заявка в поддержку, включает оператор). Установленный раздел — CompanyAddon(code=..., source="install").
Кто уже пользовался разделом, тому он включён сразу (source="auto", команда menu_modules_autoinstall).
"""
from __future__ import annotations

MODULES: dict[str, str] = {
    "m-supplier-returns": "Возвраты поставщику",
    "m-documents": "Инвентаризация и перемещения",
    "m-roles": "Свои роли",
    "m-salary": "Зарплата сотрудников",
    "m-debts": "Книга долгов",
    "m-rentals": "Прокат",
    "m-variants": "Варианты товара",
    "m-customer-display": "Экран покупателя",
}
START_PRICE_MONTH = 500


def is_start(company) -> bool:
    from apps.users.services_subscription import resolve_plan_code

    return resolve_plan_code(getattr(company, "subscription_plan", None)) == "start"


def used_modules(company) -> set[str]:
    """Какими разделами компания уже пользовалась (есть хоть одна запись)."""
    from django.apps import apps

    def has(label, **extra):
        try:
            return apps.get_model(label).objects.filter(company=company, **extra).exists()
        except Exception:
            return False

    out = set()
    if has("main.SupplierReturn"):
        out.add("m-supplier-returns")
    if has("main.ProductInventorySession") or has("main.BranchTransfer"):
        out.add("m-documents")
    if has("users.User", custom_role__isnull=False):
        out.add("m-roles")
    if has("main.MarketSaleEmployeePayProfile") or has("warehouse.AgentSalaryAccrual"):
        out.add("m-salary")
    if has("main.Debt"):
        out.add("m-debts")
    if has("main.Rental"):
        out.add("m-rentals")
    if has("main.ProductVariant"):
        out.add("m-variants")
    if has("main.MarketCustomerDisplaySettings"):
        out.add("m-customer-display")
    return out


def autoinstall(company, *, dry_run=False) -> list[str]:
    """Включить компании разделы, которыми она пользовалась. Уже существующие записи не трогаем."""
    from apps.users.models import CompanyAddon

    have = set(CompanyAddon.objects.filter(company=company, code__in=MODULES).values_list("code", flat=True))
    new = sorted(used_modules(company) - have)
    if not dry_run:
        for code in new:
            CompanyAddon.objects.create(company=company, code=code, active=True, source="auto", note="mm1811: раздел уже использовался")
    return new
