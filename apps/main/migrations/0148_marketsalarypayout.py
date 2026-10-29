import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    # ТЗ Н-18: выплаты зарплаты магазина.

    dependencies = [
        ("main", "0147_tz26_showcase_order_telegram"),
        ("construction", "0031_alter_cashflow_source_kind"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MarketSalaryPayout",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("salary", "Зарплата"), ("advance", "Аванс"), ("bonus", "Премия"), ("deduction", "Удержание")], db_default="salary", default="salary", max_length=16, verbose_name="Вид")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Сумма")),
                ("period_from", models.DateField(blank=True, null=True, verbose_name="Период с")),
                ("period_to", models.DateField(blank=True, null=True, verbose_name="Период по")),
                ("method", models.CharField(blank=True, db_default="", default="", max_length=32, verbose_name="Способ")),
                ("comment", models.CharField(blank=True, db_default="", default="", max_length=255, verbose_name="Комментарий")),
                ("reversed_at", models.DateTimeField(blank=True, null=True, verbose_name="Отменена")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("cashbox", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="market_salary_payouts", to="construction.cashbox", verbose_name="Касса")),
                ("cashflow", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="construction.cashflow", verbose_name="Расход")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="market_salary_payouts", to="users.company", verbose_name="Компания")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="market_salary_payouts_created", to=settings.AUTH_USER_MODEL, verbose_name="Кто выплатил")),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="market_salary_payouts", to=settings.AUTH_USER_MODEL, verbose_name="Сотрудник")),
                ("reversal_cashflow", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="construction.cashflow", verbose_name="Обратное движение")),
                ("shift", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="market_salary_payouts", to="construction.cashshift", verbose_name="Смена")),
            ],
            options={
                "verbose_name": "Выплата зарплаты (магазин)",
                "verbose_name_plural": "Выплаты зарплаты (магазин)",
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["company", "employee", "created_at"], name="main_market_company_6b1f0e_idx")],
            },
        ),
    ]
