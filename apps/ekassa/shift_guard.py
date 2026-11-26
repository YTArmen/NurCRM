"""
Сторож смены eKassa (18.11.2026, метка sg1811).

eKassa не принимает чеки, если её смена открыта дольше 24 часов или закрыта.
Смена eKassa одна на компанию (одна ККМ), а CRM-смен может быть несколько: shift_bridge
открывает eKassa с первой CRM-сменой и закрывает с последней. Если кассиры работают
внахлёст, eKassa сама никогда не закроется — поэтому сервер следит за ней сам:

- перед чеком (не чаще раза в CHECK_EVERY на компанию) смотрит смену eKassa:
  открыта дольше ROTATE_AFTER — Z-отчёт и сразу новая смена;
- если eKassa отклонила чек — смотрит смену: закрыта → открывает, старая → переоткрывает,
  и отправляет чек ещё раз (тот же newid);
- cron (manage.py ekassa_shift_guard, раз в 20 минут) делает то же для всех компаний,
  даже когда чеков нет. Старая смена без открытых CRM-смен просто закрывается.

Ошибки сторожа никогда не мешают отправке чека — только лог.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.utils import timezone

from apps.ekassa.client import EkassaHttpClient
from apps.ekassa.exceptions import EkassaAPIError

logger = logging.getLogger(__name__)

ROTATE_AFTER = timedelta(hours=23)
CHECK_EVERY = 600  # секунд между проверками перед чеком
LOCK_TTL = 90
EKASSA_TZ = ZoneInfo("Asia/Bishkek")  # shift_date eKassa — местное время


def is_guarded(cfg) -> bool:
    """Сторож работает только для боевой eKassa (pos.ekassa.kg); тестовые стенды не трогаем."""
    return "pos.ekassa.kg" in (cfg.api_base_url or "").lower()


def _fn(cfg) -> str:
    return (cfg.fiscal_number or "").strip()


def _payload_text(e: EkassaAPIError) -> str:
    return f"{e} {e.payload}".lower()


def shift_info(cfg, cli: EkassaHttpClient | None = None) -> tuple[int | None, datetime | None]:
    """(shift_state, shift_date) из get_pos_by_fiscal_number: 1 — открыта, 0 — закрыта;
    shift_date — время последнего открытия/закрытия."""
    cli = cli or EkassaHttpClient(cfg)
    r = cli.request_json("POST", "/api/get_pos_by_fiscal_number", json_body={"fiscal_number": _fn(cfg)})
    d = r.get("data") or {}
    if isinstance(d, list):
        d = d[0] if d else {}
    try:
        state = int(d.get("shift_state"))
    except (TypeError, ValueError):
        state = None
    at = None
    raw = str(d.get("shift_date") or "")[:19]
    if raw:
        try:
            at = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EKASSA_TZ)
        except ValueError:
            at = None
    return state, at


def _open(cli: EkassaHttpClient, cfg) -> None:
    try:
        cli.request_json("POST", "/api/shift_open_by_fiscal_number", json_body={"fiscal_number": _fn(cfg)})
    except EkassaAPIError as e:
        if "already opened" not in _payload_text(e):
            raise


def _close(cli: EkassaHttpClient, cfg) -> None:
    try:
        cli.request_json("POST", "/api/shift_close_by_fiscal_number", json_body={"fiscal_number": _fn(cfg)})
    except EkassaAPIError as e:
        if "no opened shift" not in _payload_text(e):
            raise


def _crm_shift_open(company_id) -> bool:
    from apps.construction.models import CashShift

    return CashShift.objects.filter(company_id=company_id, status=CashShift.Status.OPEN).exists()


def _lock(company_id, wait: float = 20.0) -> bool:
    key = f"ekassa:sg1811:lock:{company_id}"
    deadline = time.monotonic() + wait
    while True:
        if cache.add(key, 1, LOCK_TTL):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _unlock(company_id) -> None:
    cache.delete(f"ekassa:sg1811:lock:{company_id}")


def ensure_fresh_shift(cfg, *, need_open: bool = False, now: datetime | None = None) -> str:
    """Привести смену eKassa в порядок. need_open — сейчас будет чек, смена нужна открытой.
    Возвращает: ok | opened | rotated | closed | busy."""
    company_id = cfg.company_id
    if not _lock(company_id):
        return "busy"
    try:
        cli = EkassaHttpClient(cfg)
        state, at = shift_info(cfg, cli)
        now = now or timezone.now()
        action = "ok"
        if state == 1 and at is not None and now - at >= ROTATE_AFTER:
            _close(cli, cfg)
            if need_open or _crm_shift_open(company_id):
                _open(cli, cfg)
                action = "rotated"
            else:
                action = "closed"
        elif state == 0 and need_open:
            _open(cli, cfg)
            action = "opened"
        cache.set(f"ekassa:sg1811:checked:{company_id}", 1, CHECK_EVERY)
        if action != "ok":
            logger.info("eKassa shift guard company=%s: %s (state=%s, shift_date=%s)", company_id, action, state, at)
        return action
    finally:
        _unlock(company_id)


def before_receipt(cfg) -> None:
    """Перед чеком: проверка старой смены, не чаще раза в CHECK_EVERY. Никогда не бросает."""
    if cache.get(f"ekassa:sg1811:checked:{cfg.company_id}"):
        return
    try:
        ensure_fresh_shift(cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("eKassa shift guard (before) company=%s: %s", cfg.company_id, e)


def send_receipt(cfg, body: dict) -> dict:
    """POST /api/v2/receipt со сторожем смены: при отказе чинит смену и повторяет один раз."""
    cli = EkassaHttpClient(cfg)
    if not is_guarded(cfg):
        return cli.request_json("POST", "/api/v2/receipt", json_body=body)
    before_receipt(cfg)
    try:
        return cli.request_json("POST", "/api/v2/receipt", json_body=body)
    except EkassaAPIError as first:
        try:
            action = ensure_fresh_shift(cfg, need_open=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("eKassa shift guard (recover) company=%s: %s", cfg.company_id, e)
            raise first
        if action not in ("opened", "rotated"):
            raise first
        logger.info("eKassa receipt retry after shift %s company=%s", action, cfg.company_id)
        return cli.request_json("POST", "/api/v2/receipt", json_body=body)
