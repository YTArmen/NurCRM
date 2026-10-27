import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from django.conf import settings

from . import models
from . import op_permissions as perms
from . import stock as stock_service
from .models import q_qty
from .op_permissions import BusinessRuleError, OperationForbidden
from .utils import effective_payment_kind


MULTI_WAREHOUSE_DOC_TYPES = frozenset({
    models.Document.DocType.SALE,
    models.Document.DocType.SALE_RETURN,
    models.Document.DocType.COMMERCIAL_OFFER,
})

# Типы документов агента ("Мои остатки"), где строки могут ссылаться на разные
# склады: списание/зачисление идёт со склада каждой позиции (product.warehouse),
# а единый warehouse_from на уровне документа необязателен.
AGENT_MULTI_WAREHOUSE_DOC_TYPES = frozenset({
    models.Document.DocType.SALE,
    models.Document.DocType.SALE_RETURN,
    models.Document.DocType.PURCHASE_RETURN,
    models.Document.DocType.WRITE_OFF,
})


# Окно, внутри которого повторное проведение документа с тем же составом
# считается дублем: двойной клик по «Провести», повтор запроса после таймаута
# или потери связи. Такие дубли удваивают остаток на складе.
DUPLICATE_POST_WINDOW_SECONDS = 180


def _document_items_signature(document) -> tuple:
    """Состав документа: набор пар (товар, количество), независимо от порядка строк."""
    return tuple(sorted(
        (str(item.product_id), str(q_qty(item.qty)))
        for item in document.items.all()
    ))


def find_recent_duplicate_document(document, window_seconds: int = DUPLICATE_POST_WINDOW_SECONDS):
    """
    Недавно проведённый документ-близнец: тот же тип, склады, контрагент, агент,
    сумма и точно такой же состав позиций. Возвращает найденный документ или None.
    """
    window_start = timezone.now() - timedelta(seconds=window_seconds)
    candidates = (
        models.Document.objects
        .filter(
            doc_type=document.doc_type,
            status__in=(models.Document.Status.POSTED, models.Document.Status.CASH_PENDING),
            warehouse_from_id=document.warehouse_from_id,
            warehouse_to_id=document.warehouse_to_id,
            counterparty_id=document.counterparty_id,
            agent_id=document.agent_id,
            total=document.total,
            created_at__gte=window_start,
        )
        .exclude(pk=document.pk)
        .order_by("-created_at")
        .prefetch_related("items")[:5]
    )
    signature = _document_items_signature(document)
    for candidate in candidates:
        if _document_items_signature(candidate) == signature:
            return candidate
    return None


def document_allows_multi_warehouse(document) -> bool:
    """Строки документа могут списываться/зачисляться с разных складов.

    Владелец: продажа/возврат/КП. Агент ("Мои остатки"): продажа, возвраты и
    списание — склад берётся из каждой позиции (product.warehouse), а не из
    единого warehouse_from документа.
    """
    if getattr(document, "agent_id", None):
        return document.doc_type in AGENT_MULTI_WAREHOUSE_DOC_TYPES
    return document.doc_type in MULTI_WAREHOUSE_DOC_TYPES


def resolve_item_warehouse(document, item):
    """Склад для проверки остатков и движения по строке документа."""
    if document_allows_multi_warehouse(document):
        product = getattr(item, "product", None)
        wh = getattr(product, "warehouse", None) if product is not None else None
        if wh is not None:
            return wh
    return document.warehouse_from


def resolve_document_context_warehouse(document):
    """Склад для кассы/предоплаты: warehouse_from или первый склад из строк."""
    if document.warehouse_from_id:
        return document.warehouse_from
    if document_allows_multi_warehouse(document):
        first = document.items.select_related("product__warehouse").order_by("id").first()
        if first is not None:
            wh = resolve_item_warehouse(document, first)
            if wh is not None:
                return wh
    raise ValueError("Для документа нужен warehouse_from или строки с товарами со склада.")


def agent_has_common_access_to_warehouse(*, user, warehouse, company=None) -> bool:
    """Активное членство агента с общим доступом к указанному складу (продажи с остатка склада)."""
    if user is None or warehouse is None:
        return False
    qs = models.CompanyWarehouseAgent.objects.filter(
        user=user,
        status=models.CompanyWarehouseAgent.Status.ACTIVE,
        common_access_enabled=True,
    )
    if company is not None:
        qs = qs.filter(company=company)
    # Доступ есть, если: включён доступ ко всем складам компании; либо склад входит
    # в набор общего доступа (M2M); либо совпадает с legacy-FK common_warehouse
    # (для записей, ещё не переведённых на M2M).
    qs = qs.filter(
        models.Q(common_all_warehouses=True, company_id=warehouse.company_id)
        | models.Q(common_warehouses=warehouse)
        | models.Q(common_warehouse=warehouse)
    )
    return qs.exists()


def agent_can_sell_wholesale(*, user, company=None) -> bool:
    """Активный агент компании, которому владелец разрешил оптовую продажу (can_sell_wholesale=True)."""
    if user is None:
        return False
    qs = models.CompanyWarehouseAgent.objects.filter(
        user=user,
        status=models.CompanyWarehouseAgent.Status.ACTIVE,
        can_sell_wholesale=True,
    )
    if company is not None:
        qs = qs.filter(company=company)
    return qs.exists()


def effective_document_line_discount_percent(
    line_discount_percent,
    document_discount_percent,
) -> Decimal:
    """
    Скидка по строке: если задан процент на товар — он главный; иначе подставляется общая скидка документа.
    Не суммирует и не перемножает обе скидки.
    """
    ld = Decimal(line_discount_percent or 0)
    if ld > 0:
        return ld
    return Decimal(document_discount_percent or 0)


def effective_document_line_discount_amount(
    line_discount_amount,
    effective_discount_percent,
) -> Decimal:
    """
    Сумма скидки по строке применяется, только если процент скидки по строке не действует.
    Клиенты присылают одну и ту же скидку двумя полями (5% и её сумму в сомах) —
    вычитать оба нельзя: товар за 100 сом с 5% давал бы 90 вместо 95.
    """
    if Decimal(effective_discount_percent or 0) > 0:
        return Decimal("0.00")
    return Decimal(line_discount_amount or 0)


def compute_document_line_total(
    *,
    price,
    qty,
    line_discount_percent,
    line_discount_amount,
    document_discount_percent,
) -> Decimal:
    """Единая формула line_total: одна скидка на строку — либо процент, либо сумма."""
    p = Decimal(price or 0)
    q = Decimal(qty or 0)
    eff_pct = effective_document_line_discount_percent(line_discount_percent, document_discount_percent)
    da = effective_document_line_discount_amount(line_discount_amount, eff_pct)
    subtotal = (p * q * (Decimal("1") - eff_pct / Decimal("100"))).quantize(Decimal("0.01"))
    return max(Decimal("0.00"), (subtotal - da).quantize(Decimal("0.01")))


def document_local_date(value):
    """Календарная дата документа по местному времени (Asia/Bishkek), а не по UTC (QA B33)."""
    if value is None:
        return timezone.localdate()
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        return timezone.localtime(value).date()
    return value


def _ensure_number(document: models.Document):
    # Номер TYPE-YYYYMMDD-0001: дата — местная дата самого документа (QA B07/B33),
    # иначе у документа задним числом или проведённого ночью дата в номере «врёт».
    today = document_local_date(document.date)
    with transaction.atomic():
        seq, created = models.DocumentSequence.objects.select_for_update().get_or_create(
            doc_type=document.doc_type, date=today, defaults={"seq": 0}
        )
        seq.seq += 1
        seq.save()
        document.number = f"{document.doc_type}-{today.strftime('%Y%m%d')}-{seq.seq:04d}"
        document.save()


DOCUMENT_FUTURE_DATE_ERROR = "Дата документа не может быть в будущем."


def is_document_date_in_future(value) -> bool:
    """Дата документа позже «сейчас + 1 день» — такие продажи выпадают из текущего периода аналитики."""
    if not value:
        return False
    if not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value > timezone.now() + timedelta(days=1)


