from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, status as http, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import Branch, User
from core.exceptions import ApiError

from . import audit
from .branch_filter import apply_branch, branch_ids_from_request
from .constants import PRESETS, SECTION_TITLES, SECTIONS
from .models import (ArendaAccess, ArendaBranchInfo, ArendaSettings, Asset, Charge, Counter, Employee, Expense,
                     Lease, Payment, Tenant, Unit, UnitType)
from .pagination import ArendaPagination
from .permissions import ArendaSection, get_ctx
from .serializers import (AccessInSerializer, LeaseInSerializer, SettingsSerializer, TenantInSerializer,
                          UnitSerializer, UnitTypeSerializer)
from .services import stats
from .services.charges import (charge_amount_for_period, ensure_rent_charge, lease_current_rent,
                               lease_monthly_rent, month_start)
from .services.debt import DebtInfo, calc_penalty, lease_days_left, lease_display_status
from .services.money import calc_rent, round_int


class CtxMixin:
    pagination_class = ArendaPagination
    filter_backends = []

    @property
    def ctx(self):
        return get_ctx(self.request)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "ctx": self.ctx}

    def today(self):
        return timezone.localdate()

    def settings_obj(self):
        return self.ctx.company.arenda_settings

    def branch_ids(self):
        return branch_ids_from_request(self.ctx, self.request)



class ManualPaging:
    """Пагинация списков, собранных в Python (для ViewSet без GenericAPIView)."""

    @property
    def paginator(self):
        if not hasattr(self, "_paginator"):
            self._paginator = ArendaPagination()
        return self._paginator

    def paginate_queryset(self, items):
        return self.paginator.paginate_queryset(items, self.request, view=self)

    def get_paginated_response(self, data):
        return self.paginator.get_paginated_response(data)


def _page_response(view, items, serialize, extra=None):
    page = view.paginate_queryset(items)
    resp = view.get_paginated_response([serialize(i) for i in page])
    if extra:
        data = dict(resp.data)
        res = data.pop("results")
        data.update(extra)
        data["results"] = res
        resp.data = data
    return resp


def _ver(model, fields):
    return {f: (str(getattr(model, f)) if getattr(model, f) is not None else None) for f in fields}


# ======================================================================= филиалы


