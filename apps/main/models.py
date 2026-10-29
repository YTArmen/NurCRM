from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from datetime import timedelta
from dateutil.relativedelta import relativedelta
from django.utils.dateparse import parse_date, parse_datetime
from django.db import transaction, connection
from django.db.models import Sum, F, Q, Max, IntegerField, Value, Case, When
from mptt.models import MPTTModel, TreeForeignKey
import uuid, secrets
from django.core.files.base import ContentFile
from PIL import Image
from django.db.models.functions import Cast, Coalesce
import io
import logging
import json

from apps.users.models import (
    Company,
    User,
    Branch,
    SCALE_BARCODE_AMOUNT_UNIT_SOM,
)
from apps.consalting.models import ServicesConsalting
# from apps.construction.models import Department   # УДАЛЕНО: отделы больше не используются

_Q2 = Decimal("0.01")
def _money(x: Decimal) -> Decimal:
    return (x or Decimal("0")).quantize(_Q2, rounding=ROUND_HALF_UP)


def product_image_upload_to(instance, filename: str) -> str:
    # всегда сохраняем в .webp с новым именем
    return f"products/{instance.product_id}/{uuid.uuid4().hex}.webp"


# ==========================
# Contact
# ==========================
class Contact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='contacts')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_contacts',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='contacts')

    name = models.CharField(max_length=128)
    email = models.EmailField()
    phone = models.CharField(max_length=32)
    address = models.CharField(max_length=256)
    client_company = models.CharField(max_length=128)
    notes = models.TextField(blank=True, null=True)
    department = models.CharField(max_length=64, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Контакт'
        verbose_name_plural = 'Контакты'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['company', 'created_at']),
            models.Index(fields=['company', 'branch', 'created_at']),
        ]

    def __str__(self):
        return f"{self.name} ({self.client_company})"

    def clean(self):
        # branch ↔ company
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        # owner ↔ company
        owner_company_id = getattr(self.owner, "company_id", None)
        if owner_company_id and self.company_id and owner_company_id != self.company_id:
            raise ValidationError({"owner": "Сотрудник принадлежит другой компании."})


# ==========================
# Pipeline
# ==========================
class Pipeline(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='pipelines')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_pipelines',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pipelines')

    name = models.CharField(max_length=128)
    stages = models.JSONField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Воронка продаж'
        verbose_name_plural = 'Воронки продаж'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['company', 'created_at']),
            models.Index(fields=['company', 'branch', 'created_at']),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        owner_company_id = getattr(self.owner, "company_id", None)
        if owner_company_id and self.company_id and owner_company_id != self.company_id:
            raise ValidationError({"owner": "Сотрудник принадлежит другой компании."})


# ==========================
# Deal
# ==========================
class Deal(models.Model):
    STATUS_CHOICES = [
        ('lead', 'Лид'),
        ('prospect', 'Потенциальный клиент'),
        ('deal', 'Сделка в работе'),
        ('closed', 'Закрыта'),
        ('lost', 'Потеряна'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='deals')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_deals',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name='deals')
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='deals')
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_deals')

    title = models.CharField(max_length=255)
    value = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    stage = models.CharField(max_length=128)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Сделка'
        verbose_name_plural = 'Сделки'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['company', 'status']),
            models.Index(fields=['company', 'branch', 'status']),
        ]

    def __str__(self):
        return f"{self.title} ({self.status})"

    def clean(self):
        # branch ↔ company
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        # связи — та же компания
        if self.pipeline_id and self.pipeline.company_id != self.company_id:
            raise ValidationError({"pipeline": "Воронка другой компании."})
        if self.contact_id and self.contact.company_id != self.company_id:
            raise ValidationError({"contact": "Контакт другой компании."})
        if self.assigned_to_id:
            assigned_company_id = getattr(self.assigned_to, "company_id", None)
            if assigned_company_id and assigned_company_id != self.company_id:
                raise ValidationError({"assigned_to": "Сотрудник другой компании."})
        # branch согласованность: дочерние — глобальные или того же филиала
        if self.branch_id:
            if self.pipeline and self.pipeline.branch_id not in (None, self.branch_id):
                raise ValidationError({"pipeline": "Воронка другого филиала."})
            if self.contact and self.contact.branch_id not in (None, self.branch_id):
                raise ValidationError({"contact": "Контакт другого филиала."})

    def save(self, *args, **kwargs):
        if not self.company_id:
            if self.pipeline_id:
                self.company_id = self.pipeline.company_id
            elif self.contact_id:
                self.company_id = self.contact.company_id
        # если pipeline/контакт филиальные — подставим их филиал при отсутствии
        if not self.branch_id:
            self.branch_id = (
                self.pipeline.branch_id or self.contact.branch_id
                if (self.pipeline_id or self.contact_id) else None
            )
        self.full_clean()
        return super().save(*args, **kwargs)


# ==========================
# Task
# ==========================
class Task(models.Model):
    STATUS_CHOICES = [
        ('pending', 'В ожидании'),
        ('in_progress', 'В процессе'),
        ('done', 'Выполнена'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='tasks')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_tasks',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='tasks')
    deal = models.ForeignKey(Deal, on_delete=models.SET_NULL, null=True, blank=True, related_name='tasks')

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    due_date = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # --- ТЗ 26.10 Б-62 задача (каркас tz26; логика — в apps/main/tz26/) ---
    priority = models.CharField(max_length=10, default="normal", db_default="normal")  # low|normal|high
    remind_at = models.DateTimeField(null=True, blank=True)
    reminded_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_tasks")
    completed_at = models.DateTimeField(null=True, blank=True)
    report = models.TextField(blank=True, default="", db_default="")
    source = models.CharField(max_length=20, blank=True, default="", db_default="")  # web|pos|mobile|voice_ai|telegram

    class Meta:
        verbose_name = 'Задача'
        verbose_name_plural = 'Задачи'
        ordering = ['-due_date']
        indexes = [
            models.Index(fields=['company', 'status']),
            models.Index(fields=['company', 'branch', 'status']),
        ]

    def __str__(self):
        return f"{self.title} — {self.status}"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        if self.deal_id and self.deal.company_id != self.company_id:
            raise ValidationError({"deal": "Сделка другой компании."})
        if self.assigned_to_id:
            assigned_company_id = getattr(self.assigned_to, "company_id", None)
            if assigned_company_id and assigned_company_id != self.company_id:
                raise ValidationError({"assigned_to": "Сотрудник другой компании."})
        if self.branch_id and self.deal_id and self.deal.branch_id not in (None, self.branch_id):
            raise ValidationError({"deal": "Сделка другого филиала."})

    def save(self, *args, **kwargs):
        if not self.company_id and self.deal_id:
            self.company_id = self.deal.company_id
        if not self.branch_id and self.deal_id:
            self.branch_id = self.deal.branch_id
        self.full_clean()
        return super().save(*args, **kwargs)


# ==========================
# Order / OrderItem
# ==========================
class Order(models.Model):
    STATUS_CHOICES = [
        ('new', 'Новый'),
        ('pending', 'В процессе'),
        ('completed', 'Завершён'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='orders')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_orders',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    order_number = models.CharField(max_length=50)
    customer_name = models.CharField(max_length=128)
    date_ordered = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    phone = models.CharField(max_length=32)
    department = models.CharField(max_length=64)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Заказ'
        verbose_name_plural = 'Заказы'
        ordering = ['-date_ordered']
        indexes = [
            models.Index(fields=['company', 'date_ordered']),
            models.Index(fields=['company', 'branch', 'date_ordered']),
        ]

    def __str__(self):
        return f"{self.order_number} — {self.customer_name}"

    @property
    def total(self):
        return sum(item.total for item in self.items.all())

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})


class OrderItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='order_items', verbose_name='Компания')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_order_items',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items', verbose_name='Заказ')
    product = models.ForeignKey("Product", on_delete=models.PROTECT, related_name='order_items', verbose_name='Товар')
    quantity = models.PositiveIntegerField(verbose_name='Количество')
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Цена за единицу', editable=False)
    total = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='Итоговая сумма', editable=False)

    class Meta:
        verbose_name = 'Товар в заказе'
        verbose_name_plural = 'Товары в заказе'
        indexes = [
            models.Index(fields=['company']),
            models.Index(fields=['company', 'branch']),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

    def clean(self):
        if self.order_id and self.company_id and self.order.company_id != self.company_id:
            raise ValidationError({"company": "Компания позиции должна совпадать с компанией заказа."})
        if self.order_id and self.branch_id is not None and self.order.branch_id not in (None, self.branch_id):
            raise ValidationError({"branch": "Филиал позиции должен совпадать с филиалом заказа (или быть глобальным вместе с ним)."})
        if self.product_id and self.company_id and self.product.company_id != self.company_id:
            raise ValidationError({"product": "Товар принадлежит другой компании."})
        if self.quantity is not None and self.quantity < 1:
            raise ValidationError({"quantity": "Количество должно быть положительным."})

    def save(self, *args, **kwargs):
        if self.order_id:
            if not self.company_id:
                self.company_id = self.order.company_id
            if self.branch_id is None:
                self.branch_id = self.order.branch_id
        if not self.price:
            self.price = getattr(self.product, "price", None) or Decimal("0.00")
        self.total = (self.price or Decimal("0.00")) * Decimal(self.quantity or 0)
        self.full_clean()
        super().save(*args, **kwargs)


# ==========================
# Global Brand/Category/Product (без company/branch)
# ==========================
class GlobalBrand(MPTTModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128, unique=True, verbose_name='Название бренда')
    parent = TreeForeignKey('self', on_delete=models.CASCADE, null=True, blank=True,
                            related_name='children', verbose_name='Родительский бренд')

    class MPTTMeta:
        order_insertion_by = ['name']

    class Meta:
        verbose_name = 'Глобальный бренд'
        verbose_name_plural = 'Глобальные бренды'

    def __str__(self):
        return self.name


class GlobalCategory(MPTTModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128, unique=True, verbose_name='Название категории')
    parent = TreeForeignKey('self', on_delete=models.CASCADE, null=True, blank=True,
                            related_name='children', verbose_name='Родительская категория')

    class MPTTMeta:
        order_insertion_by = ['name']

    class Meta:
        verbose_name = 'Глобальная категория'
        verbose_name_plural = 'Глобальные категории'

    def __str__(self):
        return self.name


class GlobalProduct(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    barcode = models.CharField(max_length=64, blank=True, null=True, unique=True)
    brand = models.ForeignKey(GlobalBrand, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    category = models.ForeignKey(GlobalCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    created_at = models.DateTimeField(auto_now_add=True)
    # ТЗ 29.10 Б-97: для выгрузки справочника пачками (?since=)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = "Глобальный товар"
        verbose_name_plural = "Глобальные товары"

    def __str__(self):
        return f"{self.name} ({self.barcode or 'без штрих-кода'})"


# ==========================
# ProductCategory / ProductBrand (компания/филиал)
# ==========================
class ProductCategory(MPTTModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=128, verbose_name='Название категории')
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='categories', verbose_name='Компания')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_categories',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    parent = TreeForeignKey('self', on_delete=models.CASCADE, null=True, blank=True,
                            related_name='children', verbose_name='Родительская категория')

    class MPTTMeta:
        order_insertion_by = ['name']

    class Meta:
        verbose_name = 'Категория товара'
        verbose_name_plural = 'Категории товаров'
        constraints = [
            models.UniqueConstraint(
                fields=('branch', 'name'),
                name='uq_crm_category_name_per_branch',
                condition=models.Q(branch__isnull=False),
            ),
            models.UniqueConstraint(
                fields=('company', 'name'),
                name='uq_crm_category_name_global_per_company',
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


class ProductBrand(MPTTModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=128, verbose_name='Название бренда')
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='brands', verbose_name='Компания')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_brands',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    parent = TreeForeignKey('self', on_delete=models.CASCADE, null=True, blank=True,
                            related_name='children', verbose_name='Родительский бренд')

    class MPTTMeta:
        order_insertion_by = ['name']

    class Meta:
        verbose_name = 'Бренд'
        verbose_name_plural = 'Бренды'
        constraints = [
            models.UniqueConstraint(
                fields=('branch', 'name'),
                name='uq_crm_brand_name_per_branch',
                condition=models.Q(branch__isnull=False),
            ),
            models.UniqueConstraint(
                fields=('company', 'name'),
                name='uq_crm_brand_name_global_per_company',
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


class PromoRule(models.Model):
    """
    Динамическое правило "подарка".
    Пример:
    - min_qty=20, gift_qty=1, inclusive=False  => если >20 шт -> 1 в подарок
    - min_qty=50, gift_qty=4, inclusive=True   => если >=50 шт -> 4 в подарок
    scope: либо конкретный product, либо бренд, либо категория, либо вообще все.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='promo_rules', verbose_name='Компания')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_promo_rules',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    product  = models.ForeignKey("Product", on_delete=models.CASCADE, null=True, blank=True, related_name="promo_rules")
    brand    = models.ForeignKey("ProductBrand", on_delete=models.CASCADE, null=True, blank=True, related_name="promo_rules")
    category = models.ForeignKey("ProductCategory", on_delete=models.CASCADE, null=True, blank=True, related_name="promo_rules")

    title = models.CharField(max_length=128, blank=True, default="", verbose_name="Название правила")

    min_qty   = models.PositiveIntegerField(verbose_name="Порог количества")
    gift_qty  = models.PositiveIntegerField(verbose_name="Подарок (шт)")
    inclusive = models.BooleanField(
        default=False,
        verbose_name="Включительно (≥ вместо >). Если True, то условие qty ≥ min_qty. Если False, qty > min_qty."
    )

    priority = models.IntegerField(default=0, verbose_name="Приоритет")
    active_from = models.DateField(null=True, blank=True)
    active_to   = models.DateField(null=True, blank=True)
    is_active   = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Правило подарков"
        verbose_name_plural = "Правила подарков"
        ordering = ["-priority", "-min_qty", "-id"]
        indexes = [
            models.Index(fields=["company", "branch", "is_active"]),
            models.Index(fields=["company", "product", "min_qty"]),
            models.Index(fields=["company", "brand", "min_qty"]),
            models.Index(fields=["company", "category", "min_qty"]),
        ]
        constraints = [
            # Разрешаем не более одного scоpe одновременно
            # (гарантируем на уровне clean(), это просто инфо-коммент)
        ]

    def __str__(self):
        scope = self.product or self.brand or self.category or "все товары"
        sign = "≥" if self.inclusive else ">"
        return f"{self.title or 'Промо'}: {scope} — если {sign} {self.min_qty} → +{self.gift_qty}"

    def clean(self):
        # филиал должен относиться к компании
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})

        # только один таргет: product ИЛИ brand ИЛИ category ИЛИ ни одного
        chosen = [self.product_id, self.brand_id, self.category_id]
        if sum(bool(x) for x in chosen) > 1:
            raise ValidationError("Укажите только product ИЛИ brand ИЛИ category (или ни одного).")

        # проверка компании у таргета
        for rel, name in [(self.product, "product"), (self.brand, "brand"), (self.category, "category")]:
            if rel:
                if getattr(rel, "company_id", None) != self.company_id:
                    raise ValidationError({name: "Объект принадлежит другой компании."})
                if self.branch_id and getattr(rel, "branch_id", None) not in (None, self.branch_id):
                    raise ValidationError({name: "Объект другого филиала."})

        if self.min_qty < 1:
            raise ValidationError({"min_qty": "Порог должен быть ≥ 1."})
        if self.gift_qty < 1:
            raise ValidationError({"gift_qty": "Подарок должен быть ≥ 1."})


# ==========================
# Product
# ==========================
def _pg_advisory_xact_lock_company(company_id):
    """Транзакционный advisory-lock на компанию (Postgres). На других СУБД — no-op."""
    if not company_id or connection.vendor != "postgresql":
        return
    key = int(str(company_id).replace("-", "")[:16], 16) & 0x7FFFFFFFFFFFFFFF
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s::bigint);", [key])


def assert_barcode_unique_in_company(company_id, barcode, *, exclude_product_id=None, branch_id=None):
    """Перекрёстная уникальность штрихкода в рамках компании и филиала/склада.

    Значение не должно принадлежать ДРУГОМУ товару того же филиала (или главного склада)
    ни как основной `Product.barcode`, ни как дополнительный `ProductAlternateBarcode.barcode`.
    """
    bc = (barcode or "").strip()
    if not bc or not company_id:
        return
    prod_qs = Product.objects.filter(company_id=company_id, barcode=bc)
    alt_qs = ProductAlternateBarcode.objects.filter(company_id=company_id, barcode=bc)
    if branch_id:
        prod_qs = prod_qs.filter(branch_id=branch_id)
        alt_qs = alt_qs.filter(product__branch_id=branch_id)
    else:
        prod_qs = prod_qs.filter(branch__isnull=True)
        alt_qs = alt_qs.filter(product__branch__isnull=True)

    if exclude_product_id:
        prod_qs = prod_qs.exclude(pk=exclude_product_id)
        alt_qs = alt_qs.exclude(product_id=exclude_product_id)
    other = prod_qs.only("id", "name", "code").first()
    if other is None:
        alt = alt_qs.select_related("product").only("product__id", "product__name", "product__code").first()
        other = alt.product if alt is not None else None
    if other is not None:
        # «Калькуляция», задача 3: в тексте — какой именно товар уже с этим штрихкодом.
        code = f" (код {other.code})" if getattr(other, "code", None) else ""
        raise ValidationError(
            {"barcode": f"Штрихкод «{bc}» уже есть у товара «{other.name}»{code} в этом филиале/складе."}
        )


class ProductQuerySet(models.QuerySet):
    """
    BE2-19: массовые update()/bulk_update() (остатки при продаже, приходе, возврате)
    тоже двигают updated_at — иначе касса не увидит изменение в products/list/?updated_since=.
    """

    def update(self, **kwargs):
        kwargs.setdefault("updated_at", timezone.now())
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if "updated_at" not in fields:
            now = timezone.now()
            for obj in objs:
                obj.updated_at = now
            fields = [*fields, "updated_at"]
        return super().bulk_update(objs, fields, batch_size=batch_size)


class Product(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидание"
        ACCEPTED = "accepted", "Принят"
        REJECTED = "rejected", "Отказ"
        ARCHIVED = "archived", "В архиве"
    
    class Kind(models.TextChoices):
        PRODUCT = "product", "Товар"
        SERVICE = "service", "Услуга"
        BUNDLE = "bundle", "Комплект"

    class HotkeyGroup(models.TextChoices):
        """Статические группы для быстрых клавиш POS (F1…F12)."""
        F1 = "F1", "F1"
        F2 = "F2", "F2"
        F3 = "F3", "F3"
        F4 = "F4", "F4"
        F5 = "F5", "F5"
        F6 = "F6", "F6"
        F7 = "F7", "F7"
        F8 = "F8", "F8"
        F9 = "F9", "F9"
        F10 = "F10", "F10"
        F11 = "F11", "F11"
        F12 = "F12", "F12"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="products",
        verbose_name="Компания",
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="crm_products",
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Филиал",
    )

    kind = models.CharField(
        "Тип позиции",
        max_length=16,
        choices=Kind.choices,
        default=Kind.PRODUCT,
        db_index=True,
    )

    client = models.ForeignKey(
        "Client",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
        verbose_name="Клиент",
    )
    suppliers = models.ManyToManyField(
        "Client",
        blank=True,
        related_name="supplied_products",
        verbose_name="Поставщики",
        limit_choices_to=Q(type="suppliers"),
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Создал",
    )

    # ---- Код / артикул ----
    code = models.CharField(
        "Код товара",
        max_length=16,
        blank=True,
        db_index=True,
        help_text="Автогенерация в формате 0001 внутри компании",
    )
    
    article = models.CharField("Артикул", max_length=64, blank=True)

    name = models.CharField("Название", max_length=255)
    # ТЗ Н-33: таблица размеров для публичной карточки витрины
    size_chart = models.ForeignKey("SizeChart", on_delete=models.SET_NULL, null=True, blank=True, related_name="products")
    description = models.TextField("Описание", blank=True, null=True)
    barcode = models.CharField("Штрихкод", max_length=64, null=True, blank=True)

    brand = models.ForeignKey(
        ProductBrand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Бренд",
    )
    category = models.ForeignKey(
        ProductCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Категория",
    )

    # ---- Единица и весовой товар ----
    unit = models.CharField(
        "Единица измерения",
        max_length=32,
        default="шт.",
        help_text=(
            "Вводится вручную: шт., кг, м, упак., пачка, л и т.д. "
            "Если остаток ведёте в пачках — укажите пачку/упак.; цена и закупка тогда за одну такую единицу."
        ),
    )
    is_weight = models.BooleanField(
        "Весовой товар",
        default=False,
        help_text="Если товар продаётся по весу (обычно кг)",
    )
    is_adult = models.BooleanField(
        "Товар 18+",
        default=False,
        help_text="Флаг 18+ для товаров (алкоголь, табак и т.д.)",
    )


    quantity = models.DecimalField(
        "Количество/Остаток",
        max_digits=12,
        decimal_places=3,
        default=0,
        null=True,
        blank=True,
        help_text="В единицах unit (например пачки или шт.); поштучная продажа из пачки — через ProductPackage и sale_package на кассе.",
    )
    minimum_quantity = models.DecimalField(
        "Минимальный остаток",
        max_digits=12,
        decimal_places=3,
        default=Decimal("0"),
        null=True,
        blank=True,
        help_text="Порог для алерта «мало на складе» (фильтр preset=stock_below_min).",
    )

    # ---- Цены / наценка / скидка ----
    purchase_price = models.DecimalField(
        "Цена закупки",
        max_digits=11,
        decimal_places=3,
        default=0,
        help_text=(
            "За одну учётную единицу товара (как в поле «Количество/Остаток» и unit). "
            "Если остаток в пачках и заведена упаковка ProductPackage для поштучной продажи "
            "(например сигареты), закупка указывается за пачку; закупка за штуку на кассе = закупка пачки / quantity_in_package."
        ),
    )
    markup_percent = models.DecimalField(
        "Наценка, %",
        max_digits=12,
        decimal_places=4,
        default=0,
        help_text="Наценка в процентах к закупочной цене",
    )
    price = models.DecimalField(
        "Цена продажи",
        max_digits=10,
        decimal_places=3,
        default=0,
        help_text=(
            "За одну учётную единицу (как остаток). При продаже поштучно из пачки через sale_package "
            "цена за штуку = эта цена / quantity_in_package соответствующей упаковки. "
            "Иначе считается из закупки и наценки, если не задана вручную."
        ),
    )
    wholesale_price = models.DecimalField(
        "Цена оптовой продажи",
        max_digits=10,
        decimal_places=3,
        default=0,
        help_text="Оптовая цена продажи за одну учётную единицу (как остаток).",
    )
    discount_percent = models.DecimalField(
        "Скидка, %",
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Скидка в процентах от цены продажи",
    )
    upsell_priority = models.PositiveSmallIntegerField(
        "Приоритет допродажи (0-100)",
        default=0,
        validators=[MaxValueValidator(100)],
        help_text="Приоритет товара в подсказках кассиру от 0 до 100",
    )
    upsell_excluded = models.BooleanField(
        "Исключить из допродажи",
        default=False,
        help_text="Товар никогда не предлагать в подсказках (алкоголь, табак и т.д.)",
    )
    telegram_photo_file_id = models.CharField(
        "Telegram Photo file_id",
        max_length=255,
        blank=True,
        default="",
        help_text="Кэш file_id Telegram для быстрой отправки фото без повторной загрузки",
    )

    # ---- Услуги: длительность и процент мастера ----
    duration_min = models.PositiveIntegerField(
        "Длительность услуги, мин", null=True, blank=True
    )
    performer_commission_percent = models.DecimalField(
        "Процент мастеру", max_digits=5, decimal_places=2, null=True, blank=True
    )

    # ---- ПЛУ для весов ----
    plu = models.PositiveIntegerField(
        "ПЛУ",
        blank=True,
        null=True,
        help_text="Номер ПЛУ для весов (можно не заполнять)",
    )

    # ---- Страна и прочее ----
    country = models.CharField(
        "Страна происхождения",
        max_length=64,
        blank=True,
        help_text="Например: Россия, Китай, Кыргызстан",
    )

    status = models.CharField(
        "Статус",
        max_length=16,
        choices=Status.choices,
        db_index=True,
        blank=True,
        null=True,
    )

    hotkey_group = models.CharField(
        "Группа F1–F12",
        max_length=3,
        choices=HotkeyGroup.choices,
        blank=True,
        null=True,
        db_index=True,
        help_text="Статическая группа для кассы: GET /api/main/products/list/?hotkey_group=F4",
    )

    # ✅ фикс: без null=True — «акция»: см. также ProductPromotionTier (ступени сумма → скидка %)
    stock = models.BooleanField(
        "Акционный товар",
        default=False,
        help_text="Включите и передайте promotion_rules_input: для каждой ступени — min_amount, discount_percent, при необходимости promo_quantity.",
    )

    item_make = models.ManyToManyField(
        "ItemMake",
        blank=True,
        related_name="products",
        verbose_name="Единицы товара",
    )

    date = models.DateTimeField("Дата", blank=True, null=True)

    expiration_date = models.DateField("Срок годности", null=True, blank=True)
    shelf_life_days = models.PositiveIntegerField("Срок хранения (дней)", null=True, blank=True)

    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    # --- ТЗ 26.10 Б-52, Б-57 товар (каркас tz26; логика — в apps/main/tz26/) ---
    unit_code = models.CharField("Единица (код)", max_length=8, blank=True, default="", db_default="")
    quantity_step = models.DecimalField("Шаг количества", max_digits=12, decimal_places=3, null=True, blank=True)
    min_sale_quantity = models.DecimalField("Минимум продажи", max_digits=12, decimal_places=3, null=True, blank=True)
    quantity_decimals = models.PositiveSmallIntegerField("Знаков в количестве", null=True, blank=True)
    attributes = models.JSONField("Атрибуты", default=list, blank=True)

    objects = ProductQuerySet.as_manager()

    # ---- Порядковый номер для стабильной пагинации ----
    # Монотонно возрастает в рамках компании (как code/plu), присваивается под тем же
    # advisory-lock в save(). Нужен курсорной пагинации списка товаров: created_at не
    # уникален, и при массовом создании товары с одинаковым created_at проскакивали
    # мимо курсора («исчезали» из бесконечного скролла). seq уникален и монотонен —
    # курсор по нему не пропускает и не дублирует строки.
    seq = models.BigIntegerField(
        "Порядковый номер",
        null=True,
        blank=True,
        editable=False,
        db_index=True,
        help_text="Автоинкремент внутри компании для стабильной сортировки/пагинации.",
    )

    class Meta:
        verbose_name = "Товар"
        verbose_name_plural = "Товары"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "branch", "status"]),
            models.Index(fields=["company", "hotkey_group"]),
            models.Index(fields=["company", "plu"]),
            # Оптимизация для сканирования по штрих-коду
            models.Index(fields=["company", "barcode"], name="idx_product_company_barcode"),
            models.Index(fields=["company", "branch", "barcode"], name="idx_prod_co_br_barcode"),
            # Курсорная/стабильная пагинация «сначала новые» внутри компании.
            models.Index(fields=["company", "seq"], name="idx_product_company_seq"),
            models.Index(fields=["company", "date"], name="idx_product_company_date"),
            models.Index(fields=["company", "client"], name="idx_product_company_client"),
        ]
        constraints = [
            # ✅ штрихкод уникален в рамках главного склада компании
            models.UniqueConstraint(
                fields=("company", "barcode"),
                condition=Q(barcode__isnull=False) & ~Q(barcode="") & Q(branch__isnull=True),
                name="uq_company_main_barcode_not_empty",
            ),
            # ✅ штрихкод уникален в рамках конкретного филиала компании
            models.UniqueConstraint(
                fields=("company", "branch", "barcode"),
                condition=Q(barcode__isnull=False) & ~Q(barcode="") & Q(branch__isnull=False),
                name="uq_company_branch_barcode_not_empty",
            ),
            # код товара уникален в рамках компании, если указан и не пустой
            models.UniqueConstraint(
                fields=("company", "code"),
                condition=Q(code__isnull=False) & ~Q(code=""),
                name="uq_company_code_not_empty",
            ),
            # ПЛУ уникален в рамках компании, если задан
            models.UniqueConstraint(
                fields=("company", "plu"),
                condition=Q(plu__isnull=False),
                name="uq_company_plu_not_null",
            ),
            # Порядковый номер уникален в рамках компании (гарантия для курсора)
            models.UniqueConstraint(
                fields=("company", "seq"),
                condition=Q(seq__isnull=False),
                name="uq_company_seq_not_null",
            ),
        ]

    def __str__(self):
        return self.name

    def delete(self, *args, **kwargs):
        # AN-10 / Rule 2.4: Товар не удаляется физически, если на него есть продажи, возвраты или движения:
        # переводится в archived, исторические строки сохраняют product_id.
        has_history = self.sale_items.exists()
        if not has_history:
            ret_rel = getattr(self, "return_items", None)
            if ret_rel and ret_rel.exists():
                has_history = True
        if not has_history:
            sm_rel = getattr(self, "stock_movements", None)
            if sm_rel and sm_rel.exists():
                has_history = True
        if has_history:
            # Удалённый товар освобождает PLU и штрихкоды: их можно сразу отдать другому товару
            # (в строках чеков штрихкод сохранён в barcode_snapshot).
            # Через update(), а не save(): save() выдал бы весовому товару новый PLU.
            from django.core.cache import cache

            old_plu = self.plu
            old_barcodes = [self.barcode, *self.alternate_barcodes.values_list("barcode", flat=True)]
            self.status = self.Status.ARCHIVED
            self.plu = None
            self.barcode = None
            self.updated_at = timezone.now()
            Product.objects.filter(pk=self.pk).update(
                status=self.status, plu=None, barcode=None, updated_at=self.updated_at
            )
            self.alternate_barcodes.all().delete()
            if old_plu is not None:
                cache.delete(f"product_plu:{self.company_id}:{old_plu}")
            for bc in old_barcodes:
                if (bc or "").strip():
                    cache.delete(f"product_barcode:{self.company_id}:{bc.strip()}")
            # Для кассы архивный товар — удалённый: убирает его из каталога при синхронизации.
            ProductDeletion.objects.create(company_id=self.company_id, product_id=self.pk)
            return 1, {self._meta.label: 1}
        return super().delete(*args, **kwargs)

    # ---------- Postgres advisory lock ----------
    def _pg_lock_company(self):
        """
        Защита от гонок при генерации max()+1.
        В Postgres pg_advisory_xact_lock принимает BIGINT (int8).
        """
        if not self.company_id or connection.vendor == "sqlite":
            return

        # 64-bit key (0..2^63-1)
        key = int(str(self.company_id).replace("-", "")[:16], 16)
        key = key & 0x7FFFFFFFFFFFFFFF  # чтобы точно влез в signed BIGINT

        with connection.cursor() as cur:
            # ЯВНО кастим к BIGINT, чтобы не улетало в numeric
            cur.execute("SELECT pg_advisory_xact_lock(%s::bigint);", [key])
    
    # --------- внутренние методы ---------
    def _auto_generate_plu(self):
        if not self.is_weight:
            return
        if self.plu is not None or not self.company_id:
            return

        max_plu = (
            Product.objects
            .filter(company_id=self.company_id, plu__isnull=False)
            .aggregate(m=Max("plu"))
            .get("m") or 0
        )
        self.plu = max_plu + 1

    def _auto_generate_code(self):
        if self.code or not self.company_id:
            return

        qs = (
            Product.objects
            .filter(company_id=self.company_id)
            .exclude(code__isnull=True)
            .exclude(code__exact="")
            .filter(code__regex=r"^\d+$")
            .annotate(code_int=Cast("code", IntegerField()))
        )

        last_num = qs.aggregate(max_num=Max("code_int"))["max_num"] or 0
        self.code = f"{last_num + 1:04d}"

    def _auto_generate_seq(self):
        # Монотонный порядковый номер внутри компании. Вызывается под advisory-lock
        # в save(), поэтому max()+1 не гонится. Для существующих строк проставляется
        # бэкфилл-командой backfill_product_seq.
        if self.seq is not None or not self.company_id:
            return
        max_seq = (
            Product.objects
            .filter(company_id=self.company_id, seq__isnull=False)
            .aggregate(m=Max("seq"))
            .get("m") or 0
        )
        self.seq = max_seq + 1

    def _recalc_price(self):
        """
        Б-06 (27.10.2026): цена продажи (price) — источник истины. Сервер никогда не
        пересчитывает её сам при сохранении существующего товара: раньше любой save()
        с markup_percent > 0 перезаписывал price = purchase_price × (1 + наценка), и после
        смены закупки (PATCH purchase_price, приход) касса продавала по «новой» цене
        (150→160 при наценке 30 % давало 195→205). Теперь:
          - существующий товар: price только квантуется; пересчёт из наценки — только
            если код-путь явно выставил `_recalc_price_from_markup = True`
            (PATCH {"recalc_price": true}, строка прихода с recalc_price);
          - новый товар: как раньше — если задана наценка, а цена нет, цена считается
            из закупки и наценки (так создают карточки импорт и create-by-barcode).
        """
        base = self.purchase_price or Decimal("0")
        percent = self.markup_percent or Decimal("0")

        def _q(v):
            return Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if getattr(self, "_recalc_price_from_markup", False):
            self.price = _q(base * (Decimal("1") + percent / Decimal("100")))
            return

        # Если цена задана вручную в конкретном код-пути (например create-manual),
        # не пересчитываем её из наценки, чтобы не появлялись «копейки» из-за округлений.
        if getattr(self, "_manual_price", False) and self.price is not None:
            self.price = _q(self.price)
            return

        # ✅ уважай ручную цену даже если price=0
        if self.price is not None and percent == Decimal("0"):
            self.price = _q(self.price)
            return

        if not self._state.adding:
            # Существующий товар: цену не трогаем.
            if self.price is not None:
                self.price = _q(self.price)
            return

        self.price = _q(base * (Decimal("1") + percent / Decimal("100")))

    def clean(self):
        if self.kind != self.Kind.PRODUCT:
            self.is_adult = False

        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})


        for rel, name in [(self.brand, "brand"), (self.category, "category"), (self.client, "client")]:
            if rel and getattr(rel, "company_id", None) != self.company_id:
                raise ValidationError({name: "Объект принадлежит другой компании."})
            if self.branch_id and rel and getattr(rel, "branch_id", None) not in (None, self.branch_id):
                raise ValidationError({name: "Объект другого филиала."})

        if self.discount_percent is not None and not (Decimal("0") <= self.discount_percent <= Decimal("100")):
            raise ValidationError({"discount_percent": "Скидка должна быть от 0 до 100%."})

    def save(self, *args, **kwargs):
        # Инвалидация кэша при изменении barcode или plu
        old_barcode = None
        old_plu = None
        old_code = None
        if self.pk:
            try:
                old_instance = Product.objects.get(pk=self.pk)
                old_barcode = old_instance.barcode
                old_plu = old_instance.plu
                old_code = old_instance.code
            except Product.DoesNotExist:
                pass
        
        self._recalc_price()
        with transaction.atomic():
            self._pg_lock_company()
            self._auto_generate_code()
            self._auto_generate_plu()
            self._auto_generate_seq()
            # Перекрёстная уникальность ШК: значение не должно принадлежать другому
            # товару ни как основной, ни как дополнительный код (под advisory-lock).
            # Проверяем только при создании/смене ШК, чтобы не блокировать
            # редактирование легаси-товаров с уже существующей коллизией.
            if self.barcode and self.barcode != old_barcode:
                assert_barcode_unique_in_company(
                    self.company_id, self.barcode, exclude_product_id=self.pk, branch_id=self.branch_id
                )
            if kwargs.get("update_fields") is not None:
                update_fields = set(kwargs["update_fields"])
                if self.seq is not None:
                    update_fields.add("seq")
                if self.code is not None:
                    update_fields.add("code")
                if self.plu is not None:
                    update_fields.add("plu")
                # Б-46 (27.10.2026): auto_now не пишется в БД, если поля нет в update_fields.
                # Любое частичное сохранение товара двигает updated_at — иначе кассы,
                # следящие за каталогом по updated_at, не узнают об изменении.
                update_fields.add("updated_at")
                kwargs["update_fields"] = list(update_fields)
            super().save(*args, **kwargs)
            
        # Инвалидация кэша после сохранения
        from django.core.cache import cache
        if old_barcode and old_barcode != self.barcode:
            cache.delete(f"product_barcode:{self.company_id}:{old_barcode}")
        if self.barcode:
            cache.delete(f"product_barcode:{self.company_id}:{self.barcode}")
        if old_plu and old_plu != self.plu:
            cache.delete(f"product_plu:{self.company_id}:{old_plu}")
        if self.plu:
            cache.delete(f"product_plu:{self.company_id}:{self.plu}")
        if old_code and old_code != self.code:
            for code_value in {str(old_code), str(old_code).zfill(4) if str(old_code).isdigit() else None}:
                if code_value:
                    cache.delete(f"product_code:{self.company_id}:{code_value}")
        if self.code:
            code_str = str(self.code)
            cache.delete(f"product_code:{self.company_id}:{code_str}")
            if code_str.isdigit():
                cache.delete(f"product_code:{self.company_id}:{code_str.zfill(4)}")


class ProductExpiryBatch(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активна"
        CONSUMED = "consumed", "Израсходована"
        EXPIRED = "expired", "Просрочена"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="product_expiry_batches")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="expiry_batches")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    remaining_quantity = models.DecimalField(max_digits=12, decimal_places=3)
    received_at = models.DateTimeField()
    expires_at = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    source_kind = models.CharField(max_length=32)
    source_id = models.CharField(max_length=64, null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["expires_at", "received_at"]
        indexes = [
            models.Index(fields=["product", "status", "expires_at"]),
            models.Index(fields=["company", "expires_at"]),
        ]


class ProductFavorite(models.Model):
    """
    Избранное компании для товаров (один набор на компанию, видят все сотрудники).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="favorite_products",
        db_index=True,
    )
    product = models.ForeignKey(
        "main.Product",
        on_delete=models.CASCADE,
        related_name="favorites",
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "product"],
                name="uq_product_favorite_company_product",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["product", "created_at"]),
        ]


class ProductPromotionTier(models.Model):
    """
    Ступени акции для товара (галочка «Акционный товар» на Product.stock):
    от какой суммы строки в чеке — какая скидка в %, опционально лимит количества единиц по этой ступени.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        "Product",
        on_delete=models.CASCADE,
        related_name="promotion_tiers",
        verbose_name="Товар",
    )
    position = models.PositiveSmallIntegerField("Порядок", default=0)
    min_amount = models.DecimalField(
        "Сумма позиции от",
        max_digits=14,
        decimal_places=2,
        help_text="Минимальная сумма строки (цена × количество), с которой действует скидка.",
    )
    discount_percent = models.DecimalField("Скидка, %", max_digits=5, decimal_places=2)
    promo_quantity = models.PositiveIntegerField(
        "Лимит по акции, шт.",
        null=True,
        blank=True,
        help_text="Сколько единиц товара по этой ступени; пусто — без ограничения.",
    )

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "Ступень акции товара"
        verbose_name_plural = "Ступени акции товара"
        indexes = [
            models.Index(fields=["product", "position"]),
        ]

    def __str__(self):
        return f"{self.product_id}: от {self.min_amount} → {self.discount_percent}%"

    def clean(self):
        if self.min_amount is not None and self.min_amount < 0:
            raise ValidationError({"min_amount": "Сумма не может быть отрицательной."})
        dp = self.discount_percent
        if dp is None or dp <= 0 or dp > 100:
            raise ValidationError({"discount_percent": "Скидка должна быть от 0.01 до 100%."})
        if self.promo_quantity is not None and self.promo_quantity < 1:
            raise ValidationError({"promo_quantity": "Лимит должен быть ≥ 1."})


class ProductAlternateBarcode(models.Model):
    """
    Дополнительные штрихкоды маркет-товара (Product): поиск на кассе и в API как по основному коду.
    Уникальность значения в рамках компании (как у основного barcode у Product).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        "Product",
        on_delete=models.CASCADE,
        related_name="alternate_barcodes",
        verbose_name="Товар",
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="product_alternate_barcodes",
        verbose_name="Компания",
    )
    barcode = models.CharField("Штрихкод", max_length=64)
    name = models.CharField("Название / Описание", max_length=255, blank=True, default="")
    quantity = models.PositiveIntegerField("Количество в упаковке", null=True, blank=True)

    class Meta:
        verbose_name = "Дополнительный штрихкод товара"
        verbose_name_plural = "Дополнительные штрихкоды товара"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "barcode"),
                name="uq_main_paltbc_co_bc",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "barcode"], name="main_paltbc_co_bc"),
            models.Index(fields=["product", "barcode"], name="main_paltbc_pr_bc"),
        ]

    def save(self, *args, **kwargs):
        if self.product_id and not self.company_id:
            p = getattr(self, "product", None)
            self.company_id = getattr(p, "company_id", None) or Product.objects.filter(
                pk=self.product_id
            ).values_list("company_id", flat=True).first()
        with transaction.atomic():
            _pg_advisory_xact_lock_company(self.company_id)
            product_branch_id = None
            if self.product_id:
                p = getattr(self, "product", None)
                product_branch_id = getattr(p, "branch_id", None) or Product.objects.filter(
                    pk=self.product_id
                ).values_list("branch_id", flat=True).first()
            assert_barcode_unique_in_company(
                self.company_id, self.barcode, exclude_product_id=self.product_id, branch_id=product_branch_id
            )
            super().save(*args, **kwargs)
        from django.core.cache import cache

        if self.company_id and (self.barcode or "").strip():
            cache.delete(f"product_barcode:{self.company_id}:{self.barcode.strip()}")

    def delete(self, *args, **kwargs):
        cid, bc = self.company_id, (self.barcode or "").strip()
        super().delete(*args, **kwargs)
        from django.core.cache import cache

        if cid and bc:
            cache.delete(f"product_barcode:{cid}:{bc}")


