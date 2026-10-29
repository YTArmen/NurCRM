from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Добавляет статус "pending" ("Ожидает") в SalaryAccrualConsalting.
    Фронтенд отправляет status=pending при выборе фильтра "Ожидает",
    но раньше этот статус отсутствовал -> 400 Bad Request.
    """

    dependencies = [
        ('consalting', '0022_merge_branches'),
    ]

    operations = [
        migrations.AlterField(
            model_name='salaryaccrualconsalting',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Ожидает'),
                    ('accrued', 'Начислено'),
                    ('paid', 'Выплачено'),
                    ('canceled', 'Отменено'),
                ],
                default='accrued',
                max_length=16,
                verbose_name='Статус',
            ),
        ),
    ]
