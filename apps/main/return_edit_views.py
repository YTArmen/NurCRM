"""ТЗ 10.11: правка/аннулирование/история возврата поставщику (логика — apps/main/return_edit.py)."""
from django.shortcuts import get_object_or_404
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main import return_edit as re_
from apps.main.models import SupplierReturn
from apps.main.serializers import SupplierReturnReadSerializer
from apps.main.views import CompanyBranchRestrictedMixin, SupplierReturnRetrieveAPIView
from apps.users.permissions import read_only_guard
from core.exceptions import ApiError


def _ekassa_after_return_change(r, rid=None):
    """ek1411: после правки/аннулирования возврата (ещё не пробитого) — отправить в eKassa новый состав."""
    try:
        from apps.main.models import SaleReturn
        from apps.ekassa.runtime import schedule_after_commit
        from apps.ekassa.sale_bridge import try_fiscalize_sale_return

        if not isinstance(r, SaleReturn):
            r = SaleReturn.objects.filter(pk=rid).only("id", "sale_id").first()
        if r is not None and r.sale_id:
            schedule_after_commit(try_fiscalize_sale_return, r.sale_id, r.pk)
    except Exception:  # noqa: BLE001 — eKassa не должна ломать правку возврата
        pass


def _with_key(view, request, impl, pk):
    from apps.integrations.idempotency import _request_key, idempotent

    if not _request_key(request, True):
        raise ApiError("idempotency_key_required", "Нужен заголовок Idempotency-Key (uuid).", status_code=400)
    return idempotent(body_key=True, replay_flag=True)(impl)(view, request, pk)


def card(request, ret):
    ret = SupplierReturn.objects.select_related("supplier", "receipt", "created_by", "company", "branch",
                                                "edited_by", "voided_by", "void_approved_by") \
        .prefetch_related("items", "items__product").get(pk=ret.pk)
    data = SupplierReturnReadSerializer(ret, context={"request": request}).data
    data["lines"] = data.get("items") or []
    return data


@read_only_guard("can_edit_procurement")
class SupplierReturnEditAPIView(CompanyBranchRestrictedMixin, APIView):
    """PATCH suppliers/returns/<id>/ — полный новый состав (If-Match, Idempotency-Key, X-Confirm-Token)."""

    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, pk):
        def _impl(v, req, rid):
            ret = re_.edit_supplier_return(v, req, rid)
            return Response(card(req, ret))

        return _with_key(self, request, _impl, pk)


@read_only_guard("can_edit_procurement")
class SupplierReturnVoidAPIView(CompanyBranchRestrictedMixin, APIView):
    """POST suppliers/returns/<id>/void/ {version, reason}."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        def _impl(v, req, rid):
            ret = re_.void_supplier_return(v, req, rid)
            return Response(card(req, ret))

        return _with_key(self, request, _impl, pk)


class SupplierReturnHistoryAPIView(CompanyBranchRestrictedMixin, APIView):
    """GET suppliers/returns/<id>/history/ — только владелец/администратор."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        if not re_.is_owner_like(request.user):
            raise ApiError("permission_denied", "История доступна владельцу и администратору.", status_code=403)
        ret = get_object_or_404(self._filter_qs_company_branch(SupplierReturn.objects.all()), pk=pk)
        rows = re_.history_payload(ret.company, re_.OBJ_SUPPLIER, ret.pk)
        first_before = None
        from apps.main.models import ReturnAuditLog

        first = ReturnAuditLog.objects.filter(company=ret.company, object_type=re_.OBJ_SUPPLIER, object_id=ret.pk) \
            .order_by("version_after", "at").first()
        first_before = first.snapshot_before if first is not None else re_.supplier_snapshot(ret)
        v1 = {
            "version": first.version_before if first is not None else ret.version, "action": "created",
            "at": ret.created_at.isoformat(), "user_name": re_._uname(ret.created_by), "approved_by_name": None,
            "reason": "", "total": (first_before or {}).get("total_amount"), "snapshot": first_before, "diff": [],
        }
        return Response([v1] + rows)


@read_only_guard("can_edit_procurement")
class SupplierReturnDetailEditAPIView(SupplierReturnRetrieveAPIView):
    """GET (карточка, документ виден в любом статусе) + PATCH (правка в течение 24 ч)."""

    def patch(self, request, pk):
        def _impl(v, req, rid):
            ret = re_.edit_supplier_return(v, req, rid)
            return Response(card(req, ret))

        return _with_key(self, request, _impl, pk)


