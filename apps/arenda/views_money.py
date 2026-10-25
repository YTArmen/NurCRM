"""Этап 2 «Деньги» (ТЗ §3.2, §3.4, §3.5): оплаты, чеки, взыскания, карточка арендатора, акты сверки."""
import calendar
import uuid
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from apps.users.models import Branch, User
from core.exceptions import ApiError

from . import audit
from .branch_filter import apply_branch
from .models import Charge, Payment, ReconciliationAct, Receipt, Reminder, Remark, Tenant
from .permissions import ArendaSection, deny
from .serializers_money import ActInSerializer, PaymentInSerializer, RemarkInSerializer, StornoSerializer
from .services import acts as acts_srv
from .services import payments as pay_srv
from .services import stats
from .services.charges import month_start
from .services.debt import DebtInfo, calc_penalty
from .services.words import fmt_som
from .views import CtxMixin, ManualPaging, lease_payload
from .views_docs import IgnoreFormatNegotiation


# ------------------------------------------------------------------ общие помощники


def _tenant_or_404(ctx, pk):
    t = Tenant.objects.filter(pk=pk, company=ctx.company, archived_at__isnull=True).first()
    if not t:
        raise ApiError("not_found", "Арендатор не найден.", status_code=404)
    return t


def _check_branch(ctx, branch_id):
    allowed = ctx.allowed_branch_ids()
    if allowed is not None and branch_id not in allowed:
        raise ApiError("permission_denied", "Нет доступа к филиалу.", status_code=403)


def _resolve_branch(ctx, branch_id, tenant, required):
    if branch_id:
        if not Branch.objects.filter(id=branch_id, company=ctx.company).exists():
            raise ApiError("not_found", "Филиал не найден.", status_code=404)
        _check_branch(ctx, branch_id)
        return branch_id
    if tenant is not None:
        b = pay_srv.tenant_branch_id(tenant)
        if b:
            _check_branch(ctx, b)
            return b
    if required or tenant is not None:
        raise ValidationError({"branch": ["Укажите филиал."]})
    return None


def _parse_date(raw, name):
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError({name: ["Формат даты YYYY-MM-DD."]})


def _parse_month(raw):
    if not raw:
        return None
    try:
        y, m = raw.split("-")
        return date(int(y), int(m), 1)
    except (ValueError, TypeError):
        raise ValidationError({"month": ["Формат месяца YYYY-MM."]})


def _brief_user(u):
    return pay_srv.user_name(u)


def payment_payload(p):
    receipt = getattr(p, "receipt", None)
    local = timezone.localtime(p.paid_at)
    cat = pay_srv.category_of(p.description)
    return {
        "id": str(p.id), "kind": "in", "branch": str(p.branch_id),
        "tenant": str(p.tenant_id) if p.tenant_id else None,
        "tenant_name": p.tenant.name if p.tenant_id else None,
        "amount": p.amount, "method": p.method, "paid_at": local.isoformat(), "date": local.date().isoformat(),
        "cashier_name": _brief_user(p.cashier), "description": p.description, "category": cat,
        "add_penalty": p.add_penalty, "status": p.status, "reversal_reason": p.reversal_reason,
        "receipt_id": str(receipt.id) if receipt else None, "receipt_label": receipt.label if receipt else None,
    }


def receipt_payload(r, full=True):
    p = r.payment
    out = {"id": str(r.id), "number": r.number, "label": r.label, "payment_id": str(p.id),
           "tenant": str(p.tenant_id), "amount": p.amount, "status": p.status,
           "paid_at": r.snapshot.get("paid_at")}
    if full:
        out["snapshot"] = r.snapshot
        out["text"] = pay_srv.receipt_text(r)
    return out