class ProductCharacteristics(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="product_characteristics",
        verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="crm_product_characteristics",
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Филиал",
    )

    product = models.OneToOneField(
        "Product",
        on_delete=models.CASCADE,
        related_name="characteristics",
        verbose_name="Товар",
    )

    height_cm = models.DecimalField(
        "Высота, см",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
    )
    width_cm = models.DecimalField(
        "Ширина, см",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
    )
    depth_cm = models.DecimalField(
        "Глубина, см",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
    )
    factual_weight_kg = models.DecimalField(
        "Фактический вес, кг",
        max_digits=8,
        decimal_places=3,
        null=True,
        blank=True,
    )
    description = models.TextField(
        "Описание",
        blank=True,
    )

    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Характеристики товара"
        verbose_name_plural = "Характеристики товара"

    def __str__(self):
        return f"Характеристики: {self.product}"

    def clean(self):
        if self.product_id:
            # компания / филиал должны совпадать с товаром
            if self.company_id and self.product.company_id != self.company_id:
                raise ValidationError({"company": "Компания должна совпадать с компанией товара."})
            if self.branch_id is not None and self.product.branch_id != self.branch_id:
                raise ValidationError({"branch": "Филиал должен совпадать с филиалом товара (оба None или одинаковые)."})

    def save(self, *args, **kwargs):
        # если не указали company/branch — подставляем из товара
        if self.product_id:
            if not self.company_id:
                self.company_id = self.product.company_id
            if self.branch_id is None:
                self.branch_id = self.product.branch_id
        super().save(*args, **kwargs)


class ProductPackage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="product_packages",
        verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="crm_product_packages",
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Филиал",
    )
    
    product = models.ForeignKey(
        "Product",
        on_delete=models.CASCADE,
        related_name="packages",
        verbose_name="Товар",
    )

    name = models.CharField(
        "Упаковка",
        max_length=64,
        help_text="Например: коробка, пачка, блок, рулон",
    )

    quantity_in_package = models.DecimalField(
        "Количество в упаковке",
        max_digits=10,
        decimal_places=3,
        help_text="Сколько базовых единиц в одной упаковке",
    )

    unit = models.CharField(
        "Ед. изм.",
        max_length=32,
        blank=True,
        help_text="Если пусто — берём единицу товара",
    )

    piece_unit_price = models.DecimalField(
        "Цена за штуку (поштучно)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Розничная цена одной штуки при продаже через sale_package на кассе. "
            "Если задано — используется вместо product.price / quantity_in_package."
        ),
    )

    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Упаковка товара"
        verbose_name_plural = "Упаковки товара"

    def __str__(self):
        return f"{self.name}: {self.quantity_in_package} {self.unit or self.product.unit}"

    def clean(self):
        if self.quantity_in_package is not None and self.quantity_in_package <= 0:
            raise ValidationError(
                {"quantity_in_package": "Количество в упаковке должно быть больше 0."}
            )
        if self.piece_unit_price is not None and Decimal(str(self.piece_unit_price)) < 0:
            raise ValidationError({"piece_unit_price": "Цена за штуку не может быть отрицательной."})

        if self.product_id:
            if self.company_id and self.product.company_id != self.company_id:
                raise ValidationError({"company": "Компания должна совпадать с компанией товара."})
            if self.branch_id is not None and self.product.branch_id != self.branch_id:
                raise ValidationError({"branch": "Филиал должен совпадать с филиалом товара (оба None или одинаковые)."})

    def save(self, *args, **kwargs):
        # автоподстановка company/branch из товара, если не заданы
        if self.product_id:
            if not self.company_id:
                self.company_id = self.product.company_id
            if self.branch_id is None:
                self.branch_id = self.product.branch_id
            # если unit не указали — наследуем от товара
            if not self.unit:
                self.unit = self.product.unit

        super().save(*args, **kwargs)

class ProductImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="product_images", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="crm_product_images",
        null=True, blank=True, db_index=True, verbose_name="Филиал"
    )
    product = models.ForeignKey("Product", on_delete=models.CASCADE, related_name="images", verbose_name="Товар")

    image = models.ImageField(upload_to=product_image_upload_to, null=True, blank=True, verbose_name="Изображение (WebP)")
    alt = models.CharField(max_length=255, blank=True, verbose_name="Alt-текст")
    is_primary = models.BooleanField(default=False, verbose_name="Основное изображение")
    # ТЗ Н-26: фото конкретного цвета (название цвета варианта), пусто = общее фото
    color = models.CharField("Цвет", max_length=64, blank=True, default="", db_default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Фото товара"
        verbose_name_plural = "Фото товара"
        constraints = [
            # не более одного основного снимка на продукт
            models.UniqueConstraint(
                fields=("product",),
                condition=models.Q(is_primary=True),
                name="uq_primary_product_image",
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
        # company/branch должны совпадать с продуктом
        if self.product_id:
            if self.company_id and self.product.company_id != self.company_id:
                raise ValidationError({"company": "Компания изображения должна совпадать с компанией товара."})
            if self.branch_id is not None and self.product.branch_id not in (None, self.branch_id):
                raise ValidationError({"branch": "Филиал изображения должен совпадать с филиалом товара (или быть глобальным вместе с ним)."})

    def save(self, *args, **kwargs):
        # ВАЖНО: уникальный constraint разрешает только одну is_primary=True на продукт.
        # Поэтому при попытке сохранить новую/обновлённую primary-картинку
        # нужно СНАЧАЛА снять primary со старой, иначе INSERT/UPDATE упадёт с IntegrityError.
        if self.product_id and self.is_primary:
            (type(self).objects
                .filter(product_id=self.product_id, is_primary=True)
                .exclude(pk=self.pk)
                .update(is_primary=False))

        # Подставим company/branch от продукта если не заданы
        if self.product_id:
            if not self.company_id:
                self.company_id = self.product.company_id
            if self.branch_id is None:
                self.branch_id = self.product.branch_id

        # Если загружен файл (любой формат) — преобразуем в WebP и перезапишем self.image
        if self.image and hasattr(self.image, "file"):
            try:
                self.image = self._convert_to_webp(self.image)
            except Exception as e:
                raise ValidationError({"image": f"Не удалось конвертировать в WebP: {e}"})

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        storage = self.image.storage if self.image else None
        name = self.image.name if self.image else None
        super().delete(*args, **kwargs)
        # удалим файл из хранилища
        if storage and name and storage.exists(name):
            storage.delete(name)

    @staticmethod
    def _convert_to_webp(field_file) -> ContentFile:
        """
        Принимает загруженный файл любого поддерживаемого PIL формата,
        возвращает ContentFile с webp и корректным именем.
        """
        field_file.seek(0)
        im = Image.open(field_file)

        # для WebP нужен RGB
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")

        buf = io.BytesIO()
        # quality 80 / method 6 — хорошее качество и компрессия
        im.save(buf, format="WEBP", quality=80, method=6)
        buf.seek(0)

        content = ContentFile(buf.read())
        # новое имя с webp-расширением
        content.name = f"{uuid.uuid4().hex}.webp"
        return content


class ItemMake(models.Model):
    class Kind(models.TextChoices):
        RAW = "raw", "Сырьё"
        PROCESSED = "processed", "Обработанное"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="item_makes", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_item_makes',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    kind = models.CharField(
        "Тип",
        max_length=16,
        choices=Kind.choices,
        default=Kind.RAW,
        db_index=True,
    )
    source = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="processed_items",
        verbose_name="Исходное сырьё",
    )

    name = models.CharField("Название", max_length=255)
    supplier = models.ForeignKey(
        "main.Client",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="item_makes",
        verbose_name="Поставщик",
    )
    price = models.DecimalField("Цена", max_digits=10, decimal_places=2, default=0)
    unit = models.CharField("Единица измерения", max_length=50)
    quantity = models.DecimalField("Количество", max_digits=18, decimal_places=3, default=Decimal("0.000"))
    needs_processing = models.BooleanField(
        "Нуждается в обработке",
        default=False,
        db_index=True,
        help_text="Только для сырого сырья: если true — в рецепт нельзя, сначала /process/",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Единица товара"
        verbose_name_plural = "Единицы товаров"
        indexes = [
            models.Index(fields=["company", "name"]),
            models.Index(fields=["company", "branch", "name"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.quantity} {self.unit})"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        if self.kind == self.Kind.PROCESSED and not self.source_id:
            raise ValidationError({'source': 'Для обработанного сырья укажите исходное сырьё.'})
        if self.source_id:
            if self.source.company_id != self.company_id:
                raise ValidationError({'source': 'Исходное сырьё другой компании.'})
            if (self.source.branch_id or None) != (self.branch_id or None):
                raise ValidationError({'source': 'Исходное сырьё другого филиала.'})
        if self.kind == self.Kind.PROCESSED:
            self.needs_processing = False


class ProductRecipeItem(models.Model):
    """
    Рецепт готового товара: связь Product <-> ItemMake с нормой расхода.
    qty_per_unit — расход сырья на 1 единицу готового товара.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="recipe_items",
        verbose_name="Готовый товар",
    )
    item_make = models.ForeignKey(
        ItemMake,
        on_delete=models.PROTECT,
        related_name="recipe_usages",
        verbose_name="Сырьё",
    )
    qty_per_unit = models.DecimalField(
        "Расход на 1 ед. товара",
        max_digits=12,
        decimal_places=3,
        help_text="Количество сырья, необходимое для производства 1 единицы готового товара",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Позиция рецепта"
        verbose_name_plural = "Позиции рецептов"
        constraints = [
            models.UniqueConstraint(
                fields=("product", "item_make"),
                name="uq_product_recipe_item",
            ),
            models.CheckConstraint(
                check=models.Q(qty_per_unit__gt=0),
                name="ck_recipe_qty_per_unit_positive",
            ),
        ]

    def __str__(self):
        return f"{self.product.name} <- {self.item_make.name} x{self.qty_per_unit}"


def scale_amount_step(company) -> Decimal:
    """Шаг, которым весы печатают сумму: целые сомы либо тыйыны (2 знака)."""
    unit = getattr(company, "scale_barcode_amount_unit", None)
    return Decimal("1") if unit == SCALE_BARCODE_AMOUNT_UNIT_SOM else Decimal("0.01")


def cart_line_base(item, amount_step: Decimal) -> Decimal:
    """
    Сумма строки корзины/чека — так же, как её считают весы.

    Для весового товара весы отбрасывают дробную часть суммы: 0.170 кг × 540 сом/кг
    = 91.80, а на этикетке напечатано «91». Покупатель платит по этикетке, поэтому
    сумма весовой строки округляется вниз с тем же шагом, что и на весах
    (Company.scale_barcode_amount_unit). Цену за кг и вес это не трогает и скидкой
    не оформляется — просто сумма считается как на весах.

    Невесовые позиции считаются как раньше: цена × количество.
    """
    base = Decimal(str(item.unit_price or 0)) * Decimal(str(item.quantity or 0))
    if amount_step > 0 and getattr(getattr(item, "product", None), "is_weight", False):
        return base.quantize(amount_step, rounding=ROUND_FLOOR)
    return base


def _cart_item_promotion_line_discount(product, unit_price: Decimal, quantity: Decimal) -> Decimal:
    return _cart_item_promotion(product, unit_price, quantity)[0]


def _cart_item_promotion(product, unit_price: Decimal, quantity: Decimal):
    """
    (скидка по акции, сработавшая ступень | None) для строки корзины (Product.stock + ProductPromotionTier).
    Порог min_amount сравнивается с суммой строки unit_price × quantity.
    Выбирается ступень с наибольшим min_amount, для которого сумма строки всё ещё ≥ min_amount.
    promo_quantity ограничивает количество учётных единиц, на которые начисляется процент скидки.

    Если ступень не подошла, Cart.recalc() возвращает строке ручную скидку кассира (manual_discount).
    """
    zero = (Decimal("0.00"), None)
    if product is None or not getattr(product, "stock", False):
        return zero
    tiers = list(product.promotion_tiers.all())
    if not tiers:
        return zero
    unit_price = Decimal(str(unit_price or 0))
    quantity = Decimal(str(quantity or 0))
    gross = _money(unit_price * quantity)
    if gross <= 0:
        return zero
    tiers.sort(key=lambda t: (-(t.min_amount or Decimal("0")), t.position, str(t.id)))
    tier = None
    for t in tiers:
        if gross >= (t.min_amount or Decimal("0")):
            tier = t
            break
    if tier is None:
        return zero
    dp = tier.discount_percent or Decimal("0")
    if dp <= 0:
        return zero
    pq = tier.promo_quantity
    if pq is not None:
        cap = Decimal(int(pq))
        q_eff = quantity if quantity <= cap else cap
    else:
        q_eff = quantity
    base = unit_price * q_eff
    return _money(base * dp / Decimal("100")), tier


# ==========================
# Cart / CartItem / Sale / SaleItem / MobileScannerToken
# ==========================
class Cart(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активна"
        CHECKED_OUT = "checked_out", "Завершена"
        ABANDONED = "abandoned", "Отменена"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name="ID")

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="carts", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="crm_carts",
        null=True, blank=True, db_index=True, verbose_name="Филиал"
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="carts", verbose_name="Пользователь"
    )
    session_key = models.CharField(max_length=64, null=True, blank=True, verbose_name="Ключ сессии")

    shift = models.ForeignKey(
        "construction.CashShift",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="carts",
        db_index=True,
        verbose_name="Смена",
    )

    is_wholesale = models.BooleanField(default=False, db_index=True, verbose_name="Оптовая продажа")
    is_default = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Основная корзина смены",
        help_text="Ровно одна основная (is_default=true) open-корзина на смену и кассира.",
    )

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE, verbose_name="Статус")

    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    order_discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    # Скидка на чек в % (0–100). Если задана — пересчитывается в recalc от subtotal
    order_discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True, default=None,
        verbose_name="Скидка на чек, %",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "branch", "status"]),
            models.Index(fields=["session_key"]),
            models.Index(fields=["shift", "status"]),
            models.Index(fields=["shift", "user", "status"]),
        ]
        constraints = [
            # 1) со сменой: одна основная активная корзина на (shift,user)
            models.UniqueConstraint(
                fields=("shift", "user"),
                condition=Q(status="active") & Q(is_default=True) & Q(shift__isnull=False) & Q(user__isnull=False),
                name="uq_default_active_cart_per_shift_user",
            ),
            # 2) без смены и branch НЕ NULL
            models.UniqueConstraint(
                fields=("company", "branch", "user"),
                condition=Q(status="active") & Q(shift__isnull=True) & Q(branch__isnull=False) & Q(user__isnull=False),
                name="uq_active_cart_per_user_no_shift_branch",
            ),
            # 3) без смены и branch IS NULL
            models.UniqueConstraint(
                fields=("company", "user"),
                condition=Q(status="active") & Q(shift__isnull=True) & Q(branch__isnull=True) & Q(user__isnull=False),
                name="uq_active_cart_per_user_no_shift_global",
            ),
        ]
        verbose_name = "Корзина"
        verbose_name_plural = "Корзины"

    def _calc_tax(self, taxable_base: Decimal) -> Decimal:
        return Decimal("0.00")

    def clean(self):
        super().clean()

        if self.branch_id and self.company_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})

        if self.user_id and self.company_id:
            user_company_id = getattr(self.user, "company_id", None)
            if user_company_id and user_company_id != self.company_id:
                raise ValidationError({"user": "Пользователь другой компании."})

        if self.shift_id:
            if self.company_id and self.shift.company_id != self.company_id:
                raise ValidationError({"shift": "Смена другой компании."})
            if self.branch_id is not None and (self.shift.branch_id or None) != (self.branch_id or None):
                raise ValidationError({"shift": "Смена другого филиала."})

    # Поля, которые пишет recalc(): это вычисляемые суммы, они не участвуют
    # ни в одном UniqueConstraint и не проверяются в clean().
    _TOTALS_ONLY_UPDATE_FIELDS = frozenset(
        {"subtotal", "discount_total", "tax_total", "total", "updated_at"}
    )

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")

        # Быстрый путь для recalc(): обновляем только суммы.
        # Пропускаем full_clean()/validate_unique() (3 SELECT'а по partial-индексам)
        # и sync полей из смены — company/branch/user существующей корзины тут не меняются.
        # Это горячий путь POS (scan/add-item/start/checkout), вызывается на каждое действие.
        if update_fields is not None and set(update_fields) <= self._TOTALS_ONLY_UPDATE_FIELDS:
            return super().save(*args, **kwargs)

        touched = set()

        # shift есть → всё берём из смены
        if self.shift_id:
            if self.company_id != self.shift.company_id:
                self.company_id = self.shift.company_id
                touched.add("company")
            if (self.branch_id or None) != (self.shift.branch_id or None):
                self.branch_id = self.shift.branch_id
                touched.add("branch")
            if not self.user_id and self.shift.cashier_id:
                self.user_id = self.shift.cashier_id
                touched.add("user")
        else:
            # мягкий fallback (если вдруг создают без твоего mixin-а)
            if not self.company_id and self.user_id:
                u_company_id = getattr(self.user, "company_id", None)
                if u_company_id:
                    self.company_id = u_company_id
                    touched.add("company")
            if self.branch_id is None and self.user_id:
                u_branch_id = getattr(self.user, "branch_id", None)
                if u_branch_id:
                    self.branch_id = u_branch_id
                    touched.add("branch")

        # если нас сохраняют через update_fields=["shift"], нужно дописать принудительные поля тоже
        if update_fields is not None and touched:
            # updated_at — auto_now, но Django не обновит его без явного включения в update_fields
            kwargs["update_fields"] = list(set(update_fields) | touched | {"updated_at"})

        self.full_clean()
        return super().save(*args, **kwargs)

    def recalc(self):
        # Скидка строки (BE2-06): подошла ступень акции (Product.stock + promotion_tiers) —
        # действует акция, ручная скидка кассира не складывается; не подошла — ручная
        # скидка (manual_discount). Ручная скидка хранится отдельно и не теряется.
        items = list(
            self.items.select_related("product").prefetch_related("product__promotion_tiers")
        )
        for item in items:
            cur = _money(Decimal(str(item.line_discount or 0)))
            p = item.product
            tiers = list(p.promotion_tiers.all()) if (p is not None and getattr(p, "stock", False)) else []
            if tiers:
                promo_d, tier = _cart_item_promotion(
                    p,
                    Decimal(str(item.unit_price or 0)),
                    Decimal(str(item.quantity or 0)),
                )
                if promo_d > 0:
                    new_d, source, promo_id = _money(promo_d), CartItem.DiscountSource.PROMOTION, tier.id
                else:
                    new_d = _money(Decimal(str(item.manual_discount or 0)))
                    source, promo_id = CartItem.DiscountSource.MANUAL, None
            else:
                new_d, source, promo_id = cur, CartItem.DiscountSource.MANUAL, None
            if new_d <= 0:
                source = CartItem.DiscountSource.NONE
            if (new_d, source, promo_id) != (cur, item.discount_source, item.promotion_id):
                CartItem.objects.filter(pk=item.pk).update(
                    line_discount=new_d, discount_source=source, promotion_id=promo_id
                )
                item.line_discount, item.discount_source, item.promotion_id = new_d, source, promo_id

        # Подытог считается по цене продажи (unit_price), которую видит кассир.
        # Отклонение unit_price от каталожной product.price — это не скидка,
        # в discount_total и в чек оно попадать не должно.
        # Весовые строки округляются вниз как на весах — см. cart_line_base().
        amount_step = scale_amount_step(self.company)
        subtotal_raw = Decimal("0")
        line_discount_raw = Decimal("0")
        for item in items:
            base = cart_line_base(item, amount_step)
            disc = Decimal(str(item.line_discount or 0))
            disc = min(max(disc, Decimal("0")), base)
            subtotal_raw += base
            line_discount_raw += disc

        subtotal = _money(subtotal_raw)
        line_discount_total = _money(line_discount_raw)

        # Скидка на чек: либо % от остатка суммы чека (после скидок на товары), либо фиксированная сумма
        discountable_subtotal = max(Decimal("0"), subtotal - line_discount_total)
        order_percent = getattr(self, "order_discount_percent", None)
        if order_percent is not None and Decimal(str(order_percent)) > 0:
            requested_extra = _money(discountable_subtotal * Decimal(str(order_percent)) / Decimal("100"))
        else:
            requested_extra = _money(self.order_discount_total or Decimal("0"))
        max_extra = discountable_subtotal
        extra_discount = min(requested_extra, max_extra)

        discount_total = _money(line_discount_total + extra_discount)
        taxable_base = subtotal - discount_total
        tax_total = _money(self._calc_tax(taxable_base))
        total = _money(subtotal - discount_total + tax_total)

        # Частый случай (переключение вкладок корзин): суммы не изменились —
        # не трогаем БД, не бампим updated_at, не шлём лишний UPDATE.
        unchanged = (
            self.subtotal == subtotal
            and self.discount_total == discount_total
            and self.tax_total == tax_total
            and self.total == total
        )

        self.subtotal = subtotal
        self.discount_total = discount_total
        self.tax_total = tax_total
        self.total = total

        if unchanged:
            return

        self.save(update_fields=["subtotal", "discount_total", "tax_total", "total", "updated_at"])


class DiscountSource(models.TextChoices):
    NONE = "none", "Нет"
    MANUAL = "manual", "Ручная"
    PROMOTION = "promotion", "Акция"


class CartItem(models.Model):
    DiscountSource = DiscountSource

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="cart_items")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="crm_cart_items", null=True, blank=True, db_index=True)

    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("main.Product", null=True, blank=True, on_delete=models.SET_NULL, related_name="cart_items")
    custom_name = models.CharField(max_length=255, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("1.000"))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    # Скидка на строку (хранится отдельно от цены — можно менять цену и скидку независимо)
    line_discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    # BE2-06/07: скидка кассира хранится отдельно от итоговой line_discount,
    # источник итоговой скидки строки — акция, ручная или нет.
    manual_discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Ручная скидка кассира"
    )
    discount_source = models.CharField(
        max_length=16, choices=DiscountSource.choices, default=DiscountSource.NONE, verbose_name="Источник скидки"
    )
    promotion_id = models.UUIDField(null=True, blank=True, verbose_name="Ступень акции")
    price_manually_edited = models.BooleanField(
        default=False,
        verbose_name="Цена изменена вручную",
    )
    # Продажа поштучно из пачки: quantity — в штуках, списание остатка = quantity / quantity_in_package
    sale_package = models.ForeignKey(
        "main.ProductPackage",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="cart_items",
        verbose_name="Упаковка (поштучно)",
    )
    variant = models.ForeignKey(
        "main.ProductVariant",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="cart_items",
        verbose_name="Вариант (размер/цвет)",
    )
    performer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Мастер",
    )
    is_wholesale = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Оптовая позиция",
    )

    # --- ТЗ 26.10 Б-53, Б-55 строка корзины (каркас tz26; логика — в apps/main/tz26/) ---
    sale_unit = models.ForeignKey("main.ProductSaleUnit", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    base_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    price_source = models.CharField(max_length=40, blank=True, default="", db_default="")
    base_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "product"],
                condition=models.Q(sale_package__isnull=True, product__isnull=False, variant__isnull=True),
                name="uniq_cartitem_cart_product_pack_sale",
            ),
            models.UniqueConstraint(
                fields=["cart", "variant"],
                condition=models.Q(variant__isnull=False),
                name="uniq_cartitem_cart_variant",
            ),
            models.UniqueConstraint(
                fields=["cart", "product", "sale_package"],
                condition=models.Q(sale_package__isnull=False),
                name="uniq_cartitem_cart_product_piece_pkg",
            ),
        ]

    def manual_discount_base(self) -> Decimal:
        """Текущая ручная скидка строки: у строк до BE2-06 она лежала только в line_discount."""
        if self.discount_source == DiscountSource.PROMOTION or self.manual_discount:
            return Decimal(str(self.manual_discount or 0))
        return Decimal(str(self.line_discount or 0))

    def clean(self):
        if self.cart_id and self.company_id and self.cart.company_id != self.company_id:
            raise ValidationError({"company": "Компания позиции должна совпадать с компанией корзины."})
        if self.cart_id and self.branch_id is not None and self.cart.branch_id not in (None, self.branch_id):
            raise ValidationError({"branch": "Филиал позиции должен совпадать с филиалом корзины."})
        if self.product_id and self.company_id and self.product.company_id != self.company_id:
            raise ValidationError({"product": "Товар принадлежит другой компании."})

        if self.sale_package_id and not self.product_id:
            raise ValidationError({"sale_package": "Поштучная продажа возможна только для строки с товаром."})
        if self.sale_package_id and self.product_id and self.sale_package.product_id != self.product_id:
            raise ValidationError({"sale_package": "Упаковка не относится к этому товару."})
        if self.sale_package_id and self.company_id and self.sale_package.company_id != self.company_id:
            raise ValidationError({"sale_package": "Упаковка другой компании."})

        # ✅ запрет 0 и минуса
        if self.quantity is None or Decimal(self.quantity) <= 0:
            raise ValidationError({"quantity": "Количество должно быть > 0."})

        # ✅ цена продажи не ниже закупочной, кроме случая со скидкой (со скидкой можно ниже)
        if self.product_id and self.unit_price is not None:
            line_disc = Decimal(str(getattr(self, "line_discount", None) or 0))
            if line_disc <= 0:
                purchase_price = getattr(self.product, "purchase_price", None) or Decimal("0")
                if self.sale_package_id:
                    ipp = Decimal(str(self.sale_package.quantity_in_package or 0))
                    min_unit = (purchase_price / ipp) if ipp > 0 else purchase_price
                else:
                    min_unit = purchase_price
                qty = Decimal(str(self.quantity or 1))
                effective_unit = Decimal(str(self.unit_price)) - (line_disc / qty) if qty else Decimal(str(self.unit_price))
                if effective_unit < Decimal(str(min_unit)):
                    raise ValidationError({
                        "unit_price": f"Цена продажи не может быть ниже закупочной ({min_unit}).",
                    })

    def save(self, *args, **kwargs):
        skip_full_clean = bool(kwargs.pop("skip_full_clean", False))
        if self.cart_id:
            self.company_id = self.cart.company_id
            self.branch_id = self.cart.branch_id

        if self.unit_price is None:
            if self.product_id and self.sale_package_id:
                from apps.main.pos_utils import default_unit_price_for_package

                sp = self.sale_package
                if getattr(self.cart, "is_wholesale", False):
                    raw_wholesale = getattr(self.product, "wholesale_price", None)
                    raw_retail = getattr(self.product, "price", None)
                    pack_price = (
                        Decimal(str(raw_wholesale))
                        if raw_wholesale not in (None, 0, "0")
                        else Decimal(str(raw_retail or 0))
                    )
                    ipp = Decimal(str(getattr(sp, "quantity_in_package", None) or 0))
                    self.unit_price = (pack_price / ipp) if ipp > 0 else pack_price
                else:
                    self.unit_price = default_unit_price_for_package(self.product, sp)
            else:
                # Product.price может иметь 3 знака после запятой, а unit_price — 2.
                if getattr(self.cart, "is_wholesale", False):
                    raw_wholesale = getattr(self.product, "wholesale_price", None) if self.product else None
                    raw_retail = getattr(self.product, "price", None) if self.product else None
                    self.unit_price = raw_wholesale if raw_wholesale not in (None, 0, "0") else (raw_retail or Decimal("0"))
                else:
                    self.unit_price = self.product.price if self.product else Decimal("0")
        # На всякий случай нормализуем в денежный формат (2 знака)
        self.unit_price = _money(self.unit_price)
        if hasattr(self, "line_discount"):
            self.line_discount = _money(getattr(self, "line_discount", None) or Decimal("0.00"))

        if not skip_full_clean:
            self.full_clean()
        super().save(*args, **kwargs)
        self.cart.recalc()


class CartItemDeletionLog(models.Model):
    """
    Журнал: удаление позиции из корзины POS (товар, количество, кто, время).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="cart_item_deletion_logs",
        verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="cart_item_deletion_logs",
        verbose_name="Филиал",
    )
    cart = models.ForeignKey(
        Cart,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="item_deletion_logs",
        verbose_name="Корзина",
    )
    cart_item_id = models.UUIDField(null=True, blank=True, verbose_name="ID позиции (до удаления)")
    product = models.ForeignKey(
        "main.Product",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cart_item_deletion_logs",
        verbose_name="Товар",
    )
    product_name = models.CharField("Товар (название)", max_length=255)
    quantity = models.DecimalField(
        "Количество",
        max_digits=12,
        decimal_places=3,
    )
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cart_item_deletion_logs",
        verbose_name="Кто удалил",
    )
    created_at = models.DateTimeField("Время", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Журнал: удаление из корзины"
        verbose_name_plural = "Журнал: удаления из корзины"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["company", "branch", "created_at"]),
        ]

    def __str__(self):
        return f"{self.product_name} × {self.quantity} @ {self.created_at}"