class BranchViewSet(ManualPaging, CtxMixin, viewsets.ViewSet):
    permission_classes = [IsAuthenticated, ArendaSection("branches")]

    # --- helpers
    def _qs(self):
        qs = Branch.objects.filter(company=self.ctx.company, is_active=True).order_by("name")
        allowed = self.ctx.allowed_branch_ids()
        if allowed is not None:
            qs = qs.filter(id__in=allowed)
        return qs

    def _info(self, branch):
        info, _ = ArendaBranchInfo.objects.get_or_create(branch=branch, defaults={"company": self.ctx.company})
        return info

    def _payload(self, branch, st=None):
        info = self._info(branch)
        st = st or {}
        return {
            "id": str(branch.id), "name": branch.name, "city": info.city, "address": branch.address or "",
            "phone": branch.phone or "", "object_type": info.object_type, "floors": info.floors,
            "manager_name": info.manager_name,
            "manager_user": str(info.manager_user_id) if info.manager_user_id else None,
            "units_count": st.get("units", 0), "occupancy_percent": st.get("occupancy", 0.0),
            "monthly_charged": st.get("charged", 0),
        }

    def _stats(self, branch_ids):
        today = timezone.localdate()
        units = Unit.objects.filter(company=self.ctx.company, archived_at__isnull=True)
        if branch_ids is not None:
            units = units.filter(branch_id__in=branch_ids)
        state = stats.unit_state_map(self.ctx.company, today, branch_ids)
        res = defaultdict(lambda: {"units": 0, "total": Decimal(0), "rented": Decimal(0), "charged": 0})
        for u in units:
            r = res[u.branch_id]
            r["units"] += 1
            r["total"] += u.area
            if state.get(u.id, {}).get("status") in ("rented", "due_soon", "overdue"):
                r["rented"] += u.area
        for b, amount in (Charge.objects.filter(company=self.ctx.company, voided_at__isnull=True,
                                                period=month_start(today))
                          .values_list("branch_id", "amount")):
            res[b]["charged"] += amount
        out = {}
        for b, r in res.items():
            out[b] = {"units": r["units"], "charged": int(r["charged"]),
                      "occupancy": float(round(r["rented"] / r["total"] * 100, 1)) if r["total"] else 0.0}
        return out

    def _save(self, request, branch, data):
        company = self.ctx.company
        if "name" in data:
            name = data["name"].strip()
            if not name:
                raise ValidationError({"name": ["Укажите название."]})
            clash = Branch.objects.filter(company=company, name=name)
            if branch is not None:
                clash = clash.exclude(id=branch.id)
            if clash.exists():
                raise ValidationError({"name": ["Филиал с таким названием уже есть."]})
            data["name"] = name
        with transaction.atomic():
            if branch is None:
                if "name" not in data:
                    raise ValidationError({"name": ["Укажите название."]})
                branch = Branch.objects.create(company=company, name=data["name"], address=data.get("address"),
                                               phone=data.get("phone"))
                info = self._info(branch)
                info.created_by = request.user
            else:
                info = self._info(branch)
                for f in ("name", "address", "phone"):
                    if f in data:
                        setattr(branch, f, data[f])
                branch.save()
            for f in ("city", "object_type", "manager_name"):
                if f in data:
                    setattr(info, f, data[f] or "")
            if "floors" in data:
                try:
                    info.floors = max(1, int(data["floors"]))
                except (TypeError, ValueError):
                    raise ValidationError({"floors": ["Число этажей."]})
            if "manager_user" in data:
                mu = data["manager_user"]
                if mu:
                    u = User.objects.filter(id=mu, company=company).first()
                    if not u:
                        raise ValidationError({"manager_user": ["Пользователь не найден."]})
                    info.manager_user = u
                else:
                    info.manager_user = None
            info.save()
        return branch

    # --- actions
    def list(self, request):
        ids = self.branch_ids()
        qs = self._qs()
        if ids is not None:
            qs = qs.filter(id__in=ids)
        st = self._stats(ids)
        return _page_response(self, list(qs), lambda b: self._payload(b, st.get(b.id)))

    def retrieve(self, request, pk=None):
        b = self._get(pk)
        return Response(self._payload(b, self._stats([b.id]).get(b.id)))

    def create(self, request):
        b = self._save(request, None, dict(request.data))
        audit.log(request, self.ctx.company, "create", "branch", b, after={"name": b.name}, branch=b)
        return Response(self._payload(b), status=201)

    def partial_update(self, request, pk=None):
        b = self._get(pk)
        before = {"name": b.name, "address": b.address}
        b = self._save(request, b, dict(request.data))
        audit.log(request, self.ctx.company, "update", "branch", b, before=before, after={"name": b.name,
                  "address": b.address}, branch=b)
        return Response(self._payload(b, self._stats([b.id]).get(b.id)))

    update = partial_update

    def destroy(self, request, pk=None):
        b = self._get(pk)
        for model in (Unit, Lease, Charge, Payment, Expense, Employee, Asset):
            if model.objects.filter(company=self.ctx.company, branch=b).exists():
                raise ApiError("branch_not_empty", "Нельзя удалить филиал, в котором есть данные.", status_code=409)
        b.is_active = False
        b.save(update_fields=["is_active"])
        audit.log(request, self.ctx.company, "archive", "branch", b, before={"name": b.name}, branch=b)
        return Response(status=204)

    def _get(self, pk):
        obj = self._qs().filter(pk=pk).first()
        if not obj:
            raise ApiError("not_found", "Филиал не найден.", status_code=404)
        return obj


# ======================================================================= типы и помещения


class UnitTypeViewSet(CtxMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, ArendaSection("chess", "leases")]
    serializer_class = UnitTypeSerializer

    def get_queryset(self):
        return UnitType.objects.filter(company=self.ctx.company, archived_at__isnull=True)

    def _chess_only(self):
        if "chess" not in self.ctx.sections:
            from .permissions import deny
            raise deny("chess")

    def perform_create(self, serializer):
        self._chess_only()
        name = serializer.validated_data["name"].strip()
        if UnitType.objects.filter(company=self.ctx.company, name=name).exists():
            raise ApiError("conflict", "Такой тип уже есть.", status_code=409)
        serializer.save(company=self.ctx.company, created_by=self.request.user, name=name, is_system=False)

    def perform_update(self, serializer):
        self._chess_only()
        if serializer.instance.is_system:
            raise ApiError("conflict", "Системный тип менять нельзя.", status_code=409)
        serializer.save()

    def perform_destroy(self, instance):
        self._chess_only()
        if instance.is_system:
            raise ApiError("conflict", "Системный тип удалить нельзя.", status_code=409)
        instance.archived_at = timezone.now()
        instance.save(update_fields=["archived_at"])


