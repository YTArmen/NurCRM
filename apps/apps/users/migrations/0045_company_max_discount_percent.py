from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('users', '0044_alter_company_scale_barcode_layout'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='max_discount_percent',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True, verbose_name='Максимальная скидка (%)'),
        ),
    ]
