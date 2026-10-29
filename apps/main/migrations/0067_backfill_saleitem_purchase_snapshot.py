from decimal import Decimal
from django.db import migrations
from django.db.models import Q

def forwards(apps, schema_editor):
    SaleItem = apps.get_model("main", "SaleItem")
    Product = apps.get_model("main", "Product")

    si_table = SaleItem._meta.db_table
    p_table = Product._meta.db_table

    # snapshot заполняем только если:
    # - sale_item.product_id NOT NULL
    # - snapshot IS NULL OR snapshot = 0
    # - product.purchase_price > 0 (чтобы не затирать реальные "0" если так задумано)
    vendor = schema_editor.connection.vendor

    if vendor == "postgresql":
        schema_editor.execute(f"""
            UPDATE {si_table} si
            SET purchase_price_snapshot = p.purchase_price
            FROM {p_table} p
            WHERE si.product_id = p.id
              AND (si.purchase_price_snapshot IS NULL OR si.purchase_price_snapshot = 0)
              AND p.purchase_price > 0;
        """)
        return

    # универсальный fallback (медленнее, но работает везде):
    # достаём пары product_id -> purchase_price и обновляем батчами
    price_map = dict(
        Product.objects.filter(purchase_price__gt=0).values_list("id", "purchase_price")
    )

    BATCH = 2000
    to_update = []
    qs = SaleItem.objects.filter(product_id__isnull=False).filter(
        Q(purchase_price_snapshot__isnull=True) | Q(purchase_price_snapshot=Decimal("0.00"))
    )

    for si in qs.iterator(chunk_size=BATCH):
        pp = price_map.get(si.product_id)
        if pp is None:
            continue
        si.purchase_price_snapshot = pp
        to_update.append(si)
        if len(to_update) >= BATCH:
            SaleItem.objects.bulk_update(to_update, ["purchase_price_snapshot"], batch_size=BATCH)
            to_update = []

    if to_update:
        SaleItem.objects.bulk_update(to_update, ["purchase_price_snapshot"], batch_size=BATCH)


def backwards(apps, schema_editor):
    # Откат не делаем, чтобы не потерять корректные данные
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0066_alter_saleitem_purchase_price_snapshot"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