class UnitViewSet(CtxMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, ArendaSection("chess", "leases")]
    serializer_class = UnitSerializer

    def _chess_only(self):
        if "chess" not in self.ctx.sections:
            from .permissions import deny
            raise deny("chess")

    def get_queryset(self):
        qs = Unit.objects.filter(company=self.ctx.company, archived_at__isnull=True).select_related("unit_type")
        qs = apply_branch(qs, self.branch_ids())
        p = self.request.query_params
        if p.get("floor") not in (None, ""):
            try:
                qs = qs.filter(floor=int(p["floor"]))
            except ValueError:
                raise ValidationError({"floor": ["Число."]})
        if p.get("search"):
            qs = qs.filter(number__icontains=p["search"])
        return qs

    def get_serializer_context(self):
        c = super().get_serializer_context()
        if self.action in ("list", "retrieve", "create", "partial_update", "update"):
            c["state_map"] = stats.unit_state_map(self.ctx.company, self.today(), None,
                                                  self.settings_obj().soon_days)
        return c

    def list(self, request, *a, **kw):
        items = list(self.get_queryset())
        # контекст (карта статусов) считаем один раз; get_serializer() пересчитывал бы её на каждое помещение
        ctx = self.get_serializer_context()
        sm = ctx["state_map"]
        want = request.query_params.get("status")
        if want:
            items = [u for u in items if sm.get(u.id, {}).get("status") == want]
        ser = lambda u: self.get_serializer_class()(u, context=ctx).data
        return _page_response(self, items, ser)

    def _check_branch_allowed(self, branch):
        allowed = self.ctx.allowed_branch_ids()
        if allowed is not None and branch.id not in allowed:
            from .permissions import deny
            raise deny("chess")

    def _check_number(self, branch, number, exclude=None):
        qs = Unit.objects.filter(branch=branch, number=number, archived_at__isnull=True)
        if exclude is not None:
            qs = qs.exclude(pk=exclude.pk)
        if qs.exists():
            raise ApiError("unit_number_taken", f"Помещение с номером {number} уже есть в этом филиале.",
                           status_code=409)

    def perform_create(self, serializer):
        self._chess_only()
        d = serializer.validated_data
        self._check_branch_allowed(d["branch"])
        self._check_number(d["branch"], d["number"])
        try:
            with transaction.atomic():
                obj = serializer.save(company=self.ctx.company, created_by=self.request.user)
        except IntegrityError:
            raise ApiError("unit_number_taken", "Такой номер уже занят.", status_code=409)
        audit.log(self.request, self.ctx.company, "create", "unit", obj, after=_ver(obj, ["number", "floor", "area", "rate"]),
                  branch=obj.branch)

    def perform_update(self, serializer):
        self._chess_only()
        inst = serializer.instance
        d = serializer.validated_data
        if "number" in d:
            self._check_number(inst.branch, d["number"], exclude=inst)
        before = _ver(inst, ["number", "floor", "area", "rate"])
        obj = serializer.save()
        audit.log(self.request, self.ctx.company, "update", "unit", obj, before=before,
                  after=_ver(obj, ["number", "floor", "area", "rate"]), branch=obj.branch)

    def perform_destroy(self, instance):
        self._chess_only()
        if Lease.objects.filter(units=instance, status="active", archived_at__isnull=True).exists():
            raise ApiError("unit_has_active_lease", "Нельзя удалить помещение с действующим договором.",
                           status_code=409)
        instance.archived_at = timezone.now()
        instance.save(update_fields=["archived_at"])
        audit.log(self.request, self.ctx.company, "archive", "unit", instance, branch=instance.branch)

    @action(detail=False, methods=["get"], url_path="next-number")
    def next_number(self, request):
        bid = request.query_params.get("branch")
        branch = Branch.objects.filter(id=bid, company=self.ctx.company).first() if bid not in (None, "", "all") else None
        if not branch:
            raise ValidationError({"branch": ["Укажите филиал."]})
        self._check_branch_allowed(branch)
        floor = request.query_params.get("floor")
        try:
            floor = int(floor) if floor not in (None, "") else None
        except ValueError:
            raise ValidationError({"floor": ["Число."]})
        taken = set(Unit.objects.filter(branch=branch, archived_at__isnull=True).values_list("number", flat=True))
        nums_on_floor = [int(n) for n in
                         Unit.objects.filter(branch=branch, archived_at__isnull=True, floor=floor)
                         .values_list("number", flat=True) if str(n).isdigit()] if floor is not None else []
        all_nums = [int(n) for n in taken if str(n).isdigit()]
        if nums_on_floor:
            cand = max(nums_on_floor) + 1
        elif floor is not None and floor > 0:
            cand = floor * 100 + 1
        else:
            cand = (max(all_nums) + 1) if all_nums else 1
        while str(cand) in taken:
            cand += 1
        return Response({"number": str(cand)})


