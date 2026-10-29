from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('consalting', '0043_leadadspend'),
    ]

    operations = [
        migrations.AddField(
            model_name='whatsappmessageconsalting',
            name='provider',
            field=models.CharField(
                choices=[('wazzup', 'Wazzup'), ('greenapi', 'GreenAPI')],
                default='wazzup',
                max_length=20,
                verbose_name='Провайдер отправки'
            ),
        ),
        migrations.AddField(
            model_name='wazzupaccountconsalting',
            name='green_api_enabled',
            field=models.BooleanField(default=True, verbose_name='Включить GREEN-API как резервный канал'),
        ),
        migrations.AddField(
            model_name='wazzupaccountconsalting',
            name='green_api_id_instance',
            field=models.CharField(blank=True, default='', help_text='idInstance из личного кабинета GREEN-API', max_length=255, verbose_name='GREEN-API idInstance'),
        ),
        migrations.AddField(
            model_name='wazzupaccountconsalting',
            name='green_api_media_url',
            field=models.CharField(blank=True, default='https://api.greenapi.com', max_length=255, verbose_name='GREEN-API Media Host URL'),
        ),
        migrations.AddField(
            model_name='wazzupaccountconsalting',
            name='green_api_token_instance',
            field=models.CharField(blank=True, default='', help_text='apiTokenInstance из личного кабинета GREEN-API', max_length=255, verbose_name='GREEN-API apiTokenInstance'),
        ),
        migrations.AddField(
            model_name='wazzupaccountconsalting',
            name='green_api_url',
            field=models.CharField(blank=True, default='https://api.greenapi.com', max_length=255, verbose_name='GREEN-API Host URL'),
        ),
    ]
