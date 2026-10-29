import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    # ТЗ Н-23: код владельца, журнал notify-owner, owner_user_id
    dependencies = [
        ("main", "0149_tz1023_rentals_docs"),
        ("users", "0064_adminpin_companypinsetting"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="telegrambotsettings",
            name="owner_user_id",
            field=models.CharField(blank=True, default="", db_default="", max_length=64, verbose_name="Telegram from.id владельца"),
        ),
        migrations.CreateModel(
            name="TelegramOwnerNotification",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("dedupe_key", models.CharField(blank=True, default="", max_length=128, verbose_name="Ключ идемпотентности")),
                ("text", models.TextField(blank=True, default="", verbose_name="Текст")),
                ("ok", models.BooleanField(default=False, verbose_name="Отправлено")),
                ("error", models.CharField(blank=True, default="", max_length=255, verbose_name="Ошибка")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="telegram_owner_notifications", to="users.company")),
            ],
            options={"db_table": "telegram_owner_notifications"},
        ),
        migrations.AddIndex(
            model_name="telegramownernotification",
            index=models.Index(fields=["company", "dedupe_key", "-created_at"], name="main_tgown_notif_idx"),
        ),
        migrations.CreateModel(
            name="TelegramOwnerCode",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code_hash", models.CharField(max_length=64)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="telegram_owner_codes", to="users.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "telegram_owner_codes"},
        ),
        migrations.AddIndex(
            model_name="telegramownercode",
            index=models.Index(fields=["company", "-created_at"], name="main_tgown_code_idx"),
        ),
    ]
