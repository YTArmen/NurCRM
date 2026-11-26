"""П0-4 К-1: чтение журнала остатков (main_stockledger).

GET stock/ledger/?product=&source=&from=&to=&page=&page_size=   — строки журнала, новые сверху
GET products/<id>/ledger/?from=&to=&page=&page_size=           — то же по товару + текущий остаток
GET stock/balance/?at=ISO[&product=][&page=&page_size=]          — остаток на момент at
GET stock/turnover/?from=&to=[&product=][&page=&page_size=]      — оборот: начало, приход, расход, конец
Журнал ведётся с момента включения (ledger_since): для более ранней даты complete=false.
"""
from datetime import datetime, time
from decimal import Decimal

from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

Z = Decimal("0")


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _owner_like(user):
    from apps.main.pos_serializers import _is_owner_like

    return _is_owner_like(user)


def _err(detail, field):
    return Response({"code": "validation_error", "detail": detail, "fields": {field: [detail]}}, status=400)


def _dt(raw, *, end=False):
    """ISO-дата/время -> aware datetime; дата без времени — начало (или конец) дня."""
    if not raw:
        return None
    day = parse_date(raw) if len(raw) == 10 else None
    if day is not None:
        d = datetime.combine(day, time.max if end else time.min)
    else:
        d = parse_datetime(raw)
        if d is None:
            raise ValueError(raw)
    return timezone.make_aware(d) if timezone.is_naive(d) else d


def _page(request):
    try:
        page = max(1, int(request.query_params.get("page") or 1))
        size = min(500, max(1, int(request.query_params.get("page_size") or 100)))
    except ValueError:
        page, size = 1, 100
    return page, size


def _q3(v):
    return format(Decimal(v or 0).quantize(Decimal("0.001")), "f")


def ledger_since(company_id):
    from apps.main.models import StockLedger

    row = StockLedger.objects.filter(company_id=company_id).exclude(kind="opening").order_by("id").values("created_at").first()
    op = StockLedger.objects.filter(company_id=company_id, kind="opening").order_by("id").values("occurred_at").first()
    cands = [r for r in ((row or {}).get("created_at"), (op or {}).get("occurred_at")) if r]
    return min(cands) if cands else None


def _row(r, show_cost, names):
    return {
        "id": r.id, "product_id": str(r.product_id), "product_name": names.get(r.product_id),
        "kind": r.kind, "qty_before": _q3(r.qty_before), "qty_after": _q3(r.qty_after), "qty_delta": _q3(r.qty_delta),
        "source": r.source, "method": r.method, "doc_id": r.doc_id,
        "user_id": str(r.user_id) if r.user_id else None, "client": r.client, "request_id": r.request_id,
        "unit_cost": (format(r.unit_cost, "f") if r.unit_cost is not None else None) if show_cost else None,
        "branch_id": str(r.branch_id) if r.branch_id else None,
        "occurred_at": r.occurred_at.isoformat(), "created_at": r.created_at.isoformat(),
    }


def _names(ids):
    from apps.main.models import Product

    return dict(Product.objects.filter(id__in=set(ids)).values_list("id", "name"))


