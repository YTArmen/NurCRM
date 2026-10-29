import uuid
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    # ТЗ Н-24 (5): журнал звонков голосового агента
    dependencies = [
        ("main", "0150_tz1023_bot_owner_code"),
        ("users", "0064_adminpin_companypinsetting"),
    ]

    operations = [
        migrations.CreateModel(
            name="VoiceCall",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("phone", models.CharField(blank=True, default="", max_length=32, verbose_name="Номер звонящего")),
                ("duration_seconds", models.PositiveIntegerField(default=0)),
                ("language", models.CharField(blank=True, default="", max_length=8)),
                ("outcome", models.CharField(blank=True, db_index=True, default="", max_length=32, verbose_name="Итог")),
                ("transcript", models.TextField(blank=True, default="")),
                ("recording_url", models.URLField(blank=True, default="", max_length=500)),
                ("cost", models.DecimalField(decimal_places=4, default=Decimal("0"), max_digits=10)),
                ("callback_needed", models.BooleanField(db_index=True, default=False, verbose_name="Нужно перезвонить")),
                ("callback_done", models.BooleanField(default=False)),
                ("idempotency_key", models.CharField(blank=True, max_length=128, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("client", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="main.client")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="voice_calls", to="users.company")),
            ],
            options={"db_table": "voice_calls", "ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="voicecall",
            index=models.Index(fields=["company", "-created_at"], name="main_voicecall_co_idx"),
        ),
        migrations.AddConstraint(
            model_name="voicecall",
            constraint=models.UniqueConstraint(
                condition=models.Q(("idempotency_key__isnull", False)),
                fields=("company", "idempotency_key"), name="main_voicecall_idem_uniq",
            ),
        ),
    ]
