from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0065_saleitem_purchase_price_snapshot_and_more"),  # поменяй на свою последнюю
    ]

    operations = [
        migrations.AlterField(
            model_name="saleitem",
            name="purchase_price_snapshot",
            field=models.DecimalField(
                verbose_name="Себестоимость (снапшот)",
                max_digits=12,
                decimal_places=2,
                null=True,
                blank=True,
                default=None,
            ),
        ),
    ]
