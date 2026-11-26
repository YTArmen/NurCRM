"""Сторож смены eKassa по расписанию (sg1811): cron раз в 20 минут.
Смена eKassa открыта дольше 23 ч → Z-отчёт и новая смена (если в CRM есть открытые смены),
иначе просто закрыть. См. apps/ekassa/shift_guard.py."""
from django.core.management.base import BaseCommand

from apps.ekassa.models import EkassaIntegration
from apps.ekassa.shift_guard import ensure_fresh_shift, is_guarded


class Command(BaseCommand):
    help = "Переоткрыть смену eKassa, если она открыта дольше 23 часов"

    def handle(self, *args, **opts):
        for cfg in EkassaIntegration.objects.filter(is_enabled=True).select_related("company"):
            if not cfg.is_ready() or not is_guarded(cfg):
                continue
            try:
                action = ensure_fresh_shift(cfg)
            except Exception as e:  # noqa: BLE001
                action = f"error: {e}"
            if action != "ok":
                self.stdout.write(f"{cfg.company_id} {cfg.company.name}: {action}")
