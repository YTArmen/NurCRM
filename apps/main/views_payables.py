"""GET /api/main/suppliers/payables/ — долги поставщикам одним запросом (ТЗ «Бухгалтер», п. 5).

Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
from decimal import Decimal

from django.db.models import Count, Max
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Client, ClientDeal, SupplierReceipt
from apps.main.views import CompanyBranchRestrictedMixin

ZERO = Decimal("0.00")


class SupplierPayablesAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    ?branch=<uuid> (необязательно) → {total, overdue_total, items: [
        {supplier_id, name, debt, overdue, oldest_due_date, next_due_date}]}
    Долг — остаток по сделкам kind=debt поставщика (Client.type=suppliers).
    overdue — неоплаченные части графика с датой < сегодня.
    oldest_due_date — самая старая неоплаченная дата, next_due_date — ближайшая не просроченная.

    ТЗ Н-11 (только добавлено): в строке prepayment_total (как kpis.prepayment поставщика),
    last_receipt_at, receipts_count; ?ordering=-debt|debt|name|-name|overdue|-overdue|
    last_receipt_at|-last_receipt_at (по умолчанию -debt); ?overdue=true — только с просрочкой;
    ?page / ?page_size — пагинация (без них ответ целиком, как раньше; тогда добавляются
    count/next/previous/results, items остаётся). total и overdue_total — по всему фильтру.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        today = timezone.localdate()
        deals = ClientDeal.objects.filter(
            client__type=Client.StatusClient.SUPPLIERS,
            kind__in=(ClientDeal.Kind.DEBT, ClientDeal.Kind.PREPAYMENT),
        ).select_related("client").prefetch_related("installments")
        deals = self._filter_qs_company_branch(deals)
        branch = request.query_params.get("branch")
        if branch:
            deals = deals.filter(branch_id=branch)

        by_supplier = {}
        prepay = {}
        for deal in deals:
            if deal.kind == ClientDeal.Kind.PREPAYMENT:
                prepay[deal.client_id] = prepay.get(deal.client_id, ZERO) + (deal.amount or ZERO)
                continue
            if deal.prepayment and deal.prepayment > 0:
                prepay[deal.client_id] = prepay.get(deal.client_id, ZERO) + deal.prepayment
            debt = deal.remaining_debt
            if debt <= 0:
                continue
            row = by_supplier.setdefault(deal.client_id, {
                "supplier_id": str(deal.client_id),
                "name": deal.client.full_name or deal.client.llc or "",
                "debt": ZERO, "overdue": ZERO, "oldest_due_date": None, "next_due_date": None,
            })
            row["debt"] += debt
            for inst in deal.installments.all():
                left = inst.remaining_for_period
                if left <= 0 or not inst.due_date:
                    continue
                if inst.due_date < today:
                    row["overdue"] += left
                    if row["oldest_due_date"] is None or inst.due_date < row["oldest_due_date"]:
                        row["oldest_due_date"] = inst.due_date
                elif row["next_due_date"] is None or inst.due_date < row["next_due_date"]:
                    row["next_due_date"] = inst.due_date

        # ТЗ Н-11: приходы поставщиков одним запросом
        rc = {}
        if by_supplier:
            rc_qs = self._filter_qs_company_branch(
                SupplierReceipt.objects.filter(supplier_id__in=list(by_supplier.keys()))
            ).order_by().values("supplier_id").annotate(n=Count("id"), last=Max("created_at"))
            rc = {r["supplier_id"]: r for r in rc_qs}
        for sid, row in by_supplier.items():
            row["prepayment_total"] = prepay.get(sid, ZERO)
            row["receipts_count"] = rc[sid]["n"] if sid in rc else 0
            row["last_receipt_at"] = rc[sid]["last"] if sid in rc else None

        rows = list(by_supplier.values())
        if str(request.query_params.get("overdue") or "").lower() in ("1", "true", "yes"):
            rows = [r for r in rows if r["overdue"] > 0]

        ordering = (request.query_params.get("ordering") or "-debt").strip()
        field = ordering.lstrip("-")
        if field not in ("debt", "name", "overdue", "last_receipt_at", "prepayment_total", "receipts_count"):
            field, ordering = "debt", "-debt"
        reverse = ordering.startswith("-")
        if field == "name":
            rows.sort(key=lambda r: (r["name"] or "").lower(), reverse=reverse)
        elif field == "last_receipt_at":
            # без приходов — в конец при любом направлении
            have = sorted((r for r in rows if r["last_receipt_at"]), key=lambda r: r["last_receipt_at"], reverse=reverse)
            rows = have + [r for r in rows if not r["last_receipt_at"]]
        else:
            rows.sort(key=lambda r: r[field], reverse=reverse)

        total = sum((r["debt"] for r in rows), ZERO)
        overdue_total = sum((r["overdue"] for r in rows), ZERO)

        qp = request.query_params
        paged = bool(qp.get("page") or qp.get("page_size"))
        count = len(rows)
        nxt = prev = None
        if paged:
            try:
                size = max(1, min(int(qp.get("page_size") or 50), 500))
                page = max(1, int(qp.get("page") or 1))
            except ValueError:
                size, page = 50, 1
            rows = rows[(page - 1) * size: page * size]
            def _link(n):
                q = request.query_params.copy()
                q["page"] = str(n)
                return request.build_absolute_uri(request.path + "?" + q.urlencode())
            if page * size < count:
                nxt = _link(page + 1)
            if page > 1:
                prev = _link(page - 1)

        items = [
            {
                **r,
                "debt": f"{r['debt']:.2f}",
                "overdue": f"{r['overdue']:.2f}",
                "prepayment_total": f"{r['prepayment_total']:.2f}",
                "last_receipt_at": r["last_receipt_at"].isoformat() if r["last_receipt_at"] else None,
                "oldest_due_date": r["oldest_due_date"].isoformat() if r["oldest_due_date"] else None,
                "next_due_date": r["next_due_date"].isoformat() if r["next_due_date"] else None,
            }
            for r in rows
        ]
        data = {
            "total": f"{total:.2f}",
            "overdue_total": f"{overdue_total:.2f}",
            "items": items,
        }
        if paged:
            data.update({"count": count, "next": nxt, "previous": prev, "results": items})
        return Response(data)
