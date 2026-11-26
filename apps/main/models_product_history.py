"""21.11.2026 (hist2111): полная история товара.

Каждую вставку / изменение / удаление строки main_product (и его штрихкодов, фото) пишет триггер PostgreSQL
nur_product_history в main_producthistory — мимо него не пройдёт ни один путь (API, касса, импорт, .update(), SQL).
Кто и откуда — из того же контекста запроса, что журнал остатков (set_config('nur.ledger', ...)).
Остаток (quantity) сюда не пишется — он в main_stockledger.
Ссылок (FK) нет нарочно: история удалённого товара остаётся.
"""
from django.db import models
from django.utils import timezone


class ProductHistory(models.Model):
    id = models.BigAutoField(primary_key=True)
    company_id = models.UUIDField()
    product_id = models.UUIDField()
    product_name = models.CharField(max_length=255, blank=True, default="")
    # create / update / delete / barcode_insert / barcode_update / barcode_delete / image_insert / image_update / image_delete
    op = models.CharField(max_length=24)
    changes = models.JSONField(default=dict)
    source = models.CharField(max_length=64, default="system")
    method = models.CharField(max_length=8, null=True, blank=True)
    user_id = models.UUIDField(null=True, blank=True)
    client = models.CharField(max_length=32, null=True, blank=True)
    ip = models.CharField(max_length=64, null=True, blank=True)
    ua = models.CharField(max_length=160, null=True, blank=True)
    request_id = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "main"
        db_table = "main_producthistory"
        verbose_name = "История товара"
        verbose_name_plural = "История товаров"
        indexes = [
            models.Index(fields=["product_id", "id"], name="ix_phist_product"),
            models.Index(fields=["company_id", "created_at"], name="ix_phist_company_time"),
        ]
