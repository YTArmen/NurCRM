"""19.11.2026 К-4/К-5 (k458): партии товара (ведёт триггер nur_stock_batches, см. stock_batches.py)."""
from django.db import models


class StockBatch(models.Model):
    id = models.UUIDField(primary_key=True)
    company_id = models.UUIDField()
    branch_id = models.UUIDField(null=True)
    product_id = models.UUIDField()
    source_doc_type = models.CharField(max_length=32)
    source_doc_id = models.CharField(max_length=64, null=True)
    supplier_id = models.UUIDField(null=True)
    qty_in = models.DecimalField(max_digits=14, decimal_places=3)
    qty_left = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4)
    received_at = models.DateTimeField()
    expires_at = models.DateField(null=True)
    lot_no = models.CharField(max_length=64)
    status = models.CharField(max_length=10)
    created_at = models.DateTimeField()

    class Meta:
        app_label = "main"
        db_table = "main_stockbatch"
        managed = False


class StockBatchMove(models.Model):
    id = models.BigAutoField(primary_key=True)
    ledger_id = models.BigIntegerField(null=True)
    company_id = models.UUIDField()
    product_id = models.UUIDField()
    batch_id = models.UUIDField(null=True)
    qty = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4)
    source = models.CharField(max_length=64)
    doc_id = models.CharField(max_length=64, null=True)
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField()

    class Meta:
        app_label = "main"
        db_table = "main_stockbatchmove"
        managed = False


# К-8 (k458): резервы (stock_reservations.py)
class StockReservation(models.Model):
    id = models.UUIDField(primary_key=True)
    company_id = models.UUIDField(db_index=True)
    product_id = models.UUIDField()
    variant_id = models.UUIDField(null=True)
    qty = models.DecimalField(max_digits=14, decimal_places=3)
    source = models.CharField(max_length=16)
    source_id = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=10, default="active")
    expires_at = models.DateTimeField(null=True)
    comment = models.CharField(max_length=255, blank=True, default="")
    created_by_id = models.UUIDField(null=True)
    created_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True)

    class Meta:
        app_label = "main"
        db_table = "main_stockreservation"
        managed = False
