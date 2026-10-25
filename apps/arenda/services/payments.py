"""Оплаты: разнесение по начислениям (старые первыми), пеня, сторно, чеки (ТЗ §3.4, §5.2)."""
from datetime import datetime, time
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.exceptions import ApiError

from ..models import Charge, Counter, Lease, Payment, PaymentAllocation, Receipt
from . import stats
from .charges import create_penalty_charge, month_start
from .debt import DebtInfo, calc_penalty
from .words import fmt_som

CATEGORIES = {"advertising": "Реклама", "parking": "Парковка", "other": "Прочее"}
METHOD_TITLES = {"cash": "Наличные", "bank": "Банк", "qr": "QR / Элкарт", "wallet": "Кошелёк"}
KIND_TITLES = {"rent": "Аренда", "penalty": "Пеня", "fine": "Штраф", "utilities": "Коммунальные"}


def paid_at_for(day, now=None):
    now = now or timezone.now()
    if day == timezone.localtime(now).date():
        return now
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


def category_of(description: str):
    """Категория прочего прихода хранится префиксом описания: 'Реклама: текст'."""
    for key, title in CATEGORIES.items():
        if description == title or description.startswith(title + ": "):
            return key
    return None


def tenant_branch_id(tenant):
    lease = (Lease.objects.filter(tenant=tenant, status="active", archived_at__isnull=True)
             .order_by("start_date").first())
    if lease:
        return lease.branch_id
    return tenant.branch_id


def tenant_penalty_rate(tenant, default_rate):
    lease = (Lease.objects.filter(tenant=tenant, status="active", archived_at__isnull=True)
             .order_by("start_date").first())
    return lease.penalty_rate if lease else default_rate


def rebuild_allocations(company, tenant):
    """Пересобирает разнесение: оплаты по порядку приёма закрывают самые старые начисления.
    Остаток оплат без начислений — аванс (зачтётся автоматически при следующем пересчёте)."""
    PaymentAllocation.objects.filter(company=company, payment__tenant=tenant).delete()
    charges = list(Charge.objects.filter(company=company, tenant=tenant, voided_at__isnull=True)
                   .order_by("due_date", "created_at"))
    rest = {c.id: c.amount for c in charges}
    payments = Payment.objects.filter(company=company, tenant=tenant, status="posted").order_by("paid_at", "created_at")
    rows = []
    for p in payments:
        left = p.amount
        for c in charges:
            if left <= 0:
                break
            r = rest[c.id]
            if r <= 0:
                continue
            take = min(r, left)
            rest[c.id] -= take
            left -= take
            rows.append(PaymentAllocation(company=company, payment=p, charge=c, amount=take))
    PaymentAllocation.objects.bulk_create(rows)


def tenant_debt(company, tenant, today, soon_days=7) -> DebtInfo:
    return stats.debt_map(company, today, None, [tenant.id], soon_days).get(tenant.id, DebtInfo())


def tenant_units(company, tenant, branch_id=None):
    qs = Lease.objects.filter(company=company, tenant=tenant, status="active", archived_at__isnull=True)
    if branch_id:
        qs = qs.filter(branch_id=branch_id)
    out = []
    for lease in qs.prefetch_related("units"):
        for u in lease.units.all():
            if u.number not in out:
                out.append(u.number)
    return out


def user_name(u):
    return (u.get_full_name() if u else "") or ""


def period_text(periods):
    if not periods:
        return ""
    lo, hi = min(periods), max(periods)
    if lo == hi:
        return f"{lo:%m.%Y}"
    return f"{lo:%m.%Y} – {hi:%m.%Y}"