# ======================================================================
#  Возврат от клиента (pos/returns/<id>/)
# ======================================================================
def _pos_card(request, pk):
    """Карточка возврата — тот же словарь, что в списке pos/returns/ (с полями правки)."""
    from apps.main.kassa_views import SaleReturnListAPIView

    view = SaleReturnListAPIView()
    view.request = request
    view.format_kwarg = None
    from apps.main.models import SaleReturn
    from apps.main.kassa_views import _return_item, _pos_edit_fields
    from apps.main.pos_utils import money
    from apps.main.tz26.pos_sync.services import return_origin_fields
    from decimal import Decimal

    r = get_object_or_404(
        SaleReturn.objects.filter(company=view._company())
        .select_related("sale", "user", "sale__client", "origin", "voided_by", "edited_by", "void_approved_by"), pk=pk)
    get_object_or_404(view._filter_qs_company_branch(type(r.sale).objects.all()), pk=r.sale_id)
    items = r.returned_items or []
    ZERO = Decimal("0.00")
    return {
        "id": str(r.id), "original_sale_id": str(r.sale_id), "sale": str(r.sale_id),
        "sale_number": r.sale.doc_number if r.sale else None,
        "employee_id": str(r.user_id) if r.user_id else None,
        "customer_id": str(r.sale.client_id) if (r.sale and r.sale.client_id) else None,
        "amount": str(money(r.returned_amount or ZERO)), "total": str(money(r.returned_amount or ZERO)),
        "total_amount": str(money(r.returned_amount or ZERO)),
        "refund_method": r.refund_method or (r.sale.payment_method if r.sale else None),
        "is_full": r.is_full, "is_defect": r.is_defect, "items": [_return_item(i, r) for i in items],
        "reason": r.reason, "cashier": str(r.user_id) if r.user_id else None,
        "shift": str(r.shift_id) if r.shift_id else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        **return_origin_fields(r), **_pos_edit_fields(r, request.user),
        **__import__("apps.main.fiscal_kassa", fromlist=["fiscal_payload"]).fiscal_payload(r),  # П0-1
    }


@read_only_guard("can_edit_procurement")
class PosReturnDetailAPIView(CompanyBranchRestrictedMixin, APIView):
    """GET pos/returns/<id>/ — карточка (любой статус); PATCH — правка в течение 24 ч."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        return Response(_pos_card(request, pk))

    def patch(self, request, pk):
        from apps.main import pos_return_edit as pre
        from apps.main.fiscal_kassa import return_locked_response  # П0-1

        _locked = return_locked_response(pk)
        if _locked is not None:
            return _locked

        def _impl(v, req, rid):
            r = pre.edit_pos_return(v, req, rid)
            _ekassa_after_return_change(r)  # ek1411
            return Response(_pos_card(req, rid))

        return _with_key(self, request, _impl, pk)


class PosReturnVoidAPIView(CompanyBranchRestrictedMixin, APIView):
    """POST pos/returns/<id>/void/ {version, reason}."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from apps.main import pos_return_edit as pre
        from apps.main.fiscal_kassa import return_locked_response  # П0-1

        _locked = return_locked_response(pk)
        if _locked is not None:
            return _locked

        def _impl(v, req, rid):
            r = pre.void_pos_return(v, req, rid)
            _ekassa_after_return_change(r, rid)  # ek1411
            return Response(_pos_card(req, rid))

        return _with_key(self, request, _impl, pk)


class PosReturnHistoryAPIView(CompanyBranchRestrictedMixin, APIView):
    """GET pos/returns/<id>/history/ — только владелец/администратор."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        from apps.main.models import ReturnAuditLog, SaleReturn
        from apps.main.pos_return_edit import pos_snapshot

        if not re_.is_owner_like(request.user):
            raise ApiError("permission_denied", "История доступна владельцу и администратору.", status_code=403)
        r = get_object_or_404(SaleReturn.objects.filter(company=self._company()).select_related("sale"), pk=pk)
        rows = re_.history_payload(r.company, re_.OBJ_POS, r.pk)
        first = ReturnAuditLog.objects.filter(company=r.company, object_type=re_.OBJ_POS, object_id=r.pk) \
            .order_by("version_after", "at").first()
        snap = first.snapshot_before if first is not None else pos_snapshot(r)
        v1 = {"version": first.version_before if first is not None else r.version, "action": "created",
              "at": r.created_at.isoformat(), "user_name": re_._uname(r.user), "approved_by_name": None,
              "reason": "", "total": (snap or {}).get("total_amount"), "snapshot": snap, "diff": []}
        return Response([v1] + rows)
