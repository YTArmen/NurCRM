"""Периодические задачи аренды (celery beat, см. CELERY_BEAT_SCHEDULE в core/settings.py).

Все задачи идемпотентны: ежедневный запуск безопасен, повтор на ту же дату ничего не дублирует."""
import logging
from datetime import date

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


def _on_date(on_date):
    return date.fromisoformat(on_date) if isinstance(on_date, str) and on_date else timezone.localdate()


@shared_task(name="apps.arenda.tasks.monthly_charges")
def monthly_charges(on_date: str = ""):
    """Начисления аренды за месяц по всем действующим договорам + налоговый календарь."""
    from .services.charges import generate_monthly_charges

    d = _on_date(on_date)
    res = generate_monthly_charges(d)
    res["tax_calendar"] = fill_tax_calendar(d)
    logger.info("arenda monthly_charges: %s", res)
    return res


def fill_tax_calendar(d: date) -> dict:
    """Заполняет налоговый календарь (удержания и взносы с зарплаты) на текущий и следующий месяц."""
    from .models import ArendaSettings
    from .services.expenses import add_months, month_start, sync_payroll_taxes

    months = [month_start(d), add_months(month_start(d), 1)]
    done = failed = 0
    for s in ArendaSettings.objects.select_related("company"):
        for m in months:
            try:
                sync_payroll_taxes(s.company, m)
                done += 1
            except Exception:  # сбой одной компании не должен ронять остальные
                failed += 1
                logger.exception("arenda tax calendar failed: company=%s month=%s", s.company_id, m)
    return {"done": done, "failed": failed}


@shared_task(name="apps.arenda.tasks.auto_reminders")
def auto_reminders(on_date: str = ""):
    """Автонапоминания и автопеня по включённым правилам всех компаний."""
    from .models import ArendaSettings
    from .services.reminders import run_auto_reminders

    d = _on_date(on_date)
    total, failed = {}, 0
    for s in ArendaSettings.objects.select_related("company"):
        try:
            for k, v in run_auto_reminders(s.company, d).items():
                total[k] = total.get(k, 0) + v
        except Exception:
            failed += 1
            logger.exception("arenda auto_reminders failed: company=%s", s.company_id)
    total["failed_companies"] = failed
    logger.info("arenda auto_reminders %s: %s", d, total)
    return total