class _Base(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _qs(self, request):
        from apps.main.models import StockLedger

        company = _company(request.user)
        qs = StockLedger.objects.filter(company_id=getattr(company, "id", None))
        qp = request.query_params
        if qp.get("product"):
            qs = qs.filter(product_id=qp["product"])
        if qp.get("source"):
            qs = qs.filter(source=qp["source"])
        if qp.get("kind"):
            qs = qs.filter(kind=qp["kind"])
        f, t = _dt(qp.get("from")), _dt(qp.get("to"), end=True)
        if f:
            qs = qs.filter(occurred_at__gte=f)
        if t:
            qs = qs.filter(occurred_at__lte=t)
        return company, qs

    def _list(self, request, qs, extra=None):
        page, size = _page(request)
        total = qs.count()
        rows = list(qs.order_by("-id")[(page - 1) * size: page * size])
        names = _names(r.product_id for r in rows)
        show = _owner_like(request.user)
        return Response({**(extra or {}), "count": total, "page": page, "page_size": size,
                         "results": [_row(r, show, names) for r in rows]})


class StockLedgerListAPIView(_Base):
    def get(self, request):
        try:
            company, qs = self._qs(request)
        except (ValueError, Exception) as e:  # noqa: BLE001 — неверный uuid/дата
            return _err(f"Неверный параметр: {e}", "query")
        return self._list(request, qs, {"ledger_since": (lambda s: s.isoformat() if s else None)(ledger_since(company.id))})


class ProductHistoryAPIView(_Base):
    def get(self, request, pk):
        from apps.main.models import Product

        company = _company(request.user)
        product = get_object_or_404(Product, pk=pk, company=company)
        try:
            _, qs = self._qs(request)
        except Exception as e:  # noqa: BLE001
            return _err(f"Неверный параметр: {e}", "query")
        qs = qs.filter(product_id=product.id)
        agg = qs.model.objects.filter(product_id=product.id).aggregate(s=Sum("qty_delta"))
        has_opening = qs.model.objects.filter(product_id=product.id, kind="opening").exists()
        return self._list(request, qs, {
            "product_id": str(product.id), "product_name": product.name, "quantity": _q3(product.quantity),
            "ledger_sum": _q3(agg["s"]), "consistent": has_opening and Decimal(agg["s"] or 0) == Decimal(product.quantity or 0),
        })


def _products_page(request, company):
    from apps.main.models import Product

    qs = Product.objects.filter(company=company)
    if request.query_params.get("product"):
        qs = qs.filter(id=request.query_params["product"])
    page, size = _page(request)
    total = qs.count()
    return total, page, size, list(qs.order_by("name", "id").values("id", "name", "quantity")[(page - 1) * size: page * size])


def _sums(company, ids, *, after=None, lo=None, hi=None):
    from apps.main.models import StockLedger

    qs = StockLedger.objects.filter(company_id=company.id, product_id__in=ids).exclude(kind="opening")
    if after is not None:
        qs = qs.filter(occurred_at__gt=after)
    if lo is not None:
        qs = qs.filter(occurred_at__gte=lo)
    if hi is not None:
        qs = qs.filter(occurred_at__lte=hi)
    return {r["product_id"]: r for r in qs.values("product_id").annotate(
        net=Sum("qty_delta"), inc=Sum("qty_delta", filter=Q(qty_delta__gt=0)), out=Sum("qty_delta", filter=Q(qty_delta__lt=0)))}


class StockBalanceAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request.user)
        try:
            at = _dt(request.query_params.get("at"), end=True) or timezone.now()
            total, page, size, prods = _products_page(request, company)
        except Exception as e:  # noqa: BLE001
            return _err(f"Неверный параметр: {e}", "at")
        after = _sums(company, [p["id"] for p in prods], after=at)
        since = ledger_since(company.id)
        res = [{"product_id": str(p["id"]), "name": p["name"], "quantity_now": _q3(p["quantity"]),
                "quantity_at": _q3(Decimal(p["quantity"] or 0) - Decimal((after.get(p["id"]) or {}).get("net") or 0))}
               for p in prods]
        return Response({"at": at.isoformat(), "ledger_since": since.isoformat() if since else None,
                         "complete": bool(since and at >= since), "count": total, "page": page, "page_size": size,
                         "results": res})


class StockTurnoverAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request.user)
        try:
            lo, hi = _dt(request.query_params.get("from")), _dt(request.query_params.get("to"), end=True)
            if lo is None or hi is None:
                return _err("Укажите from и to.", "from")
            total, page, size, prods = _products_page(request, company)
        except Exception as e:  # noqa: BLE001
            return _err(f"Неверный параметр: {e}", "from")
        ids = [p["id"] for p in prods]
        after_hi = _sums(company, ids, after=hi)
        inside = _sums(company, ids, lo=lo, hi=hi)
        res = []
        for p in prods:
            closing = Decimal(p["quantity"] or 0) - Decimal((after_hi.get(p["id"]) or {}).get("net") or 0)
            r = inside.get(p["id"]) or {}
            inc, out = Decimal(r.get("inc") or 0), Decimal(r.get("out") or 0)
            res.append({"product_id": str(p["id"]), "name": p["name"], "opening": _q3(closing - inc - out),
                        "income": _q3(inc), "outcome": _q3(-out), "closing": _q3(closing)})
        since = ledger_since(company.id)
        return Response({"from": lo.isoformat(), "to": hi.isoformat(), "ledger_since": since.isoformat() if since else None,
                         "complete": bool(since and lo >= since), "count": total, "page": page, "page_size": size,
                         "results": res})
