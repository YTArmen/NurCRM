"""Этап 6: уведомления, напоминания, правила автонапоминаний (ТЗ §3.9)."""
from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ApiError

from . import audit
from .branch_filter import branch_ids_from_request
from .constants import REMINDER_RULE_KINDS
from .models import Reminder, ReminderRule, Tenant
from .pagination import ArendaPagination
from .permissions import ArendaSection
from .services import reminders as svc
from .views import CtxMixin

NOTIFY = [IsAuthenticated, ArendaSection("notifications")]


def reminder_payload(r):
    return {"id": str(r.id), "tenant": str(r.tenant_id) if r.tenant_id else None, "channel": r.channel,
            "text": r.text, "is_auto": r.is_auto, "rule_kind": r.rule_kind, "status": r.status, "error": r.error,
            "sent_at": r.sent_at, "created_at": r.created_at}


class SummaryView(CtxMixin, APIView):
    permission_classes = NOTIFY

    def get(self, request):
        return Response(svc.build_summary(self.ctx.company, self.today(), self.branch_ids()))


class RemindInSerializer(serializers.Serializer):
    tenant_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    all = serializers.BooleanField(required=False)
    channel = serializers.ChoiceField(choices=["sms", "whatsapp", "telegram"], default="sms")
    text = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class RemindView(CtxMixin, APIView):
    """POST notifications/remind/ {tenant_ids:[...]|all:true (или "tenant_ids":"all"), channel, text?}"""
    permission_classes = NOTIFY

    def post(self, request):
        data = dict(request.data) if hasattr(request.data, "items") else {}
        if data.get("tenant_ids") == "all" or data.get("tenant_ids") == ["all"]:
            data = {**{k: request.data.get(k) for k in ("channel", "text") if k in request.data}, "all": True}
        ser = RemindInSerializer(data=data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        if not d.get("all") and not d.get("tenant_ids"):
            raise ValidationError({"tenant_ids": ["Выберите арендаторов или «всех»."]})
        ctx = self.ctx
        today = self.today()
        s = ctx.company.arenda_settings
        ids = self.branch_ids()
        states = svc.tenant_states(ctx.company, today, ids, s.soon_days)
        rates = svc._rates(ctx.company, today, s, ids)
        if d.get("all"):
            target_ids = [t for t, st in states.items() if st["info"].status == "overdue"]
        else:
            target_ids = list(d["tenant_ids"])
        tenants = Tenant.objects.filter(company=ctx.company, id__in=target_ids, archived_at__isnull=True)
        found = {t.id: t for t in tenants}
        if not d.get("all") and set(found) != set(target_ids):
            raise ApiError("not_found", "Арендатор не найден.", status_code=404)
        out = []
        for tid, t in found.items():
            st = states.get(tid)
            info = st["info"] if st else None
            rate, lease = rates.get(tid, (s.penalty_rate, None))
            if d.get("text"):
                text = d["text"]
            elif info is not None and info.debt > 0:
                text = svc.debt_text(ctx.company, t, info, rate)
            else:
                text = f"{t.name}, напоминаем об оплате аренды."
            r, _ = svc.make_reminder(ctx.company, t, d["channel"], text, lease=lease, user=request.user)
            out.append(reminder_payload(r))
        return Response({"count": len(out), "results": out}, status=201)


class ReminderListView(CtxMixin, APIView):
    """GET reminders/?tenant= — история напоминаний со статусом доставки."""
    permission_classes = NOTIFY

    def get(self, request):
        qs = Reminder.objects.filter(company=self.ctx.company)
        if request.query_params.get("tenant"):
            qs = qs.filter(tenant_id=request.query_params["tenant"])
        pager = ArendaPagination()
        page = pager.paginate_queryset(qs, request, view=self)
        return pager.get_paginated_response([reminder_payload(r) for r in page])


def rule_payload(r):
    return {"kind": r.kind, "title": svc.RULE_TITLES.get(r.kind, r.kind), "enabled": r.enabled,
            "params": {k: v for k, v in (r.params or {}).items()}}


class RulesView(CtxMixin, APIView):
    """GET/PUT reminder-rules/ — 5 правил, каждое включается отдельно. PUT: [{kind, enabled, params?}] или {kind: bool}."""
    permission_classes = NOTIFY

    def _list(self):
        rules = svc.rules_state(self.ctx.company)
        return [rule_payload(rules[k]) for k in REMINDER_RULE_KINDS]

    def get(self, request):
        return Response(self._list())

    def put(self, request):
        rules = svc.rules_state(self.ctx.company)
        body = request.data
        if isinstance(body, dict):
            body = [{"kind": k, "enabled": v} for k, v in body.items() if k in REMINDER_RULE_KINDS]
        if not isinstance(body, list):
            raise ValidationError({"detail": ["Ожидается список правил."]})
        before = {k: r.enabled for k, r in rules.items()}
        with transaction.atomic():
            for item in body:
                kind = item.get("kind")
                if kind not in rules:
                    raise ValidationError({"kind": [f"Неизвестное правило: {kind}."]})
                r = rules[kind]
                if "enabled" in item:
                    r.enabled = bool(item["enabled"])
                if isinstance(item.get("params"), dict):
                    r.params = item["params"]
                r.save()
        after = {k: r.enabled for k, r in rules.items()}
        if before != after:
            audit.log(request, self.ctx.company, "update", "reminder_rules", object_id="rules",
                      object_name="Автонапоминания", before=before, after=after)
        return Response(self._list())

    patch = put
