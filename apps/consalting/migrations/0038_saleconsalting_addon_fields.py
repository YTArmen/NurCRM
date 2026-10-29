# Generated manually for 14-client-upsell-sale

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('consalting', '0037_subscriptionconsalting_autorenew_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleconsalting',
            name='deal',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='consalting_direct_sales',
                to='main.clientdeal',
                verbose_name='Сделка'
            ),
        ),
        migrations.AddField(
            model_name='saleconsalting',
            name='kind',
            field=models.CharField(
                choices=[('sale', 'Обычная продажа'), ('subscription', 'Подписка'), ('addon', 'Доп. продажа')],
                db_index=True,
                default='sale',
                max_length=16,
                verbose_name='Тип продажи'
            ),
        ),
        migrations.AddField(
            model_name='saleconsalting',
            name='source',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                max_length=32,
                verbose_name='Источник'
            ),
        ),
        migrations.AddField(
            model_name='saleconsalting',
            name='payment_mode',
            field=models.CharField(
                blank=True,
                default='cash',
                max_length=24,
                verbose_name='Способ оплаты'
            ),
        ),
        migrations.AddField(
            model_name='saleconsalting',
            name='debt_months',
            field=models.PositiveIntegerField(
                default=0,
                verbose_name='Срок рассрочки (мес)'
            ),
        ),
        migrations.AddField(
            model_name='saleconsalting',
            name='idempotency_key',
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=64,
                null=True,
                verbose_name='Ключ идемпотентности'
            ),
        ),
        migrations.AddField(
            model_name='saleitemconsalting',
            name='quantity',
            field=models.PositiveIntegerField(
                default=1,
                verbose_name='Количество'
            ),
        ),
    ]