def compute_net_amounts(items, doc_discount) -> dict:
    """
    Чистая сумма каждой строки: line_total минус пропорциональная доля скидки документа.
    Остаток округления уходит в самую крупную строку, чтобы Σ net_amount == итог документа.
    Возвращает {item.pk: Decimal}.
    """
    cent = Decimal("0.01")
    items = list(items)
    lines = {i.pk: Decimal(i.line_total or 0) for i in items}
    subtotal = sum(lines.values(), Decimal("0.00"))
    doc_discount = Decimal(doc_discount or 0)
    target = max(Decimal("0.00"), (subtotal - doc_discount).quantize(cent))
    if not lines:
        return {}
    if subtotal <= 0 or doc_discount <= 0:
        nets = {pk: v.quantize(cent) for pk, v in lines.items()}
    else:
        nets = {
            pk: (v - doc_discount * v / subtotal).quantize(cent) for pk, v in lines.items()
        }
    diff = target - sum(nets.values(), Decimal("0.00"))
    if diff != 0:
        biggest = max(lines, key=lambda pk: lines[pk])
        nets[biggest] = (nets[biggest] + diff).quantize(cent)
    return nets


def recalc_document_totals(document: models.Document) -> models.Document:
    # line_total: одна скидка на строку — процент (строковый или общий по документу),
    # а если процента нет — сумма скидки строки. Оба сразу не вычитаем.
    doc_dp = Decimal(document.discount_percent or 0)
    for item in document.items.select_related("product").all():
        new_line_total = compute_document_line_total(
            price=item.price,
            qty=item.qty,
            line_discount_percent=item.discount_percent,
            line_discount_amount=item.discount_amount,
            document_discount_percent=doc_dp,
        )
        if item.line_total != new_line_total:
            item.line_total = new_line_total
            item.save(update_fields=["line_total"])

    # Процент общей скидки уже учтён в строках (fallback для позиций без своего %); итог = сумма строк минус сумма документа
    subtotal = sum(
        ((item.line_total or Decimal("0.00")) for item in document.items.all()),
        Decimal("0.00"),
    )
    subtotal = subtotal.quantize(Decimal("0.01"))

    doc_da = Decimal(document.discount_amount or 0)
    total = max(Decimal("0.00"), (subtotal - doc_da).quantize(Decimal("0.01")))
    document.total = total
    document.save(update_fields=["total"])

    all_items = list(document.items.all())
    nets = compute_net_amounts(all_items, doc_da)
    for item in all_items:
        net = nets.get(item.pk, Decimal("0.00"))
        if item.net_amount != net:
            item.net_amount = net
            item.save(update_fields=["net_amount"])
    return document


def _apply_agent_move(move: models.AgentStockMove):
    bal, _ = models.AgentStockBalance.objects.select_for_update().get_or_create(
        agent=move.agent,
        warehouse=move.warehouse,
        product=move.product,
        defaults={
            "qty": Decimal("0.000"),
            "company": move.warehouse.company,
            "branch": move.warehouse.branch,
        },
    )
    bal.qty = Decimal(bal.qty or 0) + Decimal(move.qty_delta or 0)
    bal.save()


def _resolve_money_doc_type(doc_type: str):
    """
    Для каждой складской операции определяем, какой денежный документ нужен.
    None = кассовый документ не создается, но решение approve/reject все равно требуется.
    """
    mapping = {
        models.Document.DocType.SALE: models.MoneyDocument.DocType.MONEY_RECEIPT,
        models.Document.DocType.PURCHASE: models.MoneyDocument.DocType.MONEY_EXPENSE,
        models.Document.DocType.SALE_RETURN: models.MoneyDocument.DocType.MONEY_EXPENSE,
        models.Document.DocType.PURCHASE_RETURN: models.MoneyDocument.DocType.MONEY_RECEIPT,
        # Приход товара, оплаченный из кассы, — это расход денег (поставщику).
        models.Document.DocType.RECEIPT: models.MoneyDocument.DocType.MONEY_EXPENSE,
        # WRITE_OFF / INVENTORY / TRANSFER - без денежного движения.
    }
    return mapping.get(doc_type)


def _resolve_document_payment_category(document, company, branch):
    """
    Категория платежа для авто-денежного документа: указанная в документе, иначе
    системная по типу документа (создаётся при необходимости), иначе None («Без категории»).
    Раньше бралась «первая попавшаяся» категория компании — отсюда мусор в отчётах.
    """
    payment_category = getattr(document, "payment_category", None)
    if payment_category is not None:
        return payment_category

    SC = models.PaymentCategory.SystemCode
    code_by_doc_type = {
        models.Document.DocType.SALE: SC.SALE,
        models.Document.DocType.PURCHASE: SC.PURCHASE,
        models.Document.DocType.RECEIPT: SC.PURCHASE,
        models.Document.DocType.SALE_RETURN: SC.SALE_RETURN,
        models.Document.DocType.PURCHASE_RETURN: SC.PURCHASE_RETURN,
    }
    code = code_by_doc_type.get(document.doc_type)
    if code is None:
        return None
    from .utils import system_payment_category

    return system_payment_category(company, code)


def _pick_single(qs, *, what: str, allow_multiple_take_first: bool = False):
    """
    Возвращает единственный объект из qs, иначе:
      - None, если пусто
      - ValueError, если найдено больше одного (неоднозначно), кроме случая allow_multiple_take_first=True
      - при allow_multiple_take_first=True при нескольких объектах возвращает первый
    """
    objs = list(qs[:2])
    if len(objs) == 1:
        return objs[0]
    if len(objs) == 0:
        return None
    if allow_multiple_take_first:
        try:
            return qs.order_by("id").first()
        except Exception:
            return qs.first()
    raise ValueError(f"Найдено несколько объектов ({what}). Укажите явно в документе.")


class CashRegisterNotFound(ValueError):
    """Касса для складского документа не найдена. api_code — для фронта (кнопка «Создать кассу»)."""

    api_code = "cash_register_not_found"


def error_payload(exc) -> dict:
    """Тело ответа для ошибки сервиса: {"detail": ...}, "code" и доп. поля, если они есть у исключения."""
    data = {"detail": str(exc)}
    code = getattr(exc, "api_code", None)
    if code:
        data["code"] = code
    extra = getattr(exc, "extra", None)
    if extra:
        data.update(extra)
    return data


def error_status(exc) -> int:
    """HTTP-статус для ошибки сервиса: 403 — нет права (OperationForbidden), иначе 400."""
    return 403 if isinstance(exc, OperationForbidden) else 400


# ---------------------------------------------------------------------------
# Закрытие периода и смена даты (QA B07)
# ---------------------------------------------------------------------------

def get_closed_until(company):
    """Дата закрытия периода компании или None."""
    if company is None:
        return None
    company_id = getattr(company, "id", company)
    return (
        models.WarehouseAccountingSettings.objects.filter(company_id=company_id)
        .values_list("closed_until", flat=True)
        .first()
    )


def ensure_period_open(*, company, dates, user=None):
    """
    Документ с датой <= closed_until нельзя проводить, отменять, редактировать и удалять
    без права can_post_closed_period. dates — старая и/или новая дата документа.
    """
    closed_until = get_closed_until(company)
    if closed_until is None:
        return
    for value in dates:
        if value is None:
            continue
        if document_local_date(value) <= closed_until:
            if perms.has_op_permission(user, perms.POST_CLOSED_PERIOD):
                return
            raise BusinessRuleError(
                f"Период до {closed_until.strftime('%d.%m.%Y')} закрыт. Изменения запрещены.",
                "period_closed",
                closed_until=closed_until.isoformat(),
            )