# ======================================================================= шахматка


class ChessView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("chess")]

    def get(self, request):
        ctx, today = self.ctx, self.today()
        ids = branch_ids_from_request(ctx, request)
        soon = ctx.company.arenda_settings.soon_days
        units = list(apply_branch(Unit.objects.filter(company=ctx.company, archived_at__isnull=True), ids)
                     .select_related("unit_type"))
        state = stats.unit_state_map(ctx.company, today, ids, soon)
        counters = {"all": len(units), "free": 0, "reserved": 0, "rented": 0, "due_soon": 0, "overdue": 0}
        floor_counters = defaultdict(int)
        for u in units:
            counters[state[u.id]["status"]] += 1
        p = request.query_params
        floor = None
        if p.get("floor") not in (None, ""):
            try:
                floor = int(p["floor"])
            except ValueError:
                raise ValidationError({"floor": ["Число."]})
        want = p.get("status") or None
        for u in units:
            if want is None or state[u.id]["status"] == want:
                floor_counters[u.floor] += 1
        shown = [u for u in units if (floor is None or u.floor == floor)
                 and (want is None or state[u.id]["status"] == want)]
        by_floor = defaultdict(list)
        for u in sorted(shown, key=lambda x: (x.floor, len(x.number), x.number)):
            s = state[u.id]
            t = s["tenant"]
            by_floor[u.floor].append({
                "id": str(u.id), "number": u.number, "floor": u.floor, "area": str(u.area), "rate": u.rate,
                "unit_type_name": u.unit_type.name if u.unit_type else None,
                "status": s["status"], "overdue_days": s["overdue_days"],
                "tenant": {"id": str(t.id), "name": t.name, "phone": t.phone} if t else None,
                "reserved_for": u.reserved_for, "reserved_until": u.reserved_until.isoformat() if u.reserved_until else None,
            })
        return Response({
            "kpi": stats.kpis(ctx.company, today, ids, soon, state=state),
            "counters": counters,
            "floor_counters": {str(k): v for k, v in sorted(floor_counters.items())},
            "floors": [{"floor": f, "units": by_floor[f]} for f in sorted(by_floor)],
        })


# ======================================================================= арендаторы


def lease_payload(lease, today, expiring_days=45):
    units = list(lease.units.all())
    area = sum((u.area for u in units), Decimal(0))
    disp = lease_display_status(lease.status, lease.start_date, lease.end_date, today, expiring_days)
    return {
        "id": str(lease.id), "number": lease.number, "label": lease.label, "branch": str(lease.branch_id),
        "tenant": str(lease.tenant_id), "tenant_name": lease.tenant.name,
        "units": [{"id": str(u.id), "number": u.number, "floor": u.floor, "area": str(u.area)} for u in units],
        "total_area": str(area),
        "start_date": lease.start_date.isoformat(), "end_date": lease.end_date.isoformat() if lease.end_date else None,
        "rent_monthly": lease.rent_monthly, "rent_monthly_calc": lease_monthly_rent(lease),
        "pay_day": lease.pay_day, "deposit_months": lease.deposit_months, "deposit_amount": lease.deposit_amount,
        "penalty_rate": str(lease.penalty_rate), "indexation_percent": str(lease.indexation_percent),
        "utilities": lease.utilities, "status": lease.status, "status_display": disp,
        "days_left": lease_days_left(lease.end_date, today) if lease.status == "active" else None,
        "template": str(lease.template_id) if lease.template_id else None,
        "signed_at": lease.signed_at.isoformat() if lease.signed_at else None,
        "has_snapshot": bool(lease.text_snapshot),
    }


