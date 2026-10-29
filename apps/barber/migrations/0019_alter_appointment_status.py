from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('barber', '0018_mastersalarypayout_servicesalaryrate_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='appointment',
            name='status',
            field=models.CharField(
                choices=[
                    ('booked', 'Забронировано'),
                    ('confirmed', 'Подтверждено'),
                    ('completed', 'Завершено'),
                    ('canceled', 'Отменено'),
                    ('no_show', 'Не пришёл'),
                    ('deleted', 'Удалено'),
                ],
                db_index=True,
                default='booked',
                max_length=16,
            ),
        ),
    ]
