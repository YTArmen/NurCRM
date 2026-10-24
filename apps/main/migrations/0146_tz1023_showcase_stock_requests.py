import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    # ТЗ Н-27: заявки «сообщить о поступлении».
    dependencies = [
        ("main", "0145_tz1023_showcase_variant_color"),
        ("users", "0058_company_market_spheres"),
    ]

    operations = [
        migrations.CreateModel(
            name="ShowcaseStockRequest",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("phone", models.CharField(blank=True, db_default="", default="", max_length=64, verbose_name="Телефон")),
                ("telegram_chat_id", models.CharField(blank=True, db_default="", default="", max_length=64, verbose_name="Telegram chat id")),
                ("client_ip", models.CharField(blank=True, db_default="", default="", max_length=64, verbose_name="IP")),
                ("status", models.CharField(
                    choices=[("open", "Ждёт поступления"), ("ready", "Поступило (покупателю нужно сообщить)"),
                             ("notified", "Покупателю сообщили"), ("canceled", "Отменена")],
                    db_default="open", db_index=True, default="open", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("notified_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="showcase_stock_requests", to="users.company")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="main.product")),
                ("variant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="+", to="main.productvariant")),
            ],
            options={
                "verbose_name": "Заявка «сообщить о поступлении»",
                "verbose_name_plural": "Заявки «сообщить о поступлении»",
                "ordering": ("-created_at",),
                "indexes": [models.Index(fields=["company", "status"], name="idx_sc_stockreq_co_status")],
            },
        ),
    ]
