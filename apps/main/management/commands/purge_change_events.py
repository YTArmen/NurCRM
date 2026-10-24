"""ТЗ Н-31: удаляет события ленты изменений старше 7 дней (запускать раз в сутки)."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.main.change_feed import RETENTION_DAYS
from apps.main.models import ChangeEvent


class Command(BaseCommand):
    help = "Удалить события ленты изменений старше 7 дней"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=RETENTION_DAYS)

    def handle(self, *args, **opts):
        border = timezone.now() - timedelta(days=opts["days"])
        total = 0
        while True:  # пачками, чтобы не держать долгую транзакцию
            ids = list(ChangeEvent.objects.filter(created_at__lt=border).values_list("id", flat=True)[:5000])
            if not ids:
                break
            total += ChangeEvent.objects.filter(id__in=ids).delete()[0]
        self.stdout.write(f"Удалено событий: {total}")
