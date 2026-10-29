from django.db import migrations, models


def populate_client_sectors(apps, schema_editor):
    Client = apps.get_model("main", "Client")
    
    # 1. Clients with consulting services / subscriptions / leads / deals
    consalting_client_ids = set()
    
    consalting_client_ids.update(
        Client.objects.filter(service__isnull=False).values_list("id", flat=True)
    )
    
    try:
        SubscriptionConsalting = apps.get_model("consalting", "SubscriptionConsalting")
        consalting_client_ids.update(
            SubscriptionConsalting.objects.filter(client__isnull=False).values_list("client_id", flat=True)
        )
    except Exception:
        pass
        
    try:
        SaleConsalting = apps.get_model("consalting", "SaleConsalting")
        consalting_client_ids.update(
            SaleConsalting.objects.filter(client__isnull=False).values_list("client_id", flat=True)
        )
    except Exception:
        pass

    try:
        LeadConsalting = apps.get_model("consalting", "LeadConsalting")
        consalting_client_ids.update(
            LeadConsalting.objects.filter(client__isnull=False).values_list("client_id", flat=True)
        )
    except Exception:
        pass

    # 2. Clients with market sales
    market_client_ids = set()
    try:
        Sale = apps.get_model("main", "Sale")
        market_client_ids.update(
            Sale.objects.filter(client__isnull=False).values_list("client_id", flat=True)
        )
    except Exception:
        pass

    # Batch update
    for c in Client.objects.all():
        has_c = c.id in consalting_client_ids
        has_m = c.id in market_client_ids
        if has_c and has_m:
            c.sector = "all"
        elif has_c:
            c.sector = "consalting"
        elif has_m:
            c.sector = "market"
        else:
            c.sector = "all"
        c.save(update_fields=["sector"])


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0119_debt_dealpayment_payment_method'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='sector',
            field=models.CharField(
                blank=True, choices=[('all', 'Общий'), ('market', 'Маркет'), ('consalting', 'Консалтинг')],
                db_index=True, default='all', max_length=16, null=True, verbose_name='Сектор'
            ),
        ),
        migrations.RunPython(populate_client_sectors, reverse_code=migrations.RunPython.noop),
    ]
