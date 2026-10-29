from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("warehouse", "0037_alter_document_date_alter_moneydocument_date_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="warehouseproduct",
            name="minimum_quantity",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                default=Decimal("0.000"),
                help_text="Порог для алерта «мало на складе».",
                max_digits=12,
                null=True,
                verbose_name="Минимальный остаток",
            ),
        ),
        migrations.AddField(
            model_name="warehouseproduct",
            name="supplier",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to=Q(("type__in", ["SUPPLIER", "BOTH"])),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="supplied_products",
                to="warehouse.counterparty",
                verbose_name="Поставщик",
            ),
        ),
    ]
