from apps.integrations.idempotency import idempotent
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.db import transaction
from django.db.models import Sum, Count, Q, Exists, OuterRef
from django.db.models import Case, When, Value, CharField

from django.conf import settings
from django.http import QueryDict
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date

from rest_framework import generics, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError, NotFound
from rest_framework.pagination import PageNumberPagination

from apps.construction.models import Cashbox, CashFlow, CashFlowCategory, CashShift
from apps.users.permissions import read_only_guard
from apps.users.permissions_flags import PosAccess, flag_guard, flag_permission, is_privileged, has_any_flag, denied  # ТЗ Н-16
from apps.users.pin import pin_guard

from apps.main.notifications_market import notify_shift_closed, notify_shift_opened
from apps.ekassa.runtime import schedule_after_commit
from apps.ekassa.shift_bridge import (
    sync_ekassa_after_local_shift_close_by_id,
    sync_ekassa_after_local_shift_open_by_id,
)

from apps.construction.serializers import (
    CashboxSerializer,
    CashFlowSerializer,
    CashFlowCategorySerializer,
    CashboxWithFlowsSerializer,
    CashFlowInsideCashboxSerializer,
    CashShiftListSerializer,
    CashShiftOpenSerializer,
    CashShiftCloseSerializer,
    CashFlowBulkStatusSerializer,
    CashFlowEditRequestSerializer,
    CashFlowCancelRequestSerializer,
)

from apps.construction.utils import (
    get_company_from_user as _get_company,
    is_owner_like as _is_owner_like,
    fixed_branch_from_user as _fixed_branch_from_user,
    get_active_branch as _get_active_branch,
)


def _guess_sale_model():
    candidates = []
    for m in apps.get_models():
        try:
            concrete = {f.name: f for f in m._meta.get_fields() if getattr(f, "concrete", False)}
        except Exception:
            continue

        if "cashbox" not in concrete:
            continue

        f = concrete["cashbox"]
        if not getattr(f, "is_relation", False):
            continue
        if getattr(f, "related_model", None) is not Cashbox:
            continue

        has_total = "total" in concrete
        has_status = "status" in concrete
        has_pm = "payment_method" in concrete
        has_shift = "shift" in concrete

        score = (10 if has_total else 0) + (10 if has_status else 0) + (5 if has_pm else 0) + (3 if has_shift else 0)

        name = (m.__name__ or "").lower()
        if "sale" in name:
            score += 3

        candidates.append((score, m))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1] if candidates else None


SALE_MODEL = None


def get_sale_model():
    global SALE_MODEL
    if SALE_MODEL is None:
        SALE_MODEL = _guess_sale_model()
    return SALE_MODEL


def _choice_value(model, enum_name: str, member: str, fallback: str):
    enum = getattr(model, enum_name, None)
    v = getattr(enum, member, None)
    return getattr(v, "value", None) or v or fallback


def _ensure_cash_period_open(request, company, dates):
    """Закрытие периода (ТЗ «Бухгалтер», п. 2): операции кассы с датой <= closed_until."""
    from apps.warehouse.services import ensure_period_open_api

    ensure_period_open_api(company=company, dates=dates, user=request.user)


# ─────────────────────────────────────────────────────────────
# base mixin: company + branch scope
# ─────────────────────────────────────────────────────────────
class CompanyBranchScopedMixin:
    permission_classes = [permissions.IsAuthenticated]

    def _company(self):
        return _get_company(getattr(self.request, "user", None))

    def _active_branch(self):
        return _get_active_branch(self.request)

    def _model_has_field(self, queryset, field_name: str) -> bool:
        return field_name in {f.name for f in queryset.model._meta.concrete_fields}

    def _scoped_queryset(self, base_qs):
        if getattr(self, "swagger_fake_view", False):
            return base_qs.none()

        company = self._company()
        if not company:
            return base_qs.none()

        qs = base_qs
        if self._model_has_field(qs, "company"):
            qs = qs.filter(company=company)

        if self._model_has_field(qs, "branch"):
            br = self._active_branch()
            if br is not None:
                include_global = False
                if hasattr(self.request, "query_params"):
                    include_global = (self.request.query_params.get("include_global") or "").strip().lower() in ("1", "true", "yes", "on")
                if include_global:
                    qs = qs.filter(Q(branch=br) | Q(branch__isnull=True))
                else:
                    qs = qs.filter(branch=br)

        return qs

    def _inject_company_branch_on_save(self, serializer):
        company = self._company()
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")

        br = self._active_branch()

        model = getattr(getattr(serializer, "Meta", None), "model", None)
        kwargs = {}

        if model:
            model_fields = {f.name for f in model._meta.concrete_fields}
            if "company" in model_fields:
                kwargs["company"] = company
            if "branch" in model_fields and br is not None:
                kwargs["branch"] = br
        else:
            kwargs["company"] = company

        serializer.save(**kwargs)

    def perform_update(self, serializer):
        company = self._company()
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")

        model = getattr(getattr(serializer, "Meta", None), "model", None)
        kwargs = {}

        if model:
            model_fields = {f.name for f in model._meta.concrete_fields}
            if "company" in model_fields:
                kwargs["company"] = company
        else:
            kwargs["company"] = company

        serializer.save(**kwargs)


