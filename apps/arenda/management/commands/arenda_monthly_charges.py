from datetime import date

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.arenda.services.charges import generate_monthly_charges


class Command(BaseCommand):
    help = "Начисления аренды за месяц по всем действующим договорам (идемпотентно). Запускать 1-го числа."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Любая дата нужного месяца, YYYY-MM-DD (по умолчанию сегодня)")

    def handle(self, *args, **opts):
        d = date.fromisoformat(opts["date"]) if opts.get("date") else timezone.localdate()
        res = generate_monthly_charges(d)
        self.stdout.write(f"Начисления за {res['period']}: создано {res['created']}, уже были {res['skipped']}")
