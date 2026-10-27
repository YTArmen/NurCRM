"""ТЗ 26.10 Б-61: партии с оттенком/калибром, отрезной товар и обрезки.

Все сущности — отдельные таблицы, существующие модели не меняются.
"""
import uuid

from django.db import models

__all__ = [
    "ProductBatch", "ProductBatchReceiptLine", "CartItemBatch", "SaleItemBatch",
    "ProductCutSettings", "ProductRoll", "ProductRemnant",
]


class ProductBatch(models.Model):
    """Партия товара (оттенок/калибр): остаток по метке, пришедшей строкой прихода."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="tz26_product_batches")
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="tz26_batches")
    label = models.CharField("Партия (оттенок/калибр)", max_length=32)
    received_total = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    remaining = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "main"
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["product", "label"], name="uniq_tz26_batch_product_label")]


class ProductBatchReceiptLine(models.Model):
    """Какая строка прихода поставщика сколько привезла в партию."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(ProductBatch, on_delete=models.CASCADE, related_name="receipt_lines")
    receipt_item = models.OneToOneField(
        "main.SupplierReceiptItem", on_delete=models.CASCADE, related_name="tz26_batch_line")
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"


class CartItemBatch(models.Model):
    """Выбранная партия строки корзины (строка корзины удаляется при оформлении — связь тоже)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cart_item = models.OneToOneField("main.CartItem", on_delete=models.CASCADE, related_name="tz26_batch_link")
    batch = models.ForeignKey(ProductBatch, on_delete=models.CASCADE, related_name="cart_links")

    class Meta:
        app_label = "main"


class SaleItemBatch(models.Model):
    """Из какой партии списана строка продажи."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sale_item = models.OneToOneField("main.SaleItem", on_delete=models.CASCADE, related_name="tz26_batch_link")
    batch = models.ForeignKey(ProductBatch, on_delete=models.PROTECT, related_name="sale_links")
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"


class ProductCutSettings(models.Model):
    """Настройка отрезного товара (кабель, труба): длина бухты, минимальный отрез, порог обрезка."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="tz26_cut_settings")
    product = models.OneToOneField("main.Product", on_delete=models.CASCADE, related_name="tz26_cut")
    roll_length = models.DecimalField(max_digits=12, decimal_places=3)
    min_cut = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    remnant_threshold = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    remnant_discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "main"


class ProductRoll(models.Model):
    """Бухта отрезного товара. Создаётся лениво при первой продаже, сверяясь с остатком товара."""

    class Status(models.TextChoices):
        ACTIVE = "active", "В работе"
        CONSUMED = "consumed", "Выбрана"
        REMNANT = "remnant", "Переведена в обрезок"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="tz26_rolls")
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="tz26_rolls")
    length = models.DecimalField(max_digits=12, decimal_places=3)
    remaining = models.DecimalField(max_digits=12, decimal_places=3)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "main"
        ordering = ["remaining", "opened_at"]


class ProductRemnant(models.Model):
    """Товар-обрезок (remnant_of — исходный товар) со скидкой."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="tz26_remnants")
    product = models.OneToOneField("main.Product", on_delete=models.CASCADE, related_name="tz26_remnant_info")
    remnant_of = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="tz26_remnants")
    roll = models.ForeignKey(ProductRoll, null=True, blank=True, on_delete=models.SET_NULL, related_name="remnants")
    length = models.DecimalField(max_digits=12, decimal_places=3)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        ordering = ["-created_at"]
