"""Уведомления и автонапоминания (ТЗ §3.9, §5.1, §5.2). Каналы — заглушки: SMS/WhatsApp пишутся как not_sent,
Telegram владельцу отправляется, только если настроен (ARENDA_TELEGRAM_BOT_TOKEN + chat id в params правила)."""
import json
import logging
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

from django.conf import settings as dj_settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..constants import REMINDER_RULE_KINDS
from ..defaults import ensure_company_defaults
from ..models import Charge, Lease, Payment, Reminder, ReminderRule, Tenant
from .charges import month_start
from .debt import allocate_fifo, calc_penalty, compute_debt
from .stats import rented_leases

logger = logging.getLogger(__name__)

RULE_TITLES = {
    "before_3_days": "За 3 дня до срока оплаты",
    "due_day": "В день оплаты (с суммой и реквизитами)",
    "overdue_every_3_days": "Каждые 3 дня просрочки (копия владельцу в Telegram)",
    "penalty_from_day_1": "Начисление пени с 1-го дня просрочки",
    "lease_end_45_days": "За 45 дней до конца договора (владельцу)",
}


def fmt_money(t: int) -> str:
    return f"{int(t) // 100:,}".replace(",", " ") + " с"


# ------------------------------------------------------------------ состояние арендаторов


def tenant_states(company, today, branch_ids=None, soon_days=7, tenant_ids=None):
    """{tenant_id: {"info": DebtInfo, "remaining": [(due, kind, rest)], "soon_amount", "soon_due"}}"""
    cq = Charge.objects.filter(company=company, voided_at__isnull=True)
    pq = Payment.objects.filter(company=company, status="posted", tenant__isnull=False)
    if branch_ids is not None:
        cq, pq = cq.filter(branch_id__in=branch_ids), pq.filter(branch_id__in=branch_ids)
    if tenant_ids is not None:
        cq, pq = cq.filter(tenant_id__in=tenant_ids), pq.filter(tenant_id__in=tenant_ids)
    by_t = defaultdict(list)
    for t, due, amount, kind in cq.values_list("tenant_id", "due_date", "amount", "kind"):
        by_t[t].append((due, amount, kind))
    paid = defaultdict(int)
    for t, amount in pq.values_list("tenant_id", "amount"):
        paid[t] += amount
    out = {}
    for t, charges in by_t.items():
        info = compute_debt(charges, paid.get(t, 0), today, soon_days)
        remaining, _ = allocate_fifo(charges, paid.get(t, 0))
        soon = [(d, r) for d, k, r in remaining if r > 0 and today <= d <= today + timedelta(days=soon_days)]
        out[t] = {"info": info, "remaining": [x for x in remaining if x[2] > 0],
                  "soon_amount": sum(r for _, r in soon), "soon_due": min((d for d, _ in soon), default=None)}
    return out


def _rates(company, today, settings_obj, branch_ids=None):
    """{tenant_id: (ставка пени %, lease)} по действующим договорам."""
    out = {}
    for l in rented_leases(company, today, branch_ids):
        out.setdefault(l.tenant_id, (l.penalty_rate, l))
    return out


def build_summary(company, today, branch_ids=None):
    s = company.arenda_settings
    states = tenant_states(company, today, branch_ids, s.soon_days)
    rates = _rates(company, today, s, branch_ids)
    tenants = {t.id: t for t in Tenant.objects.filter(id__in=list(states), archived_at__isnull=True)}
    debtors, soon = [], []
    for tid, st in states.items():
        t = tenants.get(tid)
        if not t:
            continue
        info = st["info"]
        rate = rates.get(tid, (s.penalty_rate, None))[0]
        brief = {"id": str(t.id), "name": t.name, "phone": t.phone}
        if info.status == "overdue":
            debtors.append({"tenant": brief, "debt": info.debt, "overdue_days": info.overdue_days,
                            "penalty_calc": calc_penalty(info.overdue_base, rate, info.overdue_days)})
        elif info.status == "due_soon":
            soon.append({"tenant": brief, "amount": st["soon_amount"], "due_date": st["soon_due"],
                         "days_left": (st["soon_due"] - today).days if st["soon_due"] else None})
    debtors.sort(key=lambda r: (-r["overdue_days"], -r["debt"]))
    soon.sort(key=lambda r: (r["due_date"] or today))
    expiring = []
    lq = Lease.objects.filter(company=company, status="active", archived_at__isnull=True, end_date__isnull=False,
                              end_date__gte=today, end_date__lte=today + timedelta(days=s.lease_expiring_days))
    if branch_ids is not None:
        lq = lq.filter(branch_id__in=branch_ids)
    for l in lq.select_related("tenant").order_by("end_date"):
        expiring.append({"lease_id": str(l.id), "label": l.label, "tenant": {"id": str(l.tenant_id), "name": l.tenant.name},
                         "end_date": l.end_date, "days_left": (l.end_date - today).days})
    return {"count": len(debtors) + len(soon), "overdue_count": len(debtors), "due_soon_count": len(soon),
            "expiring_count": len(expiring), "debtors": debtors, "due_soon": soon, "expiring_leases": expiring}