class Sale(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новый"
        PAID = "paid", "Оплачен"
        DEBT = "debt", "Долг"
        CANCELED = "canceled", "Отменён"
        PARTIALLY_RETURNED = "partially_returned", "Частичный возврат"

    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Наличные"
        TRANSFER = "transfer", "Перевод"
        DEBT = "debt", "Долг"
        MBANK = "mbank", "Мбанк"
        OPTIMA = "optima", "Оптима"
        OBANK = "obank", "Обанк"
        BAKAI = "bakai", "Бакай"
        MIXED = "mixed", "Смешанная"
        OFFSET = "offset", "Зачёт (предоплата/обмен)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="sales")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="crm_sales", null=True, blank=True, db_index=True)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="sales")

    # смена ОПЦИОНАЛЬНО
    shift = models.ForeignKey(
        "construction.CashShift",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="sales",
        db_index=True,
        verbose_name="Смена",
    )

    # касса НУЖНА если shift нет
    cashbox = models.ForeignKey(
        "construction.Cashbox",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="sales",
        db_index=True,
        verbose_name="Касса",
    )

    client = models.ForeignKey("main.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="sale")
    # --- ТЗ 26.10 Б-56, Б-67, Б-80 продажа (каркас tz26; логика — в apps/main/tz26/) ---
    client_object = models.ForeignKey("main.ClientObject", on_delete=models.SET_NULL, null=True, blank=True, related_name="sales")
    source = models.CharField("Источник", max_length=20, blank=True, default="", db_default="", db_index=True)
    device_key = models.CharField("Устройство", max_length=64, blank=True, default="", db_default="", db_index=True)
    device_name = models.CharField(max_length=64, blank=True, default="", db_default="")
    conversation_id = models.UUIDField(null=True, blank=True)


    # Консультант и комиссия от выручки
    consultant = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consultant_sales",
        verbose_name="Консультант",
    )
    consultant_commission_enabled = models.BooleanField(default=False, verbose_name="Начислять процент консультанту")
    consultant_commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True, verbose_name="Процент комиссии консультанта"
    )
    consultant_commission_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, default=Decimal("0.00"), verbose_name="Сумма комиссии консультанта"
    )

    status = models.CharField(max_length=32, choices=Status.choices, default=Status.NEW)
    doc_number = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    bonus_redeemed = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), db_default=Decimal("0.00"),
        verbose_name="Оплачено бонусами",
    )
    idempotency_key = models.CharField(
        max_length=128, null=True, blank=True, verbose_name="Ключ идемпотентности кассы"
    )

    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    payment_method = models.CharField(max_length=16, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    cash_received = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # AN-01: Продажа в долг и детализация оплат
    paid_now = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), db_default=Decimal("0.00"),
        verbose_name="Внесено сразу",
    )
    cash_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), db_default=Decimal("0.00"),
        verbose_name="Наличными",
    )
    card_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), db_default=Decimal("0.00"),
        verbose_name="Безналичными / картой",
    )
    debt_initial = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), db_default=Decimal("0.00"),
        verbose_name="Долг при оформлении",
    )
    debt_remaining = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), db_default=Decimal("0.00"),
        verbose_name="Остаток долга",
    )

    @property
    def debt_amount(self) -> Decimal:
        return self.debt_remaining or Decimal("0.00")


    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    # BE2-10: продажа сделана кассой без связи и дослана позже; created_at/paid_at — фактическое время
    # продажи, received_at — когда сервер её принял.
    is_offline = models.BooleanField(default=False, verbose_name="Продажа без связи")
    received_at = models.DateTimeField(null=True, blank=True, verbose_name="Принята сервером")

    # Фискализация eKassa (после успешной оплаты, вне транзакции чекаута)
    ekassa_fiscal = models.JSONField(
        null=True,
        blank=True,
        verbose_name="eKassa: фискальный чек",
        help_text="Статус, newid, номер ФД и служебные данные ответа API.",
    )

    class Meta:
        indexes = [
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["company", "branch", "created_at"]),
            models.Index(fields=["shift", "created_at"]),
            models.Index(fields=["cashbox", "created_at"]),
            models.Index(fields=["company", "consultant", "paid_at"]),
            models.Index(fields=["company", "doc_number"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="uniq_sale_idempotency_key",
            )
        ]

    def clean(self):
        super().clean()

        if self.branch_id and self.company_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})

        if self.user_id and self.company_id:
            user_company_id = getattr(self.user, "company_id", None)
            if user_company_id and user_company_id != self.company_id:
                raise ValidationError({"user": "Пользователь другой компании."})

        if self.consultant_id and self.company_id:
            consultant_company_id = getattr(self.consultant, "company_id", None)
            if consultant_company_id and consultant_company_id != self.company_id:
                raise ValidationError({"consultant": "Консультант другой компании."})

        if self.client_id and self.company_id and self.client.company_id != self.company_id:
            raise ValidationError({"client": "Клиент другой компании."})
        if self.branch_id and self.client_id and self.client.branch_id not in (None, self.branch_id):
            raise ValidationError({"client": "Клиент другого филиала."})

        # ✅ shift есть — строгие проверки компании/филиала + cashbox должен совпасть со сменой (если передан)
        if self.shift_id:
            if self.company_id and self.shift.company_id != self.company_id:
                raise ValidationError({"shift": "Смена другой компании."})
            if (self.branch_id or None) != (self.shift.branch_id or None):
                raise ValidationError({"shift": "Смена другого филиала."})
            if self.cashbox_id and self.cashbox_id != self.shift.cashbox_id:
                raise ValidationError({"cashbox": "Касса продажи должна совпадать с кассой смены."})

        # ✅ shift НЕТ — требуем cashbox
        else:
            if not self.cashbox_id:
                raise ValidationError({"cashbox": "Укажите кассу (если смены нет)."})
            if self.company_id and self.cashbox.company_id != self.company_id:
                raise ValidationError({"cashbox": "Касса другой компании."})
            if (self.branch_id or None) != (self.cashbox.branch_id or None):
                # branch может быть None — тогда проверка ок только если и у кассы None
                raise ValidationError({"cashbox": "Касса другого филиала."})

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        touched = set()

        # shift есть → ЖЁСТКО всё берём из смены
        if self.shift_id:
            if self.company_id != self.shift.company_id:
                self.company_id = self.shift.company_id
                touched.add("company")
            if (self.branch_id or None) != (self.shift.branch_id or None):
                self.branch_id = self.shift.branch_id
                touched.add("branch")
            if self.cashbox_id != self.shift.cashbox_id:
                self.cashbox_id = self.shift.cashbox_id  # ← важно: всегда, не только если пусто
                touched.add("cashbox")
            if not self.user_id and self.shift.cashier_id:
                self.user_id = self.shift.cashier_id
                touched.add("user")

        # shift нет → если cashbox указан, можно подтянуть company/branch (если пустые)
        elif self.cashbox_id:
            if not self.company_id:
                self.company_id = self.cashbox.company_id
                touched.add("company")
            if self.branch_id is None:
                self.branch_id = self.cashbox.branch_id
                touched.add("branch")

        if update_fields is not None and touched:
            kwargs["update_fields"] = list(set(update_fields) | touched)

        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def change(self) -> Decimal:
        cash_portion = self.cash_payment_amount()
        if cash_portion <= 0:
            return Decimal("0.00")
        diff = (self.cash_received or Decimal("0")) - cash_portion
        if diff <= 0:
            return Decimal("0.00")
        return diff.quantize(_Q2, rounding=ROUND_HALF_UP)

    def cash_payment_amount(self) -> Decimal:
        rows = list(self.payments.all()) if hasattr(self, "payments") else []
        if rows:
            return sum(
                (p.amount or Decimal("0.00") for p in rows if p.method == self.PaymentMethod.CASH),
                Decimal("0.00"),
            ).quantize(_Q2, rounding=ROUND_HALF_UP)
        if self.payment_method == self.PaymentMethod.CASH:
            return (self.total or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)
        return Decimal("0.00")

    def noncash_payment_amount(self) -> Decimal:
        rows = list(self.payments.all()) if hasattr(self, "payments") else []
        if rows:
            return sum(
                (
                    p.amount or Decimal("0.00")
                    for p in rows
                    if p.method not in (self.PaymentMethod.CASH, self.PaymentMethod.DEBT)
                ),
                Decimal("0.00"),
            ).quantize(_Q2, rounding=ROUND_HALF_UP)
        if self.payment_method in (self.PaymentMethod.DEBT, self.PaymentMethod.CASH):
            return Decimal("0.00")
        return (self.total or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)

    def payment_lines(self):
        rows = list(self.payments.all()) if hasattr(self, "payments") else []
        if rows:
            return rows
        if self.status in (self.Status.PAID, self.Status.PARTIALLY_RETURNED) and self.payment_method != self.PaymentMethod.DEBT:
            return [
                SalePayment(
                    sale=self,
                    company_id=self.company_id,
                    method=self.payment_method,
                    amount=self.total or Decimal("0.00"),
                )
            ]
        if self.status == self.Status.DEBT or self.payment_method == self.PaymentMethod.DEBT:
            lines = []
            c_val = self.cash_amount if self.cash_amount > 0 else (self.cash_received or Decimal("0.00"))
            if c_val > 0:
                lines.append(SalePayment(sale=self, company_id=self.company_id, method=self.PaymentMethod.CASH, amount=c_val))
            if (self.card_amount or Decimal("0.00")) > 0:
                lines.append(SalePayment(sale=self, company_id=self.company_id, method=self.PaymentMethod.TRANSFER, amount=self.card_amount))
            d_val = self.debt_initial if self.debt_initial > 0 else max(Decimal("0.00"), (self.total or Decimal("0.00")) - sum(l.amount for l in lines))
            if d_val > 0:
                lines.append(SalePayment(sale=self, company_id=self.company_id, method=self.PaymentMethod.DEBT, amount=d_val))
            return lines
        return []

    def _emit_paid_event(self):
        from apps.integrations.events import emit_event

        lines = self.payment_lines()
        emit_event(
            self.company_id,
            "sale.paid",
            {
                "sale": self.pk,
                "number": self.doc_number,
                "total": self.total,
                "discount_total": self.discount_total,
                "payment_method": self.payment_method,
                "payments": [{"method": p.method, "amount": p.amount} for p in lines],
                "shift": self.shift_id,
                "cashbox": self.cashbox_id,  # ТЗ 29.10 Б-102
                "cashier": self.user_id,
                "client": self.client_id,
                "paid_at": self.paid_at,
            },
        )

    def mark_paid(
        self,
        payment_method=None,
        cash_received=None,
        *,
        payments=None,
        cash_amount=None,
        card_amount=None,
        card_method=None,
        prepayment_method=None,
        skip_ekassa_schedule=False,
    ):
        if not self.doc_number and self.pk:
            from apps.main.utils_numbers import ensure_sale_doc_number

            ensure_sale_doc_number(self)

        sale_total = (self.total or Decimal("0.00")).quantize(_Q2, rounding=ROUND_HALF_UP)

        if payments:
            total_paid = sum((p.get("amount") or Decimal("0.00") for p in payments), Decimal("0.00"))
            if total_paid.quantize(_Q2, rounding=ROUND_HALF_UP) != sale_total:
                raise ValueError(
                    f"Сумма оплат ({total_paid}) не совпадает с суммой продажи ({sale_total})."
                )

            if len(payments) == 1:
                self.payment_method = payments[0]["method"]
            else:
                self.payment_method = self.PaymentMethod.MIXED

            cash_portion = sum(
                (p["amount"] for p in payments if p["method"] == self.PaymentMethod.CASH),
                Decimal("0.00"),
            )
            card_portion = sum(
                (p["amount"] for p in payments if p["method"] not in (self.PaymentMethod.CASH, self.PaymentMethod.DEBT)),
                Decimal("0.00"),
            )
            debt_portion = sum(
                (p["amount"] for p in payments if p["method"] == self.PaymentMethod.DEBT),
                Decimal("0.00"),
            )

            self.cash_amount = cash_portion
            self.card_amount = card_portion
            self.paid_now = cash_portion + card_portion
            self.debt_initial = debt_portion
            self.debt_remaining = debt_portion

            if cash_portion > 0:
                if cash_received is not None:
                    self.cash_received = cash_received
                else:
                    self.cash_received = cash_portion
                if self.cash_received < cash_portion:
                    raise ValueError("Сумма, полученная наличными, меньше наличной части оплаты.")
            else:
                self.cash_received = Decimal("0.00")

            if debt_portion > 0:
                self.status = self.Status.DEBT
                self.paid_at = None
            else:
                self.status = self.Status.PAID
                self.paid_at = timezone.now()

            self.save(update_fields=[
                "status", "paid_at", "payment_method", "cash_received",
                "paid_now", "cash_amount", "card_amount", "debt_initial", "debt_remaining"
            ])

            self.payments.all().delete()
            SalePayment.objects.bulk_create(
                [
                    SalePayment(
                        sale=self,
                        company_id=self.company_id,
                        method=line["method"],
                        amount=line["amount"],
                    )
                    for line in payments
                ]
            )
        elif payment_method is not None:
            self.payment_method = payment_method

            if self.payment_method == self.PaymentMethod.DEBT:
                self.status = self.Status.DEBT
                self.paid_at = None
                c_amt = cash_amount if cash_amount is not None else (cash_received or Decimal("0.00"))
                k_amt = card_amount or Decimal("0.00")
                self.cash_amount = c_amt
                self.card_amount = k_amt
                self.cash_received = c_amt
                self.paid_now = c_amt + k_amt
                self.debt_initial = max(Decimal("0.00"), sale_total - self.paid_now)
                self.debt_remaining = self.debt_initial
                self.save(update_fields=[
                    "status", "paid_at", "payment_method", "cash_received",
                    "paid_now", "cash_amount", "card_amount", "debt_initial", "debt_remaining"
                ])

                self.payments.all().delete()
                lines = []
                if c_amt > 0:
                    lines.append(SalePayment(sale=self, company_id=self.company_id, method=self.PaymentMethod.CASH, amount=c_amt))
                if k_amt > 0:
                    noncash_method = prepayment_method or card_method or self.PaymentMethod.TRANSFER
                    lines.append(SalePayment(sale=self, company_id=self.company_id, method=noncash_method, amount=k_amt))
                if self.debt_initial > 0:
                    lines.append(SalePayment(sale=self, company_id=self.company_id, method=self.PaymentMethod.DEBT, amount=self.debt_initial))
                if lines:
                    SalePayment.objects.bulk_create(lines)
                return

            self.paid_now = sale_total
            if self.payment_method == self.PaymentMethod.CASH:
                self.cash_amount = sale_total
                self.card_amount = Decimal("0.00")
                if cash_received is not None:
                    self.cash_received = cash_received
                else:
                    self.cash_received = sale_total
            else:
                self.cash_amount = Decimal("0.00")
                self.card_amount = sale_total
                self.cash_received = Decimal("0.00")

            self.debt_initial = Decimal("0.00")
            self.debt_remaining = Decimal("0.00")
            self.status = self.Status.PAID
            self.paid_at = timezone.now()
            self.save(update_fields=[
                "status", "paid_at", "payment_method", "cash_received",
                "paid_now", "cash_amount", "card_amount", "debt_initial", "debt_remaining"
            ])

            self.payments.all().delete()
            SalePayment.objects.create(
                sale=self,
                company_id=self.company_id,
                method=self.payment_method,
                amount=sale_total,
            )
        else:
            self.paid_now = sale_total
            if self.payment_method == self.PaymentMethod.CASH:
                self.cash_amount = sale_total
                self.card_amount = Decimal("0.00")
            else:
                self.cash_amount = Decimal("0.00")
                self.card_amount = sale_total
            self.debt_initial = Decimal("0.00")
            self.debt_remaining = Decimal("0.00")
            self.status = self.Status.PAID
            self.paid_at = timezone.now()
            self.save(update_fields=[
                "status", "paid_at", "paid_now", "cash_amount", "card_amount", "debt_initial", "debt_remaining"
            ])

        self._emit_paid_event()

        if skip_ekassa_schedule:
            return

        sale_pk = self.pk

        from apps.ekassa.runtime import schedule_after_commit
        from apps.ekassa.sale_bridge import try_fiscalize_pos_sale

        schedule_after_commit(try_fiscalize_pos_sale, sale_pk)


class SaleDocCounter(models.Model):
    """Последний выданный номер чека компании (Sale.doc_number). Строку блокируем при выдаче."""

    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="sale_doc_counter",
    )
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Счётчик номеров чеков"
        verbose_name_plural = "Счётчики номеров чеков"


class SalePayment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name="payments",
        verbose_name="Продажа",
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="sale_payments",
        verbose_name="Компания",
    )
    method = models.CharField(max_length=16, choices=Sale.PaymentMethod.choices, verbose_name="Способ оплаты")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Сумма")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["sale", "method"]),
            models.Index(fields=["company", "created_at"]),
        ]
        verbose_name = "Оплата продажи"
        verbose_name_plural = "Оплаты продажи"

    def clean(self):
        if self.sale_id and self.company_id and self.sale.company_id != self.company_id:
            raise ValidationError({"company": "Компания оплаты должна совпадать с компанией продажи."})
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Сумма должна быть > 0."})
        if self.method == Sale.PaymentMethod.DEBT:
            raise ValidationError({"method": "Долг нельзя указывать в строках оплаты."})

    def save(self, *args, **kwargs):
        if self.sale_id and not self.company_id:
            self.company_id = self.sale.company_id
        self.full_clean()
        return super().save(*args, **kwargs)


def sale_refund_split(sale, refund_method, amount):
    """
    (наличными, безналом) для суммы денег, выданной по возврату.
    Явный способ возврата (refund_method, кроме original/same) — вся сумма туда;
    иначе — в пропорции исходной оплаты без долговой части и взаимозачёта.
    """
    amount = _money(Decimal(str(amount or 0)))
    zero = Decimal("0.00")
    if amount <= 0:
        return zero, zero
    rm = str(refund_method or "").strip().lower()
    if rm and rm not in ("original", "same"):
        return (amount, zero) if rm == Sale.PaymentMethod.CASH else (zero, amount)
    skip = (Sale.PaymentMethod.DEBT, Sale.PaymentMethod.OFFSET)
    lines = [(p.method, p.amount or zero) for p in sale.payments.all() if (p.amount or zero) > 0 and p.method not in skip]
    paid = sum((a for _, a in lines), zero)
    if paid > 0:
        cash = _money(amount * sum((a for m, a in lines if m == Sale.PaymentMethod.CASH), zero) / paid)
        return cash, amount - cash
    # Строк оплат нет (старые чеки): по способу оплаты чека; долг возвращают из предоплаты наличными.
    if sale.payment_method in (Sale.PaymentMethod.CASH, Sale.PaymentMethod.DEBT, None, ""):
        return amount, zero
    return zero, amount


class SaleReturn(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name="returns",
        verbose_name="Продажа",
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="sale_returns",
        verbose_name="Компания",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_sale_returns",
        verbose_name="Кассир/пользователь",
    )
    idempotency_key = models.CharField(max_length=128, db_index=True, verbose_name="Ключ идемпотентности")
    returned_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Возвращённая сумма")
    is_defect = models.BooleanField(default=False, verbose_name="Брак")
    is_full = models.BooleanField(default=True, verbose_name="Полный возврат")
    items_payload = models.JSONField(null=True, blank=True, verbose_name="Спецификация возврата")
    response_data = models.JSONField(null=True, blank=True, verbose_name="Ответ API")
    shift = models.ForeignKey(
        "construction.CashShift",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sale_returns",
        verbose_name="Смена возврата",
    )
    returned_items = models.JSONField(null=True, blank=True, verbose_name="Возвращённые позиции")
    reason = models.CharField(max_length=255, blank=True, default="", db_default="", verbose_name="Причина")
    refund_method = models.CharField(max_length=16, blank=True, default="", verbose_name="Способ возврата денег")
    # 11: сколько денег фактически отдали по возврату — наличными и безналом
    # (по способу возврата refund_method, а не по исходной оплате). Возврат по чеку
    # в долг может не выдавать денег вовсе — тогда обе суммы 0.
    refund_cash = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Возвращено наличными"
    )
    refund_noncash = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Возвращено безналом"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата возврата")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "idempotency_key"], name="uniq_sale_return_idempotency")
        ]
        indexes = [
            models.Index(fields=["sale", "created_at"]),
        ]
        verbose_name = "Возврат продажи"
        verbose_name_plural = "Возвраты продаж"


class SaleItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="sale_items")
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="crm_sale_items",
        null=True,
        blank=True,
        db_index=True,
    )

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        "main.Product",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="sale_items",
    )

    name_snapshot = models.CharField(max_length=255)
    barcode_snapshot = models.CharField(max_length=64, null=True, blank=True)

    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("1.000"))
    # Скидка на строку (сумма). Храним отдельно от unit_price, как и в CartItem.
    line_discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    manual_discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Ручная скидка кассира"
    )
    discount_source = models.CharField(
        max_length=16, choices=DiscountSource.choices, default=DiscountSource.NONE, verbose_name="Источник скидки"
    )
    promotion_id = models.UUIDField(null=True, blank=True, verbose_name="Ступень акции")
    price_manually_edited = models.BooleanField(
        default=False,
        verbose_name="Цена изменена вручную",
    )
    is_custom = models.BooleanField(
        default=False,
        verbose_name="Доп. услуга / произвольная позиция",
        help_text="Флаг для позиций без привязки к товару из каталога (AN-10).",
    )
    is_wholesale = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Оптовая позиция",
    )

    sale_package = models.ForeignKey(
        "main.ProductPackage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sale_items",
        verbose_name="Упаковка (поштучно)",
    )
    variant = models.ForeignKey(
        "main.ProductVariant",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sale_items",
        verbose_name="Вариант (размер/цвет)",
    )
    performer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="performed_sale_items",
        verbose_name="Мастер",
    )
    performer_commission_amount = models.DecimalField(
        "Начислено мастеру",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        db_default=Decimal("0.00"),
    )

    # ✅ себестоимость единицы на момент продажи (для маржи)
    purchase_price_snapshot = models.DecimalField(
        "Себестоимость (снапшот)",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        default=None,
    )


    # --- ТЗ 26.10 Б-53, Б-55 строка продажи (каркас tz26; логика — в apps/main/tz26/) ---
    sale_unit = models.ForeignKey("main.ProductSaleUnit", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    base_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    price_source = models.CharField(max_length=40, blank=True, default="", db_default="")
    base_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["company", "sale"]),
            models.Index(fields=["company", "branch", "sale"]),
            models.Index(fields=["company", "product"]),
        ]

    def clean(self):
        if self.sale_id and self.company_id and self.sale.company_id != self.company_id:
            raise ValidationError({"company": "Компания позиции должна совпадать с компанией продажи."})

        if self.sale_id and self.branch_id is not None and self.sale.branch_id not in (None, self.branch_id):
            raise ValidationError({"branch": "Филиал позиции должен совпадать с филиалом продажи."})

        if self.product_id and self.company_id and self.product.company_id != self.company_id:
            raise ValidationError({"product": "Товар принадлежит другой компании."})

        if self.quantity is None or Decimal(str(self.quantity)) <= 0:
            raise ValidationError({"quantity": "Количество должно быть > 0."})

        if not self.product_id:
            self.is_custom = True
            if not (self.name_snapshot or "").strip():
                raise ValidationError({"name_snapshot": "Укажите название позиции (если товар не выбран)."})

        # себестоимость не может быть отрицательной
        if self.purchase_price_snapshot is not None and Decimal(str(self.purchase_price_snapshot)) < 0:
            raise ValidationError({"purchase_price_snapshot": "Себестоимость не может быть отрицательной."})

    def save(self, *args, **kwargs):
        if not self.product_id:
            self.is_custom = True

        if self.sale_id:
            self.company_id = self.sale.company_id
            self.branch_id = self.sale.branch_id

        # снапшоты — только при создании
        if self.pk is None:
            if self.product_id:
                if not (self.name_snapshot or "").strip():
                    self.name_snapshot = self.product.name

                # ✅ НЕ "not self.unit_price", а именно None
                if self.unit_price is None:
                    # Product.price может иметь 3 знака после запятой, а unit_price — 2.
                    self.unit_price = self.product.price

                if not (self.barcode_snapshot or "").strip():
                    self.barcode_snapshot = self.product.barcode

                # ✅ ключевое для маржи (за штуку при поштучной продаже из пачки)
                if self.purchase_price_snapshot is None:
                    pp = self.product.purchase_price or Decimal("0.00")
                    if self.sale_package_id:
                        ipp = Decimal(str(self.sale_package.quantity_in_package or 0))
                        self.purchase_price_snapshot = (
                            _money(pp / ipp) if ipp > 0 else _money(pp)
                        )
                    else:
                        self.purchase_price_snapshot = _money(pp)
            else:
                self.is_custom = True
                # если товар не выбран — себестоимость неизвестна
                if self.purchase_price_snapshot is None:
                    self.purchase_price_snapshot = Decimal("0.00")

        # нормализуем денежные поля перед валидацией (иначе падаем на 3-х знаках у Product.price)
        if self.unit_price is not None:
            self.unit_price = _money(self.unit_price)
        if hasattr(self, "line_discount"):
            self.line_discount = _money(getattr(self, "line_discount", None) or Decimal("0.00"))
        if self.purchase_price_snapshot is not None:
            self.purchase_price_snapshot = _money(self.purchase_price_snapshot)

        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def line_total(self) -> Decimal:
        base = Decimal(self.unit_price or 0) * Decimal(self.quantity or 0)
        disc = Decimal(getattr(self, "line_discount", None) or 0)
        return (base - disc).quantize(Decimal("0.01"))

    @property
    def amount(self) -> Decimal:
        return self.line_total

    @property
    def cost_price(self) -> Decimal:
        return self.purchase_price_snapshot or Decimal("0.00")

    @property
    def line_cogs(self) -> Decimal:
        return (Decimal(self.purchase_price_snapshot or 0) * Decimal(self.quantity or 0)).quantize(Decimal("0.01"))

    @property
    def line_profit(self) -> Decimal:
        return (self.line_total - self.line_cogs).quantize(Decimal("0.01"))
    
class MobileScannerToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name="ID")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="mobile_tokens", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_mobile_tokens',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="mobile_tokens", verbose_name="Корзина")
    token = models.CharField(max_length=64, unique=True, db_index=True, verbose_name="Токен")
    expires_at = models.DateTimeField(verbose_name="Срок действия")

    class Meta:
        verbose_name = "Мобильный токен для сканера"
        verbose_name_plural = "Мобильные токены для сканера"
        indexes = [
            models.Index(fields=['company']),
            models.Index(fields=['company', 'branch']),
        ]

    @classmethod
    def issue(cls, cart, ttl_minutes=10):
        return cls.objects.create(
            company=cart.company,
            branch=cart.branch,
            cart=cart,
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timezone.timedelta(minutes=ttl_minutes),
        )

    def is_valid(self):
        return timezone.now() <= self.expires_at

    def __str__(self):
        return f"Токен для корзины {self.cart_id} (действует до {self.expires_at})"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        if self.cart_id:
            if self.cart.company_id != self.company_id:
                raise ValidationError({'cart': 'Корзина другой компании.'})
            if self.branch_id and self.cart.branch_id not in (None, self.branch_id):
                raise ValidationError({'cart': 'Корзина другого филиала.'})


# ==========================
# Reviews / Notifications / Integrations / Analytics / Events
# ==========================
class Review(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='reviews')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_reviews',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews')

    rating = models.PositiveSmallIntegerField()
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Отзыв'
        verbose_name_plural = 'Отзывы'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['company', 'created_at']),
            models.Index(fields=['company', 'branch', 'created_at']),
        ]

    def __str__(self):
        return f"{self.user.email} — {self.rating}★"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        if self.user_id and getattr(self.user, "company_id", None) not in (None, self.company_id):
            raise ValidationError({'user': 'Пользователь другой компании.'})


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='notifications')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_notifications',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')

    message = models.TextField()
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Real-time / типизация (doc 08)
    class Level(models.TextChoices):
        INFO = "info", "Инфо"
        SUCCESS = "success", "Успех"
        WARNING = "warning", "Предупреждение"
        HIGH = "high", "Важно"
        CRITICAL = "critical", "Критично"

    class Category(models.TextChoices):
        TARIFF = "tariff", "Тариф"
        SYSTEM = "system", "Системные"
        NEWS = "news", "Новости"
        OTHER = "other", "Другое"

    category = models.CharField("Категория", max_length=40, choices=Category.choices, default=Category.OTHER, db_index=True)
    type = models.CharField("Тип события", max_length=40, default="system", db_index=True)
    title = models.CharField("Заголовок", max_length=255, blank=True, default="")
    url = models.CharField("Ссылка для перехода", max_length=512, blank=True, default="")
    level = models.CharField("Важность", max_length=16, choices=Level.choices, default=Level.INFO)
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='actor_notifications', verbose_name='Инициатор',
    )
    data = models.JSONField("Данные для UI", default=dict, blank=True)

    class Meta:
        verbose_name = 'Уведомление'
        verbose_name_plural = 'Уведомления'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['company', 'created_at']),
            models.Index(fields=['company', 'branch', 'created_at']),
            models.Index(fields=['user', 'is_read']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.user.email}: {self.message[:30]}..."

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})


class Integration(models.Model):
    TYPE_CHOICES = [
        ('telephony', 'Телефония'),
        ('messenger', 'Мессенджер'),
        ('1c', '1C'),
    ]
    STATUS_CHOICES = [
        ('active', 'Активна'),
        ('inactive', 'Неактивна'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='integrations')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_integrations',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    config = models.JSONField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='inactive')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Интеграция'
        verbose_name_plural = 'Интеграции'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['company', 'type']),
            models.Index(fields=['company', 'branch', 'type']),
        ]

    def __str__(self):
        return f"{self.type} — {self.status}"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})


class Analytics(models.Model):
    TYPE_CHOICES = [
        ('sales', 'Продажи'),
        ('activity', 'Активность'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='analytics')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_analytics',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Аналитика'
        verbose_name_plural = 'Аналитика'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['company', 'type']),
            models.Index(fields=['company', 'branch', 'type']),
        ]

    def __str__(self):
        return f"{self.type} — {self.data.get('metric', '')}"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})


class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='events')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_events',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    title = models.CharField(max_length=255)
    datetime = models.DateTimeField()
    participants = models.ManyToManyField(User, related_name='events')

    notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Событие'
        verbose_name_plural = 'События'
        ordering = ['-datetime']
        indexes = [
            models.Index(fields=['company', 'datetime']),
            models.Index(fields=['company', 'branch', 'datetime']),
        ]

    def __str__(self):
        return f"{self.title} — {self.datetime.strftime('%Y-%m-%d %H:%M')}"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})


# ==========================
# Warehouse / WarehouseEvent (CRM-локальные)
# ==========================
class Warehouse(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name='ID склада')
    name = models.CharField(max_length=255, verbose_name='Название склада')
    location = models.CharField(max_length=255, blank=True, null=True, verbose_name='Местоположение')
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='warehouses', verbose_name='Компания')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_warehouses',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата создания')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Дата обновления')

    class Meta:
        verbose_name = 'Склад'
        verbose_name_plural = 'Склады'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['company', 'name']),
            models.Index(fields=['company', 'branch', 'name']),

        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})


class WarehouseEvent(models.Model):
    STATUS_CHOICES = [
        ('draf', 'Черновик'),
        ('conducted', 'Проведен'),
        ('cancelled', 'Отменен'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name='ID события')
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name='events', verbose_name='Склад')
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='warehouse_events', verbose_name='Компания')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_warehouse_events',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    responsible_person = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                           related_name='responsible_warehouse_events', verbose_name='Ответственное лицо')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, verbose_name='Статус события')
    client_name = models.CharField(max_length=128, verbose_name='Имя клиента')
    title = models.CharField(max_length=255, verbose_name='Название события')
    description = models.TextField(blank=True, null=True, verbose_name='Описание события')
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='Сумма')
    event_date = models.DateTimeField(verbose_name='Дата события')
    participants = models.ManyToManyField(User, related_name='warehouse_events', verbose_name='Участники')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата создания события')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Дата обновления события')

    class Meta:
        verbose_name = 'Складское событие'
        verbose_name_plural = 'Складские события'
        ordering = ['event_date']
        indexes = [
            models.Index(fields=['company', 'event_date']),
            models.Index(fields=['company', 'branch', 'event_date']),
        ]

    def __str__(self):
        return f"{self.title} — {self.event_date.strftime('%Y-%m-%d %H:%M')}"

    def clean(self):
        # branch/company согласованность
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        if self.warehouse_id:
            if self.warehouse.company_id != self.company_id:
                raise ValidationError({'warehouse': 'Склад другой компании.'})
            if self.branch_id and self.warehouse.branch_id not in (None, self.branch_id):
                raise ValidationError({'warehouse': 'Склад другого филиала.'})
        if self.responsible_person_id:
            rp_company_id = getattr(self.responsible_person, "company_id", None)
            if rp_company_id and rp_company_id != self.company_id:
                raise ValidationError({"responsible_person": "Ответственный из другой компании."})


# ==========================
# Инвентаризация товаров (остаток Product.quantity)
# ==========================
class ProductInventorySession(models.Model):
    """
    Акт инвентаризации: в черновике фиксируются товары и учётные количества (quantity_fact),
    по кнопке «провести» остатки Product.quantity выравниваются под факт.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        APPLIED = "applied", "Проведено"
        CANCELED = "canceled", "Отменено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="product_inventory_sessions")
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="product_inventory_sessions",
        null=True,
        blank=True,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="product_inventory_sessions_created",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    note = models.TextField(blank=True, default="")
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Инвентаризация товаров"
        verbose_name_plural = "Инвентаризации товаров"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["company", "branch", "created_at"]),
            models.Index(fields=["company", "status"]),
        ]

    def __str__(self):
        return f"Инвентаризация {self.get_status_display()} ({self.created_at:%Y-%m-%d %H:%M})"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})


class ProductInventoryItem(models.Model):
    """Строка акта: товар и учётное количество после инвентаризации."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ProductInventorySession,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    product = models.ForeignKey(
        "Product",
        on_delete=models.CASCADE,
        related_name="inventory_lines",
    )
    quantity_before = models.DecimalField(
        "Было на момент проведения",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    quantity_fact = models.DecimalField(
        "Учётное количество (факт)",
        max_digits=12,
        decimal_places=2,
    )

    class Meta:
        verbose_name = "Строка инвентаризации товара"
        verbose_name_plural = "Строки инвентаризации товаров"
        constraints = [
            models.UniqueConstraint(fields=["session", "product"], name="uq_product_inventory_line_session_product"),
        ]
        indexes = [
            models.Index(fields=["session", "product"]),
        ]

    def clean(self):
        if self.session_id and self.product_id and self.product.company_id != self.session.company_id:
            raise ValidationError({"product": "Товар другой компании."})
        if self.session_id and self.product_id:
            sb = self.session.branch_id
            pb = self.product.branch_id
            if sb is None and pb is not None:
                raise ValidationError({"product": "В акте без филиала можно только глобальные товары (без филиала)."})
            if sb is not None and pb not in (None, sb):
                raise ValidationError({"product": "Товар другого филиала."})


# ==========================
# Client / ClientDeal / DealInstallment / Bids / SocialApplications
# ==========================
class Client(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новый"

    class StatusClient(models.TextChoices):
        CLIENT = "client", "клиент"
        SUPPLIERS = "suppliers", "Поставщики"
        IMPLEMENTERS = "implementers", "Реализаторы"
        CONTRACTOR = "contractor", "Подрядчик"

    class Sector(models.TextChoices):
        ALL = "all", "Общий"
        MARKET = "market", "Маркет"
        CONSALTING = "consalting", "Консалтинг"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name="ID клиента")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="clients", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_clients',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    sector = models.CharField("Сектор", max_length=16, choices=Sector.choices,
                            default=Sector.ALL, db_index=True, blank=True, null=True)
    type = models.CharField("Тип клиента", max_length=16, choices=StatusClient.choices,
                            default=StatusClient.CLIENT, null=True, blank=True)
    enterprise = models.CharField("Предприятие O", max_length=255, blank=True, null=True)
    full_name = models.CharField("ФИО", max_length=255)
    phone = models.CharField("Телефон", max_length=32)
    # E.164 (+996…), считается из phone в save(): поиск одного клиента кассой, приложением и по QR.
    phone_normalized = models.CharField(
        "Телефон (E.164)", max_length=20, blank=True, default="", db_default="", editable=False
    )
    email = models.EmailField("Почта", blank=True)
    date = models.DateField("Дата", null=True, blank=True)
    status = models.CharField("Статус", max_length=16, choices=Status.choices, default=Status.NEW)

    llc = models.CharField("Название компании", max_length=255, blank=True, null=True)
    inn = models.CharField("ИНН", max_length=32, blank=True, null=True)
    okpo = models.CharField("ОКПО", max_length=32, blank=True, null=True)
    score = models.CharField("Расчетный счет", max_length=64, blank=True, null=True)
    bik = models.CharField("БИК", max_length=32, blank=True, null=True)
    address = models.CharField("Адрес", max_length=255, blank=True, null=True)

    salesperson = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="clients_as_salesperson", verbose_name="Продавец")
    service = models.ForeignKey(ServicesConsalting, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="clients_using_service", verbose_name="Услуга")

    class ProvisionStatus(models.TextChoices):
        NONE = "none", "Не требуется"
        PENDING = "pending", "Ожидает оплаты"
        CREATED = "created", "Аккаунт создан"
        FAILED = "failed", "Ошибка создания"

    nur_company = models.ForeignKey(
        Company, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="consalting_clients", verbose_name="CRM-аккаунт (tenant)"
    )
    provision_status = models.CharField(
        "Статус создания CRM", max_length=16, choices=ProvisionStatus.choices, default=ProvisionStatus.NONE
    )
    provision_error = models.TextField("Ошибка создания CRM", blank=True, default="")
    provisioned_at = models.DateTimeField("Дата создания CRM", null=True, blank=True)

    telegram_chat_id = models.CharField("Telegram chat id", max_length=64, null=True, blank=True)
    bonus_balance = models.DecimalField(
        "Баланс бонусов", max_digits=12, decimal_places=2, default=Decimal("0.00"), db_default=Decimal("0.00")
    )

    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    # --- ТЗ 26.10 Б-55, Б-56, Б-74 клиент (каркас tz26; логика — в apps/main/tz26/) ---
    price_level = models.ForeignKey("main.PriceLevel", on_delete=models.SET_NULL, null=True, blank=True, related_name="clients")
    credit_limit = models.DecimalField("Лимит долга", max_digits=12, decimal_places=2, null=True, blank=True)
    default_due_days = models.PositiveIntegerField("Срок долга по умолчанию, дней", null=True, blank=True)
    client_kind = models.CharField(max_length=10, blank=True, default="", db_default="")  # person|foreman|team|company
    instagram_username = models.CharField(max_length=64, blank=True, default="", db_default="")
    whatsapp_phone = models.CharField(max_length=32, blank=True, default="", db_default="")

    class Meta:
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "phone"]),
            models.Index(fields=["company", "branch", "status"]),
            models.Index(fields=["phone_normalized"], name="idx_client_phone_norm"),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.phone})"

    def save(self, *args, **kwargs):
        from apps.main.phone_utils import normalize_phone_e164

        normalized = normalize_phone_e164(self.phone)
        if normalized != (self.phone_normalized or ""):
            self.phone_normalized = normalized
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "phone_normalized" not in update_fields:
                kwargs["update_fields"] = list(update_fields) + ["phone_normalized"]
        return super().save(*args, **kwargs)

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        if self.salesperson_id:
            sp_company_id = getattr(self.salesperson, "company_id", None)
            if sp_company_id and sp_company_id != self.company_id:
                raise ValidationError({'salesperson': 'Продавец другой компании.'})


# ==========================
# Supplier receipts (оприходования)
# ==========================
class SupplierReceipt(models.Model):
    """
    Журнал оприходований от поставщиков.
    Создаётся в SupplierReceiptAPIView (POST /api/main/suppliers/<id>/receipt/).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="supplier_receipts", db_index=True)
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="supplier_receipts",
        null=True,
        blank=True,
        db_index=True,
    )
    supplier = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="supplier_receipts",
        db_index=True,
        limit_choices_to=Q(type=Client.StatusClient.SUPPLIERS),
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_supplier_receipts",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # ТЗ Н-29: приход по заказу поставщику
    order = models.ForeignKey(
        "main.PurchaseOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="receipts",
    )
    # ТЗ Н-33: скидка (сумма; процент — если задана в %) и доп. расходы прихода
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), db_default=Decimal("0.00"))
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    extra_costs = models.JSONField(default=list, blank=True, db_default=[])

    class Meta:
        verbose_name = "Оприходование от поставщика"
        verbose_name_plural = "Оприходования от поставщика"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "supplier", "created_at"]),
            models.Index(fields=["company", "branch", "created_at"]),
        ]

    def clean(self):
        if self.supplier_id and self.company_id and self.supplier.company_id != self.company_id:
            raise ValidationError({"supplier": "Поставщик другой компании."})
        if self.branch_id and self.company_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})


class PurchaseOrder(models.Model):
    """ТЗ Н-29: заказ поставщику как документ (общий для всех касс/ПК/телефонов компании)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        SENT = "sent", "Отправлен"
        PARTIAL = "partial", "Принят частично"
        RECEIVED = "received", "Принят полностью"
        CANCELLED = "cancelled", "Отменён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="purchase_orders", db_index=True)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="purchase_orders", null=True, blank=True, db_index=True)
    number = models.PositiveIntegerField(verbose_name="Номер (по компании)")
    supplier = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="purchase_orders", db_index=True,
        limit_choices_to=Q(type=Client.StatusClient.SUPPLIERS),
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    note = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_purchase_orders")
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Заказ поставщику"
        verbose_name_plural = "Заказы поставщикам"
        ordering = ["-created_at", "-number"]
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="uniq_purchase_order_number_company")]
        indexes = [models.Index(fields=["company", "supplier", "status"])]


class PurchaseOrderLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="purchase_order_lines")
    qty = models.DecimalField(max_digits=12, decimal_places=3)
    purchase_price = models.DecimalField(max_digits=11, decimal_places=3, null=True, blank=True)
    received_qty = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))

    class Meta:
        ordering = ["id"]


class SupplierReceiptItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    receipt = models.ForeignKey(SupplierReceipt, on_delete=models.CASCADE, related_name="items", db_index=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="supplier_receipt_items", db_index=True)
    qty = models.PositiveIntegerField()
    purchase_price = models.DecimalField(max_digits=11, decimal_places=3, null=True, blank=True)
    # ТЗ Н-33: закупка за единицу с учётом скидки и доп. расходов (только если они переданы)
    landed_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)

    class Meta:
        verbose_name = "Строка оприходования"
        verbose_name_plural = "Строки оприходования"
        indexes = [
            models.Index(fields=["receipt", "product"]),
        ]

    def clean(self):
        if self.receipt_id and self.product_id:
            if self.product.company_id != self.receipt.company_id:
                raise ValidationError({"product": "Товар другой компании."})
            rb = self.receipt.branch_id
            pb = self.product.branch_id
            if rb is None and pb is not None:
                raise ValidationError({"product": "В оприходовании без филиала можно только глобальные товары (без филиала)."})
            if rb is not None and pb not in (None, rb):
                raise ValidationError({"product": "Товар другого филиала."})
            if self.receipt.supplier_id:
                if getattr(self.product, "client_id", None) == self.receipt.supplier_id:
                    return
                try:
                    if self.product.suppliers.filter(id=self.receipt.supplier_id).exists():
                        return
                except Exception:
                    pass
                raise ValidationError({"product": "Товар не принадлежит выбранному поставщику."})


class SupplierReturn(models.Model):
    """
    Документ возврата товара поставщику (Маркет).
    """

    class Reason(models.TextChoices):
        DEFECT = "defect", "Брак"
        SURPLUS = "surplus", "Излишек"
        WRONG_ITEM = "wrong_item", "Ошибка поставки"
        EXPIRED = "expired", "Просрочка"
        OTHER = "other", "Другое"

    class Compensation(models.TextChoices):
        CASH = "cash", "Приход в кассу"
        DEBT_OFFSET = "debt_offset", "Списание долга"
        NONE = "none", "Без движения денег"

    class Status(models.TextChoices):
        POSTED = "posted", "Проведён"
        CANCELLED = "cancelled", "Отменён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="supplier_returns", db_index=True)
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="supplier_returns",
        null=True,
        blank=True,
        db_index=True,
    )
    supplier = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="supplier_returns",
        db_index=True,
        limit_choices_to=Q(type=Client.StatusClient.SUPPLIERS),
    )
    receipt = models.ForeignKey(
        SupplierReceipt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returns",
        db_index=True,
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_supplier_returns",
    )
    reason = models.CharField(max_length=32, choices=Reason.choices, default=Reason.DEFECT, db_index=True)
    comment = models.TextField(blank=True)
    compensation = models.CharField(max_length=32, choices=Compensation.choices, default=Compensation.NONE, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.POSTED, db_index=True)
    cashbox = models.ForeignKey("construction.Cashbox", on_delete=models.SET_NULL, null=True, blank=True, related_name="supplier_returns")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Возврат поставщику"
        verbose_name_plural = "Возвраты поставщику"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "supplier", "created_at"]),
            models.Index(fields=["company", "branch", "created_at"]),
        ]

    def clean(self):
        if self.supplier_id and self.company_id and self.supplier.company_id != self.company_id:
            raise ValidationError({"supplier": "Поставщик другой компании."})
        if self.branch_id and self.company_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})
        if self.receipt_id and self.supplier_id and self.receipt.supplier_id != self.supplier_id:
            raise ValidationError({"receipt": "Приход принадлежит другому поставщику."})


class SupplierReturnItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    supplier_return = models.ForeignKey(SupplierReturn, on_delete=models.CASCADE, related_name="items", db_index=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="supplier_return_items", db_index=True)
    receipt_item = models.ForeignKey(SupplierReceiptItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="returns", db_index=True)
    qty = models.DecimalField(max_digits=12, decimal_places=3)
    purchase_price = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)

    class Meta:
        verbose_name = "Строка возврата поставщику"
        verbose_name_plural = "Строки возврата поставщику"
        indexes = [
            models.Index(fields=["supplier_return", "product"]),
            models.Index(fields=["receipt_item"]),
        ]


