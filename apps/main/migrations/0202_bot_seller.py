# 26.11.2026 (bot2611): бот-продавец Ф1 — настройки seller_config и журнал передач продавцу.
import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0201_product_history"),
    ]

    operations = [
        migrations.AddField(
            model_name="telegrambotsettings",
            name="seller_config",
            field=models.JSONField(blank=True, default=dict, verbose_name="Настройки бота-продавца"),
        ),
        migrations.CreateModel(
            name="TelegramHandoff",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("chat_id", models.CharField(db_index=True, max_length=64)),
                ("customer_name", models.CharField(blank=True, default="", max_length=255)),
                ("lang", models.CharField(default="ru", max_length=2)),
                ("reason", models.CharField(default="human", max_length=16)),
                ("question", models.TextField(blank=True, default="")),
                ("status", models.CharField(choices=[("waiting", "Ждёт продавца"), ("taken", "У продавца"), ("returned", "Возвращён боту"), ("expired", "Вернулся к боту по тишине"), ("callback", "Перезвонить (вне часов)"), ("logged", "Только в отчёт")], db_index=True, default="waiting", max_length=10)),
                ("conversation_id", models.CharField(blank=True, default="", max_length=64)),
                ("card_chat_id", models.CharField(blank=True, default="", max_length=64)),
                ("card_message_id", models.BigIntegerField(blank=True, null=True)),
                ("phone", models.CharField(blank=True, default="", max_length=32)),
                ("note", models.TextField(blank=True, default="")),
                ("until", models.DateTimeField(blank=True, null=True)),
                ("taken_at", models.DateTimeField(blank=True, null=True)),
                ("taken_by", models.CharField(blank=True, default="", max_length=255)),
                ("first_answer_at", models.DateTimeField(blank=True, null=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="telegram_handoffs", to="users.company")),
                ("order", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="main.showcaseorder")),
            ],
            options={
                "db_table": "telegram_handoffs",
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["company", "chat_id", "-created_at"], name="tg_handoff_chat_idx")],
            },
        ),
    ]
