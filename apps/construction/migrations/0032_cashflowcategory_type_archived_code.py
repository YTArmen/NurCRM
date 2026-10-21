from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("construction", "0031_alter_cashflow_source_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="cashflowcategory",
            name="type",
            field=models.CharField(
                choices=[("income", "Доход"), ("expense", "Расход"), ("both", "Любой")],
                default="both", db_default="both", max_length=10, verbose_name="Тип статьи",
            ),
        ),
        migrations.AddField(
            model_name="cashflowcategory",
            name="archived",
            field=models.BooleanField(default=False, db_default=False, verbose_name="В архиве"),
        ),
        migrations.AddField(
            model_name="cashflowcategory",
            name="code",
            field=models.CharField(blank=True, default="", db_default="", max_length=32, verbose_name="Код статьи по умолчанию"),
        ),
    ]