class TenantViewSet(ManualPaging, CtxMixin, viewsets.ViewSet):
    permission_classes = [IsAuthenticated, ArendaSection("tenants")]

    ORDER = {
        "name": lambda r: r["name"].lower(), "debt": lambda r: r["debt"], "rent": lambda r: r["rent_monthly"],
        "area": lambda r: Decimal(r["total_area"]),
        "lease_end": lambda r: (r["lease_end"] is None, r["lease_end"] or ""),
        "status": lambda r: {"overdue": 0, "due_soon": 1, "ok": 2}[r["status"]],
    }

    def _qs(self, ids):
        qs = Tenant.objects.filter(company=self.ctx.company, archived_at__isnull=True)
        if ids is not None:
            qs = qs.filter(Q(leases__branch_id__in=ids) | Q(branch_id__in=ids)).distinct()
        return qs

    def _rows(self, tenants, ids):
        today = self.today()
        s = self.settings_obj()
        tids = [t.id for t in tenants]
        leases = (Lease.objects.filter(company=self.ctx.company, tenant_id__in=tids, status="active",
                                       archived_at__isnull=True, start_date__lte=today)
                  .select_related("tenant").prefetch_related("units"))
        leases = apply_branch(leases, ids)
        by_t = defaultdict(list)
        for l in leases:
            by_t[l.tenant_id].append(l)
        debts = stats.debt_map(self.ctx.company, today, ids, tids, s.soon_days)
        remarks = defaultdict(int)
        from .models import Remark
        for tid in Remark.objects.filter(tenant_id__in=tids, archived_at__isnull=True).values_list("tenant_id", flat=True):
            remarks[tid] += 1
        rows = []
        for t in tenants:
            ls = by_t.get(t.id, [])
            info = debts.get(t.id, DebtInfo())
            units = [u for l in ls for u in l.units.all()]
            main = sorted(ls, key=lambda l: (l.end_date is None, l.end_date or today))[0] if ls else None
            rate = main.penalty_rate if main else s.penalty_rate
            rows.append({
                "id": str(t.id), "name": t.name, "form": t.form, "inn": t.inn, "contact_person": t.contact_person,
                "passport": t.passport, "phone": t.phone, "email": t.email, "activity": t.activity,
                "units": [{"id": str(u.id), "number": u.number, "floor": u.floor, "area": str(u.area)} for u in units],
                "total_area": str(sum((u.area for u in units), Decimal(0))),
                "rent_monthly": sum(lease_current_rent(l, today) for l in ls),
                "debt": info.debt, "advance": info.advance, "status": info.status,
                "overdue_days": info.overdue_days,
                "penalty_calc": calc_penalty(info.overdue_base, rate, info.overdue_days),
                "lease_id": str(main.id) if main else None, "lease_label": main.label if main else None,
                "lease_end": main.end_date.isoformat() if main and main.end_date else None,
                "lease_days_left": lease_days_left(main.end_date, today) if main else None,
                "lease_status": lease_display_status(main.status, main.start_date, main.end_date, today,
                                                     s.lease_expiring_days) if main else None,
                "remarks_count": remarks.get(t.id, 0),
            })
        return rows

    def _get(self, pk):
        t = Tenant.objects.filter(pk=pk, company=self.ctx.company, archived_at__isnull=True).first()
        if not t:
            raise ApiError("not_found", "Арендатор не найден.", status_code=404)
        return t

    def list(self, request):
        ids = self.branch_ids()
        p = request.query_params
        qs = self._qs(ids)
        q = (p.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(leases__units__number__icontains=q)).distinct()
        rows = self._rows(list(qs), ids)
        want = p.get("status")
        if want:
            want = "ok" if want == "paid" else want
            rows = [r for r in rows if r["status"] == want]
        ordering = p.get("ordering") or "name"
        desc = ordering.startswith("-")
        key = self.ORDER.get(ordering.lstrip("-"))
        if key is None:
            raise ValidationError({"ordering": ["Неизвестное поле сортировки."]})
        rows.sort(key=key, reverse=desc)
        totals = {"rent_monthly": sum(r["rent_monthly"] for r in rows), "debt": sum(r["debt"] for r in rows)}
        page = self.paginate_queryset(rows)
        resp = self.get_paginated_response(page)
        data = dict(resp.data)
        res = data.pop("results")
        data["totals"] = totals
        data["results"] = res
        resp.data = data
        return resp

    def retrieve(self, request, pk=None):
        t = self._get(pk)
        row = self._rows([t], self.branch_ids())[0]
        leases = (Lease.objects.filter(tenant=t, archived_at__isnull=True).select_related("tenant")
                  .prefetch_related("units"))
        row["leases"] = [lease_payload(l, self.today(), self.settings_obj().lease_expiring_days) for l in leases]
        return Response(row)

    def create(self, request):
        ser = TenantInSerializer(data=request.data, context={"ctx": self.ctx})
        ser.is_valid(raise_exception=True)
        t = ser.save(company=self.ctx.company, created_by=request.user)
        audit.log(request, self.ctx.company, "create", "tenant", t, after={"name": t.name})
        return Response(self._rows([t], None)[0], status=201)

    def partial_update(self, request, pk=None):
        t = self._get(pk)
        before = {"name": t.name, "phone": t.phone}
        ser = TenantInSerializer(t, data=request.data, partial=True, context={"ctx": self.ctx})
        ser.is_valid(raise_exception=True)
        t = ser.save()
        audit.log(request, self.ctx.company, "update", "tenant", t, before=before, after={"name": t.name, "phone": t.phone})
        return Response(self._rows([t], None)[0])

    update = partial_update

    def destroy(self, request, pk=None):
        t = self._get(pk)
        if Lease.objects.filter(tenant=t, status="active", archived_at__isnull=True).exists():
            raise ApiError("tenant_has_active_lease", "У арендатора есть действующий договор.", status_code=409)
        t.archived_at = timezone.now()
        t.save(update_fields=["archived_at"])
        audit.log(request, self.ctx.company, "archive", "tenant", t)
        return Response(status=204)