# ------------------------------------------------------------------ каналы


def _telegram_post(token, chat_id, text):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text[:4000]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
        return json.loads(r.read().decode()).get("ok", False)


def send_owner_telegram(company, text):
    """(ok, error). Токен — settings.ARENDA_TELEGRAM_BOT_TOKEN, чат владельца — params.telegram_chat_id любого правила."""
    token = getattr(dj_settings, "ARENDA_TELEGRAM_BOT_TOKEN", "")
    chat_id = ""
    for r in ReminderRule.objects.filter(company=company):
        chat_id = (r.params or {}).get("telegram_chat_id") or chat_id
    if not token or not chat_id:
        return False, "Telegram не настроен"
    try:
        return (True, "") if _telegram_post(token, chat_id, text) else (False, "Telegram отклонил сообщение")
    except Exception as exc:  # сеть, таймаут
        logger.warning("arenda telegram failed: %s", type(exc).__name__)
        return False, "Ошибка отправки в Telegram"


def deliver(reminder):
    """SMS/WhatsApp — заглушка (not_sent). Telegram — владельцу, если настроен."""
    if reminder.channel == "telegram":
        ok, err = send_owner_telegram(reminder.company, reminder.text)
        reminder.status = "sent" if ok else "not_sent"
        reminder.error = err
        reminder.sent_at = timezone.now() if ok else None
        reminder.save(update_fields=["status", "error", "sent_at", "updated_at"])
    return reminder


def make_reminder(company, tenant, channel, text, *, lease=None, is_auto=False, rule_kind="", dedupe_key="",
                  user=None):
    """Создаёт напоминание; (reminder, created). Повтор с тем же dedupe_key ничего не создаёт."""
    if dedupe_key:
        ex = Reminder.objects.filter(company=company, dedupe_key=dedupe_key).first()
        if ex:
            return ex, False
    try:
        with transaction.atomic():
            r = Reminder.objects.create(company=company, tenant=tenant, lease=lease, channel=channel, text=text,
                                        is_auto=is_auto, rule_kind=rule_kind, dedupe_key=dedupe_key, created_by=user)
    except IntegrityError:
        return Reminder.objects.get(company=company, dedupe_key=dedupe_key), False
    deliver(r)
    return r, True


def debt_text(company, tenant, info, rate):
    s = company.arenda_settings
    pen = calc_penalty(info.overdue_base, rate, info.overdue_days)
    txt = f"{tenant.name}, напоминаем: задолженность по аренде {fmt_money(info.debt)}"
    if info.overdue_days:
        txt += f", просрочка {info.overdue_days} дн."
    if pen:
        txt += f", расчётная пеня {fmt_money(pen)}"
    return txt + "." + _requisites(s)


def _requisites(s):
    parts = [x for x in (s.bank, f"р/с {s.account}" if s.account else "") if x]
    return (" Реквизиты: " + ", ".join(parts) + ".") if parts else ""


# ------------------------------------------------------------------ пеня


def create_penalty_charge(company, tenant_id, lease, on_date, base, rate_percent, created_by=None):
    """Начисление пени за один день просрочки (Charge kind=penalty). Идемпотентно по (арендатор, дата).
    Пеня на пеню не начисляется: base — просроченный остаток без пени."""
    amount = calc_penalty(base, rate_percent, 1)
    if amount <= 0:
        return None, False
    desc = f"Пеня (авто) за {on_date:%d.%m.%Y}"
    ex = Charge.objects.filter(company=company, tenant_id=tenant_id, kind="penalty", due_date=on_date,
                               description=desc, voided_at__isnull=True).first()
    if ex:
        return ex, False
    ch = Charge.objects.create(company=company, branch_id=lease.branch_id, tenant_id=tenant_id, lease=lease,
                               kind="penalty", period=month_start(on_date), amount=amount, due_date=on_date,
                               description=desc, created_by=created_by)
    return ch, True


# ------------------------------------------------------------------ автоправила


def rules_state(company):
    ensure_company_defaults(company)
    for kind in REMINDER_RULE_KINDS:
        ReminderRule.objects.get_or_create(company=company, kind=kind)
    return {r.kind: r for r in ReminderRule.objects.filter(company=company)}


