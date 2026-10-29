# Generated manually for 12-regional-supervisor-rbac

from django.db import migrations, models


def backfill_lead_regions(apps, schema_editor):
    LeadConsalting = apps.get_model('consalting', 'LeadConsalting')
    InboundLeadConsalting = apps.get_model('consalting', 'InboundLeadConsalting')
    RegionalFunnelRule = apps.get_model('consalting', 'RegionalFunnelRuleConsalting')

    funnel_to_region = {}
    for rule in RegionalFunnelRule.objects.all():
        if rule.funnel_id and rule.region_code:
            funnel_to_region[rule.funnel_id] = rule.region_code

    for lead in LeadConsalting.objects.filter(region_code=""):
        if lead.funnel_id and lead.funnel_id in funnel_to_region:
            lead.region_code = funnel_to_region[lead.funnel_id]
            lead.save(update_fields=['region_code'])

    for ib in InboundLeadConsalting.objects.filter(region_code=""):
        if ib.lead_id:
            try:
                l = LeadConsalting.objects.filter(id=ib.lead_id).first()
                if l and l.region_code:
                    ib.region_code = l.region_code
                    ib.save(update_fields=['region_code'])
            except Exception:
                pass


class Migration(migrations.Migration):

    dependencies = [
        ('consalting', '0033_leadconsalting_unified_fields'),
        ('users', '0053_user_consulting_region_codes_alter_user_role'),
    ]

    operations = [
        migrations.AddField(
            model_name='leadconsalting',
            name='region_code',
            field=models.CharField(blank=True, db_index=True, max_length=32, verbose_name='Код региона'),
        ),
        migrations.AddField(
            model_name='inboundleadconsalting',
            name='region_code',
            field=models.CharField(blank=True, db_index=True, max_length=32, verbose_name='Код региона'),
        ),
        migrations.AddField(
            model_name='regionalfunnelroutingconsalting',
            name='balance_strategy',
            field=models.CharField(
                choices=[('least_loaded', 'Наименее загруженный'), ('round_robin', 'Round-Robin')],
                default='least_loaded',
                max_length=16,
                verbose_name='Стратегия балансировки'
            ),
        ),
        migrations.AddField(
            model_name='regionalfunnelruleconsalting',
            name='label',
            field=models.CharField(blank=True, max_length=64, verbose_name='Название региона'),
        ),
        migrations.AddField(
            model_name='regionalfunnelruleconsalting',
            name='is_active',
            field=models.BooleanField(default=True, verbose_name='Активно'),
        ),
        migrations.AlterUniqueTogether(
            name='regionalfunnelruleconsalting',
            unique_together={('routing', 'region_code')},
        ),
        migrations.RunPython(
            backfill_lead_regions,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
