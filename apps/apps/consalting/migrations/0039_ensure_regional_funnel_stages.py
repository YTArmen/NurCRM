from django.db import migrations


def backfill_funnel_system_stages(apps, schema_editor):
    FunnelConsalting = apps.get_model("consalting", "FunnelConsalting")
    FunnelStageConsalting = apps.get_model("consalting", "FunnelStageConsalting")
    LeadConsalting = apps.get_model("consalting", "LeadConsalting")

    for funnel in FunnelConsalting.objects.all():
        company = funnel.company
        stages = list(funnel.stages.all().order_by("order"))

        # 1. Intake / New Lead stage
        intake_stage = next((s for s in stages if getattr(s, "system_key", "") == "intake" or s.order == 1 or s.stage_type == "new_lead"), None)
        if not intake_stage:
            intake_stage = FunnelStageConsalting.objects.create(
                company=company,
                funnel=funnel,
                name="Новый лид",
                stage_type="new_lead",
                system_key="intake",
                order=1,
                color="#3498db"
            )
            stages.insert(0, intake_stage)

        # 2. In progress stage
        has_progress = any(getattr(s, "system_key", "") == "in_progress" or (s.order > 1 and s.stage_type not in ["won", "lost"]) for s in stages)
        if not has_progress:
            FunnelStageConsalting.objects.create(
                company=company,
                funnel=funnel,
                name="В работе",
                stage_type="in_work",
                system_key="in_progress",
                order=2,
                color="#f39c12"
            )

        # 3. Won / Completed stage
        has_won = any(s.stage_type == "won" for s in stages)
        if not has_won:
            max_order = max([s.order for s in stages] + [2]) + 1
            FunnelStageConsalting.objects.create(
                company=company,
                funnel=funnel,
                name="Успешно завершено",
                stage_type="won",
                system_key="completed",
                order=max_order,
                color="#2ecc71"
            )

        # Backfill orphan leads in this funnel that have stage_id=None
        first_stage = funnel.stages.order_by("order").first()
        if first_stage:
            LeadConsalting.objects.filter(funnel=funnel, stage__isnull=True).update(stage=first_stage)


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("consalting", "0038_saleconsalting_addon_fields"),
    ]

    operations = [
        migrations.RunPython(backfill_funnel_system_stages, reverse_code=reverse_backfill),
    ]
