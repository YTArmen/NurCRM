from django.db import migrations


def seed(apps, schema_editor):
    from apps.support.rules import seed_default_rules

    seed_default_rules(apps.get_model("support", "SupportAlertRule"))


class Migration(migrations.Migration):

    dependencies = [
        ("support", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
