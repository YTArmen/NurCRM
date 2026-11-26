"""ТЗ 31.10 п.22: карточка и документы перемещения товара между складами.

GET    api/main/stock/transfers/{id}/                       карточка (document_number, attachments[])
PATCH  api/main/stock/transfers/{id}/                       {document_number, note}
GET    api/main/stock/transfers/{id}/file/                  {attachments: [...]}
POST   api/main/stock/transfers/{id}/file/                  multipart file → 201 {attachments: [...]}
DELETE api/main/stock/transfers/{id}/file/{media_id}/       → {attachments: [...]}
Те же адреса работают и под stock-transfers/. Файлы — как у приходов: xlsx/xls/pdf/png/jpg/webp до 5 МБ.
"""
from rest_framework import permissions, status
from rest_framework.generics import get_object_or_404
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.kassa_stage3 import _company, _transfer_payload, transfer_attachments_payload
from apps.main.media_receipts import _err, store_company_media
from apps.main.models import MainMedia, MarketStockTransfer
from apps.main.receipt_files import INVOICE_TYPES


def _get_transfer(request, pk):
    company = _company(request)
    return get_object_or_404(MarketStockTransfer.objects.prefetch_related("items", "attachments"),
                             pk=pk, company=company)


class StockTransferDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request, pk):
        return Response(_transfer_payload(_get_transfer(request, pk)))

    def patch(self, request, pk):
        t = _get_transfer(request, pk)
        fields = []
        if "document_number" in request.data:
            number = str(request.data.get("document_number") or "").strip()
            if len(number) > 64:
                return _err("invalid", "Номер документа не длиннее 64 символов.",
                            fields={"document_number": "Слишком длинный."})
            t.document_number = number
            fields.append("document_number")
        if "note" in request.data:
            t.note = str(request.data.get("note") or "")[:255]
            fields.append("note")
        if fields:
            t.save(update_fields=fields)
        return Response(_transfer_payload(t))


class StockTransferFileAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def _payload(self, request, t):
        return {"attachments": transfer_attachments_payload(t, request)}

    def get(self, request, pk):
        return Response(self._payload(request, _get_transfer(request, pk)))

    def post(self, request, pk):
        t = _get_transfer(request, pk)
        upload = request.FILES.get("file")
        if upload is None:
            return _err("file_required", "Файл не передан.", fields={"file": "Обязательное поле."})
        media, error = store_company_media(t.company, upload, MainMedia.Purpose.INVOICE, request.user,
                                           INVOICE_TYPES)
        if error is not None:
            if error.data.get("code") == "invalid_type":
                error.data["detail"] = "Документ: Excel (xlsx, xls), PDF или фото (PNG, JPG, WebP)."
            return error
        t.attachments.add(media)
        return Response(self._payload(request, t), status=status.HTTP_201_CREATED)

    def delete(self, request, pk, media_id):
        t = _get_transfer(request, pk)
        media = get_object_or_404(t.attachments.all(), pk=media_id)
        t.attachments.remove(media)
        if not media.market_stock_transfers.exists() and not media.supplier_receipts.exists():
            media.file.delete(save=False)
            media.delete()
        return Response(self._payload(request, t))
