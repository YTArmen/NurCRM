import uuid
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

from django.db import models, transaction, connection
from django.conf import settings
from django.db.models import Q, Max, IntegerField, Sum
from django.db.models.functions import Cast
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.utils import timezone

from PIL import Image


# -----------------------
# Base / helpers
# -----------------------


class BaseModelId(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class BaseModelDate(models.Model):
    created_date = models.DateTimeField(auto_now_add=True, verbose_name="Дата открытия")
    updated_date = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        abstract = True


class BaseModelCompanyBranch(models.Model):
    company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        verbose_name='Компания'
    )

    branch = models.ForeignKey(
        "users.Branch",
        on_delete=models.CASCADE,
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    class Meta:
        abstract = True


def product_image_upload_to(instance, filename: str) -> str:
    return f"products/{instance.product_id}/{uuid.uuid4().hex}.webp"


# -----------------------
# Warehouse model
# -----------------------


class Warehouse(BaseModelId, BaseModelDate, BaseModelCompanyBranch):
    name = models.CharField(max_length=128, verbose_name="Название", null=True, blank=True)
    location = models.TextField(verbose_name="Локация", blank=False)

    class Status(models.TextChoices):
        active = "active", "Активен"
        inactive = "inactive", "Неактивен"

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.inactive,
        verbose_name="Статус"
    )

    class Meta:
        verbose_name = "Склад"
        verbose_name_plural = "Склады"


# -----------------------
# Brand / Category
# -----------------------


from mptt.models import TreeForeignKey


