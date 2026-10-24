from django.db import migrations, models


class Migration(migrations.Migration):
    # ТЗ Н-26: цвет (#RRGGBB) и порядок варианта, цвет фото товара.
    dependencies = [
        ("main", "0144_telegram_bot_part15_voice_actions"),
    ]

    operations = [
        migrations.AddField(
            model_name="productvariant",
            name="color_hex",
            field=models.CharField(blank=True, db_default="", default="", max_length=7, verbose_name="Цвет (#RRGGBB)"),
        ),
        migrations.AddField(
            model_name="productvariant",
            name="sort_order",
            field=models.IntegerField(db_default=0, default=0, verbose_name="Порядок"),
        ),
        migrations.AddField(
            model_name="productimage",
            name="color",
            field=models.CharField(blank=True, db_default="", default="", max_length=64, verbose_name="Цвет"),
        ),
    ]