def ensure_period_open_api(*, company, dates, user=None):
    """
    То же, что ensure_period_open, но для DRF-вьюх (кассы, возвраты, оплаты долгов):
    вместо BusinessRuleError бросает APIException → 400 {"detail", "code": "period_closed", "closed_until"}.
    """
    try:
        ensure_period_open(company=company, dates=dates, user=user)
    except BusinessRuleError as exc:
        from rest_framework.exceptions import APIException

        class PeriodClosed(APIException):
            status_code = 400

        raise PeriodClosed(
            {"detail": str(exc), "code": exc.api_code, **(exc.extra or {})}
        )


def ensure_can_set_document_date(*, user, new_date):
    """Дата документа, отличная от сегодняшней, — только с правом can_change_document_date."""
    if user is None or new_date is None:
        return
    if document_local_date(new_date) != timezone.localdate():
        perms.require_op_permission(user, perms.CHANGE_DOCUMENT_DATE)


def document_company(document):
    company_id = getattr(document, "company_id", None) or document.resolve_company_id()
    return company_id


def ensure_allow_negative_permitted(document, user):
    """
    QA B02: allow_negative=true из запроса — только с правом can_post_negative_stock;
    агентам (в т.ч. мобильное приложение) — никогда.
    """
    if user is None:
        return
    if document.agent_id or perms.is_warehouse_agent(user):
        raise OperationForbidden(perms.POST_NEGATIVE_STOCK, "Агенту проведение в минус запрещено.")
    perms.require_op_permission(user, perms.POST_NEGATIVE_STOCK)


def resolve_cash_register(*, company, branch, explicit=None):
    """
    Касса для денег складского документа.

    explicit — касса из документа: своя компания; касса филиала склада или касса
    компании без филиала (общая). Без явной кассы: касса филиала склада (при нескольких —
    первая), иначе — единственная касса компании без филиала. Нет подходящей —
    CashRegisterNotFound с понятным текстом.
    """
    if explicit is not None:
        if explicit.company_id != company.id:
            raise ValueError("Касса принадлежит другой компании.")
        if explicit.branch_id is not None and (branch is None or explicit.branch_id != branch.id):
            raise ValueError("Касса принадлежит другому филиалу.")
        return explicit

    base = models.CashRegister.objects.filter(company=company)
    if branch is not None:
        cash_register = _pick_single(base.filter(branch=branch), what="касс", allow_multiple_take_first=True)
        if cash_register is not None:
            return cash_register
    company_level = list(base.filter(branch__isnull=True).order_by("id")[:2])
    if len(company_level) == 1:
        return company_level[0]
    if len(company_level) > 1:
        if branch is None:
            return company_level[0]
        raise CashRegisterNotFound(
            f"В филиале «{branch.name}» нет кассы, а касс компании несколько. "
            "Создайте кассу филиала или выберите кассу в документе."
        )
    where = f"В компании (филиале «{branch.name}»)" if branch is not None else "В компании"
    raise CashRegisterNotFound(
        f"{where} нет кассы. Создайте кассу или выберите оплату «В долг» / «Вне кассы»."
    )


def _create_or_reset_cash_request(document: models.Document):
    """
    После проведения создается запрос на кассовое подтверждение.
    Денежный документ будет создан только на approve.
    """
    money_doc_type = _resolve_money_doc_type(document.doc_type)
    payment_kind = effective_payment_kind(document.payment_kind)
    requires_money = (
        payment_kind == models.Document.PaymentKind.CASH
        and money_doc_type is not None
        and not document.agent_id
    )
    amount = Decimal(document.total or 0).quantize(Decimal("0.01"))

    req, _created = models.CashApprovalRequest.objects.update_or_create(
        document=document,
        defaults={
            "status": models.CashApprovalRequest.Status.PENDING,
            "requires_money": requires_money,
            "money_doc_type": money_doc_type if requires_money else None,
            "amount": amount,
            "decision_note": "",
            "decided_at": None,
            "decided_by": None,
            "money_document": None,
        },
    )
    return req


def resolve_document_company(document):
    try:
        wh = resolve_document_context_warehouse(document)
        if wh and wh.company:
            return wh.company
    except Exception:
        pass
    if getattr(document, "warehouse_from", None) and document.warehouse_from.company:
        return document.warehouse_from.company
    if getattr(document, "warehouse_to", None) and document.warehouse_to.company:
        return document.warehouse_to.company
    return getattr(document, "company", None)


def is_cash_confirmation_enabled(company) -> bool:
    if not company:
        return False
    company_id = getattr(company, "id", company)
    conf = getattr(company, "warehouse_cash_confirmation", None)
    if conf is not None:
        return bool(conf.enabled)
    conf = models.WarehouseCashConfirmationSettings.objects.filter(company_id=company_id).first()
    return bool(conf.enabled) if conf else False


def _create_or_post_money_document(
    document: models.Document,
    money_doc_type: str = None,
    amount: Decimal = None,
    *,
    user=None,
    allow_negative_cash: bool = False,
):
    money_doc_type = money_doc_type or _resolve_money_doc_type(document.doc_type)
    if not money_doc_type:
        raise ValueError("Не удалось определить тип денежного документа.")

    amount = Decimal(amount if amount is not None else (document.total or 0)).quantize(Decimal("0.01"))
    if amount <= 0:
        raise ValueError("Сумма для кассы должна быть больше 0.")

    # Идемпотентность: если денежный документ по этому складу уже есть — не создаём дубликат
    existing = getattr(document, "money_document", None)
    if existing is None:
        try:
            existing = models.MoneyDocument.objects.get(source_document_id=document.id)
        except models.MoneyDocument.DoesNotExist:
            pass
    if existing is not None:
        from . import services_money
        if existing.status == models.MoneyDocument.Status.DRAFT:
            existing.doc_type = money_doc_type
            existing.amount = amount
            existing.payment_method = document.payment_method
            existing.counterparty = document.counterparty
            if getattr(document, "cash_register", None):
                existing.cash_register = document.cash_register
            if getattr(document, "payment_category", None):
                existing.payment_category = document.payment_category
            # Дата денег = дата товарного документа (B07): после смены даты
            # и повторного проведения денежный документ не остаётся со старой датой.
            existing.date = document.date
            existing.save()
            services_money.post_money_document(existing, user=user, allow_negative_cash=allow_negative_cash)
        return existing

    if not document.counterparty_id and document.doc_type in (
        models.Document.DocType.SALE,
        models.Document.DocType.SALE_RETURN,
        models.Document.DocType.PURCHASE_RETURN,
    ):
        raise ValueError("Для проведения в кассу укажите контрагента в документе.")

    warehouse = resolve_document_context_warehouse(document)

    company = warehouse.company
    branch = warehouse.branch

    # cash register: from document or auto-pick (касса филиала, иначе касса компании)
    cash_register = resolve_cash_register(
        company=company, branch=branch, explicit=getattr(document, "cash_register", None)
    )

    # payment category: с документа или автовыбор (как касса — при нескольких берём первую); можно не указывать
    payment_category = _resolve_document_payment_category(document, company, branch)

    if payment_category is not None:
        if payment_category.company_id != company.id:
            raise ValueError("Категория платежа принадлежит другой компании.")
        # Категория компании (branch=NULL) подходит любому филиалу (системные — только такие, B13).
        if payment_category.branch_id is not None and (branch is None or payment_category.branch_id != branch.id):
            raise ValueError("Категория платежа принадлежит другому филиалу.")

    from . import services_money

    money_doc = models.MoneyDocument.objects.create(
        doc_type=money_doc_type,
        status=models.MoneyDocument.Status.DRAFT,
        date=document.date,
        cash_register=cash_register,
        counterparty=document.counterparty,
        payment_category=payment_category,
        payment_method=document.payment_method,
        amount=amount,
        comment=f"АВТО: {document.doc_type} {document.number or document.id}",
        company=company,
        branch=branch,
        source_document=document,
    )
    services_money.post_money_document(money_doc, user=user, allow_negative_cash=allow_negative_cash)
    return money_doc


def _create_money_document_for_request(document: models.Document, request_obj: models.CashApprovalRequest):
    if not request_obj.requires_money:
        return None
    return _create_or_post_money_document(
        document,
        money_doc_type=request_obj.money_doc_type,
        amount=request_obj.amount,
    )