class WarehouseProductBrand(BaseModelId, BaseModelCompanyBranch):
    name = models.CharField(max_length=128, verbose_name="Название")

    parent = TreeForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='children', verbose_name='Родительский бренд')

    class MPTTMeta:
        order_insertion_by = ['name']

    class Meta:
        verbose_name = 'Бренд'
        verbose_name_plural = 'Бренды'
        constraints = [
            models.UniqueConstraint(
                fields=('branch', 'name'),
                name='uq_warehouse_brand_name_per_branch',
                condition=models.Q(branch__isnull=False),
            ),
            models.UniqueConstraint(
                fields=('company', 'name'),
                name='uq_warehouse_brand_name_global_per_company',
                condition=models.Q(branch__isnull=True),
            ),
        ]
        indexes = [
            models.Index(fields=['company', 'name']),
            models.Index(fields=['company', 'branch', 'name']),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        if self.parent_id:
            if self.parent.company_id != self.company_id:
                raise ValidationError({'parent': 'Родительский бренд другой компании.'})
            if (self.parent.branch_id or None) != (self.branch_id or None):
                raise ValidationError({'parent': 'Родительский бренд другого филиала.'})


class WarehouseProductCategory(BaseModelId, BaseModelCompanyBranch):
    name = models.CharField(max_length=128, verbose_name="Название")

    parent = TreeForeignKey(
        'self', on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='children', verbose_name='Родительская категория')

    class MPTTMeta:
        order_insertion_by = ['name']

    class Meta:
        verbose_name = 'Категория товара'
        verbose_name_plural = 'Категории товаров'
        constraints = [
            models.UniqueConstraint(
                fields=('branch', 'name'),
                name='uq_warehouse_category_name_per_branch',
                condition=models.Q(branch__isnull=False),
            ),
            models.UniqueConstraint(
                fields=('company', 'name'),
                name='uq_warehouse_category_name_global_per_company',
                condition=models.Q(branch__isnull=True),
            ),
        ]
        indexes = [
            models.Index(fields=['company', 'name']),
            models.Index(fields=['company', 'branch', 'name']),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        if self.parent_id:
            if self.parent.company_id != self.company_id:
                raise ValidationError({'parent': 'Родительская категория другой компании.'})
            if (self.parent.branch_id or None) != (self.branch_id or None):
                raise ValidationError({'parent': 'Родительская категория другого филиала.'})


# -----------------------
# Warehouse product group (grouping inside warehouse, like 1C)
# -----------------------


class WarehouseProductGroup(BaseModelId, BaseModelCompanyBranch):
    """
    Группа товаров внутри склада (иерархия как в 1С).
    Одна группа может содержать подгруппы и/или товары.
    """
    warehouse = models.ForeignKey(
        "warehouse.Warehouse",
        on_delete=models.CASCADE,
        related_name="product_groups",
        verbose_name="Склад",
    )
    name = models.CharField(max_length=128, verbose_name="Название")

    parent = TreeForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        verbose_name="Родительская группа",
    )

    class MPTTMeta:
        order_insertion_by = ["name"]

    class Meta:
        verbose_name = "Группа товаров (склад)"
        verbose_name_plural = "Группы товаров (склад)"
        constraints = [
            models.UniqueConstraint(
                fields=("warehouse", "name"),
                condition=Q(parent__isnull=True),
                name="uq_wh_product_group_root_name_per_warehouse",
            ),
            models.UniqueConstraint(
                fields=("parent", "name"),
                condition=Q(parent__isnull=False),
                name="uq_wh_product_group_child_name_per_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["warehouse"]),
            models.Index(fields=["company", "warehouse"]),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.warehouse_id and self.company_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "Склад принадлежит другой компании."})
        if self.branch_id and self.warehouse_id and self.warehouse.branch_id not in (None, self.branch_id):
            raise ValidationError({"warehouse": "Склад другого филиала."})
        if self.parent_id:
            if self.parent.warehouse_id != self.warehouse_id:
                raise ValidationError({"parent": "Родительская группа должна принадлежать тому же складу."})
            if self.parent.company_id != self.company_id:
                raise ValidationError({"parent": "Родительская группа другой компании."})
            if (self.parent.branch_id or None) != (self.branch_id or None):
                raise ValidationError({"parent": "Родительская группа другого филиала."})

    def save(self, *args, **kwargs):
        if self.warehouse_id:
            if not self.company_id:
                self.company_id = self.warehouse.company_id
            if self.branch_id is None:
                self.branch_id = self.warehouse.branch_id
        super().save(*args, **kwargs)


# -----------------------
# Product and related
# -----------------------


QTY3 = Decimal("0.001")
MONEY = Decimal("0.001")
PCT3 = Decimal("0.001")


def q_money(x: Decimal) -> Decimal:
    return (x or Decimal("0")).quantize(MONEY, rounding=ROUND_HALF_UP)


def q_qty(x: Decimal) -> Decimal:
    return (x or Decimal("0")).quantize(QTY3, rounding=ROUND_HALF_UP)


def q_pct(x: Decimal) -> Decimal:
    return (x or Decimal("0")).quantize(PCT3, rounding=ROUND_HALF_UP)


class WarehouseProduct(BaseModelId, BaseModelDate, BaseModelCompanyBranch):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидание"
        ACCEPTED = "accepted", "Принят"
        REJECTED = "rejected", "Отказ"

    class Kind(models.TextChoices):
        PRODUCT = "product", "Товар"
        SERVICE = "service", "Услуга"
        BUNDLE = "bundle", "Комплект"

    brand = models.ForeignKey(
        "warehouse.WarehouseProductBrand",
        on_delete=models.SET_NULL,
        verbose_name="Бренд",
        null=True,
        blank=True,
    )

    category = models.ForeignKey(
        "warehouse.WarehouseProductCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Категория",
    )

    product_group = models.ForeignKey(
        "warehouse.WarehouseProductGroup",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
        verbose_name="Группа (склад)",
        help_text="Группировка товаров внутри склада (как в 1С).",
    )

    warehouse = models.ForeignKey(
        "warehouse.Warehouse",
        on_delete=models.CASCADE,
        verbose_name="Склад",
        related_name="products",
    )

    supplier = models.ForeignKey(
        "warehouse.Counterparty",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplied_products",
        limit_choices_to=Q(type__in=["SUPPLIER", "BOTH"]),
        verbose_name="Поставщик",
    )

    article = models.CharField("Артикул", max_length=64, blank=True)
    name = models.CharField("Название", max_length=255)
    description = models.TextField("Описание", blank=True, null=True)

    barcode = models.CharField("Штрихкод", max_length=64, null=True, blank=True)

    code = models.CharField(
        "Код товара",
        max_length=32,
        blank=True,
        null=True,
        db_index=True,
        help_text="Автогенерация, если не указан. Новые коды — сквозные по компании (QA B31).",
    )

    # QA B01: общий ключ всех копий одного товара в компании (по складам). Перемещение
    # зачисляет товар на склад-получатель только в карточку с тем же ключом
    # (или с тем же штрихкодом), а не по коду/названию.
    catalog_key = models.UUIDField(
        "Ключ товара в компании",
        null=True,
        blank=True,
        db_index=True,
        help_text="Одинаковый у всех копий одного товара на разных складах компании.",
    )

    unit = models.CharField(
        "Единица измерения",
        max_length=32,
        default="шт.",
        help_text="Вводится вручную: шт., кг, м, упак., л и т.д.",
    )

    is_weight = models.BooleanField(
        "Весовой товар",
        default=False,
        help_text="Если товар продаётся по весу (обычно кг).",
    )
    is_adult = models.BooleanField(
        "Товар 18+",
        default=False,
        help_text="Флаг 18+ для товаров (алкоголь, табак и т.д.).",
    )


    quantity = models.DecimalField(
        "Количество",
        max_digits=12,
        decimal_places=3,
        default=Decimal("0.000"),
    )

    minimum_quantity = models.DecimalField(
        "Минимальный остаток",
        max_digits=12,
        decimal_places=3,
        default=Decimal("0.000"),
        null=True,
        blank=True,
        help_text="Порог для алерта «мало на складе».",
    )

    purchase_price = models.DecimalField(
        "Цена закупки",
        max_digits=11,
        decimal_places=3,
        default=Decimal("0.00"),
    )

    markup_percent = models.DecimalField(
        "Наценка, %",
        max_digits=12,
        decimal_places=3,
        default=Decimal("0.00"),
    )

    price = models.DecimalField(
        "Цена продажи",
        max_digits=11,
        decimal_places=3,
        default=Decimal("0.00"),
        help_text="Считается автоматически из закупки и наценки (если наценка > 0).",
    )

    wholesale_price = models.DecimalField(
        "Цена оптовой продажи",
        max_digits=11,
        decimal_places=3,
        default=Decimal("0.00"),
        help_text="Оптовая цена продажи за учётную единицу. Заполняется вручную (из наценки не считается).",
    )

    discount_percent = models.DecimalField(
        "Скидка, %",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    plu = models.PositiveIntegerField(
        "ПЛУ",
        blank=True,
        null=True,
        help_text="Номер ПЛУ для весов (можно не заполнять). Уникален в рамках склада.",
    )

    country = models.CharField("Страна происхождения", max_length=64, blank=True)

    status = models.CharField(
        "Статус",
        max_length=16,
        choices=Status.choices,
        db_index=True,
        blank=True,
        null=True,
    )

    stock = models.BooleanField("Акционный товар", default=False)

    expiration_date = models.DateField("Срок годности", null=True, blank=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Товар"
        verbose_name_plural = "Товары"
        indexes = [
            models.Index(fields=["company", "branch", "status"]),
            models.Index(fields=["company", "warehouse", "status"]),
            models.Index(fields=["company", "warehouse", "plu"]),
            models.Index(fields=["company", "warehouse", "barcode"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("company", "warehouse", "barcode"),
                condition=Q(barcode__isnull=False) & ~Q(barcode=""),
                name="uq_wh_company_warehouse_barcode_not_empty",
            ),
            models.UniqueConstraint(
                fields=("company", "warehouse", "code"),
                condition=Q(code__isnull=False) & ~Q(code=""),
                name="uq_wh_company_warehouse_code_not_empty",
            ),
            models.UniqueConstraint(
                fields=("company", "warehouse", "plu"),
                condition=Q(plu__isnull=False),
                name="uq_wh_company_warehouse_plu_not_null",
            ),
        ]

    def _pg_lock_company(self):
        if not self.company_id:
            return
        # Use PostgreSQL advisory locks when available. Skip for SQLite/other backends.
        try:
            vendor = connection.vendor
        except Exception:
            vendor = None

        if vendor != "postgresql":
            return

        key = int(str(self.company_id).replace("-", "")[:16], 16) & 0x7FFFFFFFFFFFFFFF
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s::bigint);", [key])

    def _auto_generate_plu(self):
        if not self.is_weight:
            return
        if self.plu is not None or not self.company_id or not self.warehouse_id:
            return

        max_plu = (
            WarehouseProduct.objects.filter(
                company_id=self.company_id,
                warehouse_id=self.warehouse_id,
                plu__isnull=False,
            )
            .aggregate(m=Max("plu"))
            .get("m")
            or 0
        )
        self.plu = max_plu + 1

    def _auto_generate_code(self):
        if self.code or not self.company_id or not self.warehouse_id:
            return

        # QA B31: нумерация сквозная по компании, а не «0001 в каждом складе» —
        # иначе коды разных товаров разных складов совпадают.
        qs = (
            WarehouseProduct.objects.filter(company_id=self.company_id)
            .exclude(code__isnull=True)
            .exclude(code__exact="")
            .filter(code__regex=r"^\d+$")
            .annotate(code_int=Cast("code", IntegerField()))
        )
        last_num = qs.aggregate(max_num=Max("code_int"))["max_num"] or 0
        self.code = f"{last_num + 1:04d}"

    def _recalc_price(
        self,
        *,
        old_price: Decimal | None = None,
        old_purchase_price: Decimal | None = None,
        old_markup_percent: Decimal | None = None,
    ):
        """
        Правило пересчёта:
        - если меняется price или purchase_price -> price главный, markup_percent пересчитываем из purchase_price→price
        - если меняется только markup_percent -> пересчитываем price из purchase_price + markup_percent
        - если ничего не менялось -> только нормализуем price
        """
        price = Decimal(self.price or 0)
        purchase = Decimal(self.purchase_price or 0)
        markup = Decimal(self.markup_percent or 0)

        if old_price is None and old_purchase_price is None and old_markup_percent is None:
            # create(): при создании считаем price главным, если задана закупка (иначе просто нормализуем)
            self.price = q_money(price)
            if purchase != 0:
                self.markup_percent = q_pct(((price / purchase) - Decimal("1")) * Decimal("100"))
            else:
                self.markup_percent = q_pct(markup)
            return

        old_price = Decimal(old_price or 0)
        old_purchase = Decimal(old_purchase_price or 0)
        old_markup = Decimal(old_markup_percent or 0)

        price_changed = price != old_price
        purchase_changed = purchase != old_purchase
        markup_changed = markup != old_markup

        if price_changed or purchase_changed:
            self.price = q_money(price)
            if purchase != 0:
                self.markup_percent = q_pct(((price / purchase) - Decimal("1")) * Decimal("100"))
            else:
                self.markup_percent = q_pct(Decimal("0"))
            return

        if markup_changed:
            if markup == Decimal("0"):
                self.price = q_money(price)
                return
            result = purchase * (Decimal("1") + markup / Decimal("100"))
            self.price = q_money(result)
            return

        self.price = q_money(price)

    def clean(self):
        super().clean()

        if self.branch_id and self.company_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})

        for rel, name in [(self.brand, "brand"), (self.category, "category")]:
            if rel and getattr(rel, "company_id", None) and rel.company_id != self.company_id:
                raise ValidationError({name: "Объект принадлежит другой компании."})
            if self.branch_id and rel and getattr(rel, "branch_id", None) not in (None, self.branch_id):
                raise ValidationError({name: "Объект другого филиала."})

        if self.product_group_id and self.warehouse_id:
            if self.product_group.warehouse_id != self.warehouse_id:
                raise ValidationError({"product_group": "Группа должна принадлежать тому же складу, что и товар."})

        if self.discount_percent is not None:
            dp = Decimal(self.discount_percent)
            if not (Decimal("0") <= dp <= Decimal("100")):
                raise ValidationError({"discount_percent": "Скидка должна быть от 0 до 100%."})

        if self.quantity is not None and Decimal(self.quantity) < 0:
            raise ValidationError({"quantity": "Количество не может быть отрицательным."})

    def save(self, *args, **kwargs):
        # Инвалидация кэша при изменении barcode или plu
        old_barcode = None
        old_plu = None
        old_price = None
        old_purchase_price = None
        old_markup_percent = None
        if self.pk:
            try:
                old_instance = WarehouseProduct.objects.get(pk=self.pk)
                old_barcode = old_instance.barcode
                old_plu = old_instance.plu
                old_price = old_instance.price
                old_purchase_price = old_instance.purchase_price
                old_markup_percent = old_instance.markup_percent
            except WarehouseProduct.DoesNotExist:
                pass

        if self.pk:
            self._recalc_price(
                old_price=old_price,
                old_purchase_price=old_purchase_price,
                old_markup_percent=old_markup_percent,
            )
        else:
            self._recalc_price()
        self.quantity = q_qty(Decimal(self.quantity or 0))

        with transaction.atomic():
            self._pg_lock_company()
            self._auto_generate_code()
            self._auto_generate_plu()
            result = super().save(*args, **kwargs)
            
            # Инвалидация кэша после сохранения
            from django.core.cache import cache
            if old_barcode and old_barcode != self.barcode:
                cache_key = f"warehouse_product_barcode:{self.company_id}:{old_barcode}"
                cache.delete(cache_key)
            if self.barcode:
                cache_key = f"warehouse_product_barcode:{self.company_id}:{self.barcode}"
                cache.delete(cache_key)
            if old_plu and old_plu != self.plu:
                cache_key = f"warehouse_product_plu:{self.company_id}:{self.warehouse_id}:{old_plu}"
                cache.delete(cache_key)
            if self.plu:
                cache_key = f"warehouse_product_plu:{self.company_id}:{self.warehouse_id}:{self.plu}"
                cache.delete(cache_key)
            
            return result


class WarehouseProductAlternateBarcode(BaseModelId):
    """
    Дополнительные штрихкоды товара на складе (тот же учётный товар, несколько кодов для сканера).
    Основной штрихкод хранится в WarehouseProduct.barcode.
    """

    product = models.ForeignKey(
        "warehouse.WarehouseProduct",
        on_delete=models.CASCADE,
        related_name="alternate_barcodes",
        verbose_name="Товар",
    )
    barcode = models.CharField("Штрихкод", max_length=64)
    name = models.CharField("Название / Описание", max_length=255, blank=True, default="")
    quantity = models.PositiveIntegerField("Количество в упаковке", null=True, blank=True)

    class Meta:
        verbose_name = "Дополнительный штрихкод товара"
        verbose_name_plural = "Дополнительные штрихкоды товаров"
        constraints = [
            models.UniqueConstraint(
                fields=("product", "barcode"),
                name="uq_wh_alt_barcode_per_product_line",
            ),
        ]
        indexes = [
            models.Index(fields=["barcode"]),
            models.Index(fields=["product", "barcode"]),
        ]

    def __str__(self):
        return f"{self.barcode} → {self.product_id}"

    def clean(self):
        b = (self.barcode or "").strip()
        if not b:
            raise ValidationError({"barcode": "Штрихкод не может быть пустым."})
        self.barcode = b
        if self.product_id:
            main = (self.product.barcode or "").strip()
            if main and b == main:
                raise ValidationError({"barcode": "Дублирует основной штрихкод товара — укажите его только в поле barcode."})

    def save(self, *args, **kwargs):
        from django.core.cache import cache

        self.barcode = (self.barcode or "").strip()
        old_barcode = None
        if self.pk:
            try:
                old = type(self).objects.get(pk=self.pk)
                old_barcode = (old.barcode or "").strip() or None
            except type(self).DoesNotExist:
                pass
        super().save(*args, **kwargs)
        cid = getattr(self.product, "company_id", None)
        if cid:
            if old_barcode and old_barcode != self.barcode:
                cache.delete(f"warehouse_product_barcode:{cid}:{old_barcode}")
            if self.barcode:
                cache.delete(f"warehouse_product_barcode:{cid}:{self.barcode}")

    def delete(self, *args, **kwargs):
        from django.core.cache import cache

        cid = getattr(self.product, "company_id", None)
        b = (self.barcode or "").strip()
        super().delete(*args, **kwargs)
        if cid and b:
            cache.delete(f"warehouse_product_barcode:{cid}:{b}")


class WarehouseProductCharasteristics(BaseModelId, BaseModelCompanyBranch, BaseModelDate):
    product = models.OneToOneField(
        "warehouse.WarehouseProduct",
        on_delete=models.CASCADE,
        related_name="characteristics",
        verbose_name="Товар",
    )

    height_cm = models.DecimalField("Высота, см", max_digits=8, decimal_places=2, null=True, blank=True)
    width_cm = models.DecimalField("Ширина, см", max_digits=8, decimal_places=2, null=True, blank=True)
    depth_cm = models.DecimalField("Глубина, см", max_digits=8, decimal_places=2, null=True, blank=True)
    factual_weight_kg = models.DecimalField("Фактический вес, кг", max_digits=8, decimal_places=3, null=True, blank=True)
    description = models.TextField("Описание", blank=True)

    class Meta:
        verbose_name = "Характеристики товара"
        verbose_name_plural = "Характеристики товара"

    def __str__(self):
        return f"Характеристики: {self.product}"

    def clean(self):
        if self.product_id:
            if self.company_id and self.product.company_id != self.company_id:
                raise ValidationError({"company": "Компания должна совпадать с компанией товара."})
            if self.branch_id is not None and self.product.branch_id != self.branch_id:
                raise ValidationError({"branch": "Филиал должен совпадать с филиалом товара (оба None или одинаковые)."})

    def save(self, *args, **kwargs):
        if self.product_id:
            if not self.company_id:
                self.company_id = self.product.company_id
            if self.branch_id is None:
                self.branch_id = self.product.branch_id
        super().save(*args, **kwargs)


class WarehouseProductImage(BaseModelId, BaseModelCompanyBranch):
    product = models.ForeignKey(
        "WarehouseProduct",
        on_delete=models.CASCADE,
        related_name="images", verbose_name="Товар"
    )

    image = models.ImageField(upload_to=product_image_upload_to, null=True, blank=True, verbose_name="Изображение (WebP)")
    alt = models.CharField(max_length=255, blank=True, verbose_name="Alt-текст")
    is_primary = models.BooleanField(default=False, verbose_name="Основное изображение")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")

    class Meta:
        verbose_name = "Фото товара"
        verbose_name_plural = "Фото товара"
        constraints = [
            models.UniqueConstraint(
                fields=("product",),
                condition=models.Q(is_primary=True),
                name="uq_warehouse_primary_product_image",
            )
        ]
        indexes = [
            models.Index(fields=["company"]),
            models.Index(fields=["company", "branch"]),
            models.Index(fields=["product", "is_primary"]),
        ]

    def __str__(self):
        return f"{self.product.name} — image {self.pk}"

    def clean(self):
        if self.product_id:
            if self.company_id and self.product.company_id != self.company_id:
                raise ValidationError({"company": "Компания изображения должна совпадать с компанией товара."})
            if self.branch_id is not None and self.product.branch_id not in (None, self.branch_id):
                raise ValidationError({"branch": "Филиал изображения должен совпадать с филиалом товара (или быть глобальным вместе с ним)."})

    def save(self, *args, **kwargs):
        if self.product_id:
            if not self.company_id:
                self.company_id = self.product.company_id
            if self.branch_id is None:
                self.branch_id = self.product.branch_id

        if self.image and hasattr(self.image, "file"):
            try:
                self.image = self._convert_to_webp(self.image)
            except Exception as e:
                raise ValidationError({"image": f"Не удалось конвертировать в WebP: {e}"})

        super().save(*args, **kwargs)

        if self.is_primary:
            (type(self).objects
                .filter(product=self.product, is_primary=True)
                .exclude(pk=self.pk)
                .update(is_primary=False))

    def delete(self, *args, **kwargs):
        storage = self.image.storage if self.image else None
        name = self.image.name if self.image else None
        super().delete(*args, **kwargs)
        if storage and name and storage.exists(name):
            storage.delete(name)

    @staticmethod
    def _convert_to_webp(field_file) -> ContentFile:
        field_file.seek(0)
        im = Image.open(field_file)

        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")

        buf = BytesIO()
        im.save(buf, format="WEBP", quality=80, method=6)
        buf.seek(0)

        content = ContentFile(buf.read())
        content.name = f"{uuid.uuid4().hex}.webp"
        return content


class WarehouseProductPackage(BaseModelId, BaseModelCompanyBranch):
    product = models.ForeignKey(
        "warehouse.WarehouseProduct",
        on_delete=models.CASCADE,
        related_name="packages",
        verbose_name="Товар",
    )

    name = models.CharField("Упаковка", max_length=64, help_text="Например: коробка, пачка, блок, рулон")

    quantity_in_package = models.DecimalField(
        "Количество в упаковке", max_digits=10, decimal_places=3, help_text="Сколько базовых единиц в одной упаковке",
    )

    unit = models.CharField("Ед. изм.", max_length=32, blank=True, help_text="Если пусто — берём единицу товара")

    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Упаковка товара"
        verbose_name_plural = "Упаковки товара"

    def __str__(self):
        return f"{self.name}: {self.quantity_in_package} {self.unit or self.product.unit}"

    def clean(self):
        if self.quantity_in_package is not None and self.quantity_in_package <= 0:
            raise ValidationError({"quantity_in_package": "Количество в упаковке должно быть больше 0."})

        if self.product_id:
            if self.company_id and self.product.company_id != self.company_id:
                raise ValidationError({"company": "Компания должна совпадать с компанией товара."})
            if self.branch_id is not None and self.product.branch_id != self.branch_id:
                raise ValidationError({"branch": "Филиал должен совпадать с филиалом товара (оба None или одинаковые)."})

    def save(self, *args, **kwargs):
        if self.product_id:
            if not self.company_id:
                self.company_id = self.product.company_id
            if self.branch_id is None:
                self.branch_id = self.product.branch_id
            if not self.unit:
                self.unit = self.product.unit

        super().save(*args, **kwargs)


# -----------------------
# Documents / stock models
# -----------------------


class StockBalance(models.Model):
    warehouse = models.ForeignKey("warehouse.Warehouse", on_delete=models.CASCADE, related_name="balances", verbose_name="Склад")
    product = models.ForeignKey("warehouse.WarehouseProduct", on_delete=models.CASCADE, related_name="balances", verbose_name="Товар")
    qty = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Количество")

    class Meta:
        verbose_name = "Остаток на складе"
        verbose_name_plural = "Остатки на складах"
        unique_together = ("warehouse", "product")
        indexes = [models.Index(fields=["warehouse", "product"]) ]

    def __str__(self):
        return f"Balance {self.warehouse} / {self.product} = {self.qty}"


class AgentStockBalance(BaseModelId, BaseModelCompanyBranch):
    """
    Остатки товаров на руках у агента.
    """
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="warehouse_agent_balances")
    warehouse = models.ForeignKey("warehouse.Warehouse", on_delete=models.CASCADE, related_name="agent_balances")
    product = models.ForeignKey("warehouse.WarehouseProduct", on_delete=models.CASCADE, related_name="agent_balances")
    qty = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Количество")

    class Meta:
        verbose_name = "Остаток у агента"
        verbose_name_plural = "Остатки у агентов"
        unique_together = ("agent", "warehouse", "product")
        indexes = [
            models.Index(fields=["company", "agent", "warehouse"]),
            models.Index(fields=["company", "agent", "product"]),
        ]

    def __str__(self):
        return f"AgentBalance {self.agent_id} / {self.product_id} = {self.qty}"

    def clean(self):
        if self.warehouse_id and self.company_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "Склад принадлежит другой компании."})
        if self.product_id and self.company_id and self.product.company_id != self.company_id:
            raise ValidationError({"product": "Товар принадлежит другой компании."})
        if self.branch_id and self.warehouse_id and self.warehouse.branch_id not in (None, self.branch_id):
            raise ValidationError({"warehouse": "Склад другого филиала."})
        if self.branch_id and self.product_id and self.product.branch_id not in (None, self.branch_id):
            raise ValidationError({"product": "Товар другого филиала."})


