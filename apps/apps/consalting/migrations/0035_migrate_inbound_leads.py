from django.db import migrations


def migrate_inbound_leads_to_lead_consalting(apps, schema_editor):
    LeadConsalting = apps.get_model('consalting', 'LeadConsalting')
    InboundLeadConsalting = apps.get_model('consalting', 'InboundLeadConsalting')
    FunnelConsalting = apps.get_model('consalting', 'FunnelConsalting')
    FunnelStageConsalting = apps.get_model('consalting', 'FunnelStageConsalting')

    # 1. Update existing LeadConsalting records queue_status
    for lead in LeadConsalting.objects.all():
        if not lead.queue_status or lead.queue_status == 'new':
            if lead.status == 'won':
                lead.queue_status = 'converted'
                if not lead.converted_at:
                    lead.converted_at = lead.won_at or lead.closed_at
            elif lead.status == 'lost':
                lead.queue_status = 'rejected'
            elif lead.owner_id:
                lead.queue_status = 'assigned'
            else:
                lead.queue_status = 'new'
            lead.save(update_fields=['queue_status', 'converted_at'])

    # 2. Convert orphaned InboundLeadConsalting to LeadConsalting
    for ib in InboundLeadConsalting.objects.filter(lead__isnull=True):
        company = ib.company
        if not company:
            continue
        funnel = FunnelConsalting.objects.filter(company=company, is_main=True).first() or FunnelConsalting.objects.filter(company=company).first()
        if not funnel:
            funnel = FunnelConsalting.objects.create(company=company, name="Основная воронка", is_main=True)

        stage = funnel.stages.order_by("order").first()
        if not stage:
            stage = FunnelStageConsalting.objects.create(company=company, funnel=funnel, name="Новый", order=1, system_key="intake")

        q_stat = ib.status or 'new'
        lead_stat = 'new'
        if q_stat == 'converted':
            lead_stat = 'won'
        elif q_stat == 'rejected':
            lead_stat = 'lost'
        elif q_stat in ('assigned', 'in_work', 'deferred'):
            lead_stat = 'in_work'

        lead = LeadConsalting.objects.create(
            company=company,
            branch=getattr(ib, 'branch', None),
            funnel=funnel,
            stage=stage,
            owner=ib.owner,
            title=ib.full_name or f"Лид {ib.phone or ib.id}",
            full_name=ib.full_name or "",
            phone=ib.phone or "",
            email=getattr(ib, 'email', ""),
            description=ib.message or "",
            status=lead_stat,
            queue_status=q_stat,
            channel=ib.source or "",
            remind_at=ib.remind_at,
            defer_reason=ib.defer_reason or "",
            defer_comment=ib.defer_comment or "",
            defer_count=ib.defer_count or 0,
            deferred_at=ib.deferred_at,
            reminded_at=ib.reminded_at,
            reject_reason=ib.reject_reason or "",
            reject_comment=ib.reject_comment or "",
            first_reply_at=ib.first_reply_at,
            converted_at=ib.converted_at,
            closed_at=ib.closed_at,
            inbound_external_id=ib.external_id or f"inbound:{ib.id}",
            created_at=ib.created_at,
        )

        ib.lead = lead
        ib.save(update_fields=['lead'])


class Migration(migrations.Migration):

    dependencies = [
        ('consalting', '0034_regional_supervisor_rbac'),
    ]

    operations = [
        migrations.RunPython(
            migrate_inbound_leads_to_lead_consalting,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
