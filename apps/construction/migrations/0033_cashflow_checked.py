import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("construction", "0032_cashflowcategory_type_archived_code"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="cashflow",
            name="checked_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="Проверено"),
        ),
        migrations.AddField(
            model_name="cashflow",
            name="checked_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="checked_cash_flows", to=settings.AUTH_USER_MODEL, verbose_name="Кто проверил",
            ),
        ),
        migrations.AddField(
            model_name="cashflow",
            name="accountant_note",
            field=models.TextField(blank=True, default="", db_default="", verbose_name="Комментарий бухгалтера"),
        ),
    ]