def apply_cash_request_effects(
    document: models.Document, *, user=None, note: str = "", allow_negative_cash: bool = False
):
    """
    Применяет денежные эффекты проведения наличного документа:
    - Создает и сразу проводит MoneyDocument (зачисление/списание в кассу)
    - Если у документа уже был CashApprovalRequest, обновляет его статус до APPROVED
    - Синхронизирует автоматический cashflow
    """
    money_doc_type = _resolve_money_doc_type(document.doc_type)
    amount = Decimal(document.total or 0).quantize(Decimal("0.01"))
    money_doc = _create_or_post_money_document(
        document, money_doc_type=money_doc_type, amount=amount,
        user=user, allow_negative_cash=allow_negative_cash,
    )

    request_obj = getattr(document, "cash_request", None)
    if request_obj is not None:
        request_obj.status = models.CashApprovalRequest.Status.APPROVED
        request_obj.requires_money = True
        request_obj.money_doc_type = money_doc_type
        request_obj.amount = amount
        request_obj.decision_note = note or request_obj.decision_note or "Авто: подтверждение кассы отключено."
        request_obj.decided_at = timezone.now()
        request_obj.decided_by = user
        request_obj.money_document = money_doc
        request_obj.save(update_fields=[
            "status",
            "requires_money",
            "money_doc_type",
            "amount",
            "decision_note",
            "decided_at",
            "decided_by",
            "money_document",
        ])

    sync_document_auto_cashflow(document, user=user)
    return money_doc


# ---------------------------------------------------------------------------
# Возвраты по документу-основанию (QA B04)
# ---------------------------------------------------------------------------

RETURN_DOC_TYPES = frozenset({
    models.Document.DocType.SALE_RETURN,
    models.Document.DocType.PURCHASE_RETURN,
})

RETURN_BASE_TYPE = {
    models.Document.DocType.SALE_RETURN: models.Document.DocType.SALE,
    models.Document.DocType.PURCHASE_RETURN: models.Document.DocType.PURCHASE,
}

_POSTED_STATUSES = (models.Document.Status.POSTED, models.Document.Status.CASH_PENDING)


def returned_qty_by_base_item(base_document, *, exclude_document_id=None) -> dict:
    """{base_item_id: Σ qty} по проведённым возвратам этого документа-основания."""
    from django.db.models import Sum

    qs = models.DocumentItem.objects.filter(
        base_item__document=base_document,
        document__status__in=_POSTED_STATUSES,
    )
    if exclude_document_id is not None:
        qs = qs.exclude(document_id=exclude_document_id)
    return {
        row["base_item_id"]: q_qty(Decimal(row["s"] or 0))
        for row in qs.values("base_item_id").annotate(s=Sum("qty"))
    }


def returnable_items(base_document) -> list:
    """Сколько можно вернуть по каждой строке документа-основания (GET .../returnable/)."""
    returned = returned_qty_by_base_item(base_document)
    out = []
    for item in base_document.items.select_related("product").order_by("id"):
        sold = q_qty(Decimal(item.qty or 0))
        ret = returned.get(item.id, Decimal("0.000"))
        qty = Decimal(item.qty or 0)
        unit_price = (Decimal(item.net_amount or item.line_total or 0) / qty) if qty else Decimal("0")
        out.append({
            "base_item": str(item.id),
            "product": str(item.product_id) if item.product_id else None,
            "name": getattr(item.product, "name", "") if item.product_id else "",
            "sold": str(sold),
            "returned": str(ret),
            "returnable": str(max(Decimal("0.000"), q_qty(sold - ret))),
            "price": str(Decimal(item.price or 0).quantize(Decimal("0.01"))),
            "unit_net_price": str(unit_price.quantize(Decimal("0.01"))),
        })
    return out


def _base_document_paid_amount(base_document) -> Decimal:
    """Сколько реально оплачено по продаже/закупу деньгами (для return_exceeds_paid)."""
    md = models.MoneyDocument.objects.filter(
        source_document=base_document, status=models.MoneyDocument.Status.POSTED
    ).first()
    if md is not None:
        return Decimal(md.amount or 0).quantize(Decimal("0.01"))
    return Decimal("0.00")


def _returns_cash_refunded(base_document, *, exclude_document_id=None) -> Decimal:
    from django.db.models import Sum

    qs = models.MoneyDocument.objects.filter(
        source_document__base_document=base_document,
        status=models.MoneyDocument.Status.POSTED,
    )
    if exclude_document_id is not None:
        qs = qs.exclude(source_document_id=exclude_document_id)
    return Decimal(qs.aggregate(s=Sum("amount"))["s"] or 0).quantize(Decimal("0.01"))


def validate_return_document(document, *, user=None):
    """
    Проверки возврата при проведении (B04):
    - есть base_document (переходный период: без основания — только с правом
      can_post_negative_stock; документы агента — по старым правилам);
    - основание проведено, того же типа/контрагента/компании;
    - по каждой строке qty <= продано − уже возвращено (проведённые возвраты);
    - деньгами из кассы — не больше, чем фактически оплачено по основанию.
    """
    base = document.base_document
    if base is None:
        if document.agent_id:
            return
        if user is not None and perms.has_op_permission(user, perms.POST_NEGATIVE_STOCK):
            return
        raise BusinessRuleError(
            "Укажите документ-основание: возврат оформляется по конкретной "
            + ("продаже." if document.doc_type == document.DocType.SALE_RETURN else "закупке."),
            "return_base_required",
        )

    expected_type = RETURN_BASE_TYPE[document.doc_type]
    if (
        base.doc_type != expected_type
        or base.status != models.Document.Status.POSTED
        or (document.counterparty_id and base.counterparty_id != document.counterparty_id)
        or (document_company(base) != document_company(document))
    ):
        raise BusinessRuleError(
            "Документ-основание не подходит: он должен быть проведённой "
            + ("продажей" if expected_type == models.Document.DocType.SALE else "закупкой")
            + " этого же контрагента и компании.",
            "return_base_invalid",
        )

    returned = returned_qty_by_base_item(base, exclude_document_id=document.pk)
    requested = {}
    for item in document.items.select_related("base_item", "product"):
        if item.base_item_id is None or item.base_item.document_id != base.pk:
            raise BusinessRuleError(
                "Каждая строка возврата должна ссылаться на строку документа-основания (base_item).",
                "return_base_invalid",
            )
        requested[item.base_item_id] = requested.get(item.base_item_id, Decimal("0")) + Decimal(item.qty or 0)

    problems = []
    for base_item in base.items.filter(pk__in=list(requested)).select_related("product"):
        sold = q_qty(Decimal(base_item.qty or 0))
        ret = returned.get(base_item.pk, Decimal("0.000"))
        req = q_qty(requested[base_item.pk])
        if req > sold - ret:
            problems.append({
                "base_item": str(base_item.pk),
                "name": getattr(base_item.product, "name", "") if base_item.product_id else "",
                "sold": str(sold),
                "returned": str(ret),
                "requested": str(req),
            })
    if problems:
        p0 = problems[0]
        can = q_qty(Decimal(p0["sold"]) - Decimal(p0["returned"]))
        raise BusinessRuleError(
            f"По строке «{p0['name']}» продано {p0['sold']}, уже возвращено {p0['returned']}. "
            f"Вернуть можно не больше {max(can, Decimal('0.000'))}.",
            "return_exceeds_sold",
            items=problems,
        )

    payment_kind = effective_payment_kind(document.payment_kind)
    cash_out = Decimal("0.00")
    if payment_kind == models.Document.PaymentKind.CASH:
        cash_out = Decimal(document.total or 0)
    cash_out += Decimal(getattr(document, "prepayment_amount", None) or 0)
    if cash_out > 0:
        paid = _base_document_paid_amount(base)
        already = _returns_cash_refunded(base, exclude_document_id=document.pk)
        available = max(Decimal("0.00"), paid - already)
        if cash_out.quantize(Decimal("0.01")) > available:
            raise BusinessRuleError(
                f"Деньгами можно вернуть не больше {available} сом: столько оплачено по документу-основанию "
                f"(с учётом прошлых возвратов). Остаток оформите возвратом в долг (уменьшением долга).",
                "return_exceeds_paid",
                paid=str(paid),
                refunded=str(already),
                amount=str(cash_out.quantize(Decimal("0.01"))),
            )