# ─────────────────────────────────────────────────────────────
# CASHBOXES
# ─────────────────────────────────────────────────────────────
@flag_guard("can_view_cashbox")  # ТЗ Н-16: касса компании — запись только с правом «Кассы»
@read_only_guard("can_edit_cashbox")
class CashboxListCreateView(CompanyBranchScopedMixin, generics.ListCreateAPIView):
    queryset = Cashbox.objects.select_related("company", "branch").order_by("-created_at")
    serializer_class = CashboxSerializer

    def get_queryset(self):
        qs = self._scoped_queryset(super().get_queryset())
        include_archived = (self.request.query_params.get("include_archived") or "").strip().lower() in ("1", "true", "yes")
        if not include_archived:
            qs = qs.filter(is_active=True)
        users_param = self.request.query_params.get("users")
        if not users_param:
            return qs
        user_ids = [x.strip() for x in users_param.split(",") if x.strip()]
        if not user_ids:
            return qs

        shift_exists = CashShift.objects.filter(cashbox_id=OuterRef("pk"), cashier_id__in=user_ids)
        q = Q(Exists(shift_exists))

        sale_model = get_sale_model()
        if sale_model is not None:
            fields = {f.name for f in sale_model._meta.concrete_fields}
            if "user" in fields and "cashbox" in fields:
                sale_qs = sale_model.objects.filter(user_id__in=user_ids)
                if "shift" in fields:
                    sale_qs = sale_qs.filter(Q(cashbox_id=OuterRef("pk")) | Q(shift__cashbox_id=OuterRef("pk")))
                else:
                    sale_qs = sale_qs.filter(cashbox_id=OuterRef("pk"))
                q |= Q(Exists(sale_qs))

        return qs.filter(q)

    def perform_create(self, serializer):
        self._inject_company_branch_on_save(serializer)

    def list(self, request, *args, **kwargs):
        """
        ✅ Ускорение: analytics считаем пачкой.
        ✅ ВАЖНО: теперь у кассы может быть несколько OPEN смен.
            - отдаём open_shifts: список по кассирам
            - оставляем open_shift_expected_cash (для совместимости) как "самая свежая open смена"
        """
        z = Decimal("0.00")

        qs = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(qs)
        cashboxes = page if page is not None else list(qs)

        ids = [cb.id for cb in cashboxes]

        analytics_map = {
            str(cb_id): {
                "income_total": z,
                "expense_total": z,
                "sales_count": 0,
                "sales_total": z,
                "cash_sales_total": z,
                "noncash_sales_total": z,

                # backward compatible (one value)
                "open_shift_expected_cash": None,

                # new (correct)
                "open_shifts": [],
            }
            for cb_id in ids
        }

        if ids:
            # ---- flows (approved) by cashbox ----
            flows = (
                CashFlow.objects
                .filter(cashbox_id__in=ids)
                .money()
                .values("cashbox_id")
                .annotate(
                    income=Sum("amount", filter=Q(type=CashFlow.Type.INCOME)),
                    expense=Sum("amount", filter=Q(type=CashFlow.Type.EXPENSE)),
                )
            )
            for r in flows:
                k = str(r["cashbox_id"])
                analytics_map[k]["income_total"] = r["income"] or z
                analytics_map[k]["expense_total"] = r["expense"] or z

            # ---- sales (paid) by cashbox ----
            sale_model = get_sale_model()
            if sale_model is not None:
                paid_value = _choice_value(sale_model, "Status", "PAID", "paid")
                cash_value = _choice_value(sale_model, "PaymentMethod", "CASH", "cash")

                sales = (
                    sale_model.objects
                    .filter(cashbox_id__in=ids, status=paid_value)
                    .values("cashbox_id")
                    .annotate(
                        cnt=Count("id"),
                        total_sum=Sum("total"),
                        cash_sum=Sum("total", filter=Q(payment_method=cash_value)),
                        noncash_sum=Sum("total", filter=~Q(payment_method=cash_value)),
                    )
                )
                for r in sales:
                    k = str(r["cashbox_id"])
                    analytics_map[k]["sales_count"] = r["cnt"] or 0
                    analytics_map[k]["sales_total"] = r["total_sum"] or z
                    analytics_map[k]["cash_sales_total"] = r["cash_sum"] or z
                    analytics_map[k]["noncash_sales_total"] = r["noncash_sum"] or z

            # ---- OPEN shifts (many) ----
            open_shifts = (
                CashShift.objects
                .filter(cashbox_id__in=ids, status=CashShift.Status.OPEN)
                .select_related("cashier")
                .only("id", "cashbox_id", "cashier_id", "opening_cash", "opened_at")
                .order_by("cashbox_id", "-opened_at")
            )

            # group shifts by cashbox
            by_cashbox = {}
            for sh in open_shifts:
                by_cashbox.setdefault(sh.cashbox_id, []).append(sh)

            if by_cashbox:
                all_open_ids = [sh.id for lst in by_cashbox.values() for sh in lst]

                # flows inside open shifts
                shift_flows = (
                    CashFlow.objects
                    .filter(shift_id__in=all_open_ids, status=CashFlow.Status.APPROVED, request_kind__isnull=True)
                    .values("shift_id")
                    .annotate(
                        income=Sum(
                            "amount",
                            # Только движения ящика смены (как CashShift._live_totals):
                            # безнал (например, залог переводом) в ожидаемые наличные не входит.
                            filter=Q(type=CashFlow.Type.INCOME) & Q(affects_shift_drawer=True) & ~Q(
                                source_kind__in=[
                                    CashFlow.SourceKind.POS_SALE,
                                    CashFlow.SourceKind.POS_PREPAYMENT,
                                ]
                            ),
                        ),
                        expense=Sum(
                            "amount",
                            filter=Q(type=CashFlow.Type.EXPENSE) & Q(affects_shift_drawer=True),
                        ),
                    )
                )
                sf_map = {r["shift_id"]: r for r in shift_flows}

                # cash sales inside open shifts
                cash_sales_map = {}
                sale_model = get_sale_model()
                if sale_model is not None:
                    paid_value = _choice_value(sale_model, "Status", "PAID", "paid")
                    cash_value = _choice_value(sale_model, "PaymentMethod", "CASH", "cash")

                    cash_sales = (
                        sale_model.objects
                        .filter(shift_id__in=all_open_ids, status=paid_value, payment_method=cash_value)
                        .values("shift_id")
                        .annotate(cash_sum=Sum("total"))
                    )
                    cash_sales_map = {r["shift_id"]: (r["cash_sum"] or z) for r in cash_sales}

                # build per-cashbox open_shifts list
                for cb_id, shifts in by_cashbox.items():
                    k = str(cb_id)
                    items = []

                    for sh in shifts:
                        inc = (sf_map.get(sh.id) or {}).get("income") or z
                        exp = (sf_map.get(sh.id) or {}).get("expense") or z
                        cash_sales_total = cash_sales_map.get(sh.id, z)
                        opening_cash = sh.opening_cash or z
                        expected = opening_cash + cash_sales_total + inc - exp

                        items.append({
                            "shift_id": str(sh.id),
                            "cashier_id": str(sh.cashier_id),
                            "opened_at": sh.opened_at.isoformat() if sh.opened_at else None,
                            "opening_cash": str(opening_cash),
                            "expected_cash": str(expected),
                        })

                    analytics_map[k]["open_shifts"] = items

                    # backward compatible single value: most recent open shift
                    if items:
                        analytics_map[k]["open_shift_expected_cash"] = items[0]["expected_cash"]

        serializer = self.get_serializer(
            cashboxes,
            many=True,
            context={"request": request, "analytics_map": analytics_map},
        )

        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


