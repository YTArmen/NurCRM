"""ТЗ 26.10: кусок hw_prices (Б-55 уровни цен, Б-56 клиент-прораб и объекты)."""
from django.db import models

__all__ = ["PriceLevel", "ClientObject", "ProductLevelPrice", "ProductVolumePrice", "DealObject"]


class PriceLevel(models.Model):
    """Б-55: уровень цен компании (retail, wholesale — системные; свои — до 5)."""
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="price_levels")
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=64)
    sort_order = models.PositiveIntegerField(default=0)
    is_system = models.BooleanField(default=False)

    class Meta:
        app_label = "main"
        ordering = ["sort_order", "id"]
        unique_together = [("company", "code")]


class ProductLevelPrice(models.Model):
    """Б-55: цена товара по своему (не системному) уровню. retail/wholesale лежат в Product.price / wholesale_price."""
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="level_prices")
    level = models.ForeignKey("main.PriceLevel", on_delete=models.CASCADE, related_name="product_prices")
    price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        app_label = "main"
        ordering = ["level_id", "id"]
        unique_together = [("product", "level")]


class ProductVolumePrice(models.Model):
    """Б-55: цена «от N штук». level пусто — для всех клиентов; min_qty — в базовых единицах товара."""
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="volume_prices")
    level = models.ForeignKey("main.PriceLevel", on_delete=models.CASCADE, null=True, blank=True, related_name="volume_prices")
    min_qty = models.DecimalField(max_digits=14, decimal_places=3)
    price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        app_label = "main"
        ordering = ["min_qty", "id"]


class ClientObject(models.Model):
    """Б-56: объект клиента-прораба."""
    client = models.ForeignKey("main.Client", on_delete=models.CASCADE, related_name="objects_list")
    name = models.CharField(max_length=128)
    address = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        ordering = ["name", "id"]


class DealObject(models.Model):
    """Б-56: объект, к которому отнесён долг без продажи (ClientDeal). Поля в ClientDeal не добавляем."""
    deal = models.OneToOneField("main.ClientDeal", on_delete=models.CASCADE, related_name="hw_object_link")
    client_object = models.ForeignKey("main.ClientObject", on_delete=models.SET_NULL, null=True, blank=True, related_name="deal_links")

    class Meta:
        app_label = "main"
