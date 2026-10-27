from django.db import models

__all__ = ["PriceLevel", "ClientObject"]


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
