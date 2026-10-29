# Generated manually for cashflow_requests_enabled and can_view_leads_inbox
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0055_user_can_manage_lead_ad_spend'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='cashflow_requests_enabled',
            field=models.BooleanField(
                default=False,
                help_text='True — ручные записи не-владельца, компенсация возврата и правка/отмена одобренного движения создают pending-заявку. False — проводятся немедленно.',
                verbose_name='Запросы на подтверждение движений кассы',
            ),
        ),
        migrations.AddField(
            model_name='user',
            name='can_view_leads_inbox',
            field=models.BooleanField(
                blank=True,
                default=False,
                null=True,
                verbose_name='Консалтинг: доступ к входящим лидам',
            ),
        ),
    ]
