from django.core.management.base import BaseCommand

from apps.main.tz1029_bonus_expire import expire_bonuses


class Command(BaseCommand):
    help = "ТЗ 29.10 Б-99: сжигает бонусы клиентов старше срока points_expire_days"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="только напечатать")

    def handle(self, *args, **opts):
        res = expire_bonuses(dry_run=opts["dry_run"], out=self.stdout.write)
        self.stdout.write(f"Клиентов: {len(res)}, сумма: {sum(a for _, a in res)}" + (" (dry-run)" if opts["dry_run"] else ""))
