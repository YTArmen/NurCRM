"""Статьи движения денег по умолчанию (ТЗ «Бухгалтер», п. 3). Создаются лениво, без data-миграции."""
from apps.construction.models import CashFlow, CashFlowCategory

K = CashFlowCategory.Kind

# (code, название, тип)
DEFAULT_CATEGORIES = [
    ("revenue", "Выручка", K.INCOME),
    ("debt_payment", "Оплата долга покупателем", K.INCOME),
    ("deposit", "Внесение", K.INCOME),
    ("purchase", "Закупка товара", K.EXPENSE),
    ("supplier_payment", "Оплата поставщику", K.EXPENSE),
    ("salary", "Зарплата", K.EXPENSE),
    ("rent", "Аренда", K.EXPENSE),
    ("utilities", "Коммунальные", K.EXPENSE),
    ("taxes", "Налоги", K.EXPENSE),
    ("ads", "Реклама", K.EXPENSE),
    ("other", "Прочие", K.BOTH),
]

# Статья для операций, которые создаёт сервер.
SOURCE_KIND_CODE = {
    CashFlow.SourceKind.POS_SALE: "revenue",
    CashFlow.SourceKind.POS_PREPAYMENT: "revenue",
    CashFlow.SourceKind.DEBT_REPAYMENT: "debt_payment",
    CashFlow.SourceKind.SHIFT_DRAWER_INFLOW: "deposit",
    CashFlow.SourceKind.WAREHOUSE_PURCHASE: "purchase",
    CashFlow.SourceKind.PROCUREMENT_RECEIPT: "purchase",
    CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT: "supplier_payment",
}


def ensure_default_categories(company):
    """Создаёт набор по умолчанию, если у компании ещё нет ни одной статьи с кодом."""
    if company is None:
        return
    if CashFlowCategory.objects.filter(company=company).exclude(code="").exists():
        return
    for code, title, kind in DEFAULT_CATEGORIES:
        obj = CashFlowCategory.objects.filter(company=company, branch__isnull=True, title__iexact=title).first()
        if obj is None:
            CashFlowCategory.objects.create(company=company, title=title, type=kind, code=code)
        elif not obj.code:
            obj.code = code
            obj.type = kind
            obj.save(update_fields=["code", "type"])


def get_default_category(company, code):
    """Статья по умолчанию по коду (None, если нет компании/статьи)."""
    if company is None:
        return None
    ensure_default_categories(company)
    return CashFlowCategory.objects.filter(company=company, code=code, branch__isnull=True).first()


def category_for_source(company, source_kind):
    code = SOURCE_KIND_CODE.get(source_kind)
    if not code:
        return None
    try:
        return get_default_category(company, code)
    except Exception:  # статья не должна ломать продажу
        return None