def _snapshot(payment, settings_obj, allocations, debt_after):
    by_kind = {"rent": 0, "penalty": 0, "fine": 0, "utilities": 0}
    periods = []
    for a in allocations:
        by_kind[a.charge.kind] = by_kind.get(a.charge.kind, 0) + a.amount
        if a.charge.kind in ("rent", "utilities"):
            periods.append(a.charge.period)
    advance = payment.amount - sum(a.amount for a in allocations)
    if not periods:
        periods = [month_start(timezone.localtime(payment.paid_at).date())]
    lines = [{"kind": k, "title": KIND_TITLES[k], "amount": v} for k, v in by_kind.items() if v]
    if advance:
        lines.append({"kind": "advance", "title": "Аванс (переплата)", "amount": advance})
    tenant = payment.tenant
    return {
        "company": {"name": settings_obj.org_name, "inn": settings_obj.inn, "okpo": settings_obj.okpo,
                    "address": settings_obj.legal_address, "phone": settings_obj.phone},
        "tenant": {"id": str(tenant.id), "name": tenant.name, "inn": tenant.inn, "phone": tenant.phone},
        "units": tenant_units(payment.company, tenant, payment.branch_id),
        "period": period_text(periods),
        "rent": by_kind["rent"], "penalty": by_kind["penalty"], "fine": by_kind["fine"],
        "utilities": by_kind["utilities"], "advance": advance,
        "lines": lines,
        "amount": payment.amount,
        "method": payment.method, "method_title": METHOD_TITLES.get(payment.method, payment.method),
        "cashier": user_name(payment.cashier),
        "paid_at": timezone.localtime(payment.paid_at).isoformat(),
        "debt_after": debt_after,
    }


@transaction.atomic
def post_payment(*, company, settings_obj, user, branch_id, tenant, amount, method, day, add_penalty=False,
                 description="", today=None):
    """Проводит оплату. Для арендатора: (опц.) пеня → разнесение → чек. Возвращает dict с результатом."""
    today = today or timezone.localdate()
    penalty_charge = None
    if tenant is not None:
        type(tenant).objects.select_for_update().filter(pk=tenant.pk).first()
        if add_penalty:
            info = tenant_debt(company, tenant, today, settings_obj.soon_days)
            pen = calc_penalty(info.overdue_base, tenant_penalty_rate(tenant, settings_obj.penalty_rate),
                               info.overdue_days)
            penalty_charge, created = create_penalty_charge(
                company=company, tenant_id=tenant.pk, branch_id=branch_id, on_date=today, amount=pen,
                description=f"Пеня на {today:%d.%m.%Y}", created_by=user)
            if not created:
                penalty_charge = None  # за сегодня пеня уже начислена (авто-правилом или прошлой оплатой)
    payment = Payment.objects.create(
        company=company, branch_id=branch_id, tenant=tenant, amount=amount, method=method,
        paid_at=paid_at_for(day), cashier=user, description=description, add_penalty=bool(add_penalty),
        created_by=user)
    if penalty_charge is not None:
        penalty_charge.description = f"Пеня при оплате {payment.id}"
        penalty_charge.save(update_fields=["description"])
    receipt = None
    allocations = []
    debt_after = None
    if tenant is not None:
        rebuild_allocations(company, tenant)
        allocations = list(payment.allocations.select_related("charge").order_by("charge__due_date"))
        debt_after = tenant_debt(company, tenant, today, settings_obj.soon_days).debt
        payment = Payment.objects.select_related("tenant", "cashier", "company").get(pk=payment.pk)
        n = Counter.next(company, "receipt")
        receipt = Receipt.objects.create(
            company=company, payment=payment, number=n, label=f"Ч-{n:05d}", created_by=user,
            snapshot=_snapshot(payment, settings_obj, allocations, debt_after))
    return {"payment": payment, "receipt": receipt, "allocations": allocations, "debt_after": debt_after,
            "penalty_charge": penalty_charge}


@transaction.atomic
def storno_payment(*, company, settings_obj, payment, user, reason, today=None):
    """Сторно: запись остаётся (status=reversed), разнесение снимается, пеня «при оплате» аннулируется."""
    if payment.status == "reversed":
        raise ApiError("already_reversed", "Оплата уже сторнирована.", status_code=409)
    before = {"allocations": [{"charge": str(a.charge_id), "amount": a.amount} for a in payment.allocations.all()]}
    now = timezone.now()
    payment.status = "reversed"
    payment.reversal_reason = reason
    payment.reversed_at = now
    payment.reversed_by = user
    payment.save(update_fields=["status", "reversal_reason", "reversed_at", "reversed_by", "updated_at"])
    voided = []
    if payment.tenant_id:
        for ch in Charge.objects.filter(company=company, tenant_id=payment.tenant_id, kind="penalty",
                                        voided_at__isnull=True, description=f"Пеня при оплате {payment.id}"):
            ch.voided_at = now
            ch.save(update_fields=["voided_at", "updated_at"])
            voided.append(str(ch.id))
        rebuild_allocations(company, payment.tenant)
    before["voided_penalty_charges"] = voided
    return before


