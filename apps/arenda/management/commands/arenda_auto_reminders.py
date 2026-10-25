from datetime import date

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.arenda.models import ArendaSettings
from apps.arenda.services.reminders import run_auto_reminders


class Command(BaseCommand):
    help = ("Автонапоминания и автопеня аренды по включённым правилам (ТЗ §3.9). Идемпотентно: повторный запуск "
            "на ту же дату не создаёт дублей. Запускать раз в день.")

    def add_arguments(self, parser):
        parser.add_argument("--date", help="YYYY-MM-DD (по умолчанию сегодня)")

    def handle(self, *args, **opts):
        d = date.fromisoformat(opts["date"]) if opts.get("date") else timezone.localdate()
        total = {}
        for s in ArendaSettings.objects.select_related("company"):
            for k, v in run_auto_reminders(s.company, d).items():
                total[k] = total.get(k, 0) + v
        self.stdout.write(f"Автонапоминания за {d}: " + (", ".join(f"{k}={v}" for k, v in total.items()) or "ничего нового"))
