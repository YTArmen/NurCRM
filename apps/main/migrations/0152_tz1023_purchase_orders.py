import uuid
from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    # ТЗ Н-29: заказ поставщику как документ
    dependencies = [
        ("main", "0151_tz1023_bot_voice_calls"),
        ("users", "0064_adminpin_companypinsetting"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseOrder",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("number", models.PositiveIntegerField(verbose_name="Номер (по компании)")),
                ("status", models.CharField(choices=[("draft", "Черновик"), ("sent", "Отправлен"), ("partial", "Принят частично"), ("received", "Принят полностью"), ("cancelled", "Отменён")], db_index=True, default="draft", max_length=16)),
                ("note", models.TextField(blank=True, default="")),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="purchase_orders", to="users.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="purchase_orders", to="users.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_purchase_orders", to=settings.AUTH_USER_MODEL)),
                ("supplier", models.ForeignKey(limit_choices_to=models.Q(("type", "suppliers")), on_delete=django.db.models.deletion.CASCADE, related_name="purchase_orders", to="main.client")),
            ],
            options={
                "verbose_name": "Заказ поставщику",
                "verbose_name_plural": "Заказы поставщикам",
                "ordering": ["-created_at", "-number"],
            },
        ),
        migrations.CreateModel(
            name="PurchaseOrderLine",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("qty", models.DecimalField(decimal_places=3, max_digits=12)),
                ("purchase_price", models.DecimalField(blank=True, decimal_places=3, max_digits=11, null=True)),
                ("received_qty", models.DecimalField(decimal_places=3, default=Decimal("0"), max_digits=12)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="main.purchaseorder")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="purchase_order_lines", to="main.product")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.AddConstraint(
            model_name="purchaseorder",
            constraint=models.UniqueConstraint(fields=("company", "number"), name="uniq_purchase_order_number_company"),
        ),
        migrations.AddIndex(
            model_name="purchaseorder",
            index=models.Index(fields=["company", "supplier", "status"], name="main_purcha_company_5bd0c5_idx"),
        ),
        migrations.AddField(
            model_name="supplierreceipt",
            name="order",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="receipts", to="main.purchaseorder"),
        ),
    ]