class MarketProductFormLayout(models.Model):
    """
    Раскладка формы создания/редактирования товара (Маркет).
    Одна запись на компанию (singleton).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        related_name="product_form_layout",
        verbose_name="Компания",
    )
    hidden = models.JSONField("Скрытые блоки формы", default=list, blank=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_product_form_layouts",
        verbose_name="Кто обновил",
    )

    class Meta:
        verbose_name = "Раскладка формы товара"
        verbose_name_plural = "Раскладки форм товаров"

    def __str__(self):
        return f"FormLayout ({getattr(self.company, 'name', self.company_id)})"


class ClientDeal(models.Model):
    class Kind(models.TextChoices):
        AMOUNT = "amount", "Сумма договора"
        SALE = "sale", "Продажа"
        DEBT = "debt", "Долг"
        PREPAYMENT = "prepayment", "Предоплата"
        CANCELED = "canceled", "Отменён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="client_deals",
        verbose_name="Компания",
        db_index=True,
                null=True,
        blank=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="crm_client_deals",
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Филиал",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="deals",
        verbose_name="Клиент",
        db_index=True,
    )

    title = models.CharField("Название сделки", max_length=255)
    kind = models.CharField(
        "Тип сделки",
        max_length=16,
        choices=Kind.choices,
        default=Kind.SALE,
        db_index=True,
    )

    amount = models.DecimalField("Сумма договора", max_digits=12, decimal_places=2, default=0)
    prepayment = models.DecimalField("Предоплата", max_digits=12, decimal_places=2, default=0)

    debt_days = models.PositiveSmallIntegerField(
        "Срок (дн.)",
        blank=True,
        null=True,
        db_column="debt_months",
    )
    schedule_version = models.CharField(
        "Версия графика",
        max_length=8,
        default="v2",
        choices=[("v1", "v1"), ("v2", "v2")],
    )

    debt_months = models.PositiveSmallIntegerField("Срок (мес.)", blank=True, null=True, db_column="debt_months_v2")
    interval_days = models.PositiveSmallIntegerField("Интервал (дни)", default=1, blank=True, null=True)
    interval_months = models.PositiveSmallIntegerField("Интервал (месяцы)", default=1, blank=True, null=True)
    sale = models.ForeignKey(
        "Sale",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deals",
        verbose_name="Связанная продажа",
    )
    first_due_date = models.DateField("Первая дата оплаты", blank=True, null=True)

    auto_schedule = models.BooleanField(
        "Автоматический график",
        default=True,
        help_text="Если выключено — график не пересобирается автоматически.",
    )

    note = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Сделка"
        verbose_name_plural = "Сделки"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "client"]),
            models.Index(fields=["company", "branch", "kind"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(amount__gte=0) & Q(prepayment__gte=0),
                name="clientdeal_amount_prepayment_non_negative",
            ),
            models.CheckConstraint(
                check=Q(prepayment__lte=F("amount")),
                name="clientdeal_prepayment_lte_amount",
            ),
        ]

    # ===== computed =====
    @property
    def debt_amount(self) -> Decimal:
        return (self.amount or Decimal("0")) - (self.prepayment or Decimal("0"))

    @property
    def paid_total(self) -> Decimal:
        s = self.installments.aggregate(s=Sum("paid_amount"))["s"] or Decimal("0")
        return s.quantize(Decimal("0.01"))

    @property
    def remaining_debt(self) -> Decimal:
        return (self.debt_amount - self.paid_total).quantize(Decimal("0.01"))

    @property
    def daily_payment(self) -> Decimal:
        cnt = self.debt_days or self.debt_months
        if not cnt or cnt <= 0:
            return Decimal("0.00")
        return (self.debt_amount / Decimal(cnt)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

    # ===== validation =====
    def clean(self):
        super().clean()

        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})

        if self.client_id and self.client.company_id != self.company_id:
            raise ValidationError({"client": "Клиент другой компании."})

        # клиент может быть общий (branch=None)
        if self.branch_id and self.client_id and self.client.branch_id not in (None, self.branch_id):
            raise ValidationError({"client": "Клиент другого филиала."})

        a = self.amount or Decimal("0")
        p = self.prepayment or Decimal("0")

        if a < 0:
            raise ValidationError({"amount": "Сумма не может быть отрицательной."})
        if p < 0:
            raise ValidationError({"prepayment": "Предоплата не может быть отрицательной."})
        if p > a:
            raise ValidationError({"prepayment": "Предоплата не может превышать сумму договора."})

        # прод-аудит: если есть платежи — нельзя менять условия
        if self.pk:
            old = ClientDeal.objects.filter(pk=self.pk).values(
                "kind", "amount", "prepayment", "debt_days", "first_due_date"
            ).first()
            if old:
                has_payments = self.payments.exists()
                changed_terms = (
                    self.kind != old["kind"]
                    or (self.amount or Decimal("0")) != (old["amount"] or Decimal("0"))
                    or (self.prepayment or Decimal("0")) != (old["prepayment"] or Decimal("0"))
                    or (self.debt_days != old["debt_days"])
                    or (self.first_due_date != old["first_due_date"])
                )
                if has_payments and changed_terms and not getattr(self, "_is_return_adjustment", False):
                    raise ValidationError("Нельзя менять тип/суммы/срок/дату: по сделке уже есть платежи.")

        if self.kind == self.Kind.DEBT:
            if not self.pk and (a - p) <= 0:
                raise ValidationError({"prepayment": 'Для типа "Долг" сумма договора должна быть больше предоплаты.'})
            if (a - p) < 0:
                raise ValidationError({"prepayment": 'Предоплата не может превышать сумму договора.'})
            if self.debt_days and self.debt_months:
                raise ValidationError({"debt_months": "Нельзя одновременно указывать debt_days и debt_months."})
            if self.schedule_version == "v2":
                if not self.debt_days and not self.debt_months:
                    raise ValidationError({"debt_days": "Укажите количество платежей (debt_days или debt_months) для v2."})
            else:
                if (not self.debt_days or self.debt_days <= 0) and not self.pk:
                    raise ValidationError({"debt_days": "Укажите срок (в днях) для рассрочки."})
        else:
            self.debt_days = None
            self.debt_months = None
            self.first_due_date = None
            self.auto_schedule = False

    # ===== schedule =====
    def rebuild_installments(self, force: bool = False, custom_installments: list = None):
        if self.kind != self.Kind.DEBT:
            self.installments.all().delete()
            return

        total = self.debt_amount
        if total <= 0:
            self.installments.all().delete()
            return

        if not force and self.payments.exists():
            raise ValidationError("Нельзя пересобрать график: по сделке уже есть платежи.")

        installments_to_create = []

        if custom_installments:
            import calendar
            balance = total
            for idx, inst in enumerate(custom_installments, start=1):
                amt = Decimal(str(inst.get("amount", "0")))
                d_date = inst.get("due_date")
                if isinstance(d_date, str):
                    d_date = parse_date(d_date)
                p_date = inst.get("promised_date")
                if isinstance(p_date, str):
                    p_date = parse_date(p_date)
                balance = (balance - amt).quantize(Decimal("0.01"))
                installments_to_create.append(
                    DealInstallment(
                        id=uuid.uuid4(),
                        company=self.company,
                        branch=self.branch,
                        deal=self,
                        number=inst.get("number", idx),
                        due_date=d_date,
                        promised_date=p_date,
                        amount=amt,
                        balance_after=max(Decimal("0.00"), balance),
                    )
                )
        elif self.schedule_version == "v2":
            import calendar
            def _add_months(d, months: int):
                new_month = d.month - 1 + months
                new_year = d.year + new_month // 12
                new_month = new_month % 12 + 1
                max_days = calendar.monthrange(new_year, new_month)[1]
                new_day = min(d.day, max_days)
                return d.replace(year=new_year, month=new_month, day=new_day)

            count = self.debt_days or self.debt_months or 1
            step_days = self.interval_days or 1
            step_months = self.interval_months or 1
            is_months = bool(self.debt_months)

            total_cents = int(round(total * Decimal("100")))
            base_cents = total_cents // count
            remainder_cents = total_cents - (base_cents * count)

            start_date = self.first_due_date or timezone.localdate()
            balance = total

            for i in range(count):
                if is_months:
                    due_date = _add_months(start_date, i * step_months)
                else:
                    due_date = start_date + timedelta(days=i * step_days)

                cents = base_cents + (remainder_cents if i == count - 1 else 0)
                amt = (Decimal(cents) / Decimal("100")).quantize(Decimal("0.01"))
                balance = (balance - amt).quantize(Decimal("0.01"))

                installments_to_create.append(
                    DealInstallment(
                        id=uuid.uuid4(),
                        company=self.company,
                        branch=self.branch,
                        deal=self,
                        number=i + 1,
                        due_date=due_date,
                        amount=amt,
                        balance_after=max(Decimal("0.00"), balance),
                    )
                )
        else:
            due_date = self.first_due_date or (timezone.localdate() + timedelta(days=self.debt_days or 30))
            installments_to_create.append(
                DealInstallment(
                    id=uuid.uuid4(),
                    company=self.company,
                    branch=self.branch,
                    deal=self,
                    number=1,
                    due_date=due_date,
                    amount=total,
                    balance_after=Decimal("0.00"),
                )
            )

        with transaction.atomic():
            self.installments.all().delete()
            DealInstallment.objects.bulk_create(installments_to_create)

    def save(self, *args, **kwargs):
        custom_installments = getattr(self, "_custom_installments", None)
        schedule_snapshot = None
        if self.pk and not self._state.adding:
            _old = ClientDeal.objects.filter(pk=self.pk).first()
            if _old is not None:
                schedule_snapshot = _old._schedule_terms()
        if self.kind != self.Kind.DEBT:
            self.debt_days = None
            self.debt_months = None
            self.first_due_date = None
            self.auto_schedule = False

        self.full_clean()
        super().save(*args, **kwargs)

        if self.kind != self.Kind.DEBT:
            self.installments.all().delete()
            return

        if self.auto_schedule or custom_installments:
            # ТЗ 12.6: если по сделке уже есть платежи, а условия графика не менялись
            # (правка title/note и т.п.), график не пересобираем.
            if not custom_installments and schedule_snapshot is not None and self.payments.exists():
                if self._schedule_terms() == schedule_snapshot:
                    return
            self.rebuild_installments(custom_installments=custom_installments)

    def _schedule_terms(self):
        return (
            self.kind, self.amount or Decimal("0"), self.prepayment or Decimal("0"),
            self.debt_days, self.debt_months, self.first_due_date, self.schedule_version,
        )


def sync_sale_status_from_deal(deal_id) -> None:
    """
    09-sale-deal-linkage: статус продажи в долг следует за остатком её сделки.
    Остаток 0 — продажа «paid» (способ оплаты остаётся debt: деньги пришли погашением,
    а не в чеке); снова появился остаток (возврат платежа) — обратно «debt».
    Частичное погашение статус не меняет: до полного погашения продажа — «debt».
    Продажи, оплаченные иначе, отменённые и с возвратом не трогаем.
    """
    deal = ClientDeal.objects.select_related("sale").filter(pk=deal_id).first()
    if deal is None or deal.sale_id is None or deal.kind != ClientDeal.Kind.DEBT:
        return
    sale = deal.sale
    if sale.payment_method != Sale.PaymentMethod.DEBT:
        return
    if sale.status not in (Sale.Status.DEBT, Sale.Status.PAID):
        return
    remaining = deal.remaining_debt
    target = Sale.Status.PAID if remaining <= 0 else Sale.Status.DEBT
    left = max(Decimal("0.00"), remaining)
    updates = {}
    if sale.status != target:
        updates["status"] = target
    if sale.debt_remaining != left:
        updates["debt_remaining"] = left
    if updates:
        Sale.objects.filter(pk=sale.pk).update(**updates)


class DealInstallment(models.Model):
    # ✅ теперь тоже UUID (как ты хочешь “всё на uuid”)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="deal_installments",
        verbose_name="Компания",
        db_index=True,
        null=True,
        blank=True,
        
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="deal_installments",
        null=True,
        blank=True,
        verbose_name="Филиал",
        db_index=True,
    )

    deal = models.ForeignKey(
        ClientDeal,
        on_delete=models.CASCADE,
        related_name="installments",
        verbose_name="Сделка",
        db_index=True,
    )

    number = models.PositiveSmallIntegerField("№")
    due_date = models.DateField("Срок оплаты")
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)
    balance_after = models.DecimalField("Остаток", max_digits=12, decimal_places=2)

    paid_on = models.DateField("Оплачен", blank=True, null=True)
    paid_amount = models.DecimalField("Оплачено за период", max_digits=12, decimal_places=2, default=0)
    promised_date = models.DateField("Обещанная дата", blank=True, null=True)

    class Meta:
        verbose_name = "Платёж по графику"
        verbose_name_plural = "График платежей"
        ordering = ["deal", "number"]
        indexes = [
            models.Index(fields=["company", "branch", "deal"]),
            models.Index(fields=["company", "deal", "number"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["deal", "number"], name="uniq_installment_deal_number"),
            models.CheckConstraint(check=Q(amount__gte=0) & Q(paid_amount__gte=0), name="installment_non_negative"),
            models.CheckConstraint(check=Q(paid_amount__lte=F("amount")), name="installment_paid_lte_amount"),
        ]

    def clean(self):
        super().clean()
        if self.deal_id and self.company_id and self.deal.company_id != self.company_id:
            raise ValidationError({"company": "Компания взноса должна совпадать с компанией сделки."})
        if self.deal_id and self.branch_id != self.deal.branch_id:
            raise ValidationError({"branch": "Филиал взноса должен совпадать с филиалом сделки (включая NULL)."})

    def save(self, *args, **kwargs):
        # авто-подтягиваем из сделки (админка/скрипты)
        if self.deal_id:
            self.company_id = self.deal.company_id
            self.branch_id = self.deal.branch_id

        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def remaining_for_period(self) -> Decimal:
        return (self.amount - (self.paid_amount or Decimal("0"))).quantize(Decimal("0.01"))

    @property
    def is_overdue(self) -> bool:
        if self.paid_on is not None:
            return False
        today = timezone.localdate()
        return bool(self.due_date and self.due_date < today and self.remaining_for_period > 0)

    @property
    def overdue_days(self) -> int:
        if not self.is_overdue or not self.due_date:
            return 0
        today = timezone.localdate()
        return max(0, (today - self.due_date).days)

    @property
    def overdue_amount(self) -> Decimal:
        if not self.is_overdue:
            return Decimal("0.00")
        return self.remaining_for_period


class DealInstallmentHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="installment_history",
        verbose_name="Компания",
        db_index=True,
        null=True,
        blank=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="installment_history",
        null=True,
        blank=True,
        verbose_name="Филиал",
        db_index=True,
    )

    deal = models.ForeignKey(
        ClientDeal,
        on_delete=models.CASCADE,
        related_name="installment_history",
        verbose_name="Сделка",
        db_index=True,
    )

    installment = models.ForeignKey(
        DealInstallment,
        on_delete=models.CASCADE,
        related_name="history",
        verbose_name="Взнос",
        db_index=True,
    )

    idempotency_key = models.UUIDField("Ключ идемпотентности", null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="installment_history",
        verbose_name="Кем изменено",
    )

    old_due_date = models.DateField("Старый срок оплаты", null=True, blank=True)
    new_due_date = models.DateField("Новый срок оплаты", null=True, blank=True)

    old_promised_date = models.DateField("Старая обещанная дата", null=True, blank=True)
    new_promised_date = models.DateField("Новая обещанная дата", null=True, blank=True)

    note = models.TextField("Комментарий / Причина", blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "История изменения взноса"
        verbose_name_plural = "История изменений взносов"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "installment", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["installment", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="uniq_installment_history_idempotency_key",
            ),
        ]


class DealPayment(models.Model):
    class Kind(models.TextChoices):
        PAY = "pay", "Оплата"
        REFUND = "refund", "Возврат"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="deal_payments",
        verbose_name="Компания",
        db_index=True,
                null=True,
        blank=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="deal_payments",
        null=True,
        blank=True,
        verbose_name="Филиал",
        db_index=True,
    )

    deal = models.ForeignKey(
        ClientDeal,
        on_delete=models.CASCADE,
        related_name="payments",
        verbose_name="Сделка",
        db_index=True,
                null=True,
        blank=True,
    )

    installment = models.ForeignKey(
        DealInstallment,
        on_delete=models.CASCADE,
        related_name="payments",
        verbose_name="Взнос",
        db_index=True,
                null=True,
        blank=True,
    )

    kind = models.CharField("Тип", max_length=16, choices=Kind.choices, default=Kind.PAY)
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)

    paid_date = models.DateField("Дата платежа")
    idempotency_key = models.UUIDField("Ключ идемпотентности", null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deal_payments",
        verbose_name="Кем создан",
    )

    note = models.TextField("Комментарий", blank=True)
    payment_method = models.CharField("Способ оплаты", max_length=32, null=True, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Платёж"
        verbose_name_plural = "Платежи"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "branch", "deal", "created_at"]),
            models.Index(fields=["company", "installment", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(amount__gt=0), name="dealpayment_amount_gt_0"),
            models.UniqueConstraint(
                fields=["deal", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="uniq_deal_idempotency_key",
            ),
        ]

    def clean(self):
        super().clean()

        if self.deal_id and self.company_id and self.deal.company_id != self.company_id:
            raise ValidationError({"company": "Компания платежа должна совпадать с компанией сделки."})
        if self.deal_id and self.branch_id != self.deal.branch_id:
            raise ValidationError({"branch": "Филиал платежа должен совпадать с филиалом сделки (включая NULL)."})

        if self.installment_id and self.deal_id and self.installment.deal_id != self.deal_id:
            raise ValidationError({"installment": "Взнос не принадлежит этой сделке."})

        if self.installment_id and self.company_id and self.installment.company_id != self.company_id:
            raise ValidationError({"company": "Компания платежа должна совпадать с компанией взноса."})
        if self.installment_id and self.branch_id != self.installment.branch_id:
            raise ValidationError({"branch": "Филиал платежа должен совпадать с филиалом взноса (включая NULL)."})

    def save(self, *args, **kwargs):
        # авто-подтягиваем из сделки
        if self.deal_id:
            self.company_id = self.deal.company_id
            self.branch_id = self.deal.branch_id

        self.full_clean()
        super().save(*args, **kwargs)


class ClientDebtBulkPayment(models.Model):
    """Idempotency record for one payment distributed across a client's debts."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="client_debt_bulk_payments")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="debt_bulk_payments")
    idempotency_key = models.UUIDField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    affected_deal_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "idempotency_key"],
                name="uniq_client_bulk_debt_payment_idempotency",
            ),
        ]
        indexes = [models.Index(fields=["company", "client", "created_at"])]
            
class Bid(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новый"
        PROCESSING = "processing", "В обработке"
        REFUSAL = "refusal", "Отказ"
        THINKS = "thinks", "Думает"
        CONNECTED = "connected", "Подключено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name = models.CharField(max_length=255, verbose_name="ФИО")
    phone = models.CharField(max_length=255, verbose_name="Номер телефона")
    text = models.TextField(verbose_name="Обращение")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    status = models.CharField("Тип сделки", max_length=16, choices=Status.choices, default=Status.NEW)

    def __str__(self):
        return f"{self.full_name} - {self.phone} - {self.text}"

    class Meta:
        verbose_name = "Заявка на подключение"
        verbose_name_plural = "Заявки на подключение"
        ordering = ["-created_at"]


class SocialApplications(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новый"
        PROCESSING = "processing", "В обработке"
        CONNECTED = "connected", "Подключено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.CharField(max_length=255, verbose_name="Компания")
    text = models.TextField(verbose_name="Обращение")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    status = models.CharField("Тип сделки", max_length=16, choices=Status.choices, default=Status.NEW)

    def __str__(self):
        return f"{self.company} — {self.text[:30]}..."

    class Meta:
        verbose_name = "Заявка на соц. сети"
        verbose_name_plural = "Заявки на соц. сети"
        ordering = ["-created_at"]


# ==========================
# TransactionRecord
# ==========================
class TransactionRecord(models.Model):
    class Status(models.TextChoices):
        NEW = 'new', 'Новая'
        APPROVED = 'approved', 'Подтверждена'
        CANCELLED = 'cancelled', 'Отменена'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='transaction_records', verbose_name='Компания')
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_transaction_records',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    description = models.TextField(verbose_name="Обращение")

    # УДАЛЕНО: department FK, теперь запись не привязана к отделу
    name = models.CharField('Наименование', max_length=255)
    amount = models.DecimalField('Сумма', max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0'))])
    status = models.CharField('Статус', max_length=16, choices=Status.choices, default=Status.NEW)
    date = models.DateField('Дата')

    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Запись'
        verbose_name_plural = 'Записи'
        ordering = ['-date', '-created_at']
        indexes = [
            models.Index(fields=['company', 'date']),
            models.Index(fields=['company', 'branch', 'date']),
        ]

    def __str__(self):
        return f'{self.name} — {self.amount} ({self.get_status_display()})'

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})

    def save(self, *args, **kwargs):
        self.full_clean(exclude=None)
        super().save(*args, **kwargs)


# ==========================
# ContractorWork
# ==========================
class ContractorWork(models.Model):
    class ContractorType(models.TextChoices):
        LLC = "llc", "ОсОО / ООО"
        IP  = "ip",  "ИП"

    class Status(models.TextChoices):
        PROCESS = "process", "В процессе"
        COMPLETED  = "completed",  "Завершен"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name="ID")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="contractor_works", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_contractor_works',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    # УДАЛЕНО: department FK, теперь подрядные работы не привязаны к отделу

    title = models.CharField("Наименование", max_length=255)
    contractor_name = models.CharField("Имя подрядчика", max_length=255)
    contractor_phone = models.CharField("Телефон", max_length=32)
    contractor_entity_type = models.CharField("Тип юрлица", max_length=8, choices=ContractorType.choices, null=True, blank=True)
    contractor_entity_name = models.CharField("Название его ООО/ИП", max_length=255, null=True, blank=True)

    amount = models.DecimalField("Сумма договора", max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    status = models.CharField("Статус", max_length=255, choices=Status.choices, null=True, blank=True)
    start_date = models.DateField("Дата начала", null=True, blank=True)
    end_date = models.DateField("Дата окончания", null=True, blank=True)
    planned_completion_date = models.DateField("Плановая дата завершения", null=True, blank=True)
    work_calendar_date = models.DateField("Дата календаря выполнения работ", null=True, blank=True)

    description = models.TextField("Описание", blank=True)

    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Подрядные работы"
        verbose_name_plural = "Подрядные работы"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "start_date"]),
            models.Index(fields=["company", "branch", "start_date"]),
        ]

    def __str__(self):
        return f"{self.title} — {self.contractor_name}"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "Дата окончания не может быть раньше даты начала."})
        if self.planned_completion_date and self.start_date and self.planned_completion_date < self.start_date:
            raise ValidationError({"planned_completion_date": "Плановая дата завершения не может быть раньше начала."})

    @property
    def duration_days(self):
        if self.start_date and self.end_date:
            return (self.end_date - self.start_date).days
        return None


# ==========================
# Debt / DebtPayment
# ==========================
class Debt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="debts", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_debts',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    name = models.CharField("Имя", max_length=255)
    phone = models.CharField("Телефон", max_length=32)
    amount = models.DecimalField("Сумма долга", max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    due_date = models.DateTimeField(verbose_name="дата возвращения", null=True, blank=True)
    payment_method = models.CharField("Способ оплаты", max_length=32, null=True, blank=True)
    sale = models.ForeignKey(
        "Sale",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="legacy_debts",
        verbose_name="Связанная продажа",
    )

    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Долг"
        verbose_name_plural = "Долги"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "phone"]),
            models.Index(fields=["company", "branch", "created_at"]),
        ]

    def __str__(self):
        return f"{self.name} — {self.phone} ({self.amount} c)"

    @property
    def paid_total(self) -> Decimal:
        return self.payments.aggregate(s=models.Sum("amount"))["s"] or Decimal("0")

    @property
    def balance(self) -> Decimal:
        return (self.amount - self.paid_total).quantize(Decimal("0.01"))

    def add_payment(self, amount: Decimal, paid_at=None, note: str = "", payment_method: str = "cash"):
        payment = DebtPayment(
            debt=self, company=self.company, branch=self.branch, amount=amount,
            paid_at=paid_at or timezone.now().date(), note=note,
            payment_method=payment_method,
        )
        payment.full_clean()
        payment.save()
        return payment

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})


class DebtPayment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="debt_payments", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_debt_payments',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    debt = models.ForeignKey(Debt, on_delete=models.CASCADE, related_name="payments", verbose_name="Долг")

    amount = models.DecimalField("Сумма оплаты", max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    paid_at = models.DateField("Дата оплаты", default=timezone.localdate)
    note = models.CharField("Комментарий", max_length=255, blank=True)
    payment_method = models.CharField("Способ оплаты", max_length=32, null=True, blank=True)

    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Оплата долга"
        verbose_name_plural = "Оплаты долга"
        ordering = ["-paid_at", "-created_at"]
        indexes = [
            models.Index(fields=["company", "paid_at"]),
            models.Index(fields=["company", "branch", "paid_at"]),
            models.Index(fields=["debt", "paid_at"]),
        ]

    def __str__(self):
        return f"{self.amount} c от {self.paid_at} ({self.debt.name})"

    def clean(self):
        if self.debt and self.company_id and self.debt.company_id != self.company_id:
            raise ValidationError({"company": "Компания платежа должна совпадать с компанией долга."})
        if self.debt and self.branch_id is not None and self.debt.branch_id not in (None, self.branch_id):
            raise ValidationError({"branch": "Филиал платежа должен совпадать с филиалом долга (или быть глобальным вместе с ней)."})
        if self.debt_id and self.amount:
            qs = self.debt.payments.exclude(pk=self.pk) if self.pk else self.debt.payments
            already = qs.aggregate(s=models.Sum("amount"))["s"] or Decimal("0")
            rest = (self.debt.amount - already)
            if self.amount > rest:
                raise ValidationError({"amount": f"Сумма оплаты превышает остаток долга ({rest} c)."})

    def save(self, *args, **kwargs):
        if self.debt_id:
            if not self.company_id:
                self.company_id = self.debt.company_id
            if self.branch_id is None:
                self.branch_id = self.debt.branch_id
        self.full_clean()
        super().save(*args, **kwargs)


# ==========================
# Object Items / Sales
# ==========================
class ObjectItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="object_items")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_object_items',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )

    name = models.CharField("Наименование", max_length=255)
    description = models.TextField("Описание", blank=True)
    price = models.DecimalField("Цена", max_digits=12, decimal_places=2)
    date = models.DateField("Дата", default=timezone.localdate)
    quantity = models.PositiveIntegerField("Количество", default=1, validators=[MinValueValidator(1)])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=['company', 'date']),
            models.Index(fields=['company', 'branch', 'date']),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})


class ObjectSale(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новая"
        PAID = "paid", "Оплачена"
        CANCELED = "canceled", "Отменена"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="object_sales", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='crm_object_sales',
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    client = models.ForeignKey("main.Client", blank=True, null=True, on_delete=models.SET_NULL,
                               related_name="object_sales", verbose_name="Клиент")

    status = models.CharField("Статус", max_length=16, choices=Status.choices, default=Status.NEW)
    sold_at = models.DateField("Дата продажи", default=timezone.localdate)
    note = models.CharField("Комментарий", max_length=255, blank=True)

    subtotal = models.DecimalField("Сумма", max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        ordering = ["-sold_at", "-created_at"]
        indexes = [
            models.Index(fields=["company", "sold_at"]),
            models.Index(fields=["company", "branch", "sold_at"]),
            models.Index(fields=["company", "client"]),
        ]

    def __str__(self):
        return f"Продажа {self.id} — {self.get_status_display()}"

    def recalc(self):
        total = sum((i.unit_price * i.quantity for i in self.items.all()), Decimal("0"))
        self.subtotal = total
        self.save(update_fields=["subtotal"])

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})


class ObjectSaleItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sale = models.ForeignKey(ObjectSale, on_delete=models.CASCADE, related_name="items", verbose_name="Продажа")
    name_snapshot = models.CharField("Наименование (снимок)", max_length=255)
    unit_price = models.DecimalField("Цена за единицу", max_digits=12, decimal_places=2)
    quantity = models.PositiveIntegerField("Кол-во", validators=[MinValueValidator(1)])
    object_item = models.ForeignKey(ObjectItem, on_delete=models.PROTECT, related_name="sold_items", verbose_name="Объект")

    def save(self, *args, **kwargs):
        creating = self.pk is None
        if creating:
            if not self.name_snapshot:
                self.name_snapshot = self.object_item.name
            if not self.unit_price:
                self.unit_price = self.object_item.price

        if self.unit_price is not None:
            self.unit_price = _money(self.unit_price)
        super().save(*args, **kwargs)
        if creating:
            self.object_item.quantity = max(0, self.object_item.quantity - self.quantity)
            self.object_item.save(update_fields=["quantity"])
        self.sale.recalc()

class ManufactureSubreal(models.Model):
    class Status(models.TextChoices):
        OPEN   = "open",   "Открыта"
        CLOSED = "closed", "Закрыта"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="subreals")
    branch = models.ForeignKey(
        "users.Branch", on_delete=models.CASCADE, related_name="crm_subreals",
        null=True, blank=True, db_index=True, verbose_name="Филиал"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="subreals")
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="subreals_as_agent")
    product = models.ForeignKey("main.Product", on_delete=models.PROTECT, related_name="subreals")

    # Опциональный идемпотентный ключ на создание передачи
    external_ref = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    # Флаг «пилорама» — авто-приём при создании
    is_sawmill = models.BooleanField(
        default=False, db_index=True, help_text="Если True — авто-приём на весь остаток при создании."
    )

    qty_transferred = models.PositiveIntegerField()
    qty_accepted    = models.PositiveIntegerField(default=0)
    qty_returned    = models.PositiveIntegerField(default=0)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Передача агенту"
        verbose_name_plural = "Передачи агентам"
        indexes = [
            models.Index(fields=["company", "agent", "product", "status"]),
            models.Index(fields=["company", "branch", "agent", "product", "status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["company", "is_sawmill", "status"]),

        ]
        constraints = [
            # идемпотентность создания передачи (включается только когда external_ref не NULL)
            models.UniqueConstraint(
                fields=["company", "external_ref"],
                name="uniq_subreal_company_external_ref",
                condition=Q(external_ref__isnull=False),
            ),
        ]

    def __str__(self):
        agent_name = (
            getattr(self.agent, "get_full_name", lambda: "")() or
            getattr(self.agent, "username", None) or
            str(self.agent_id)
        )
        prod_name = getattr(self.product, "name", None) or str(self.product_id)
        return f"{agent_name} · {prod_name} · {self.qty_transferred}"

    @property
    def qty_remaining(self) -> int:
        return max((self.qty_transferred or 0) - (self.qty_accepted or 0), 0)

    @property
    def qty_on_agent(self) -> int:
        """
        Базовое свойство без учета продаж (для обратной совместимости).
        Для правильного расчета используйте get_qty_on_hand_with_sales().
        """
        return max((self.qty_accepted or 0) - (self.qty_returned or 0), 0)
    
    def get_qty_on_hand_with_sales(self, company_id=None, *, exclude_pending_return_id=None) -> int:
        """
        Вычисляет количество на руках с учетом продаж через AgentSaleAllocation.
        Если company_id не указан, используется self.company_id.

        Также учитывает "резерв" под возвраты со статусом PENDING:
          available = accepted - returned - sold(paid/debt) - pending_reserved

        exclude_pending_return_id:
          - полезно при approve конкретного возврата, чтобы не вычитать его самого из резерва.
        """
        company_id = company_id or self.company_id
        accepted = int(self.qty_accepted or 0)
        returned = int(self.qty_returned or 0)
        
        # Вычисляем проданное количество
        sold = (
            AgentSaleAllocation.objects
            .filter(
                subreal_id=self.pk,
                company_id=company_id,
                sale__status__in=[Sale.Status.PAID, Sale.Status.DEBT],
            )
            .aggregate(total=Sum("qty"))["total"] or 0
        )
        sold = int(sold)

        pending_qs = ReturnFromAgent.objects.filter(
            company_id=company_id,
            subreal_id=self.pk,
            status=ReturnFromAgent.Status.PENDING,
        )
        if exclude_pending_return_id:
            pending_qs = pending_qs.exclude(pk=exclude_pending_return_id)
        pending_reserved = int(pending_qs.aggregate(s=Sum("qty"))["s"] or 0)

        return max(accepted - returned - sold - pending_reserved, 0)

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})
        if self.agent_id and getattr(self.agent, "company_id", None) not in (None, self.company_id):
            raise ValidationError({"agent": "Агент принадлежит другой компании."})
        if self.product_id and self.product.company_id != self.company_id:
            raise ValidationError({"product": "Товар принадлежит другой компании."})
        if self.branch_id and self.product and self.product.branch_id not in (None, self.branch_id):
            raise ValidationError({"product": "Товар другого филиала."})
        if (self.qty_accepted or 0) > (self.qty_transferred or 0):
            raise ValidationError({"qty_accepted": "Принято не может превышать переданное."})
        if (self.qty_returned or 0) > (self.qty_accepted or 0):
            raise ValidationError({"qty_returned": "Возвращено не может превышать принятое."})

    def try_close(self):
        if self.qty_remaining == 0 and self.status != self.Status.CLOSED:
            self.status = self.Status.CLOSED
            self.save(update_fields=["status"])

    @transaction.atomic
    def auto_accept_if_needed(self, by_user):
        """
        Для is_sawmill=True: принять весь остаток сразу один раз.
        Идемпотентно и без конфликтов.
        """
        if not self.is_sawmill:
            return

        # 1. Жёстко лочим ТОЛЬКО сам subreal, без join'ов
        locked = (
            type(self).objects
            .select_related(None)        # убираем join'ы
            .select_for_update()
            .get(pk=self.pk)
        )

        remaining = locked.qty_remaining
        if remaining <= 0 or locked.status != locked.Status.OPEN:
            return

        # 2. Нам ещё нужны company и branch для Acceptance.
        #    Они уже есть на self (или можем рефрешнуть locked с нужными связями без FOR UPDATE).
        #    Сейчас проще так: возьмём company/branch с self, они не меняются в процессе.
        company = self.company
        branch = self.branch

        Acceptance.objects.create(
            company=company,
            branch=branch,
            subreal=locked,
            accepted_by=by_user,
            qty=remaining,
            accepted_at=timezone.now(),
        )
        # Остальное (qty_accepted, try_close) сделает Acceptance.save()


class Acceptance(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="acceptances")
    branch = models.ForeignKey(
        "users.Branch", on_delete=models.CASCADE, related_name="crm_acceptances",
        null=True, blank=True, db_index=True, verbose_name="Филиал"
    )
    subreal = models.ForeignKey(ManufactureSubreal, on_delete=models.CASCADE, related_name="acceptances")
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="acceptances")
    qty = models.PositiveIntegerField()
    accepted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Приём по передаче"
        verbose_name_plural = "Приёмы по передаче"
        ordering = ["-accepted_at"]
        indexes = [
            models.Index(fields=["company", "accepted_at"]),
            models.Index(fields=["company", "branch", "accepted_at"]),
            models.Index(fields=["subreal"]),

        ]

    def clean(self):
        if self.subreal_id and self.company_id and self.subreal.company_id != self.company_id:
            raise ValidationError({"company": "Компания приёма должна совпадать с компанией передачи."})
        if self.subreal_id and self.branch_id is not None and self.subreal.branch_id not in (None, self.branch_id):
            raise ValidationError({"branch": "Филиал приёма должен совпадать с филиалом передачи."})
        if (self.qty or 0) < 1:
            raise ValidationError({"qty": "Минимум 1."})
        # запретим приём в закрытую передачу на уровне модели
        if self.subreal and self.subreal.status != ManufactureSubreal.Status.OPEN:
            raise ValidationError({"subreal": "Передача уже закрыта."})
        if self.subreal and (self.qty or 0) > self.subreal.qty_remaining:
            raise ValidationError({"qty": f"Нельзя принять {self.qty}: доступно {self.subreal.qty_remaining}."})

    def save(self, *args, **kwargs):
        if self.subreal_id:
            if not self.company_id:
                self.company_id = self.subreal.company_id
            if self.branch_id is None:
                self.branch_id = self.subreal.branch_id
        self.full_clean()
        creating = self._state.adding
        super().save(*args, **kwargs)
        if creating:
            ManufactureSubreal.objects.filter(pk=self.subreal_id).update(qty_accepted=F("qty_accepted") + self.qty)
            self.subreal.refresh_from_db(fields=["qty_accepted", "qty_transferred", "status"])
            self.subreal.try_close()