def act_payload(a, full=True):
    out = {"id": str(a.id), "number": a.number, "label": a.label, "tenant": str(a.tenant_id),
           "tenant_name": a.tenant.name, "period_from": a.period_from.isoformat(),
           "period_to": a.period_to.isoformat(), "opening_balance": a.opening_balance,
           "closing_balance": a.closing_balance, "created_at": timezone.localtime(a.created_at).isoformat()}
    if full:
        s = a.snapshot
        out.update({"rows": s["rows"], "turnover_debit": s["turnover_debit"],
                    "turnover_credit": s["turnover_credit"], "summary": s["summary"],
                    "signatures": s["signatures"], "snapshot": s})
    return out


# ------------------------------------------------------------------ оплаты


class PaymentsView(ManualPaging, CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("cash")]

    def get(self, request):
        ctx = self.ctx
        ids = self.branch_ids()
        p = request.query_params
        day = _parse_date(p.get("date"), "date")
        month = _parse_month(p.get("month"))
        qs = Payment.objects.filter(company=ctx.company).select_related("tenant", "cashier", "receipt")
        qs = apply_branch(qs, ids)
        if p.get("tenant"):
            try:
                qs = qs.filter(tenant_id=uuid.UUID(p["tenant"]))
            except ValueError:
                raise ValidationError({"tenant": ["Некорректный идентификатор."]})
        today = timezone.localdate()
        if day:
            qs = qs.filter(paid_at__date=day)
        elif month:
            qs = qs.filter(paid_at__date__gte=month,
                           paid_at__date__lte=month.replace(day=calendar.monthrange(month.year, month.month)[1]))
        # итоги дня и месяца (проведённые), независимо от пагинации
        base = apply_branch(Payment.objects.filter(company=ctx.company, status="posted"), ids)
        d0 = day or today
        m0 = month or month_start(day or today)
        m1 = m0.replace(day=calendar.monthrange(m0.year, m0.month)[1])

        def totals(q):
            by = {m: 0 for m in pay_srv.METHOD_TITLES}
            for r in q.values("method").annotate(s=Sum("amount")):
                by[r["method"]] = int(r["s"] or 0)
            return {"in": sum(by.values()), "by_method": by}

        extra = {
            "balances": pay_srv.balances(ctx.company, ids),
            "totals": {"day": {"date": d0.isoformat(), **totals(base.filter(paid_at__date=d0))},
                       "month": {"month": f"{m0:%Y-%m}",
                                 **totals(base.filter(paid_at__date__gte=m0, paid_at__date__lte=m1))}},
        }
        page = self.paginate_queryset(list(qs))
        resp = self.get_paginated_response([payment_payload(x) for x in page])
        data = dict(resp.data)
        res = data.pop("results")
        data.update(extra)
        data["results"] = res
        resp.data = data
        return resp

    @idempotent
    def post(self, request):
        ctx = self.ctx
        ser = PaymentInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        tenant = _tenant_or_404(ctx, d["tenant"]) if d.get("tenant") else None
        branch_id = _resolve_branch(ctx, d.get("branch"), tenant, required=tenant is None)
        today = timezone.localdate()
        day = d.get("date") or today
        if day > today:
            raise ValidationError({"date": ["Дата оплаты не может быть в будущем."]})
        description = ""
        if tenant is None:
            title = pay_srv.CATEGORIES[d["category"]]
            description = f"{title}: {d['comment']}" if d["comment"] else title
        else:
            description = d["comment"]
        res = pay_srv.post_payment(
            company=ctx.company, settings_obj=self.settings_obj(), user=request.user, branch_id=branch_id,
            tenant=tenant, amount=d["amount"], method=d["method"], day=day,
            add_penalty=d["add_penalty"] and tenant is not None, description=description, today=today)
        pay = res["payment"]
        audit.log(request, ctx.company, "payment", "payment", pay, branch=pay.branch,
                  object_name=f"{tenant.name if tenant else description}: {fmt_som(pay.amount)} сом",
                  after={"amount": pay.amount, "method": pay.method, "tenant": str(pay.tenant_id or ""),
                         "penalty_added": res["penalty_charge"].amount if res["penalty_charge"] else 0,
                         "receipt": res["receipt"].label if res["receipt"] else None})
        pay = Payment.objects.select_related("tenant", "cashier", "receipt").get(pk=pay.pk) if tenant else \
            Payment.objects.select_related("cashier").get(pk=pay.pk)
        return Response({
            "payment": payment_payload(pay),
            "receipt": receipt_payload(res["receipt"]) if res["receipt"] else None,
            "allocations": [{"charge": str(a.charge_id), "kind": a.charge.kind, "period": a.charge.period.isoformat(),
                             "amount": a.amount} for a in res["allocations"]],
            "penalty_added": res["penalty_charge"].amount if res["penalty_charge"] else 0,
            "debt_after": res["debt_after"],
        }, status=201)


