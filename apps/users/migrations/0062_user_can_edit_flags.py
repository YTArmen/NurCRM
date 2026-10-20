from django.db import migrations, models

FIELDS = [
    ("can_edit_cashbox", "Менять: кассы"),
    ("can_edit_products", "Менять: товары"),
    ("can_edit_procurement", "Менять: закупки"),
    ("can_edit_documents", "Менять: документы склада"),
    ("can_edit_clients", "Менять: клиенты и долги"),
    ("can_edit_salary", "Менять: зарплата"),
    ("can_edit_employees", "Менять: сотрудники"),
    ("can_edit_settings", "Менять: настройки"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0061_user_can_cash_negative_user_can_change_document_date_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name=name,
            field=models.BooleanField(blank=True, default=None, null=True, verbose_name=label),
        )
        for name, label in FIELDS
    ]
