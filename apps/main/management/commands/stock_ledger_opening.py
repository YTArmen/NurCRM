"""П0-4 К-1: стартовые строки журнала (kind=opening) — остаток каждого товара на момент включения журнала.

Остаток на старте = qty_before первой строки журнала товара, а если строк нет — текущий остаток.
Повторный запуск безопасен (товары со строкой opening пропускаются). Пакетами по --batch.
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction

SQL = """
INSERT INTO main_stockledger (company_id, branch_id, product_id, qty_before, qty_after, qty_delta, kind,
                              source, unit_cost, occurred_at)
SELECT p.company_id, p.branch_id, p.id, 0, s.q, s.q, 'opening', 'opening', p.purchase_price, %(since)s
FROM (
  SELECT p.id, coalesce((SELECT l.qty_before FROM main_stockledger l
                         WHERE l.product_id = p.id AND l.kind <> 'opening' ORDER BY l.id LIMIT 1),
                        p.quantity, 0) AS q
  FROM main_product p
  WHERE p.id > %(after)s
    AND NOT EXISTS (SELECT 1 FROM main_stockledger o WHERE o.product_id = p.id AND o.kind = 'opening')
  ORDER BY p.id LIMIT %(batch)s
) s JOIN main_product p ON p.id = s.id
WHERE s.q <> 0
RETURNING product_id
"""


class Command(BaseCommand):
    help = "Стартовые остатки в журнал main_stockledger (повтор безопасен)."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=5000)

    def handle(self, *args, batch, **opts):
        with connection.cursor() as c:
            c.execute("SELECT coalesce(min(created_at), now()) FROM main_stockledger WHERE kind <> 'opening'")
            since = c.fetchone()[0]
            c.execute("SELECT id FROM main_product ORDER BY id")
            ids = [r[0] for r in c.fetchall()]
        total, after = 0, "00000000-0000-0000-0000-000000000000"
        for i in range(0, len(ids), batch):
            chunk_last = ids[min(i + batch, len(ids)) - 1]
            with transaction.atomic(), connection.cursor() as c:
                c.execute(SQL, {"since": since, "after": after, "batch": batch})
                total += len(c.fetchall())
            after = str(chunk_last)
        self.stdout.write(f"opening: добавлено {total} строк, товаров {len(ids)}, начало журнала {since.isoformat()}")
