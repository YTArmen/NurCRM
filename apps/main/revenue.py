"""
ТЗ 31.10 п.11: единое определение выручки для отчётов (Сводка, Прибыль и убытки, ABC).

Выручка = сумма `Sale.total` чеков-продаж:
  * оплаченные (paid) и с частичным возвратом (partially_returned) — по дате оплаты paid_at;
    total частично возвращённого чека уже уменьшен на возврат, поэтому возвраты вычитаются;
  * чеки в долг (debt) — это продажа, входят в сумму чека целиком — по дате создания (paid_at у них пуст);
  * отменённые (canceled) и новые (new) не входят;
  * бонусы — часть оплаты (total чека уже включает оплаченное бонусами), отдельно не вычитаются.
Чек, оплаченный после долга, считается по дате погашения (paid_at) — как и раньше в «Продажах».
"""
from django.db.models import Q, Sum, Value
from django.db.models.functions import Coalesce

REVENUE_CODE = "sales_total_minus_returns_v1"
REVENUE_TEXT = (
    "Сумма чеков: оплаченные и с частичным возвратом (за вычетом возвратов) по дате оплаты плюс чеки в долг "
    "целиком по дате продажи; отменённые не входят; бонусы — часть оплаты, отдельно не вычитаются."
)


def revenue_definition(note: str = "", **extra) -> dict:
    d = {"code": REVENUE_CODE, "text": REVENUE_TEXT}
    if note:
        d["note"] = note
    d.update(extra)
    return d


def revenue_sales_qs(Sale, qs, period):
    """qs уже ограничен компанией/филиалом/фильтрами; здесь — статусы и период по единому правилу."""
    paid = (Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED)
    in_paid = Q(status__in=paid, paid_at__gte=period.start, paid_at__lt=period.end)
    in_debt = Q(status=Sale.Status.DEBT, created_at__gte=period.start, created_at__lt=period.end)
    # Аудит 12.11: погашенный/частично возвращённый долговой чек без paid_at — по дате продажи.
    in_paid_no_date = Q(status__in=paid, paid_at__isnull=True, created_at__gte=period.start, created_at__lt=period.end)
    return qs.filter(in_paid | in_debt | in_paid_no_date)


def revenue_total(sales_qs):
    from decimal import Decimal
    from django.db.models import DecimalField
    zero = Value(Decimal("0.00"), output_field=DecimalField(max_digits=14, decimal_places=2))
    return sales_qs.aggregate(
        s=Coalesce(Sum("total"), zero, output_field=DecimalField(max_digits=14, decimal_places=2))
    )["s"]
