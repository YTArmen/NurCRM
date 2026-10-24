import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    # ТЗ Н-24 (1): лимит запросов ключа и журнал вызовов
    dependencies = [
        ("integrations", "0003_idempotencyrecord_key_255"),
    ]

    operations = [
        migrations.AddField(
            model_name="apikey",
            name="rate_limit_per_minute",
            field=models.PositiveIntegerField(db_default=120, default=120),
        ),
        migrations.CreateModel(
            name="ApiKeyCallLog",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("method", models.CharField(max_length=8)),
                ("path", models.CharField(max_length=255)),
                ("scope", models.CharField(blank=True, default="", max_length=32)),
                ("allowed", models.BooleanField(default=True)),
                ("reason", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("key", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="call_logs", to="integrations.apikey")),
            ],
            options={"ordering": ("-created_at",)},
        ),
    ]