# ======================================================================= договоры


def find_overlaps(unit_ids, start, end, exclude_id=None):
    """Помещения, на которые уже есть действующий договор с пересекающимися датами."""
    qs = Lease.objects.filter(units__in=unit_ids, status="active", archived_at__isnull=True)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    qs = qs.filter(Q(end_date__isnull=True) | Q(end_date__gte=start))
    if end is not None:
        qs = qs.filter(start_date__lte=end)
    return sorted({str(u) for u in qs.values_list("units", flat=True) if u in set(unit_ids)})


class LeaseViewSet(ManualPaging, CtxMixin, viewsets.ViewSet):
    permission_classes = [IsAuthenticated, ArendaSection("leases")]

    def _qs(self):
        qs = (Lease.objects.filter(company=self.ctx.company, archived_at__isnull=True)
              .select_related("tenant").prefetch_related("units"))
        return apply_branch(qs, self.branch_ids())

    def _get(self, pk):
        l = self._qs().filter(pk=pk).first()
        if not l:
            raise ApiError("not_found", "Договор не найден.", status_code=404)
        return l

    def _payload(self, l):
        return lease_payload(l, self.today(), self.settings_obj().lease_expiring_days)

    def list(self, request):
        qs = self._qs()
        p = request.query_params
        if p.get("tenant"):
            qs = qs.filter(tenant_id=p["tenant"])
        if p.get("search"):
            qs = qs.filter(Q(tenant__name__icontains=p["search"]) | Q(label__icontains=p["search"])
                           | Q(units__number__icontains=p["search"])).distinct()
        items = [self._payload(l) for l in qs.order_by("-start_date", "-created_at")]
        if p.get("status"):
            items = [i for i in items if i["status_display"] == p["status"]]
        page = self.paginate_queryset(items)
        return self.get_paginated_response(page)

    def retrieve(self, request, pk=None):
        return Response(self._payload(self._get(pk)))

    def _check_overlap(self, unit_ids, start, end, exclude_id=None):
        bad = find_overlaps(unit_ids, start, end, exclude_id)
        if bad:
            raise ApiError("lease_overlap", "На это помещение уже есть договор с пересекающимися датами.",
                           status_code=409, data={"units": bad})

    def create(self, request):
        ctx = self.ctx
        ser = LeaseInSerializer(data=request.data, context={"ctx": ctx})
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        units = d["units"]
        allowed = ctx.allowed_branch_ids()
        if allowed is not None and units[0].branch_id not in allowed:
            from .permissions import deny
            raise deny("leases")
        with transaction.atomic():
            if d["status"] == "active":
                # блокируем помещения, чтобы параллельные договоры не пересеклись
                list(Unit.objects.select_for_update().filter(pk__in=[u.pk for u in units]))
                self._check_overlap([u.pk for u in units], d["start_date"], d.get("end_date"))
            n = Counter.next(ctx.company, "lease")
            s = self.settings_obj()
            monthly = d.get("rent_monthly")
            base_rent = monthly if monthly is not None else sum(calc_rent(u.area, u.rate) for u in units)
            dm = d.get("deposit_months", 0)
            lease = Lease.objects.create(
                company=ctx.company, created_by=request.user, branch_id=units[0].branch_id,
                number=n, label=f"А-{self.today().year}/{n:03d}", tenant=d["tenant"],
                start_date=d["start_date"], end_date=d.get("end_date"), rent_monthly=monthly,
                pay_day=d.get("pay_day", 10), deposit_months=dm,
                deposit_amount=d["deposit_amount"] if d.get("deposit_amount") is not None else base_rent * dm,
                penalty_rate=d.get("penalty_rate", s.penalty_rate),
                indexation_percent=d.get("indexation_percent", 0), utilities=d.get("utilities", ""),
                status=d["status"],
            )
            lease.units.set(units)
            if lease.status == "active":
                ensure_rent_charge(lease, self.today(), request.user)
            audit.log(request, ctx.company, "create", "lease", lease, after={
                "label": lease.label, "tenant": str(lease.tenant_id), "rent_monthly": lease.rent_monthly,
                "start": str(lease.start_date), "end": str(lease.end_date)}, branch=lease.branch)
        lease = self._get(lease.pk)
        return Response(self._payload(lease), status=201)

    def partial_update(self, request, pk=None):
        lease = self._get(pk)
        if lease.status == "terminated":
            raise ApiError("conflict", "Расторгнутый договор менять нельзя.", status_code=409)
        data = dict(request.data) if not hasattr(request.data, "dict") else request.data.dict()
        merged = {
            "tenant": str(lease.tenant_id), "units": [str(u.pk) for u in lease.units.all()],
            "start_date": str(lease.start_date), "end_date": str(lease.end_date) if lease.end_date else None,
        }
        ser = LeaseInSerializer(data={**merged, **data}, context={"ctx": self.ctx})
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        before = self._payload(lease)
        units = d["units"]
        new_status = d.get("status", lease.status) if "status" in data else lease.status
        with transaction.atomic():
            if new_status == "active":
                list(Unit.objects.select_for_update().filter(pk__in=[u.pk for u in units]))
                self._check_overlap([u.pk for u in units], d["start_date"], d.get("end_date"), lease.pk)
            lease.tenant, lease.start_date, lease.end_date = d["tenant"], d["start_date"], d.get("end_date")
            lease.branch_id = units[0].branch_id
            for f in ("rent_monthly", "pay_day", "deposit_months", "deposit_amount", "penalty_rate",
                      "indexation_percent", "utilities"):
                if f in data:
                    setattr(lease, f, d.get(f))
            lease.status = new_status
            lease.save()
            lease.units.set(units)
            audit.log(request, self.ctx.company, "update", "lease", lease, before=before,
                      after=self._payload(self._get(lease.pk)), branch=lease.branch)
        return Response(self._payload(self._get(lease.pk)))

    update = partial_update

    @action(detail=True, methods=["post"])
    def terminate(self, request, pk=None):
        lease = self._get(pk)
        if lease.status != "active":
            raise ApiError("conflict", "Расторгнуть можно только действующий договор.", status_code=409)
        raw = request.data.get("date")
        try:
            from datetime import date as _d
            when = _d.fromisoformat(raw) if raw else self.today()
        except ValueError:
            raise ValidationError({"date": ["Формат YYYY-MM-DD."]})
        before = self._payload(lease)
        lease.status = "terminated"
        lease.terminated_at = when
        if lease.end_date is None or lease.end_date > when:
            lease.end_date = when
        lease.save()
        audit.log(request, self.ctx.company, "terminate", "lease", lease, before=before,
                  after=self._payload(self._get(lease.pk)), branch=lease.branch)
        return Response(self._payload(self._get(lease.pk)))

    def destroy(self, request, pk=None):
        lease = self._get(pk)
        if lease.status != "draft":
            raise ApiError("conflict", "Удалить можно только черновик; действующий договор расторгается.",
                           status_code=409)
        lease.archived_at = timezone.now()
        lease.save(update_fields=["archived_at"])
        audit.log(request, self.ctx.company, "archive", "lease", lease, branch=lease.branch)
        return Response(status=204)


