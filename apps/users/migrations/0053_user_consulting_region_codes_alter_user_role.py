# Generated manually for 12-regional-supervisor-rbac

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0052_alter_company_debt_schedule_version'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='consulting_region_codes',
            field=models.JSONField(blank=True, default=list, verbose_name='Коды регионов (консалтинг)'),
        ),
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                blank=True,
                choices=[
                    ('owner', 'Владелец'),
                    ('admin', 'Администратор'),
                    ('rop', 'Руководитель отдела продаж (РОП)'),
                    ('supervisor', 'Руководитель региона (Supervisor)'),
                    ('salesperson', 'Сотрудник / Продавец'),
                    ('manager', 'Менеджер'),
                    ('receptionist', 'Администратор'),
                    ('worker', 'Работник'),
                    ('specialist', 'Специалист'),
                    ('cashier', 'Кассир'),
                ],
                max_length=32,
                null=True,
                verbose_name='Системная роль'
            ),
        ),
    ]