class PaymentStornoView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("cash")]

    def post(self, request, pk):
        ctx = self.ctx
        if not ctx.is_owner:
            raise ApiError("permission_denied", "Сторно оплаты доступно только владельцу.", status_code=403)
        ser = StornoSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            pay = (Payment.objects.select_for_update().filter(pk=pk, company=ctx.company)
                   .select_related("tenant", "cashier").first())
            if not pay:
                raise ApiError("not_found", "Оплата не найдена.", status_code=404)
            _check_branch(ctx, pay.branch_id)
            before = pay_srv.storno_payment(company=ctx.company, settings_obj=self.settings_obj(), payment=pay,
                                            user=request.user, reason=ser.validated_data["reason"])
        audit.log(request, ctx.company, "storno", "payment", pay, branch=pay.branch,
                  object_name=f"Сторно {fmt_som(pay.amount)} сом", before=before,
                  after={"status": "reversed", "reason": pay.reversal_reason})
        pay = Payment.objects.select_related("tenant", "cashier", "receipt").get(pk=pay.pk)
        return Response(payment_payload(pay))


# ------------------------------------------------------------------ чеки


def _receipt_or_404(ctx, pk):
    r = (Receipt.objects.filter(pk=pk, company=ctx.company)
         .select_related("payment", "payment__tenant", "payment__cashier").first())
    if not r:
        raise ApiError("not_found", "Чек не найден.", status_code=404)
    _check_branch(ctx, r.payment.branch_id)
    return r


class ReceiptListView(ManualPaging, CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("cash", "tenants")]

    def get(self, request):
        qs = Receipt.objects.filter(company=self.ctx.company).select_related("payment")
        qs = apply_branch(qs, self.branch_ids(), "payment__branch_id")
        t = request.query_params.get("tenant")
        if t:
            try:
                qs = qs.filter(payment__tenant_id=uuid.UUID(t))
            except ValueError:
                raise ValidationError({"tenant": ["Некорректный идентификатор."]})
        page = self.paginate_queryset(list(qs.order_by("-number")))
        return self.get_paginated_response([receipt_payload(r, full=False) for r in page])


class ReceiptDetailView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("cash", "tenants")]

    def get(self, request, pk):
        return Response(receipt_payload(_receipt_or_404(self.ctx, pk)))


class ReceiptTextView(CtxMixin, APIView):
    content_negotiation_class = IgnoreFormatNegotiation  # ответ — text/plain или HTML, Accept клиента не мешает
    permission_classes = [IsAuthenticated, ArendaSection("cash", "tenants")]

    def get(self, request, pk):
        r = _receipt_or_404(self.ctx, pk)
        return HttpResponse(pay_srv.receipt_text(r), content_type="text/plain; charset=utf-8")


class ReceiptPrintView(CtxMixin, APIView):
    content_negotiation_class = IgnoreFormatNegotiation  # ответ — text/plain или HTML, Accept клиента не мешает
    permission_classes = [IsAuthenticated, ArendaSection("cash", "tenants")]

    def get(self, request, pk):
        r = _receipt_or_404(self.ctx, pk)
        return HttpResponse(pay_srv.receipt_html(r), content_type="text/html; charset=utf-8")


# ------------------------------------------------------------------ замечания и взыскания