class AgentStockMove(models.Model):
    """Движение товара у агента. Каждое движение — приход или расход."""

    class MoveKind(models.TextChoices):
        RECEIPT = "RECEIPT", "Приход"
        EXPENSE = "EXPENSE", "Расход"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey("warehouse.Document", on_delete=models.CASCADE, related_name="agent_moves", verbose_name="Документ")
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="warehouse_agent_moves")
    warehouse = models.ForeignKey("warehouse.Warehouse", on_delete=models.CASCADE, verbose_name="Склад")
    product = models.ForeignKey("warehouse.WarehouseProduct", on_delete=models.CASCADE, verbose_name="Товар")
    qty_delta = models.DecimalField(max_digits=18, decimal_places=3, verbose_name="Изменение количества")
    move_kind = models.CharField(
        max_length=16,
        choices=MoveKind.choices,
        verbose_name="Вид движения",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")

    class Meta:
        verbose_name = "Движение товара у агента"
        verbose_name_plural = "Движения товаров у агентов"
        indexes = [
            models.Index(fields=["agent", "warehouse", "product", "created_at"]),
            models.Index(fields=["document", "move_kind"]),
        ]

    def __str__(self):
        return f"AgentMove {self.document.number} {self.product} {self.qty_delta} @ {self.agent_id}"


class Counterparty(models.Model):
    class Type(models.TextChoices):
        CLIENT = "CLIENT", "Клиент"
        SUPPLIER = "SUPPLIER", "Поставщик"
        BOTH = "BOTH", "Оба"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Компания",
    )
    branch = models.ForeignKey(
        "users.Branch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Филиал",
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_counterparties",
        verbose_name="Агент",
    )
    name = models.CharField(max_length=255, verbose_name="Название")
    phone = models.CharField(max_length=32, verbose_name="Телефон", blank=True, default="")
    type = models.CharField(max_length=16, choices=Type.choices, default=Type.BOTH, verbose_name="Тип")
    inn = models.CharField("ИНН", max_length=32, blank=True, default="")
    okpo = models.CharField("ОКПО", max_length=32, blank=True, default="")
    score = models.CharField("Расчетный счет", max_length=64, blank=True, default="")
    bik = models.CharField("БИК", max_length=32, blank=True, default="")
    address = models.CharField("Адрес", max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "Контрагент"
        verbose_name_plural = "Контрагенты"
        indexes = [
            models.Index(fields=["company", "branch"]),
            models.Index(fields=["company", "agent"]),
            models.Index(fields=["agent", "name"]),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.branch_id and self.company_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})
        if self.agent_id and self.company_id:
            agent_company_id = getattr(self.agent, "company_id", None)
            if agent_company_id == self.company_id:
                return
            if CompanyWarehouseAgent.objects.filter(
                user=self.agent,
                company_id=self.company_id,
                status=CompanyWarehouseAgent.Status.ACTIVE,
            ).exists():
                return
            raise ValidationError({"agent": "Агент должен быть сотрудником или активным агентом этой компании."})


class CounterpartyBankAccount(models.Model):
    """
    Банковский реквизит контрагента: расчётный счёт (Р/С) и БИК создаются парой.
    У контрагента может быть несколько таких пар.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    counterparty = models.ForeignKey(
        "warehouse.Counterparty",
        on_delete=models.CASCADE,
        related_name="bank_accounts",
        verbose_name="Контрагент",
    )
    score = models.CharField("Расчетный счет", max_length=64)
    bik = models.CharField("БИК", max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Банковский реквизит контрагента"
        verbose_name_plural = "Банковские реквизиты контрагентов"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["counterparty"]),
        ]

    def __str__(self):
        return f"{self.score} / {self.bik}"

    def clean(self):
        if not self.score or not self.bik:
            raise ValidationError("Р/С и БИК должны указываться вместе.")


class CompanyWarehouseAgent(models.Model):
    """
    Заявка/членство: пользователь как агент склада компании.
    Агент может сам отправить заявку (pending), владелец/админ принимает (active)
    или отказывает (rejected). Позже владелец может отстранить агента (removed).
    """
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает решения"
        ACTIVE = "active", "Активен (сотрудник компании)"
        REJECTED = "rejected", "Отклонён"
        REMOVED = "removed", "Отстранён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="warehouse_agent_requests",
        verbose_name="Компания",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="warehouse_company_agent_memberships",
        verbose_name="Агент (пользователь)",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name="Статус",
    )
    note = models.CharField(
        max_length=512,
        blank=True,
        verbose_name="Сообщение от агента при запросе",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата запроса")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата решения")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_agent_decisions",
        verbose_name="Кем решено",
    )
    assigned_warehouse = models.ForeignKey(
        "warehouse.Warehouse",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_company_agents",
        verbose_name="Назначенный склад",
        help_text="Если указан — агент работает только с этим складом в рамках компании.",
    )

    common_access_enabled = models.BooleanField(
        "Доступ к общему товару",
        default=False,
        help_text="Если включено — агент может работать с общим остатком выбранного склада (продажи со склада).",
    )
    common_warehouse = models.ForeignKey(
        "warehouse.Warehouse",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="common_access_agents",
        verbose_name="Склад общего доступа (основной)",
        help_text=(
            "Первый из складов общего доступа. Оставлен для обратной совместимости; "
            "полный набор — в common_warehouses."
        ),
    )
    common_warehouses = models.ManyToManyField(
        "warehouse.Warehouse",
        blank=True,
        related_name="common_access_agents_multi",
        verbose_name="Склады общего доступа",
        help_text="Склады, по которым агенту открыт общий доступ (если common_access_enabled=true).",
    )
    common_all_warehouses = models.BooleanField(
        "Доступ ко всем складам",
        default=False,
        help_text=(
            "Если включено — общий доступ ко всем складам компании (и всем их товарам). "
            "Явный список common_warehouses при этом игнорируется."
        ),
    )

    def common_warehouse_ids(self):
        """
        Все склады общего доступа:
        - common_all_warehouses=true → все склады компании;
        - иначе из M2M common_warehouses, с откатом на legacy-FK (для старых записей).
        """
        if self.common_all_warehouses:
            return list(
                Warehouse.objects.filter(company_id=self.company_id).values_list("id", flat=True)
            )
        ids = list(self.common_warehouses.values_list("id", flat=True))
        if not ids and self.common_warehouse_id:
            ids = [self.common_warehouse_id]
        return ids

    can_sell_wholesale = models.BooleanField(
        "Разрешена оптовая продажа",
        default=False,
        help_text=(
            "Выдаётся владельцем. Если включено — агент может оформлять продажи по оптовой цене "
            "(is_wholesale). Если выключено — агент продаёт только в розницу."
        ),
    )

    can_sell_without_approval = models.BooleanField(
        "Продажа без одобрения",
        default=False,
        help_text=(
            "Выдаётся владельцем доверенному агенту. Если включено — заявка агента одобряется "
            "автоматически в момент отправки (submit): товар сразу списывается со склада и "
            "зачисляется агенту, без участия владельца."
        ),
    )

    class Meta:
        verbose_name = "Агент склада (заявка в компанию)"
        verbose_name_plural = "Агенты складов (заявки в компании)"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "user"),
                name="uq_warehouse_company_warehouse_agent_company_user",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self):
        return f"{self.user_id} → {self.company_id} [{self.status}]"

    @classmethod
    def ensure_active_for_warehouse(cls, user, warehouse):
        """Проверяет, что пользователь — активный агент компании склада."""
        if warehouse is None:
            raise ValidationError({"warehouse": "Укажите склад."})
        membership = cls.objects.filter(
            company_id=warehouse.company_id,
            user=user,
            status=cls.Status.ACTIVE,
        ).first()
        if not membership:
            raise ValidationError({"agent": "Пользователь не является активным агентом этой компании."})
        assigned_id = membership.assigned_warehouse_id
        if assigned_id and assigned_id != warehouse.id:
            raise ValidationError({"agent": "Агенту назначен другой склад."})
        return membership


class CompanyStockPartnership(models.Model):
    """
    Пара компаний с активным партнёрством: общий доступ к складам и кассам (инкассация между компаниями).
    Пара хранится в каноническом порядке company_a.id < company_b.id (лексикографически по str(uuid)).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_a = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="stock_partnerships_as_a",
        verbose_name="Компания A",
    )
    company_b = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="stock_partnerships_as_b",
        verbose_name="Компания B",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Активно"
        TERMINATED = "TERMINATED", "Разорвано"

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True, verbose_name="Статус"
    )
    # Флаги «своей стороны»: a_* — решение компании A о себе (B может забирать у A без подтверждения и т.п.)
    a_allows_direct_pull = models.BooleanField(default=False, verbose_name="A разрешает B забирать без подтверждения")
    b_allows_direct_pull = models.BooleanField(default=False, verbose_name="B разрешает A забирать без подтверждения")
    a_shares_sales_history = models.BooleanField(default=True, verbose_name="A показывает B свои продажи")
    b_shares_sales_history = models.BooleanField(default=True, verbose_name="B показывает A свои продажи")
    created_from_request = models.ForeignKey(
        "warehouse.CompanyStockPartnershipRequest", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", verbose_name="Из заявки",
    )
    activated_at = models.DateTimeField(null=True, blank=True, verbose_name="Активировано")
    terminated_at = models.DateTimeField(null=True, blank=True, verbose_name="Разорвано")
    terminated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        verbose_name="Кто разорвал",
    )
    terminated_by_company = models.ForeignKey(
        "users.Company", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        verbose_name="Какая компания разорвала",
    )

    def _side(self, company_id) -> str:
        cid = str(company_id)
        if cid == str(self.company_a_id):
            return "a"
        if cid == str(self.company_b_id):
            return "b"
        raise ValueError("Компания не участвует в этом партнёрстве.")

    def partner_id_of(self, company_id):
        return self.company_b_id if self._side(company_id) == "a" else self.company_a_id

    def allows_direct_pull_from(self, company_id) -> bool:
        """Разрешила ли компания company_id партнёру забирать у себя без подтверждения."""
        return bool(getattr(self, f"{self._side(company_id)}_allows_direct_pull"))

    def shares_sales_history_of(self, company_id) -> bool:
        """Показывает ли компания company_id партнёру свою историю продаж."""
        return bool(getattr(self, f"{self._side(company_id)}_shares_sales_history"))

    def set_side_flag(self, company_id, name: str, value: bool):
        setattr(self, f"{self._side(company_id)}_{name}", bool(value))

    class Meta:
        verbose_name = "Партнёрство компаний (склад и касса)"
        verbose_name_plural = "Партнёрства компаний (склад и касса)"
        constraints = [
            models.UniqueConstraint(fields=("company_a", "company_b"), name="uq_stock_partnership_company_pair"),
        ]
        indexes = [
            models.Index(fields=["company_a", "company_b"]),
        ]

    def __str__(self):
        return f"{self.company_a_id} ↔ {self.company_b_id}"


