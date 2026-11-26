"""ТЗ Н-33: GET suppliers/receipts/{id}/pdf/ — накладная прихода от поставщика (PDF, A4)."""
import io
from decimal import Decimal, ROUND_HALF_UP

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import generics, permissions

from apps.main.views import CompanyBranchRestrictedMixin, SupplierReceiptQuerysetMixin


def receipt_number(receipt) -> str:
    """Номер прихода в накладной: «ПР-» + первые 8 символов id (в модели своего номера нет)."""
    return "ПР-" + receipt.id.hex[:8].upper()


def _m(v):
    return f"{Decimal(str(v or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def build_supplier_receipt_pdf(receipt) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas

    font, bold = "Helvetica", "Helvetica-Bold"
    names = pdfmetrics.getRegisteredFontNames()
    if "DejaVu" in names and "DejaVu-Bold" in names:
        font, bold = "DejaVu", "DejaVu-Bold"
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 50
    sup = receipt.supplier
    dt = timezone.localtime(receipt.created_at) if receipt.created_at else timezone.localtime()
    c.setFont(bold, 14)
    c.drawString(40, y, f"Приходная накладная № {receipt_number(receipt)}")
    y -= 18
    c.setFont(font, 10)
    c.drawString(40, y, f"Дата: {dt:%d.%m.%Y %H:%M}")
    y -= 14
    c.drawString(40, y, f"Поставщик: {(sup.llc or sup.full_name or '')[:70]}")
    y -= 14
    c.drawString(40, y, f"Компания: {(receipt.company.name or '')[:70]}")
    y -= 24

    def head(y):
        c.setFont(bold, 9)
        c.drawString(40, y, "№")
        c.drawString(60, y, "Товар")
        c.drawRightString(390, y, "Кол-во")
        c.drawRightString(470, y, "Цена")
        c.drawRightString(w - 40, y, "Сумма")
        c.line(40, y - 3, w - 40, y - 3)
        return y - 16

    y = head(y)
    c.setFont(font, 9)
    subtotal = Decimal("0")
    for i, it in enumerate(receipt.items.all(), 1):
        if y < 80:
            c.showPage()
            y = head(h - 50)
            c.setFont(font, 9)
        price = Decimal(str(it.purchase_price or 0))
        line = price * Decimal(str(it.qty))
        subtotal += line
        c.drawString(40, y, str(i))
        c.drawString(60, y, (it.product.name or "")[:60])
        c.drawRightString(390, y, str(it.qty))
        c.drawRightString(470, y, _m(price))
        c.drawRightString(w - 40, y, _m(line))
        y -= 14
    y -= 6
    c.line(40, y + 8, w - 40, y + 8)
    c.setFont(font, 9)
    if receipt.discount and receipt.discount > 0:
        c.drawRightString(470, y, "Скидка:")
        c.drawRightString(w - 40, y, _m(receipt.discount))
        y -= 13
    for ex in receipt.extra_costs or []:
        c.drawRightString(470, y, f"{ex.get('name') or 'Доп. расход'}:"[:40])
        c.drawRightString(w - 40, y, _m(ex.get("amount")))
        y -= 13
    c.setFont(bold, 11)
    c.drawRightString(470, y - 4, "Итого:")
    c.drawRightString(w - 40, y - 4, _m(subtotal))
    c.save()
    return buf.getvalue()


class SupplierReceiptPdfAPIView(SupplierReceiptQuerysetMixin, CompanyBranchRestrictedMixin, generics.GenericAPIView):
    """GET /api/main/suppliers/receipts/<uuid:pk>/pdf/ → application/pdf."""

    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None
    filter_backends = []

    def get_queryset(self):
        return self._supplier_receipts_base_queryset()

    def get(self, request, pk):
        receipt = self.get_queryset().filter(pk=pk).first()
        if receipt is None:
            from rest_framework.exceptions import NotFound

            raise NotFound("Приход не найден.")
        resp = HttpResponse(build_supplier_receipt_pdf(receipt), content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="receipt-{receipt_number(receipt)}.pdf"'
        return resp