def remark_payload(r):
    return {"id": str(r.id), "type": r.type, "date": r.date.isoformat(), "text": r.text,
            "issued_by": str(r.issued_by_id) if r.issued_by_id else None,
            "issued_by_name": _brief_user(r.issued_by), "fine_amount": r.fine_amount,
            "notify": r.notify_tenant, "charge": str(r.charge_id) if r.charge_id else None}


class RemarksView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("tenants")]

    def get(self, request, pk):
        t = _tenant_or_404(self.ctx, pk)
        qs = Remark.objects.filter(tenant=t, archived_at__isnull=True).select_related("issued_by").order_by("-date", "-created_at")
        return Response([remark_payload(r) for r in qs])

    def post(self, request, pk):
        ctx = self.ctx
        t = _tenant_or_404(ctx, pk)
        ser = RemarkInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        today = timezone.localdate()
        issuer = request.user
        if d.get("issued_by"):
            issuer = User.objects.filter(pk=d["issued_by"], company=ctx.company).first()
            if issuer is None:
                raise ValidationError({"issued_by": ["Сотрудник не найден."]})
        day = d.get("date") or today
        charge = None
        if d["type"] == "fine":
            branch_id = pay_srv.tenant_branch_id(t)
            if not branch_id:
                raise ValidationError({"branch": ["У арендатора нет договора и филиала — штраф некуда начислить."]})
            _check_branch(ctx, branch_id)
            charge = Charge.objects.create(
                company=ctx.company, branch_id=branch_id, tenant=t, kind="fine", period=month_start(day),
                amount=d["fine_amount"], due_date=today, description=d["text"][:255] or "Штраф", created_by=request.user)
        remark = Remark.objects.create(
            company=ctx.company, tenant=t, type=d["type"], date=day, text=d["text"], issued_by=issuer,
            fine_amount=d["fine_amount"], notify_tenant=d["notify"], charge=charge, created_by=request.user)
        if d["notify"]:
            titles = dict(Remark.TYPES)
            text = f"{titles[d['type']]} от {day:%d.%m.%Y}: {d['text']}".strip()
            if charge:
                text += f" Штраф {fmt_som(charge.amount)} сом."
            Reminder.objects.create(company=ctx.company, tenant=t, channel=d["channel"], text=text,
                                    status="not_sent", created_by=request.user)
        audit.log(request, ctx.company, "create", "remark", remark, object_name=f"{t.name}: {remark.get_type_display()}",
                  after={"type": d["type"], "fine_amount": d["fine_amount"], "charge": str(charge.id) if charge else None,
                         "notify": d["notify"]})
        return Response(remark_payload(remark), status=201)


class RemarkDetailView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("tenants")]

    def delete(self, request, pk, rid):
        ctx = self.ctx
        t = _tenant_or_404(ctx, pk)
        r = Remark.objects.filter(pk=rid, tenant=t, archived_at__isnull=True).first()
        if not r:
            raise ApiError("not_found", "Запись не найдена.", status_code=404)
        if r.type == "fine":
            raise ApiError("fine_not_deletable", "Штраф увеличил долг и не удаляется.", status_code=409)
        r.archived_at = timezone.now()
        r.save(update_fields=["archived_at"])
        audit.log(request, ctx.company, "archive", "remark", r, object_name=t.name)
        return Response(status=204)


# ------------------------------------------------------------------ карточка арендатора