class CompanyStockPartnershipRequest(models.Model):
    """
    Запрос на партнёрство (склад + касса / инкассация): от одной компании к другой.
    После принятия создаётся CompanyStockPartnership.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Ожидает"
        ACCEPTED = "ACCEPTED", "Принят"
        REJECTED = "REJECTED", "Отклонён"
        CANCELLED = "CANCELLED", "Отозван"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    from_company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="stock_partnership_requests_out",
        verbose_name="От компании",
    )
    to_company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="stock_partnership_requests_in",
        verbose_name="К компании",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name="Статус",
    )
    note = models.CharField(max_length=512, blank=True, verbose_name="Комментарий")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_partnership_requests_created",
        verbose_name="Кто создал",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_partnership_requests_decided",
        verbose_name="Кто решил",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата решения")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        verbose_name = "Запрос партнёрства (склад и касса)"
        verbose_name_plural = "Запросы партнёрства (склад и касса)"
        constraints = [
            models.UniqueConstraint(
                fields=("from_company", "to_company"),
                condition=models.Q(status="PENDING"),
                name="uq_stock_partnership_req_pending_direction",
            ),
        ]
        indexes = [
            models.Index(fields=["from_company", "status"]),
            models.Index(fields=["to_company", "status"]),
        ]

    def __str__(self):
        return f"{self.from_company_id} → {self.to_company_id} [{self.status}]"


class CompanyCashIncassation(models.Model):
    """
    Инкассация: перевод наличных с кассы одной компании на кассу партнёрской компании.
    Создаёт пару проведённых денежных документов (расход + приход).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    from_company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="cash_incassations_out",
        verbose_name="Компания-отправитель",
    )
    to_company = models.ForeignKey(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="cash_incassations_in",
        verbose_name="Компания-получатель",
    )
    cash_register_from = models.ForeignKey(
        "warehouse.CashRegister",
        on_delete=models.PROTECT,
        related_name="incassations_out",
        verbose_name="Касса-источник",
    )
    cash_register_to = models.ForeignKey(
        "warehouse.CashRegister",
        on_delete=models.PROTECT,
        related_name="incassations_in",
        verbose_name="Касса-приёмник",
    )
    expense_document = models.OneToOneField(
        "warehouse.MoneyDocument",
        on_delete=models.PROTECT,
        related_name="incassation_as_expense",
        verbose_name="Расход (исходящий)",
    )
    receipt_document = models.OneToOneField(
        "warehouse.MoneyDocument",
        on_delete=models.PROTECT,
        related_name="incassation_as_receipt",
        verbose_name="Приход (входящий)",
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2, verbose_name="Сумма")
    comment = models.CharField(max_length=512, blank=True, verbose_name="Комментарий")
    partner_operation = models.ForeignKey(
        "warehouse.PartnerOperationRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        verbose_name="Подтверждённый запрос",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_incassations_created",
        verbose_name="Кто создал",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")

    class Meta:
        verbose_name = "Инкассация между компаниями"
        verbose_name_plural = "Инкассации между компаниями"
        indexes = [
            models.Index(fields=["from_company", "created_at"]),
            models.Index(fields=["to_company", "created_at"]),
        ]

    def __str__(self):
        return f"{self.from_company_id} → {self.to_company_id} {self.amount}"


class CompanyStockPartnershipEvent(models.Model):
    """Журнал партнёрства: активация, разрыв, смена настроек стороны. Не удаляется."""

    class Kind(models.TextChoices):
        ACTIVATED = "ACTIVATED", "Активировано"
        TERMINATED = "TERMINATED", "Разорвано"
        SETTINGS_CHANGED = "SETTINGS_CHANGED", "Настройки изменены"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partnership = models.ForeignKey(CompanyStockPartnership, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    company = models.ForeignKey("users.Company", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Событие партнёрства"
        verbose_name_plural = "События партнёрства"
        ordering = ("-created_at",)


class PartnerOperationRequest(models.Model):
    """«Забрать у партнёра» (товар или деньги), ожидающее подтверждения владельца/админа партнёра."""

    class Kind(models.TextChoices):
        TRANSFER = "TRANSFER", "Товар"
        INCASSATION = "INCASSATION", "Деньги"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Ожидает"
        APPROVED = "APPROVED", "Проведена"
        REJECTED = "REJECTED", "Отклонена"
        CANCELLED = "CANCELLED", "Отозвана"
        FAILED = "FAILED", "Ошибка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partnership = models.ForeignKey(CompanyStockPartnership, on_delete=models.PROTECT, related_name="operations")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    initiator_company = models.ForeignKey(
        "users.Company", on_delete=models.PROTECT, related_name="partner_operations_out"
    )
    source_company = models.ForeignKey(
        "users.Company", on_delete=models.PROTECT, related_name="partner_operations_in"
    )
    warehouse_from = models.ForeignKey("warehouse.Warehouse", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    warehouse_to = models.ForeignKey("warehouse.Warehouse", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    items = models.JSONField(default=list, blank=True)
    cash_register_from = models.ForeignKey("warehouse.CashRegister", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    cash_register_to = models.ForeignKey("warehouse.CashRegister", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    comment = models.CharField(max_length=512, blank=True)
    reject_reason = models.CharField(max_length=512, blank=True)
    error = models.CharField(max_length=512, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    document = models.ForeignKey("warehouse.Document", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    incassation = models.ForeignKey(
        "warehouse.CompanyCashIncassation", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Операция с партнёром (ожидает подтверждения)"
        verbose_name_plural = "Операции с партнёром"
        indexes = [
            models.Index(fields=["source_company", "status"]),
            models.Index(fields=["initiator_company", "status"]),
        ]


def get_stock_partnership(company_id_a, company_id_b, *, active_only=True):
    if not company_id_a or not company_id_b or str(company_id_a) == str(company_id_b):
        return None
    id_lo, id_hi = canonical_company_pair_ids(company_id_a, company_id_b)
    qs = CompanyStockPartnership.objects.filter(company_a_id=id_lo, company_b_id=id_hi)
    if active_only:
        qs = qs.filter(status=CompanyStockPartnership.Status.ACTIVE)
    return qs.first()


def canonical_company_pair_ids(company_id_a, company_id_b):
    """Два UUID компании в стабильном порядке для company_a / company_b."""
    return sorted([company_id_a, company_id_b], key=str)


def has_active_stock_partnership_between_ids(company_id_a, company_id_b) -> bool:
    if not company_id_a or not company_id_b or str(company_id_a) == str(company_id_b):
        return False
    id_lo, id_hi = canonical_company_pair_ids(company_id_a, company_id_b)
    return CompanyStockPartnership.objects.filter(
        company_a_id=id_lo, company_b_id=id_hi, status=CompanyStockPartnership.Status.ACTIVE
    ).exists()


def list_active_stock_partner_companies(company):
    """
    Компании с активным складским партнёрством для `company`.
    Тот же набор, что возвращает GET /api/warehouse/stock-partnerships/active/.
    """
    if not company:
        return []
    qs = CompanyStockPartnership.objects.filter(
        Q(company_a=company) | Q(company_b=company), status=CompanyStockPartnership.Status.ACTIVE
    ).select_related("company_a", "company_b")
    partners = []
    seen = set()
    for row in qs:
        partner = row.company_b if row.company_a_id == company.id else row.company_a
        if partner.id in seen:
            continue
        seen.add(partner.id)
        partners.append(partner)
    return partners


class DocumentSequence(models.Model):
    doc_type = models.CharField(max_length=32, verbose_name="Тип документа")
    date = models.DateField(verbose_name="Дата")
    seq = models.PositiveIntegerField(default=0, verbose_name="Последовательность")

    class Meta:
        verbose_name = "Последовательность документов"
        verbose_name_plural = "Последовательности документов"
        unique_together = (("doc_type", "date"),)


class Document(models.Model):
    class DocType(models.TextChoices):
        COMMERCIAL_OFFER = "COMMERCIAL_OFFER", "Коммерческое предложение"
        SALE = "SALE", "Продажа"
        PURCHASE = "PURCHASE", "Покупка"
        SALE_RETURN = "SALE_RETURN", "Возврат продажи"
        PURCHASE_RETURN = "PURCHASE_RETURN", "Возврат покупки"
        INVENTORY = "INVENTORY", "Инвентаризация"
        RECEIPT = "RECEIPT", "Приход"
        WRITE_OFF = "WRITE_OFF", "Списание"
        TRANSFER = "TRANSFER", "Перемещение"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Черновик"
        SALE_REQUEST = "SALE_REQUEST", "Заявка на продажу"
        CASH_PENDING = "CASH_PENDING", "Ожидает решения кассы"
        POSTED = "POSTED", "Проведен"
        REJECTED = "REJECTED", "Отклонен"

    class PaymentKind(models.TextChoices):
        """Способ оплаты по документу (для SALE/PURCHASE и возвратов)."""
        CASH = "cash", "Оплата сразу"
        CREDIT = "credit", "В долг"
        EXTERNAL = "external", "Вне кассы"

    class PaymentMethod(models.TextChoices):
        """Форма оплаты: наличными или безналичными (для фильтрации на кассе)."""
        CASH = "cash", "Наличными"
        CASHLESS = "cashless", "Безналичными"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    doc_type = models.CharField(max_length=32, choices=DocType.choices, verbose_name="Тип документа")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, verbose_name="Статус")
    number = models.CharField(max_length=64, unique=True, null=True, blank=True, verbose_name="Номер")
    date = models.DateTimeField(
        default=timezone.now,
        verbose_name="Дата",
        help_text="Операционная дата документа (для печати, списков и аналитики). "
                  "Задаётся пользователем; по умолчанию — текущий момент. "
                  "Не путать с created_at (момент создания записи).",
    )

    payment_kind = models.CharField(
        max_length=16,
        choices=PaymentKind.choices,
        default=PaymentKind.CASH,
        blank=True,
        null=True,
        verbose_name="Оплата",
        help_text="Продажа/покупка/возвраты: cash или credit. Приход (RECEIPT): cash, credit или external (приход на склад без кассы).",
    )

    payment_method = models.CharField(
        max_length=16,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
        blank=True,
        null=True,
        verbose_name="Форма оплаты",
        help_text="Наличными или безналичными. Переносится в денежный документ кассы для фильтрации.",
    )

    prepayment_amount = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Предоплата",
        help_text="Для payment_kind=credit: сумма предоплаты по документу (создаст денежный документ при проведении).",
    )

    warehouse_from = models.ForeignKey("warehouse.Warehouse", on_delete=models.SET_NULL, null=True, blank=True, related_name="documents_from", verbose_name="Склад-источник")
    warehouse_to = models.ForeignKey("warehouse.Warehouse", on_delete=models.SET_NULL, null=True, blank=True, related_name="documents_to", verbose_name="Склад-приемник")
    counterparty = models.ForeignKey("warehouse.Counterparty", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Контрагент")
    cash_register = models.ForeignKey(
        "warehouse.CashRegister",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_documents",
        verbose_name="Касса",
        help_text="Если payment_kind=cash и включена автокасса — по этой кассе будет создан MONEY_RECEIPT/MONEY_EXPENSE.",
    )
    payment_category = models.ForeignKey(
        "warehouse.PaymentCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_documents",
        verbose_name="Категория платежа",
        help_text="Категория для автосоздаваемого денежного документа при payment_kind=cash.",
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_documents",
        verbose_name="Агент",
    )

    use_common_stock = models.BooleanField(
        "Использовать общий товар со склада",
        default=False,
        db_index=True,
        help_text="Для документов агента: списывать со склада (общий товар), а не с остатков агента.",
    )
    is_sale_request = models.BooleanField(
        "Заявка на продажу",
        default=False,
        help_text="Если включено для документа SALE — статус документа будет 'Заявка на продажу'.",
    )
    is_wholesale = models.BooleanField(
        "Оптовая продажа",
        default=False,
        db_index=True,
        help_text=(
            "Переключатель опт/розница для документа SALE. Если включено — для строк без явной "
            "цены подставляется оптовая цена товара (с откатом на розничную, если опт не задана)."
        ),
    )

    comment = models.TextField(blank=True, verbose_name="Комментарий")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        verbose_name="Кто создал",
    )
    # QA B04: возврат оформляется по конкретной продаже (SALE_RETURN) или закупу (PURCHASE_RETURN).
    base_document = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="returns",
        verbose_name="Документ-основание",
        help_text="Продажа (для SALE_RETURN) или закуп (для PURCHASE_RETURN), по которой оформлен возврат.",
    )
    # QA B02: кто и когда провёл документ в минус (allow_negative).
    posted_negative_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        verbose_name="Проведён в минус: кто",
    )
    posted_negative_at = models.DateTimeField(null=True, blank=True, verbose_name="Проведён в минус: когда")
    # QA B07: кто и когда последним менял дату документа.
    date_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        verbose_name="Дату менял",
    )
    date_changed_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата изменена")
    initiator_company = models.ForeignKey(
        "users.Company", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        verbose_name="Компания-инициатор (межкомпанейское перемещение)",
    )
    # Компания документа (A13): заполняется по складу-источнику (иначе приёмнику), для
    # мультискладских документов без склада — по товарам строк при проведении.
    company = models.ForeignKey(
        "users.Company", null=True, blank=True, on_delete=models.SET_NULL, related_name="warehouse_documents",
        verbose_name="Компания",
    )
    total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Итого")
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        verbose_name="Общая скидка, %", help_text="Скидка на весь документ в процентах"
    )
    discount_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        verbose_name="Общая скидка, сумма", help_text="Фиксированная скидка на весь документ"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        verbose_name = "Документ"
        verbose_name_plural = "Документы"
        indexes = [
            models.Index(fields=["company", "doc_type", "status", "date"]),
        ]

    def __str__(self):
        return f"{self.number} ({self.doc_type})"

    def resolve_company_id(self):
        """Компания по складам документа, иначе по товарам строк (для мультискладских)."""
        for wh in (self.warehouse_from, self.warehouse_to):
            if wh is not None and wh.company_id:
                return wh.company_id
        if self.pk:
            return (
                DocumentItem.objects.filter(document_id=self.pk, product__isnull=False)
                .values_list("product__company_id", flat=True)
                .first()
            )
        return None

    def save(self, *args, **kwargs):
        if self.company_id is None:
            company_id = None
            for wh_id_attr, wh_attr in (("warehouse_from_id", "warehouse_from"), ("warehouse_to_id", "warehouse_to")):
                if getattr(self, wh_id_attr):
                    company_id = getattr(self, wh_attr).company_id
                    break
            if company_id is not None:
                self.company_id = company_id
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = list(set(update_fields) | {"company"})
        super().save(*args, **kwargs)

    def clean(self):
        from django.core.exceptions import ValidationError
        from .utils import normalize_payment_kind

        if self.payment_kind:
            self.payment_kind = normalize_payment_kind(self.payment_kind)

        prepayment = Decimal(getattr(self, "prepayment_amount", None) or 0)
        if prepayment < 0:
            raise ValidationError({"prepayment_amount": "Предоплата не может быть отрицательной."})

        if self.doc_type == self.DocType.COMMERCIAL_OFFER:
            # Коммерческое предложение — только расчет, без проведения/остатков/кассы.
            if not self.warehouse_from and not self.agent_id:
                pass
            elif not self.warehouse_from:
                raise ValidationError("Document requires warehouse_from")
            if prepayment > 0:
                raise ValidationError({"prepayment_amount": "Предоплата недоступна для коммерческого предложения."})
            # payment_kind не имеет смысла; оставляем как есть, но не валидируем дальше.
            return

        if self.doc_type == self.DocType.TRANSFER:
            if not self.warehouse_from or not self.warehouse_to:
                raise ValidationError("TRANSFER requires both warehouse_from and warehouse_to")
            if self.warehouse_from_id == self.warehouse_to_id:
                raise ValidationError("warehouse_from and warehouse_to must be different")
            if self.warehouse_from and self.warehouse_to:
                cfrom = self.warehouse_from.company_id
                cto = self.warehouse_to.company_id
                if cfrom != cto:
                    if not has_active_stock_partnership_between_ids(cfrom, cto):
                        raise ValidationError(
                            {
                                "warehouse_to": "Межкомпанейское перемещение доступно только при принятом партнёрстве складов между компаниями."
                            }
                        )

        if self.agent_id:
            from apps.warehouse.services import AGENT_MULTI_WAREHOUSE_DOC_TYPES

            if self.doc_type in (self.DocType.TRANSFER, self.DocType.INVENTORY):
                raise ValidationError("Agent documents cannot be TRANSFER or INVENTORY")
            # Для мультискладских документов агента единый warehouse_from
            # необязателен — склад берётся из каждой позиции.
            agent_multi = self.doc_type in AGENT_MULTI_WAREHOUSE_DOC_TYPES
            if not self.warehouse_from and not agent_multi:
                raise ValidationError("Agent document requires warehouse_from")
            if getattr(self.agent, "company_id", None) and self.warehouse_from:
                if self.agent.company_id != self.warehouse_from.company_id:
                    raise ValidationError("Agent belongs to another company")
            if self.counterparty_id:
                cp_agent_id = getattr(self.counterparty, "agent_id", None)
                if cp_agent_id != self.agent_id:
                    raise ValidationError({"counterparty": "Контрагент не принадлежит агенту."})

        if self.doc_type in (self.DocType.SALE, self.DocType.SALE_RETURN, self.DocType.PURCHASE, self.DocType.PURCHASE_RETURN, self.DocType.RECEIPT, self.DocType.WRITE_OFF):
            from apps.warehouse.services import AGENT_MULTI_WAREHOUSE_DOC_TYPES

            # require warehouse_from for most operations (warehouse where stock changes).
            # Мультисклад (владелец: продажа/возврат; агент: продажа/возвраты/списание)
            # допускает пустой warehouse_from — склад берётся из позиций.
            multi_warehouse_no_from = (
                (self.doc_type in (self.DocType.SALE, self.DocType.SALE_RETURN) and not self.agent_id)
                or (self.agent_id and self.doc_type in AGENT_MULTI_WAREHOUSE_DOC_TYPES)
            )
            if not multi_warehouse_no_from and not self.warehouse_from:
                raise ValidationError("Document requires warehouse_from")
            if self.doc_type in (self.DocType.SALE_RETURN, self.DocType.PURCHASE_RETURN) and not self.counterparty:
                raise ValidationError("Document requires counterparty")

        if self.doc_type in (self.DocType.SALE, self.DocType.PURCHASE, self.DocType.SALE_RETURN, self.DocType.PURCHASE_RETURN):
            if self.payment_kind and self.payment_kind not in (self.PaymentKind.CASH, self.PaymentKind.CREDIT):
                raise ValidationError({"payment_kind": "Укажите cash (оплата сразу) или credit (в долг)."})
        elif self.doc_type == self.DocType.RECEIPT:
            if self.payment_kind and self.payment_kind not in (
                self.PaymentKind.CASH,
                self.PaymentKind.CREDIT,
                self.PaymentKind.EXTERNAL,
            ):
                raise ValidationError(
                    {"payment_kind": "Для прихода укажите cash, credit или external (без кассы / оплата иными средствами)."}
                )
        else:
            # Предоплата имеет смысл только для документов с payment_kind (продажа/покупка и возвраты).
            if prepayment > 0:
                raise ValidationError({"prepayment_amount": "Предоплата доступна только для SALE/PURCHASE и возвратов."})

        if prepayment > 0:
            pk = self.payment_kind or self.PaymentKind.CASH
            if pk != self.PaymentKind.CREDIT:
                raise ValidationError({"prepayment_amount": "Предоплата возможна только при payment_kind=credit."})

        # Общая скидка на документ
        dp = Decimal(getattr(self, "discount_percent", None) or 0)
        if not (Decimal("0") <= dp <= Decimal("100")):
            raise ValidationError({"discount_percent": "Общая скидка должна быть от 0 до 100%."})
        da = Decimal(getattr(self, "discount_amount", None) or 0)
        if da < 0:
            raise ValidationError({"discount_amount": "Общая скидка не может быть отрицательной."})


class DocumentItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="items", verbose_name="Документ")
    # SET_NULL: товар можно удалить, строки документов (история) сохраняются.
    product = models.ForeignKey(
        "warehouse.WarehouseProduct", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="document_items", verbose_name="Товар",
    )
    qty = models.DecimalField(max_digits=18, decimal_places=3, verbose_name="Количество")
    price = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Цена")
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"), verbose_name="Скидка на товар, %"
    )
    # Применяется, только если процент скидки не действует (ни свой, ни общий по документу):
    # процент и сумму не суммируем, см. services.compute_document_line_total.
    discount_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        verbose_name="Скидка на товар, сумма", help_text="Фиксированная скидка по строке"
    )
    line_total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Итого по строке")
    net_amount = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Чистая сумма строки",
        help_text="line_total минус доля скидки документа; Σ по документу == Document.total.",
    )
    cost_price = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        verbose_name="Себестоимость единицы",
        help_text="Закупочная цена товара на момент проведения. NULL — строка проведена до появления поля "
                  "(аналитика берёт текущую закупочную цену и помечает cost_is_estimated).",
    )
    # QA B04: строка продажи/закупа, по которой возвращают товар.
    base_item = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="returned_items",
        verbose_name="Строка-основание",
    )
    # QA B01: куда зачислен товар на складе-получателе (для TRANSFER).
    target_product = models.ForeignKey(
        "warehouse.WarehouseProduct", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Товар на складе-получателе",
    )

    class Meta:
        verbose_name = "Строка документа"
        verbose_name_plural = "Строки документов"
        ordering = ["product__name", "id"]

    def clean(self):
        from django.core.exceptions import ValidationError

        doc_for_qty = getattr(self, "document", None)
        is_inventory = getattr(doc_for_qty, "doc_type", None) == "INVENTORY"
        # Инвентаризация задаёт фактический остаток — факт 0 допустим.
        if self.qty is None or Decimal(self.qty) < Decimal("0") or (
            Decimal(self.qty) == Decimal("0") and not is_inventory
        ):
            raise ValidationError({"qty": "Quantity must be > 0"})

        if self.discount_percent is None:
            self.discount_percent = Decimal("0.00")
        dp = Decimal(self.discount_percent)
        if not (Decimal("0") <= dp <= Decimal("100")):
            raise ValidationError({"discount_percent": "Скидка должна быть от 0 до 100%."})
        if self.discount_amount is None:
            self.discount_amount = Decimal("0.00")
        da = Decimal(self.discount_amount)
        if da < 0:
            raise ValidationError({"discount_amount": "Скидка по строке не может быть отрицательной."})

        # Проверка соответствия товара складу документа
        # Проверяем только если document уже сохранен (имеет pk) или передан напрямую
        doc = getattr(self, 'document', None)
        if doc and self.product_id:
            from apps.warehouse.services import document_allows_multi_warehouse

            multi_warehouse = document_allows_multi_warehouse(doc)

            # Если document еще не сохранен, но передан - проверяем по ID
            if hasattr(doc, 'pk') and doc.pk is None:
                # Документ еще не сохранен - проверяем по warehouse_from_id напрямую
                if hasattr(doc, 'warehouse_from_id') and doc.warehouse_from_id and not multi_warehouse:
                    prod = self.product
                    if doc.doc_type == doc.DocType.TRANSFER:
                        if prod.warehouse_id != doc.warehouse_from_id:
                            raise ValidationError({"product": "Товар должен принадлежать складу-источнику перемещения."})
                    else:
                        if prod.warehouse_id != doc.warehouse_from_id:
                            raise ValidationError({"product": "Товар должен принадлежать складу документа."})
            elif hasattr(doc, 'pk') and doc.pk:
                # Документ сохранен - полная проверка
                prod = self.product

                doc_company_id = None
                if hasattr(doc, 'warehouse_from') and doc.warehouse_from_id:
                    if not hasattr(doc.warehouse_from, 'company_id'):
                        doc.warehouse_from.refresh_from_db()
                    doc_company_id = doc.warehouse_from.company_id
                elif multi_warehouse and prod.warehouse_id:
                    doc_company_id = prod.company_id

                if doc_company_id is not None and prod.company_id != doc_company_id:
                    raise ValidationError({"product": "Товар принадлежит другой компании, чем склад документа."})

                # Проверка склада для операций с одним складом
                if not multi_warehouse:
                    if doc.doc_type != doc.DocType.TRANSFER:
                        if doc.warehouse_from_id and prod.warehouse_id != doc.warehouse_from_id:
                            warehouse_name = doc.warehouse_from.name if hasattr(doc, 'warehouse_from') and doc.warehouse_from else "документа"
                            raise ValidationError({"product": f"Товар должен принадлежать складу '{warehouse_name}'."})
                    else:
                        # Для TRANSFER товар должен принадлежать складу-источнику
                        if doc.warehouse_from_id and prod.warehouse_id != doc.warehouse_from_id:
                            raise ValidationError({"product": "Товар должен принадлежать складу-источнику перемещения."})
                elif not prod.warehouse_id:
                    raise ValidationError({"product": "Товар должен быть привязан к складу."})

            # Склад позиции: в мультискладском документе агента — склад товара
            # (product.warehouse), иначе — единый warehouse_from документа.
            item_warehouse_id = self.product.warehouse_id if multi_warehouse else doc.warehouse_from_id
            if doc.agent_id and item_warehouse_id:
                # Для документов агента возможны два режима списания:
                # - use_common_stock=True: списываем со склада (общий товар) — не требуем AgentStockBalance
                # - use_common_stock=False: списываем с остатков агента — требуем AgentStockBalance
                if not bool(getattr(doc, "use_common_stock", False)):
                    has_balance = AgentStockBalance.objects.filter(
                        agent_id=doc.agent_id,
                        warehouse_id=item_warehouse_id,
                        product_id=self.product_id,
                    ).exists()
                    if not has_balance:
                        raise ValidationError({"product": "Товар отсутствует в остатках агента."})

        # integral check for PCS
        try:
            unit = self.product.unit if self.product_id else None
        except Exception:
            unit = None

        if unit and unit.lower().startswith("pcs") or (self.product_id and getattr(self.product, "is_weight", False) is False):
            # treat as pieces: require integer qty
            if (Decimal(self.qty) % 1) != 0:
                raise ValidationError({"qty": "Quantity must be integer for piece items"})

    def save(self, *args, **kwargs):
        from apps.warehouse.services import compute_document_line_total

        doc = getattr(self, "document", None)
        doc_dp = Decimal(getattr(doc, "discount_percent", None) or 0) if doc is not None else Decimal("0")
        self.line_total = compute_document_line_total(
            price=self.price,
            qty=self.qty,
            line_discount_percent=self.discount_percent,
            line_discount_amount=self.discount_amount,
            document_discount_percent=doc_dp,
        )
        super().save(*args, **kwargs)