def _anniversary(start: date, year: int):
    try:
        return start.replace(year=year)
    except ValueError:  # 29 февраля
        return start.replace(year=year, day=28)


def run_auto_reminders(company, on_date: date) -> dict:
    """Идемпотентный прогон автоправил на дату. Возвращает счётчики."""
    s = ensure_company_defaults(company)
    rules = rules_state(company)
    on = {k for k, r in rules.items() if r.enabled}
    res = defaultdict(int)
    states = tenant_states(company, on_date, None, s.soon_days) if on else {}
    tenants = {t.id: t for t in Tenant.objects.filter(id__in=list(states), archived_at__isnull=True)}
    rates = _rates(company, on_date, s)

    for tid, st in states.items():
        t = tenants.get(tid)
        if not t:
            continue
        info = st["info"]
        rate, lease = rates.get(tid, (s.penalty_rate, None))
        if "before_3_days" in on:
            target = on_date + timedelta(days=3)
            amt = sum(r for d, k, r in st["remaining"] if d == target and k != "penalty")
            if amt > 0:
                text = (f"{t.name}, напоминаем: {target:%d.%m.%Y} срок оплаты аренды, к оплате {fmt_money(amt)}.")
                _, c = make_reminder(company, t, "sms", text, lease=lease, is_auto=True, rule_kind="before_3_days",
                                     dedupe_key=f"auto:before_3_days:{tid}:{target}")
                res["before_3_days"] += int(c)
        if "due_day" in on:
            amt = sum(r for d, k, r in st["remaining"] if d == on_date and k != "penalty")
            if amt > 0:
                text = f"{t.name}, сегодня срок оплаты аренды: {fmt_money(amt)}." + _requisites(s)
                _, c = make_reminder(company, t, "sms", text, lease=lease, is_auto=True, rule_kind="due_day",
                                     dedupe_key=f"auto:due_day:{tid}:{on_date}")
                res["due_day"] += int(c)
        if "overdue_every_3_days" in on and info.status == "overdue" and info.overdue_days % 3 == 0:
            text = debt_text(company, t, info, rate)
            _, c = make_reminder(company, t, "sms", text, lease=lease, is_auto=True, rule_kind="overdue_every_3_days",
                                 dedupe_key=f"auto:overdue3:{tid}:{on_date}")
            res["overdue_every_3_days"] += int(c)
            make_reminder(company, t, "telegram", "Копия владельцу: " + text, lease=lease, is_auto=True,
                          rule_kind="overdue_every_3_days", dedupe_key=f"auto:overdue3:{tid}:{on_date}:owner")
        if "penalty_from_day_1" in on and info.status == "overdue" and lease is not None:
            _, c = create_penalty_charge(company, tid, lease, on_date, info.overdue_base, rate)
            res["penalty_from_day_1"] += int(c)

    if "lease_end_45_days" in on:
        target = on_date + timedelta(days=s.lease_expiring_days)
        for l in Lease.objects.filter(company=company, status="active", archived_at__isnull=True,
                                      end_date=target).select_related("tenant"):
            text = (f"Владельцу: договор {l.label or l.number} с {l.tenant.name} заканчивается {l.end_date:%d.%m.%Y} "
                    f"(через {s.lease_expiring_days} дн.).")
            _, c = make_reminder(company, l.tenant, "telegram", text, lease=l, is_auto=True,
                                 rule_kind="lease_end_45_days", dedupe_key=f"auto:lease_end:{l.id}:{l.end_date}")
            res["lease_end_45_days"] += int(c)

    # уведомление об индексации за N дней (ТЗ §5.1) — владельцу, отдельного переключателя в ТЗ нет
    notice = s.indexation_notice_days
    for l in Lease.objects.filter(company=company, status="active", archived_at__isnull=True,
                                  indexation_percent__gt=0).select_related("tenant"):
        for y in (on_date.year, on_date.year + 1):
            anniv = _anniversary(l.start_date, y)
            if anniv > l.start_date and (l.end_date is None or anniv <= l.end_date) \
                    and anniv - timedelta(days=notice) == on_date:
                text = (f"Владельцу: с {anniv:%d.%m.%Y} аренда по договору {l.label or l.number} ({l.tenant.name}) "
                        f"вырастет на {l.indexation_percent}% (индексация).")
                _, c = make_reminder(company, l.tenant, "telegram", text, lease=l, is_auto=True,
                                     rule_kind="indexation", dedupe_key=f"auto:index:{l.id}:{anniv}")
                res["indexation"] += int(c)
    return dict(res)
