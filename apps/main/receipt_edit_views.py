"""
ТЗ редактор накладных 30.10: версии прихода, откат и лента правок (логика — apps/main/receipt_edit.py).

GET  suppliers/receipts/<id>/versions/
GET  suppliers/receipts/<id>/versions/<n>/
POST suppliers/receipts/<id>/revert/ {to_version, reason, reason_code?, version?, dry_run?}
GET  suppliers/receipts/changes/?since=
"""
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import SupplierReceipt, SupplierReceiptVersion
from apps.main.receipt_edit import (
    _s,
    _user_ref,
    _validation,
    _version_row,
    _with_idempotency,
    edit_receipt,
    parse_body,
    revert_body,
)
from apps.main.views import CompanyBranchRestrictedMixin
from apps.users.permissions import read_only_guard
from core.exceptions import ApiError


class _ReceiptViewBase(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _receipt(self, pk):
        return get_object_or_404(self._filter_qs_company_branch(SupplierReceipt.objects.all()), pk=pk)


def _version_payload(row, prev_total=None, with_snapshot=False):
    out = {
        "version": row.version, "action": row.action or "edit",
        "at": row.at.isoformat() if row.at else None, "user": _user_ref(row.user),
        "device": row.device, "source": row.source, "reason": row.reason, "reason_code": row.reason_code,
        "total": _s(row.total), "total_before": _s(prev_total) if prev_total is not None else None,
        "lines_diff": row.lines_diff or [],
    }
    if with_snapshot:
        out["snapshot"] = row.snapshot or {}
    return out



class SupplierReceiptVersionsAPIView(CompanyBranchRestrictedMixin, _ReceiptViewBase):
    """GET suppliers/receipts/<id>/versions/ → [{version, action, at, user, device, reason, total, lines_diff}]"""

    def get(self, request, pk):
        receipt = self._receipt(pk)
        rows = list(SupplierReceiptVersion.objects.filter(receipt=receipt).select_related("user").order_by("version"))
        if not rows:
            rows = [_version_row(receipt, receipt.version or 1)]
        out, prev = [], None
        for r in rows:
            out.append(_version_payload(r, prev))
            prev = r.total
        return Response({"receipt_id": str(receipt.id), "current_version": receipt.version, "results": out})

class SupplierReceiptVersionDetailAPIView(CompanyBranchRestrictedMixin, _ReceiptViewBase):
    """GET suppliers/receipts/<id>/versions/<n>/ → версия со снимком (шапка и строки)."""

    def get(self, request, pk, n):
        receipt = self._receipt(pk)
        row = _version_row(receipt, int(n))
        if row is None:
            raise ApiError("version_not_found", "Нет такой версии прихода.", status_code=404)
        prev = SupplierReceiptVersion.objects.filter(receipt=receipt, version__lt=row.version).order_by("-version").first()
        return Response(_version_payload(row, prev.total if prev else None, with_snapshot=True))

@read_only_guard("can_edit_procurement")
class SupplierReceiptRevertAPIView(CompanyBranchRestrictedMixin, _ReceiptViewBase):
    """POST suppliers/receipts/<id>/revert/ {to_version, reason, reason_code?, version?, dry_run?} — как PATCH."""

    def post(self, request, pk):
        data = request.data if hasattr(request.data, "get") else {}
        try:
            to_version = int(data.get("to_version"))
        except (TypeError, ValueError):
            raise _validation({"to_version": ["Номер версии."]})
        receipt = self._receipt(pk)
        row = _version_row(receipt, to_version)
        if row is None:
            raise ApiError("version_not_found", "Нет такой версии прихода.", status_code=404)
        if to_version == receipt.version:
            raise _validation({"to_version": ["Это текущая версия."]})
        body = parse_body(revert_body(receipt, row.snapshot or {}, data), request.query_params)
        body["reason_code"] = body["reason_code"] or "other"
        if body["dry_run"]:
            return edit_receipt(self, request, pk, body, action="revert")

        def _impl(v, req, rid):
            return edit_receipt(v, req, rid, body, action="revert")

        return _with_idempotency(self, request, _impl, pk)

class SupplierReceiptChangesAPIView(CompanyBranchRestrictedMixin, _ReceiptViewBase):
    """
    GET suppliers/receipts/changes/?since=<ISO|мс>&limit=200 — правки приходов (событие receipt_changed),
    по образцу products/changes/: {since, changes[{receipt_id, version, supplier_id, edited_by, at, action}],
    cursor, server_time, server_time_ms, has_more}.
    """

    def get(self, request):
        from apps.main.product_changes import _ms, parse_since

        since = parse_since(request.query_params.get("since"))
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 200), 1000))
        except ValueError:
            raise _validation({"limit": ["Целое число 1…1000."]})
        server_time = timezone.now()
        receipts = self._filter_qs_company_branch(SupplierReceipt.objects.all())
        qs = (SupplierReceiptVersion.objects.filter(receipt__in=receipts.values("pk"))
              .exclude(action__in=("create", "baseline")).select_related("user", "receipt"))
        if since is not None:
            qs = qs.filter(at__gte=since)
        rows = list(qs.order_by("at", "id")[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        cursor = rows[-1].at if has_more and rows else server_time
        return Response({
            "since": since.isoformat() if since else None,
            "changes": [{"receipt_id": str(r.receipt_id), "version": r.version,
                         "supplier_id": str(r.receipt.supplier_id), "edited_by": _user_ref(r.user),
                         "at": r.at.isoformat(), "action": r.action} for r in rows],
            "cursor": _ms(cursor), "server_time": server_time.isoformat(), "server_time_ms": _ms(server_time),
            "has_more": has_more,
        })