class AgentRequestCart(BaseModelId, BaseModelDate, BaseModelCompanyBranch):
    """
    Заявка агента на получение товара со склада.
    """
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        SUBMITTED = "submitted", "Отправлено владельцу"
        APPROVED = "approved", "Одобрено и выдано"
        REJECTED = "rejected", "Отклонено"

    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="warehouse_agent_carts",
        verbose_name="Агент",
    )
    warehouse = models.ForeignKey(
        "warehouse.Warehouse",
        on_delete=models.PROTECT,
        related_name="agent_request_carts",
        verbose_name="Склад",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    note = models.CharField(max_length=255, blank=True, verbose_name="Комментарий агента")

    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="warehouse_approved_agent_carts",
        verbose_name="Кем одобрено",
    )
    auto_approved = models.BooleanField(
        "Автоодобрено",
        default=False,
        help_text=(
            "True — заявка одобрена автоматически по праву агента "
            "can_sell_without_approval (в момент submit, без участия владельца). "
            "В этом случае approved_by = NULL."
        ),
    )

    sale_document = models.OneToOneField(
        "warehouse.Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_agent_cart",
        verbose_name="Документ продажи",
        help_text="Если по заявке оформлена продажа — ссылка на документ SALE, привязанный к агенту.",
    )

    class Meta:
        verbose_name = "Заявка агента (склад)"
        verbose_name_plural = "Заявки агентов (склад)"
        ordering = ["-created_date"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "branch", "status"]),
            models.Index(fields=["agent", "status"]),
        ]

    def __str__(self):
        return f"Заявка {self.id} от {getattr(self.agent, 'username', self.agent_id)} [{self.get_status_display()}]"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})
        if self.warehouse_id and self.company_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "Склад принадлежит другой компании."})
        if self.branch_id and self.warehouse_id and self.warehouse.branch_id not in (None, self.branch_id):
            raise ValidationError({"warehouse": "Склад другого филиала."})
        # Агент без компании (company_id=None) может быть агентом по заявке (CompanyWarehouseAgent) — доступ проверяется в API
        agent_company_id = getattr(self.agent, "company_id", None)
        if self.agent_id and self.company_id and agent_company_id is not None and agent_company_id != self.company_id:
            raise ValidationError({"agent": "Агент принадлежит другой компании."})

    def is_editable(self) -> bool:
        return self.status == self.Status.DRAFT

    def _warehouse_on_hand(self, product):
        from apps.warehouse import stock as stock_service

        return stock_service.get_on_hand(warehouse=self.warehouse, product=product)

    def _submitted_reserved_qty(self, product_id, *, exclude_cart_id=None):
        qs = AgentRequestItem.objects.filter(
            cart__warehouse_id=self.warehouse_id,
            cart__status=self.Status.SUBMITTED,
            product_id=product_id,
        )
        if exclude_cart_id:
            qs = qs.exclude(cart_id=exclude_cart_id)
        return q_qty(qs.aggregate(total=Sum("quantity_requested"))["total"] or 0)

    def warehouse_available_qty(self, product, *, exclude_item_id=None):
        """
        Сколько ещё можно добавить в заявку с учётом:
        - остатка на складе;
        - других заявок в статусе submitted;
        - других позиций этого же товара в текущем черновике.
        """
        on_hand = self._warehouse_on_hand(product)
        reserved = self._submitted_reserved_qty(product.pk, exclude_cart_id=self.pk)
        other_in_cart = Decimal("0.000")
        if self.pk:
            qs = AgentRequestItem.objects.filter(cart_id=self.pk, product_id=product.pk)
            if exclude_item_id:
                qs = qs.exclude(pk=exclude_item_id)
            other_in_cart = q_qty(qs.aggregate(total=Sum("quantity_requested"))["total"] or 0)
        return max(on_hand - reserved - other_in_cart, Decimal("0.000"))

    def _validate_items_against_warehouse_stock(self):
        totals = {}
        products = {}
        for it in self.items.select_related("product"):
            pid = it.product_id
            totals[pid] = totals.get(pid, Decimal("0.000")) + q_qty(Decimal(it.quantity_requested or 0))
            products[pid] = it.product
        for pid, need in totals.items():
            if need <= 0:
                continue
            prod = products[pid]
            on_hand = self._warehouse_on_hand(prod)
            reserved = self._submitted_reserved_qty(pid, exclude_cart_id=self.pk)
            available = max(on_hand - reserved, Decimal("0.000"))
            if need > available:
                raise ValidationError({
                    "items": (
                        f"Недостаточно на складе для {prod.name}: "
                        f"запрошено {need}, доступно {available}."
                    )
                })

    @transaction.atomic
    def submit(self, *, auto_approve=False):
        """
        Отправка заявки агентом (draft → submitted).

        Если auto_approve=True (агент с правом can_sell_without_approval),
        в той же транзакции выполняется логика approve: остатки проверяются и
        блокируются, товар списывается со склада и зачисляется агенту, а заявка
        сразу переходит в approved (approved_by=NULL, auto_approved=True).
        """
        if self.status != self.Status.DRAFT:
            raise ValidationError("Можно отправить только черновик.")
        if not self.items.exists():
            raise ValidationError("Нельзя отправить пустую заявку.")
        self._validate_items_against_warehouse_stock()

        now = timezone.now()
        self.submitted_at = now

        if not auto_approve:
            self.status = self.Status.SUBMITTED
            self.full_clean()
            self.save(update_fields=["status", "submitted_at", "updated_date"])
            return

        # Автоодобрение: та же логика, что и approve, но без участия владельца.
        self._transfer_items_to_agent()
        self.status = self.Status.APPROVED
        self.approved_at = now
        self.approved_by = None
        self.auto_approved = True
        self.full_clean()
        self.save(update_fields=[
            "status", "submitted_at", "approved_at", "approved_by",
            "auto_approved", "updated_date",
        ])

    def _transfer_items_to_agent(self):
        from apps.warehouse import stock as stock_service

        for it in self.items.select_related("product"):
            prod = it.product
            need_qty = q_qty(Decimal(it.quantity_requested or 0))
            if need_qty <= 0:
                continue

            cur_qty = stock_service.get_on_hand(warehouse=self.warehouse, product=prod, lock=True)
            if cur_qty < need_qty:
                raise ValidationError({
                    "items": f"Недостаточно на складе для {prod.name}: нужно {need_qty}, доступно {cur_qty}."
                })

            stock_service.apply_stock_delta(
                warehouse=self.warehouse,
                product=prod,
                delta=-need_qty,
                source_kind=StockMove.SourceKind.AGENT_ISSUE,
                source_id=self.pk,
            )

            stock, _ = AgentStockBalance.objects.select_for_update().get_or_create(
                agent=self.agent,
                warehouse=self.warehouse,
                product=prod,
                defaults={
                    "qty": Decimal("0.000"),
                    "company": self.company,
                    "branch": self.branch,
                },
            )
            stock.qty = q_qty(Decimal(stock.qty or 0) + need_qty)
            stock.save(update_fields=["qty"])

    @transaction.atomic
    def approve(self, by_user):
        if self.status != self.Status.SUBMITTED:
            raise ValidationError("Можно одобрить только заявку в статусе 'submitted'.")
        if not self.items.exists():
            raise ValidationError("Нельзя одобрить пустую заявку.")

        self._transfer_items_to_agent()

        self.status = self.Status.APPROVED
        self.approved_at = timezone.now()
        self.approved_by = by_user
        self.full_clean()
        self.save(update_fields=["status", "approved_at", "approved_by", "updated_date"])

    @transaction.atomic
    def dispatch_by_owner(self, by_user):
        """Владелец выдаёт товар агенту без предварительной заявки агента."""
        if self.status != self.Status.DRAFT:
            raise ValidationError("Можно выдать товар только из черновика.")
        if not self.items.exists():
            raise ValidationError("Нельзя выдать товар по пустой заявке.")

        self._validate_items_against_warehouse_stock()
        self._transfer_items_to_agent()

        now = timezone.now()
        self.status = self.Status.APPROVED
        self.submitted_at = now
        self.approved_at = now
        self.approved_by = by_user
        self.full_clean()
        self.save(update_fields=["status", "submitted_at", "approved_at", "approved_by", "updated_date"])

    @transaction.atomic
    def reject(self, by_user):
        if self.status != self.Status.SUBMITTED:
            raise ValidationError("Можно отклонить только заявку в статусе 'submitted'.")
        self.status = self.Status.REJECTED
        self.approved_at = timezone.now()
        self.approved_by = by_user
        self.full_clean()
        self.save(update_fields=["status", "approved_at", "approved_by", "updated_date"])


