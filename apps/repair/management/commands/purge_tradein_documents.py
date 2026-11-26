"""П2-1: паспортные данные скупки хранятся 1 год (решение 15.11, п.5). Запуск раз в день (cron)."""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    def handle(self, *a, **o):
        from apps.repair.models import TradeIn

        n = TradeIn.objects.filter(document_expires_at__lt=timezone.now()).exclude(document_enc="").update(document_enc="", document_expires_at=None)
        self.stdout.write(f"очищено документов: {n}")
