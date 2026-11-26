"""П2-4 (14.11.2026, p2414): акт сверки с поставщиком.

GET suppliers/<id>/statement/?from=YYYY-MM-DD&to=YYYY-MM-DD
→ {supplier:{id,name}, from, to, opening, rows:[{date, kind, doc, debit, credit, balance, note}], turnover:{debit, credit},
   closing, current_debt, difference}
Знак: debit — наш долг поставщику вырос (приход), credit — уменьшился (оплата при приходе, оплата долга, зачёт возврата).
balance > 0 — мы должны поставщику. current_debt — долг по сделкам сейчас (как suppliers/payables/); difference ≠ 0
означает, что в истории есть операции, не попавшие в акт (старые ручные правки долга) — для бухгалтера.
Права: владелец/админ или «Поставщики», «Закупки» или «Аналитика».
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

Z = Decimal("0.00")
FLAGS = ("can_view_market_supplier", "can_view_market_procurement", "can_view_analytics")


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def _q(v):
    return Decimal(v or 0).quantize(Decimal("0.01"))


def _num(obj):
    try:
        ni = obj.number_info
        return (ni or {}).get("number_display") if isinstance(ni, dict) else None
    except Exception:  # noqa: BLE001
        return None


def events(company, supplier, until=None):
    """Все движения расчётов с поставщиком (до until, не включая) — [(datetime, kind, doc, debit, credit, note)]."""
    from apps.main.models import ClientDeal, DealPayment, SupplierReceipt, SupplierReturn
    from apps.main.views import _receipt_payment_info

    out = []
    rq = SupplierReceipt.objects.filter(company=company, supplier=supplier).prefetch_related("items")
    if until is not None:
        rq = rq.filter(created_at__lt=until)
    receipts = list(rq)
    info = _receipt_payment_info(receipts)
    for r in receipts:
        inf = info.get(str(r.id)) or {}
        from apps.main.views import receipt_item_amount

        total = _q(sum((receipt_item_amount(it) for it in r.items.all()), Decimal("0")))
        doc = "Приход " + (_num(r) or (r.document_number or f"от {timezone.localtime(r.created_at):%d.%m.%Y}"))
        out.append((r.created_at, "receipt", doc, total, Z, (inf.get("payment_type") or "")))
        paid = _q(inf.get("paid_amount"))
        if paid > 0:
            out.append((r.created_at, "receipt_payment", doc, Z, paid, "оплата при приходе"))
    pq = DealPayment.objects.filter(company=company, deal__client=supplier, deal__kind=ClientDeal.Kind.DEBT, kind="pay")
    if until is not None:
        pq = pq.filter(created_at__lt=until)
    for p in pq.select_related("deal"):
        when = p.created_at
        out.append((when, "payment", "Оплата долга", Z, _q(p.amount), p.payment_method or ""))
    sq = SupplierReturn.objects.filter(company=company, supplier=supplier, status="posted")
    if until is not None:
        sq = sq.filter(created_at__lt=until)
    for s in sq:
        doc = "Возврат поставщику " + (_num(s) or "")
        if s.compensation == "debt_offset":
            off = _q(sum((Decimal(str(a.get("amount") or 0)) for a in (s.debt_allocations or [])), Decimal("0")))
            out.append((s.created_at, "return_offset", doc.strip(), Z, off, "зачёт в счёт долга"))
        else:
            out.append((s.created_at, "return", doc.strip(), Z, Z,
                        "деньги вернули в кассу" if s.compensation == "cash" else "без движения денег"))
    out.sort(key=lambda e: (e[0], {"receipt": 0, "receipt_payment": 1}.get(e[1], 2)))
    return out


class SupplierStatementAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, supplier_id):
        from apps.main.models import Client
        from apps.main.views import _supplier_debt_total
        from apps.users.permissions_flags import has_any_flag, is_privileged

        user = request.user
        if not (is_privileged(user) or has_any_flag(user, FLAGS)):
            return _err("permission_denied", "Нет права «Поставщики».", 403, permission=FLAGS[0], required=FLAGS[0], required_any=list(FLAGS))
        company = _company(user)
        sup = get_object_or_404(Client, pk=supplier_id, company=company)
        qp = request.query_params
        d1, d2 = parse_date(qp.get("from") or ""), parse_date(qp.get("to") or "")
        if d1 is None or d2 is None or d2 < d1:
            return _err("validation_error", "Укажите from и to (YYYY-MM-DD), from ≤ to.", fields={"from": ["YYYY-MM-DD."]})
        if (d2 - d1).days > 731:
            return _err("validation_error", "Период не длиннее двух лет.", fields={"to": ["Не длиннее 2 лет."]})
        tz = timezone.get_current_timezone()
        a = timezone.make_aware(datetime.combine(d1, time.min), tz)
        b = timezone.make_aware(datetime.combine(d2 + timedelta(days=1), time.min), tz)
        evs = events(company, sup, until=b)
        bal = Z
        rows, deb, cred = [], Z, Z
        for when, kind, doc, dt, ct, note in evs:
            if when < a:
                bal += dt - ct
                continue
            if not rows:
                opening = bal
            bal += dt - ct
            deb += dt
            cred += ct
            rows.append({"date": timezone.localtime(when).date().isoformat(), "at": when.isoformat(), "kind": kind, "doc": doc,
                         "debit": format(dt, "f"), "credit": format(ct, "f"), "balance": format(_q(bal), "f"), "note": note})
        if not rows:
            opening = bal
        all_bal = _q(sum((e[3] - e[4] for e in events(company, sup)), Z))
        current = _supplier_debt_total(company, sup.pk)
        return Response({
            "supplier": {"id": str(sup.pk), "name": sup.full_name or getattr(sup, "llc", "") or ""},
            "from": d1.isoformat(), "to": d2.isoformat(),
            "opening": format(_q(opening), "f"), "rows": rows,
            "turnover": {"debit": format(_q(deb), "f"), "credit": format(_q(cred), "f")},
            "closing": format(_q(bal), "f"),
            "current_debt": format(current, "f"),
            "difference": format(_q(current - all_bal), "f"),
        })
