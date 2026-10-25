import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """ТЗ Н-16: can_use_pos (db_default=True — никто не теряет кассу) и коды доступа сотрудников (хэши)."""

    dependencies = [
        ("users", "0064_adminpin_companypinsetting"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="can_use_pos",
            field=models.BooleanField(db_default=True, default=True, verbose_name="Работа на кассе (POS)"),
        ),
        migrations.CreateModel(
            name="EmployeeAccessCode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=64)),
                ("code_hash", models.CharField(max_length=256)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="employee_access_codes", to="users.company")),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="access_codes", to=settings.AUTH_USER_MODEL)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "Код доступа сотрудника", "verbose_name_plural": "Коды доступа сотрудников"},
        ),
        migrations.AddIndex(
            model_name="employeeaccesscode",
            index=models.Index(fields=["company", "action"], name="ix_empcode_company_action"),
        ),
        migrations.AddConstraint(
            model_name="employeeaccesscode",
            constraint=models.UniqueConstraint(fields=("employee", "action"), name="uniq_employee_access_code_action"),
        ),
    ]
