import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("construction", "0033_cashflow_checked"),
        ("users", "0062_user_can_edit_flags"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("action", models.CharField(max_length=32)),
                ("object_type", models.CharField(max_length=32)),
                ("object_id", models.CharField(blank=True, default="", max_length=64)),
                ("object_name", models.CharField(blank=True, default="", max_length=255)),
                ("before", models.JSONField(blank=True, null=True)),
                ("after", models.JSONField(blank=True, null=True)),
                ("ip", models.GenericIPAddressField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="audit_logs", to="users.company")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(model_name="auditlog", index=models.Index(fields=["company", "object_type", "object_id", "-created_at"], name="ix_audit_obj")),
        migrations.AddIndex(model_name="auditlog", index=models.Index(fields=["company", "-created_at"], name="ix_audit_company_date")),
    ]
