"""Акт сверки (ТЗ §3.5): сальдо = начисления (по сроку оплаты) минус оплаты. >0 — долг арендатора."""
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import Charge, Counter, Payment, ReconciliationAct
from .payments import KIND_TITLES
from .words import fmt_som, som_to_words


def _charge_doc(ch):
    title = KIND_TITLES.get(ch.kind, ch.kind)
    if ch.kind in ("rent", "utilities"):
        return f"{title} за {ch.period:%m.%Y}"
    return ch.description or title


def summary_phrase(closing, company_name, tenant_name) -> str:
    if closing == 0:
        return "Задолженность отсутствует."
    owner = company_name if closing > 0 else tenant_name
    amount = abs(closing)
    return (f"Задолженность в пользу {owner} составляет {fmt_som(amount)} сом "
            f"({som_to_words(amount)}).")


def build_snapshot(company, settings_obj, tenant, date_from, date_to):
    cq = Charge.objects.filter(company=company, tenant=tenant, voided_at__isnull=True)
    pq = Payment.objects.filter(company=company, tenant=tenant, status="posted")
    opening = ((cq.filter(due_date__lt=date_from).aggregate(s=Sum("amount"))["s"] or 0)
               - (pq.filter(paid_at__date__lt=date_from).aggregate(s=Sum("amount"))["s"] or 0))
    rows = []
    for ch in cq.filter(due_date__gte=date_from, due_date__lte=date_to):
        rows.append({"date": ch.due_date.isoformat(), "order": 0, "kind": ch.kind,
                     "document": _charge_doc(ch), "debit": ch.amount, "credit": 0})
    for p in pq.filter(paid_at__date__gte=date_from, paid_at__date__lte=date_to).select_related("receipt"):
        receipt = getattr(p, "receipt", None)
        doc = f"Оплата, чек № {receipt.label}" if receipt else "Оплата"
        rows.append({"date": timezone.localtime(p.paid_at).date().isoformat(), "order": 1, "kind": "payment",
                     "document": doc, "debit": 0, "credit": p.amount})
    rows.sort(key=lambda r: (r["date"], r["order"]))
    bal = opening
    for r in rows:
        bal += r["debit"] - r["credit"]
        r["balance"] = bal
        del r["order"]
    td = sum(r["debit"] for r in rows)
    tc = sum(r["credit"] for r in rows)
    closing = opening + td - tc
    return {
        "company": {"name": settings_obj.org_name, "inn": settings_obj.inn, "address": settings_obj.legal_address},
        "tenant": {"id": str(tenant.id), "name": tenant.name, "inn": tenant.inn},
        "period_from": date_from.isoformat(), "period_to": date_to.isoformat(),
        "opening_balance": opening, "rows": rows, "turnover_debit": td, "turnover_credit": tc,
        "closing_balance": closing,
        "summary": summary_phrase(closing, settings_obj.org_name or "арендодателя", tenant.name),
        "signatures": {"director": settings_obj.director, "chief_accountant": settings_obj.chief_accountant,
                       "tenant": tenant.name},
    }


@transaction.atomic
def create_act(*, company, settings_obj, tenant, date_from, date_to, user):
    snap = build_snapshot(company, settings_obj, tenant, date_from, date_to)
    n = Counter.next(company, "act")
    year = timezone.localdate().year
    return ReconciliationAct.objects.create(
        company=company, tenant=tenant, number=n, label=f"АС-{year}/{n:03d}", period_from=date_from,
        period_to=date_to, opening_balance=snap["opening_balance"], closing_balance=snap["closing_balance"],
        snapshot=snap, created_by=user)


def act_html(act) -> str:
    from django.utils.html import escape
    s = act.snapshot
    body = "".join(
        f"<tr><td>{r['date'][8:10]}.{r['date'][5:7]}.{r['date'][:4]}</td><td>{escape(r['document'])}</td>"
        f"<td class='r'>{fmt_som(r['debit']) if r['debit'] else ''}</td>"
        f"<td class='r'>{fmt_som(r['credit']) if r['credit'] else ''}</td></tr>" for r in s["rows"])
    pf, pt = s["period_from"], s["period_to"]
    d = lambda x: f"{x[8:10]}.{x[5:7]}.{x[:4]}"
    sg = s["signatures"]
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Акт сверки {escape(act.label)}</title>
<style>
@page {{ size: A4; margin: 15mm; }}
body {{ font: 13px/1.4 'Times New Roman', serif; color: #000; }}
h1 {{ font-size: 17px; text-align: center; margin: 0 0 4px; }} .c {{ text-align: center; }} .r {{ text-align: right; }}
table {{ width: 100%; border-collapse: collapse; margin: 12px 0; }}
th, td {{ border: 1px solid #000; padding: 4px 6px; }} th {{ background: #eee; }}
.sign {{ display: flex; justify-content: space-between; margin-top: 40px; gap: 24px; }}
.sign div {{ flex: 1; border-top: 1px solid #000; padding-top: 4px; }}
</style></head><body>
<h1>АКТ СВЕРКИ № {escape(act.label)}</h1>
<p class="c">взаимных расчётов за период с {d(pf)} по {d(pt)}</p>
<p>Между: <b>{escape(s['company']['name'] or '')}</b> и <b>{escape(s['tenant']['name'])}</b></p>
<table><thead><tr><th>Дата</th><th>Документ</th><th>Дебет, сом</th><th>Кредит, сом</th></tr></thead><tbody>
<tr><td colspan="2"><b>Сальдо на начало</b></td><td class="r" colspan="2">{fmt_som(s['opening_balance'])}</td></tr>
{body}
<tr><td colspan="2"><b>Обороты за период</b></td><td class="r"><b>{fmt_som(s['turnover_debit'])}</b></td><td class="r"><b>{fmt_som(s['turnover_credit'])}</b></td></tr>
<tr><td colspan="2"><b>Сальдо на конец</b></td><td class="r" colspan="2"><b>{fmt_som(s['closing_balance'])}</b></td></tr>
</tbody></table>
<p>{escape(s['summary'])}</p>
<div class="sign">
<div>Директор {escape(sg.get('director') or '')}</div>
<div>Бухгалтер {escape(sg.get('chief_accountant') or '')}</div>
<div>Арендатор {escape(sg.get('tenant') or '')}</div>
</div></body></html>"""
