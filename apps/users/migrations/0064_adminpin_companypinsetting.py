import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0063_can_edit_products_backfill"),
    ]

    operations = [
        migrations.CreateModel(
            name="AdminPin",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("pin_hash", models.CharField(max_length=256)),
                ("failed_attempts", models.PositiveSmallIntegerField(db_default=0, default=0)),
                ("locked_until", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="admin_pin", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "PIN администратора", "verbose_name_plural": "PIN администраторов"},
        ),
        migrations.CreateModel(
            name="CompanyPinSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("require_pin", models.BooleanField(db_default=False, default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="pin_setting", to="users.company")),
            ],
            options={"verbose_name": "Настройка PIN компании", "verbose_name_plural": "Настройки PIN компаний"},
        ),
    ]
