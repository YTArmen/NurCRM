from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('consalting', '0035_migrate_inbound_leads'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleconsalting',
            name='paid_months',
            field=models.PositiveIntegerField(default=1, verbose_name='Оплачено месяцев вперёд'),
        ),
        migrations.AddField(
            model_name='subscriptionconsalting',
            name='paid_through',
            field=models.DateField(blank=True, null=True, verbose_name='Оплачено по'),
        ),
        migrations.AddField(
            model_name='subscriptionpaymentconsalting',
            name='paid_via',
            field=models.CharField(blank=True, default='', max_length=32, verbose_name='Канал оплаты'),
        ),
        migrations.AddField(
            model_name='cashoperationconsalting',
            name='subscription',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='cash_operations', to='consalting.subscriptionconsalting'),
        ),
        migrations.AddField(
            model_name='cashrequestconsalting',
            name='subscription',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='cash_requests', to='consalting.subscriptionconsalting'),
        ),
        migrations.AddField(
            model_name='cashrequestconsalting',
            name='period_month',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='cashrequestconsalting',
            name='prepaid_count',
            field=models.PositiveIntegerField(default=1),
        ),
    ]