def post_document(
    document: models.Document,
    allow_negative: bool = None,
    user=None,
    allow_duplicate: bool = False,
    allow_negative_cash: bool = False,
) -> models.Document:
    """
    Проведение складского документа.

    user — кто проводит (для прав §2); None — системный вызов без проверки прав.
    allow_negative=True из запроса — только с правом can_post_negative_stock (B02).
    allow_negative_cash=True — расход из кассы в минус, только с правом can_cash_negative (B06).
    """
    if document.status in (document.Status.CASH_PENDING, document.Status.POSTED):
        raise ValueError("Document already posted")


    if document.doc_type == document.DocType.COMMERCIAL_OFFER:
        raise ValueError("Commercial offer cannot be posted")

    if not document.items.exists():
        raise BusinessRuleError("Нельзя провести документ без строк.", "document_empty")

    if is_document_date_in_future(document.date):
        raise ValueError(DOCUMENT_FUTURE_DATE_ERROR)

    explicit_negative = bool(allow_negative)
    if explicit_negative:
        ensure_allow_negative_permitted(document, user)
    if allow_negative_cash:
        perms.require_op_permission(user, perms.CASH_NEGATIVE)

    ensure_period_open(company=document_company(document), dates=[document.date], user=user)

    # Валидация документа перед проведением
    try:
        document.clean()
    except Exception as e:
        raise ValueError(f"Document validation failed: {str(e)}")
    
    # Валидация всех items
    for item in document.items.select_related("product").all():
        try:
            item.clean()
        except Exception as e:
            raise ValueError(f"Item validation failed for product {item.product_id}: {str(e)}")

    # Если allow_negative не передан явно, берем из настроек
    if allow_negative is None:
        allow_negative = getattr(settings, "ALLOW_NEGATIVE_STOCK", False)
    # Персональный остаток агента — без минуса; общий склад (use_common_stock) — как у обычного документа
    if document.agent_id and not bool(getattr(document, "use_common_stock", False)):
        allow_negative = False

    with transaction.atomic():
        # Блокируем строку документа и перечитываем статус из БД. Проверка выше сделана
        # по экземпляру в памяти: без блокировки два параллельных запроса на проведение
        # (двойной клик по «Провести», ретрай после таймаута) спишут остаток дважды.
        current_status = (
            models.Document.objects.select_for_update()
            .filter(pk=document.pk)
            .values_list("status", flat=True)
            .first()
        )
        if current_status is None:
            raise ValueError("Document not found")
        if current_status in (document.Status.CASH_PENDING, document.Status.POSTED):
            raise ValueError("Document already posted")

        if not document.number:
            _ensure_number(document)

        recalc_document_totals(document)

        if document.doc_type in RETURN_DOC_TYPES:
            if document.base_document_id:
                # Блокируем основание: два параллельных возврата не вернут больше проданного.
                models.Document.objects.select_for_update().filter(pk=document.base_document_id).first()
            validate_return_document(document, user=user)

        # Защита от дубля: тот же документ, отправленный дважды подряд (двойной клик,
        # ретрай после таймаута), иначе остаток на складе меняется дважды.
        if not allow_duplicate:
            twin = find_recent_duplicate_document(document)
            if twin is not None:
                raise ValueError(
                    f"Точно такой же документ уже проведён минуту назад: {twin.number or twin.pk}. "
                    "Похоже на повторную отправку. Если это действительно вторая такая же операция, "
                    "проведите документ ещё раз с параметром allow_duplicate=true."
                )

        # Оптимизация: предзагружаем items с продуктами
        items = list(document.items.select_related("product", "product__warehouse", "product__brand", "product__category").all())

        if document.company_id is None:
            company_id = document.resolve_company_id()
            if company_id is not None:
                document.company_id = company_id
                models.Document.objects.filter(pk=document.pk).update(company_id=company_id)

        # Себестоимость фиксируется на момент проведения (C5): смена закупочной цены
        # позже не меняет прибыль и убытки прошлых периодов.
        costed = []
        for item in items:
            cost = Decimal(getattr(item.product, "purchase_price", None) or 0).quantize(Decimal("0.01"))
            if item.cost_price != cost:
                item.cost_price = cost
                costed.append(item)
        if costed:
            models.DocumentItem.objects.bulk_update(costed, ["cost_price"])

        agent_personal_stock = bool(
            document.agent_id and not bool(getattr(document, "use_common_stock", False))
        )
        if agent_personal_stock:
            if document.doc_type in (document.DocType.TRANSFER, document.DocType.INVENTORY):
                raise ValueError("Agent documents cannot be TRANSFER or INVENTORY")

            sign_map = {
                document.DocType.SALE: Decimal("-1"),
                document.DocType.PURCHASE: Decimal("1"),
                document.DocType.SALE_RETURN: Decimal("1"),
                document.DocType.PURCHASE_RETURN: Decimal("-1"),
                document.DocType.RECEIPT: Decimal("1"),
                document.DocType.WRITE_OFF: Decimal("-1"),
            }
            sign = sign_map.get(document.doc_type)
            if sign is None:
                raise ValueError("Unsupported document type for agent posting")

            for item in items:
                delta = sign * Decimal(item.qty)
                # Мультисклад: списываем/зачисляем по складу каждой позиции
                # (product.warehouse), с откатом на общий warehouse_from документа.
                item_wh = resolve_item_warehouse(document, item)
                if item_wh is None:
                    raise ValueError(
                        f"Не удалось определить склад для товара {item.product_id}. "
                        "Укажите warehouse_from или выберите товар, привязанный к складу."
                    )
                bal, _ = models.AgentStockBalance.objects.select_for_update().get_or_create(
                    agent_id=document.agent_id,
                    warehouse=item_wh,
                    product=item.product,
                    defaults={
                        "qty": Decimal("0.000"),
                        "company": item_wh.company,
                        "branch": item_wh.branch,
                    },
                )
                cur = Decimal(bal.qty or 0)
                if not allow_negative and cur + delta < 0:
                    product_display = item.product.article if item.product.article else item.product.name
                    if not product_display:
                        product_display = f"ID {item.product_id}"
                    raise ValueError(
                        f"Недостаточно у агента для товара '{product_display}'. Доступно: {cur}, требуется: {abs(delta)}"
                    )

                move_kind = models.AgentStockMove.MoveKind.RECEIPT if delta > 0 else models.AgentStockMove.MoveKind.EXPENSE
                mv = models.AgentStockMove.objects.create(
                    document=document,
                    agent_id=document.agent_id,
                    warehouse=item_wh,
                    product=item.product,
                    qty_delta=delta,
                    move_kind=move_kind,
                )
                _apply_agent_move(mv)

        # create moves according to type
        elif document.doc_type == document.DocType.TRANSFER:
            def _get_or_create_transfer_product(source: models.WarehouseProduct, warehouse_to: models.Warehouse):
                dest_company = warehouse_to.company
                same_company = source.company_id == dest_company.id
                qs = models.WarehouseProduct.objects.filter(company_id=dest_company.id, warehouse=warehouse_to)

                if same_company:
                    brand = source.brand
                    category = source.category
                else:
                    brand = (
                        source.brand
                        if source.brand_id and getattr(source.brand, "company_id", None) == dest_company.id
                        else None
                    )
                    category = (
                        source.category
                        if source.category_id and getattr(source.category, "company_id", None) == dest_company.id
                        else None
                    )

                # QA B01: товар-получатель ищем строго так —
                #   1) та же «карточка компании» (catalog_key) на складе-получателе;
                #   2) тот же непустой штрихкод (несколько совпадений → ошибка);
                #   3) иначе новая карточка с тем же catalog_key.
                # По коду и названию НЕ сопоставляем: коды начинались с 0001 в каждом
                # складе, и товар зачислялся на чужую карточку.
                if not source.catalog_key:
                    source.catalog_key = uuid.uuid4()
                    models.WarehouseProduct.objects.filter(pk=source.pk).update(catalog_key=source.catalog_key)
                existing = qs.filter(catalog_key=source.catalog_key).order_by("created_date").first()
                if existing:
                    return existing

                barcode = (source.barcode or "").strip()
                if barcode:
                    matches = list(qs.filter(barcode=barcode).order_by("created_date")[:5])
                    if len(matches) > 1:
                        names = ", ".join(f"«{m.name}»" for m in matches)
                        raise BusinessRuleError(
                            f"На складе «{warehouse_to.name}» несколько товаров со штрихкодом {barcode}: "
                            f"{names}. Уточните товар.",
                            "transfer_target_ambiguous",
                        )
                    if matches:
                        target = matches[0]
                        if not target.catalog_key:
                            target.catalog_key = source.catalog_key
                            models.WarehouseProduct.objects.filter(pk=target.pk).update(
                                catalog_key=source.catalog_key
                            )
                        return target

                # Код новой карточки генерируется заново — сквозной по компании (B31).
                code = None

                plu = getattr(source, "plu", None)
                if plu is not None and qs.filter(plu=plu).exists():
                    plu = None

                new_p = models.WarehouseProduct.objects.create(
                    company=dest_company,
                    branch=warehouse_to.branch,
                    warehouse=warehouse_to,
                    brand=brand,
                    category=category,
                    article=source.article,
                    name=source.name,
                    description=source.description,
                    barcode=source.barcode,
                    code=code,
                    unit=source.unit,
                    is_weight=source.is_weight,
                    purchase_price=source.purchase_price,
                    markup_percent=source.markup_percent,
                    price=source.price,
                    discount_percent=source.discount_percent,
                    plu=plu,
                    country=source.country,
                    status=source.status,
                    stock=source.stock,
                    expiration_date=source.expiration_date,
                    quantity=Decimal("0.000"),
                    catalog_key=source.catalog_key,
                    wholesale_price=source.wholesale_price,
                    minimum_quantity=source.minimum_quantity,
                )
                for link in source.alternate_barcodes.all():
                    if not link.barcode:
                        continue
                    if models.WarehouseProductAlternateBarcode.objects.filter(
                        product__company_id=new_p.company_id,
                        product__warehouse=warehouse_to,
                        barcode=link.barcode,
                    ).exists():
                        continue
                    models.WarehouseProductAlternateBarcode.objects.create(product=new_p, barcode=link.barcode)
                return new_p

            transfer_targets = []
            for item in items:
                if item.product.warehouse_id != document.warehouse_from_id:
                    raise ValueError("Transfer requires product from warehouse_from")
                qty_to_move = q_qty(Decimal(item.qty))
                # Проверка остатка — только по регистру склада-источника, под блокировкой.
                if not allow_negative:
                    cur_from = stock_service.get_on_hand(
                        warehouse=document.warehouse_from, product=item.product, lock=True
                    )
                    if cur_from - qty_to_move < 0:
                        raise stock_service.InsufficientStock(
                            product=item.product,
                            warehouse=document.warehouse_from,
                            available=cur_from,
                            required=qty_to_move,
                        )

                # from — расход со склада-источника
                stock_service.apply_stock_delta(
                    warehouse=document.warehouse_from,
                    product=item.product,
                    delta=-qty_to_move,
                    move_kind=models.StockMove.MoveKind.EXPENSE,
                    document=document,
                    source_kind=models.StockMove.SourceKind.DOCUMENT,
                    allow_negative=allow_negative,
                )
                # to — приход на склад-приёмник (product in destination warehouse)
                dest_product = _get_or_create_transfer_product(item.product, document.warehouse_to)
                transfer_targets.append((item, dest_product))
                stock_service.apply_stock_delta(
                    warehouse=document.warehouse_to,
                    product=dest_product,
                    delta=qty_to_move,
                    move_kind=models.StockMove.MoveKind.RECEIPT,
                    document=document,
                    source_kind=models.StockMove.SourceKind.DOCUMENT,
                    allow_negative=True,
                )

            # Куда зачислено (ответ проведения, B01) и оценка перемещения по
            # себестоимости, а не по цене продажи (B22).
            repriced = []
            for item, dest_product in transfer_targets:
                item.target_product = dest_product
                cost = Decimal(item.cost_price or 0).quantize(Decimal("0.01"))
                if Decimal(item.price or 0) != cost:
                    item.price = cost
                    item.discount_percent = Decimal("0.00")
                    item.discount_amount = Decimal("0.00")
                repriced.append(item)
            if repriced:
                models.DocumentItem.objects.bulk_update(
                    repriced, ["target_product", "price", "discount_percent", "discount_amount"]
                )
                recalc_document_totals(document)

        elif document.doc_type == document.DocType.INVENTORY:
            if document.warehouse_from is None:
                raise ValueError("Для инвентаризации укажите склад (warehouse_from).")
            for item in items:
                # Инвентаризация ставит ровно факт: delta = факт − регистр (под блокировкой).
                fact = q_qty(Decimal(item.qty))
                cur = stock_service.get_on_hand(
                    warehouse=document.warehouse_from, product=item.product, lock=True
                )
                delta = fact - cur
                if delta == 0:
                    continue
                if not allow_negative and fact < 0:
                    warehouse_name = document.warehouse_from.name if document.warehouse_from else "не указан"
                    raise ValueError(
                        f"Инвентаризация приведет к отрицательному остатку для товара "
                        f"'{stock_service.product_display(item.product)}' на складе '{warehouse_name}'. "
                        f"Текущий остаток: {cur}, устанавливается: {item.qty}"
                    )
                stock_service.apply_stock_delta(
                    warehouse=document.warehouse_from,
                    product=item.product,
                    delta=delta,
                    document=document,
                    source_kind=models.StockMove.SourceKind.DOCUMENT,
                    allow_negative=True,
                )

        else:
            # other single-warehouse operations
            sign_map = {
                document.DocType.SALE: Decimal("-1"),
                document.DocType.PURCHASE: Decimal("1"),
                document.DocType.SALE_RETURN: Decimal("1"),
                document.DocType.PURCHASE_RETURN: Decimal("-1"),
                document.DocType.RECEIPT: Decimal("1"),
                document.DocType.WRITE_OFF: Decimal("-1"),
            }
            sign = sign_map.get(document.doc_type)
            if sign is None:
                raise ValueError("Unsupported document type for posting")
            for item in items:
                delta = sign * Decimal(item.qty)
                warehouse = resolve_item_warehouse(document, item)
                if warehouse is None:
                    raise ValueError(
                        f"Не удалось определить склад для товара {item.product_id}. "
                        "Укажите warehouse_from или выберите товар, привязанный к складу."
                    )
                if not allow_negative and delta < 0:
                    # Одно число и для проверки, и для списания: регистр под блокировкой.
                    cur = stock_service.get_on_hand(warehouse=warehouse, product=item.product, lock=True)
                    if cur + delta < 0:
                        raise stock_service.InsufficientStock(
                            product=item.product,
                            warehouse=warehouse,
                            available=cur,
                            required=-delta,
                        )
                stock_service.apply_stock_delta(
                    warehouse=warehouse,
                    product=item.product,
                    delta=delta,
                    document=document,
                    source_kind=models.StockMove.SourceKind.DOCUMENT,
                    allow_negative=allow_negative,
                )

        # Предоплата для credit-документов: создаём и сразу проводим денежный документ на сумму предоплаты.
        prepayment = Decimal(getattr(document, "prepayment_amount", None) or 0).quantize(Decimal("0.01"))
        if prepayment > 0:
            if document.agent_id:
                raise ValueError("Предоплата не поддерживается для документов агента.")

            payment_kind = effective_payment_kind(document.payment_kind)
            if payment_kind != models.Document.PaymentKind.CREDIT:
                raise ValueError("Предоплата возможна только при payment_kind=credit.")

            total = Decimal(document.total or 0).quantize(Decimal("0.01"))
            if prepayment > total:
                raise ValueError("Предоплата не может быть больше суммы документа.")

            money_doc_type = _resolve_money_doc_type(document.doc_type)
            if not money_doc_type:
                raise ValueError("Для этого типа документа предоплата недоступна.")

            if not document.counterparty_id and document.doc_type in (
                models.Document.DocType.SALE,
                models.Document.DocType.SALE_RETURN,
                models.Document.DocType.PURCHASE_RETURN,
            ):
                raise ValueError("Для предоплаты укажите контрагента в документе.")

            warehouse = resolve_document_context_warehouse(document)

            company = warehouse.company
            branch = warehouse.branch

            cash_register = resolve_cash_register(
                company=company, branch=branch, explicit=getattr(document, "cash_register", None)
            )

            payment_category = _resolve_document_payment_category(document, company, branch)

            # Идемпотентность: используем OneToOne money_document (source_document)
            money_doc = getattr(document, "money_document", None)
            if money_doc is None:
                money_doc = models.MoneyDocument.objects.create(
                    doc_type=money_doc_type,
                    status=models.MoneyDocument.Status.DRAFT,
                    date=document.date,
                    cash_register=cash_register,
                    counterparty=document.counterparty,
                    payment_category=payment_category,
                    payment_method=document.payment_method,
                    amount=prepayment,
                    comment=f"ПРЕДОПЛАТА: {document.doc_type} {document.number or document.id}",
                    company=company,
                    branch=branch,
                    source_document=document,
                )
            else:
                # Обновление возможно только если денежный документ ещё не проведён
                if money_doc.status == models.MoneyDocument.Status.POSTED:
                    # если уже проведено — считаем, что предоплата уже учтена
                    pass
                else:
                    money_doc.doc_type = money_doc_type
                    money_doc.cash_register = cash_register
                    money_doc.counterparty = document.counterparty
                    money_doc.payment_category = payment_category
                    money_doc.payment_method = document.payment_method
                    money_doc.amount = prepayment
                    money_doc.comment = f"ПРЕДОПЛАТА: {document.doc_type} {document.number or document.id}"
                    money_doc.company = company
                    money_doc.branch = branch
                    money_doc.save()

            if money_doc.status == models.MoneyDocument.Status.DRAFT:
                from . import services_money
                if money_doc.date != document.date:
                    money_doc.date = document.date
                    money_doc.save(update_fields=["date"])
                services_money.post_money_document(
                    money_doc, user=user, allow_negative_cash=allow_negative_cash
                )

        # Деньги создаём/подтверждаем только для payment_kind=cash.
        # Для credit (и для типов без денежного движения) документ сразу считается проведённым.
        money_doc_type = _resolve_money_doc_type(document.doc_type)
        payment_kind = effective_payment_kind(document.payment_kind)
        requires_money = (
            payment_kind == models.Document.PaymentKind.CASH
            and money_doc_type is not None
            and not document.agent_id
        )

        if requires_money:
            company = resolve_document_company(document)
            confirmation_enabled = is_cash_confirmation_enabled(company)
            if confirmation_enabled:
                document.status = document.Status.CASH_PENDING
                document.save(update_fields=["status"])
                # На этапе post создаем запрос на решение по кассе.
                _create_or_reset_cash_request(document)
            else:
                document.status = document.Status.POSTED
                document.save(update_fields=["status"])
                apply_cash_request_effects(document, user=user, allow_negative_cash=allow_negative_cash)
        else:
            # Если по документу раньше был кассовый запрос (например, сменили payment_kind),
            # помечаем его как обработанный, чтобы не висел в PENDING.
            try:
                req = document.cash_request
            except Exception:
                req = None
            if req is not None:
                req.status = models.CashApprovalRequest.Status.APPROVED
                req.requires_money = False
                req.money_doc_type = None
                req.amount = Decimal(document.total or 0).quantize(Decimal("0.01"))
                req.decision_note = "Авто: касса не требуется."
                req.decided_at = timezone.now()
                req.decided_by = None
                req.money_document = None
                req.save(
                    update_fields=[
                        "status",
                        "requires_money",
                        "money_doc_type",
                        "amount",
                        "decision_note",
                        "decided_at",
                        "decided_by",
                        "money_document",
                    ]
                )

            document.status = document.Status.POSTED
            document.save(update_fields=["status"])
            sync_document_auto_cashflow(document, user=user)

        if explicit_negative and allow_negative:
            # Кто и когда провёл документ в обход запрета минуса (B02).
            document.posted_negative_by = user
            document.posted_negative_at = timezone.now()
            document.save(update_fields=["posted_negative_by", "posted_negative_at"])

        # Начисления зарплаты агенту (процент с продажи со склада-источника).
        # В той же транзакции, что и проведение продажи.
        from apps.warehouse import salary_services
        salary_services.create_accruals_for_document(document)

    return document