class ReturnFromAgent(models.Model):
    class Status(models.TextChoices):
        PENDING  = "pending",  "Ожидает приёма"
        ACCEPTED = "accepted", "Принят"
        REJECTED = "rejected", "Отклонён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="returns")
    branch = models.ForeignKey(
        "users.Branch", on_delete=models.CASCADE, related_name="crm_returns",
        null=True, blank=True, db_index=True, verbose_name="Филиал"
    )
    subreal = models.ForeignKey(ManufactureSubreal, on_delete=models.CASCADE, related_name="returns")
    returned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="returns")
    qty = models.PositiveIntegerField()
    returned_at = models.DateTimeField(default=timezone.now)

    # Брак vs обычный возврат.
    # is_defect=False → товар возвращается на склад (при accept Product.quantity += qty).
    # is_defect=True  → товар списывается как брак, на склад НЕ возвращается.
    is_defect = models.BooleanField(
        "Брак", default=False, db_index=True,
        help_text="True — товар списывается как брак и на склад не возвращается.",
    )
    # Денежная сумма возврата/списания (по цене продажи строки чека).
    # Заполняется для возвратов, созданных из возврата продажи; для возвратов
    # неproданного остатка остаётся 0.
    amount = models.DecimalField(
        "Сумма", max_digits=12, decimal_places=2, default=Decimal("0.00"),
    )
    # Контрагент (клиент) из чека, по которому был возврат/брак.
    # Заполняется только для возвратов, созданных из возврата продажи.
    client = models.ForeignKey(
        "main.Client", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agent_returns", db_index=True, verbose_name="Клиент",
    )

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="accepted_returns", null=True, blank=True
    )
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Возврат от агента"
        verbose_name_plural = "Возвраты от агентов"
        ordering = ["-returned_at"]
        indexes = [
            models.Index(fields=["company", "returned_at"]),
            models.Index(fields=["company", "branch", "returned_at"]),
            models.Index(fields=["subreal"]),
            models.Index(fields=["status"]),
            # Раздельная аналитика возвратов/брака: фильтр по is_defect + статусу,
            # срезы по агенту.
            models.Index(fields=["company", "is_defect", "status"]),
            models.Index(fields=["returned_by", "is_defect"]),
        ]

    def clean(self):
        if self.subreal_id and self.company_id and self.subreal.company_id != self.company_id:
            raise ValidationError({"company": "Компания возврата должна совпадать с компанией передачи."})
        if self.subreal_id and self.branch_id is not None and self.subreal.branch_id not in (None, self.branch_id):
            raise ValidationError({"branch": "Филиал возврата должен совпадать с филиалом передачи."})
        if (self.qty or 0) < 1:
            raise ValidationError({"qty": "Минимум 1."})
        if self.subreal and self.status == self.Status.PENDING:
            qty_on_hand = self.subreal.get_qty_on_hand_with_sales(
                company_id=self.company_id,
                exclude_pending_return_id=self.pk,  # при апдейте не считаем резерв "самого себя"
            )
            if (self.qty or 0) > qty_on_hand:
                raise ValidationError({"qty": f"Нельзя вернуть {self.qty}: на руках {qty_on_hand}."})

    def save(self, *args, **kwargs):
        if self.subreal_id:
            if not self.company_id:
                self.company_id = self.subreal.company_id
            if self.branch_id is None:
                self.branch_id = self.subreal.branch_id
        self.full_clean()
        super().save(*args, **kwargs)

    @transaction.atomic
    def accept(self, by_user):
        if self.status != self.Status.PENDING:
            raise ValidationError("Возврат уже обработан.")
        locked_sub = ManufactureSubreal.objects.select_for_update().get(pk=self.subreal_id)
        qty_on_hand = locked_sub.get_qty_on_hand_with_sales(
            company_id=self.company_id,
            exclude_pending_return_id=self.pk,
        )
        if self.qty > qty_on_hand:
            # расширенный ответ, чтобы было понятно почему 0
            accepted = int(locked_sub.qty_accepted or 0)
            returned = int(locked_sub.qty_returned or 0)
            sold = (
                AgentSaleAllocation.objects
                .filter(
                    subreal_id=locked_sub.pk,
                    company_id=self.company_id,
                    sale__status__in=[Sale.Status.PAID, Sale.Status.DEBT],
                )
                .aggregate(total=Sum("qty"))["total"] or 0
            )
            sold = int(sold)
            pending_reserved_other = int(
                ReturnFromAgent.objects.filter(
                    company_id=self.company_id,
                    subreal_id=locked_sub.pk,
                    status=ReturnFromAgent.Status.PENDING,
                )
                .exclude(pk=self.pk)
                .aggregate(s=Sum("qty"))["s"] or 0
            )
            debug_payload = {
                "qty_accepted": accepted,
                "qty_returned": returned,
                "sold_paid_debt": sold,
                "pending_reserved_other": pending_reserved_other,
                "available_now": qty_on_hand,
            }
            raise ValidationError(
                {
                    "qty": [f"Можно принять максимум {qty_on_hand}."],
                    # Django ValidationError ожидает строку/список строк, не dict
                    "debug": [json.dumps(debug_payload, ensure_ascii=False, default=str)],
                }
            )
        product = locked_sub.product
        prod_model = type(product)
        prod_id = product.pk

        # Брак на склад НЕ возвращаем — товар списывается.
        # Обычный возврат — возвращаем количество на склад (Product.quantity).
        if not self.is_defect:
            qty_before = prod_model.objects.filter(pk=product.pk).values_list("quantity", flat=True).first() or Decimal("0")
            prod_model.objects.select_for_update().filter(pk=product.pk).update(quantity=F("quantity") + self.qty)

            # Журнал: возврат от агента (приход на склад готовой продукции)
            try:
                record_stock_movement(
                    company=self.company, branch=self.branch,
                    type=StockMovement.Type.AGENT_RETURN,
                    object_id=product.pk, product_name=getattr(product, "name", ""),
                    warehouse=StockMovement.Warehouse.FINISHED_GOODS,
                    qty_before=qty_before, change=self.qty, qty_after=qty_before + self.qty,
                    created_by=by_user, sender=self.returned_by,
                    ref_type="return", ref_id=self.pk,
                )
            except Exception:
                logging.getLogger("crm.webhooks").error(
                    "Failed to record stock movement on return accept. return_id=%s", self.pk, exc_info=True,
                )

            def _send_webhook():
                from apps.main.services.webhooks import send_product_webhook

                try:
                    prod = prod_model.objects.get(pk=prod_id)
                    send_product_webhook(prod, "product.updated")
                except Exception:
                    logging.getLogger("crm.webhooks").error(
                        "Failed to send product.updated webhook after return accept. product_id=%s",
                        prod_id,
                        exc_info=True,
                    )

            try:
                transaction.on_commit(_send_webhook)
            except Exception:
                _send_webhook()
        ManufactureSubreal.objects.filter(pk=locked_sub.pk).update(qty_returned=F("qty_returned") + self.qty)
        self.status = self.Status.ACCEPTED
        self.accepted_by = by_user
        self.accepted_at = timezone.now()
        super().save(update_fields=["status", "accepted_by", "accepted_at"])

    @transaction.atomic
    def reject(self, by_user):
        """
        Владелец/админ отклонил возврат.
        Товар на склад не возвращаем.
        """
        if self.status != self.Status.PENDING:
            raise ValidationError("Возврат уже обработан.")
        self.status = self.Status.REJECTED
        self.accepted_by = by_user
        self.accepted_at = timezone.now()
        super().save(update_fields=["status", "accepted_by", "accepted_at"])


class AgentSaleAllocation(models.Model):
    company   = models.ForeignKey("users.Company", on_delete=models.CASCADE)
    agent     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, db_index=True)
    subreal   = models.ForeignKey(ManufactureSubreal, on_delete=models.CASCADE, related_name="sale_allocations", db_index=True)
    sale      = models.ForeignKey("main.Sale", on_delete=models.CASCADE, related_name="agent_allocations")
    sale_item = models.ForeignKey("main.SaleItem", on_delete=models.CASCADE, related_name="agent_allocations")
    product   = models.ForeignKey("main.Product", on_delete=models.PROTECT)
    qty       = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["agent", "product"]),
            models.Index(fields=["subreal", "product"]),
            models.Index(fields=["sale", "product", "subreal"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sale_item", "subreal"],
                name="uniq_allocation_saleitem_subreal",
            ),
        ]


class MarketSaleEmployeePayProfile(models.Model):
    """
    Схема ЗП продавца по чекам main.Sale (поле user): оклад / % от личных продаж / оклад + %.
    Расчёт периода — см. analytics_market AnalyticsView tab=salary.
    """

    class PayScheme(models.TextChoices):
        SALARY = "salary", "Оклад"
        PERCENT = "percent", "Процент от продаж"
        SALARY_PLUS_PERCENT = "salary_plus_percent", "Оклад + процент от продаж"
        # ТЗ ч.13, 3: фиксированная сумма за каждую проданную единицу
        PER_ITEM = "per_item", "За проданный товар"
        SALARY_PLUS_PER_ITEM = "salary_plus_per_item", "Оклад + за проданный товар"
        PERCENT_PLUS_PER_ITEM = "percent_plus_per_item", "Процент + за проданный товар"
        # ТЗ 26.10 Б-63: ставка за каждую закрытую смену
        PER_SHIFT = "per_shift", "За смену"
        PER_SHIFT_PLUS_PERCENT = "per_shift_plus_percent", "За смену + процент"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="market_sale_employee_pay_profiles",
        verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="market_sale_employee_pay_profiles",
        verbose_name="Филиал",
        null=True,
        blank=True,
        db_index=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="market_sale_employee_pay_profiles",
        verbose_name="Сотрудник",
    )
    pay_scheme = models.CharField(
        max_length=24,
        choices=PayScheme.choices,
        default=PayScheme.SALARY_PLUS_PERCENT,
        verbose_name="Схема оплаты",
    )
    monthly_base_salary = models.DecimalField(
        "Оклад в месяц",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    sales_percent = models.DecimalField(
        "Процент от личных продаж",
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    per_item_amount = models.DecimalField(
        "Сумма за каждый проданный товар",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Сом за единицу; весовой товар — 1 за строку чека. Если > 0, добавляется к любой схеме.",
    )
    # ТЗ 26.10 Б-63: сменная ставка (схемы per_shift, per_shift_plus_percent), сом за закрытую смену
    per_shift_amount = models.DecimalField(
        "Ставка за смену",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        db_default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )

    class Meta:
        verbose_name = "Зарплата продавца (маркет)"
        verbose_name_plural = "Зарплаты продавцов (маркет)"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "branch", "user"],
                name="uniq_market_sale_pay_company_branch_user",
                condition=Q(branch__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["company", "user"],
                name="uniq_market_sale_pay_company_user_global_branch",
                condition=Q(branch__isnull=True),
            ),
        ]
        indexes = [
            models.Index(fields=["company", "user"]),
        ]

    def clean(self):
        if self.user_id and self.company_id:
            uid = getattr(self.user, "company_id", None)
            if uid and uid != self.company_id:
                raise ValidationError({"user": "Пользователь другой компании."})
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал другой компании."})
        if self.sales_percent > Decimal("100"):
            raise ValidationError({"sales_percent": "Не больше 100%."})
        if self.pay_scheme in (self.PayScheme.PER_SHIFT, self.PayScheme.PER_SHIFT_PLUS_PERCENT):
            if (self.per_shift_amount or Decimal("0")) <= 0:
                raise ValidationError({"per_shift_amount": "Для сменной схемы укажите ставку за смену больше 0."})
            if self.pay_scheme == self.PayScheme.PER_SHIFT_PLUS_PERCENT and (self.sales_percent or Decimal("0")) <= 0:
                raise ValidationError({"sales_percent": "Для схемы «За смену + процент» укажите процент больше 0."})
        if self.pay_scheme == self.PayScheme.SALARY:
            if (self.monthly_base_salary or Decimal("0")) <= 0:
                raise ValidationError({"monthly_base_salary": "Для схемы «Оклад» укажите оклад больше 0."})
        elif self.pay_scheme == self.PayScheme.PERCENT:
            if (self.sales_percent or Decimal("0")) <= 0:
                raise ValidationError({"sales_percent": "Для схемы «Процент» укажите процент больше 0."})
        elif self.pay_scheme == self.PayScheme.SALARY_PLUS_PERCENT:
            if (self.monthly_base_salary or Decimal("0")) <= 0 or (self.sales_percent or Decimal("0")) <= 0:
                raise ValidationError(
                    {
                        "pay_scheme": "Для схемы «Оклад + процент» задайте и оклад, и процент больше 0.",
                    }
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user_id} @ {self.company_id}"


class AgentRequestCart(models.Model):
    """
    Заявка агента на получение товара.
    Агент сначала копит товары в статусе draft, потом отправляет (submitted),
    владелец подтверждает (approved), после чего товар списывается со склада
    и создаются передачи (ManufactureSubreal) на агента.
    """

    class Status(models.TextChoices):
        DRAFT     = "draft", "Черновик"
        SUBMITTED = "submitted", "Отправлено владельцу"
        APPROVED  = "approved", "Одобрено и выдано"
        REJECTED  = "rejected", "Отклонено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="agent_carts", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="crm_agent_carts",
        null=True, blank=True, db_index=True, verbose_name='Филиал'
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="agent_request_carts",
        verbose_name="Агент",
        help_text="Кому выдаём товар",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="agent_request_carts",
        verbose_name="Клиент"
    )

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    note = models.CharField(max_length=255, blank=True, verbose_name="Комментарий агента")

    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="approved_agent_carts",
        verbose_name="Кем одобрено"
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        verbose_name = "Заявка агента на товар"
        verbose_name_plural = "Заявки агентов на товар"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "branch", "status"]),
            models.Index(fields=["agent", "status"]),
        ]

    def __str__(self):
        return f"Заявка {self.id} от {getattr(self.agent,'username',self.agent_id)} [{self.get_status_display()}]"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({'branch': 'Филиал принадлежит другой компании.'})
        if self.agent_id and getattr(self.agent, "company_id", None) not in (None, self.company_id):
            raise ValidationError({'agent': 'Агент принадлежит другой компании.'})
        if self.client_id and self.client.company_id != self.company_id:
            raise ValidationError({'client': 'Клиент другой компании.'})
        if self.branch_id and self.client_id and self.client.branch_id not in (None, self.branch_id):
            raise ValidationError({'client': 'Клиент другого филиала.'})

    def is_editable(self) -> bool:
        return self.status == self.Status.DRAFT

    def _recalc_gifts_for_items(self):
        """
        Пересчитать подарки для всех позиций.
        Вызывается при submit() — фиксируем gift_quantity и total_quantity.
        """
        for it in self.items.select_related("product"):
            base_qty = int(it.quantity_requested or 0)
            from apps.utils import compute_gift_qty
            gift_qty = compute_gift_qty(
                product=it.product,
                qty=base_qty,
                company=self.company,
                branch=self.branch,
            )
            it.gift_quantity = gift_qty
            it.total_quantity = base_qty + gift_qty
            # price_snapshot хранится с 2 знаками после запятой (денежный формат),
            # а Product.price может быть с 3 знаками -> округляем.
            if not it.price_snapshot:
                it.price_snapshot = _money(it.product.price if it.product else Decimal("0"))
            else:
                it.price_snapshot = _money(it.price_snapshot)
            it.save(update_fields=["gift_quantity", "total_quantity", "price_snapshot", "updated_at"])

    @transaction.atomic
    def submit(self):
        """
        Агент нажал 'Отправить запрос'.
        После этого менять корзину нельзя.
        """
        if self.status != self.Status.DRAFT:
            raise ValidationError("Можно отправить только черновик.")
        if not self.items.exists():
            raise ValidationError("Нельзя отправить пустую заявку.")
        # фиксируем подарки
        self._recalc_gifts_for_items()
        self.status = self.Status.SUBMITTED
        self.submitted_at = timezone.now()
        self.full_clean()
        self.save(update_fields=["status", "submitted_at", "updated_at"])

    @transaction.atomic
    def approve(self, by_user):
        """
        Владелец/админ подтверждает заявку.
        Мы списываем товар со склада, создаём передачи (ManufactureSubreal) на агента,
        и привязываем каждую позицию к созданной передаче.
        """
        if self.status != self.Status.SUBMITTED:
            raise ValidationError("Можно одобрить только заявку в статусе 'submitted'.")

        # пересчёт подарков, чтобы qty/подарок/итого были зафиксированы
        self._recalc_gifts_for_items()

        for it in self.items.select_related("product"):
            prod = it.product
            need_qty = int(it.total_quantity or 0)
            if need_qty <= 0:
                continue  # пустышка - пропускаем

            # 💡 безопасная блокировка конкретного продукта без join'ов
            locked_qs = (
                type(prod).objects
                .select_related(None)     # ВАЖНО: убираем автоджойны
                .select_for_update()
                .filter(pk=prod.pk)
            )

            current_qty = locked_qs.values_list("quantity", flat=True).first() or 0
            if current_qty < need_qty:
                raise ValidationError({
                    "items": f"Недостаточно на складе для {prod.name}: нужно {need_qty}, доступно {current_qty}."
                })

            # списываем со склада
            # ТЗ-28 §2.1: продажа меняет updated_at, иначе касса пропустит остаток при ?updated_since=
            locked_qs.update(quantity=F("quantity") - need_qty, updated_at=timezone.now())

            prod_model = type(prod)
            prod_id = prod.pk

            def _send_webhook():
                from apps.main.services.webhooks import send_product_webhook

                try:
                    p = prod_model.objects.get(pk=prod_id)
                    send_product_webhook(p, "product.updated")
                except Exception:
                    logging.getLogger("crm.webhooks").error(
                        "Failed to send product.updated webhook after agent request approve. product_id=%s",
                        prod_id,
                        exc_info=True,
                    )

            try:
                transaction.on_commit(_send_webhook)
            except Exception:
                _send_webhook()

            # создаём передачу агенту
            sub = ManufactureSubreal.objects.create(
                company=self.company,
                branch=self.branch,
                user=by_user,        # кто выдал
                agent=self.agent,    # кто получил
                product=prod,
                qty_transferred=need_qty,
                is_sawmill=True,     # сразу считаем, что он взял в руки
            )

            # авто-принять (это поднимет qty_accepted и может закрыть передачу)
            sub.auto_accept_if_needed(by_user)

            # привязываем позицию к созданной передаче
            it.subreal = sub
            it.save(update_fields=["subreal", "updated_at"])

        self.status = self.Status.APPROVED
        self.approved_at = timezone.now()
        self.approved_by = by_user
        self.full_clean()
        self.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])

    @transaction.atomic
    def reject(self, by_user):
        """
        Владелец/админ отклонил.
        Товар не списываем.
        """
        if self.status != self.Status.SUBMITTED:
            raise ValidationError("Можно отклонить только заявку в статусе 'submitted'.")
        self.status = self.Status.REJECTED
        self.approved_at = timezone.now()
        self.approved_by = by_user
        self.full_clean()
        self.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])


class AgentRequestItem(models.Model):
    """
    Строка внутри AgentRequestCart.
    В черновике агент просто накидывает product + quantity_requested.
    Когда заявка отправляется (submit), мы фиксируем:
      - gift_quantity (сколько бесплатно),
      - total_quantity (итого нужно выдать агенту),
      - price_snapshot (цена на момент заявки).
    При approve мы создаём ManufactureSubreal и кладём ссылку сюда.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cart = models.ForeignKey(
        AgentRequestCart,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Заявка"
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="agent_request_items",
        verbose_name="Товар"
    )

    quantity_requested = models.PositiveIntegerField(verbose_name="Запрошено (шт)")
    gift_quantity = models.PositiveIntegerField(default=0, verbose_name="Подарок (шт)")
    total_quantity = models.PositiveIntegerField(default=0, verbose_name="Итого выдать (шт)")

    price_snapshot = models.DecimalField(
        "Цена на момент заявки",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="фиксируется при отправке"
    )

    subreal = models.ForeignKey(
        ManufactureSubreal,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="request_items",
        verbose_name="Передача агенту"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Позиция заявки агента"
        verbose_name_plural = "Позиции заявки агента"
        indexes = [
            models.Index(fields=["cart"]),
            models.Index(fields=["product"]),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.quantity_requested}"

    def clean(self):
        # нельзя чужую компанию/филиал
        if self.cart_id and self.product_id:
            if self.product.company_id != self.cart.company_id:
                raise ValidationError({"product": "Товар другой компании."})
            if self.cart.branch_id and self.product.branch_id not in (None, self.cart.branch_id):
                raise ValidationError({"product": "Товар другого филиала."})
        if self.quantity_requested < 1:
            raise ValidationError({"quantity_requested": "Количество должно быть ≥ 1."})

        # если корзина уже SUBMITTED/APPROVED/REJECTED — менять нельзя
        if self.cart_id and self.cart.status != AgentRequestCart.Status.DRAFT:
            raise ValidationError("Нельзя редактировать позиции не в черновике.")

    def save(self, *args, **kwargs):
        """
        Правила:
        - Пока корзина DRAFT:
            можно свободно создавать/редактировать строку (агент наполняет заявку).
        - Когда корзина уже SUBMITTED:
            агент руками редактировать не может,
            но backend при submit()/approve() может:
            * зафиксировать gift_quantity / total_quantity / price_snapshot
            * проставить ссылку subreal после фактической выдачи
            Эти апдейты приходят с явным update_fields.
        - Когда корзина APPROVED или REJECTED:
            больше никаких изменений.
        """

        if not self.cart_id:
            raise ValidationError("Строка без cart не сохраняется.")

        cart_status = self.cart.status
        creating = self._state.adding  # True если это новая позиция (INSERT)

        # ===== 1. Корзина ещё черновик -> полный доступ
        if cart_status == AgentRequestCart.Status.DRAFT:
            # зафиксируем price_snapshot если не задан
            if not self.price_snapshot:
                # Product.price может иметь 3 знака после запятой, а снапшот — 2.
                self.price_snapshot = _money(self.product.price if self.product_id else Decimal("0"))
            else:
                self.price_snapshot = _money(self.price_snapshot)
            # обычная валидация
            self.full_clean()
            return super().save(*args, **kwargs)

        # ===== 2. Корзина SUBMITTED -> только служебные апдейты бекэнда
        if cart_status == AgentRequestCart.Status.SUBMITTED:
            # агент не может добавлять новые позиции
            if creating:
                raise ValidationError("Нельзя добавлять позиции после отправки заявки.")

            allowed_fields = kwargs.get("update_fields")

            if allowed_fields:
                allowed_fields = set(allowed_fields)

                # набор полей, которые мы разрешаем менять после сабмита:
                allowed_service_fields = {
                    "subreal",          # линковка позиции к созданной передаче
                    "updated_at",
                    "gift_quantity",    # фиксируем подарок
                    "total_quantity",   # фиксируем итоговую выдачу
                    "price_snapshot",   # фиксируем цену на момент заявки
                }

                # если ВСЕ поля, которые хотят сохранить — из разрешённого списка,
                # то даём сохранить без full_clean (чтобы не упасть на статусе).
                if allowed_fields.issubset(allowed_service_fields):
                    # если апдейтим price_snapshot после submit — приводим к денежному формату (2 знака)
                    if "price_snapshot" in allowed_fields:
                        self.price_snapshot = _money(self.price_snapshot)
                    return super().save(*args, **kwargs)

                # кто-то пытается поменять product, quantity_requested и т.д.
                raise ValidationError("Редактирование позиций после отправки запрещено.")

            # если update_fields не задан (т.е. кто-то делает .save() без ограничений) — не даём
            raise ValidationError("Редактирование позиций после отправки запрещено.")

        # ===== 3. Корзина APPROVED / REJECTED -> вообще нельзя трогать
        if cart_status in (AgentRequestCart.Status.APPROVED, AgentRequestCart.Status.REJECTED):
            raise ValidationError("Заявка уже обработана. Изменения позиций запрещены.")

        # safety net
        raise ValidationError(f"Нельзя сохранить позицию при статусе {cart_status!r}.")


class KnowledgeBaseCourse(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField("Название курса", max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Курс базы знаний"
        verbose_name_plural = "Курсы базы знаний"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["title"]),
        ]

    def __str__(self):
        return self.title


class KnowledgeBaseLesson(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course = models.ForeignKey(
        KnowledgeBaseCourse,
        on_delete=models.CASCADE,
        related_name="lessons",
        verbose_name="Курс",
    )
    title = models.CharField("Название урока", max_length=255)
    description = models.TextField("Описание", blank=True)
    url = models.URLField("Ссылка на урок", max_length=500)

    # Кастомное превью. Хранится либо файлом (админ загрузил картинку), либо
    # ссылкой (админ указал URL). Если оба пустые — фронт строит превью сам
    # из YouTube/Vimeo по полю url.
    thumbnail = models.ImageField(
        "Превью (файл)",
        upload_to="knowledge-base/thumbnails/%Y/%m/",
        blank=True,
        null=True,
    )
    thumbnail_url = models.URLField(
        "Превью (ссылка)",
        max_length=500,
        blank=True,
        default="",
    )

    order = models.PositiveIntegerField("Порядок", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def preview_url(self) -> str:
        """Готовая ссылка на превью или пустая строка."""
        if self.thumbnail:
            return self.thumbnail.url
        return self.thumbnail_url or ""

    class Meta:
        verbose_name = "Урок базы знаний"
        verbose_name_plural = "Уроки базы знаний"
        ordering = ["order", "created_at"]
        indexes = [
            models.Index(fields=["course", "order"]),
        ]

    def __str__(self):
        return f"{self.course.title} — {self.title}"


class FinishedToRawTransfer(models.Model):
    """
    Частичное перемещение готовой продукции обратно в сырьё (переработка/разборка).

    Списывает указанное количество из Product.quantity и добавляет столько же в
    сопоставленную запись ItemMake (сырьё). Операцию можно отменить (возврат
    количества в готовую продукцию и списание из сырья).
    """

    class Status(models.TextChoices):
        DONE = "done", "Выполнено"
        CANCELED = "canceled", "Отменено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="finished_to_raw_transfers",
        verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="crm_finished_to_raw_transfers",
        null=True, blank=True, db_index=True, verbose_name="Филиал",
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="finished_to_raw_transfers",
        verbose_name="Готовая продукция",
    )
    raw_item = models.ForeignKey(
        ItemMake, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="finished_to_raw_transfers", verbose_name="Сырьё",
    )
    quantity = models.DecimalField(
        "Количество", max_digits=14, decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    reason = models.TextField("Причина", blank=True, default="")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DONE, db_index=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finished_to_raw_transfers", verbose_name="Кто выполнил",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    canceled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="canceled_finished_to_raw_transfers", verbose_name="Кто отменил",
    )

    class Meta:
        verbose_name = "Перемещение готовой продукции в сырьё"
        verbose_name_plural = "Перемещения готовой продукции в сырьё"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["product"]),
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["company", "created_at"]),
        ]

    def __str__(self):
        return f"{self.product_id} → сырьё · {self.quantity} ({self.status})"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})
        if self.product_id and self.product.company_id != self.company_id:
            raise ValidationError({"product": "Товар принадлежит другой компании."})


class Inventory(models.Model):
    """
    Сессия инвентаризации (сверка учётного и фактического остатка).

    warehouse = finished_goods → позиции ссылаются на Product;
    warehouse = raw_materials  → позиции ссылаются на ItemMake.
    Подтверждение приводит учётный остаток к фактическому.
    """

    class Warehouse(models.TextChoices):
        FINISHED_GOODS = "finished_goods", "Готовая продукция"
        RAW_MATERIALS = "raw_materials", "Сырьё"

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        CONFIRMED = "confirmed", "Подтверждена"
        CANCELED = "canceled", "Отменена"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="inventories", verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="crm_inventories",
        null=True, blank=True, db_index=True, verbose_name="Филиал",
    )
    warehouse = models.CharField("Склад", max_length=20, choices=Warehouse.choices, db_index=True)
    status = models.CharField(
        "Статус", max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True,
    )
    comment = models.TextField("Комментарий", blank=True, default="")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="inventories", verbose_name="Ответственный",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    surplus_qty = models.DecimalField("Излишек", max_digits=14, decimal_places=3, default=Decimal("0.000"))
    shortage_qty = models.DecimalField("Недостача", max_digits=14, decimal_places=3, default=Decimal("0.000"))

    class Meta:
        verbose_name = "Инвентаризация"
        verbose_name_plural = "Инвентаризации"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["warehouse", "status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["company", "status", "created_at"]),
        ]

    def __str__(self):
        return f"Инвентаризация {self.warehouse} · {self.status} · {self.created_at:%Y-%m-%d}"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})

    def recompute_totals(self, *, save=True):
        agg = self.items.aggregate(
            surplus=Coalesce(Sum("diff", filter=Q(diff__gt=0)), Value(Decimal("0.000"))),
            shortage=Coalesce(Sum("diff", filter=Q(diff__lt=0)), Value(Decimal("0.000"))),
        )
        self.surplus_qty = agg["surplus"] or Decimal("0.000")
        # shortage хранится положительным числом
        self.shortage_qty = abs(agg["shortage"] or Decimal("0.000"))
        if save:
            super().save(update_fields=["surplus_qty", "shortage_qty"])


class InventoryItem(models.Model):
    """Строка сверки. object_id ссылается на Product или ItemMake (по Inventory.warehouse)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inventory = models.ForeignKey(
        Inventory, on_delete=models.CASCADE, related_name="items", verbose_name="Инвентаризация",
    )
    object_id = models.UUIDField("ID товара/сырья", db_index=True)
    product_name = models.CharField("Название (снимок)", max_length=255, blank=True, default="")
    qty_system = models.DecimalField("Учётный остаток", max_digits=14, decimal_places=3)
    qty_fact = models.DecimalField("Фактический остаток", max_digits=14, decimal_places=3)
    diff = models.DecimalField("Расхождение", max_digits=14, decimal_places=3, default=Decimal("0.000"))

    class Meta:
        verbose_name = "Позиция инвентаризации"
        verbose_name_plural = "Позиции инвентаризации"
        indexes = [
            models.Index(fields=["inventory"]),
            models.Index(fields=["object_id"]),
        ]

    def __str__(self):
        return f"{self.product_name or self.object_id}: {self.qty_system} → {self.qty_fact} ({self.diff})"


class StockShortageEvent(models.Model):
    """
    Событие нехватки готовой продукции при попытке выдачи/передачи.
    Журнал + источник уведомлений ответственным сотрудникам.
    """

    class Source(models.TextChoices):
        TRANSFER = "transfer", "Передача"
        REQUEST = "request", "Заявка"
        SALE = "sale", "Продажа"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="stock_shortage_events", verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="crm_stock_shortage_events",
        null=True, blank=True, db_index=True, verbose_name="Филиал",
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="stock_shortage_events", verbose_name="Товар",
    )
    requested_qty = models.DecimalField("Запрошено", max_digits=14, decimal_places=3)
    available_qty = models.DecimalField("Доступно", max_digits=14, decimal_places=3)
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stock_shortage_events_as_agent", verbose_name="Агент",
    )
    source = models.CharField("Источник", max_length=16, choices=Source.choices, default=Source.TRANSFER)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="stock_shortage_events", verbose_name="Инициатор",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Событие нехватки склада"
        verbose_name_plural = "События нехватки склада"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["product"]),
            models.Index(fields=["company", "created_at"]),
        ]

    def __str__(self):
        return f"Нехватка {self.product_id}: запрошено {self.requested_qty}, доступно {self.available_qty}"

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})
        if self.product_id and self.product.company_id != self.company_id:
            raise ValidationError({"product": "Товар принадлежит другой компании."})


