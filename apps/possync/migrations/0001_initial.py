"""
ТЗ-27 §4 / ТЗ-28 §3, §8.4, §10.2.

Миграция написана руками: в репозитории нет части миграций apps.main (0116–0122, 0136–0143),
поэтому makemigrations по всему проекту не проходит. Зависимость — последняя миграция main,
которая есть в git. Если на сервере есть более новые миграции main, зависимость менять не нужно.

Индексы раздела 10.2 создаются CONCURRENTLY (без блокировки записи) — поэтому atomic = False.
"""
import uuid
from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


INDEXES = [
    # (имя, таблица, колонки) — CREATE INDEX CONCURRENTLY IF NOT EXISTS
    ("tz28_sale_company_client_status", "main_sale", "(company_id, client_id, status)"),
    ("tz28_sale_company_created_id", "main_sale", "(company_id, created_at DESC, id DESC)"),
    ("tz28_sale_company_shift", "main_sale", "(company_id, shift_id)"),
    ("tz28_product_company_barcode", "main_product", "(company_id, barcode)"),
    ("tz28_product_company_updated", "main_product", "(company_id, updated_at)"),
    ("tz28_product_company_sku", "main_product", "(company_id, code)"),
    ("tz28_product_company_plu", "main_product", "(company_id, plu)"),
]


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        for name, table, cols in INDEXES:
            cur.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{name}" ON "{table}" {cols};')


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        for name, _table, _cols in INDEXES:
            cur.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}";')


class Migration(migrations.Migration):
    atomic = False
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("users", "0001_initial"),
        ("main", "0144_telegram_bot_part15_voice_actions"),
    ]

    operations = [
        migrations.CreateModel(
            name="StockAdjustment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("seq", models.PositiveIntegerField(verbose_name="Порядковый номер по компании")),
                ("kind", models.CharField(choices=[("receive", "Приход"), ("write_off", "Списание"), ("count_fix", "Корректировка (точное количество)"), ("return_to_supplier", "Возврат поставщику")], db_index=True, max_length=24)),
                ("reason", models.CharField(blank=True, default="", max_length=255, verbose_name="Причина")),
                ("source", models.CharField(choices=[("kassa", "Касса"), ("phone", "Телефон"), ("web", "Веб")], default="kassa", max_length=8)),
                ("idempotency_key", models.CharField(blank=True, max_length=128, null=True)),
                ("device_id", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="stock_adjustments", to="users.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="stock_adjustments", to="users.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="stock_adjustments", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Изменение остатка",
                "verbose_name_plural": "Изменения остатка",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="stockadjustment",
            constraint=models.UniqueConstraint(fields=("company", "seq"), name="uniq_stockadj_company_seq"),
        ),
        migrations.AddConstraint(
            model_name="stockadjustment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("idempotency_key__isnull", False)),
                fields=("company", "idempotency_key"),
                name="uniq_stockadj_idempotency",
            ),
        ),
        migrations.CreateModel(
            name="StockAdjustmentLine",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("delta", models.DecimalField(decimal_places=3, max_digits=14)),
                ("before", models.DecimalField(decimal_places=3, default=Decimal("0"), max_digits=14)),
                ("after", models.DecimalField(decimal_places=3, default=Decimal("0"), max_digits=14)),
                ("purchase_price", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("adjustment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="possync.stockadjustment")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="stock_adjustment_lines", to="main.product")),
                ("variant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="stock_adjustment_lines", to="main.productvariant")),
            ],
            options={
                "verbose_name": "Строка изменения остатка",
                "verbose_name_plural": "Строки изменения остатка",
            },
        ),
        migrations.AddIndex(
            model_name="stockadjustmentline",
            index=models.Index(fields=["product"], name="possync_adjline_product"),
        ),
        migrations.RunPython(create_indexes, drop_indexes),
    ]
