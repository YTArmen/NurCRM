"""
ТЗ 31.10 п.13: проставить branch у старых чеков без филиала.

Филиал берётся из смены чека (shift.branch), затем из кассы смены (shift.cashbox.branch),
затем из кассы чека (cashbox.branch). Чеки, где филиал так и не нашёлся, не трогаются.

Запуск:
  python manage.py backfill_sale_branch                      # только показать (dry-run)
  python manage.py backfill_sale_branch --apply              # записать
  python manage.py backfill_sale_branch --company-id <uuid>  # одна компания
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand

from apps.main.models import Sale


class Command(BaseCommand):
    help = "Проставить branch у чеков без филиала (из смены/кассы). По умолчанию dry-run."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Записать изменения (без флага — только показать).")
        parser.add_argument("--company-id", default=None)

    def handle(self, *args, **opts):
        apply = opts["apply"]
        qs = Sale.objects.filter(branch__isnull=True).select_related(
            "shift__branch", "shift__cashbox__branch", "cashbox__branch"
        )
        if opts["company_id"]:
            qs = qs.filter(company_id=opts["company_id"])
        found = Counter()
        updates = {}  # branch_id -> [sale_id]
        total = 0
        for sale in qs.iterator(chunk_size=500):
            total += 1
            shift = sale.shift
            branch_id = (
                (shift.branch_id if shift else None)
                or (shift.cashbox.branch_id if shift and shift.cashbox_id else None)
                or (sale.cashbox.branch_id if sale.cashbox_id else None)
            )
            if branch_id:
                updates.setdefault(branch_id, []).append(sale.pk)
            else:
                found["без филиала в смене/кассе"] += 1
        fixed = sum(len(v) for v in updates.values())
        if apply:
            for branch_id, ids in updates.items():
                for i in range(0, len(ids), 1000):
                    Sale.objects.filter(pk__in=ids[i:i + 1000], branch__isnull=True).update(branch_id=branch_id)
        self.stdout.write(
            f"Чеков без branch: {total}; можно проставить: {fixed}; не найден филиал: {found['без филиала в смене/кассе']}."
            + (" Записано." if apply else " Это dry-run, для записи добавьте --apply.")
        )
