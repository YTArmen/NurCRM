"""
График долга в checkout (ТЗ веб-кассы, п. 12.8).

Раньше pos/checkout/ создавал сделку с одним взносом через 30 дней, а касса досылала график вторым
PATCH — если касса закрывалась между запросами, график оставался 30-дневным. Теперь график
принимается в самом чеке и применяется к сделке в той же транзакции.

Тело (все поля необязательны):
    "schedule_version": "v1" | "v2",
    "debt_schedule": либо список взносов  [{"due_date": "2026-11-01", "amount": "500.00"}, …],
                     либо объект {"installments": [...], "debt_days": 3, "debt_months": null,
                                  "interval_days": 7, "interval_months": 1,
                                  "first_due_date": "2026-11-01"}
Сумма взносов должна совпасть с долгом по чеку (±0.05), иначе 400 invalid_debt_schedule.
Без installments график строится из срока (как PATCH сделки: debt_days — число платежей).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.dateparse import parse_date

from apps.main.models import ClientDeal
from core.exceptions import ApiError

_INT_FIELDS = ("debt_days", "debt_months", "interval_days", "interval_months")


def _bad(detail, field="debt_schedule"):
    return ApiError("invalid_debt_schedule", detail, fields={field: [detail]})


def _int(val, name):
    if val in (None, ""):
        return None
    try:
        n = int(val)
    except (TypeError, ValueError):
        raise _bad(f"{name}: ожидается целое число.", f"debt_schedule.{name}")
    if n <= 0:
        raise _bad(f"{name}: должно быть больше нуля.", f"debt_schedule.{name}")
    return n


def _parse_installments(raw):
    if not isinstance(raw, list) or not raw:
        raise _bad("installments: ожидается непустой список взносов.")
    out = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise _bad(f"Взнос №{i + 1}: ожидается объект.")
        d = row.get("due_date")
        d = d if isinstance(d, date) else parse_date(str(d or ""))
        if d is None:
            raise _bad(f"Взнос №{i + 1}: укажите due_date (YYYY-MM-DD).")
        try:
            amt = Decimal(str(row.get("amount")))
        except (InvalidOperation, ValueError):
            raise _bad(f"Взнос №{i + 1}: укажите сумму amount.")
        if amt <= 0:
            raise _bad(f"Взнос №{i + 1}: сумма должна быть больше нуля.")
        item = {"due_date": d.isoformat(), "amount": str(amt.quantize(Decimal("0.01")))}
        if row.get("promised_date"):
            item["promised_date"] = str(row["promised_date"])
        out.append(item)
    return out


def apply_debt_schedule(sale, data) -> ClientDeal | None:
    raw = data.get("debt_schedule")
    version = (data.get("schedule_version") or "").strip().lower()
    if raw in (None, "", [], {}) and not version:
        return None
    deal = ClientDeal.objects.filter(sale=sale, kind=ClientDeal.Kind.DEBT).order_by("created_at").first()
    if deal is None:
        return None  # долга по чеку нет (оплачен целиком или нет клиента) — график не нужен

    params = {}
    installments = None
    if isinstance(raw, list):
        installments = _parse_installments(raw)
    elif isinstance(raw, dict):
        if raw.get("installments") not in (None, ""):
            installments = _parse_installments(raw["installments"])
        for f in _INT_FIELDS:
            if f in raw:
                params[f] = _int(raw[f], f)
        if raw.get("first_due_date"):
            fd = parse_date(str(raw["first_due_date"]))
            if fd is None:
                raise _bad("first_due_date: ожидается YYYY-MM-DD.", "debt_schedule.first_due_date")
            params["first_due_date"] = fd
    elif raw not in (None, ""):
        raise _bad("debt_schedule: ожидается список взносов или объект.")
    if version:
        if version not in ("v1", "v2", "1", "2"):
            raise _bad("schedule_version: допустимо v1 или v2.", "schedule_version")
        deal.schedule_version = "v1" if version in ("v1", "1") else "v2"

    if params.get("debt_days") and params.get("debt_months"):
        raise _bad("Нельзя одновременно указывать debt_days и debt_months.")
    if params.get("debt_months"):
        params["debt_days"] = None
    elif params.get("debt_days"):
        params["debt_months"] = None

    if installments is not None:
        total = sum((Decimal(i["amount"]) for i in installments), Decimal("0"))
        if abs(total - deal.debt_amount) > Decimal("0.05"):
            raise _bad(f"Сумма графика ({total}) должна совпадать с долгом по чеку ({deal.debt_amount}).")
        deal._custom_installments = installments
        params.setdefault("debt_days", len(installments))
        params["debt_months"] = None
        params.setdefault("first_due_date", parse_date(installments[0]["due_date"]))
    for k, v in params.items():
        setattr(deal, k, v)
    deal.auto_schedule = True
    try:
        deal.save()
    except DjangoValidationError as e:
        raise _bad(" ".join(e.messages))
    return deal


def schedule_payload(sale) -> dict:
    deal = ClientDeal.objects.filter(sale=sale, kind=ClientDeal.Kind.DEBT).order_by("created_at").first()
    if deal is None:
        return {}
    return {
        "deal_id": str(deal.id),
        "schedule_version": deal.schedule_version,
        "debt_schedule": [
            {"number": i.number, "due_date": i.due_date.isoformat(), "amount": str(i.amount)}
            for i in deal.installments.order_by("number")
        ],
    }
