"""
Заполнение фото глобального каталога (GlobalProduct). Можно запускать повторно и продолжать после остановки.

  manage.py fill_global_photos --source store            # быстро: ссылки на уже загруженные фото магазинов
  manage.py fill_global_photos --source off              # долго: Open Food Facts, ~80 запросов/мин
  manage.py fill_global_photos --source all --dry-run    # только посчитать
"""
from django.core.management.base import BaseCommand

from apps.catalog_media import services


class Command(BaseCommand):
    help = "Заполняет apps.catalog_media.GlobalProductPhoto (store = фото магазинов, off = Open Food Facts)"

    def add_arguments(self, parser):
        parser.add_argument("--source", choices=["store", "off", "all"], default="all")
        parser.add_argument("--limit", type=int, default=None, help="Максимум найденных (store) / запросов (off)")
        parser.add_argument("--dry-run", action="store_true", help="Ничего не писать и не ходить в OFF")
        parser.add_argument("--rate", type=int, default=80, help="Запросов в минуту к OFF (лимит OFF ~100)")
        parser.add_argument("--recheck-days", type=int, default=30,
                            help="Повторить OFF-промахи старше N дней (по умолчанию 30)")
        parser.add_argument("--no-checksum", action="store_true",
                            help="Не проверять контрольную цифру GTIN (только 8–14 цифр)")

    def handle(self, *args, **opts):
        out = lambda m: self.stdout.write(m)  # noqa: E731
        src = opts["source"]
        dry = opts["dry_run"]
        if dry:
            out("DRY-RUN: ничего не записывается")
        try:
            if src in ("store", "all"):
                out(f"store: {services.fill_store(limit=opts['limit'], dry_run=dry, out=out)}")
            if src in ("off", "all"):
                out(f"off: {services.fill_off(limit=opts['limit'], dry_run=dry, rate_per_min=opts['rate'], recheck_days=opts['recheck_days'], check_digit=not opts['no_checksum'], out=out)}")
        except KeyboardInterrupt:
            out("Остановлено. Прогресс сохранён, запустите снова, чтобы продолжить.")
