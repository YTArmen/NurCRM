from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0045_company_max_discount_percent'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='scale_barcode_amount_unit',
            field=models.CharField(
                choices=[('tiyin', 'Тыйын (03800 = 38.00 сом)'), ('som', 'Сом (00036 = 36 сом)')],
                default='tiyin',
                help_text='Работает только когда поле ШК трактуется как СУММА. «Тыйын» — 03800 = 38.00 сом; «Сом» — 00036 = 36 сом.',
                max_length=10,
                verbose_name='Единица суммы в штрихкоде весов',
            ),
        ),
    ]