class StockMovement(models.Model):
    """
    Неизменяемый журнал движения склада (центральный аудит остатков).
    Записи создаёт бэкенд внутри транзакций операций; UI — только чтение.

    object_id ссылается на Product или ItemMake (по warehouse). Отображаемые
    имена (product_name/source_name/...) — снимки на момент записи, чтобы журнал
    оставался самодостаточным и неизменяемым.
    """

    class Type(models.TextChoices):
        INCOME = "income", "Приход"
        EXPENSE = "expense", "Расход"
        TRANSFER = "transfer", "Перемещение"
        RETURN = "return", "Возврат"
        WRITEOFF = "writeoff", "Списание"
        INVENTORY = "inventory", "Инвентаризация"
        ADJUSTMENT = "adjustment", "Корректировка"
        AGENT_TRANSFER = "agent_transfer", "Передача агенту"
        AGENT_RETURN = "agent_return", "Возврат от агента"
        STAFF_TRANSFER = "staff_transfer", "Передача между сотрудниками"
        STAFF_RETURN = "staff_return", "Возврат между сотрудниками"

    class Warehouse(models.TextChoices):
        FINISHED_GOODS = "finished_goods", "Готовая продукция"
        RAW_MATERIALS = "raw_materials", "Сырьё"
        AGENT = "agent", "Агент"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="stock_movements", verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="crm_stock_movements",
        null=True, blank=True, db_index=True, verbose_name="Филиал",
    )
    type = models.CharField("Тип операции", max_length=24, choices=Type.choices, db_index=True)

    object_id = models.UUIDField("ID товара/сырья", db_index=True)
    product_name = models.CharField("Товар (снимок)", max_length=255, blank=True, default="")
    warehouse = models.CharField(
        "Склад", max_length=20, choices=Warehouse.choices, null=True, blank=True, db_index=True,
    )

    qty_before = models.DecimalField("Остаток до", max_digits=14, decimal_places=3)
    change = models.DecimalField("Изменение", max_digits=14, decimal_places=3)
    qty_after = models.DecimalField("Остаток после", max_digits=14, decimal_places=3)

    source_type = models.CharField(max_length=20, null=True, blank=True)
    source_id = models.UUIDField(null=True, blank=True)
    source_name = models.CharField(max_length=255, blank=True, default="")
    target_type = models.CharField(max_length=20, null=True, blank=True)
    target_id = models.UUIDField(null=True, blank=True)
    target_name = models.CharField(max_length=255, blank=True, default="")

    sender_id = models.UUIDField(null=True, blank=True, db_index=True)
    sender_name = models.CharField(max_length=255, blank=True, default="")
    receiver_id = models.UUIDField(null=True, blank=True, db_index=True)
    receiver_name = models.CharField(max_length=255, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="stock_movements", verbose_name="Автор",
    )
    created_by_name = models.CharField(max_length=255, blank=True, default="")
    comment = models.TextField("Комментарий", blank=True, default="")

    ref_type = models.CharField("Источник (сущность)", max_length=40, blank=True, default="")
    ref_id = models.UUIDField("Источник (id)", null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Движение склада"
        verbose_name_plural = "Журнал движения склада"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["type"]),
            models.Index(fields=["object_id"]),
            models.Index(fields=["warehouse"]),
            models.Index(fields=["sender_id"]),
            models.Index(fields=["receiver_id"]),
        ]

    def __str__(self):
        return f"{self.type} {self.product_name or self.object_id}: {self.change}"


def _user_display_name(user) -> str:
    if user is None:
        return ""
    full = f"{(getattr(user, 'first_name', '') or '').strip()} {(getattr(user, 'last_name', '') or '').strip()}".strip()
    return full or getattr(user, "email", "") or ""


def record_stock_movement(*, company, type, object_id, created_by,
                          qty_before, change, qty_after,
                          branch=None, product_name="", warehouse=None,
                          source_type=None, source_id=None, source_name="",
                          target_type=None, target_id=None, target_name="",
                          sender=None, sender_id=None, sender_name="",
                          receiver=None, receiver_id=None, receiver_name="",
                          comment="", ref_type="", ref_id=None):
    """
    Пишет запись в журнал движения склада. Вызывать ВНУТРИ транзакции операции.
    sender/receiver можно передать как объект User (тогда id/имя извлекаются).
    """
    if sender is not None:
        sender_id = getattr(sender, "pk", None)
        sender_name = sender_name or _user_display_name(sender)
    if receiver is not None:
        receiver_id = getattr(receiver, "pk", None)
        receiver_name = receiver_name or _user_display_name(receiver)
    return StockMovement.objects.create(
        company=company,
        branch=branch,
        type=type,
        object_id=object_id,
        product_name=product_name or "",
        warehouse=warehouse,
        qty_before=qty_before,
        change=change,
        qty_after=qty_after,
        source_type=source_type,
        source_id=source_id,
        source_name=source_name or "",
        target_type=target_type,
        target_id=target_id,
        target_name=target_name or "",
        sender_id=sender_id,
        sender_name=sender_name or "",
        receiver_id=receiver_id,
        receiver_name=receiver_name or "",
        created_by=created_by,
        created_by_name=_user_display_name(created_by),
        comment=comment or "",
        ref_type=ref_type or "",
        ref_id=ref_id,
    )


class ProductionRecord(models.Model):
    """
    Журнал производства ГП из сырья (вкладка «Производство» на складе).

    Записи создаёт бэкенд при изготовлении товара по рецепту; UI — только чтение.
    Имена товара/автора — снимки на момент производства, чтобы журнал оставался
    самодостаточным при переименовании и удалении.

    Закупка готового товара (без рецепта) производством не считается и сюда не пишется.
    """

    class Shift(models.TextChoices):
        DAY = "day", "День"
        NIGHT = "night", "Ночь"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_records", verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="crm_production_records",
        null=True, blank=True, db_index=True, verbose_name="Филиал",
    )

    product = models.ForeignKey(
        "main.Product", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="production_records", verbose_name="Товар",
    )
    product_name = models.CharField("Товар (снимок)", max_length=255, blank=True, default="")

    quantity = models.DecimalField("Произведено", max_digits=14, decimal_places=3)
    unit = models.CharField("Единица", max_length=32, blank=True, default="")
    cost_total = models.DecimalField(
        "Себестоимость (сумма списанного сырья)", max_digits=12, decimal_places=2,
        default=Decimal("0.00"),
    )

    produced_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="production_records", verbose_name="Автор",
    )
    produced_by_name = models.CharField("Автор (снимок)", max_length=255, blank=True, default="")

    produced_at = models.DateTimeField("Дата производства", default=timezone.now, db_index=True)
    shift = models.CharField("Смена", max_length=8, choices=Shift.choices, db_index=True)

    class Meta:
        verbose_name = "Производство"
        verbose_name_plural = "Журнал производства"
        ordering = ["-produced_at"]
        indexes = [
            models.Index(fields=["company", "produced_at"]),
            models.Index(fields=["company", "shift"]),
        ]

    def __str__(self):
        return f"{self.product_name or self.product_id}: {self.quantity} ({self.get_shift_display()})"


# Границы смен в местном времени компании: день 08:00:00–19:59:59, остальное — ночь.
DAY_SHIFT_START_HOUR = 8
DAY_SHIFT_END_HOUR = 20


def production_shift_for(dt) -> str:
    """Смена по местному времени: 08:00–19:59:59 — день, иначе ночь."""
    local_hour = timezone.localtime(dt).hour
    if DAY_SHIFT_START_HOUR <= local_hour < DAY_SHIFT_END_HOUR:
        return ProductionRecord.Shift.DAY
    return ProductionRecord.Shift.NIGHT


def record_production(*, company, product, quantity, produced_by,
                      branch=None, unit="", cost_total=Decimal("0.00"), produced_at=None):
    """
    Пишет запись в журнал производства. Вызывать ВНУТРИ транзакции операции.
    Смена вычисляется от produced_at по местному времени.

    Здесь же начисляется сдельная зарплата: это единственная точка, через которую
    проходит любое производство, поэтому хук стоит тут, а не в каждой вьюхе.
    """
    moment = produced_at or timezone.now()
    record = ProductionRecord.objects.create(
        company=company,
        branch=branch,
        product=product,
        product_name=getattr(product, "name", "") or "",
        quantity=quantity,
        unit=unit or getattr(product, "unit", "") or "",
        cost_total=cost_total or Decimal("0.00"),
        produced_by=produced_by,
        produced_by_name=_user_display_name(produced_by),
        produced_at=moment,
        shift=production_shift_for(moment),
    )

    # Локальный импорт: services зависят от models, обратная связь только здесь.
    from apps.main.production_salary_services import create_piece_accrual

    create_piece_accrual(record)
    return record


class SupplierPurchase(models.Model):
    """
    Журнал закупок у поставщика (история отношений в карточке поставщика).

    Пишется в момент операции снимком: сырьё (ItemMake), докупка сырья,
    закупка готового товара (create-manual с поставщиком), оприходование магазина.
    Последующее изменение товара историю не меняет.
    """

    class PaymentType(models.TextChoices):
        CASH = "cash", "Наличные"
        TRANSFER = "transfer", "Перевод"
        DEBT = "debt", "Долг"
        PREPAYMENT = "prepayment", "Предоплата"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="supplier_purchases", verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="crm_supplier_purchases",
        null=True, blank=True, db_index=True, verbose_name="Филиал",
    )
    supplier = models.ForeignKey(
        "main.Client", on_delete=models.CASCADE, related_name="purchases",
        db_index=True, verbose_name="Поставщик",
    )

    # Что закуплено: товар или сырьё (одно из двух; имя — снимок).
    product = models.ForeignKey(
        "main.Product", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supplier_purchases", verbose_name="Товар",
    )
    item_make = models.ForeignKey(
        "main.ItemMake", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supplier_purchases", verbose_name="Сырьё",
    )
    product_name = models.CharField("Наименование (снимок)", max_length=255, blank=True, default="")

    quantity = models.DecimalField("Количество", max_digits=14, decimal_places=3)
    unit = models.CharField("Единица", max_length=50, blank=True, default="")
    unit_price = models.DecimalField("Цена за единицу", max_digits=12, decimal_places=2, default=Decimal("0.00"))
    amount = models.DecimalField("Сумма", max_digits=14, decimal_places=2, default=Decimal("0.00"))

    payment_type = models.CharField(
        "Тип оплаты", max_length=12, choices=PaymentType.choices,
        default=PaymentType.CASH, db_index=True,
    )
    purchased_at = models.DateTimeField("Дата закупки", default=timezone.now, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supplier_purchases_created", verbose_name="Кто оформил",
    )

    class Meta:
        verbose_name = "Закупка у поставщика"
        verbose_name_plural = "История закупок у поставщиков"
        ordering = ["-purchased_at"]
        indexes = [
            models.Index(fields=["company", "supplier", "purchased_at"]),
        ]

    def __str__(self):
        return f"{self.product_name}: {self.quantity} × {self.unit_price}"


def record_supplier_purchase(*, company, supplier, quantity, unit_price, created_by,
                             branch=None, product=None, item_make=None, product_name="",
                             unit="", payment_type=None, purchased_at=None):
    """
    Пишет строку в журнал закупок. Вызывать ВНУТРИ транзакции операции.
    Без поставщика или с нулевым количеством запись не создаётся.
    """
    if supplier is None:
        return None

    qty = Decimal(str(quantity or 0))
    if qty <= 0:
        return None

    price = Decimal(str(unit_price or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    source = product or item_make
    return SupplierPurchase.objects.create(
        company=company,
        branch=branch,
        supplier=supplier,
        product=product,
        item_make=item_make,
        product_name=product_name or getattr(source, "name", "") or "",
        quantity=qty,
        unit=unit or getattr(source, "unit", "") or "",
        unit_price=price,
        amount=(qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        payment_type=payment_type or SupplierPurchase.PaymentType.CASH,
        purchased_at=purchased_at or timezone.now(),
        created_by=created_by,
    )


# ─────────────────────────────────────────────────────────────
# Зарплата в производстве: почасовой оклад + сдельная оплата
# ─────────────────────────────────────────────────────────────
class ProductionEmployeeRate(models.Model):
    """Почасовая ставка сотрудника (сом/час)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_employee_rates", verbose_name="Компания",
    )
    employee = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="production_rate", verbose_name="Сотрудник",
    )
    hourly_rate = models.DecimalField(
        "Ставка, сом/час", max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="production_rates_updated", verbose_name="Кем изменено",
    )

    class Meta:
        verbose_name = "Ставка сотрудника (производство)"
        verbose_name_plural = "Ставки сотрудников (производство)"

    def __str__(self):
        return f"{_user_display_name(self.employee)}: {self.hourly_rate}/час"


class ProductionPieceRate(models.Model):
    """Сдельная ставка за единицу произведённого товара (сом/шт)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_piece_rates", verbose_name="Компания",
    )
    product = models.ForeignKey(
        "main.Product", on_delete=models.CASCADE, related_name="piece_rates", verbose_name="Товар",
    )
    amount_per_unit = models.DecimalField(
        "Сдельно, сом/ед.", max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="production_piece_rates_updated", verbose_name="Кем изменено",
    )

    class Meta:
        verbose_name = "Сдельная ставка (производство)"
        verbose_name_plural = "Сдельные ставки (производство)"
        constraints = [
            models.UniqueConstraint(fields=["company", "product"], name="uq_production_piece_rate"),
        ]

    def __str__(self):
        return f"{self.product_id}: {self.amount_per_unit}/ед."


class ProductionWorkSession(models.Model):
    """Табель: сколько часов сотрудник отработал в конкретный день."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_work_sessions", verbose_name="Компания",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="production_work_sessions", verbose_name="Сотрудник",
    )
    date = models.DateField("Рабочий день", db_index=True)
    hours = models.DecimalField(
        "Отработано часов", max_digits=6, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(Decimal("24"))],
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="production_work_sessions_created", verbose_name="Кто внёс",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Табель (производство)"
        verbose_name_plural = "Табель (производство)"
        ordering = ["-date", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["company", "employee", "date"], name="uq_production_work_session_day"),
        ]
        indexes = [
            models.Index(fields=["company", "date"]),
        ]

    def __str__(self):
        return f"{_user_display_name(self.employee)} {self.date}: {self.hours} ч"


class ProductionSalaryAccrual(models.Model):
    """
    Начисление зарплаты: за часы (hourly) или за выработку (piece).

    Ставки хранятся снимком на момент начисления — изменение ставки
    не пересчитывает прошлые начисления.
    """

    class Kind(models.TextChoices):
        HOURLY = "hourly", "За часы"
        PIECE = "piece", "Сдельно"

    class Status(models.TextChoices):
        ACCRUED = "accrued", "Начислено"
        PAID = "paid", "Выплачено"
        CANCELED = "canceled", "Отменено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_salary_accruals", verbose_name="Компания",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="production_salary_accruals", verbose_name="Сотрудник",
    )
    kind = models.CharField("Тип", max_length=10, choices=Kind.choices, db_index=True)

    # hourly
    work_session = models.ForeignKey(
        ProductionWorkSession, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="accruals", verbose_name="Табель",
    )
    hours = models.DecimalField("Часы", max_digits=6, decimal_places=2, null=True, blank=True)
    rate = models.DecimalField("Ставка, сом/час (снимок)", max_digits=10, decimal_places=2, null=True, blank=True)

    # piece
    production_record = models.ForeignKey(
        ProductionRecord, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="salary_accruals", verbose_name="Производство",
    )
    product = models.ForeignKey(
        "main.Product", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="production_salary_accruals", verbose_name="Товар",
    )
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3, null=True, blank=True)
    amount_per_unit = models.DecimalField(
        "Сдельно, сом/ед. (снимок)", max_digits=10, decimal_places=2, null=True, blank=True,
    )

    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(
        "Статус", max_length=10, choices=Status.choices, default=Status.ACCRUED, db_index=True,
    )
    payout = models.ForeignKey(
        "main.ProductionSalaryPayout", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="accruals", verbose_name="Выплата",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Начисление зарплаты (производство)"
        verbose_name_plural = "Начисления зарплаты (производство)"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "employee", "status"]),
            models.Index(fields=["company", "created_at"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {_user_display_name(self.employee)}: {self.amount}"


class ProductionSalaryPayout(models.Model):
    """Выплата зарплаты: закрывает начисления FIFO и создаёт расход кассы."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_salary_payouts", verbose_name="Компания",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="production_salary_payouts", verbose_name="Сотрудник",
    )
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)
    cashbox = models.ForeignKey(
        "construction.Cashbox", on_delete=models.PROTECT,
        related_name="production_salary_payouts", verbose_name="Касса",
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="production_salary_payouts_created", verbose_name="Кто выплатил",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Выплата зарплаты (производство)"
        verbose_name_plural = "Выплаты зарплаты (производство)"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "employee", "created_at"]),
        ]

    def __str__(self):
        return f"Зарплата {_user_display_name(self.employee)}: {self.amount}"


class MarketSalaryPayout(models.Model):
    """ТЗ Н-18: выплата зарплаты магазина (оклад/аванс/премия/удержание). Один расход кассы на выплату."""

    class Kind(models.TextChoices):
        SALARY = "salary", "Зарплата"
        ADVANCE = "advance", "Аванс"
        BONUS = "bonus", "Премия"
        DEDUCTION = "deduction", "Удержание"
        FINE = "fine", "Штраф"  # ТЗ 26.10 Б-63: запись начисления, денег не двигает (как и bonus)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="market_salary_payouts", verbose_name="Компания",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="market_salary_payouts", verbose_name="Сотрудник",
    )
    kind = models.CharField("Вид", max_length=16, choices=Kind.choices, default=Kind.SALARY, db_default="salary")
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)
    period_from = models.DateField("Период с", null=True, blank=True)
    period_to = models.DateField("Период по", null=True, blank=True)
    # ТЗ 26.10 Б-63: дата штрафа/премии (для fine/bonus); у остальных пусто
    date = models.DateField("Дата записи", null=True, blank=True)
    method = models.CharField("Способ", max_length=32, blank=True, default="", db_default="")
    cashbox = models.ForeignKey(
        "construction.Cashbox", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="market_salary_payouts", verbose_name="Касса",
    )
    shift = models.ForeignKey(
        "construction.CashShift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="market_salary_payouts", verbose_name="Смена",
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True, default="", db_default="")
    cashflow = models.ForeignKey(
        "construction.CashFlow", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Расход",
    )
    reversal_cashflow = models.ForeignKey(
        "construction.CashFlow", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Обратное движение",
    )
    reversed_at = models.DateTimeField("Отменена", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="market_salary_payouts_created", verbose_name="Кто выплатил",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Выплата зарплаты (магазин)"
        verbose_name_plural = "Выплаты зарплаты (магазин)"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "employee", "created_at"], name="main_market_company_6b1f0e_idx")]

    def __str__(self):
        return f"{self.get_kind_display()} {_user_display_name(self.employee)}: {self.amount}"


class PosPrinterSetting(models.Model):
    """
    POS: синхронизация ESC/POS-конфига принтера чеков per-device (или per-branch workstation).
    Хранит настройки шрифта, ширины бумаги, code page, денежного ящика и т.д.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="pos_printer_settings",
        verbose_name="Компания",
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pos_printer_settings",
        verbose_name="Филиал",
        db_index=True,
    )
    cashbox = models.ForeignKey(
        "construction.Cashbox",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pos_printer_settings",
        verbose_name="Касса",
        db_index=True,
    )
    device_key = models.CharField(
        max_length=128,
        db_index=True,
        verbose_name="Идентификатор устройства",
    )
    settings = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Настройки ESC/POS принтера",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        verbose_name = "Настройка POS-принтера"
        verbose_name_plural = "Настройки POS-принтеров"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "device_key"],
                name="uq_pos_printer_setting_company_device_key",
            )
        ]
        indexes = [
            models.Index(fields=["company", "device_key"]),
            models.Index(fields=["company", "branch"]),
        ]

    def __str__(self):
        return f"POS Printer {self.device_key} ({self.company.name})"


class ClientBonusTransaction(models.Model):
    """Движение бонусов клиента. Баланс — Client.bonus_balance (меняется только здесь)."""

    class Reason(models.TextChoices):
        EARN = "earn", "Начисление"
        REDEEM = "redeem", "Списание"
        MANUAL = "manual", "Ручная корректировка"
        EXPIRE = "expire", "Сгорание"  # ТЗ 29.10 Б-99: срока бонусов в настройках пока нет, значение зарезервировано

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="bonus_transactions")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="bonus_transactions")
    sale = models.ForeignKey(
        Sale, on_delete=models.SET_NULL, null=True, blank=True, related_name="bonus_transactions"
    )
    delta = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=16, choices=Reason.choices)
    note = models.CharField(max_length=255, blank=True, default="")
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # ТЗ 29.10 Б-99: где начислили/списали
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cashbox = models.ForeignKey(
        "construction.Cashbox", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["client", "created_at"]), models.Index(fields=["company", "created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="uniq_bonus_tx_idempotency",
            )
        ]
        verbose_name = "Движение бонусов"
        verbose_name_plural = "Движения бонусов"


# =====================================================================
#  Касса NurMarket, этап 3: варианты, склады, запись, заказ-наряды, обмен
# =====================================================================

class ProductVariant(models.Model):
    """Размер/цвет товара. Если у товара есть варианты, Product.quantity = сумма их остатков."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="product_variants")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    size = models.CharField("Размер", max_length=32, blank=True, default="")
    color = models.CharField("Цвет", max_length=64, blank=True, default="")
    # ТЗ Н-26: цвет в виде #RRGGBB (необязательно) и ручной порядок (0 = по умолчанию, по размерам)
    color_hex = models.CharField("Цвет (#RRGGBB)", max_length=7, blank=True, default="", db_default="")
    sort_order = models.IntegerField("Порядок", default=0, db_default=0)
    barcode = models.CharField("Штрихкод", max_length=64, null=True, blank=True)
    quantity = models.DecimalField("Остаток", max_digits=12, decimal_places=3, default=Decimal("0"))
    price = models.DecimalField("Цена", max_digits=12, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("product", "size", "color")
        verbose_name = "Вариант товара"
        verbose_name_plural = "Варианты товаров"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "barcode"],
                condition=models.Q(barcode__isnull=False) & ~models.Q(barcode=""),
                name="uniq_variant_company_barcode",
            ),
            models.UniqueConstraint(fields=["product", "size", "color"], name="uniq_variant_product_size_color"),
        ]

    @property
    def effective_price(self):
        return self.price if self.price is not None else self.product.price

    def __str__(self):
        return f"{self.product_id} {self.size} {self.color}".strip()


class ProductStock(models.Model):
    """Остаток товара (или варианта) на складе компании. Остаток «в магазине» — Product.quantity."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="product_stocks")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name="product_stocks")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="warehouse_stocks")
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE, null=True, blank=True, related_name="warehouse_stocks"
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Остаток на складе"
        verbose_name_plural = "Остатки на складах"
        constraints = [
            models.UniqueConstraint(
                fields=["warehouse", "product"],
                condition=models.Q(variant__isnull=True),
                name="uniq_stock_warehouse_product",
            ),
            models.UniqueConstraint(
                fields=["warehouse", "variant"],
                condition=models.Q(variant__isnull=False),
                name="uniq_stock_warehouse_variant",
            ),
        ]


class MarketStockTransfer(models.Model):
    """Перемещение между складами. from/to = null — торговый зал магазина (Product.quantity)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="market_stock_transfers")
    from_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, null=True, blank=True, related_name="transfers_out"
    )
    to_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, null=True, blank=True, related_name="transfers_in"
    )
    note = models.CharField(max_length=255, blank=True, default="")
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Перемещение товара"
        verbose_name_plural = "Перемещения товара"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="uniq_stock_transfer_idempotency",
            )
        ]


class MarketStockTransferItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transfer = models.ForeignKey(MarketStockTransfer, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)


class BranchTransfer(models.Model):
    """
    Перемещение товаров между складами / филиалами с накладной (ТОРГ-13).
    Склад-источник или склад-получатель null означает главный склад компании.
    """
    class Status(models.TextChoices):
        COMPLETED = "completed", "Проведено"
        CANCELLED = "cancelled", "Отменено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="branch_transfers",
        verbose_name="Компания",
    )
    number = models.CharField("Номер накладной", max_length=32)
    status = models.CharField(
        "Статус",
        max_length=16,
        choices=Status.choices,
        default=Status.COMPLETED,
        db_index=True,
    )
    date = models.DateField("Дата документа", db_index=True)
    from_branch = models.ForeignKey(
        Branch,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="transfers_outgoing",
        verbose_name="Склад-отправитель",
    )
    to_branch = models.ForeignKey(
        Branch,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="transfers_incoming",
        verbose_name="Склад-получатель",
    )
    comment = models.CharField("Комментарий", max_length=500, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Создал",
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True, db_index=True)
    cancelled_at = models.DateTimeField("Отменено в", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Отменил",
    )
    cancel_reason = models.CharField("Причина отмены", max_length=500, blank=True, default="")
    total_quantity = models.DecimalField("Всего количество", max_digits=14, decimal_places=3, default=Decimal("0"))
    total_amount = models.DecimalField("Общая сумма", max_digits=14, decimal_places=2, default=Decimal("0"))
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = "Перемещение между филиалами"
        verbose_name_plural = "Перемещения между филиалами"
        ordering = ["-date", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["company", "number"], name="uniq_transfer_number"),
            models.UniqueConstraint(
                fields=["company", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False) & ~models.Q(idempotency_key=""),
                name="uniq_transfer_company_idempotency_key",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "-date", "-created_at"]),
            models.Index(fields=["company", "from_branch"]),
            models.Index(fields=["company", "to_branch"]),
            models.Index(fields=["company", "status"]),
        ]

    def __str__(self):
        return f"{self.number} ({self.get_status_display()})"


class BranchTransferItem(models.Model):
    """
    Позиция накладной перемещения ТОРГ-13 (снимок данных товара на момент проведения).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transfer = models.ForeignKey(
        BranchTransfer,
        related_name="items",
        on_delete=models.CASCADE,
        verbose_name="Перемещение",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="transfer_items_as_source",
        verbose_name="Товар-источник",
    )
    dest_product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="transfer_items_as_dest",
        verbose_name="Товар-получатель",
    )
    name = models.CharField("Название товара", max_length=255)
    article = models.CharField("Артикул", max_length=64, blank=True, default="")
    barcode = models.CharField("Штрихкод", max_length=64, blank=True, default="")
    unit = models.CharField("Единица измерения", max_length=32, blank=True, default="шт")
    quantity = models.DecimalField("Количество", max_digits=14, decimal_places=3)
    price = models.DecimalField("Учётная цена", max_digits=14, decimal_places=2)
    amount = models.DecimalField("Сумма", max_digits=14, decimal_places=2)

    class Meta:
        verbose_name = "Позиция перемещения"
        verbose_name_plural = "Позиции перемещения"
        ordering = ["name", "id"]

    def __str__(self):
        return f"{self.name} x {self.quantity}"


class MarketAppointment(models.Model):
    """Запись клиента на услугу к мастеру (салоны, мастерские)."""

    class Status(models.TextChoices):
        BOOKED = "booked", "Записан"
        CAME = "came", "Пришёл"
        CANCELED = "canceled", "Отменена"
        NO_SHOW = "no_show", "Не пришёл"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="market_appointments")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="market_appointments")
    service = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="appointments")
    performer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="market_appointments"
    )
    start = models.DateTimeField()
    end = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.BOOKED)
    note = models.CharField(max_length=500, blank=True, default="")
    cart = models.ForeignKey(Cart, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("start",)
        indexes = [
            models.Index(fields=["company", "start"]),
            models.Index(fields=["performer", "start"]),
        ]
        verbose_name = "Запись на услугу"
        verbose_name_plural = "Записи на услуги"


class WorkOrder(models.Model):
    """Заказ-наряд (ремонт/мастерская): приём → в работе → готов → выдан (продажа)."""

    class Status(models.TextChoices):
        ACCEPTED = "accepted", "Принят"
        IN_WORK = "in_work", "В работе"
        READY = "ready", "Готов"
        ISSUED = "issued", "Выдан"
        CANCELED = "canceled", "Отменён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="work_orders")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    number = models.PositiveIntegerField()
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="work_orders")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACCEPTED)
    items = models.JSONField(default=list, help_text="[{product|custom,name,qty,price,discount,performer}]")
    description = models.TextField(blank=True, default="")
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    prepayment = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    prepayment_method = models.CharField(max_length=32, blank=True, default="")
    sale = models.ForeignKey(Sale, on_delete=models.SET_NULL, null=True, blank=True, related_name="work_orders")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    issued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["company", "number"], name="uniq_work_order_number"),
        ]
        verbose_name = "Заказ-наряд"
        verbose_name_plural = "Заказ-наряды"


class SaleExchange(models.Model):
    """Обмен: возврат части чека + новый чек, деньгами проходит только разница."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="sale_exchanges")
    original_sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="exchanges_out")
    new_sale = models.ForeignKey(Sale, on_delete=models.PROTECT, null=True, blank=True, related_name="exchanges_in")
    sale_return = models.ForeignKey(SaleReturn, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    returned_amount = models.DecimalField(max_digits=12, decimal_places=2)
    new_amount = models.DecimalField(max_digits=12, decimal_places=2)
    difference = models.DecimalField(max_digits=12, decimal_places=2)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["company", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="uniq_sale_exchange_idempotency",
            )
        ]
        verbose_name = "Обмен"
        verbose_name_plural = "Обмены"


class Rental(models.Model):
    """Прокат вещей с залогом (деньги или документ)."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Выдан"
        RETURNED = "returned", "Возвращён"

    class DepositType(models.TextChoices):
        MONEY = "money", "Деньги"
        DOCUMENT = "document", "Документ"

    class DepositMethod(models.TextChoices):
        CASH = "cash", "Наличные"
        TRANSFER = "transfer", "Перевод"
        CARD = "card", "Карта"

    class Condition(models.TextChoices):
        OK = "ok", "Без повреждений"
        DAMAGED = "damaged", "Повреждено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="rentals")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    number = models.PositiveIntegerField()
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="rentals")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    date_from = models.DateField()
    date_to = models.DateField()
    tariff = models.CharField(max_length=255, blank=True, default="")
    rent_amount = models.DecimalField("Стоимость проката", max_digits=12, decimal_places=2, default=Decimal("0.00"), blank=True)
    sale = models.ForeignKey("Sale", on_delete=models.SET_NULL, null=True, blank=True, related_name="rentals", verbose_name="Продажа проката")
    deposit_type = models.CharField(max_length=16, choices=DepositType.choices, default=DepositType.MONEY)
    deposit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    deposit_method = models.CharField(max_length=32, choices=DepositMethod.choices, blank=True, default=DepositMethod.CASH)
    deposit_document = models.CharField(max_length=255, blank=True, default="")
    condition = models.CharField(max_length=16, choices=Condition.choices, blank=True, default="")
    penalty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    penalty_sale = models.ForeignKey(Sale, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    note = models.CharField(max_length=500, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    # ТЗ Н-21: пеня за день просрочки, касса и смена выдачи
    late_penalty_per_day = models.DecimalField(
        "Пеня за день просрочки", max_digits=12, decimal_places=2, default=Decimal("0.00"), db_default=Decimal("0.00")
    )
    cashbox = models.ForeignKey("construction.Cashbox", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    shift = models.ForeignKey("construction.CashShift", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="uniq_rental_number")]
        indexes = [models.Index(fields=["company", "status", "date_to"])]
        verbose_name = "Прокат"
        verbose_name_plural = "Прокаты"


class RentalItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rental = models.ForeignKey(Rental, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("1"))
    returned_quantity = models.DecimalField("Возвращено", max_digits=12, decimal_places=3, default=Decimal("0"))


class RentalExtension(models.Model):
    """ТЗ Н-21: продление проката (новая дата возврата и доплата)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rental = models.ForeignKey(Rental, on_delete=models.CASCADE, related_name="extensions")
    old_date_to = models.DateField()
    new_date_to = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    payment_method = models.CharField(max_length=32, blank=True, default="cash")
    shift = models.ForeignKey("construction.CashShift", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["rental", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="uniq_rental_extension_idem",
            )
        ]


