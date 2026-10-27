from django.db import models

__all__ = ["ProductSaleUnit"]


class ProductSaleUnit(models.Model):
    """Б-53: единица продажи/закупки товара (коробка 200 шт, мешок 50 кг)."""
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="sale_units")
    name = models.CharField(max_length=64)
    unit_code = models.CharField(max_length=8, blank=True, default="")
    factor = models.DecimalField(max_digits=14, decimal_places=4)
    barcode = models.CharField(max_length=64, blank=True, default="", db_index=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    wholesale_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    for_sale = models.BooleanField(default=True)
    for_receipt = models.BooleanField(default=True)
    is_default_sale = models.BooleanField(default=False)
    is_default_receipt = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        app_label = "main"
        ordering = ["sort_order", "id"]
