"""ТЗ 30.10 «Поступления» п.3: файл накладной у прихода (Excel, PDF, фото).

POST   api/main/suppliers/receipts/{id}/file/            multipart file → 201 {attachments: [...]}
DELETE api/main/suppliers/receipts/{id}/file/{media_id}/ → {attachments: [...]}
Файлы лежат в хранилище компании (MainMedia, purpose=invoice, лимит 5 МБ и квота — как в Н-32).

Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
from rest_framework import permissions, status
from rest_framework.generics import get_object_or_404
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.media_receipts import XLS, XLSX, _err, media_payload, store_company_media
from apps.main.models import MainMedia
from apps.main.views import CompanyBranchRestrictedMixin, SupplierReceiptQuerysetMixin

INVOICE_TYPES = {"image/png", "image/jpeg", "image/webp", "application/pdf", XLSX, XLS}


class SupplierReceiptFileAPIView(SupplierReceiptQuerysetMixin, CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def _receipt(self, pk):
        return get_object_or_404(self._supplier_receipts_base_queryset(), pk=pk)

    def _payload(self, request, receipt):
        return {"attachments": [media_payload(m, request) for m in receipt.attachments.all()]}

    def post(self, request, pk):
        receipt = self._receipt(pk)
        upload = request.FILES.get("file")
        if upload is None:
            return _err("file_required", "Файл не передан.", fields={"file": "Обязательное поле."})
        media, error = store_company_media(receipt.company, upload, MainMedia.Purpose.INVOICE, request.user,
                                           INVOICE_TYPES)
        if error is not None:
            if error.data.get("code") == "invalid_type":
                error.data["detail"] = "Накладная: Excel (xlsx, xls), PDF или фото (PNG, JPG, WebP)."
            return error
        receipt.attachments.add(media)
        return Response(self._payload(request, receipt), status=status.HTTP_201_CREATED)

    def delete(self, request, pk, media_id):
        receipt = self._receipt(pk)
        media = get_object_or_404(receipt.attachments.all(), pk=media_id)
        receipt.attachments.remove(media)
        if not media.supplier_receipts.exists():
            media.file.delete(save=False)
            media.delete()
        return Response(self._payload(request, receipt))
