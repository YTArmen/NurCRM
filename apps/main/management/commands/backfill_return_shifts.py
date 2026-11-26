from django.core.management.base import BaseCommand

from apps.main.management.commands.backfill_sale_returns import Command as _Legacy
from apps.main.models import SaleReturn


class Command(BaseCommand):
    help = (
        "ТЗ Н-07: проставляет смену у старых возвратов без shift (по движению возврата и по "
        "смене кассира на момент возврата). По умолчанию — только показывает (dry-run); "
        "записать — с флагом --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Записать изменения (без флага — только отчёт).")

    def handle(self, *args, **opts):
        finder = _Legacy()._shift_id
        qs = SaleReturn.objects.filter(shift__isnull=True)
        total = qs.count()
        found = 0
        for r in qs.iterator():
            shift_id = finder(r)
            if not shift_id:
                self.stdout.write(f"  не найдена смена: возврат {r.id} (чек {r.sale_id}, {r.created_at:%Y-%m-%d %H:%M})")
                continue
            found += 1
            if opts["apply"]:
                SaleReturn.objects.filter(pk=r.pk, shift__isnull=True).update(shift_id=shift_id)
        self.stdout.write(self.style.SUCCESS(
            f"Возвратов без смены: {total}, смена найдена: {found}"
            + ("" if opts["apply"] else " (dry-run, для записи добавьте --apply)")
        ))