# ======================================================================= настройки, доступы, профиль


class SettingsView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("settings")]

    def get(self, request):
        return Response(SettingsSerializer(self.settings_obj()).data)

    def patch(self, request):
        obj = self.settings_obj()
        before = SettingsSerializer(obj).data
        ser = SettingsSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        audit.log(request, self.ctx.company, "update", "settings", obj, before=dict(before),
                  after=dict(SettingsSerializer(obj).data), object_name="Настройки")
        return Response(SettingsSerializer(obj).data)

    put = patch


def _user_brief(u):
    return {"id": str(u.id), "name": f"{u.first_name} {u.last_name}".strip() or u.email,
            "phone": u.phone_number or "", "email": u.email}


def _access_payload(user, acc):
    return {
        "user": _user_brief(user),
        "preset": acc.preset if acc else "",
        "sections": list(acc.sections) if acc else [],
        "all_branches": acc.all_branches if acc else True,
        "branches": [str(b) for b in acc.branches.values_list("id", flat=True)] if acc else [],
        "is_active": bool(acc and acc.is_active),
    }


class AccessListView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("settings")]

    def get(self, request):
        company = self.ctx.company
        users = list(User.objects.filter(company=company).exclude(id=company.owner_id).order_by("first_name"))
        accs = {a.user_id: a for a in ArendaAccess.objects.filter(company=company).prefetch_related("branches")}
        return Response({"count": len(users), "next": None, "previous": None,
                         "results": [_access_payload(u, accs.get(u.id)) for u in users]})


