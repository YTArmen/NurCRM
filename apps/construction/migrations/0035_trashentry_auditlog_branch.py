import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("construction", "0034_auditlog"),
        ("users", "0064_adminpin_companypinsetting"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="branch",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="users.branch"),
        ),
        migrations.CreateModel(
            name="TrashEntry",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("type", models.CharField(max_length=16)),
                ("object_id", models.CharField(max_length=64)),
                ("object_name", models.CharField(blank=True, default="", max_length=255)),
                ("payload", models.JSONField(default=dict)),
                ("deleted_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="trash_entries", to="users.company")),
                ("deleted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-deleted_at"]},
        ),
        migrations.AddIndex(model_name="trashentry", index=models.Index(fields=["company", "type", "-deleted_at"], name="ix_trash_company_type")),
        migrations.AddIndex(model_name="trashentry", index=models.Index(fields=["company", "type", "object_id"], name="ix_trash_obj")),
    ]
