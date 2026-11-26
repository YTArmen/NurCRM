"""П2-2 (14.11.2026, p2214): готовая сводка отчётов для кассы, программы владельца и мобилки.

GET analytics/summary/?from=YYYY-MM-DD&to=YYYY-MM-DD[&compare=prev_period|prev_year][&group_by=day|hour|weekday|cashier|branch][&branch=]
→ {from, to, group_by, totals:{…}, series:[{key, label, …}], compare:{from, to, totals, series} | null}
Показатели: revenue (выручка чеков), checks, avg_check, returns (возвраты по чеку и без чека), net (revenue − returns),
cost (себестоимость по закупочной цене на момент продажи), profit (revenue − cost).
Выручка — чеки «оплачен», «в долг», «частичный возврат» и отменённые возвратом (иначе возврат вычитался бы дважды).
Права: владелец/админ или «Аналитика» (can_view_analytics). Период — не длиннее 366 дней.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import Coalesce, ExtractHour, ExtractIsoWeekDay, TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

Z = Decimal("0.00")
GROUPS = ("day", "hour", "weekday", "cashier", "branch")
MONEY = DecimalField(max_digits=18, decimal_places=2)


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def _m(v):
    return format(Decimal(v or 0).quantize(Decimal("0.01")), "f")


def _bounds(d1, d2):
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(d1, time.min), tz),
            timezone.make_aware(datetime.combine(d2 + timedelta(days=1), time.min), tz))


def _sales_qs(company, a, b, branch):
    from apps.main.models import Sale

    qs = Sale.objects.filter(company=company, created_at__gte=a, created_at__lt=b).filter(
        Q(status__in=("paid", "debt", "partially_returned")) | Q(status="canceled", returns__isnull=False)).distinct()
    if branch:
        qs = qs.filter(branch_id=branch)
    qs = qs.exclude(is_test=True)  # П0-4 К-18
    return qs


def _key_expr(group, field="created_at"):
    tz = timezone.get_current_timezone()
    if group == "day":
        return TruncDate(field, tzinfo=tz)
    if group == "hour":
        return ExtractHour(field, tzinfo=tz)
    if group == "weekday":
        return ExtractIsoWeekDay(field, tzinfo=tz)
    return None


def _group_field(group, kind):
    if group == "cashier":
        return "user_id"
    if group == "branch":
        return {"sale": "branch_id", "sr": "sale__branch_id", "rw": "branch_id"}[kind]
    return None


def build(company, d1, d2, group, branch=None):
    from apps.main.models import ReturnWithoutSale, SaleItem, SaleReturn

    a, b = _bounds(d1, d2)
    sales = _sales_qs(company, a, b, branch)
    srs = SaleReturn.objects.filter(company=company, created_at__gte=a, created_at__lt=b).exclude(status="voided")
    srs = srs.exclude(sale__is_test=True)  # П0-4 К-18
    rws = ReturnWithoutSale.objects.filter(company=company, created_at__gte=a, created_at__lt=b)
    if branch:
        srs, rws = srs.filter(sale__branch_id=branch), rws.filter(branch_id=branch)
    cost_expr = ExpressionWrapper(
        Coalesce(F("purchase_price_snapshot"), Value(Z)) * Coalesce(F("base_quantity"), F("quantity")), output_field=MONEY)
    items = SaleItem.objects.filter(sale__in=sales.values("id"))

    def agg(qs, kind, keyname, value_fields):
        expr = _key_expr(group, "sale__created_at" if kind == "item" else "created_at") if group in ("day", "hour", "weekday") else None
        gf = _group_field(group, {"item": "sale", "sale": "sale", "sr": "sr", "rw": "rw"}[kind]) if group in ("cashier", "branch") else None
        if kind == "item" and gf:
            gf = "sale__" + gf
        if expr is not None:
            qs = qs.annotate(k=expr).values("k")
        elif gf:
            qs = qs.values(k=F(gf))
        else:
            return {None: qs.aggregate(**value_fields)}
        return {r["k"]: r for r in qs.annotate(**value_fields)}

    s = agg(sales, "sale", "k", {"rev": Coalesce(Sum("total"), Value(Z), output_field=MONEY), "cnt": Count("id")})
    c = agg(items, "item", "k", {"cost": Coalesce(Sum(cost_expr), Value(Z), output_field=MONEY)})
    r1 = agg(srs, "sr", "k", {"ret": Coalesce(Sum("returned_amount"), Value(Z), output_field=MONEY)})
    r2 = agg(rws, "rw", "k", {"ret": Coalesce(Sum("total"), Value(Z), output_field=MONEY)})
    keys = set(s) | set(c) | set(r1) | set(r2)

    def row(k):
        rev = (s.get(k) or {}).get("rev") or Z
        cnt = (s.get(k) or {}).get("cnt") or 0
        cost = (c.get(k) or {}).get("cost") or Z
        ret = ((r1.get(k) or {}).get("ret") or Z) + ((r2.get(k) or {}).get("ret") or Z)
        return {"revenue": _m(rev), "checks": cnt, "avg_check": _m(rev / cnt if cnt else Z), "returns": _m(ret),
                "net": _m(rev - ret), "cost": _m(cost), "profit": _m(rev - cost)}

    if group is None:
        return row(None), []
    # полные ряды без пропусков для времени
    if group == "day":
        order = [d1 + timedelta(days=i) for i in range((d2 - d1).days + 1)]
    elif group == "hour":
        order = list(range(24))
    elif group == "weekday":
        order = list(range(1, 8))
    else:
        order = sorted((k for k in keys if k is not None), key=str) + ([None] if None in keys else [])
    labels = {}
    if group == "cashier":
        from apps.users.models import User

        for u in User.objects.filter(pk__in=[k for k in order if k]):
            labels[u.pk] = (f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email or str(u.pk))
    if group == "branch":
        from apps.users.models import Branch

        for br in Branch.objects.filter(pk__in=[k for k in order if k]):
            labels[br.pk] = br.name
    series = []
    for k in order:
        lab = k.isoformat() if hasattr(k, "isoformat") else (labels.get(k) if group in ("cashier", "branch") else k)
        series.append({"key": (k.isoformat() if hasattr(k, "isoformat") else (str(k) if k is not None else None)),
                       "label": lab if lab is not None else "—", **row(k)})
    if group in ("cashier", "branch"):
        series.sort(key=lambda x: -Decimal(x["revenue"]))
    return build(company, d1, d2, None, branch)[0], series


class AnalyticsSummaryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.users.permissions_flags import has_any_flag, is_privileged

        user = request.user
        if not (is_privileged(user) or getattr(user, "role", None) in ("rop", "supervisor") or has_any_flag(user, ("can_view_analytics",))):
            return _err("permission_denied", "Нет права «Аналитика».", 403, permission="can_view_analytics", required="can_view_analytics")
        qp = request.query_params
        d1, d2 = parse_date(qp.get("from") or ""), parse_date(qp.get("to") or "")
        if d1 is None or d2 is None or d2 < d1:
            return _err("validation_error", "Укажите from и to (YYYY-MM-DD), from ≤ to.", fields={"from": ["YYYY-MM-DD."]})
        if (d2 - d1).days > 366:
            return _err("validation_error", "Период не длиннее 366 дней.", fields={"to": ["Не длиннее 366 дней."]})
        group = (qp.get("group_by") or "").strip() or None
        if group and group not in GROUPS:
            return _err("validation_error", "group_by: " + ", ".join(GROUPS) + ".", fields={"group_by": list(GROUPS)}, allowed=list(GROUPS))
        compare = (qp.get("compare") or "").strip() or None
        if compare and compare not in ("prev_period", "prev_year"):
            return _err("validation_error", "compare: prev_period или prev_year.", fields={"compare": ["prev_period, prev_year"]})
        company = _company(user)
        branch = (qp.get("branch") or "").strip() or None
        totals, series = build(company, d1, d2, group, branch)
        out = {"from": d1.isoformat(), "to": d2.isoformat(), "group_by": group, "totals": totals, "series": series, "compare": None}
        if compare:
            if compare == "prev_period":
                n = (d2 - d1).days + 1
                c1, c2 = d1 - timedelta(days=n), d1 - timedelta(days=1)
            else:
                def back(d):
                    try:
                        return d.replace(year=d.year - 1)
                    except ValueError:
                        return d.replace(year=d.year - 1, day=28)
                c1, c2 = back(d1), back(d2)
            ct, cs = build(company, c1, c2, group, branch)
            out["compare"] = {"kind": compare, "from": c1.isoformat(), "to": c2.isoformat(), "totals": ct, "series": cs}
        return Response(out)