def balances(company, branch_ids=None):
    """Остатки по способам: проведённые оплаты минус расходы (по способу)."""
    from ..models import Expense
    out = {m: 0 for m in METHOD_TITLES}
    pq = Payment.objects.filter(company=company, status="posted")
    eq = Expense.objects.filter(company=company, voided_at__isnull=True)
    if branch_ids is not None:
        pq, eq = pq.filter(branch_id__in=branch_ids), eq.filter(branch_id__in=branch_ids)
    for r in pq.values("method").annotate(s=Sum("amount")):
        out[r["method"]] = out.get(r["method"], 0) + int(r["s"] or 0)
    for r in eq.values("method").annotate(s=Sum("amount")):
        out[r["method"]] = out.get(r["method"], 0) - int(r["s"] or 0)
    return out


# ------------------------------------------------------------------ вид чека


def receipt_text(receipt) -> str:
    s = receipt.snapshot
    c = s["company"]
    paid = datetime.fromisoformat(s["paid_at"])
    lines = [f"ЧЕК № {receipt.label}", c.get("name") or ""]
    if c.get("inn"):
        lines.append(f"ИНН {c['inn']}")
    if c.get("address"):
        lines.append(c["address"])
    lines.append(f"Дата: {paid:%d.%m.%Y %H:%M}")
    lines.append(f"Арендатор: {s['tenant']['name']}")
    if s.get("units"):
        lines.append("Помещение: " + ", ".join(s["units"]))
    if s.get("period"):
        lines.append(f"За период: {s['period']}")
    for ln in s["lines"]:
        lines.append(f"{ln['title']}: {fmt_som(ln['amount'])} сом")
    lines.append(f"ИТОГО: {fmt_som(s['amount'])} сом")
    lines.append(f"Способ: {s['method_title']}")
    if s.get("cashier"):
        lines.append(f"Кассир: {s['cashier']}")
    lines.append(f"Остаток долга: {fmt_som(s['debt_after'])} сом")
    if receipt.payment.status == "reversed":
        lines.append("ОПЛАТА СТОРНИРОВАНА")
    return "\n".join(x for x in lines if x is not None)


def receipt_html(receipt) -> str:
    from django.utils.html import escape
    s = receipt.snapshot
    c = s["company"]
    paid = datetime.fromisoformat(s["paid_at"])
    rows = "".join(f"<tr><td>{escape(ln['title'])}</td><td class='r'>{fmt_som(ln['amount'])}</td></tr>"
                   for ln in s["lines"])
    stamp = "<p class='c'><b>ОПЛАТА СТОРНИРОВАНА</b></p>" if receipt.payment.status == "reversed" else ""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Чек {escape(receipt.label)}</title>
<style>
@page {{ size: 80mm auto; margin: 3mm; }}
body {{ width: 74mm; margin: 0 auto; font: 12px/1.35 'DejaVu Sans Mono', monospace; color: #000; }}
h1 {{ font-size: 14px; text-align: center; margin: 4px 0; }}
.c {{ text-align: center; margin: 2px 0; }} .r {{ text-align: right; }}
table {{ width: 100%; border-collapse: collapse; }} td {{ padding: 1px 0; vertical-align: top; }}
hr {{ border: 0; border-top: 1px dashed #000; margin: 5px 0; }}
</style></head><body>
<h1>{escape(c.get('name') or '')}</h1>
<p class="c">{('ИНН ' + escape(c['inn'])) if c.get('inn') else ''}</p>
<p class="c">{escape(c.get('address') or '')}</p>
<hr><p class="c"><b>ЧЕК № {escape(receipt.label)}</b></p>
<p class="c">{paid:%d.%m.%Y %H:%M}</p><hr>
<p>Арендатор: {escape(s['tenant']['name'])}</p>
<p>Помещение: {escape(', '.join(s.get('units') or []))}</p>
<p>За период: {escape(s.get('period') or '')}</p><hr>
<table>{rows}<tr><td><b>ИТОГО, сом</b></td><td class="r"><b>{fmt_som(s['amount'])}</b></td></tr></table><hr>
<p>Способ: {escape(s['method_title'])}</p>
<p>Кассир: {escape(s.get('cashier') or '')}</p>
<p>Остаток долга: {fmt_som(s['debt_after'])} сом</p>{stamp}
<p class="c">Спасибо!</p>
</body></html>"""