def _check_unpost_negative(document, moves, *, agent_personal_stock: bool):
    """
    QA B03: откат документа, который увеличивал остаток (закуп, приход, возврат продажи,
    входящее перемещение, инвентаризация с плюсом), не должен уводить остаток в минус —
    значит, товар уже продан/списан. Проверка под блокировкой регистра.
    """
    need = {}
    for mv in moves:
        qty_delta = q_qty(Decimal(mv.qty_delta or 0))
        if qty_delta <= 0:
            continue
        key = (mv.warehouse_id, mv.product_id)
        if key not in need:
            need[key] = [mv.warehouse, mv.product, Decimal("0.000")]
        need[key][2] += qty_delta

    problems = []
    for (wh_id, product_id), (warehouse, product, qty) in need.items():
        if product is None:
            continue
        if agent_personal_stock:
            bal = (
                models.AgentStockBalance.objects.select_for_update()
                .filter(agent_id=document.agent_id, warehouse_id=wh_id, product_id=product_id)
                .first()
            )
            cur = q_qty(Decimal(getattr(bal, "qty", 0) or 0))
        else:
            cur = stock_service.get_on_hand(warehouse=warehouse, product=product, lock=True)
        after = q_qty(cur - qty)
        if after < 0:
            problems.append({
                "product": str(product_id),
                "name": getattr(product, "name", "") or "",
                "warehouse": str(wh_id),
                "current_qty": str(cur),
                "after_qty": str(after),
            })
    if problems:
        first = problems[0]
        raise BusinessRuleError(
            f"Нельзя отменить проведение: товар «{first['name']}» уже продан или списан. "
            f"Остаток станет {first['after_qty']}.",
            "unpost_negative_stock",
            items=problems,
        )


