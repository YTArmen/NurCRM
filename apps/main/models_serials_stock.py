"""П1-6 К-6 (15.11.2026, k6_1511): серийные номера, пришедшие приходом. Импортируется в конце models.py.
Статус считается на лету: «на складе», пока нет непогашенной продажи (SoldSerial без returned_at) с тем же номером."""
import uuid

from django.db import models


class StockSerial(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="stock_serials")
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="stock_serials")
    serial = models.CharField("Серийный номер", max_length=64)
    serial_norm = models.CharField(max_length=64)
    receipt = models.ForeignKey("main.SupplierReceipt", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    receipt_item = models.ForeignKey("main.SupplierReceiptItem", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    received_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        verbose_name = "Серийный номер на складе"
        verbose_name_plural = "Серийные номера на складе"
        indexes = [models.Index(fields=["company", "serial_norm"], name="idx_stockserial_company_serial"),
                   models.Index(fields=["product", "received_at"], name="idx_stockserial_product")]
