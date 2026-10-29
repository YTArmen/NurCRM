"""ТЗ 29.10 Б-100: окупаемость стартовых вложений, инвесторы и их выплаты. Только владелец/администратор."""
import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Min, Sum
from django.utils import timezone
from rest_framework import generics, permissions, serializers
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Investor, InvestorPayout
from apps.utils import _is_owner_like

MAX_MONTHS = 60


def _q(v):
    return Decimal(str(v or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PeriodClosed(APIException):
    status_code = 409

    def __init__(self, closed_until):
        super().__init__({"detail": f"Период до {closed_until.strftime('%d.%m.%Y')} закрыт. Изменения запрещены.",
                          "code": "period_closed", "closed_until": closed_until.isoformat()})


def ensure_finance_open(company, *dates):
    """ТЗ 29.10 Б-100: Company.finance_closed_until — запись инвесторов/стартовых расходов с датой <= этой даты → 409."""
    closed = getattr(company, "finance_closed_until", None)
    if closed is None:
        return
    for d in dates:
        if d is None:
            continue
        if isinstance(d, datetime):
            d = timezone.localtime(d).date() if timezone.is_aware(d) else d.date()
        if d <= closed:
            raise PeriodClosed(closed)


class OwnerOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated):
            return False
        if not _is_owner_like(u):
            raise PermissionDenied("Только владелец или администратор.")
        return True


def _company(user):
    from apps.main.analytics_market import _get_company
    c = _get_company(user)
    if c is None:
        raise PermissionDenied("У пользователя не настроена компания.")
    return c


def _month_bounds(first: date):
    from apps.main.analytics_market import Period
    tz = timezone.get_current_timezone()
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return Period(start=timezone.make_aware(datetime(first.year, first.month, 1), tz),
                  end=timezone.make_aware(datetime(nxt.year, nxt.month, 1), tz))


def month_net(request, company, first: date) -> Decimal:
    """Чистая прибыль (operating_profit из pnl) за месяц."""
    from apps.main.analytics_market import AnalyticsView
    data = AnalyticsView()._pnl(request, company, None, _month_bounds(first))
    return Decimal(data["operating_profit"])


def _add_months(first: date, n: int) -> date:
    m = first.month - 1 + n
    return date(first.year + m // 12, m % 12 + 1, 1)


class StartupPaybackAPIView(APIView):
    """GET main/analytics/startup-payback/?date_from=&date_to="""

    permission_classes = [OwnerOnly]

    def get(self, request):
        from apps.construction.models import CashFlow
        from apps.main.analytics_market import _parse_period_value

        company = _company(request.user)
        today = timezone.localdate()
        startup = CashFlow.objects.filter(company=company, kind="startup", type=CashFlow.Type.EXPENSE).money()
        d_from = _parse_period_value(request.query_params.get("date_from"))[0] if request.query_params.get("date_from") else None
        d_to = _parse_period_value(request.query_params.get("date_to"))[0] if request.query_params.get("date_to") else None
        first_dt = startup.aggregate(m=Min("created_at"))["m"]
        start_day = d_from.date() if d_from else (timezone.localtime(first_dt).date() if first_dt else today)
        end_day = d_to.date() if d_to else today
        if end_day < start_day:
            raise ValidationError({"detail": "Дата начала позже даты конца периода.", "code": "bad_period"})
        first = date(start_day.year, start_day.month, 1)
        last = date(end_day.year, end_day.month, 1)
        n = (last.year - first.year) * 12 + last.month - first.month + 1
        if n > MAX_MONTHS:
            first = _add_months(last, -(MAX_MONTHS - 1))
            n = MAX_MONTHS

        invested = startup.filter(created_at__lt=_month_bounds(last).end).aggregate(s=Sum("amount"))["s"] or Decimal("0")
        by_month, cum = [], Decimal("0")
        for i in range(n):
            m = _add_months(first, i)
            net = month_net(request, company, m)
            cum += net
            by_month.append({"month": m.strftime("%Y-%m"), "net": str(_q(net)), "cumulative": str(_q(cum))})
        invested = _q(invested)
        returned = _q(cum)
        # Вложений нет — возвращать нечего (убыток месяцев не превращается в «осталось вернуть»).
        remaining = _q(max(invested - returned, Decimal("0"))) if invested > 0 else Decimal("0.00")
        percent = float((returned / invested * 100).quantize(Decimal("0.01"))) if invested > 0 else 0.0
        # Прогноз: среднее за последние 3 полных месяца (текущий неполный не берём).
        cur = date(today.year, today.month, 1)
        full = [Decimal(r["net"]) for r in by_month if r["month"] < cur.strftime("%Y-%m")][-3:]
        avg = _q(sum(full) / len(full)) if full else Decimal("0.00")
        months_to = None
        forecast = None
        if remaining == 0 and invested > 0:
            months_to, forecast = 0.0, today.isoformat()
        elif avg > 0 and invested > 0:
            months_to = float((remaining / avg).quantize(Decimal("0.1")))
            forecast = (today + timedelta(days=round(months_to * 30.44))).isoformat()
        return Response({
            "invested": str(invested), "returned": str(returned), "remaining": str(remaining),
            "percent_back": percent, "avg_monthly_net": str(avg),
            "months_to_payback": months_to, "forecast_date": forecast, "by_month": by_month,
        })


def _audit(request, company, action, obj, name, before=None, after=None):
    from apps.construction.audit import log_change
    log_change(company=company, user=request.user, action=action, object_type=obj, object_id=getattr(request, "_aid", ""),
               object_name=name, before=before, after=after, request=request)


class InvestorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Investor
        fields = ("id", "name", "phone", "amount", "date", "terms", "version", "effective_from", "created_at", "updated_at")
        read_only_fields = ("id", "version", "created_at", "updated_at")

    def validate_terms(self, v):
        if not isinstance(v, dict):
            raise serializers.ValidationError("Ожидается объект JSON.")
        return v


class InvestorListCreateView(generics.ListCreateAPIView):
    permission_classes = [OwnerOnly]
    serializer_class = InvestorSerializer

    def get_queryset(self):
        return Investor.objects.filter(company=_company(self.request.user))

    def perform_create(self, serializer):
        company = _company(self.request.user)
        obj = serializer.save(company=company)
        self.request._aid = obj.pk
        _audit(self.request, company, "create", "investor", obj.name, after={"amount": str(obj.amount)})


class InvestorDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [OwnerOnly]
    serializer_class = InvestorSerializer

    def get_queryset(self):
        return Investor.objects.filter(company=_company(self.request.user))

    def perform_update(self, serializer):
        before = {"amount": str(serializer.instance.amount), "terms": str(serializer.instance.terms)}
        obj = serializer.save(version=serializer.instance.version + 1)  # история условий: версия растёт
        self.request._aid = obj.pk
        _audit(self.request, obj.company, "update", "investor", obj.name, before=before,
               after={"amount": str(obj.amount), "terms": str(obj.terms)})

    def perform_destroy(self, instance):
        if instance.payouts.exists():
            raise ValidationError({"detail": "У инвестора есть выплаты, удалить нельзя."})
        self.request._aid = instance.pk
        _audit(self.request, instance.company, "delete", "investor", instance.name)
        instance.delete()


class PayoutSerializer(serializers.ModelSerializer):
    investor_name = serializers.CharField(source="investor.name", read_only=True)
    accrued = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)

    class Meta:
        model = InvestorPayout
        fields = ("id", "investor", "investor_name", "month", "accrued", "tax", "paid", "paid_date", "marked_by", "created_at")
        read_only_fields = ("id", "marked_by", "created_at")

    def validate_month(self, v):
        return date(v.year, v.month, 1)

    def validate(self, attrs):
        req = self.context["request"]
        company = _company(req.user)
        inv = attrs.get("investor") or self.instance.investor
        if inv.company_id != company.pk:
            raise ValidationError({"investor": "Инвестор не найден."})
        month = attrs.get("month") or self.instance.month
        net = month_net(req, company, month)
        if "accrued" not in attrs:
            if self.instance is not None:
                accrued = self.instance.accrued
            else:
                pct = Decimal(str((inv.terms or {}).get("percent", 0) or 0))
                accrued = _q(max(net, Decimal("0")) * pct / 100)
            attrs["accrued"] = accrued
        accrued = _q(attrs["accrued"])
        tax = _q(attrs.get("tax", getattr(self.instance, "tax", 0)))
        paid = _q(attrs.get("paid", getattr(self.instance, "paid", 0)))
        if min(accrued, tax, paid) < 0:
            raise ValidationError({"detail": "Суммы не могут быть отрицательными."})
        if paid > accrued:
            raise ValidationError({"paid": "Выплачено больше, чем начислено."})
        others = InvestorPayout.objects.filter(company=company, month=month)
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        already = others.aggregate(s=Sum("accrued"))["s"] or Decimal("0")
        if already + accrued > max(net, Decimal("0")):
            raise ValidationError({
                "accrued": f"За месяц нельзя начислить больше чистой прибыли ({_q(net)}); уже начислено {_q(already)}.",
                "code": "payout_over_profit",
            })
        attrs["month"], attrs["accrued"], attrs["tax"], attrs["paid"] = month, accrued, tax, paid
        return attrs


class PayoutListCreateView(generics.ListCreateAPIView):
    permission_classes = [OwnerOnly]
    serializer_class = PayoutSerializer

    def get_queryset(self):
        qs = InvestorPayout.objects.filter(company=_company(self.request.user)).select_related("investor")
        inv = self.request.query_params.get("investor")
        if inv:
            qs = qs.filter(investor_id=inv)
        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        company = _company(self.request.user)
        ensure_finance_open(company, serializer.validated_data["month"])
        obj = serializer.save(company=company, marked_by=self.request.user)
        self.request._aid = obj.pk
        _audit(self.request, company, "create", "investor_payout", obj.investor.name,
               after={"month": str(obj.month), "accrued": str(obj.accrued), "paid": str(obj.paid)})


class PayoutDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [OwnerOnly]
    serializer_class = PayoutSerializer

    def get_queryset(self):
        return InvestorPayout.objects.filter(company=_company(self.request.user)).select_related("investor")

    def perform_update(self, serializer):
        i = serializer.instance
        ensure_finance_open(i.company, i.month, serializer.validated_data.get("month"))
        before = {"accrued": str(i.accrued), "tax": str(i.tax), "paid": str(i.paid)}
        obj = serializer.save(marked_by=self.request.user)
        self.request._aid = obj.pk
        _audit(self.request, obj.company, "update", "investor_payout", obj.investor.name, before=before,
               after={"accrued": str(obj.accrued), "tax": str(obj.tax), "paid": str(obj.paid)})

    def perform_destroy(self, instance):
        ensure_finance_open(instance.company, instance.month)
        self.request._aid = instance.pk
        _audit(self.request, instance.company, "delete", "investor_payout", instance.investor.name,
               before={"accrued": str(instance.accrued), "paid": str(instance.paid)})
        instance.delete()
