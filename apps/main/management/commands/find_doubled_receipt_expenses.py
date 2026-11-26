"""ТЗ Б-05: поиск задвоенных расходов по приходам поставщика (сайт писал второй расход поверх
procurement_receipt). Только показывает пары: один поставщик/сумма, разница во времени ≤ 5 с.
Ничего не удаляет — владелец решает сам. Запуск: manage.py find_doubled_receipt_expenses [--company ID]"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from apps.construction.models import CashFlow


class Command(BaseCommand):
    help = "Показать пары расходов с одной суммой в пределах 5 секунд (кандидаты в дубли прихода)."

    def add_arguments(self, parser):
        parser.add_argument("--company", default=None)
        parser.add_argument("--seconds", type=int, default=5)

    def handle(self, *args, **opts):
        qs = CashFlow.objects.filter(type=CashFlow.Type.EXPENSE).order_by("company_id", "amount", "created_at")
        if opts["company"]:
            qs = qs.filter(company_id=opts["company"])
        groups = defaultdict(list)
        for cf in qs.only("id", "company_id", "amount", "created_at", "name", "source_kind", "source_id", "shift_id"):
            groups[(cf.company_id, cf.amount)].append(cf)
        found = 0
        for rows in groups.values():
            for a, b in zip(rows, rows[1:]):
                if (b.created_at - a.created_at).total_seconds() <= opts["seconds"] and (
                    "procurement_receipt" in (a.source_kind, b.source_kind)
                ):
                    found += 1
                    self.stdout.write(
                        f"{a.amount} | {a.id} [{a.source_kind}, смена {a.shift_id}] <-> {b.id} [{b.source_kind}, смена {b.shift_id}] | {a.name}"
                    )
        self.stdout.write(f"Найдено пар: {found}")
