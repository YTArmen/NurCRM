from django.db import migrations


def backfill(apps, schema_editor):
    """Кто видел товары — явно получает право «менять товары» (никто не теряет доступ)."""
    User = apps.get_model("users", "User")
    User.objects.filter(can_view_products=True, can_edit_products__isnull=True).update(can_edit_products=True)


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0062_user_can_edit_flags"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
