from django.core.management.base import BaseCommand

from apps.construction import trash


class Command(BaseCommand):
    help = "Окончательно удаляет записи корзины старше 30 дней (ТЗ «Админка» п.9). Запускать раз в сутки."

    def handle(self, *args, **options):
        self.stdout.write(f"Удалено записей корзины: {trash.purge_expired()}")
