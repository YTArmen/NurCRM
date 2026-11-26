"""Хранение чатов витрины 90 дней: удаляет старые сеансы посетителей с диалогами и старый журнал ходов помощника.
Безопасно запускать повторно (cron раз в сутки): python manage.py purge_showcase_chats [--days 90]"""
from django.core.management.base import BaseCommand

from apps.inbox import assistant


class Command(BaseCommand):
    help = "Удалить чаты витрины старше N дней (по умолчанию 90)"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=assistant.RETENTION_DAYS)

    def handle(self, *args, **opts):
        res = assistant.purge_old(days=opts["days"])
        self.stdout.write(f"purge_showcase_chats: {res}")