class AgentRequestItem(BaseModelId, BaseModelDate, BaseModelCompanyBranch):
    cart = models.ForeignKey(
        AgentRequestCart,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Заявка",
    )
    product = models.ForeignKey(
        "warehouse.WarehouseProduct",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_request_items",
        verbose_name="Товар",
    )
    quantity_requested = models.DecimalField(max_digits=18, decimal_places=3, verbose_name="Запрошено")

    class Meta:
        verbose_name = "Позиция заявки агента (склад)"
        verbose_name_plural = "Позиции заявок агента (склад)"
        indexes = [
            models.Index(fields=["cart", "product"]),
        ]

    def __str__(self):
        return f"{self.cart_id} · {self.product_id} · {self.quantity_requested}"

    def clean(self):
        if self.quantity_requested is None or Decimal(self.quantity_requested) <= 0:
            raise ValidationError({"quantity_requested": "Количество должно быть больше 0."})

        if self.cart_id and self.cart.status != AgentRequestCart.Status.DRAFT:
            raise ValidationError({"cart": "Нельзя редактировать позиции, когда заявка не в черновике."})

        if self.cart_id and self.product_id:
            if self.cart.company_id and self.product.company_id != self.cart.company_id:
                raise ValidationError({"product": "Товар другой компании."})
            if self.cart.branch_id and self.product.branch_id not in (None, self.cart.branch_id):
                raise ValidationError({"product": "Товар другого филиала."})
            if self.cart.warehouse_id and self.product.warehouse_id != self.cart.warehouse_id:
                raise ValidationError({"product": "Товар должен принадлежать выбранному складу."})

            if self.cart.status == AgentRequestCart.Status.DRAFT:
                need = q_qty(Decimal(self.quantity_requested or 0))
                available = self.cart.warehouse_available_qty(self.product, exclude_item_id=self.pk)
                if need > available:
                    raise ValidationError({
                        "quantity_requested": (
                            f"Недостаточно на складе для {self.product.name}: "
                            f"запрошено {need}, доступно {available}."
                        )
                    })

    def save(self, *args, **kwargs):
        if self.cart_id:
            if not self.company_id:
                self.company_id = self.cart.company_id
            if self.branch_id is None:
                self.branch_id = self.cart.branch_id
        self.full_clean()
        super().save(*args, **kwargs)


class AgentReturnCart(BaseModelId, BaseModelDate, BaseModelCompanyBranch):
    """
    Возврат товара от агента на склад компании.
    """
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        SUBMITTED = "submitted", "Отправлено владельцу"
        APPROVED = "approved", "Принято на склад"
        REJECTED = "rejected", "Отклонено"

    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="warehouse_agent_return_carts",
        verbose_name="Агент",
    )
    warehouse = models.ForeignKey(
        "warehouse.Warehouse",
        on_delete=models.PROTECT,
        related_name="agent_return_carts",
        verbose_name="Склад",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    note = models.CharField(max_length=255, blank=True, verbose_name="Комментарий агента")

    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="warehouse_approved_agent_return_carts",
        verbose_name="Кем принято",
    )

    class Meta:
        verbose_name = "Возврат агента (склад)"
        verbose_name_plural = "Возвраты агентов (склад)"
        ordering = ["-created_date"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "branch", "status"]),
            models.Index(fields=["agent", "status"]),
        ]

    def __str__(self):
        return f"Возврат {self.id} от {getattr(self.agent, 'username', self.agent_id)} [{self.get_status_display()}]"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})
        if self.warehouse_id and self.company_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "Склад принадлежит другой компании."})
        if self.branch_id and self.warehouse_id and self.warehouse.branch_id not in (None, self.branch_id):
            raise ValidationError({"warehouse": "Склад другого филиала."})
        agent_company_id = getattr(self.agent, "company_id", None)
        if self.agent_id and self.company_id and agent_company_id is not None and agent_company_id != self.company_id:
            raise ValidationError({"agent": "Агент принадлежит другой компании."})

    def is_editable(self) -> bool:
        return self.status == self.Status.DRAFT

    def _pending_return_qty(self, product_id, exclude_item_id=None):
        qs = AgentReturnItem.objects.filter(
            cart__agent_id=self.agent_id,
            cart__warehouse_id=self.warehouse_id,
            cart__status=self.Status.SUBMITTED,
            product_id=product_id,
        )
        if self.pk:
            qs = qs.exclude(cart_id=self.pk)
        if exclude_item_id:
            qs = qs.exclude(pk=exclude_item_id)
        return q_qty(qs.aggregate(total=Sum("quantity_returned"))["total"] or Decimal("0"))

    def _agent_available_qty(self, product, exclude_item_id=None):
        stock = AgentStockBalance.objects.filter(
            agent_id=self.agent_id,
            warehouse_id=self.warehouse_id,
            product_id=product.pk,
        ).first()
        cur = q_qty(Decimal(getattr(stock, "qty", None) or 0))
        pending = self._pending_return_qty(product.pk, exclude_item_id=exclude_item_id)
        return max(cur - pending, Decimal("0.000"))

    def _transfer_items_from_agent_to_warehouse(self):
        from apps.warehouse import stock as stock_service

        for it in self.items.select_related("product"):
            prod = it.product
            return_qty = q_qty(Decimal(it.quantity_returned or 0))
            if return_qty <= 0:
                continue

            stock = AgentStockBalance.objects.select_for_update().filter(
                agent=self.agent,
                warehouse=self.warehouse,
                product=prod,
            ).first()
            cur_agent_qty = q_qty(Decimal(getattr(stock, "qty", None) or 0))
            if cur_agent_qty < return_qty:
                raise ValidationError({
                    "items": f"Недостаточно у агента для {prod.name}: нужно {return_qty}, доступно {cur_agent_qty}."
                })

            stock.qty = cur_agent_qty - return_qty
            stock.save(update_fields=["qty"])

            stock_service.apply_stock_delta(
                warehouse=self.warehouse,
                product=prod,
                delta=return_qty,
                source_kind=StockMove.SourceKind.AGENT_RETURN,
                source_id=self.pk,
            )

    @transaction.atomic
    def submit(self):
        if self.status != self.Status.DRAFT:
            raise ValidationError("Можно отправить только черновик.")
        if not self.items.exists():
            raise ValidationError("Нельзя отправить пустой возврат.")
        for it in self.items.select_related("product"):
            need = q_qty(Decimal(it.quantity_returned or 0))
            if need <= 0:
                continue
            available = self._agent_available_qty(it.product, exclude_item_id=it.pk)
            if need > available:
                raise ValidationError({
                    "items": (
                        f"Недостаточно у агента для {it.product.name}: "
                        f"нужно {need}, доступно {available}."
                    )
                })
        self.status = self.Status.SUBMITTED
        self.submitted_at = timezone.now()
        self.full_clean()
        self.save(update_fields=["status", "submitted_at", "updated_date"])

    @transaction.atomic
    def approve(self, by_user):
        if self.status != self.Status.SUBMITTED:
            raise ValidationError("Можно принять только возврат в статусе 'submitted'.")
        if not self.items.exists():
            raise ValidationError("Нельзя принять пустой возврат.")

        self._transfer_items_from_agent_to_warehouse()

        self.status = self.Status.APPROVED
        self.approved_at = timezone.now()
        self.approved_by = by_user
        self.full_clean()
        self.save(update_fields=["status", "approved_at", "approved_by", "updated_date"])

    @transaction.atomic
    def reject(self, by_user):
        if self.status != self.Status.SUBMITTED:
            raise ValidationError("Можно отклонить только возврат в статусе 'submitted'.")
        self.status = self.Status.REJECTED
        self.approved_at = timezone.now()
        self.approved_by = by_user
        self.full_clean()
        self.save(update_fields=["status", "approved_at", "approved_by", "updated_date"])

    def _validate_items_against_agent_stock(self):
        totals = {}
        products = {}
        for it in self.items.select_related("product"):
            pid = it.product_id
            totals[pid] = totals.get(pid, Decimal("0.000")) + q_qty(Decimal(it.quantity_returned or 0))
            products[pid] = it.product
        for pid, need in totals.items():
            if need <= 0:
                continue
            prod = products[pid]
            available = self._agent_available_qty(prod)
            if need > available:
                raise ValidationError({
                    "items": (
                        f"Недостаточно у агента для {prod.name}: "
                        f"запрошено {need}, доступно {available}."
                    )
                })

    @transaction.atomic
    def receive_by_owner(self, by_user):
        """Владелец принимает возврат от агента без заявки агента."""
        if self.status != self.Status.DRAFT:
            raise ValidationError("Можно принять возврат только из черновика.")
        if not self.items.exists():
            raise ValidationError("Нельзя принять пустой возврат.")

        self._validate_items_against_agent_stock()
        self._transfer_items_from_agent_to_warehouse()

        now = timezone.now()
        self.status = self.Status.APPROVED
        self.submitted_at = now
        self.approved_at = now
        self.approved_by = by_user
        self.full_clean()
        self.save(update_fields=["status", "submitted_at", "approved_at", "approved_by", "updated_date"])


