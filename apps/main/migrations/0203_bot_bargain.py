# 27.11.2026 (bot2711): бот-продавец Ф2 — правила торга товара и журнал оферт.
import uuid
from decimal import Decimal
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0202_bot_seller"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductBargain",
            fields=[
                ("product", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name="bargain_rule", serialize=False, to="main.product")),
                ("allowed", models.BooleanField(blank=True, null=True, verbose_name="Торг разрешён")),
                ("min_price", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="Минимальная цена для торга")),
                ("max_discount_percent", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True, verbose_name="Макс. скидка, %")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="users.company")),
            ],
            options={"db_table": "telegram_product_bargain"},
        ),
        migrations.CreateModel(
            name="TelegramBargainOffer",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("chat_id", models.CharField(max_length=64)),
                ("qty", models.DecimalField(decimal_places=3, default=Decimal("1"), max_digits=12)),
                ("list_price", models.DecimalField(decimal_places=2, max_digits=14)),
                ("client_offer", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("decision", models.CharField(max_length=12)),
                ("price", models.DecimalField(decimal_places=2, max_digits=14)),
                ("step", models.PositiveSmallIntegerField(default=0)),
                ("floor", models.DecimalField(decimal_places=2, max_digits=14)),
                ("limit_percent", models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=5)),
                ("token_hash", models.CharField(db_index=True, max_length=64)),
                ("token", models.TextField()),
                ("expires_at", models.DateTimeField()),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="users.company")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="main.product")),
                ("variant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="main.productvariant")),
                ("used_order", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="main.showcaseorder")),
            ],
            options={
                "db_table": "telegram_bargain_offers",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["company", "chat_id", "product", "-created_at"], name="tg_bargain_chat_idx"),
                    models.Index(fields=["used_order"], name="tg_bargain_order_idx"),
                ],
            },
        ),
    ]
