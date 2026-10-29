from decimal import Decimal
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0104_notification_category"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="sale",
            name="consultant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="consultant_sales",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Консультант",
            ),
        ),
        migrations.AddField(
            model_name="sale",
            name="consultant_commission_enabled",
            field=models.BooleanField(default=False, verbose_name="Начислять процент консультанту"),
        ),
        migrations.AddField(
            model_name="sale",
            name="consultant_commission_percent",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=5, null=True, verbose_name="Процент комиссии консультанта"
            ),
        ),
        migrations.AddField(
            model_name="sale",
            name="consultant_commission_amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=12,
                null=True,
                verbose_name="Сумма комиссии консультанта",
            ),
        ),
        migrations.AddIndex(
            model_name="sale",
            index=models.Index(fields=["company", "consultant", "paid_at"], name="main_sale_company_001_idx"),
        ),
    ]