class AgentReturnItem(BaseModelId, BaseModelDate, BaseModelCompanyBranch):
    cart = models.ForeignKey(
        AgentReturnCart,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Возврат",
    )
    product = models.ForeignKey(
        "warehouse.WarehouseProduct",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_return_items",
        verbose_name="Товар",
    )
    quantity_returned = models.DecimalField(max_digits=18, decimal_places=3, verbose_name="К возврату")

    class Meta:
        verbose_name = "Позиция возврата агента (склад)"
        verbose_name_plural = "Позиции возвратов агента (склад)"
        indexes = [
            models.Index(fields=["cart", "product"]),
        ]

    def __str__(self):
        return f"{self.cart_id} · {self.product_id} · {self.quantity_returned}"

    def clean(self):
        if self.quantity_returned is None or Decimal(self.quantity_returned) <= 0:
            raise ValidationError({"quantity_returned": "Количество должно быть больше 0."})

        if self.cart_id and self.cart.status != AgentReturnCart.Status.DRAFT:
            raise ValidationError({"cart": "Нельзя редактировать позиции, когда возврат не в черновике."})

        if self.cart_id and self.product_id:
            if self.cart.company_id and self.product.company_id != self.cart.company_id:
                raise ValidationError({"product": "Товар другой компании."})
            if self.cart.branch_id and self.product.branch_id not in (None, self.cart.branch_id):
                raise ValidationError({"product": "Товар другого филиала."})
            if self.cart.warehouse_id and self.product.warehouse_id != self.cart.warehouse_id:
                raise ValidationError({"product": "Товар должен принадлежать выбранному складу."})

            need = q_qty(Decimal(self.quantity_returned or 0))
            available = self.cart._agent_available_qty(self.product, exclude_item_id=self.pk)
            if need > available:
                raise ValidationError({
                    "quantity_returned": (
                        f"Недостаточно у агента для {self.product.name}: "
                        f"нужно {need}, доступно {available}."
                    )
                })

    def save(self, *args, **kwargs):
        if self.cart_id:
            if not self.company_id:
                self.company_id = self.cart.company_id
            if self.branch_id is None:
                self.branch_id = self.cart.branch_id
        self.full_clean()
        super().save(*args, **kwargs)


class StockMove(models.Model):
    """Движение товара. Каждое движение — приход или расход."""

    class MoveKind(models.TextChoices):
        RECEIPT = "RECEIPT", "Приход"
        EXPENSE = "EXPENSE", "Расход"

    class SourceKind(models.TextChoices):
        DOCUMENT = "document", "Документ"
        AGENT_ISSUE = "agent_issue", "Выдача агенту"
        AGENT_RETURN = "agent_return", "Возврат от агента"
        OPENING = "opening", "Начальный остаток"
        ADJUSTMENT = "adjustment", "Корректировка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Движение без документа (выдача/возврат агента, начальный остаток) — document=NULL,
    # источник в source_kind/source_id. SET_NULL: удаление документа не стирает историю,
    # иначе нарушится инвариант StockBalance.qty == Σ StockMove.qty_delta.
    document = models.ForeignKey(
        Document, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="moves", verbose_name="Документ",
    )
    source_kind = models.CharField(
        max_length=16,
        choices=SourceKind.choices,
        default=SourceKind.DOCUMENT,
        verbose_name="Источник движения",
    )
    source_id = models.UUIDField(
        null=True, blank=True, verbose_name="ID источника",
        help_text="Документ (для сторно и отвязанных движений), заявка/возврат агента и т.п.",
    )
    warehouse = models.ForeignKey("warehouse.Warehouse", on_delete=models.CASCADE, verbose_name="Склад")
    product = models.ForeignKey("warehouse.WarehouseProduct", on_delete=models.CASCADE, verbose_name="Товар")
    qty_delta = models.DecimalField(max_digits=18, decimal_places=3, verbose_name="Изменение количества")
    move_kind = models.CharField(
        max_length=16,
        choices=MoveKind.choices,
        verbose_name="Вид движения",
        help_text="Приход (увеличение остатка) или Расход (уменьшение остатка)",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")

    class Meta:
        verbose_name = "Движение товара"
        verbose_name_plural = "Движения товаров"
        indexes = [
            models.Index(fields=["warehouse", "product", "created_at"]),
            models.Index(fields=["document", "move_kind"]),
            models.Index(fields=["source_kind", "source_id"]),
        ]

    def __str__(self):
        if self.document_id:
            src = self.document.number or self.document_id
        else:
            src = f"{self.source_kind}:{self.source_id}" if self.source_id else self.source_kind
        return f"Move {src} {self.product} {self.qty_delta} @ {self.warehouse}"


# -----------------------
# Cash register (касса) and money documents
# -----------------------


class CashRegister(BaseModelId, BaseModelCompanyBranch):
    """
    Касса — место учёта наличных. Сюда попадают приходы и расходы (MoneyDocument).
    """

    name = models.CharField(max_length=128, verbose_name="Название")
    location = models.TextField(blank=True, verbose_name="Расположение")

    class Meta:
        verbose_name = "Касса"
        verbose_name_plural = "Кассы"
        indexes = [
            models.Index(fields=["company", "branch"]),
        ]

    def __str__(self):
        return self.name or str(self.id)

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})


class PaymentCategory(BaseModelId, BaseModelCompanyBranch):
    """
    Категория платежа для денежных документов (приход/расход).

    Системные категории (system_code задан) создаются автоматически для компании/филиала
    и не редактируются через API: «Продажа», «Долги».
    """

    class SystemCode(models.TextChoices):
        SALE = "sale", "Продажа"
        DEBT = "debt", "Долги"
        INCASSATION = "incassation", "Инкассация"
        PURCHASE = "purchase", "Закупка"
        SALE_RETURN = "sale_return", "Возврат покупателю"
        PURCHASE_RETURN = "purchase_return", "Возврат от поставщика"
        SALARY = "salary", "Зарплата"

    title = models.CharField(max_length=255, verbose_name="Название")
    system_code = models.CharField(
        max_length=16,
        choices=SystemCode.choices,
        null=True,
        blank=True,
        verbose_name="Системный код",
        help_text="Если задан — встроенная категория (нельзя удалить или переименовать через API).",
    )

    class Meta:
        verbose_name = "Категория платежа"
        verbose_name_plural = "Категории платежей"
        constraints = [
            models.UniqueConstraint(
                fields=("branch", "title"),
                name="uq_wh_payment_category_title_per_branch",
                condition=models.Q(branch__isnull=False),
            ),
            models.UniqueConstraint(
                fields=("company", "title"),
                name="uq_wh_payment_category_title_global_per_company",
                condition=models.Q(branch__isnull=True),
            ),
            models.UniqueConstraint(
                fields=("company", "branch", "system_code"),
                name="uq_wh_payment_category_system_code_per_scope",
                condition=models.Q(system_code__isnull=False),
            ),
        ]
        indexes = [
            models.Index(fields=["company", "title"]),
            models.Index(fields=["company", "branch", "title"]),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})


class CashApprovalRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Ожидает решения"
        APPROVED = "APPROVED", "Подтверждено"
        REJECTED = "REJECTED", "Отклонено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.OneToOneField(
        "warehouse.Document",
        on_delete=models.CASCADE,
        related_name="cash_request",
        verbose_name="Складской документ",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, verbose_name="Статус")
    requires_money = models.BooleanField(default=False, verbose_name="Нужно создавать денежный документ")
    money_doc_type = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        verbose_name="Тип денежного документа",
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Сумма")
    decision_note = models.TextField(blank=True, verbose_name="Комментарий решения")
    requested_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата запроса")
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата решения")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_cash_decisions",
        verbose_name="Кем решено",
    )
    money_document = models.OneToOneField(
        "warehouse.MoneyDocument",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_request",
        verbose_name="Созданный денежный документ",
    )

    class Meta:
        verbose_name = "Запрос на проведение в кассе"
        verbose_name_plural = "Запросы на проведение в кассе"
        indexes = [
            models.Index(fields=["status", "requested_at"]),
        ]

    def __str__(self):
        return f"{self.document_id} [{self.status}]"


class MoneyDocument(BaseModelCompanyBranch):
    """
    Денежные документы (приходы и расходы по кассе):
    - MONEY_RECEIPT: приход денег в кассу от контрагента
    - MONEY_EXPENSE: расход денег из кассы контрагенту

    В отличие от товарных документов, здесь нет items и нет StockMove.
    """

    class DocType(models.TextChoices):
        MONEY_RECEIPT = "MONEY_RECEIPT", "Приход"
        MONEY_EXPENSE = "MONEY_EXPENSE", "Расход"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Черновик"
        POSTED = "POSTED", "Проведен"
        REJECTED = "REJECTED", "Отказан"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    doc_type = models.CharField(max_length=32, choices=DocType.choices, verbose_name="Тип документа")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, verbose_name="Статус")
    number = models.CharField(max_length=64, unique=True, null=True, blank=True, verbose_name="Номер")
    date = models.DateTimeField(
        default=timezone.now,
        verbose_name="Дата",
        help_text="Операционная дата документа (для кассовых отчётов за день). "
                  "Задаётся пользователем; по умолчанию — текущий момент. "
                  "Не путать с created_at (момент создания записи).",
    )

    cash_register = models.ForeignKey(
        "warehouse.CashRegister",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="money_documents",
        verbose_name="Касса",
    )

    warehouse = models.ForeignKey(
        "warehouse.Warehouse",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="money_documents",
        verbose_name="Счёт (склад, устаревшее)",
    )

    counterparty = models.ForeignKey(
        "warehouse.Counterparty",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="money_documents",
        verbose_name="Контрагент",
    )

    payment_category = models.ForeignKey(
        "warehouse.PaymentCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="money_documents",
        verbose_name="Категория платежа",
    )

    source_document = models.OneToOneField(
        "warehouse.Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="money_document",
        verbose_name="Основание (складской документ)",
        help_text="Если документ создан автоматически из складского документа — здесь ссылка на него.",
    )

    payment_method = models.CharField(
        max_length=16,
        choices=Document.PaymentMethod.choices,
        default=Document.PaymentMethod.CASH,
        blank=True,
        null=True,
        verbose_name="Форма оплаты",
        help_text="Наличными или безналичными. Для фильтрации операций на кассе.",
    )

    amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Сумма")
    comment = models.TextField(blank=True, verbose_name="Комментарий")
    # QA B05: приход, созданный миграцией по старой оплаченной продаже. Закрывает долг
    # контрагента, но НЕ меняет остаток кассы (деньги уже учтены раньше).
    is_migration = models.BooleanField(
        default=False, db_index=True, verbose_name="Миграционный (не влияет на кассу)",
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        verbose_name = "Денежный документ"
        verbose_name_plural = "Денежные документы"
        indexes = [
            models.Index(fields=["doc_type", "status", "date"]),
            models.Index(fields=["counterparty", "date"]),
            models.Index(fields=["cash_register", "date"]),
            models.Index(fields=["warehouse", "date"]),
            models.Index(fields=["payment_category", "date"]),
            models.Index(fields=["cash_register", "payment_method", "date"]),
        ]

    def __str__(self):
        return f"{self.number or self.id} ({self.doc_type})"

    def clean(self):
        super().clean()
        # cash_register is required for money operations (касса)
        if not self.cash_register_id and not self.warehouse_id:
            raise ValidationError({"cash_register": "Укажите кассу."})
        if self.cash_register_id and self.company_id and self.cash_register.company_id != self.company_id:
            raise ValidationError({"cash_register": "Касса принадлежит другой компании."})

        if self.doc_type in (self.DocType.MONEY_RECEIPT, self.DocType.MONEY_EXPENSE):
            # Категория обязательна только для денежных документов, созданных вручную (без складского основания).
            if not self.payment_category_id and not self.source_document_id:
                raise ValidationError({"payment_category": "Укажите категорию платежа."})

        if self.amount is None or Decimal(self.amount) <= 0:
            raise ValidationError({"amount": "Сумма должна быть больше 0."})

        if self.company_id and self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})

        if self.warehouse_id:
            wh = self.warehouse
            if wh and self.company_id and wh.company_id != self.company_id:
                raise ValidationError({"warehouse": "Склад принадлежит другой компании."})


