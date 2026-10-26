from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    # Последняя миграция main, которая есть в git; на сервере main новее — менять не нужно.
    dependencies = [
        ("main", "0135_sale_card_amount_sale_cash_amount_sale_debt_initial_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="GlobalProductPhoto",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source", models.CharField(choices=[("store", "Фото товара из магазина"), ("off", "Open Food Facts")], max_length=8, verbose_name="Источник")),
                ("image_name", models.CharField(blank=True, default="", max_length=500, verbose_name="Файл (путь в media)")),
                ("url", models.URLField(blank=True, default="", max_length=500, verbose_name="Ссылка на фото")),
                ("source_product_id", models.UUIDField(blank=True, null=True, verbose_name="Товар-источник")),
                ("checked_at", models.DateTimeField(auto_now=True, db_index=True, verbose_name="Проверено")),
                ("product", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="photo", to="main.globalproduct", verbose_name="Глобальный товар")),
            ],
            options={
                "verbose_name": "Фото глобального товара",
                "verbose_name_plural": "Фото глобальных товаров",
            },
        ),
    ]