class AccessDetailView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("settings")]

    def _user(self, user_id):
        company = self.ctx.company
        u = User.objects.filter(pk=user_id, company=company).exclude(id=company.owner_id).first()
        if not u:
            raise ApiError("not_found", "Сотрудник не найден.", status_code=404)
        return u

    def get(self, request, user_id):
        u = self._user(user_id)
        return Response(_access_payload(u, ArendaAccess.objects.filter(user=u).first()))

    def put(self, request, user_id):
        u = self._user(user_id)
        ser = AccessInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        company = self.ctx.company
        acc = ArendaAccess.objects.filter(user=u).first()
        before = _access_payload(u, acc)
        preset = d.get("preset", "")
        if preset and preset not in PRESETS:
            raise ValidationError({"preset": ["Неизвестный набор."]})
        if "sections" in d:
            sections = d["sections"]
        elif preset:
            sections = PRESETS[preset]["sections"]
        else:
            sections = list(acc.sections) if acc else []
        sections = [s for s in SECTIONS if s in set(sections)]
        branch_ids = d.get("branches")
        if branch_ids is not None:
            found = set(Branch.objects.filter(company=company, id__in=branch_ids).values_list("id", flat=True))
            if found != set(branch_ids):
                raise ValidationError({"branches": ["Филиал не найден."]})
        with transaction.atomic():
            if acc is None:
                acc = ArendaAccess(company=company, user=u, created_by=request.user)
            acc.preset = preset or (acc.preset if "sections" not in d else "")
            acc.sections = sections
            if "all_branches" in d:
                acc.all_branches = d["all_branches"]
            acc.is_active = bool(sections) and d.get("is_active", True)
            acc.save()
            if branch_ids is not None:
                acc.branches.set(branch_ids)
            from .security import sync_user_state
            sync_user_state(u, acc)  # закрыт доступ = завершить сеансы
        after = _access_payload(u, acc)
        audit.log(request, company, "access", "access", acc, before=before, after=after,
                  object_name=after["user"]["name"])
        return Response(after)


class AccessPresetsView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("settings")]

    def get(self, request):
        return Response([{"key": k, "title": v["title"], "sections": v["sections"]} for k, v in PRESETS.items()])


class MeView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = self.ctx
        allowed = ctx.allowed_branch_ids()
        bq = Branch.objects.filter(company=ctx.company, is_active=True)
        if allowed is not None:
            bq = bq.filter(id__in=allowed)
        return Response({
            "is_owner": ctx.is_owner, "sections": ctx.sections, "all_branches": allowed is None,
            "branches": [{"id": str(b.id), "name": b.name} for b in bq.order_by("name")],
            "user": _user_brief(request.user),
            "section_titles": {s: SECTION_TITLES[s] for s in ctx.sections},
        })