def unpost_document(document: models.Document, user=None, allow_negative: bool = False) -> models.Document:
    """
    Отмена проведения. user — кто отменяет (право can_unpost_documents, §2);
    None — системный вызов (отказ кассы, каскад из кэшфлоу) без проверки прав.
    allow_negative=True — откат в минус, только с правом can_post_negative_stock.
    """
    if document.status not in (document.Status.POSTED, document.Status.CASH_PENDING):
        raise ValueError("Document is not posted")

    if user is not None:
        perms.require_op_permission(user, perms.UNPOST_DOCUMENTS)
    if allow_negative:
        ensure_allow_negative_permitted(document, user)
    ensure_period_open(company=document_company(document), dates=[document.date], user=user)

    # Возвраты по этому документу (B04): пока они проведены, отмена продажи/закупа
    # сделала бы возвращённое количество больше проданного.
    if document.doc_type in (document.DocType.SALE, document.DocType.PURCHASE):
        posted_returns = list(
            document.returns.filter(
                status__in=(document.Status.POSTED, document.Status.CASH_PENDING)
            ).values_list("number", flat=True)[:5]
        )
        if posted_returns:
            raise BusinessRuleError(
                "Нельзя отменить проведение: по документу проведены возвраты "
                f"({', '.join(n or '—' for n in posted_returns)}). Сначала отмените их.",
                "unpost_has_returns",
            )

    with transaction.atomic():
        agent_personal_stock = bool(
            document.agent_id and not bool(getattr(document, "use_common_stock", False))
        )
        if agent_personal_stock:
            moves = list(document.agent_moves.select_related("warehouse", "product").select_for_update())
            if not allow_negative:
                _check_unpost_negative(document, moves, agent_personal_stock=True)
            for mv in moves:
                bal, _ = models.AgentStockBalance.objects.select_for_update().get_or_create(
                    agent=mv.agent,
                    warehouse=mv.warehouse,
                    product=mv.product,
                    defaults={
                        "qty": Decimal("0.000"),
                        "company": mv.warehouse.company,
                        "branch": mv.warehouse.branch,
                    },
                )
                cur = Decimal(bal.qty or 0)
                bal.qty = cur - Decimal(mv.qty_delta or 0)
                if bal.qty < 0:
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.warning(
                        f"Unposting agent document {document.number} results in negative balance {bal.qty} "
                        f"for product {mv.product_id} at warehouse {mv.warehouse_id}"
                    )
                bal.save()
                # Личный остаток агента не связан с карточкой склада — карточку не трогаем
                # (симметрично _apply_agent_move при проведении).
                mv.delete()
        else:
            # Сторно: на каждое движение документа — обратное движение через сервис
            # остатков. Исходные движения не удаляются (история «провели → отменили»),
            # а отвязываются от документа (document=NULL, source_id=id документа),
            # чтобы повторное проведение не смешивалось со старыми движениями.
            moves = list(
                document.moves.select_related("warehouse", "product").select_for_update(of=("self",))
            )
            if not allow_negative:
                _check_unpost_negative(document, moves, agent_personal_stock=False)
            for mv in moves:
                qty_delta = q_qty(Decimal(mv.qty_delta or 0))
                bal = stock_service.apply_stock_delta(
                    warehouse=mv.warehouse,
                    product=mv.product,
                    delta=-qty_delta,
                    move_kind=(
                        models.StockMove.MoveKind.EXPENSE
                        if qty_delta > 0
                        else models.StockMove.MoveKind.RECEIPT
                    ),
                    document=None,
                    source_kind=models.StockMove.SourceKind.DOCUMENT,
                    source_id=document.id,
                    allow_negative=True,  # отмену не блокируем, но логируем минус
                )
                if Decimal(bal.qty or 0) < 0:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Unposting document %s results in negative balance %s for product %s at warehouse %s",
                        document.number, bal.qty, mv.product_id, mv.warehouse_id,
                    )
            if moves:
                models.StockMove.objects.filter(pk__in=[mv.pk for mv in moves]).update(
                    document=None, source_id=document.id
                )

        # Если был уже создан денежный документ - откатим его.
        try:
            money_doc = getattr(document, "money_document", None)
        except Exception:
            money_doc = None
        if money_doc is not None and money_doc.status == models.MoneyDocument.Status.POSTED:
            from . import services_money
            services_money.unpost_money_document(money_doc)

        # Если был pending-запрос на кассу — пометим отклоненным.
        try:
            cash_req = getattr(document, "cash_request", None)
        except Exception:
            cash_req = None
        # Важно: если запрос уже был APPROVED и потом документ распроводят,
        # запрос тоже должен отражать отмену (иначе в UI будет "подтверждено", но документ уже DRAFT).
        if cash_req is not None and cash_req.status in (
            models.CashApprovalRequest.Status.PENDING,
            models.CashApprovalRequest.Status.APPROVED,
        ):
            cash_req.status = models.CashApprovalRequest.Status.REJECTED
            cash_req.decision_note = "Отменено автоматически: документ распроведен."
            cash_req.decided_at = timezone.now()
            cash_req.decided_by = None
            cash_req.save(update_fields=["status", "decision_note", "decided_at", "decided_by"])

        document.status = document.Status.DRAFT
        document.save()

        # Распроведение продажи агента → отмена/корректировка начислений зарплаты.
        from apps.warehouse import salary_services
        salary_services.cancel_accruals_for_document(document)

    return document


