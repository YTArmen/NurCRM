"""GET /api/main/suppliers/payables/ — долги поставщикам одним запросом (ТЗ «Бухгалтер», п. 5)."""
from decimal import Decimal

from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Client, ClientDeal
from apps.main.views import CompanyBranchRestrictedMixin

ZERO = Decimal("0.00")


class SupplierPayablesAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    ?branch=<uuid> (необязательно) → {total, overdue_total, items: [
        {supplier_id, name, debt, overdue, oldest_due_date, next_due_date}]}
    Долг — остаток по сделкам kind=debt поставщика (Client.type=suppliers).
    overdue — неоплаченные части графика с датой < сегодня.
    oldest_due_date — самая старая неоплаченная дата, next_due_date — ближайшая не просроченная.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        today = timezone.localdate()
        deals = ClientDeal.objects.filter(
            client__type=Client.StatusClient.SUPPLIERS, kind=ClientDeal.Kind.DEBT
        ).select_related("client").prefetch_related("installments")
        deals = self._filter_qs_company_branch(deals)
        branch = request.query_params.get("branch")
        if branch:
            deals = deals.filter(branch_id=branch)

        by_supplier = {}
        for deal in deals:
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

        items = sorted(by_supplier.values(), key=lambda r: r["debt"], reverse=True)
        total = sum((r["debt"] for r in items), ZERO)
        overdue_total = sum((r["overdue"] for r in items), ZERO)
        return Response({
            "total": f"{total:.2f}",
            "overdue_total": f"{overdue_total:.2f}",
            "items": [
                {
                    **r,
                    "debt": f"{r['debt']:.2f}",
                    "overdue": f"{r['overdue']:.2f}",
                    "oldest_due_date": r["oldest_due_date"].isoformat() if r["oldest_due_date"] else None,
                    "next_due_date": r["next_due_date"].isoformat() if r["next_due_date"] else None,
                }
                for r in items
            ],
        })
