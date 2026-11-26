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
    from apps.construction.batch_cache import memo, ref
    return memo(("category", ref(company), source_kind), lambda: _category_for_source(company, source_kind))


def _category_for_source(company, source_kind):
    code = SOURCE_KIND_CODE.get(source_kind)
    if not code:
        return None
    try:
        return get_default_category(company, code)
    except Exception:  # статья не должна ломать продажу
        return None


# ── ТЗ Н-19: системные статьи со slug ─────────────────────────────────────
SYSTEM_SLUGS = ("salaries", "rent", "taxes", "utilities", "purchases")

# (slug, название при создании, тип)
SYSTEM_CATEGORIES = [
    ("salaries", "Зарплата", K.EXPENSE),
    ("rent", "Аренда", K.EXPENSE),
    ("taxes", "Налоги", K.EXPENSE),
    ("utilities", "Коммунальные", K.EXPENSE),
    ("purchases", "Закупка товара", K.EXPENSE),
]

# название (нижний регистр) -> slug
TITLE_TO_SLUG = {
    "зарплата": "salaries", "зарплаты": "salaries",
    "аренда": "rent",
    "налог": "taxes", "налоги": "taxes", "налог/налоги": "taxes",
    "коммунальные": "utilities", "коммунальные услуги": "utilities",
    "закупка товара": "purchases", "закупки": "purchases", "закупка": "purchases",
}
CODE_TO_SLUG = {"salary": "salaries", "rent": "rent", "taxes": "taxes", "utilities": "utilities", "purchase": "purchases"}


def system_slug_for(title, code=""):
    """Системный slug по названию (без регистра) или коду статьи; "" если не системная."""
    return CODE_TO_SLUG.get(code or "") or TITLE_TO_SLUG.get((title or "").strip().lower(), "")


def ensure_system_categories(company):
    """Идемпотентно: ставит slug/is_system существующим статьям по названию и
    создаёт недостающие системные (на всю компанию). ТЗ Н-19."""
    if company is None:
        return
    have = set()
    for c in CashFlowCategory.objects.filter(company=company, slug=""):
        slug = system_slug_for(c.title, c.code)
        if slug:
            CashFlowCategory.objects.filter(pk=c.pk).update(slug=slug, is_system=True)
    have = set(CashFlowCategory.objects.filter(company=company).exclude(slug="").values_list("slug", flat=True))
    for slug, title, kind in SYSTEM_CATEGORIES:
        if slug in have:
            continue
        if CashFlowCategory.objects.filter(company=company, branch__isnull=True, title__iexact=title).exists():
            continue
        CashFlowCategory.objects.create(company=company, title=title, type=kind, slug=slug, is_system=True)
