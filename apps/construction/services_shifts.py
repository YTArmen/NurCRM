"""
Обслуживание кассовых смен: системное автозакрытие.

Используется:
- при удалении сотрудника (закрытие в той же транзакции — ошибка откатывает удаление);
- management-командой `close_stale_shifts` (ретро-закрытие смен удалённых кассиров / зависших);
- периодической celery-задачей (смена открыта дольше суток → закрыть).
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import CashShift

logger = logging.getLogger(__name__)

# Причины автозакрытия (пишутся в CashShift.close_reason).
REASON_EMPLOYEE_DELETED = "employee_deleted"
REASON_TIMEOUT_24H = "auto_timeout_24h"

DEFAULT_MAX_AGE_HOURS = 24

# ТЗ Н-09: настройка автозакрытия лежит в общих настройках компании (CompanyAppSettings,
# без миграции): main/app-settings/nurmarket-mobile/ → ключ ShiftAutoClose
# {"enabled": true|false, "hours": 1..168}. Нет ключа — как раньше (включено, 24 ч).
AUTOCLOSE_SETTINGS_APP = "nurmarket-mobile"
AUTOCLOSE_SETTINGS_KEY = "ShiftAutoClose"
MAX_AUTOCLOSE_HOURS = 168


def _parse_autoclose(val):
    """(enabled, hours) из значения ключа; мусор → значения по умолчанию."""
    if not isinstance(val, dict):
        return True, DEFAULT_MAX_AGE_HOURS
    enabled = val.get("enabled", True)
    enabled = enabled if isinstance(enabled, bool) else True
    try:
        hours = int(val.get("hours", DEFAULT_MAX_AGE_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_MAX_AGE_HOURS
    if not 1 <= hours <= MAX_AUTOCLOSE_HOURS:
        hours = DEFAULT_MAX_AGE_HOURS
    return enabled, hours


def autoclose_config(company_id) -> tuple:
    """Настройка автозакрытия компании: (включено, часов)."""
    from apps.users.models import CompanyAppSettings

    row = CompanyAppSettings.objects.filter(
        company_id=company_id, branch__isnull=True, app=AUTOCLOSE_SETTINGS_APP
    ).first()
    return _parse_autoclose((row.settings or {}).get(AUTOCLOSE_SETTINGS_KEY) if row else None)


def _ceil_hour(dt):
    """Задача идёт ежечасно в :00 — смена закроется в ближайший целый час не раньше dt."""
    floor = dt.replace(minute=0, second=0, microsecond=0)
    return floor if floor == dt else floor + timedelta(hours=1)


def shift_auto_close_at(shift: CashShift):
    """ТЗ Н-09: когда сервер закроет открытую смену; None — смена закрыта или автозакрытие выключено."""
    if shift.status != CashShift.Status.OPEN or not shift.opened_at:
        return None
    enabled, hours = autoclose_config(shift.company_id)
    if not enabled:
        return None
    return _ceil_hour(shift.opened_at + timedelta(hours=hours))


def close_shift_as_system(shift: CashShift, reason: str, *, by_user=None) -> CashShift:
    """
    Закрыть одну открытую смену «по ожидаемому остатку» (расхождение по кассе = 0),
    штатным образом (итоги/время/статус). Ошибки НЕ подавляются — пусть решает вызывающий.
    """
    expected_cash = shift.calc_live_totals(refresh=True)["expected_cash"]
    shift.close(closing_cash=expected_cash, close_reason=reason)
    logger.info(
        "Автозакрытие смены %s (кассир=%s, касса=%s), причина=%s, инициатор=%s",
        shift.pk, shift.cashier_id, shift.cashbox_id, reason, getattr(by_user, "pk", None),
    )
    return shift


def close_open_shifts(shifts_qs, reason: str, *, by_user=None, dry_run=False) -> dict:
    """
    Пакетное закрытие открытых смен из queryset. Каждая смена — в своей транзакции,
    ошибка одной не прерывает остальные (best-effort, для команд/периодических задач).

    Возвращает {"closed": [pk...], "failed": [(pk, error)...]}.
    """
    ids = list(shifts_qs.filter(status=CashShift.Status.OPEN).values_list("pk", flat=True))
    result = {"closed": [], "failed": []}

    if dry_run:
        result["closed"] = ids
        return result

    for pk in ids:
        try:
            with transaction.atomic():
                shift = CashShift.objects.select_for_update().get(pk=pk)
                if shift.status != CashShift.Status.OPEN:
                    continue  # уже закрыта параллельно
                close_shift_as_system(shift, reason, by_user=by_user)
            result["closed"].append(pk)
        except Exception as e:  # noqa: BLE001 — одна плохая смена не должна валить весь батч
            logger.exception("Не удалось автозакрыть смену %s: %s", pk, e)
            result["failed"].append((pk, str(e)))

    return result


def close_shifts_for_deleted_cashiers(*, by_user=None, dry_run=False) -> dict:
    """Закрыть открытые смены всех удалённых (soft-deleted) кассиров."""
    qs = CashShift.objects.filter(
        status=CashShift.Status.OPEN,
        cashier__deleted_at__isnull=False,
    )
    return close_open_shifts(qs, REASON_EMPLOYEE_DELETED, by_user=by_user, dry_run=dry_run)


def close_stale_shifts(*, max_age_hours: int = DEFAULT_MAX_AGE_HOURS, now=None, dry_run=False) -> dict:
    """Закрыть смены, открытые дольше max_age_hours (по умолчанию сутки)."""
    from django.db.models import Q
    from apps.users.models import CompanyAppSettings

    now = now or timezone.now()
    cutoff = now - timedelta(hours=max_age_hours)
    # ТЗ Н-09: компании со своей настройкой — по ней (выкл → не трогаем, свой срок в часах).
    custom = {}
    for row in CompanyAppSettings.objects.filter(branch__isnull=True, app=AUTOCLOSE_SETTINGS_APP):
        if AUTOCLOSE_SETTINGS_KEY in (row.settings or {}):
            custom[row.company_id] = _parse_autoclose(row.settings[AUTOCLOSE_SETTINGS_KEY])
    cond = Q(opened_at__lte=cutoff) & ~Q(company_id__in=list(custom))
    for cid, (enabled, hours) in custom.items():
        if enabled:
            cond |= Q(company_id=cid, opened_at__lte=now - timedelta(hours=hours))
    qs = CashShift.objects.filter(cond, status=CashShift.Status.OPEN)
    return close_open_shifts(qs, REASON_TIMEOUT_24H, dry_run=dry_run)