def sync_document_auto_cashflow(document: models.Document, user=None):
    from apps.construction.auto_cashflow import create_auto_cashflow
    from apps.construction.models import CashFlow

    total = Decimal(document.total or 0).quantize(Decimal("0.01"))
    if total <= 0:
        return None

    kind_type_map = {
        models.Document.DocType.PURCHASE: (CashFlow.SourceKind.PROCUREMENT_RECEIPT, CashFlow.Type.EXPENSE, "Закупки"),
        models.Document.DocType.RECEIPT: (CashFlow.SourceKind.PROCUREMENT_RECEIPT, CashFlow.Type.EXPENSE, "Закупки"),
        models.Document.DocType.PURCHASE_RETURN: (CashFlow.SourceKind.SUPPLIER_RETURN, CashFlow.Type.INCOME, "Возврат поставщику"),
        models.Document.DocType.WRITE_OFF: (CashFlow.SourceKind.DEFECT_WRITEOFF, CashFlow.Type.EXPENSE, "Списание брака"),
        models.Document.DocType.SALE_RETURN: (CashFlow.SourceKind.PRODUCT_RETURN, CashFlow.Type.INCOME, "Возврат товара"),
    }

    if document.doc_type not in kind_type_map:
        return None

    src_kind, flow_type, default_name = kind_type_map[document.doc_type]
    warehouse = resolve_document_context_warehouse(document)
    company = warehouse.company if warehouse else (document.warehouse_from.company if document.warehouse_from else getattr(document, "company", None))
    branch = warehouse.branch if warehouse else (document.warehouse_from.branch if document.warehouse_from else None)
    if not company:
        return None

    try:
        cf = create_auto_cashflow(
            company=company,
            branch=branch,
            user=user or getattr(document, "agent", None),
            type=flow_type,
            amount=total,
            source_kind=src_kind,
            source_id=str(document.id),
            name=f"{default_name}: {document.number or document.id}",
            source_business_operation_id=default_name,
        )
        return cf
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to create auto cashflow for document %s: %s", document.id, exc)
        return None


def approve_cash_request(
    document: models.Document, *, decided_by=None, note: str = "", allow_negative_cash: bool = False
) -> models.Document:
    if document.status != document.Status.CASH_PENDING:
        raise ValueError("Документ не ожидает решения кассы.")

    request_obj = getattr(document, "cash_request", None)
    if request_obj is None:
        raise ValueError("Запрос в кассу не найден.")
    if request_obj.status != models.CashApprovalRequest.Status.PENDING:
        raise ValueError("Запрос в кассу уже обработан.")

    with transaction.atomic():
        apply_cash_request_effects(
            document, user=decided_by, note=note, allow_negative_cash=allow_negative_cash
        )
        document.status = document.Status.POSTED
        document.save(update_fields=["status"])

    return document



def reject_cash_request(document: models.Document, *, decided_by=None, note: str = "") -> models.Document:
    if document.status != document.Status.CASH_PENDING:
        raise ValueError("Документ не ожидает решения кассы.")

    request_obj = getattr(document, "cash_request", None)
    if request_obj is None:
        raise ValueError("Запрос в кассу не найден.")
    if request_obj.status != models.CashApprovalRequest.Status.PENDING:
        raise ValueError("Запрос в кассу уже обработан.")

    with transaction.atomic():
        # Откатываем складские движения
        unpost_document(document)
        document.status = document.Status.REJECTED
        document.save(update_fields=["status"])

        request_obj.status = models.CashApprovalRequest.Status.REJECTED
        request_obj.decision_note = note or ""
        request_obj.decided_at = timezone.now()
        request_obj.decided_by = decided_by
        request_obj.save(update_fields=["status", "decision_note", "decided_at", "decided_by"])

    return document