# -----------------------
# Sales summaries (Сводки продаж)
# -----------------------


class WarehouseSalesSummary(BaseModelId, BaseModelCompanyBranch):
    """
    Сводка продаж — снапшот накладных продаж (SALE) за конкретную дату по складу,
    опционально по выбранным агентам. Снапшот фиксируется при создании
    (`documents`, `products`, `totals`), чтобы документ был воспроизводим.
    Пересобрать снапшот можно через regenerate.
    """

    class Type(models.TextChoices):
        GENERAL = "general", "Общая сводка"
        BY_AGENTS = "by_agents", "Сводка агентов"

    number = models.CharField(
        max_length=32, blank=True, null=True, db_index=True,
        verbose_name="Номер", help_text="Человекочитаемый номер (СВ-000123), генерируется бэком.",
    )
    name = models.CharField(max_length=255, verbose_name="Название")
    comment = models.TextField(blank=True, default="", verbose_name="Комментарий")
    date = models.DateField(verbose_name="Дата сводки", help_text="День, за который собраны накладные.")
    type = models.CharField(
        max_length=16, choices=Type.choices, default=Type.GENERAL, verbose_name="Тип",
    )
    warehouse = models.ForeignKey(
        "warehouse.Warehouse",
        on_delete=models.CASCADE,
        related_name="sales_summaries",
        null=True,
        blank=True,
        verbose_name="Склад (основной)",
        help_text="Первый из складов сводки. Оставлен для совместимости; полный набор — в warehouses.",
    )
    warehouses = models.ManyToManyField(
        "warehouse.Warehouse",
        blank=True,
        related_name="sales_summaries_multi",
        verbose_name="Склады",
        help_text="Склады, накладные которых входят в сводку (если all_warehouses=false).",
    )
    all_warehouses = models.BooleanField(
        default=False,
        verbose_name="По всем складам",
        help_text="Если включено — в сводку попадают накладные всех складов компании за дату.",
    )
    agents = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="warehouse_sales_summaries",
        verbose_name="Агенты",
        help_text="Выбранные агенты (только для type=by_agents).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_sales_summaries_created",
        verbose_name="Автор",
    )

    # Снапшот-итоги (totals)
    documents_count = models.PositiveIntegerField(default=0, verbose_name="Кол-во накладных")
    products_count = models.PositiveIntegerField(default=0, verbose_name="Кол-во позиций")
    total_quantity = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Итого количество")
    total_weight = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Итого вес")
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Итого сумма")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        verbose_name = "Сводка продаж"
        verbose_name_plural = "Сводки продаж"
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["company", "date"]),
            models.Index(fields=["company", "type"]),
            models.Index(fields=["company", "created_by"]),
            models.Index(fields=["company", "name"]),
            models.Index(fields=["company", "warehouse", "date"]),
        ]

    def __str__(self):
        return f"{self.number or self.id} ({self.name})"

    def warehouse_ids(self):
        """
        Итоговый набор складов сводки:
        - all_warehouses=true → все склады компании;
        - иначе из M2M warehouses, с откатом на legacy-FK warehouse (для старых записей).
        """
        if self.all_warehouses:
            return list(
                Warehouse.objects.filter(company_id=self.company_id).values_list("id", flat=True)
            )
        ids = list(self.warehouses.values_list("id", flat=True))
        if not ids and self.warehouse_id:
            ids = [self.warehouse_id]
        return ids

    def clean(self):
        if self.warehouse_id and self.company_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "Склад принадлежит другой компании."})
        if self.branch_id and self.company_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})


class WarehouseSalesSummaryDocument(models.Model):
    """Снапшот накладной, вошедшей в сводку (значения зафиксированы на момент сборки)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    summary = models.ForeignKey(
        WarehouseSalesSummary, on_delete=models.CASCADE, related_name="documents", verbose_name="Сводка",
    )
    document = models.ForeignKey(
        "warehouse.Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Исходная накладная",
    )
    number = models.CharField(max_length=64, blank=True, default="", verbose_name="Номер")
    date = models.DateField(null=True, blank=True, verbose_name="Дата")
    agent = models.CharField(max_length=255, blank=True, default="", verbose_name="Агент")
    client = models.CharField(max_length=255, blank=True, default="", verbose_name="Клиент")
    address = models.CharField(max_length=255, blank=True, default="", verbose_name="Адрес")
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Количество")
    weight = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Вес")
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Сумма")

    class Meta:
        verbose_name = "Накладная в сводке"
        verbose_name_plural = "Накладные в сводке"
        ordering = ["number"]
        indexes = [models.Index(fields=["summary"])]

    def __str__(self):
        return f"{self.number} ({self.summary_id})"


class WarehouseSalesSummaryProduct(models.Model):
    """Строка агрегированной товарной таблицы сводки (GROUP BY номенклатура + единица + цена)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    summary = models.ForeignKey(
        WarehouseSalesSummary, on_delete=models.CASCADE, related_name="products", verbose_name="Сводка",
    )
    product = models.ForeignKey(
        "warehouse.WarehouseProduct",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Исходный товар",
    )
    name = models.CharField(max_length=255, blank=True, default="", verbose_name="Название")
    unit = models.CharField(max_length=32, blank=True, default="", verbose_name="Единица")
    packages = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Упаковок")
    per_package = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="В упаковке")
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Количество")
    price = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Цена")
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Сумма")
    weight = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Вес")

    class Meta:
        verbose_name = "Товар в сводке"
        verbose_name_plural = "Товары в сводке"
        ordering = ["name"]
        indexes = [models.Index(fields=["summary"])]

    def __str__(self):
        return f"{self.name} ({self.summary_id})"


class WarehouseSalesSummaryDocumentItem(models.Model):
    """
    Снапшот позиции конкретной накладной в сводке (детализация по накладным для PDF).
    В отличие от WarehouseSalesSummaryProduct (агрегат по всем накладным),
    хранит строки отдельной накладной как есть на момент сборки снапшота.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    summary_document = models.ForeignKey(
        WarehouseSalesSummaryDocument, on_delete=models.CASCADE,
        related_name="items", verbose_name="Накладная в сводке",
    )
    name = models.CharField(max_length=255, blank=True, default="", verbose_name="Наименование")
    unit = models.CharField(max_length=32, blank=True, default="", verbose_name="Единица")
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Количество")
    price = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Цена (до скидки)")
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"), verbose_name="Скидка, %")
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Скидка, сумма")
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), verbose_name="Сумма (со скидкой)")
    weight = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"), verbose_name="Вес")

    class Meta:
        verbose_name = "Позиция накладной в сводке"
        verbose_name_plural = "Позиции накладных в сводке"
        ordering = ["name"]
        indexes = [models.Index(fields=["summary_document"])]

    def __str__(self):
        return f"{self.name} ({self.summary_document_id})"


# ─────────────────────────────────────────────────────────────
# Зарплата агентов: процент с продаж по складам
# ─────────────────────────────────────────────────────────────
class WarehouseSalaryRate(models.Model):
    """
    Ставка вознаграждения агента для конкретного склада-источника.
    Отдельно для розничных и оптовых продаж. Отсутствие записи = нули.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company", on_delete=models.CASCADE,
        related_name="warehouse_salary_rates", verbose_name="Компания",
    )
    warehouse = models.OneToOneField(
        "warehouse.Warehouse", on_delete=models.CASCADE,
        related_name="salary_rate", verbose_name="Склад",
    )
    retail_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        verbose_name="Процент с розницы",
    )
    wholesale_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        verbose_name="Процент с опта",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Кем обновлено",
    )

    class Meta:
        verbose_name = "Ставка зарплаты по складу"
        verbose_name_plural = "Ставки зарплаты по складам"
        indexes = [models.Index(fields=["company"])]

    def __str__(self):
        return f"SalaryRate {self.warehouse_id}: retail={self.retail_percent} wholesale={self.wholesale_percent}"

    def clean(self):
        for field in ("retail_percent", "wholesale_percent"):
            val = getattr(self, field, None)
            if val is None:
                continue
            if val < Decimal("0") or val > Decimal("100"):
                raise ValidationError({field: "Процент должен быть от 0 до 100"})
        if self.warehouse_id and self.company_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "Склад принадлежит другой компании."})

    def percent_for(self, *, is_wholesale: bool) -> Decimal:
        return Decimal(self.wholesale_percent if is_wholesale else self.retail_percent or Decimal("0.00"))


class AgentSalaryAccrual(models.Model):
    """
    Начисление вознаграждения агенту за продажу со склада-источника.
    percent — снимок ставки в момент продажи; amount = sale_amount * percent / 100.
    """
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает оплаты (долг)"
        ACCRUED = "accrued", "Начислено (к выплате)"
        PAID = "paid", "Выплачено"
        CANCELED = "canceled", "Отменено"

    class SaleType(models.TextChoices):
        RETAIL = "retail", "Розница"
        WHOLESALE = "wholesale", "Опт"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company", on_delete=models.CASCADE,
        related_name="agent_salary_accruals", verbose_name="Компания",
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="warehouse_salary_accruals", verbose_name="Агент",
    )
    sale = models.ForeignKey(
        "warehouse.Document", on_delete=models.CASCADE,
        related_name="salary_accruals", verbose_name="Продажа (документ)",
    )
    warehouse = models.ForeignKey(
        "warehouse.Warehouse", on_delete=models.PROTECT,
        related_name="salary_accruals", verbose_name="Склад-источник",
    )
    sale_type = models.CharField(max_length=16, choices=SaleType.choices, verbose_name="Тип продажи")
    sale_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="База начисления")
    percent = models.DecimalField(max_digits=5, decimal_places=2, verbose_name="Ставка (снимок), %")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Сумма начисления")
    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.ACCRUED, db_index=True, verbose_name="Статус",
    )
    is_correction = models.BooleanField(
        default=False, verbose_name="Корректирующее начисление",
        help_text="True — запись-корректировка (напр. отрицательная при возврате уже выплаченного).",
    )
    payout = models.ForeignKey(
        "warehouse.AgentSalaryPayout", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="accruals", verbose_name="Выплата",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Создано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        verbose_name = "Начисление зарплаты агента"
        verbose_name_plural = "Начисления зарплаты агентов"
        ordering = ["-created_at"]
        constraints = [
            # Идемпотентность: одно активное базовое начисление на пару
            # (продажа, склад-источник). Отменённые (canceled) слот не занимают —
            # это позволяет пересоздать начисление после распроведения/повторного
            # проведения документа. Корректирующие записи не ограничиваем.
            models.UniqueConstraint(
                fields=["sale", "warehouse"],
                condition=Q(is_correction=False) & ~Q(status="canceled"),
                name="uq_agent_salary_accrual_sale_warehouse_base",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "agent", "status"]),
            models.Index(fields=["company", "status"]),
            models.Index(fields=["agent", "status"]),
            models.Index(fields=["company", "created_at"]),
        ]

    def __str__(self):
        return f"Accrual {self.agent_id} {self.amount} [{self.status}]"


class AgentSalaryPayout(models.Model):
    """
    Выплата агенту. Закрывает начисления в статусе accrued по FIFO.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "users.Company", on_delete=models.CASCADE,
        related_name="agent_salary_payouts", verbose_name="Компания",
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="warehouse_salary_payouts", verbose_name="Агент",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Сумма выплаты")
    comment = models.CharField(max_length=255, blank=True, default="", verbose_name="Комментарий")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="+", verbose_name="Кем создано",
    )
    # Выплата из кассы: проведённый MONEY_EXPENSE (категория «Зарплата»). NULL — выплата вне кассы.
    money_document = models.OneToOneField(
        "warehouse.MoneyDocument", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="salary_payout", verbose_name="Денежный документ (касса)",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Создано")

    class Meta:
        verbose_name = "Выплата зарплаты агенту"
        verbose_name_plural = "Выплаты зарплаты агентам"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "agent", "created_at"]),
        ]

    def __str__(self):
        return f"Payout {self.agent_id} {self.amount}"


class WarehouseCashConfirmationSettings(BaseModelId, BaseModelDate):
    company = models.OneToOneField(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="warehouse_cash_confirmation",
        verbose_name="Компания",
    )
    enabled = models.BooleanField(
        default=False,
        verbose_name="Требовать подтверждение наличных операций",
    )

    class Meta:
        verbose_name = "Настройка подтверждения кассы склада"
        verbose_name_plural = "Настройки подтверждения кассы склада"

    def __str__(self):
        return f"WarehouseCashConfirmationSettings({self.company_id}: enabled={self.enabled})"

class WarehouseAccountingSettings(BaseModelId, BaseModelDate):
    """
    Учётные настройки склада компании (QA B07).

    closed_until — дата закрытия периода: документы с датой <= closed_until нельзя
    проводить, отменять, редактировать и удалять без права can_post_closed_period.
    """

    company = models.OneToOneField(
        "users.Company",
        on_delete=models.CASCADE,
        related_name="warehouse_accounting_settings",
        verbose_name="Компания",
    )
    closed_until = models.DateField(null=True, blank=True, verbose_name="Период закрыт по")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        verbose_name="Кто изменил",
    )

    class Meta:
        verbose_name = "Учётные настройки склада"
        verbose_name_plural = "Учётные настройки склада"

    def __str__(self):
        return f"{self.company_id}: закрыт по {self.closed_until or '—'}"
