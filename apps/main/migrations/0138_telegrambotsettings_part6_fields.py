from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0137_dealinstallment_promised_date_and_history'),
    ]

    operations = [
        migrations.AddField(
            model_name='telegrambotsettings',
            name='ai_functions_enabled',
            field=models.BooleanField(default=True, verbose_name='Вызов функций ИИ (аналитика)'),
        ),
        migrations.AddField(
            model_name='telegrambotsettings',
            name='daily_report_time',
            field=models.CharField(blank=True, default='21:00', max_length=16, null=True, verbose_name='Время ежедневного отчёта (HH:MM)'),
        ),
        migrations.AddField(
            model_name='telegrambotsettings',
            name='weekly_report',
            field=models.BooleanField(default=True, verbose_name='Еженедельный отчёт (пн 9:00)'),
        ),
        migrations.AddField(
            model_name='telegrambotsettings',
            name='monthly_report',
            field=models.BooleanField(default=True, verbose_name='Ежемесячный отчёт (1-е число)'),
        ),
        migrations.AddField(
            model_name='telegrambotsettings',
            name='ai_daily_limit',
            field=models.PositiveIntegerField(default=200, verbose_name='Дневной лимит запросов к ИИ'),
        ),
    ]
