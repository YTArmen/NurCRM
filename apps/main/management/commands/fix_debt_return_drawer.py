"""
ТЗ кассы 05.11 п.2: старые возвраты чеков в долг, частично погашенных наличными.

Гашение долга наличными (debt_repayment, affects_shift_drawer=True) попадало в ящик смены, а выдача
этих денег при возврате (pos_sale_return) — нет (affects_shift_drawer=False). Ожидаемая наличность смены
возврата оставалась завышенной — у кассира ложная недостача.

Команда ставит affects_shift_drawer=True у таких расходов, если гашений наличными в ящик по сделке чека
не меньше суммы расхода (сделку чека находим по sale, гашения — по имени «Оплата долга: <title сделки>» или «Оплата долга по продаже №<номер>»;
у сделки без «№» в названии имя не уникально — такие только показываем).

  python manage.py fix_debt_return_drawer                 # только показать (dry-run)
  python manage.py fix_debt_return_drawer --apply
  python manage.py fix_debt_return_drawer --company-id <uuid>
"""
from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum

from apps.construction.models import CashFlow
from apps.main.models import ClientDeal, Sale


class Command(BaseCommand):
    help = "Выдача наличных при возврате чека в долг — из ящика смены (ТЗ 05.11 п.2). По умолчанию dry-run."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--company-id", default=None)

    def handle(self, *args, **opts):
        z = Decimal("0.00")
        flows = CashFlow.objects.filter(
            source_kind=CashFlow.SourceKind.POS_SALE_RETURN, type=CashFlow.Type.EXPENSE,
            affects_shift_drawer=False, shift__isnull=False,
        ).exclude(payment_method__in=["transfer", "card", "noncash", "mbank", "optima", "obank", "bakai"])
        if opts["company_id"]:
            flows = flows.filter(company_id=opts["company_id"])
        fixed = manual = 0
        for cf in flows.order_by("created_at"):
            sale = Sale.objects.filter(pk=cf.source_id, payment_method=Sale.PaymentMethod.DEBT).first()
            if sale is None:
                continue
            deal = ClientDeal.objects.filter(sale=sale).first()
            if deal is None:
                continue
            names = [f"Оплата долга: {deal.title}"]
            if sale.doc_number:
                names.append(f"Оплата долга по продаже №{sale.doc_number}")
            repaid = CashFlow.objects.filter(
                company_id=sale.company_id, type=CashFlow.Type.INCOME,
                source_kind=CashFlow.SourceKind.DEBT_REPAYMENT, affects_shift_drawer=True, name__in=names,
            ).aggregate(s=Sum("amount"))["s"] or z
            already = CashFlow.objects.filter(
                company_id=sale.company_id, type=CashFlow.Type.EXPENSE,
                source_kind=CashFlow.SourceKind.POS_SALE_RETURN, source_id=str(sale.id), affects_shift_drawer=True,
            ).aggregate(s=Sum("amount"))["s"] or z
            avail = repaid - already
            line = (f"чек №{sale.doc_number} компания {sale.company_id} смена {cf.shift_id}: "
                    f"расход {cf.amount}, гашений наличными {repaid}, уже из ящика {already}")
            if avail <= 0:
                continue
            if "№" not in (deal.title or "") or cf.amount > avail:
                self.stdout.write(f"  ВРУЧНУЮ: {line}")
                manual += 1
                continue
            self.stdout.write(f"  {line} → из ящика")
            if opts["apply"]:
                CashFlow.objects.filter(pk=cf.pk).update(affects_shift_drawer=True)
            fixed += 1
        mode = "ЗАПИСАНО" if opts["apply"] else "dry-run (ничего не записано)"
        self.stdout.write(self.style.SUCCESS(f"{mode}: исправить {fixed}, вручную {manual}"))
