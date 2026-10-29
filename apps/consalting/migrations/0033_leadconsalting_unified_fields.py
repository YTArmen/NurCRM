from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('consalting', '0032_regionalfunnelroutingconsalting_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='leadconsalting',
            name='channel',
            field=models.CharField(blank=True, max_length=32, verbose_name='Канал поступления'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='converted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Дата конвертации'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='defer_comment',
            field=models.TextField(blank=True, verbose_name='Комментарий к откладыванию'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='defer_count',
            field=models.PositiveIntegerField(default=0, verbose_name='Количество откладываний'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='defer_reason',
            field=models.CharField(blank=True, max_length=32, verbose_name='Причина откладывания'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='deferred_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Дата откладывания'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='first_reply_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Первый ответ менеджера'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='inbound_external_id',
            field=models.CharField(blank=True, db_index=True, max_length=128, verbose_name='Внешний ID входящего лида'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='queue_status',
            field=models.CharField(choices=[('new', 'Новый'), ('assigned', 'Назначен'), ('in_work', 'В работе'), ('deferred', 'Отложен'), ('converted', 'Конвертирован (Сделка)'), ('rejected', 'Отказ')], db_index=True, default='new', max_length=16, verbose_name='Статус очереди'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='reject_comment',
            field=models.TextField(blank=True, verbose_name='Комментарий к отказу'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='reject_reason',
            field=models.CharField(blank=True, max_length=32, verbose_name='Причина отказа'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='remind_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True, verbose_name='Напомнить в'),
        ),
        migrations.AddField(
            model_name='leadconsalting',
            name='reminded_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Дата напоминания'),
        ),
    ]

