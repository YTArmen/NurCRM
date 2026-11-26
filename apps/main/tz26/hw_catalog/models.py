"""ТЗ 26.10 Б-57…Б-59: справочник атрибутов, ячейки хранения по филиалу, связи товаров и категорий."""
import uuid

from django.db import models

__all__ = ["ProductAttributeKey", "ProductAttributeValue", "ProductStorageLocation", "ProductRelation", "CategoryRelation"]


class ProductAttributeKey(models.Model):
    """Б-57: ключ атрибута товара в справочнике компании (diameter_mm, color_ral…)."""

    class Type(models.TextChoices):
        NUMBER = "number", "Число"
        TEXT = "text", "Текст"
        ENUM = "enum", "Список"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="product_attribute_keys")
    key = models.CharField("Ключ", max_length=32)
    name = models.JSONField("Название (ru/ky/en/tr/uz)", default=dict, blank=True)
    type = models.CharField("Тип", max_length=8, choices=Type.choices, default=Type.TEXT)
    unit = models.CharField("Единица", max_length=16, blank=True, default="")
    options = models.JSONField("Варианты (для enum)", default=list, blank=True)
    filterable = models.BooleanField("Фильтр на витрине", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        ordering = ["key"]
        constraints = [models.UniqueConstraint(fields=["company", "key"], name="uniq_attrkey_company_key")]


class ProductAttributeValue(models.Model):
    """Б-57: индекс значений атрибутов товара (для фильтра attr.<key>=, поиска и facets).

    Источник правды — Product.attributes (JSON); индекс перестраивается при записи атрибутов.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="product_attribute_values")
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="attr_index")
    key = models.CharField(max_length=32)
    value = models.CharField(max_length=64)
    num = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

    class Meta:
        app_label = "main"
        indexes = [models.Index(fields=["company", "key", "value"], name="idx_attrval_company_key_val")]


class ProductStorageLocation(models.Model):
    """Б-58: ячейка хранения товара по филиалу (branch = NULL — главный склад компании)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="product_storage_locations")
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="storage_location_rows")
    branch = models.ForeignKey(
        "users.Branch", on_delete=models.CASCADE, null=True, blank=True, related_name="product_storage_locations"
    )
    location = models.CharField("Ячейка", max_length=32)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "main"
        indexes = [models.Index(fields=["company", "location"], name="idx_prodloc_company_loc")]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "branch"], condition=models.Q(branch__isnull=False), name="uniq_prodloc_product_branch"
            ),
            models.UniqueConstraint(
                fields=["product"], condition=models.Q(branch__isnull=True), name="uniq_prodloc_product_main"
            ),
        ]


class ProductRelation(models.Model):
    """Б-59: сопутствующий товар или аналог, заданный вручную (с нормой на единицу товара)."""

    class Kind(models.TextChoices):
        ACCESSORY = "accessory", "Сопутствующий"
        ANALOG = "analog", "Аналог"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="product_relations")
    product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="relations_out")
    related_product = models.ForeignKey("main.Product", on_delete=models.CASCADE, related_name="relations_in")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.ACCESSORY)
    ratio = models.DecimalField("Норма на единицу товара", max_digits=14, decimal_places=4, null=True, blank=True)
    ratio_unit = models.CharField(max_length=8, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        app_label = "main"
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["product", "related_product", "kind"], name="uniq_prodrel_pair_kind")
        ]


class CategoryRelation(models.Model):
    """Б-59: правило «к категории подходит категория» (необязательное)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="category_relations")
    category = models.ForeignKey("main.ProductCategory", on_delete=models.CASCADE, related_name="relations_out")
    related_category = models.ForeignKey("main.ProductCategory", on_delete=models.CASCADE, related_name="relations_in")
    kind = models.CharField(max_length=10, choices=ProductRelation.Kind.choices, default=ProductRelation.Kind.ACCESSORY)

    class Meta:
        app_label = "main"
        constraints = [
            models.UniqueConstraint(fields=["category", "related_category", "kind"], name="uniq_catrel_pair_kind")
        ]