class ClientDocument(models.Model):
    """ТЗ Н-22: клиентский документ — КП, счёт, накладная (номер выдаёт сервер, удаление мягкое)."""

    class Type(models.TextChoices):
        QUOTE = "quote", "Коммерческое предложение"
        INVOICE = "invoice", "Счёт"
        WAYBILL = "waybill", "Накладная"

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        SENT = "sent", "Отправлен"
        ACCEPTED = "accepted", "Принят"
        PAID = "paid", "Оплачен"
        CANCELED = "canceled", "Отменён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="client_documents")
    type = models.CharField(max_length=16, choices=Type.choices)
    number = models.PositiveIntegerField()
    client = models.ForeignKey(Client, on_delete=models.PROTECT, null=True, blank=True, related_name="documents")
    lines = models.JSONField(default=list, blank=True)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    valid_until = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    note = models.CharField(max_length=500, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False, db_default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=["company", "type", "number"], name="uniq_client_doc_number")]
        indexes = [models.Index(fields=["company", "type", "status"], name="main_clientdoc_idx")]


class ClientDocumentPayment(models.Model):
    """ТЗ Н-22: оплата по документу (ключ идемпотентности хранится 7+ суток — вечно)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(ClientDocument, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=32, blank=True, default="cash")
    shift = models.ForeignKey("construction.CashShift", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["document", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="uniq_client_doc_pay_idem",
            )
        ]


class ProductDeletion(models.Model):
    """BE2-19: удалённые товары — касса убирает их из своего каталога при быстрой синхронизации."""

    id = models.BigAutoField(primary_key=True)
    # Без ограничения внешнего ключа: запись создаётся и при каскадном удалении самой компании.
    company = models.ForeignKey(
        Company, on_delete=models.DO_NOTHING, db_constraint=False, related_name="product_deletions"
    )
    product_id = models.UUIDField()
    deleted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["company", "deleted_at"], name="main_proddel_company_idx")]


class MarketCustomerDisplaySettings(models.Model):
    """
    POS: настройки второго экрана покупателя (per-company singleton).
    Показывает приветствие, промо-слайды и тему на дисплее покупателя.
    """
    class Theme(models.TextChoices):
        DARK = "dark", "dark"
        LIGHT = "light", "light"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        related_name="customer_display_settings",
        verbose_name="Компания",
        db_index=True,
    )
    enabled = models.BooleanField(
        default=False,
        verbose_name="Показывать ли функцию второго экрана",
    )
    welcome_text = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="Текст приветствия",
    )
    theme = models.CharField(
        max_length=16,
        choices=Theme.choices,
        default=Theme.DARK,
        verbose_name="Тема экрана",
    )
    slides = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Промо-слайды",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Кто последний менял",
    )

    class Meta:
        verbose_name = "Настройка экрана покупателя"
        verbose_name_plural = "Настройки экранов покупателей"

    def __str__(self):
        return f"CustomerDisplaySettings ({self.company_id})"


# ======================================================================
# Showcase Visual Editor Models (SC-01 - SC-11)
# ======================================================================

def default_showcase_theme():
    return {
        "background": "#FFFFFF",
        "text": "#111827",
        "accent": "#F5CD15",
        "header_bg": "#111827",
        "header_text": "#FFFFFF",
        "footer_bg": "#F3F4F6",
        "card_bg": "#FFFFFF",
        "price": "#B45309",
        "font": "Inter",
        "radius": 12,
        "mode": "light",
        "preset": None,
    }


def default_showcase_layout():
    return {
        "sections": ["banners", "promos", "categories", "featured", "all_products"],
        "columns": {"desktop": 4, "mobile": 2},
        "default_sort": "new",
        "pinned_products": [],
        "hidden_products": [],
        "hidden_categories": [],
        "category_order": [],
        "product_order": [],
    }


def default_showcase_cards():
    return {
        "template": "standard",
        "show": {
            "photo": True,
            "old_price": True,
            "stock": "low_only",
            "unit": True,
            "discount_badge": True,
            "add_button": True,
        },
        "photo_ratio": "1:1",
        "shadow": True,
        "border": False,
    }


def default_showcase_carousel():
    return {
        "autoplay": True,
        "interval_s": 5,
    }


def default_showcase_brand():
    return {
        "logo": None,
        "title": "",
        "slogan": "",
        "favicon": None,
    }


def default_showcase_footer():
    return {
        "phone": "",
        "address": "",
        "hours": "",
        "socials": {
            "instagram": "",
            "whatsapp": "",
        },
    }


def get_legacy_default_showcase_design():
    """Документ по умолчанию ТЗ-BE-2026-03 (нужен только для апгрейда старых документов)."""
    return {
        "theme": default_showcase_theme(),
        "layout": default_showcase_layout(),
        "cards": default_showcase_cards(),
        "carousel": default_showcase_carousel(),
        "brand": default_showcase_brand(),
        "footer": default_showcase_footer(),
    }


def get_default_showcase_design():
    """Документ вида витрины по умолчанию (ТЗ-BE-2026-05, п. 5.1/5.2) = текущий вид витрины."""
    from apps.main.showcase.design_schema import default_document

    return default_document()


def showcase_media_upload_to(instance, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "webp"
    return f"showcase_media/{instance.company_id}/{uuid.uuid4().hex}.{ext}"


class ShowcaseDesign(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        related_name="showcase_design",
        verbose_name="Компания",
    )
    draft = models.JSONField("Черновик", default=get_default_showcase_design)
    published = models.JSONField("Опубликованный дизайн", default=get_default_showcase_design)
    version = models.PositiveIntegerField("Версия", default=1)
    published_at = models.DateTimeField("Дата публикации", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Дизайн витрины"
        verbose_name_plural = "Дизайны витрин"

    def __str__(self):
        return f"ShowcaseDesign({self.company_id}, v{self.version})"


class ShowcaseDesignVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="showcase_design_versions",
        verbose_name="Компания",
    )
    version = models.PositiveIntegerField("Номер версии")
    snapshot = models.JSONField("Снимок настроек")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Автор публикации",
    )
    published_at = models.DateTimeField("Дата публикации", default=timezone.now)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = "Версия дизайна витрины"
        verbose_name_plural = "Версии дизайна витрин"
        unique_together = ("company", "version")
        ordering = ["-version"]

    def __str__(self):
        return f"ShowcaseDesignVersion({self.company_id}, v{self.version})"


class ShowcaseMedia(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="showcase_media",
        verbose_name="Компания",
    )
    class Kind(models.TextChoices):
        LOGO = "logo", "Логотип"
        BANNER = "banner", "Баннер"
        COVER = "cover", "Обложка"
        FAVICON = "favicon", "Favicon"
        OG = "og", "Картинка для соцсетей"
        CATEGORY = "category", "Категория"
        OTHER = "other", "Другое"

    kind = models.CharField("Назначение", max_length=16, choices=Kind.choices, default=Kind.OTHER, db_index=True)
    file = models.FileField(upload_to=showcase_media_upload_to, verbose_name="Оригинал/Файл")
    urls = models.JSONField("URLs по размерам", default=dict)
    size = models.PositiveIntegerField("Размер файла, байт", default=0)
    width = models.PositiveIntegerField("Ширина", default=0)
    height = models.PositiveIntegerField("Высота", default=0)
    content_type = models.CharField("Тип контента", max_length=64, default="image/webp")
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Медиафайл витрины"
        verbose_name_plural = "Медиафайлы витрины"
        ordering = ["-created_at"]

    def __str__(self):
        return f"ShowcaseMedia({self.id})"


class ShowcaseBanner(models.Model):
    class Place(models.TextChoices):
        HERO = "hero", "Сверху-карусель (Hero)"
        INLINE = "inline", "Между рядами (Inline)"
        SIDEBAR = "sidebar", "Сбоку (Sidebar)"
        POPUP = "popup", "Всплывающий (Popup)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="showcase_banners",
        verbose_name="Компания",
    )
    # title/subtitle — текст на ru (совместимость со старым API); тексты по языкам — в *_i18n.
    title = models.CharField("Заголовок", max_length=255, blank=True, default="")
    subtitle = models.CharField("Подпись", max_length=255, blank=True, default="")
    title_i18n = models.JSONField("Заголовок (по языкам)", default=dict, blank=True)
    subtitle_i18n = models.JSONField("Подпись (по языкам)", default=dict, blank=True)
    button_text = models.JSONField("Текст кнопки (по языкам)", default=dict, blank=True)
    inline_after_row = models.PositiveSmallIntegerField("После какого ряда (inline)", null=True, blank=True)
    image = models.ForeignKey(
        ShowcaseMedia,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Изображение (десктоп)",
    )
    image_mobile = models.ForeignKey(
        ShowcaseMedia,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Изображение (мобильное)",
    )
    link = models.JSONField("Куда ведёт", default=dict)
    place = models.CharField(
        "Место",
        max_length=32,
        choices=Place.choices,
        default=Place.HERO,
    )
    starts_at = models.DateTimeField("Начало показа", null=True, blank=True)
    ends_at = models.DateTimeField("Конец показа", null=True, blank=True)
    active = models.BooleanField("Активен", default=True)
    position = models.PositiveIntegerField("Порядок", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Рекламный баннер витрины"
        verbose_name_plural = "Рекламные баннеры витрины"
        ordering = ["position", "created_at"]

    def __str__(self):
        return f"ShowcaseBanner({self.title or self.id})"


class ShowcasePromoBlock(models.Model):
    class Style(models.TextChoices):
        CAROUSEL = "carousel", "Лента (Carousel)"
        GRID = "grid", "Сетка (Grid)"
        HERO_PRODUCT = "hero_product", "Один большой товар (Hero Product)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="showcase_promo_blocks",
        verbose_name="Компания",
    )
    title = models.CharField("Заголовок", max_length=255, blank=True, default="")
    title_i18n = models.JSONField("Заголовок (по языкам)", default=dict, blank=True)
    source = models.JSONField("Источник", default=dict)
    background = models.CharField("Фон блока", max_length=7, null=True, blank=True)
    title_color = models.CharField("Цвет заголовка", max_length=7, null=True, blank=True)
    style = models.CharField(
        "Вид блока",
        max_length=32,
        choices=Style.choices,
        default=Style.CAROUSEL,
    )
    show_timer = models.BooleanField("Таймер до конца акции", default=True)
    max_items = models.PositiveIntegerField("Максимум товаров", default=12)
    position = models.PositiveIntegerField("Порядок", default=0)
    active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Промо-блок витрины"
        verbose_name_plural = "Промо-блоки витрины"
        ordering = ["position", "created_at"]

    def __str__(self):
        return f"ShowcasePromoBlock({self.title})"


class ShowcaseOrder(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новый"
        ACCEPTED = "accepted", "Принят"
        READY = "ready", "Готов"
        DONE = "done", "Выполнен"
        CANCELED = "canceled", "Отменён"

    class DeliveryType(models.TextChoices):
        PICKUP = "pickup", "Самовывоз"
        DELIVERY = "delivery", "Доставка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="showcase_orders",
        verbose_name="Компания",
    )
    number = models.PositiveIntegerField("Номер заказа")
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=Status.choices,
        default=Status.NEW,
    )
    customer_name = models.CharField("Имя клиента", max_length=128)
    customer_phone = models.CharField("Телефон клиента", max_length=64)
    delivery_type = models.CharField(
        "Тип доставки",
        max_length=32,
        choices=DeliveryType.choices,
        default=DeliveryType.PICKUP,
    )
    delivery_address = models.CharField("Адрес доставки", max_length=255, blank=True, default="")
    comment = models.TextField("Комментарий", blank=True, default="")
    total = models.DecimalField("Итоговая сумма", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    delivery_fee = models.DecimalField("Стоимость доставки", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    source = models.CharField("Источник", max_length=32, default="showcase", blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    # ТЗ Б-22: для source=telegram_bot — чат покупателя в Telegram
    telegram_chat_id = models.CharField("Telegram chat id покупателя", max_length=64, blank=True, default="", db_default="")
    # Остаток зарезервирован (списан) при создании заказа; снимается при отмене
    # или при выдаче через кассу (sale), т.к. продажа на кассе списывает сама.
    stock_reserved = models.BooleanField("Остаток зарезервирован", default=False)
    sale = models.ForeignKey(
        "Sale",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Продажа на кассе",
    )
    # ТЗ Н-33: код выдачи при самовывозе (4–6 цифр), генерируется при создании
    pickup_code = models.CharField("Код выдачи", max_length=8, blank=True, default="", db_default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self._state.adding and not self.pickup_code:
            import secrets as _secrets

            self.pickup_code = "".join(_secrets.choice("0123456789") for _ in range(_secrets.choice((4, 5, 6))))
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "Заказ с витрины"
        verbose_name_plural = "Заказы с витрины"
        unique_together = ("company", "number")
        ordering = ["-created_at"]

    def __str__(self):
        return f"ShowcaseOrder #{self.number} ({self.customer_name})"


class ShowcaseOrderItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        ShowcaseOrder,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Заказ",
    )
    product = models.ForeignKey(
        "Product",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Товар",
    )
    variant = models.ForeignKey(
        "ProductVariant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Вариант товара",
    )
    product_name = models.CharField("Название товара", max_length=255)
    qty = models.DecimalField("Количество", max_digits=12, decimal_places=3, default=Decimal("1.000"))
    price = models.DecimalField("Цена за единицу", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    discount = models.DecimalField("Скидка", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField("Сумма позиции", max_digits=14, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        verbose_name = "Позиция заказа с витрины"
        verbose_name_plural = "Позиции заказа с витрины"

    def __str__(self):
        return f"{self.product_name} x {self.qty}"


class ShowcaseStockRequest(models.Model):
    """ТЗ Н-27: «Сообщить о поступлении» — заявка покупателя на вариант товара, которого нет в наличии."""

    class Status(models.TextChoices):
        OPEN = "open", "Ждёт поступления"
        READY = "ready", "Поступило (покупателю нужно сообщить)"
        NOTIFIED = "notified", "Покупателю сообщили"
        CANCELED = "canceled", "Отменена"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="showcase_stock_requests")
    product = models.ForeignKey("Product", on_delete=models.CASCADE, related_name="+")
    variant = models.ForeignKey("ProductVariant", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    phone = models.CharField("Телефон", max_length=64, blank=True, default="", db_default="")
    telegram_chat_id = models.CharField("Telegram chat id", max_length=64, blank=True, default="", db_default="")
    client_ip = models.CharField("IP", max_length=64, blank=True, default="", db_default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_default="open", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Заявка «сообщить о поступлении»"
        verbose_name_plural = "Заявки «сообщить о поступлении»"
        indexes = [models.Index(fields=["company", "status"], name="idx_sc_stockreq_co_status")]

    def __str__(self):
        return f"{self.product_id} {self.variant_id} {self.phone or self.telegram_chat_id}"


class ShowcaseStats(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="showcase_stats",
        verbose_name="Компания",
    )
    date = models.DateField("Дата", db_index=True)
    views = models.PositiveIntegerField("Просмотры витрины", default=0)
    add_to_cart = models.PositiveIntegerField("Добавлений в корзину", default=0)
    product_views = models.JSONField("Просмотры товаров", default=dict)
    banner_clicks = models.JSONField("Клики по баннерам", default=dict)

    class Meta:
        verbose_name = "Статистика витрины"
        verbose_name_plural = "Статистика витрины"
        unique_together = ("company", "date")
        ordering = ["-date"]

    def __str__(self):
        return f"ShowcaseStats({self.company_id}, {self.date})"


class ShowcasePage(models.Model):
    """Текстовые страницы витрины («О нас», «Доставка»…) — ТЗ-BE-2026-05, п. 6.9. Черновое состояние."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="showcase_pages", verbose_name="Компания")
    slug = models.SlugField("Адрес страницы", max_length=50)
    title = models.JSONField("Заголовок (по языкам)", default=dict, blank=True)
    body = models.JSONField("Текст (по языкам, ограниченная разметка)", default=dict, blank=True)
    show_in_footer = models.BooleanField("Показывать в подвале", default=True)
    position = models.PositiveIntegerField("Порядок", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Страница витрины"
        verbose_name_plural = "Страницы витрины"
        ordering = ["position", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=("company", "slug"), name="uq_showcase_page_company_slug"),
        ]

    def __str__(self):
        return f"ShowcasePage({self.company_id}, {self.slug})"


class ShowcaseProductSettings(models.Model):
    """Скрыть / закрепить / ручной порядок / бейдж товара на витрине (п. 6.4). Черновое состояние."""

    class Badge(models.TextChoices):
        HIT = "hit", "Хит"
        SALE = "sale", "Скидка"
        NEW = "new", "Новинка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="showcase_product_settings", verbose_name="Компания")
    product = models.ForeignKey("Product", on_delete=models.CASCADE, related_name="showcase_settings", verbose_name="Товар")
    hidden = models.BooleanField("Скрыт на витрине", default=False)
    pinned = models.BooleanField("Закреплён", default=False)
    sort_order = models.PositiveIntegerField("Ручной порядок", null=True, blank=True)
    badge = models.CharField("Бейдж", max_length=16, choices=Badge.choices, null=True, blank=True)
    # Опубликованное состояние (копируется из полей выше при publish) — его читают публичные адреса.
    published_hidden = models.BooleanField("Скрыт (опубликовано)", default=False, db_index=True)
    published_pinned = models.BooleanField("Закреплён (опубликовано)", default=False)
    published_sort_order = models.PositiveIntegerField("Порядок (опубликовано)", null=True, blank=True)
    published_badge = models.CharField("Бейдж (опубликовано)", max_length=16, choices=Badge.choices, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройка товара на витрине"
        verbose_name_plural = "Настройки товаров на витрине"
        constraints = [
            models.UniqueConstraint(fields=("company", "product"), name="uq_showcase_product_settings"),
        ]

    def __str__(self):
        return f"ShowcaseProductSettings({self.product_id})"


class ShowcaseCategorySettings(models.Model):
    """Порядок, видимость, картинка и своё название категории на витрине (п. 6.5). Черновое состояние."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="showcase_category_settings", verbose_name="Компания")
    category = models.ForeignKey("ProductCategory", on_delete=models.CASCADE, related_name="showcase_settings", verbose_name="Категория")
    hidden = models.BooleanField("Скрыта на витрине", default=False)
    sort_order = models.PositiveIntegerField("Порядок", null=True, blank=True)
    image = models.ForeignKey(
        ShowcaseMedia, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="Картинка",
    )
    title_override = models.JSONField("Своё название (по языкам)", null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройка категории на витрине"
        verbose_name_plural = "Настройки категорий на витрине"
        constraints = [
            models.UniqueConstraint(fields=("company", "category"), name="uq_showcase_category_settings"),
        ]

    def __str__(self):
        return f"ShowcaseCategorySettings({self.category_id})"


class ShowcasePreviewToken(models.Model):
    """Ссылка предпросмотра черновика (п. 3.2): живёт 24 ч, потом 410."""

    token = models.CharField(primary_key=True, max_length=64)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="showcase_preview_tokens", verbose_name="Компания")
    expires_at = models.DateTimeField("Действует до", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Токен предпросмотра витрины"
        verbose_name_plural = "Токены предпросмотра витрины"

    def __str__(self):
        return f"ShowcasePreviewToken({self.company_id})"


class ShowcaseSlugRedirect(models.Model):
    """Редирект со старого slug витрины на новый (п. 6.11), действует 90 дней."""

    old_slug = models.CharField("Старый slug", max_length=80, unique=True)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="showcase_slug_redirects", verbose_name="Компания")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField("Действует до", db_index=True)

    class Meta:
        verbose_name = "Редирект slug витрины"
        verbose_name_plural = "Редиректы slug витрины"

    def __str__(self):
        return f"{self.old_slug} -> {self.company_id}"


SHOWCASE_SLUG_REDIRECT_DAYS = 90


def _showcase_slug_redirect_on_company_save(sender, instance, **kwargs):
    """Смена Company.slug → старый slug ведёт на новый 90 дней (п. 6.11)."""
    if not instance.pk or not instance.slug:
        return
    try:
        old_slug = sender.objects.filter(pk=instance.pk).values_list("slug", flat=True).first()
    except Exception:
        return
    if not old_slug or old_slug == instance.slug:
        return
    try:
        # Новый slug больше не может быть чьим-то редиректом.
        ShowcaseSlugRedirect.objects.filter(old_slug__iexact=instance.slug).delete()
        ShowcaseSlugRedirect.objects.update_or_create(
            old_slug=old_slug,
            defaults={
                "company_id": instance.pk,
                "expires_at": timezone.now() + timedelta(days=SHOWCASE_SLUG_REDIRECT_DAYS),
            },
        )
    except Exception:
        logging.getLogger(__name__).exception("showcase slug redirect save failed")


models.signals.pre_save.connect(
    _showcase_slug_redirect_on_company_save,
    sender=Company,
    dispatch_uid="showcase_slug_redirect_on_company_save",
)


class RecommendationEvent(models.Model):
    class EventType(models.TextChoices):
        SHOWN = "shown", "Показано"
        ACCEPTED = "accepted", "Принято"
        SKIPPED = "skipped", "Пропущено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="recommendation_events",
        db_index=True,
        verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendation_events",
        verbose_name="Филиал",
    )
    client_event_id = models.UUIDField("ID события кассы")
    event = models.CharField("Тип события", max_length=16, choices=EventType.choices)
    product = models.ForeignKey(
        "Product",
        on_delete=models.CASCADE,
        related_name="recommendation_events",
        verbose_name="Рекомендованный товар",
    )
    trigger_product_ids = models.JSONField("Товары в чеке", default=list, blank=True)
    cart_id = models.CharField("ID корзины", max_length=64, blank=True, null=True)
    sale = models.ForeignKey(
        "Sale",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendation_events",
        verbose_name="Продажа",
    )
    # id продажи от кассы — хранится всегда, даже если продажа (офлайн-чек) ещё не на сервере;
    # FK `sale` проставляется, когда продажа появится (recommendations.resolve_pending_sales).
    sale_ref = models.UUIDField("ID продажи от кассы", null=True, blank=True)
    price = models.DecimalField("Цена предложения", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    score = models.FloatField("Скор рекомендации", default=0.0)
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendation_events",
        verbose_name="Кассир",
    )
    device_id = models.CharField("ID кассы", max_length=128, blank=True, null=True)
    occurred_at = models.DateTimeField("Время события на кассе", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Событие рекомендации (допродажи)"
        verbose_name_plural = "События рекомендаций (допродаж)"
        unique_together = ("company", "client_event_id")
        indexes = [
            models.Index(fields=["company", "occurred_at"]),
            models.Index(fields=["company", "sale"]),
            models.Index(fields=["company", "cart_id"]),
            models.Index(fields=["company", "sale_ref"]),
        ]

    def __str__(self):
        return f"RecommendationEvent({self.company_id}, {self.event}, {self.product_id})"


class RecommendationPairsCache(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        related_name="recommendation_pairs_cache",
        verbose_name="Компания",
    )
    computed_at = models.DateTimeField("Время расчёта")
    pairs_data = models.JSONField("Данные пар", default=list)
    etag = models.CharField("ETag", max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Кэш пар товаров для допродажи"
        verbose_name_plural = "Кэши пар товаров для допродажи"


class SalesTarget(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="sales_targets",
        verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="sales_targets",
        verbose_name="Филиал",
    )
    month = models.CharField("Месяц (YYYY-MM)", max_length=7, db_index=True)
    revenue_target = models.DecimalField("Целевая выручка", max_digits=14, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "План продаж"
        verbose_name_plural = "Планы продаж"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "branch", "month"],
                condition=Q(branch__isnull=False),
                name="uniq_sales_target_company_branch_month",
            ),
            # NULL в unique не сравнивается — отдельное ограничение для плана на всю компанию.
            models.UniqueConstraint(
                fields=["company", "month"],
                condition=Q(branch__isnull=True),
                name="uniq_sales_target_company_month_no_branch",
            ),
        ]

    def __str__(self):
        return f"SalesTarget({self.company_id}, {self.month}: {self.revenue_target})"


# ТЗ Н-31: лента изменений для касс (monotonic id, хранится 7 дней)
class ChangeEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="change_events")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    type = models.CharField("Тип", max_length=48)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    entity = models.CharField("Сущность", max_length=48, blank=True, default="")
    entity_id = models.CharField("ID сущности", max_length=64, blank=True, default="")
    data = models.JSONField("Данные", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Событие изменения"
        verbose_name_plural = "Лента изменений"
        indexes = [models.Index(fields=["company", "id"], name="chgev_company_id_idx")]

    def __str__(self):
        return f"ChangeEvent({self.id}, {self.type})"


# ТЗ Н-31: запрос «на кассу» (владелец просит кассира выполнить операцию)
class PosRequest(models.Model):
    class Kind(models.TextChoices):
        DEBT_PAYMENT = "debt_payment", "Оплата долга клиента"
        SALARY_PAYOUT = "salary_payout", "Выплата зарплаты"
        SUPPLIER_PAYMENT = "supplier_payment", "Оплата поставщику"
        RENTAL_PAYMENT = "rental_payment", "Оплата проката"
        CASH_OUT = "cash_out", "Изъятие из кассы"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        ACCEPTED = "accepted", "Принят"
        DECLINED = "declined", "Отклонён"
        CANCELED = "canceled", "Отменён"
        EXPIRED = "expired", "Истёк"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="pos_requests")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    cashbox = models.ForeignKey("construction.Cashbox", on_delete=models.CASCADE, related_name="+", verbose_name="Касса-адресат")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    payload = models.JSONField(default=dict, blank=True)
    comment = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    shift = models.ForeignKey("construction.CashShift", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    result = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Запрос на кассу"
        verbose_name_plural = "Запросы на кассу"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "status", "cashbox"], name="posreq_comp_st_cb_idx")]

    def __str__(self):
        return f"PosRequest({self.kind}, {self.status})"

# ---------------------------------------------------------------------------
# ТЗ Н-32: медиа-хранилище и электронный чек; Н-33: таблица размеров
# ---------------------------------------------------------------------------

def main_media_upload_to(instance, filename):
    return f"main_media/{instance.company_id}/{instance.id.hex}_{filename}"[:240]


class MainMedia(models.Model):
    """ТЗ Н-32 (1): файл компании (тема, фото товара, накладная), POST api/main/media/."""

    class Purpose(models.TextChoices):
        THEME = "theme", "Тема"
        PRODUCT = "product", "Товар"
        INVOICE = "invoice", "Накладная"
        OTHER = "other", "Другое"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="main_media")
    purpose = models.CharField(max_length=16, choices=Purpose.choices, default=Purpose.OTHER, db_index=True)
    file = models.FileField(upload_to=main_media_upload_to, max_length=255)
    size = models.PositiveIntegerField(default=0)
    content_type = models.CharField(max_length=64, default="")
    sha256 = models.CharField(max_length=64, default="", db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Файл компании"
        verbose_name_plural = "Файлы компании"
        ordering = ["-created_at"]


def _new_receipt_token():
    import secrets as _secrets

    return _secrets.token_urlsafe(24)


class SaleReceiptLink(models.Model):
    """ТЗ Н-32 (2): публичная ссылка на электронный чек (одна на продажу)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sale = models.OneToOneField(Sale, on_delete=models.CASCADE, related_name="public_link")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
    token = models.CharField(max_length=64, unique=True, default=_new_receipt_token)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Публичная ссылка на чек"
        verbose_name_plural = "Публичные ссылки на чеки"


class SaleReceiptRating(models.Model):
    """ТЗ Н-32 (2): оценка электронного чека покупателем (один раз на чек)."""

    link = models.OneToOneField(SaleReceiptLink, on_delete=models.CASCADE, related_name="rating")
    score = models.PositiveSmallIntegerField()
    comment = models.TextField(blank=True, default="")
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Оценка чека"
        verbose_name_plural = "Оценки чеков"


class SizeChart(models.Model):
    """ТЗ Н-33: таблица размеров витрины (showcase/size-charts/)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="size_charts")
    name = models.CharField(max_length=128)
    columns = models.JSONField(default=list, blank=True)
    rows = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Таблица размеров"
        verbose_name_plural = "Таблицы размеров"
        ordering = ["name"]


from apps.main.telegram_bot.models import (
    TelegramBotSettings,
    TelegramInquiry,
    TelegramCustomerProfile,
    TelegramProcessedUpdate,
    TelegramMessageLog,
)



# ТЗ 26.10 (Б-52…Б-87): модели новых разделов лежат в apps/main/tz26/*/models.py
from apps.main.tz26.models import *  # noqa: E402,F401,F403


# ==========================
# ТЗ 29.10 Б-98: отметки тестировщиков мобилки
# ==========================
class AppTestCheck(models.Model):
    """Отметка «проверено/проблема» по пункту чек-листа на устройстве. Ключ (company, version, item_id, device_id)."""

    STATUS_CHOICES = (("ok", "Проверено"), ("issue", "Проблема"))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="app_test_checks")
    version = models.CharField(max_length=32)
    item_id = models.CharField(max_length=128)
    device_id = models.CharField(max_length=8)
    name = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=8, choices=STATUS_CHOICES)
    comment = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "version", "item_id", "device_id"], name="uniq_app_test_check"),
        ]
        indexes = [models.Index(fields=["company", "version"])]


# ==========================
# ТЗ 29.10 Б-100: инвесторы и выплаты
# ==========================
class Investor(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="investors")
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=32, blank=True, default="")
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0, verbose_name="Вложение")
    date = models.DateField(null=True, blank=True, verbose_name="Дата вложения")
    terms = models.JSONField(default=dict, blank=True, verbose_name="Условия (например {\"percent\": 20})")
    version = models.PositiveIntegerField(default=1)
    effective_from = models.DateField(null=True, blank=True, verbose_name="Действует с")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "created_at")


class InvestorPayout(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="investor_payouts")
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name="payouts")
    month = models.DateField(verbose_name="Месяц (первое число)")
    accrued = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid_date = models.DateField(null=True, blank=True)
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-month", "-created_at")
        indexes = [models.Index(fields=["company", "month"])]
