import uuid
from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    # ТЗ Н-31: лента изменений и запросы «на кассу»
    dependencies = [
        ("main", "0152_tz1023_purchase_orders"),
        ("users", "0064_adminpin_companypinsetting"),
        ("construction", "__first__"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ChangeEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("type", models.CharField(max_length=48, verbose_name="Тип")),
                ("entity", models.CharField(blank=True, default="", max_length=48, verbose_name="Сущность")),
                ("entity_id", models.CharField(blank=True, default="", max_length=64, verbose_name="ID сущности")),
                ("data", models.JSONField(blank=True, default=dict, verbose_name="Данные")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="users.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="change_events", to="users.company")),
            ],
            options={"verbose_name": "Событие изменения", "verbose_name_plural": "Лента изменений"},
        ),
        migrations.AddIndex(
            model_name="changeevent",
            index=models.Index(fields=["company", "id"], name="chgev_company_id_idx"),
        ),
        migrations.CreateModel(
            name="PosRequest",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("debt_payment", "Оплата долга клиента"), ("salary_payout", "Выплата зарплаты"), ("supplier_payment", "Оплата поставщику"), ("rental_payment", "Оплата проката"), ("cash_out", "Изъятие из кассы")], max_length=24)),
                ("status", models.CharField(choices=[("pending", "Ожидает"), ("accepted", "Принят"), ("declined", "Отклонён"), ("canceled", "Отменён"), ("expired", "Истёк")], db_index=True, default="pending", max_length=12)),
                ("amount", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=12)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("comment", models.CharField(blank=True, default="", max_length=255)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="users.branch")),
                ("cashbox", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="construction.cashbox", verbose_name="Касса-адресат")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pos_requests", to="users.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("shift", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="construction.cashshift")),
            ],
            options={"verbose_name": "Запрос на кассу", "verbose_name_plural": "Запросы на кассу", "ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="posrequest",
            index=models.Index(fields=["company", "status", "cashbox"], name="posreq_comp_st_cb_idx"),
        ),
    ]
