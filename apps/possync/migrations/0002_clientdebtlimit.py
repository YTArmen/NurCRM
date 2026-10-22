"""Лимит долга клиента (ТЗ веб-кассы, п. 2). Написана руками, схему apps.main не трогает."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("possync", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ClientDebtLimit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("limit", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="debt_limit_row", to="main.client")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="client_debt_limits", to="users.company")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "Лимит долга клиента", "verbose_name_plural": "Лимиты долга клиентов"},
        ),
    ]
