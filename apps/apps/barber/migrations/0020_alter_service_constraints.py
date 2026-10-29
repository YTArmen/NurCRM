from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('barber', '0019_alter_appointment_status'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='service',
            name='uniq_service_name_per_branch',
        ),
        migrations.RemoveConstraint(
            model_name='service',
            name='uniq_service_name_global_per_company',
        ),
        migrations.AddConstraint(
            model_name='service',
            constraint=models.UniqueConstraint(
                condition=models.Q(('branch__isnull', False), ('category__isnull', False)),
                fields=('branch', 'category', 'name'),
                name='uniq_service_name_per_branch_and_cat',
            ),
        ),
        migrations.AddConstraint(
            model_name='service',
            constraint=models.UniqueConstraint(
                condition=models.Q(('branch__isnull', True), ('category__isnull', False)),
                fields=('company', 'category', 'name'),
                name='uniq_service_name_global_per_cat',
            ),
        ),
        migrations.AddConstraint(
            model_name='service',
            constraint=models.UniqueConstraint(
                condition=models.Q(('branch__isnull', False), ('category__isnull', True)),
                fields=('branch', 'name'),
                name='uniq_service_name_per_branch_no_cat',
            ),
        ),
        migrations.AddConstraint(
            model_name='service',
            constraint=models.UniqueConstraint(
                condition=models.Q(('branch__isnull', True), ('category__isnull', True)),
                fields=('company', 'name'),
                name='uniq_service_name_global_no_cat',
            ),
        ),
    ]
