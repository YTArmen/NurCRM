from django.db import migrations, models

# ТЗ Н-19: заголовки (в нижнем регистре) -> системный slug.
TITLE_TO_SLUG = {
    "зарплата": "salaries", "зарплаты": "salaries",
    "аренда": "rent",
    "налог": "taxes", "налоги": "taxes", "налог/налоги": "taxes",
    "коммунальные": "utilities", "коммунальные услуги": "utilities",
    "закупка товара": "purchases", "закупки": "purchases", "закупка": "purchases",
}
CODE_TO_SLUG = {"salary": "salaries", "rent": "rent", "taxes": "taxes", "utilities": "utilities", "purchase": "purchases"}


def fill_slugs(apps, schema_editor):
    Cat = apps.get_model("construction", "CashFlowCategory")
    for c in Cat.objects.filter(slug="").iterator():
        slug = CODE_TO_SLUG.get(c.code) or TITLE_TO_SLUG.get((c.title or "").strip().lower())
        if slug:
            Cat.objects.filter(pk=c.pk).update(slug=slug, is_system=True)


class Migration(migrations.Migration):

    dependencies = [
        ("construction", "0035_trashentry_auditlog_branch"),
    ]

    operations = [
        migrations.AddField(
            model_name="cashflowcategory",
            name="slug",
            field=models.CharField(blank=True, db_default="", db_index=True, default="", max_length=32, verbose_name="Системный slug"),
        ),
        migrations.AddField(
            model_name="cashflowcategory",
            name="is_system",
            field=models.BooleanField(db_default=False, default=False, verbose_name="Системная статья"),
        ),
        migrations.AlterField(
            model_name="cashflow",
            name="source_kind",
            field=models.CharField(
                blank=True,
                choices=[('pos_sale', 'POS продажа'), ('pos_prepayment', 'POS предоплата'), ('debt_repayment', 'Погашение долга'), ('supplier_debt_payment', 'Оплата долга поставщику'), ('warehouse_purchase', 'Закупка товара'), ('procurement_receipt', 'Приход поставщика'), ('supplier_return', 'Возврат поставщику'), ('defect_writeoff', 'Списание брака'), ('product_return', 'Возврат товара'), ('shift_drawer_outflow', 'Расход из ящика смены'), ('shift_drawer_inflow', 'Внесение в ящик смены'), ('work_order_prepayment', 'Предоплата заказ-наряда'), ('rental_deposit', 'Залог за прокат'), ('pos_sale_return', 'Возврат продажи'), ('cashflow_cancel', 'Отмена движения'), ('manual', 'Ручная операция'), ('shift_drawer_collection', 'Инкассация из ящика смены'), ('salary_payout', 'Выплата зарплаты'), ('rent_payment', 'Оплата аренды'), ('supplier_payment', 'Оплата поставщику')],
                db_index=True,
                max_length=50,
                null=True,
                verbose_name="Тип источника",
            ),
        ),
        migrations.AlterField(
            model_name="cashflow",
            name="source_id",
            field=models.CharField(blank=True, db_index=True, max_length=128, null=True, verbose_name="ID источника"),
        ),
        migrations.RunPython(fill_slugs, migrations.RunPython.noop),
    ]
