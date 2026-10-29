# Generated for Task #24

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('consalting', '0045_merge_consalting_leaves'),
    ]

    operations = [
        migrations.AddField(
            model_name='leadconsalting',
            name='address',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='Адрес'),
        ),
    ]
