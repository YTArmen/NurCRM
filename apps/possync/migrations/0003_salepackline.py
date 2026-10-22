"""Строки чеков, проданные упаковкой (ТЗ веб-кассы, п. 3). Написана руками, apps.main не тронут."""
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("possync", "0002_clientdebtlimit")]

    operations = [
        migrations.CreateModel(
            name="SalePackLine",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("package_name", models.CharField(blank=True, default="", max_length=64)),
                ("packs", models.DecimalField(decimal_places=3, max_digits=14, verbose_name="Упаковок")),
                ("units_per_pack", models.DecimalField(decimal_places=3, max_digits=10, verbose_name="Базовых единиц в упаковке")),
                ("pack_price", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Цена упаковки")),
                ("base_qty", models.DecimalField(decimal_places=3, max_digits=14, verbose_name="Базовых единиц")),
                ("package", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="main.productpackage")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="main.product")),
                ("sale", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pack_lines", to="main.sale")),
                ("variant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="main.productvariant")),
            ],
            options={"verbose_name": "Строка чека (упаковка)", "verbose_name_plural": "Строки чеков (упаковки)"},
        ),
        migrations.AddIndex(
            model_name="salepackline",
            index=models.Index(fields=["sale", "product"], name="possync_packline_sale_prod"),
        ),
    ]
