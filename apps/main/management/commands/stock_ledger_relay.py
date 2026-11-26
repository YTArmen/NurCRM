"""П1-6 К-15: события stock_changed из журнала остатков (все изменения, включая UPDATE … F()).

Запуск cron каждую минуту: один проход и выход (--seconds 0); --seconds N — опрашивать N секунд. Позиция — в кэше
(nur:ledger_relay:last_id); при первом запуске начинает с текущего конца журнала (старое не рассылает).
Одновременный второй запуск ждёт блокировку и выходит.
"""
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import connection

KEY = "nur:ledger_relay:last_id"


class Command(BaseCommand):
    help = "stock_changed из main_stockledger в ленту изменений/WebSocket."

    def add_arguments(self, parser):
        parser.add_argument("--seconds", type=int, default=0)
        parser.add_argument("--every", type=int, default=5)

    def handle(self, *args, seconds, every, **opts):
        with connection.cursor() as c:
            c.execute("SELECT pg_try_advisory_lock(771511)")
            if not c.fetchone()[0]:
                return
        try:
            self._loop(seconds, every)
        finally:
            with connection.cursor() as c:
                c.execute("SELECT pg_advisory_unlock(771511)")

    def _loop(self, seconds, every):
        from apps.main.change_feed import _emit

        last = cache.get(KEY)
        if last is None:
            with connection.cursor() as c:
                c.execute("SELECT coalesce(max(id), 0) FROM main_stockledger")
                last = c.fetchone()[0]
            cache.set(KEY, last, None)
        end = time.monotonic() + seconds
        while True:
            with connection.cursor() as c:
                c.execute("""SELECT id, company_id, branch_id, product_id, qty_after, qty_delta, source
                             FROM main_stockledger WHERE id > %s AND kind <> 'opening' ORDER BY id LIMIT 5000""", [last])
                rows = c.fetchall()
            latest = {}
            for rid, co, br, pid, after, delta, src in rows:
                cur = latest.get(pid)
                latest[pid] = (co, br, after, (cur[3] if cur else 0) + delta, src)
                last = rid
            for pid, (co, br, after, delta, src) in latest.items():
                try:
                    _emit(co, "stock_changed", branch_id=br, entity="product", entity_id=pid,
                          data={"product": pid, "quantity": after, "delta": delta, "source": src, "via": "ledger"})
                except Exception:  # noqa: BLE001 — одна ошибка не останавливает рассылку
                    pass
            if rows:
                cache.set(KEY, last, None)
            if time.monotonic() + every > end:
                break
            time.sleep(every)