@flag_guard("can_view_cashbox")  # ТЗ Н-16: касса компании — запись только с правом «Кассы»
@read_only_guard("can_edit_cashbox")
class CashboxDetailView(CompanyBranchScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Cashbox.objects.select_related("company", "branch").prefetch_related(
        "flows__category",
        "flows__cashier",
        "flows__shift",
    )
    serializer_class = CashboxWithFlowsSerializer

    def get_queryset(self):
        # Компания + филиал сотрудника (как список касс): чужой филиал → 404.
        return self._scoped_queryset(super().get_queryset())

    def perform_update(self, serializer):
        """Changing a role must not remove the last required active route."""
        cashbox = serializer.instance
        new_role = serializer.validated_data.get("role", cashbox.role)
        if new_role != cashbox.role and cashbox.is_active:
            old_role = cashbox.role or cashbox.get_inferred_role()
            if old_role in (Cashbox.CashboxRole.POS_MAIN, Cashbox.CashboxRole.POS_BRANCH, Cashbox.CashboxRole.EXPENSE_VARIABLE):
                qs = Cashbox.objects.filter(company=cashbox.company, is_active=True)
                if old_role == Cashbox.CashboxRole.POS_BRANCH:
                    qs = qs.filter(role=old_role, branch_id=cashbox.branch_id)
                else:
                    qs = qs.filter(role=old_role)
                if qs.count() <= 1:
                    raise ValidationError({"detail": "Нельзя изменить роль последней обязательной кассы.", "code": "cashbox_role_required"})
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        company = self._company()
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")

        # 1. Belong to request.user.company, else 404
        cashbox = Cashbox.objects.filter(id=kwargs.get("pk"), company=company).first()
        if not cashbox:
            raise NotFound({"detail": "Касса не найдена — возможно, уже удалена."})

        # 2. Permissions: request.user must be owner or admin, else 403
        if not _is_owner_like(request.user):
            raise PermissionDenied("Удаление кассы разрешено только владельцу или администратору.")

        # 3. Idempotency: repeated delete of already archived cashbox -> 204
        if not cashbox.is_active:
            return Response(status=status.HTTP_204_NO_CONTENT)

        merge_into_id = request.query_params.get("merge_into")
        target = None
        if merge_into_id:
            target = Cashbox.objects.filter(id=merge_into_id, company=company, is_active=True).first()
            if not target or target.id == cashbox.id:
                return Response(
                    {"detail": "Касса-наследник недоступна.", "code": "merge_target_invalid"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # 4. Open shift on this cashbox -> 409 cashbox_has_open_shift
        if CashShift.objects.filter(cashbox=cashbox, status=CashShift.Status.OPEN).exists():
            return Response(
                {
                    "detail": "По кассе есть открытая смена. Закройте смену перед удалением.",
                    "code": "cashbox_has_open_shift",
                },
                status=status.HTTP_409_CONFLICT,
            )

        # 5. Pending movements or change requests on cashbox -> 409 cashbox_has_pending
        if CashFlow.objects.filter(cashbox_id=cashbox.id, status=CashFlow.Status.PENDING).exists():
            return Response(
                {
                    "detail": "По кассе есть неодобренные операции или заявки. Разберите их перед удалением.",
                    "code": "cashbox_has_pending",
                },
                status=status.HTTP_409_CONFLICT,
            )

        # 6. Role required for auto-operations if AUTO_CASHFLOWS is enabled
        auto_cashflows_enabled = getattr(company, "auto_cashflows", getattr(settings, "AUTO_CASHFLOWS", True))
        if auto_cashflows_enabled:
            role = cashbox.role
            if not role:
                if cashbox.is_consumption:
                    role = Cashbox.CashboxRole.EXPENSE_VARIABLE
                else:
                    role = cashbox.get_inferred_role()

            if role == Cashbox.CashboxRole.EXPENSE_VARIABLE:
                active_expense_count = (
                    Cashbox.objects.filter(company=company, is_active=True)
                    .filter(
                        Q(role=Cashbox.CashboxRole.EXPENSE_VARIABLE)
                        | (Q(role__isnull=True) & (Q(is_consumption=True) | Q(name__icontains="переменн")))
                    )
                    .count()
                )
                if active_expense_count <= 1:
                    return Response(
                        {
                            "detail": "Это последняя касса для расходов. Создайте другую, затем удалите эту.",
                            "code": "cashbox_role_required",
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
            elif role in (Cashbox.CashboxRole.POS_MAIN, Cashbox.CashboxRole.POS_BRANCH):
                active_pos_count = (
                    Cashbox.objects.filter(company=company, is_active=True)
                    .filter(
                        Q(role__in=[Cashbox.CashboxRole.POS_MAIN, Cashbox.CashboxRole.POS_BRANCH])
                        | (Q(role__isnull=True) & ~Q(is_consumption=True) & ~Q(name__icontains="расход") & ~Q(name__icontains="переменн") & ~Q(name__icontains="постоянн"))
                    )
                    .count()
                )
                if active_pos_count <= 1:
                    return Response(
                        {
                            "detail": "Это последняя активная POS-касса. Создайте другую, затем удалите эту.",
                            "code": "cashbox_role_required",
                        },
                        status=status.HTTP_409_CONFLICT,
                    )

        # 7. Decide archive or physical delete
        had_activity = (
            CashFlow.objects.filter(cashbox_id=cashbox.id).exists()
            or CashShift.objects.filter(cashbox_id=cashbox.id).exists()
        )

        if had_activity or target:
            with transaction.atomic():
                if target:
                    # History belongs to the surviving cashbox; shifts retain their
                    # original cashbox for immutable closed-shift reconciliation.
                    CashFlow.objects.filter(cashbox_id=cashbox.id).update(cashbox_id=target.id)
                    cashbox.merged_into = target
                cashbox.is_active = False
                cashbox.archived_at = timezone.now()
                cashbox.archived_by = request.user
                cashbox.save(update_fields=["is_active", "archived_at", "archived_by", "merged_into"])
        else:
            cashbox.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


@read_only_guard("can_edit_cashbox")
@flag_guard("can_view_cashbox")  # ТЗ Н-16: касса компании — запись только с правом «Кассы»
class CashboxBulkDeleteView(CompanyBranchScopedMixin, APIView):
    """Archive/delete each requested cashbox independently.

    A failed item never rolls back successfully processed items, which is the
    documented contract for the bulk UI.
    """

    def post(self, request, *args, **kwargs):
        if not _is_owner_like(request.user):
            raise PermissionDenied("Удаление кассы разрешено только владельцу или администратору.")
        ids = request.data.get("ids") if isinstance(request.data, dict) else None
        if not isinstance(ids, list) or not ids:
            raise ValidationError({"ids": "Передайте непустой список идентификаторов касс."})
        ids = list(dict.fromkeys(str(item) for item in ids))
        merge_into = request.data.get("merge_into")
        if merge_into and str(merge_into) in ids:
            raise ValidationError({"detail": "Касса-наследник не может удаляться.", "code": "merge_target_invalid"})

        succeeded, failed = [], []
        original_get = request._request.GET
        try:
            for cashbox_id in ids:
                try:
                    request._request.GET = QueryDict(f"merge_into={merge_into}") if merge_into else QueryDict("")
                    response = CashboxDetailView().destroy(request, pk=cashbox_id)
                except NotFound as exc:
                    failed.append({"id": cashbox_id, "code": "not_found", "detail": str(exc.detail)})
                    continue
                if response.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT):
                    succeeded.append(cashbox_id)
                else:
                    body = getattr(response, "data", {}) or {}
                    failed.append({
                        "id": cashbox_id,
                        "code": body.get("code", "cashbox_delete_failed"),
                        "detail": body.get("detail", "Не удалось удалить кассу."),
                    })
        finally:
            request._request.GET = original_get
        return Response({"succeeded": succeeded, "failed": failed}, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────
# CASHFLOWS
# ─────────────────────────────────────────────────────────────
class CashFlowListPagination(PageNumberPagination):
    page_size_query_param = "page_size"
    # Без верхней границы ?page_size=100000 выгружает всю кассу одним запросом.
    max_page_size = 200


def _csv_choices(qp, single_key: str, multi_key: str, allowed, *, error: str) -> list:
    """
    ?key=a или ?keys=a,b → список значений из allowed.
    Неизвестное значение — 400, а не молча пустой список.
    """
    raw = (qp.get(multi_key) or qp.get(single_key) or "").strip()
    if not raw:
        return []

    values = []
    for chunk in raw.split(","):
        v = chunk.strip().lower()
        if not v:
            continue
        if v not in allowed:
            raise ValidationError({single_key: [error]})
        if v not in values:
            values.append(v)
    return values


def _parse_amount(raw, field: str):
    """Сумма для фильтра: число ≥ 0, иначе 400. Пусто → None."""
    s = str(raw or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        value = Decimal(s)
    except (InvalidOperation, ValueError):
        raise ValidationError({field: ["Некорректная сумма."]})
    if value < 0:
        raise ValidationError({field: ["Сумма не может быть отрицательной."]})
    return value


def add_permission_class(perm):
    def deco(view_cls):
        view_cls.permission_classes = [*list(view_cls.permission_classes), perm]
        return view_cls
    return deco



class CashFlowWriteAccess(permissions.BasePermission):
    """ТЗ Н-16: запись в cashflows — владелец/админ, право can_view_cashbox, либо операции
    ящика СВОЕЙ смены (shift в запросе принадлежит этому кассиру; без shift — только если у него есть открытая смена)."""

    def _own_open_shift(self, user, shift_id):
        qs = CashShift.objects.filter(cashier_id=user.id)
        if shift_id:
            return qs.filter(pk=shift_id).exists()
        return qs.filter(status=CashShift.Status.OPEN).exists()

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in permissions.SAFE_METHODS or is_privileged(user) or has_any_flag(user, ["can_view_cashbox"]):
            return True
        if request.method == "POST":
            data = request.data if hasattr(request.data, "get") else {}
            # операции ящика смены: привязку к своей открытой смене проверяет сериализатор (400 shift)
            if str(data.get("source_kind") or "").startswith("shift_drawer_"):
                return True
            # включён режим «заявки»: расход сотрудника уходит владельцу на подтверждение (pending)
            company = getattr(user, "company", None)
            if company is not None and getattr(company, "cashflow_requests_enabled", False):
                return True
            raw = data.get("shift")
            try:
                ok = self._own_open_shift(user, raw or None)
            except Exception:  # битый uuid в shift
                ok = False
            if ok:
                return True
        elif request.method in ("PUT", "PATCH", "DELETE"):
            return True  # решает has_object_permission: только операции своей смены
        raise denied("can_view_cashbox", "Операции с кассой компании недоступны; доступны только операции вашей смены.")

    def has_object_permission(self, request, view, obj):
        user = request.user
        if request.method in permissions.SAFE_METHODS or is_privileged(user) or has_any_flag(user, ["can_view_cashbox"]):
            return True
        shift = getattr(obj, "shift", None)
        if shift is not None and shift.cashier_id == user.id:
            return True
        # режим «заявки»: сотрудник может править/отменять СВОЮ заявку (проверки статуса — в сериализаторе)
        if obj.cashier_id == user.id and getattr(obj.company, "cashflow_requests_enabled", False):
            return True
        raise denied("can_view_cashbox", "Операции с кассой компании недоступны; доступны только операции вашей смены.")



@add_permission_class(CashFlowWriteAccess)  # ТЗ Н-16
@read_only_guard("can_edit_cashbox")
class CashFlowListCreateView(CompanyBranchScopedMixin, generics.ListCreateAPIView):
    queryset = CashFlow.objects.select_related(
        "company", "branch",
        "cashbox", "cashbox__branch",
        "shift", "shift__cashier",
        "cashier",
        "category",
    )
    serializer_class = CashFlowSerializer
    pagination_class = CashFlowListPagination


    @idempotent
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def get_queryset(self):
        qs = self._scoped_queryset(super().get_queryset())
        qp = self.request.query_params

        # ✅ 1 касса: ?cashbox=<uuid>
        cashbox_id = qp.get("cashbox")
        if cashbox_id:
            qs = qs.filter(cashbox_id=cashbox_id)

        # ✅ много касс: ?cashboxes=<uuid>,<uuid>,...
        cashboxes = qp.get("cashboxes")
        if cashboxes:
            ids = [x.strip() for x in cashboxes.split(",") if x.strip()]
            qs = qs.filter(cashbox_id__in=ids)

        # ✅ проверено бухгалтером: ?checked=true|false
        checked = (qp.get("checked") or "").strip().lower()
        if checked in ("true", "1", "yes"):
            qs = qs.filter(checked_at__isnull=False)
        elif checked in ("false", "0", "no"):
            qs = qs.filter(checked_at__isnull=True)

        # ✅ по смене: ?shift=<uuid>
        shift_id = qp.get("shift")
        if shift_id:
            qs = qs.filter(shift_id=shift_id)

        # ТЗ 29.10 Б-100: ?kind=startup — только стартовые расходы
        kind_q = (qp.get("kind") or "").strip()
        if kind_q:
            qs = qs.filter(kind=kind_q)

        # ✅ по источнику: ?source_kind=shift_drawer_inflow&source_id=<id операции кассы>
        source_kinds = _csv_choices(
            qp, "source_kind", "source_kinds", CashFlow.SourceKind.values,
            error="Недопустимое значение source_kind.",
        )
        if source_kinds:
            qs = qs.filter(source_kind__in=source_kinds)
        source_id = qp.get("source_id")
        if source_id:
            qs = qs.filter(source_id=source_id)

        # ✅ по кассиру: ?cashier=<uuid> (ТЗ Н-19: ?employee= — тот же сотрудник; отдельного поля нет)
        cashier_id = qp.get("cashier") or qp.get("employee")
        if cashier_id:
            qs = qs.filter(cashier_id=cashier_id)

        # ✅ по категории: ?category=<uuid>
        category_id = qp.get("category")
        if category_id:
            qs = qs.filter(category_id=category_id)

        # ✅ по типу: ?type=expense|income (или ?types=expense,income)
        types = _csv_choices(
            qp, "type", "types", CashFlow.Type.values,
            error="Допустимые значения: income, expense.",
        )
        if types:
            qs = qs.filter(type__in=types)

        # ✅ по статусу: ?status=pending|approved|rejected (или ?statuses=...)
        # pending — это «Заявки» (ожидают подтверждения).
        statuses = _csv_choices(
            qp, "status", "statuses", CashFlow.Status.values,
            error="Допустимые значения: pending, approved, rejected.",
        )
        if statuses:
            qs = qs.filter(status__in=statuses)

        request_kind_param = (qp.get("request_kind") or "").strip().lower()
        if request_kind_param == "all":
            pass
        elif request_kind_param:
            qs = qs.filter(request_kind=request_kind_param)
        elif statuses == [CashFlow.Status.APPROVED]:
            # В ленту «Приход/Расход» (approved) попадают только реальные движения
            qs = qs.filter(request_kind__isnull=True)

        # ✅ поиск: ?search=<текст> — по названию операции и названию категории
        search = (qp.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(category__title__icontains=search)
            )

        # ✅ по сумме: ?amount_min=100&amount_max=5000
        amount_min = _parse_amount(qp.get("amount_min"), "amount_min")
        if amount_min is not None:
            qs = qs.filter(amount__gte=amount_min)
        amount_max = _parse_amount(qp.get("amount_max"), "amount_max")
        if amount_max is not None:
            qs = qs.filter(amount__lte=amount_max)

        # ✅ по периоду (по created_at): ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
        date_from_raw = (qp.get("date_from") or "").strip()
        if date_from_raw:
            df = parse_date(date_from_raw)
            if df is None:
                raise ValidationError({"date_from": ["Некорректная дата."]})
            qs = qs.filter(created_at__date__gte=df)

        date_to_raw = (qp.get("date_to") or "").strip()
        if date_to_raw:
            dt = parse_date(date_to_raw)
            if dt is None:
                raise ValidationError({"date_to": ["Некорректная дата."]})
            qs = qs.filter(created_at__date__lte=dt)

        # ✅ сортировка: ?ordering=-created_at|amount|category_title|... (whitelist)
        ordering = (qp.get("ordering") or "").strip()
        if ordering:
            allowed = {
                "created_at": "created_at",
                "amount": "amount",
                "type": "type",
                "category_title": "category__title",
                "cashbox_name": "cashbox__name",
            }
            field = allowed.get(ordering.lstrip("-"))
            if field:
                qs = qs.order_by(("-" if ordering.startswith("-") else "") + field, "-id")
                return qs

        # Пагинация требует полного порядка: created_at у операций одной секунды
        # совпадает, и без тай-брейка по id строки «прыгают» между страницами.
        return qs.order_by("-created_at", "-id")

    def create(self, request, *args, **kwargs):
        cashbox_id = request.data.get("cashbox")
        if cashbox_id:
            cb = Cashbox.objects.filter(id=cashbox_id).first()
            if cb and not cb.is_active:
                return Response(
                    {"detail": "Касса находится в архиве.", "code": "cashbox_inactive"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        _ensure_cash_period_open(self.request, self._company(), [timezone.now()])
        self._inject_company_branch_on_save(serializer)
        from apps.construction.audit import log_cashflow, cashflow_snapshot
        if isinstance(serializer.instance, CashFlow):
            log_cashflow(self.request, serializer.instance, "create", after=cashflow_snapshot(serializer.instance))

@add_permission_class(CashFlowWriteAccess)  # ТЗ Н-16
@read_only_guard("can_edit_cashbox")
class CashFlowDetailView(CompanyBranchScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = CashFlow.objects.select_related(
        "company", "branch",
        "cashbox", "cashbox__branch",
        "shift", "shift__cashier",
        "cashier",
        "category",
    )
    serializer_class = CashFlowSerializer

    def get_queryset(self):
        return self._scoped_queryset(super().get_queryset())

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        old_status = instance.status
        _ensure_cash_period_open(request, instance.company, [instance.created_at])

        # Если это заявка на редактирование или отмену
        if instance.request_kind in (CashFlow.RequestKind.EDIT, CashFlow.RequestKind.CANCEL):
            new_st = request.data.get("status")
            if new_st in (CashFlow.Status.APPROVED, CashFlow.Status.REJECTED):
                from apps.construction.services_change_requests import resolve_change_request
                resolve_change_request(instance, new_status=new_st, user=request.user)
                instance.refresh_from_db()
                from apps.construction.audit import log_cashflow
                log_cashflow(request, instance.target_flow or instance, "approve" if new_st == CashFlow.Status.APPROVED else "reject",
                             after={"request_kind": instance.request_kind, "request_id": str(instance.pk)})
                return Response(self.get_serializer(instance).data)

        from apps.construction.audit import log_cashflow, cashflow_snapshot
        before = cashflow_snapshot(instance)
        resp = super().update(request, *args, **kwargs)
        instance.refresh_from_db()
        log_cashflow(request, instance, "update", before=before, after=cashflow_snapshot(instance))
        if old_status != CashFlow.Status.REJECTED and instance.status == CashFlow.Status.REJECTED:
            from apps.construction.auto_cashflow import handle_cashflow_reject
            handle_cashflow_reject(instance, user=request.user)
        return resp

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        _ensure_cash_period_open(request, instance.company, [instance.created_at])
        from apps.construction.audit import log_cashflow, cashflow_snapshot
        log_cashflow(request, instance, "delete", before=cashflow_snapshot(instance))
        return super().destroy(request, *args, **kwargs)


@read_only_guard("can_edit_cashbox")
class CashFlowEditRequestView(CompanyBranchScopedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        qs = self._scoped_queryset(CashFlow.objects.select_for_update())
        target_flow = qs.filter(id=pk).first()
        if not target_flow:
            raise NotFound({"detail": "Движение кассы не найдено."})
        _ensure_cash_period_open(request, target_flow.company, [target_flow.created_at])

        if target_flow.request_kind:
            return Response(
                {"detail": "Нельзя создавать заявку на изменение для другой заявки."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if target_flow.status != CashFlow.Status.APPROVED:
            return Response(
                {"detail": "Редактировать можно только одобренные движения."},
                status=status.HTTP_409_CONFLICT,
            )

        if target_flow.shift_id and target_flow.shift.status == CashShift.Status.CLOSED:
            return Response(
                {"detail": "Движение относится к закрытой смене. Изменения по закрытым сменам запрещены."},
                status=422,
            )

        ser = CashFlowEditRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        proposed = ser.validated_data["proposed"]
        reason = ser.validated_data.get("reason", "").strip()
        idempotency_key = ser.validated_data.get("idempotency_key", "").strip() or None

        if idempotency_key:
            existing = CashFlow.objects.filter(
                company=target_flow.company,
                idempotency_key=idempotency_key,
            ).first()
            if existing:
                serializer = CashFlowSerializer(existing, context={"request": request})
                return Response(serializer.data, status=status.HTTP_200_OK)

        if not getattr(target_flow.company, "cashflow_requests_enabled", False):
            if not _is_owner_like(request.user):
                raise PermissionDenied("У вас нет прав на редактирование одобренного движения.")

            prop_type = proposed.get("type", target_flow.type)
            prop_amount = Decimal(str(proposed.get("amount", target_flow.amount)))
            prop_name = proposed.get("name") or target_flow.name

            target_flow.type = prop_type
            target_flow.amount = prop_amount
            target_flow.name = prop_name
            target_flow.save(update_fields=["type", "amount", "name"])

            from apps.construction.services_change_requests import send_cashflow_ws_notification
            send_cashflow_ws_notification(
                target_flow.company_id,
                "market.cashflow.updated",
                {
                    "cashflow_id": str(target_flow.id),
                    "type": target_flow.type,
                    "amount": str(target_flow.amount),
                    "cashbox_id": str(target_flow.cashbox_id) if target_flow.cashbox_id else None,
                    "status": target_flow.status,
                },
            )
            serializer = CashFlowSerializer(target_flow, context={"request": request})
            return Response(serializer.data, status=status.HTTP_200_OK)

        open_req = CashFlow.objects.filter(
            company=target_flow.company,
            target_flow=target_flow,
            status=CashFlow.Status.PENDING,
        ).first()
        if open_req:
            return Response(
                {
                    "detail": "По этому движению уже есть открытая заявка.",
                    "existing_request_id": str(open_req.id),
                },
                status=status.HTTP_409_CONFLICT,
            )

        prop_type = proposed.get("type", target_flow.type)
        prop_amount = Decimal(str(proposed.get("amount", target_flow.amount)))
        prop_name = proposed.get("name") or target_flow.name

        req_flow = CashFlow.objects.create(
            company=target_flow.company,
            branch=target_flow.branch,
            cashbox=target_flow.cashbox,
            shift=target_flow.shift if (target_flow.shift and target_flow.shift.status == CashShift.Status.OPEN) else None,
            status=CashFlow.Status.PENDING,
            request_kind=CashFlow.RequestKind.EDIT,
            target_flow=target_flow,
            proposed=proposed,
            reason=reason,
            requested_by=request.user,
            cashier=request.user,
            type=prop_type,
            amount=prop_amount,
            name=prop_name,
            idempotency_key=idempotency_key,
        )

        from apps.construction.audit import log_cashflow
        log_cashflow(request, target_flow, "edit_request", after={"proposed": {"type": str(prop_type), "amount": str(prop_amount), "name": prop_name}, "reason": reason, "request_id": str(req_flow.pk)})
        serializer = CashFlowSerializer(req_flow, context={"request": request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)


@read_only_guard("can_edit_cashbox")
@pin_guard("POST")
class CashFlowCancelRequestView(CompanyBranchScopedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        qs = self._scoped_queryset(CashFlow.objects.select_for_update())
        target_flow = qs.filter(id=pk).first()
        if not target_flow:
            raise NotFound({"detail": "Движение кассы не найдено."})
        _ensure_cash_period_open(request, target_flow.company, [target_flow.created_at])

        if target_flow.request_kind:
            return Response(
                {"detail": "Нельзя создавать заявку на отмену для другой заявки."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if target_flow.status != CashFlow.Status.APPROVED:
            return Response(
                {"detail": "Отменять можно только одобренные движения."},
                status=status.HTTP_409_CONFLICT,
            )

        if target_flow.shift_id and target_flow.shift.status == CashShift.Status.CLOSED:
            return Response(
                {"detail": "Движение относится к закрытой смене. Изменения по закрытым сменам запрещены."},
                status=422,
            )

        ser = CashFlowCancelRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        reason = ser.validated_data.get("reason", "").strip()
        idempotency_key = ser.validated_data.get("idempotency_key", "").strip() or None

        if idempotency_key:
            existing = CashFlow.objects.filter(
                company=target_flow.company,
                idempotency_key=idempotency_key,
            ).first()
            if existing:
                serializer = CashFlowSerializer(existing, context={"request": request})
                return Response(serializer.data, status=status.HTTP_200_OK)

        if not getattr(target_flow.company, "cashflow_requests_enabled", False):
            if not _is_owner_like(request.user):
                raise PermissionDenied("У вас нет прав на отмену одобренного движения.")

            target_flow.status = CashFlow.Status.REJECTED
            target_flow.save(update_fields=["status"])

            from apps.construction.services_change_requests import send_cashflow_ws_notification
            send_cashflow_ws_notification(
                target_flow.company_id,
                "market.cashflow.deleted",
                {
                    "cashflow_id": str(target_flow.id),
                    "cashbox_id": str(target_flow.cashbox_id) if target_flow.cashbox_id else None,
                    "status": target_flow.status,
                },
            )
            serializer = CashFlowSerializer(target_flow, context={"request": request})
            return Response(serializer.data, status=status.HTTP_200_OK)

        open_req = CashFlow.objects.filter(
            company=target_flow.company,
            target_flow=target_flow,
            status=CashFlow.Status.PENDING,
        ).first()
        if open_req:
            return Response(
                {
                    "detail": "По этому движению уже есть открытая заявка.",
                    "existing_request_id": str(open_req.id),
                },
                status=status.HTTP_409_CONFLICT,
            )

        rev_type = (
            CashFlow.Type.EXPENSE
            if target_flow.type == CashFlow.Type.INCOME
            else CashFlow.Type.INCOME
        )
        name = f"Отмена: {target_flow.name or ''}".strip()

        req_flow = CashFlow.objects.create(
            company=target_flow.company,
            branch=target_flow.branch,
            cashbox=target_flow.cashbox,
            shift=target_flow.shift if (target_flow.shift and target_flow.shift.status == CashShift.Status.OPEN) else None,
            status=CashFlow.Status.PENDING,
            request_kind=CashFlow.RequestKind.CANCEL,
            target_flow=target_flow,
            proposed={},
            reason=reason,
            requested_by=request.user,
            cashier=request.user,
            type=rev_type,
            amount=target_flow.amount,
            name=name,
            source_kind=CashFlow.SourceKind.CASHFLOW_CANCEL,
            idempotency_key=idempotency_key,
        )

        from apps.construction.audit import log_cashflow
        log_cashflow(request, target_flow, "cancel_request", after={"request_id": str(req_flow.pk)})
        serializer = CashFlowSerializer(req_flow, context={"request": request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)


@read_only_guard("can_edit_cashbox")
class CashFlowCategoryListCreateView(CompanyBranchScopedMixin, generics.ListCreateAPIView):
    queryset = CashFlowCategory.objects.select_related("company", "branch")
    serializer_class = CashFlowCategorySerializer
    pagination_class = None

    def get_queryset(self):
        if self.request.method == "GET":
            from apps.construction.categories import ensure_default_categories

            ensure_default_categories(self._company())
            from apps.construction.categories import ensure_system_categories

            ensure_system_categories(self._company())  # ТЗ Н-19
        qs = self._scoped_queryset(super().get_queryset()).order_by("title")
        qp = self.request.query_params
        if qp.get("type") in ("income", "expense"):
            qs = qs.filter(type__in=[qp["type"], "both"])
        if (qp.get("archived") or "").lower() in ("true", "1"):
            qs = qs.filter(archived=True)
        elif (qp.get("archived") or "").lower() in ("false", "0"):
            qs = qs.filter(archived=False)
        q = (self.request.query_params.get("search") or "").strip()
        if q:
            qs = qs.filter(title__icontains=q)
        return qs

    def perform_create(self, serializer):
        # Категория: company/branch задаются в CashFlowCategorySerializer (branch опционально, null = на всю компанию).
        serializer.save()


@read_only_guard("can_edit_cashbox")
class CashFlowCategoryDetailView(CompanyBranchScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = CashFlowCategory.objects.select_related("company", "branch")
    serializer_class = CashFlowCategorySerializer

    def get_queryset(self):
        return self._scoped_queryset(super().get_queryset())

    def destroy(self, request, *args, **kwargs):
        # ТЗ Н-19: системные статьи не удаляются.
        obj = self.get_object()
        if obj.is_system or obj.slug:
            return Response(
                {"detail": "Системную статью нельзя удалить.", "code": "category_is_system"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)


# ─────────────────────────────────────────────────────────────
# OWNER-ONLY VIEWS
# ─────────────────────────────────────────────────────────────
class CashboxOwnerDetailView(CompanyBranchScopedMixin, generics.ListAPIView):
    serializer_class = CashboxWithFlowsSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            qs = Cashbox.objects.select_related("company", "branch")
        else:
            company = _get_company(user)
            if not (company and _is_owner_like(user)):
                raise PermissionDenied("Только владельцы/админы могут просматривать кассы.")
            qs = Cashbox.objects.filter(company=company).select_related("company", "branch")
        return self._scoped_queryset(qs)


class CashboxOwnerDetailSingleView(CompanyBranchScopedMixin, generics.RetrieveAPIView):
    serializer_class = CashboxWithFlowsSerializer
    pagination_class = PageNumberPagination

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            qs = Cashbox.objects.select_related("company", "branch")
        else:
            company = _get_company(user)
            if not (company and _is_owner_like(user)):
                return Cashbox.objects.none()
            qs = Cashbox.objects.filter(company=company).select_related("company", "branch")
        return self._scoped_queryset(qs)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        flows_qs = (
            CashFlow.objects.filter(cashbox_id=instance.id)
            .select_related("category", "cashier", "shift", "shift__cashier")
            .order_by("-created_at")
        )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(flows_qs, request, view=self)
        context = self.get_serializer_context()
        flows_data = CashFlowInsideCashboxSerializer(page, many=True, context=context).data
        payload = {
            "id": instance.id,
            "company": instance.company_id,
            "branch": instance.branch_id,
            "name": instance.name,
            "is_consumption": instance.is_consumption,
            "cashflows": flows_data,
            "count": paginator.page.paginator.count,
            "next": paginator.get_next_link(),
            "previous": paginator.get_previous_link(),
        }
        return Response(payload)


# ─────────────────────────────────────────────────────────────
# CASHSHIFTS (СМЕНЫ)
# ─────────────────────────────────────────────────────────────
class CashShiftPagination(PageNumberPagination):
    """
    Каждая смена считается агрегатами по продажам и движениям — размер страницы
    дорогой. Дефолт оставлен прежним (100), чтобы не менять поведение клиентов,
    но теперь его можно уменьшить через ?page_size=.
    """
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 100


class CashShiftListView(CompanyBranchScopedMixin, generics.ListAPIView):
    serializer_class = CashShiftListSerializer
    pagination_class = CashShiftPagination

    def get_queryset(self):
        qs = CashShift.objects.select_related(
            "company", "branch", "cashbox", "cashbox__branch", "cashier"
        )

        qs = self._scoped_queryset(qs)

        user = self.request.user
        cashbox_id = self.request.query_params.get("cashbox")
        status_q = self.request.query_params.get("status")
        if not _is_owner_like(user) and status_q != CashShift.Status.OPEN:
            qs = qs.filter(cashier=user)
        if cashbox_id:
            qs = qs.filter(cashbox_id=cashbox_id)
        if status_q in ("open", "closed"):
            qs = qs.filter(status=status_q)

        # Новые смены сверху
        return qs.order_by("-opened_at", "-id")


def build_shift_report(shift: CashShift) -> dict:
    """Сводка смены для владельца/кассы: продажи по способам оплаты, скидки, возвраты, внесения, изъятия."""
    from apps.main.models import Sale, SalePayment, SaleReturn

    z = Decimal("0.00")
    q2 = lambda v: str((v or z).quantize(Decimal("0.01")))  # noqa: E731

    totals = shift.calc_live_totals(refresh=True)
    revenue_statuses = [Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED]
    sales = Sale.objects.filter(shift=shift)
    revenue = sales.filter(status__in=revenue_statuses)

    by_payment = {}
    for row in (
        SalePayment.objects.filter(sale__in=revenue.exclude(payment_method=Sale.PaymentMethod.MIXED))
        .values("method")
        .annotate(v=Sum("amount"))
    ):
        by_payment[row["method"]] = by_payment.get(row["method"], z) + (row["v"] or z)
    mixed_cash = mixed_card = z
    for row in (
        SalePayment.objects.filter(sale__in=revenue.filter(payment_method=Sale.PaymentMethod.MIXED))
        .values("method")
        .annotate(v=Sum("amount"))
    ):
        if row["method"] == Sale.PaymentMethod.CASH:
            mixed_cash += row["v"] or z
        else:
            mixed_card += row["v"] or z
    debt = z
    for row in sales.filter(status=Sale.Status.DEBT).values("total", "cash_received"):
        debt += max((row["total"] or z) - (row["cash_received"] or z), z)

    returns = shift.returns_summary()
    drawer = (
        CashFlow.objects.filter(shift=shift, status=CashFlow.Status.APPROVED, request_kind__isnull=True)
        .values("source_kind")
        .annotate(v=Sum("amount"))
    )
    drawer = {r["source_kind"]: r["v"] or z for r in drawer}

    expected = totals.get("expected_cash", shift.expected_cash)
    counted = shift.closing_cash if shift.status == CashShift.Status.CLOSED else None

    # ТЗ Н-05. Правило предоплаты по чеку в долг: в старом ключе `cash_sales` она уже
    # лежит (cash_sales_total + debt_prepayments_cash — числа не меняем), а в
    # `cash_in_drawer_breakdown` — РОВНО один раз, строкой `prepayments_cash`;
    # строка `cash_sales` раскладки — только чистые наличные продажи без предоплат.
    debt_prepay_cash = totals.get("debt_prepayments_cash") or z
    debt_prepay_noncash = totals.get("debt_prepayments_noncash") or z
    debt_paid_cash = totals.get("debt_payments_cash") or z
    # Долговая часть чеков смены (debt_initial), без отменённых.
    credit_total = (
        sales.filter(
            status__in=[Sale.Status.DEBT, Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED], debt_initial__gt=0
        ).aggregate(s=Sum("debt_initial"))["s"]
        or z
    )
    debt_paid_all = (
        shift.shift_flows.filter(
            status=CashFlow.Status.APPROVED,
            request_kind__isnull=True,
            type=CashFlow.Type.INCOME,
            source_kind=CashFlow.SourceKind.DEBT_REPAYMENT,
        ).aggregate(s=Sum("amount"))["s"]
        or z
    )
    debt_paid_noncash = max(debt_paid_all - debt_paid_cash, z)
    # Возвраты нашей же смены по своим чекам: payment-строки таких чеков уже уменьшены
    # (или чек отменён), поэтому в раскладке «наличные продажи» показываем брутто, а
    # возврат — отдельной минус-строкой; сумма не меняется.
    own_returns_cash = (
        SaleReturn.objects.filter(
            shift_id=shift.id,
            sale__shift_id=shift.id,
            sale__status__in=[Sale.Status.CANCELED, Sale.Status.PARTIALLY_RETURNED],
        ).aggregate(s=Sum("refund_cash"))["s"]
        or z
    )
    deposits = drawer.get(CashFlow.SourceKind.SHIFT_DRAWER_INFLOW) or z
    withdrawals = drawer.get(CashFlow.SourceKind.SHIFT_DRAWER_OUTFLOW) or z
    collections = drawer.get(CashFlow.SourceKind.SHIFT_DRAWER_COLLECTION) or z  # ТЗ Н-19: инкассация
    cash_sales_gross = (totals.get("drawer_cash_sales_net") or z) + own_returns_cash
    # ТЗ Б-07: платежи поставщикам и выплаты зарплаты наличными из ящика — отдельными строками.
    _out = {
        r["source_kind"]: r["v"] or z
        for r in shift.shift_flows.filter(
            status=CashFlow.Status.APPROVED, request_kind__isnull=True, type=CashFlow.Type.EXPENSE,
            source_kind__in=[
                CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT, CashFlow.SourceKind.WAREHOUSE_PURCHASE,
                CashFlow.SourceKind.PROCUREMENT_RECEIPT, CashFlow.SourceKind.SUPPLIER_PAYMENT,
                CashFlow.SourceKind.SALARY_PAYOUT,
            ],
        ).values("source_kind").annotate(v=Sum("amount"))
    }
    supplier_cash = sum(
        (v for k, v in _out.items() if k != CashFlow.SourceKind.SALARY_PAYOUT), z
    )
    salary_cash = _out.get(CashFlow.SourceKind.SALARY_PAYOUT) or z
    rows = [
        ("opening", shift.opening_cash or z),
        ("cash_sales_excl_prepayments", cash_sales_gross),
        ("prepayments_cash", debt_prepay_cash),
        ("debt_payments_cash", debt_paid_cash),
        ("returns_cash", -own_returns_cash),
        ("deposits", deposits),
        ("withdrawals", -withdrawals),
    ]
    if supplier_cash != z:
        rows.append(("supplier_payments_cash", -supplier_cash))
    if salary_cash != z:
        rows.append(("salary_payouts_cash", -salary_cash))
    if collections != z:
        rows.append(("collections", -collections))
    # Прочие приходы/расходы ящика (не внесение/изъятие/долг) — чтобы сумма сходилась с expected_cash.
    other = (
        (totals.get("non_sale_income") or z) - (totals.get("expense_total") or z)
        - debt_paid_cash - deposits + withdrawals + collections + supplier_cash + salary_cash
    )
    if other != z:
        rows.append(("other_cash", other))
    rows.append(("expected_cash", expected))

    return {
        "shift": str(shift.id),
        "status": shift.status,
        "cashbox": str(shift.cashbox_id),
        "cashier": str(shift.cashier_id),
        "opened_at": shift.opened_at.isoformat() if shift.opened_at else None,
        "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
        "opening_cash": q2(shift.opening_cash),
        "sales_count": totals.get("sales_count", 0),
        "sales_total": q2(totals.get("sales_total")),
        "cash_sales": q2(totals.get("cash_sales_total")),
        "noncash_sales": q2(totals.get("noncash_sales_total")),
        "debt_prepayments_cash": q2(totals.get("debt_prepayments_cash")),
        "debt_prepayments_noncash": q2(totals.get("debt_prepayments_noncash")),
        "debt_payments_cash": q2(totals.get("debt_payments_cash")),
        "by_payment": {
            **{k: q2(v) for k, v in sorted(by_payment.items())},
            "mixed_cash": q2(mixed_cash),
            "mixed_card": q2(mixed_card),
            "debt": q2(debt),
        },
        "discounts": q2(sales.exclude(status=Sale.Status.CANCELED).aggregate(s=Sum("discount_total"))["s"]),
        "bonus_redeemed": q2(sales.exclude(status=Sale.Status.CANCELED).aggregate(s=Sum("bonus_redeemed"))["s"]),
        **returns,
        "deposits": q2(drawer.get(CashFlow.SourceKind.SHIFT_DRAWER_INFLOW)),
        "withdrawals": q2(drawer.get(CashFlow.SourceKind.SHIFT_DRAWER_OUTFLOW)),
        "collections": q2(collections),
        "income_total": q2(totals.get("income_total")),
        "expense_total": q2(totals.get("expense_total")),
        "expected_cash": q2(expected),
        "counted_cash": q2(counted) if counted is not None else None,
        "difference": q2(counted - expected) if counted is not None else None,
        # ТЗ Н-05: выручка без долга = продажи смены минус долговая часть (предоплата входит).
        # sales_total уже за вычетом возвратов (Sale.total уменьшается, полные возвраты отменены),
        # поэтому повторно вычитать возвраты не нужно: includes_returns = false.
        "revenue_excl_debt": q2((totals.get("sales_total") or z) - credit_total),
        "revenue_excl_debt_includes_returns": False,
        "credit_sales_total": q2(credit_total),
        "debt_repaid_total": q2(debt_paid_all),
        "debt_repaid_cash": q2(debt_paid_cash),
        "debt_repaid_noncash": q2(debt_paid_noncash),
        "prepayment_total": q2(debt_prepay_cash + debt_prepay_noncash),
        "closing_cash": q2(counted) if counted is not None else None,
        # Правило Б-07: cash_sales = cash_sales_excl_prepayments + prepayments_cash (оба — после возвратов).
        # Строка раскладки с тем же кодом — брутто (возврат отдельной строкой returns_cash).
        "cash_sales_excl_prepayments": q2((totals.get("cash_sales_total") or z) - debt_prepay_cash),
        "cash_in_drawer_breakdown": [{"code": c, "amount": q2(a)} for c, a in rows],
    }


class CashShiftReportView(CompanyBranchScopedMixin, generics.GenericAPIView):
    """GET /api/construction/shifts/{id}/report/"""

    def get_queryset(self):
        qs = self._scoped_queryset(CashShift.objects.select_related("cashbox", "cashier"))
        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(Q(status=CashShift.Status.OPEN) | Q(cashier=user))
        return qs

    def get(self, request, *args, **kwargs):
        return Response(build_shift_report(self.get_object()))


class CashShiftDetailView(CompanyBranchScopedMixin, generics.RetrieveAPIView):
    serializer_class = CashShiftListSerializer

    def get_queryset(self):
        qs = CashShift.objects.select_related(
            "company", "branch", "cashbox", "cashbox__branch", "cashier"
        )
        qs = self._scoped_queryset(qs)

        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(Q(status=CashShift.Status.OPEN) | Q(cashier=user))

        return qs

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        shift = self.get_object()
        # Ключ есть всегда (null — если предупреждать не о чем), чтобы фронту не
        # приходилось различать «поля нет» и «поле пустое». У закрытой смены
        # предупреждать уже поздно и не о чем.
        response.data["cashbox_warning"] = (
            shift.cashbox_shared_warning() if shift.status == CashShift.Status.OPEN else None
        )
        return response


class CashShiftOpenView(CompanyBranchScopedMixin, generics.CreateAPIView):
    permission_classes = [permissions.IsAuthenticated, PosAccess]  # ТЗ Н-16
    """
    ✅ Открыть смену:
      - кассир открывает себе
      - owner/admin может открыть на другого кассира (cashier)
    ✅ atomic нужен для select_for_update в serializer.validate
    """
    serializer_class = CashShiftOpenSerializer

    @idempotent
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        shift = serializer.save()
        notify_shift_opened(shift, actor=request.user)
        schedule_after_commit(sync_ekassa_after_local_shift_open_by_id, shift.id)
        out = CashShiftListSerializer(shift, context={"request": request}).data
        # Не блокируем открытие: бывают легитимные схемы с общей кассой. Но если на
        # этой кассе уже открыта чужая смена — показываем, чья и сколько по её данным
        # в ящике, чтобы кассир заметил чужую кассу до закрытия смены.
        out["cashbox_warning"] = shift.cashbox_shared_warning()
        return Response(out, status=201)


class CashShiftCloseView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @idempotent
    @transaction.atomic
    def post(self, request, pk):
        company = _get_company(request.user)
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")

        shift = get_object_or_404(
            CashShift.objects.select_related("company", "cashier", "cashbox"),
            id=pk,
            company=company,
        )

        user = request.user
        if not _is_owner_like(user) and shift.cashier_id != user.id:
            raise PermissionDenied("Нельзя закрыть чужую смену.")

        ser = CashShiftCloseSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            ser.save(shift=shift)
        except Exception as e:
            raise ValidationError(str(e))

        notify_shift_closed(shift, actor=request.user)
        schedule_after_commit(sync_ekassa_after_local_shift_close_by_id, shift.id)
        out = CashShiftListSerializer(shift, context={"request": request}).data
        return Response(out, status=200)


@read_only_guard("can_edit_cashbox")
@flag_guard("can_view_cashbox")  # ТЗ Н-16: касса компании — запись только с правом «Кассы»
class CashFlowBulkStatusUpdateView(CompanyBranchScopedMixin, generics.GenericAPIView):
    serializer_class = CashFlowBulkStatusSerializer
    CHUNK_SIZE = 1000

    @transaction.atomic
    def patch(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)

        items = ser.validated_data.get("items") or []
        if not items:
            return Response({"count": 0, "updated_ids": []}, status=200)

        id_to_status = {}
        for it in items:
            _id = it["id"]
            id_to_status[_id] = it["status"]

        ids = list(id_to_status.keys())

        qs = self._scoped_queryset(CashFlow.objects.filter(id__in=ids))

        existing_ids = set(qs.values_list("id", flat=True))
        missing = [str(i) for i in ids if i not in existing_ids]
        if missing:
            raise ValidationError({"missing_ids": missing})

        old_flows = list(qs)
        for cf in old_flows:  # закрытый период: смена статуса операций до closed_until запрещена
            _ensure_cash_period_open(request, cf.company, [cf.created_at])
        updated_ids = []
        updated_count = 0
        _before_status = {cf.id: cf.status for cf in old_flows}

        from apps.construction.services_change_requests import resolve_change_request
        from apps.construction.auto_cashflow import handle_cashflow_reject

        change_reqs = [cf for cf in old_flows if cf.request_kind in (CashFlow.RequestKind.EDIT, CashFlow.RequestKind.CANCEL)]
        normal_flows = [cf for cf in old_flows if cf.request_kind not in (CashFlow.RequestKind.EDIT, CashFlow.RequestKind.CANCEL)]

        for cf in change_reqs:
            new_st = id_to_status.get(cf.id)
            if new_st in (CashFlow.Status.APPROVED, CashFlow.Status.REJECTED):
                resolve_change_request(cf, new_status=new_st, user=request.user)
                updated_count += 1
                updated_ids.append(str(cf.id))

        # Отклонённое движение через массовую смену статуса не «воскрешаем»:
        # при отклонении уже отработал handle_cashflow_reject.
        resurrect = [
            str(cf.id) for cf in normal_flows
            if cf.status == CashFlow.Status.REJECTED and id_to_status.get(cf.id) != CashFlow.Status.REJECTED
        ]
        if resurrect:
            raise ValidationError({"detail": "Отклонённые движения нельзя вернуть.", "ids": resurrect})
        company = getattr(request.user, "company", None) or getattr(request.user, "owned_company", None)
        if (
            not _is_owner_like(request.user)
            and getattr(company, "cashflow_requests_enabled", False)
            and any(
                id_to_status.get(cf.id) == CashFlow.Status.APPROVED and cf.status != CashFlow.Status.APPROVED
                for cf in normal_flows
            )
        ):
            raise PermissionDenied("Одобрять движения может только владелец или администратор.")

        if normal_flows:
            normal_ids = [cf.id for cf in normal_flows]
            for i in range(0, len(normal_ids), self.CHUNK_SIZE):
                chunk_ids = normal_ids[i:i + self.CHUNK_SIZE]

                whens = [
                    When(id=_id, then=Value(id_to_status[_id]))
                    for _id in chunk_ids
                ]

                chunk_qs = qs.filter(id__in=chunk_ids)

                updated_count += chunk_qs.update(
                    status=Case(*whens, output_field=CharField())
                )
                updated_ids.extend([str(x) for x in chunk_ids])

            for cf in normal_flows:
                new_st = id_to_status.get(cf.id)
                if cf.status != CashFlow.Status.REJECTED and new_st == CashFlow.Status.REJECTED:
                    cf.status = CashFlow.Status.REJECTED
                    handle_cashflow_reject(cf, user=request.user)

        from apps.construction.audit import log_cashflow
        for cf in old_flows:
            new_st = id_to_status.get(cf.id)
            if new_st and new_st != _before_status.get(cf.id):
                log_cashflow(request, cf, "update", before={"status": _before_status.get(cf.id)}, after={"status": new_st})

        return Response(
            {"count": updated_count, "updated_ids": updated_ids},
            status=200
        )


import zoneinfo
from datetime import datetime, date, timedelta, time
import calendar
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError


def _parse_date_param(val, param_name="date"):
    if not val:
        raise ValidationError({"detail": f"Query param '{param_name}' is required."})
    try:
        return datetime.strptime(str(val).strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise ValidationError({"detail": f"Invalid date format for '{param_name}'. Expected YYYY-MM-DD."})


def _parse_month_param(val):
    if not val:
        raise ValidationError({"detail": "Query param 'month' is required when period=month."})
    try:
        parts = str(val).strip().split("-")
        if len(parts) != 2:
            raise ValueError
        year, month = int(parts[0]), int(parts[1])
        if not (1 <= month <= 12):
            raise ValueError
        return year, month
    except Exception:
        raise ValidationError({"detail": "Invalid month format. Expected YYYY-MM."})


class CashboxReportView(CompanyBranchScopedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, cashbox_id, *args, **kwargs):
        cashboxes = self._scoped_queryset(Cashbox.objects.all())
        cashbox = cashboxes.filter(id=cashbox_id).first()
        if not cashbox:
            company = self._company()
            if company and Cashbox.objects.filter(id=cashbox_id, company=company).exists():
                raise PermissionDenied("У вас нет доступа к этой кассе.")
            raise NotFound({"detail": "Касса не найдена."})

        period = (request.query_params.get("period") or "").strip().lower()
        if period not in ("day", "month"):
            raise ValidationError({"detail": "Query param 'period' must be 'day' or 'month'."})

        status_param = (request.query_params.get("status") or "approved").strip().lower()

        tz = zoneinfo.ZoneInfo("Asia/Bishkek")

        flows_qs = CashFlow.objects.filter(cashbox=cashbox)
        if status_param in ("approved", "true"):
            flows_qs = flows_qs.filter(status__in=["approved", "true"], request_kind__isnull=True)
        elif status_param == "pending":
            flows_qs = flows_qs.filter(status="pending")

        if period == "day":
            raw_date = request.query_params.get("date")
            target_date = _parse_date_param(raw_date, "date")

            start_dt = datetime.combine(target_date, time.min, tzinfo=tz)
            end_dt = datetime.combine(target_date, time.max, tzinfo=tz)

            day_flows_qs = flows_qs.filter(created_at__gte=start_dt, created_at__lte=end_dt).order_by("created_at")

            inc = day_flows_qs.filter(type="income").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
            exp = day_flows_qs.filter(type="expense").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
            cnt = day_flows_qs.count()
            net = inc - exp

            truncated = cnt > 5000
            flows_list = day_flows_qs[:5000] if truncated else day_flows_qs

            serialized_flows = [
                {
                    "id": str(cf.id),
                    "type": cf.type,
                    "amount": f"{cf.amount:.2f}",
                    "name": cf.name or "",
                    "title": cf.name or "",
                    "created_at": cf.created_at.isoformat(),
                    "status": cf.status,
                }
                for cf in flows_list
            ]

            return Response({
                "cashbox_id": str(cashbox.id),
                "is_active": cashbox.is_active,
                "period": "day",
                "date": target_date.isoformat(),
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
                "status_filter": status_param,
                "summary": {
                    "total_income": f"{inc:.2f}",
                    "total_expense": f"{exp:.2f}",
                    "net": f"{net:.2f}",
                    "operations_count": cnt,
                },
                "flows": serialized_flows,
                "truncated": truncated,
                "complete": not truncated,
            }, status=200)

        else:
            raw_month = request.query_params.get("month")
            year, month = _parse_month_param(raw_month)

            first_day = date(year, month, 1)
            last_day_num = calendar.monthrange(year, month)[1]
            last_day = date(year, month, last_day_num)

            start_dt = datetime.combine(first_day, time.min, tzinfo=tz)
            end_dt = datetime.combine(last_day, time.max, tzinfo=tz)

            month_flows_qs = flows_qs.filter(created_at__gte=start_dt, created_at__lte=end_dt)

            total_inc = month_flows_qs.filter(type="income").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
            total_exp = month_flows_qs.filter(type="expense").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
            total_cnt = month_flows_qs.count()
            total_net = total_inc - total_exp

            truncated = total_cnt > 5000

            all_flows = list(month_flows_qs.order_by("created_at"))
            flows_by_day = {}
            for cf in all_flows:
                cf_date = cf.created_at.astimezone(tz).date()
                if cf_date not in flows_by_day:
                    flows_by_day[cf_date] = []
                flows_by_day[cf_date].append(cf)

            days_result = []
            curr_date = first_day
            total_serialized = 0
            while curr_date <= last_day:
                cfs = flows_by_day.get(curr_date, [])
                day_inc = sum((cf.amount for cf in cfs if cf.type == "income"), Decimal("0.00"))
                day_exp = sum((cf.amount for cf in cfs if cf.type == "expense"), Decimal("0.00"))
                day_net = day_inc - day_exp
                day_cnt = len(cfs)

                day_flows_serialized = []
                for cf in cfs:
                    if total_serialized < 5000:
                        day_flows_serialized.append({
                            "id": str(cf.id),
                            "type": cf.type,
                            "amount": f"{cf.amount:.2f}",
                            "name": cf.name or "",
                            "title": cf.name or "",
                            "created_at": cf.created_at.isoformat(),
                            "status": cf.status,
                        })
                        total_serialized += 1

                days_result.append({
                    "date": curr_date.isoformat(),
                    "summary": {
                        "total_income": f"{day_inc:.2f}",
                        "total_expense": f"{day_exp:.2f}",
                        "net": f"{day_net:.2f}",
                        "operations_count": day_cnt,
                    },
                    "flows": day_flows_serialized,
                })
                curr_date += timedelta(days=1)

            month_str = f"{year:04d}-{month:02d}"
            return Response({
                "cashbox_id": str(cashbox.id),
                "is_active": cashbox.is_active,
                "period": "month",
                "month": month_str,
                "date_from": first_day.isoformat(),
                "date_to": last_day.isoformat(),
                "status_filter": status_param,
                "summary": {
                    "total_income": f"{total_inc:.2f}",
                    "total_expense": f"{total_exp:.2f}",
                    "net": f"{total_net:.2f}",
                    "operations_count": total_cnt,
                },
                "days": days_result,
                "truncated": truncated,
                "complete": not truncated,
            }, status=200)


RU_MONTHS = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


class CashboxReportAnalyticsView(CompanyBranchScopedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = self._company()
        if not company:
            raise PermissionDenied("Компания не найдена.")

        tz = zoneinfo.ZoneInfo("Asia/Bishkek")
        now_bishkek = timezone.now().astimezone(tz)

        raw_df = request.query_params.get("date_from")
        raw_dt = request.query_params.get("date_to")

        if raw_df:
            d_from = _parse_date_param(raw_df, "date_from")
        else:
            d_from = date(now_bishkek.year, 1, 1)

        if raw_dt:
            d_to = _parse_date_param(raw_dt, "date_to")
        else:
            d_to = now_bishkek.date()

        cashbox_id = request.query_params.get("cashbox")
        status_param = (request.query_params.get("status") or "approved").strip().lower()

        # Компания + филиал сотрудника: итоги другого филиала не отдаём.
        flows_qs = self._scoped_queryset(CashFlow.objects.all())
        if cashbox_id:
            flows_qs = flows_qs.filter(cashbox_id=cashbox_id)
        else:
            flows_qs = flows_qs.filter(cashbox__is_active=True)

        if status_param in ("approved", "true"):
            flows_qs = flows_qs.filter(status__in=["approved", "true"], request_kind__isnull=True)
        elif status_param == "pending":
            flows_qs = flows_qs.filter(status="pending")

        start_dt = datetime.combine(d_from, time.min, tzinfo=tz)
        end_dt = datetime.combine(d_to, time.max, tzinfo=tz)

        flows_qs = flows_qs.filter(created_at__gte=start_dt, created_at__lte=end_dt)

        total_inc = flows_qs.filter(type="income").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        total_exp = flows_qs.filter(type="expense").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        total_cnt = flows_qs.count()
        total_net = total_inc - total_exp

        all_flows = list(flows_qs.order_by("created_at"))
        month_buckets = {}

        curr_y, curr_m = d_from.year, d_from.month
        end_y, end_m = d_to.year, d_to.month
        while (curr_y, curr_m) <= (end_y, end_m):
            m_key = f"{curr_y:04d}-{curr_m:02d}"
            label = f"{RU_MONTHS[curr_m - 1]} {curr_y}"
            month_buckets[m_key] = {"period": m_key, "label": label, "income": Decimal("0.00"), "expense": Decimal("0.00"), "cnt": 0}
            curr_m += 1
            if curr_m > 12:
                curr_m = 1
                curr_y += 1

        for cf in all_flows:
            cf_dt = cf.created_at.astimezone(tz)
            m_key = f"{cf_dt.year:04d}-{cf_dt.month:02d}"
            if m_key in month_buckets:
                if cf.type == "income":
                    month_buckets[m_key]["income"] += cf.amount
                elif cf.type == "expense":
                    month_buckets[m_key]["expense"] += cf.amount
                month_buckets[m_key]["cnt"] += 1

        groups_result = []
        for m_key in sorted(month_buckets.keys()):
            b = month_buckets[m_key]
            inc = b["income"]
            exp = b["expense"]
            groups_result.append({
                "period": b["period"],
                "label": b["label"],
                "income": f"{inc:.2f}",
                "expense": f"{exp:.2f}",
                "net": f"{(inc - exp):.2f}",
                "operations_count": b["cnt"],
            })

        return Response({
            "cashbox_id": str(cashbox_id) if cashbox_id else None,
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "group_by": "month",
            "status_filter": status_param,
            "summary": {
                "total_income": f"{total_inc:.2f}",
                "total_expense": f"{total_exp:.2f}",
                "net": f"{total_net:.2f}",
                "operations_count": total_cnt,
            },
            "groups": groups_result,
            "truncated": False,
            "complete": True,
        }, status=200)
