"""ТЗ 29.10 Б-99: аналитика бонусов клиентов. GET main/clients/bonus/summary/."""
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from rest_framework.views import APIView

from apps.main.kassa_views import _ClientScopedMixin
from apps.main.models import Client, ClientBonusTransaction
from apps.main.views import _filter_clients_visible_for_user

BISHKEK = ZoneInfo("Asia/Bishkek")
R = ClientBonusTransaction.Reason
_DEC = DecimalField(max_digits=14, decimal_places=2)
ZERO = Decimal("0.00")


def _m(v):
    return f"{Decimal(v or 0).quantize(Decimal('0.01'))}"


def _day(raw, name):
    d = parse_date((raw or "").strip()) if raw else None
    if raw and d is None:
        raise ValidationError({name: "Ожидается дата YYYY-MM-DD."})
    return d


def apply_bonus_filters(qs, qp):
    """reason, date_from, date_to (включительно, дни по Бишкеку)."""
    reason = (qp.get("reason") or "").strip()
    if reason:
        if reason not in R.values:
            raise ValidationError({"reason": f"Допустимо: {', '.join(R.values)}."})
        qs = qs.filter(reason=reason)
    d_from, d_to = _day(qp.get("date_from"), "date_from"), _day(qp.get("date_to"), "date_to")
    if d_from:
        qs = qs.filter(created_at__gte=datetime.combine(d_from, time.min, tzinfo=BISHKEK))
    if d_to:
        qs = qs.filter(created_at__lt=datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=BISHKEK))
    return qs


def _sum(reason):
    return Coalesce(Sum("delta", filter=Q(reason=reason)), Value(ZERO), output_field=_DEC)


class ClientBonusSummaryAPIView(_ClientScopedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qp = request.query_params
        clients = _filter_clients_visible_for_user(self._filter_qs_company_branch(Client.objects.all()), request.user)
        base = ClientBonusTransaction.objects.filter(client__in=clients)
        txs = apply_bonus_filters(base, qp)

        tot = txs.aggregate(e=_sum(R.EARN), r=_sum(R.REDEEM), x=_sum(R.EXPIRE), a=_sum(R.MANUAL),
                            active=Count("client", distinct=True))
        by_day = {}
        rows = (
            txs.annotate(d=TruncDate("created_at", tzinfo=BISHKEK)).values("d")
            .annotate(e=_sum(R.EARN), r=_sum(R.REDEEM), x=_sum(R.EXPIRE)).order_by("d")
        )
        for row in rows:
            by_day[row["d"]] = row
        bal = clients.aggregate(
            total=Coalesce(Sum("bonus_balance"), Value(ZERO), output_field=_DEC),
            n=Count("id", filter=Q(bonus_balance__gt=0)),
        )
        data = {
            "earned": _m(tot["e"]),
            "redeemed": _m(-tot["r"]),
            "expired": _m(-tot["x"]),
            "adjusted_manual": _m(tot["a"]),
            "balance_total": _m(bal["total"]),
            "clients_with_balance": bal["n"],
            "active_clients": tot["active"],
            "by_day": [
                {"date": d.isoformat(), "earned": _m(v["e"]), "redeemed": _m(-v["r"]), "expired": _m(-v["x"])}
                for d, v in by_day.items()
            ],
        }
        if (qp.get("by") or "").strip() == "branch":
            data.update(self._by_branch(txs, base))
        return Response(data)

    def _by_branch(self, txs, base):
        from apps.users.models import Branch
        names = dict(Branch.objects.filter(
            pk__in=set(base.exclude(branch__isnull=True).values_list("branch_id", flat=True).distinct())
        ).values_list("pk", "name"))
        sid = lambda b: str(b) if b else None
        agg = defaultdict(lambda: {"earned": ZERO, "redeemed": ZERO, "other": ZERO})
        for row in txs.values("branch").annotate(e=_sum(R.EARN), r=_sum(R.REDEEM)):
            a = agg[row["branch"]]
            a["earned"] += row["e"]
            a["redeemed"] += -row["r"]
        # Где потрачено бонусов, начисленных в другом филиале: списание клиента в филиале B делится
        # между филиалами его начислений (за всё время) пропорционально начисленному.
        redeems = list(txs.filter(reason=R.REDEEM).values("client_id", "branch").annotate(s=Sum("delta")))
        clients = {r["client_id"] for r in redeems}
        earned_by = defaultdict(dict)
        for r in base.filter(client_id__in=clients, reason=R.EARN).values("client_id", "branch").annotate(s=Sum("delta")):
            earned_by[r["client_id"]][r["branch"]] = r["s"]
        matrix = defaultdict(lambda: ZERO)
        for r in redeems:
            spent = -r["s"]
            lots = earned_by.get(r["client_id"]) or {}
            total = sum(lots.values(), ZERO)
            if total <= 0:
                matrix[(r["branch"], r["branch"])] += spent
                continue
            for eb, amt in lots.items():
                part = spent * amt / total
                matrix[(eb, r["branch"])] += part
                if eb != r["branch"]:
                    agg[r["branch"]]["other"] += part
        for k in {k for pair in matrix for k in pair}:
            agg[k]  # noqa: B018 - филиалы из матрицы тоже в списке
        return {
            "by_branch": [
                {"branch": sid(b), "branch_name": names.get(b), "earned": _m(v["earned"]),
                 "redeemed": _m(v["redeemed"]), "redeemed_of_other_branches": _m(v["other"])}
                for b, v in agg.items()
            ],
            "matrix": [
                {"earned_branch": sid(e), "spent_branch": sid(s), "amount": _m(a)}
                for (e, s), a in matrix.items()
            ],
        }