class TenantCardView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("tenants")]

    def get(self, request, pk):
        from .models import Lease
        ctx = self.ctx
        t = _tenant_or_404(ctx, pk)
        today = self.today()
        s = self.settings_obj()
        leases = list(Lease.objects.filter(tenant=t, company=ctx.company, archived_at__isnull=True)
                      .select_related("tenant").prefetch_related("units").order_by("-start_date"))
        active = [l for l in leases if l.status == "active" and l.start_date <= today]
        info = stats.debt_map(ctx.company, today, None, [t.id], s.soon_days).get(t.id, DebtInfo())
        rate = pay_srv.tenant_penalty_rate(t, s.penalty_rate)
        units = []
        for l in active:
            for u in l.units.all():
                units.append({"id": str(u.id), "number": u.number, "floor": u.floor, "area": str(u.area),
                              "branch": str(u.branch_id)})
        m = month_start(today)
        for _ in range(3):
            m = month_start(m - timedelta(days=1))
        pays = (Payment.objects.filter(company=ctx.company, tenant=t, paid_at__date__gte=m)
                .select_related("tenant", "cashier", "receipt").order_by("-paid_at"))
        reminders = Reminder.objects.filter(company=ctx.company, tenant=t).order_by("-created_at")[:20]
        remarks = Remark.objects.filter(tenant=t, archived_at__isnull=True).select_related("issued_by") \
            .order_by("-date", "-created_at")
        return Response({
            "tenant": {"id": str(t.id), "name": t.name, "form": t.form, "inn": t.inn,
                       "contact_person": t.contact_person, "passport": t.passport, "phone": t.phone,
                       "email": t.email, "activity": t.activity},
            "units": units,
            "leases": [lease_payload(l, today, s.lease_expiring_days) for l in leases],
            "debt": {"debt": info.debt, "advance": info.advance, "status": info.status,
                     "overdue_days": info.overdue_days,
                     "penalty_calc": calc_penalty(info.overdue_base, rate, info.overdue_days),
                     "penalty_rate": str(rate)},
            "payments_from": m.isoformat(),
            "payments": [payment_payload(p) for p in pays],
            "reminders": [{"id": str(r.id), "channel": r.channel, "text": r.text, "status": r.status,
                           "is_auto": r.is_auto, "created_at": timezone.localtime(r.created_at).isoformat()}
                          for r in reminders],
            "remarks": [remark_payload(r) for r in remarks],
        })


# ------------------------------------------------------------------ акты сверки


def _act_or_404(ctx, pk):
    a = ReconciliationAct.objects.filter(pk=pk, company=ctx.company).select_related("tenant").first()
    if not a:
        raise ApiError("not_found", "Акт не найден.", status_code=404)
    return a


class TenantActsView(ManualPaging, CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("tenants", "cash")]

    def get(self, request, pk):
        t = _tenant_or_404(self.ctx, pk)
        qs = ReconciliationAct.objects.filter(company=self.ctx.company, tenant=t).select_related("tenant").order_by("-number")
        page = self.paginate_queryset(list(qs))
        return self.get_paginated_response([act_payload(a, full=False) for a in page])

    def post(self, request, pk):
        ctx = self.ctx
        t = _tenant_or_404(ctx, pk)
        ser = ActInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        act = acts_srv.create_act(company=ctx.company, settings_obj=self.settings_obj(), tenant=t,
                                  date_from=d["date_from"], date_to=d["date_to"], user=request.user)
        audit.log(request, ctx.company, "create", "reconciliation_act", act, object_name=f"{act.label} {t.name}",
                  after={"period": [str(act.period_from), str(act.period_to)],
                         "closing_balance": act.closing_balance})
        return Response(act_payload(act), status=201)


class ActListView(ManualPaging, CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("tenants", "cash")]

    def get(self, request):
        qs = ReconciliationAct.objects.filter(company=self.ctx.company).select_related("tenant").order_by("-number")
        t = request.query_params.get("tenant")
        if t:
            try:
                qs = qs.filter(tenant_id=uuid.UUID(t))
            except ValueError:
                raise ValidationError({"tenant": ["Некорректный идентификатор."]})
        page = self.paginate_queryset(list(qs))
        return self.get_paginated_response([act_payload(a, full=False) for a in page])


class ActDetailView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("tenants", "cash")]

    def get(self, request, pk):
        return Response(act_payload(_act_or_404(self.ctx, pk)))


class ActPrintView(CtxMixin, APIView):
    content_negotiation_class = IgnoreFormatNegotiation
    permission_classes = [IsAuthenticated, ArendaSection("tenants", "cash")]

    def get(self, request, pk):
        a = _act_or_404(self.ctx, pk)
        return HttpResponse(acts_srv.act_html(a), content_type="text/html; charset=utf-8")
