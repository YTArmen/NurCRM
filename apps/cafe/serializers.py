# apps/cafe/serializers.py
import re
from django.contrib.auth import get_user_model
from django.db.models import Q
from decimal import Decimal
from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction, IntegrityError
from django.utils import timezone

from apps.cafe.models import (
    Zone, Table, Booking, Warehouse, Purchase,
    Category, MenuItem, Ingredient,
    Order, OrderItem, CafeClient,
    OrderHistory, OrderItemHistory, KitchenTask, NotificationCafe, InventorySession, InventoryItem, Equipment, EquipmentInventoryItem, EquipmentInventorySession, Kitchen,
    CafeReceiptPrinterSettings,
    CafeExpense, CafeExpenseCategory, CafeWaiterPayProfile,
    CafeHouseholdItem, CafeHouseholdMovement, CafeHouseholdInventorySession, CafeHouseholdInventoryLine,
    Preparation, PreparationIngredient, PreparationProcessing, ProcessingType,
    DishIngredient, DishIngredientProcessing,
)
from apps.users.models import Branch
from apps.utils import _is_owner_like

User = get_user_model()


def _cafe_resolve_user_company(request):
    """Компания пользователя — как CompanyBranchQuerysetMixin._user_company во views."""
    user = getattr(request, "user", None) if request else None
    if not user or not getattr(user, "is_authenticated", False):
        return None
    company = getattr(user, "company", None) or getattr(user, "owned_company", None)
    if company:
        return company
    br = getattr(user, "branch", None)
    if br is not None:
        return getattr(br, "company", None)
    return None


# ===== company/branch mixin (как в барбере/букинге) =====
class CompanyBranchReadOnlyMixin(serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    def _user(self):
        return getattr(self.context.get("request"), "user", None)

    def _user_company(self):
        return _cafe_resolve_user_company(self.context.get("request"))

    def _auto_branch(self):
        """
        Активный филиал:
          0) ?branch=<uuid> в запросе (если филиал принадлежит компании пользователя)
          1) user.primary_branch() / user.primary_branch
          2) request.branch (если есть)
          3) None

        Всегда проверяем, что branch.company_id == company.id.
        """
        request = self.context.get("request")
        user = self._user()
        company = self._user_company()
        comp_id = getattr(company, "id", None)

        if not request or not user or not comp_id:
            return None

        # 0) ?branch=<uuid> в query-параметрах
        branch_id = None
        if hasattr(request, "query_params"):
            branch_id = request.query_params.get("branch")
        elif hasattr(request, "GET"):
            branch_id = request.GET.get("branch")

        if branch_id:
            try:
                br = Branch.objects.get(id=branch_id, company_id=comp_id)
                # Зафиксируем на request, чтобы в остальных местах можно было использовать request.branch
                setattr(request, "branch", br)
                return br
            except (Branch.DoesNotExist, ValueError):
                # если кривой/чужой/несуществующий branch — тихо игнорируем и идём дальше
                pass

        # 1) primary_branch как метод
        primary = getattr(user, "primary_branch", None)
        if callable(primary):
            try:
                val = primary()
                if val and getattr(val, "company_id", None) == comp_id:
                    return val
            except Exception:
                pass

        # 1b) primary_branch как атрибут
        if primary and getattr(primary, "company_id", None) == comp_id:
            return primary

        # 2) request.branch, если кто-то уже проставил (мидлварь и т.п.)
        if hasattr(request, "branch"):
            b = getattr(request, "branch")
            if b and getattr(b, "company_id", None) == comp_id:
                return b

        # 3) глобальный режим
        return None

    def create(self, validated_data):
        company = self._user_company()
        if not company:
            raise serializers.ValidationError("Невозможно определить компанию пользователя.")
        validated_data["company"] = company

        # если у модели есть branch — проставим
        if "branch" in getattr(self.Meta, "fields", []):
            auto_branch = self._auto_branch()
            validated_data["branch"] = auto_branch if auto_branch is not None else None
        return super().create(validated_data)

    def update(self, instance, validated_data):
        company = self._user_company()
        if company:
            validated_data["company"] = company
        # не перетираем branch, если не смогли определить
        if "branch" in getattr(self.Meta, "fields", []):
            auto_branch = self._auto_branch()
            if auto_branch is not None:
                validated_data["branch"] = auto_branch
        return super().update(instance, validated_data)


def _scope_queryset_by_context(qs, serializer: CompanyBranchReadOnlyMixin):
    if not hasattr(serializer, "_user_company"):
        return qs.none()
    company = serializer._user_company()
    if not company:
        return qs.none()
    qs = qs.filter(company=company)

    # было: if hasattr(qs.model, "branch"):
    has_branch = any(getattr(f, "name", None) == "branch" for f in qs.model._meta.get_fields())
    if has_branch:
        b = serializer._auto_branch()
        if b is not None:
            qs = qs.filter(Q(branch=b) | Q(branch__isnull=True))
        else:
            qs = qs.filter(branch__isnull=True)
    return qs



class KitchenTaskSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    table_number = serializers.SerializerMethodField()
    guest = serializers.SerializerMethodField()
    waiter_label = serializers.SerializerMethodField()
    menu_item_title = serializers.CharField(source='menu_item.title', read_only=True)
    price = serializers.DecimalField(
        source="menu_item.price",
        max_digits=11,
        decimal_places=3,
        read_only=True,
    )

    class Meta:
        model = KitchenTask
        fields = [
            'id', 'company', 'branch',
            'status', 'created_at', 'started_at', 'finished_at',
            'order', 'order_item', 'menu_item',
            'table_number', 'guest', 'waiter', 'waiter_label',
            'cook', 'unit_index', 'quantity', 'menu_item_title', 'price',
            'menu_item_is_sold_by_weight', 'menu_item_sale_unit',
        ]
        read_only_fields = [
            'id', 'company', 'branch', 'created_at', 'started_at', 'finished_at',
            'order', 'order_item', 'menu_item', 'table_number', 'guest',
            'waiter', 'waiter_label', 'menu_item_title', 'price',
            'menu_item_is_sold_by_weight', 'menu_item_sale_unit',
        ]

    menu_item_is_sold_by_weight = serializers.BooleanField(
        source="menu_item.is_sold_by_weight", read_only=True, default=False,
    )
    menu_item_sale_unit = serializers.CharField(
        source="menu_item.sale_unit", read_only=True, default="kg",
    )

    def get_table_number(self, obj):
        return obj.order.table.number if obj.order_id and obj.order.table_id else None

    def get_guest(self, obj):
        return getattr(obj.order, 'client', None) and (obj.order.client.name or obj.order.client.phone) or ''

    def get_waiter_label(self, obj):
        w = obj.waiter
        if not w:
            return ''
        fn = getattr(w, 'get_full_name', lambda: '')() or ''
        return fn or getattr(w, 'email', '') or str(w.pk)

def _strip_null_bytes(value):
    """Убирает нуль-байты из строки (Django/CharField их не допускает)."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


class NullByteSafeCharField(serializers.CharField):
    """CharField, убирающий нуль-байты до валидации и при выводе (избегает ошибки Django)."""

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = data.replace("\x00", "")
        return super().to_internal_value(data)

    def to_representation(self, value):
        if value is not None and isinstance(value, str):
            value = value.replace("\x00", "")
        return super().to_representation(value)


class KitchenSerializer(CompanyBranchReadOnlyMixin):
    printer = NullByteSafeCharField(required=False, allow_blank=True, max_length=255)

    class Meta:
        model = Kitchen
        fields = ["id", "company", "branch", "title", "number", "printer"]
        read_only_fields = ["id", "company", "branch"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "printer" in data and data["printer"] is not None:
            data["printer"] = _strip_null_bytes(data["printer"])
        return data

    def validate(self, attrs):
        title = (attrs.get("title") or getattr(self.instance, "title", "") or "").strip()
        if not title:
            raise serializers.ValidationError({"title": "Название кухни обязательно."})

        number = attrs.get("number", getattr(self.instance, "number", None))
        if self.instance is None and number is None:
            raise serializers.ValidationError({"number": "Обязательное поле."})

        company = self._user_company()
        if not company:
            raise serializers.ValidationError("Невозможно определить компанию пользователя.")

        auto_branch = self._auto_branch()
        branch_to_use = (
            auto_branch
            if auto_branch is not None
            else getattr(self.instance, "branch", None)
        )

        qs = Kitchen.objects.filter(company=company)
        if branch_to_use is None:
            qs = qs.filter(branch__isnull=True)
        else:
            qs = qs.filter(branch=branch_to_use)

        if number is not None:
            num_qs = qs.filter(number=number)
            if self.instance is not None:
                num_qs = num_qs.exclude(pk=self.instance.pk)
            if num_qs.exists():
                raise serializers.ValidationError({"number": "Кухня с таким номером уже существует."})

        title_qs = qs.filter(title=title)
        if self.instance is not None:
            title_qs = title_qs.exclude(pk=self.instance.pk)
        if title_qs.exists():
            raise serializers.ValidationError({"title": "Кухня с таким названием уже существует."})

        # Убираем нуль-байты из printer (в БД могли остаться, Django их не допускает)
        if "printer" in attrs:
            attrs["printer"] = _strip_null_bytes(attrs["printer"] or "")
        elif self.instance is not None:
            current = getattr(self.instance, "printer", None) or ""
            attrs["printer"] = _strip_null_bytes(current)

        return attrs

    def create(self, validated_data):
        try:
            with transaction.atomic():
                return super().create(validated_data)
        except IntegrityError as e:
            # Дружелюбная ошибка вместо 500 IntegrityError (уникальные ограничения)
            msg = str(e)
            if "uniq_kitchen_number_" in msg:
                raise serializers.ValidationError({"number": "Кухня с таким номером уже существует."})
            if "uniq_kitchen_title_" in msg:
                raise serializers.ValidationError({"title": "Кухня с таким названием уже существует."})
            raise

    def update(self, instance, validated_data):
        try:
            with transaction.atomic():
                return super().update(instance, validated_data)
        except IntegrityError as e:
            msg = str(e)
            if "uniq_kitchen_number_" in msg:
                raise serializers.ValidationError({"number": "Кухня с таким номером уже существует."})
            if "uniq_kitchen_title_" in msg:
                raise serializers.ValidationError({"title": "Кухня с таким названием уже существует."})
            raise

# --- НОВОЕ: Notification (если нужно выводить списком) ---
class NotificationCafeSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationCafe
        fields = ['id', 'type', 'message', 'payload', 'is_read', 'created_at']
        read_only_fields = ['id', 'type', 'message', 'payload', 'is_read', 'created_at']



# --------- Простые справочники ---------
class ZoneSerializer(CompanyBranchReadOnlyMixin):
    class Meta:
        model = Zone
        fields = ["id", "company", "branch", "title"]
        read_only_fields = ["id", "company", "branch"]


class TableSerializer(CompanyBranchReadOnlyMixin):
    zone = serializers.PrimaryKeyRelatedField(queryset=Zone.objects.all())

    class Meta:
        model = Table
        fields = ["id", "company", "branch", "zone", "number", "places", "status"]
        read_only_fields = ["id", "company", "branch"]

    def get_fields(self):
        fields = super().get_fields()
        fields["zone"].queryset = _scope_queryset_by_context(Zone.objects.all(), self)
        return fields

    def validate(self, attrs):
        zone = attrs.get("zone") or getattr(self.instance, "zone", None)
        tb = self._auto_branch()
        company = self._user_company()
        if company and zone and zone.company_id != company.id:
            raise serializers.ValidationError({"zone": "Зона принадлежит другой компании."})
        if tb is not None and zone and zone.branch_id not in (None, tb.id):
            raise serializers.ValidationError({"zone": "Зона принадлежит другому филиалу."})
        return attrs


class WarehouseSerializer(CompanyBranchReadOnlyMixin):
    supplier = serializers.CharField(
        max_length=255,
        allow_blank=True,
        allow_null=True,
        required=False,
        default="",
    )

    class Meta:
        ref_name = "CafeWarehouse"
        model = Warehouse
        fields = ["id", "company", "branch", "title", "supplier", "unit", "remainder", "minimum", "unit_price"]
        read_only_fields = ["id", "company", "branch"]

    def validate_supplier(self, value):
        return "" if value is None else value

    def validate(self, attrs):
        title = attrs.get("title")
        if title is None:
            return attrs
        company = self._user_company()
        if not company:
            return attrs
        branch = self._auto_branch() if self.instance is None else (getattr(self.instance, "branch", None))
        qs = Warehouse.objects.filter(company=company, title=title)
        if branch is not None:
            qs = qs.filter(branch=branch)
        else:
            qs = qs.filter(branch__isnull=True)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            scope = "филиале" if branch else "компании (глобальный склад)"
            raise serializers.ValidationError({
                "title": f"Склад с названием «{title}» уже существует в этом {scope}."
            })
        return attrs


class PurchaseSerializer(CompanyBranchReadOnlyMixin):
    class Meta:
        model = Purchase
        fields = ["id", "company", "branch", "supplier", "positions", "price"]
        read_only_fields = ["id", "company", "branch"]


class CategorySerializer(CompanyBranchReadOnlyMixin):
    class Meta:
        model = Category
        fields = ["id", "company", "branch", "title"]
        read_only_fields = ["id", "company", "branch"]


# --------- Меню и ингредиенты ---------
class IngredientInlineSerializer(serializers.ModelSerializer):
    product_title = serializers.CharField(source="product.title", read_only=True)
    product_unit = serializers.CharField(source="product.unit", read_only=True)
    product_unit_price = serializers.DecimalField(
        source="product.unit_price", max_digits=12, decimal_places=2, read_only=True
    )
    ingredient_cost = serializers.SerializerMethodField()
    unit = serializers.CharField(read_only=True)
    quantity_in_package = serializers.DecimalField(max_digits=12, decimal_places=3, required=False)
    gross_unit = serializers.DecimalField(max_digits=12, decimal_places=5, read_only=True)
    gross_kg = serializers.DecimalField(max_digits=12, decimal_places=3, read_only=True, allow_null=True)
    cold_loss_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False)
    net_kg = serializers.DecimalField(max_digits=12, decimal_places=3, read_only=True, allow_null=True)
    hot_loss_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False)
    output_ready_kg = serializers.DecimalField(max_digits=12, decimal_places=3, read_only=True, allow_null=True)
    cost_price_rub = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    cost_per_unit_rub = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    cost_per_unit_weight_rub = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, allow_null=True)

    class Meta:
        model = Ingredient
        fields = [
            "id", "product", "product_title", "product_unit",
            "product_unit_price", "amount", "ingredient_cost"
            ,
            "unit",
            "quantity_in_package",
            "gross_unit",
            "gross_kg",
            "cold_loss_percent",
            "net_kg",
            "hot_loss_percent",
            "output_ready_kg",
            "cost_price_rub",
            "cost_per_unit_rub",
            "cost_per_unit_weight_rub",
        ]
        read_only_fields = [
            "id",
            "product_title",
            "product_unit",
            "product_unit_price",
            "ingredient_cost",
            "unit",
            "gross_unit",
            "gross_kg",
            "net_kg",
            "output_ready_kg",
            "cost_price_rub",
            "cost_per_unit_rub",
            "cost_per_unit_weight_rub",
        ]

    def get_fields(self):
        fields = super().get_fields()
        # Раньше: self.parent.parent  →  падало в Swagger
        holder = getattr(self, "root", None)
        if isinstance(holder, CompanyBranchReadOnlyMixin):
            fields["product"].queryset = _scope_queryset_by_context(Warehouse.objects.all(), holder)
        else:
            # нет контекста/корня (Swagger) — безопасно отдаём пусто
            fields["product"].queryset = Warehouse.objects.none()
        return fields

    def get_ingredient_cost(self, obj):
        """Стоимость ингредиента = количество * цена за единицу"""
        unit_price = obj.product.unit_price or Decimal("0.00")
        amount = obj.amount or Decimal("0.00")
        return (unit_price * amount).quantize(Decimal("0.01"))

    def validate(self, attrs):
        # model.clean() на связях добьёт; здесь ничего лишнего
        return attrs


class ProcessingTypeSerializer(CompanyBranchReadOnlyMixin):
    class Meta:
        model = ProcessingType
        fields = ["id", "company", "branch", "name", "cost", "charge_type", "unit", "is_active"]
        read_only_fields = ["id", "company", "branch"]

    def validate(self, attrs):
        name = (attrs.get("name") or getattr(self.instance, "name", "") or "").strip()
        if not name:
            raise serializers.ValidationError({"name": "Название обязательно."})
        cost = attrs.get("cost", getattr(self.instance, "cost", Decimal("0.00")) if self.instance else Decimal("0.00"))
        if cost is not None and cost < 0:
            raise serializers.ValidationError({"cost": "Стоимость не может быть отрицательной."})
        return attrs


class PreparationProcessingSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreparationProcessing
        fields = ["id", "name", "cost", "charge_type", "unit", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        name = (attrs.get("name") or getattr(self.instance, "name", "") or "").strip()
        if not name:
            raise serializers.ValidationError({"name": "Название обязательно."})
        cost = attrs.get("cost", getattr(self.instance, "cost", Decimal("0.00")) if self.instance else Decimal("0.00"))
        if cost is not None and cost < 0:
            raise serializers.ValidationError({"cost": "Стоимость не может быть отрицательной."})
        return attrs


class PreparationSerializer(CompanyBranchReadOnlyMixin):
    source_product_title = serializers.CharField(source="source_product.title", read_only=True)
    source_product_unit = serializers.CharField(source="source_product.unit", read_only=True)
    source_product_unit_price = serializers.DecimalField(source="source_product.unit_price", max_digits=12, decimal_places=2, read_only=True)
    processings = PreparationProcessingSerializer(many=True, required=False)

    class Meta:
        model = Preparation
        fields = [
            "id", "company", "branch",
            "name",
            "source_product", "source_product_title", "source_product_unit", "source_product_unit_price",
            "input_quantity", "input_unit",
            "output_quantity", "output_unit",
            "loss_quantity", "loss_percent",
            "raw_material_cost", "processing_cost", "total_cost", "unit_cost",
            "stock_quantity",
            "processings",
            "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "company", "branch",
            "loss_quantity", "loss_percent",
            "raw_material_cost", "total_cost", "unit_cost",
            "created_at", "updated_at",
        ]

    def get_fields(self):
        fields = super().get_fields()
        fields["source_product"].queryset = _scope_queryset_by_context(Warehouse.objects.all(), self)
        return fields

    def validate(self, attrs):
        input_q = attrs.get("input_quantity", getattr(self.instance, "input_quantity", None) if self.instance else None)
        output_q = attrs.get("output_quantity", getattr(self.instance, "output_quantity", None) if self.instance else None)
        source = attrs.get("source_product", getattr(self.instance, "source_product", None) if self.instance else None)
        has_ingredients = bool(self.instance and self.instance.ingredients.exists()) if self.instance else False

        if output_q is not None and output_q <= 0:
            raise serializers.ValidationError({"output_quantity": "Должно быть больше 0."})
        if input_q is not None and input_q <= 0:
            raise serializers.ValidationError({"input_quantity": "Должно быть больше 0."})
        if input_q is not None and output_q is not None and output_q > input_q:
            raise serializers.ValidationError({"output_quantity": "Выход не может быть больше входа."})
        if not has_ingredients and not source:
            raise serializers.ValidationError(
                {"source_product": "Укажите исходный продукт или добавьте строки техкарты (ingredients)."}
            )
        pcost = attrs.get("processing_cost")
        if pcost is not None and pcost < 0:
            raise serializers.ValidationError({"processing_cost": "Не может быть отрицательной."})
        return attrs

    def create(self, validated_data):
        rows = validated_data.pop("processings", [])
        prep = Preparation.objects.create(**validated_data)
        for row in rows:
            PreparationProcessing.objects.create(preparation=prep, **row)
        return prep

    def update(self, instance, validated_data):
        rows = validated_data.pop("processings", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if rows is not None:
            instance.processings.all().delete()
            for row in rows:
                PreparationProcessing.objects.create(preparation=instance, **row)
        return instance


class PreparationReceiveSerializer(serializers.Serializer):
    input_quantity = serializers.DecimalField(max_digits=14, decimal_places=6, required=False)
    output_quantity = serializers.DecimalField(max_digits=14, decimal_places=6, required=False)
    batch_output_quantity = serializers.DecimalField(max_digits=14, decimal_places=6, required=False)
    processing_cost = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)

    def validate(self, attrs):
        iq = attrs.get("input_quantity")
        oq = attrs.get("output_quantity")
        batch = attrs.get("batch_output_quantity")
        if iq is not None and iq <= 0:
            raise serializers.ValidationError({"input_quantity": "Должно быть больше 0."})
        if oq is not None and oq <= 0:
            raise serializers.ValidationError({"output_quantity": "Должно быть больше 0."})
        if batch is not None and batch <= 0:
            raise serializers.ValidationError({"batch_output_quantity": "Должно быть больше 0."})
        if iq is not None and oq is not None and oq > iq:
            raise serializers.ValidationError({"output_quantity": "Выход не может быть больше входа."})
        pc = attrs.get("processing_cost")
        if pc is not None and pc < 0:
            raise serializers.ValidationError({"processing_cost": "Не может быть отрицательной."})
        return attrs


class PreparationIngredientSerializer(serializers.ModelSerializer):
    product_title = serializers.CharField(source="product.title", read_only=True, allow_null=True)
    child_preparation_name = serializers.CharField(source="child_preparation.name", read_only=True, allow_null=True)
    ingredient_type = serializers.SerializerMethodField()

    class Meta:
        model = PreparationIngredient
        fields = [
            "id", "preparation",
            "product", "product_title",
            "child_preparation", "child_preparation_name",
            "ingredient_type",
            "quantity", "unit", "waste_percent",
            "unit_cost", "ingredient_cost", "processing_cost", "total_cost",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "preparation",
            "unit_cost", "ingredient_cost", "processing_cost", "total_cost",
            "created_at", "updated_at",
        ]

    def get_ingredient_type(self, obj):
        if obj.product_id:
            return "product"
        if obj.child_preparation_id:
            return "preparation"
        return None


class PreparationIngredientCreateUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreparationIngredient
        fields = [
            "id", "product", "child_preparation",
            "quantity", "unit", "waste_percent",
        ]
        read_only_fields = ["id"]

    def get_fields(self):
        fields = super().get_fields()
        holder = getattr(self, "root", None)
        if isinstance(holder, CompanyBranchReadOnlyMixin):
            if "product" in fields:
                fields["product"].queryset = _scope_queryset_by_context(Warehouse.objects.all(), holder)
            if "child_preparation" in fields:
                fields["child_preparation"].queryset = _scope_queryset_by_context(Preparation.objects.all(), holder)
        else:
            if "product" in fields:
                fields["product"].queryset = Warehouse.objects.none()
            if "child_preparation" in fields:
                fields["child_preparation"].queryset = Preparation.objects.none()
        return fields

    def validate(self, attrs):
        from apps.cafe.services.costing import check_preparation_cycle, convert_quantity, _norm_unit

        product = attrs.get("product", getattr(self.instance, "product", None) if self.instance else None)
        child = attrs.get(
            "child_preparation",
            getattr(self.instance, "child_preparation", None) if self.instance else None,
        )
        has_product = bool(product)
        has_child = bool(child)
        if has_product == has_child:
            raise serializers.ValidationError(
                "Укажите ровно один источник: product или child_preparation."
            )

        qty = attrs.get("quantity", getattr(self.instance, "quantity", None) if self.instance else None)
        if qty is not None and qty <= 0:
            raise serializers.ValidationError({"quantity": "Должно быть больше 0."})

        wp = attrs.get(
            "waste_percent",
            getattr(self.instance, "waste_percent", Decimal("0")) if self.instance else Decimal("0"),
        )
        if wp is not None:
            wp = Decimal(wp)
            if wp < 0:
                raise serializers.ValidationError({"waste_percent": "Не может быть отрицательным."})
            if wp >= 100:
                raise serializers.ValidationError({"waste_percent": "Должно быть меньше 100."})

        unit = attrs.get("unit", getattr(self.instance, "unit", "") if self.instance else "")
        if unit:
            try:
                if has_product:
                    convert_quantity(Decimal("1"), _norm_unit(unit), _norm_unit(product.unit))
                elif has_child:
                    convert_quantity(Decimal("1"), _norm_unit(unit), _norm_unit(child.output_unit))
            except ValueError as e:
                raise serializers.ValidationError({"unit": str(e)})

        preparation = self.context.get("preparation")
        if preparation is None and self.instance:
            preparation = self.instance.preparation
        if preparation and has_child:
            try:
                check_preparation_cycle(preparation, child)
            except ValueError as e:
                raise serializers.ValidationError({"child_preparation": str(e)})

        return attrs


class PreparationTechCardItemSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    type = serializers.CharField()
    name = serializers.CharField()
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    unit = serializers.CharField()
    waste_percent = serializers.DecimalField(max_digits=6, decimal_places=2)
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2)
    ingredient_cost = serializers.DecimalField(max_digits=12, decimal_places=2)
    processing_cost = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_cost = serializers.DecimalField(max_digits=12, decimal_places=2)


class PreparationTechCardSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    type = serializers.CharField(default="preparation")
    output_quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    output_unit = serializers.CharField()
    total_cost = serializers.DecimalField(max_digits=12, decimal_places=2)
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2)
    stock_quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    items = PreparationTechCardItemSerializer(many=True)


class DishIngredientProcessingSerializer(serializers.ModelSerializer):
    processing_type_name = serializers.CharField(source="processing_type.name", read_only=True, allow_null=True)
    preparation_processing_name = serializers.CharField(source="preparation_processing.name", read_only=True, allow_null=True)
    charge_type = serializers.SerializerMethodField()
    rate = serializers.SerializerMethodField()

    class Meta:
        model = DishIngredientProcessing
        fields = [
            "id",
            "ingredient",
            "processing_type",
            "processing_type_name",
            "preparation_processing",
            "preparation_processing_name",
            "charge_type",
            "rate",
            "cost",
        ]
        read_only_fields = [
            "id",
            "ingredient",
            "processing_type_name",
            "preparation_processing_name",
            "charge_type",
            "rate",
            "cost",
        ]

    def get_charge_type(self, obj):
        if obj.preparation_processing_id:
            return obj.preparation_processing.charge_type
        if obj.processing_type_id:
            return obj.processing_type.charge_type
        return None

    def get_rate(self, obj):
        if obj.preparation_processing_id:
            return obj.preparation_processing.cost
        if obj.processing_type_id:
            return obj.processing_type.cost
        return None


class DishIngredientSerializer(serializers.ModelSerializer):
    product_title = serializers.CharField(source="product.title", read_only=True)
    preparation_name = serializers.CharField(source="preparation.name", read_only=True)
    processings = DishIngredientProcessingSerializer(many=True, read_only=True)

    class Meta:
        model = DishIngredient
        fields = [
            "id",
            "dish",
            "ingredient_type",
            "product", "product_title",
            "preparation", "preparation_name",
            "quantity", "unit",
            "unit_cost", "ingredient_cost", "processing_cost", "total_cost",
            "processings",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id",
            "unit_cost", "ingredient_cost", "processing_cost", "total_cost",
            "created_at", "updated_at",
            "product_title", "preparation_name",
            "processings",
        ]

    def _user_company(self):
        return _cafe_resolve_user_company(self.context.get("request"))

    def get_fields(self):
        fields = super().get_fields()
        company = self._user_company()
        request = self.context.get("request")
        active_branch = getattr(request, "branch", None) if request else None

        if company:
            dish_qs = MenuItem.objects.filter(company=company)
            prod_qs = Warehouse.objects.filter(company=company)
            prep_qs = Preparation.objects.filter(company=company)
            if active_branch is not None:
                dish_qs = dish_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
                prod_qs = prod_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
                prep_qs = prep_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
            else:
                dish_qs = dish_qs.filter(branch__isnull=True)
                prod_qs = prod_qs.filter(branch__isnull=True)
                prep_qs = prep_qs.filter(branch__isnull=True)
            fields["dish"].queryset = dish_qs
            fields["product"].queryset = prod_qs
            fields["preparation"].queryset = prep_qs
        else:
            fields["dish"].queryset = MenuItem.objects.none()
            fields["product"].queryset = Warehouse.objects.none()
            fields["preparation"].queryset = Preparation.objects.none()

        fields["product"].required = False
        fields["product"].allow_null = True
        fields["preparation"].required = False
        fields["preparation"].allow_null = True
        return fields

    def validate(self, attrs):
        qty = attrs.get("quantity", getattr(self.instance, "quantity", None) if self.instance else None)
        if qty is not None and qty <= 0:
            raise serializers.ValidationError({"quantity": "Должно быть больше 0."})

        it = attrs.get("ingredient_type", getattr(self.instance, "ingredient_type", None) if self.instance else None)
        product = attrs.get("product") if "product" in attrs else (getattr(self.instance, "product", None) if self.instance else None)
        preparation = attrs.get("preparation") if "preparation" in attrs else (getattr(self.instance, "preparation", None) if self.instance else None)

        if it == DishIngredient.IngredientType.PRODUCT:
            if not product or preparation:
                raise serializers.ValidationError({"product": "Укажите продукт (и не указывайте заготовку)."})
        elif it == DishIngredient.IngredientType.PREPARATION:
            if not preparation or product:
                raise serializers.ValidationError({"preparation": "Укажите заготовку (и не указывайте продукт)."})
        else:
            raise serializers.ValidationError({"ingredient_type": "Некорректный тип."})
        return attrs


class DishIngredientProcessingCreateSerializer(serializers.ModelSerializer):
    processing_type = serializers.PrimaryKeyRelatedField(
        queryset=ProcessingType.objects.none(), required=False, allow_null=True
    )
    preparation_processing = serializers.PrimaryKeyRelatedField(
        queryset=PreparationProcessing.objects.none(), required=False, allow_null=True
    )

    class Meta:
        model = DishIngredientProcessing
        fields = ["id", "ingredient", "processing_type", "preparation_processing"]
        read_only_fields = ["id"]

    def _user_company(self):
        return _cafe_resolve_user_company(self.context.get("request"))

    def get_fields(self):
        fields = super().get_fields()
        company = self._user_company()
        request = self.context.get("request")
        active_branch = getattr(request, "branch", None) if request else None
        if company:
            ing_qs = DishIngredient.objects.filter(dish__company=company)
            pt_qs = ProcessingType.objects.filter(company=company)
            pp_qs = PreparationProcessing.objects.select_related("preparation").filter(preparation__company=company)
            if active_branch is not None:
                ing_qs = ing_qs.filter(Q(dish__branch=active_branch) | Q(dish__branch__isnull=True))
                pt_qs = pt_qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))
                pp_qs = pp_qs.filter(
                    Q(preparation__branch=active_branch) | Q(preparation__branch__isnull=True)
                )
            else:
                ing_qs = ing_qs.filter(dish__branch__isnull=True)
                # processing_type: вся компания; preparation_processing: вся компания (валидация по заготовке)
            fields["ingredient"].queryset = ing_qs
            fields["processing_type"].queryset = pt_qs
            fields["preparation_processing"].queryset = pp_qs
        else:
            fields["ingredient"].queryset = DishIngredient.objects.none()
            fields["processing_type"].queryset = ProcessingType.objects.none()
            fields["preparation_processing"].queryset = PreparationProcessing.objects.none()
        return fields

    def validate(self, attrs):
        ing = attrs.get("ingredient")
        pt = attrs.get("processing_type")
        pp = attrs.get("preparation_processing")
        has_pt = pt is not None
        has_pp = pp is not None
        if has_pt and has_pp:
            raise serializers.ValidationError(
                "Укажите только одно поле: processing_type или preparation_processing."
            )
        if not has_pt and not has_pp:
            raise serializers.ValidationError(
                "Укажите processing_type (для продукта) или preparation_processing (для заготовки)."
            )
        if ing:
            if ing.ingredient_type == DishIngredient.IngredientType.PRODUCT:
                if not has_pt or has_pp:
                    raise serializers.ValidationError(
                        {"processing_type": "Для ингредиента-продукта укажите тип обработки из справочника."}
                    )
            elif ing.ingredient_type == DishIngredient.IngredientType.PREPARATION:
                if not has_pp or has_pt:
                    raise serializers.ValidationError(
                        {"preparation_processing": "Для ингредиента-заготовки укажите обработку этой заготовки."}
                    )
                if pp.preparation_id != ing.preparation_id:
                    raise serializers.ValidationError(
                        {"preparation_processing": "Можно выбрать только обработку из состава выбранной заготовки."}
                    )
        return attrs


class DishCostSerializer(serializers.Serializer):
    dish_id = serializers.UUIDField()
    cost_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    sale_price = serializers.DecimalField(max_digits=11, decimal_places=3)
    margin_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    margin_percent = serializers.DecimalField(max_digits=6, decimal_places=2)


class TechCardCostExportSerializer(serializers.Serializer):
    cost_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    sale_price = serializers.DecimalField(max_digits=11, decimal_places=3)
    margin_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    margin_percent = serializers.DecimalField(max_digits=6, decimal_places=2)


class TechCardsExportRequestSerializer(serializers.Serializer):
    dish_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
        default=list,
    )
    is_all = serializers.BooleanField(required=False, default=False)
    search = serializers.CharField(required=False, allow_blank=True, default="")
    category_id = serializers.UUIDField(required=False, allow_null=True, default=None)

    def validate(self, attrs):
        is_all = attrs.get("is_all") or False
        dish_ids = attrs.get("dish_ids") or []
        if not is_all and not dish_ids:
            raise serializers.ValidationError(
                {"dish_ids": "Укажите dish_ids или передайте is_all=true."}
            )
        return attrs


class TechCardExportItemSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    title = serializers.CharField()
    image_url = serializers.CharField(allow_null=True)
    category_title = serializers.CharField(allow_null=True, allow_blank=True)
    cost = TechCardCostExportSerializer()
    ingredients = serializers.ListField(child=serializers.DictField())


class TechCardsExportResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    items = TechCardExportItemSerializer(many=True)


class DishCalculatePreviewSerializer(serializers.Serializer):
    """
    Body:
      {
        "sale_price": "250.00",
        "other_expenses": "0.00",
        "ingredients": [
          {"ingredient_type":"product","product":"<uuid>","quantity":"0.05","unit":"kg","processing_type_ids":["uuid", ...]},
          {"ingredient_type":"preparation","preparation":"<uuid>","quantity":"0.3","unit":"kg","preparation_processing_ids":["uuid", ...]}
        ]
      }
    """
    sale_price = serializers.DecimalField(max_digits=11, decimal_places=3, required=False, allow_null=True)
    other_expenses = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=Decimal("0.00"))
    ingredients = serializers.ListField(child=serializers.DictField(), allow_empty=True)


class MenuItemSerializer(CompanyBranchReadOnlyMixin):
    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
        required=False,
        allow_null=True,
    )

    kitchen = serializers.PrimaryKeyRelatedField(
        queryset=Kitchen.objects.all(),
        required=False,
        allow_null=True
    )
    kitchen_title = serializers.CharField(source="kitchen.title", read_only=True)
    kitchen_number = serializers.IntegerField(source="kitchen.number", read_only=True)

    ingredients = IngredientInlineSerializer(many=True, required=False)
    image = serializers.ImageField(required=False, allow_null=True)
    image_url = serializers.SerializerMethodField()
    
    # Новые поля для себестоимости
    vat_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, default=Decimal("0.00")
    )
    other_expenses = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=Decimal("0.00")
    )
    cost_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )
    margin_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    margin_percent_value = serializers.DecimalField(source="margin_percent", max_digits=6, decimal_places=2, read_only=True)
    
    # Вычисляемые поля (read-only)
    vat_amount = serializers.SerializerMethodField()
    profit = serializers.SerializerMethodField()
    margin_percent = serializers.SerializerMethodField()
    ingredients_cost = serializers.SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = [
            "id", "company", "branch",
            "title", "category",
            "kitchen", "kitchen_title", "kitchen_number",
            "price", "is_active",
            "is_sold_by_weight", "sale_unit",
            "image", "image_url",
            # Себестоимость и расходы
            "vat_percent", "other_expenses", "cost_price",
            "margin_amount", "margin_percent_value",
            "vat_amount", "profit", "margin_percent", "ingredients_cost",
            # Фискальные коды (налоговая ГНС КР)
            "fiscal_vat_code", "fiscal_st_code", "fiscal_calc_item_attr_code",
            "fiscal_measure", "fiscal_sgtin",
            "created_at", "updated_at", "ingredients",
        ]
        read_only_fields = [
            "id", "company", "branch", "created_at", "updated_at",
            "cost_price", "margin_amount", "margin_percent_value",
            "vat_amount", "profit", "margin_percent", "ingredients_cost"
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Если блюдо использует новую схему DishIngredient — показываем её в поле "ingredients",
        # чтобы фронт видел добавленные ингредиенты через /dishes/<id>/ingredients/.
        try:
            if getattr(instance, "dish_ingredients", None) is not None and instance.dish_ingredients.exists():
                if "dish_ingredients" in getattr(instance, "_prefetched_objects_cache", {}):  # perf1311c
                    qs = sorted(instance.dish_ingredients.all(), key=lambda d: (d.created_at, str(d.id)))
                else:
                  qs = (
                    instance.dish_ingredients
                    .select_related("product", "preparation")
                    .prefetch_related(
                        "processings__processing_type",
                        "processings__preparation_processing",
                    )
                    .all()
                )
                data["ingredients"] = DishIngredientSerializer(qs, many=True, context=self.context).data
        except Exception:
            # безопасно: в случае неожиданного состояния оставим legacy ingredients
            pass
        return data

    def get_fields(self):
        fields = super().get_fields()
        fields["category"].queryset = _scope_queryset_by_context(Category.objects.all(), self)
        fields["kitchen"].queryset = _scope_queryset_by_context(Kitchen.objects.all(), self)
        return fields

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image and hasattr(obj.image, "url"):
            url = obj.image.url
            return request.build_absolute_uri(url) if request else url
        return None

    def get_vat_amount(self, obj):
        """Сумма НДС от цены продажи"""
        return obj.vat_amount

    def get_profit(self, obj):
        """Прибыль = Цена продажи - Себестоимость - НДС"""
        return obj.profit

    def get_margin_percent(self, obj):
        """Маржа в процентах"""
        return obj.margin_percent

    def get_ingredients_cost(self, obj):
        """Стоимость всех ингредиентов (без прочих расходов)"""
        total = Decimal("0.00")
        for ingredient in obj.ingredients.all():  # perf1311c: берём из prefetch
            unit_price = ingredient.product.unit_price or Decimal("0.00")
            amount = ingredient.amount or Decimal("0.00")
            total += unit_price * amount
        return total.quantize(Decimal("0.01"))

    def validate(self, attrs):
        company = self._user_company() or getattr(self.instance, "company", None)
        tb = self._auto_branch()

        category = attrs.get("category") or getattr(self.instance, "category", None)
        kitchen = attrs.get("kitchen") if "kitchen" in attrs else getattr(self.instance, "kitchen", None)

        if company and category and category.company_id != company.id:
            raise serializers.ValidationError({"category": "Категория принадлежит другой компании."})
        if tb is not None and category and category.branch_id not in (None, tb.id):
            raise serializers.ValidationError({"category": "Категория другого филиала."})

        if kitchen:
            if company and kitchen.company_id != company.id:
                raise serializers.ValidationError({"kitchen": "Кухня принадлежит другой компании."})
            if tb is not None and kitchen.branch_id not in (None, tb.id):
                raise serializers.ValidationError({"kitchen": "Кухня другого филиала."})

        # Валидация НДС
        vat = attrs.get("vat_percent")
        if vat is not None and vat < 0:
            raise serializers.ValidationError({"vat_percent": "НДС не может быть отрицательным."})
        if vat is not None and vat > 100:
            raise serializers.ValidationError({"vat_percent": "НДС не может быть больше 100%."})

        # Валидация прочих расходов (отрицательные — только владелец/админ/staff)
        other = attrs.get("other_expenses")
        if other is not None and other < 0 and not _is_owner_like(self._user()):
            raise serializers.ValidationError(
                {"other_expenses": "Отрицательные прочие расходы доступны только владельцу или администратору."}
            )

        from .weight import normalize_sale_unit

        sold_by_weight = attrs.get(
            "is_sold_by_weight",
            getattr(self.instance, "is_sold_by_weight", False) if self.instance else False,
        )
        if "sale_unit" in attrs or sold_by_weight:
            attrs["sale_unit"] = normalize_sale_unit(
                attrs.get("sale_unit", getattr(self.instance, "sale_unit", None) if self.instance else None),
                sold_by_weight=bool(sold_by_weight),
            )
        elif not sold_by_weight:
            attrs["sale_unit"] = "kg"

        return attrs

    def _upsert_ingredients(self, menu_item, ing_list):
        for ing in ing_list:
            qty_in_pack = ing.get("quantity_in_package", Decimal("0"))
            cold = ing.get("cold_loss_percent", Decimal("0"))
            hot = ing.get("hot_loss_percent", Decimal("0"))
            Ingredient.objects.create(
                menu_item=menu_item,
                product=ing["product"],
                amount=ing["amount"],
                quantity_in_package=qty_in_pack,
                cold_loss_percent=cold,
                hot_loss_percent=hot,
            )

    def _recalc_and_save_cost(self, menu_item):
        """Пересчитать и сохранить себестоимость"""
        try:
            menu_item.recalc_cost_price()
        except ValueError as e:
            raise serializers.ValidationError({"detail": str(e)})
        menu_item.save(update_fields=["cost_price", "margin_amount", "margin_percent"])

    def create(self, validated_data):
        ingredients = validated_data.pop("ingredients", [])
        try:
            with transaction.atomic():
                obj = super().create(validated_data)
                if ingredients:
                    self._upsert_ingredients(obj, ingredients)
                # Пересчитываем себестоимость после добавления ингредиентов
                self._recalc_and_save_cost(obj)
                return obj
        except IntegrityError as e:
            # Дружелюбная ошибка вместо 500 IntegrityError (уникальные ограничения по названию)
            msg = str(e)
            if "uniq_menuitem_title_" in msg:
                raise serializers.ValidationError(
                    {"title": "Позиция меню с таким названием уже существует."}
                )
            raise

    def update(self, instance, validated_data):
        ingredients = validated_data.pop("ingredients", None)
        try:
            with transaction.atomic():
                obj = super().update(instance, validated_data)
                if ingredients is not None:
                    instance.ingredients.all().delete()
                    if ingredients:
                        self._upsert_ingredients(instance, ingredients)
                # Пересчитываем себестоимость после обновления
                self._recalc_and_save_cost(obj)
                return obj
        except IntegrityError as e:
            msg = str(e)
            if "uniq_menuitem_title_" in msg:
                raise serializers.ValidationError(
                    {"title": "Позиция меню с таким названием уже существует."}
                )
            raise



# --------- Бронь ---------
class BookingSerializer(CompanyBranchReadOnlyMixin):
    table = serializers.PrimaryKeyRelatedField(queryset=Table.objects.all())

    class Meta:
        model = Booking
        fields = [
            "id", "company", "branch",
            "guest", "phone", "date", "time", "guests", "table",
            "status", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company", "branch", "created_at", "updated_at"]

    def get_fields(self):
        fields = super().get_fields()
        fields["table"].queryset = _scope_queryset_by_context(Table.objects.all(), self)
        return fields

    def validate(self, attrs):
        table = attrs.get("table") or getattr(self.instance, "table", None)
        tb = self._auto_branch()
        if tb is not None and table and table.branch_id not in (None, tb.id):
            raise serializers.ValidationError({"table": "Стол принадлежит другому филиалу."})
        return attrs


# --------- Заказы ---------
class OrderItemInlineSerializer(CompanyBranchReadOnlyMixin):
    order = serializers.PrimaryKeyRelatedField(queryset=Order.objects.all(), required=False, allow_null=True)
    menu_item_title = serializers.CharField(source="menu_item.title", read_only=True, default="")
    menu_item_price = serializers.DecimalField(
        source="menu_item.price",
        max_digits=11,
        decimal_places=3,
        read_only=True,
        allow_null=True,
    )
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))
    menu_item_is_sold_by_weight = serializers.BooleanField(read_only=True)
    menu_item_sale_unit = serializers.CharField(read_only=True)
    is_sold_by_weight = serializers.BooleanField(source="menu_item_is_sold_by_weight", read_only=True)
    sale_unit = serializers.CharField(source="menu_item_sale_unit", read_only=True)
    refundable_quantity = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = [
            "id", "order", "line_kind", "menu_item", "menu_item_title", "menu_item_price",
            "service_title", "unit_price", "quantity", "comment",
            "menu_item_is_sold_by_weight", "menu_item_sale_unit",
            "is_sold_by_weight", "sale_unit",
            "refunded_quantity", "refundable_quantity",
            "is_rejected", "rejection_reason", "rejected_at",
        ]
        read_only_fields = [
            "id", "menu_item_title", "menu_item_price", "rejected_at",
            "menu_item_is_sold_by_weight", "menu_item_sale_unit",
            "is_sold_by_weight", "sale_unit",
            "refunded_quantity", "refundable_quantity",
        ]

    def get_refundable_quantity(self, obj):
        from .weight import quantize_quantity

        q = Decimal(obj.quantity or 0)
        r = Decimal(getattr(obj, "refunded_quantity", 0) or 0)
        return quantize_quantity(max(Decimal("0"), q - r))

    def _menu_weight_context(self, attrs, inst):
        menu_item = attrs.get("menu_item") or (inst.menu_item if inst else None)
        if menu_item:
            sold = bool(menu_item.is_sold_by_weight)
            unit = (menu_item.sale_unit or "kg").strip().lower() if sold else "kg"
            return sold, unit
        if inst:
            return bool(inst.menu_item_is_sold_by_weight), (inst.menu_item_sale_unit or "kg")
        return False, "kg"

    def validate(self, attrs):
        from .weight import validate_order_item_quantity

        inst = self.instance
        order = attrs.get("order") or (inst.order if inst else None)
        can_edit_closed_order = _is_owner_like(self._user())
        if inst and "order" in attrs and attrs["order"] != inst.order:
            raise serializers.ValidationError({"order": "Нельзя переносить позицию в другой заказ."})
        if order is None and not isinstance(getattr(self, "parent", None), serializers.ListSerializer):
            raise serializers.ValidationError({"order": "Укажите заказ."})
        if order and not can_edit_closed_order and (order.is_paid or order.status != Order.Status.OPEN):
            raise serializers.ValidationError({"order": "Можно менять позиции только у открытого неоплаченного заказа."})
        line_kind = attrs.get("line_kind", getattr(inst, "line_kind", OrderItem.LineKind.MENU) if inst else OrderItem.LineKind.MENU)
        if line_kind == OrderItem.LineKind.SERVICE:
            if attrs.get("menu_item") is not None:
                raise serializers.ValidationError({"menu_item": "Для услуги не указывайте позицию меню."})
            title = (attrs.get("service_title") or (getattr(inst, "service_title", None) if inst else None) or "").strip()
            if not title:
                raise serializers.ValidationError({"service_title": "Укажите название услуги."})
            up = attrs.get("unit_price", getattr(inst, "unit_price", None) if inst else None)
            if up is None:
                raise serializers.ValidationError({"unit_price": "Укажите цену услуги."})
        else:
            menu_item = attrs.get("menu_item") or (inst.menu_item if inst else None)
            if not menu_item:
                raise serializers.ValidationError({"menu_item": "Выберите позицию меню."})
        rej = attrs["is_rejected"] if "is_rejected" in attrs else (inst.is_rejected if inst else False)
        if "rejection_reason" in attrs:
            reason = attrs.get("rejection_reason") or ""
        elif inst:
            reason = inst.rejection_reason or ""
        else:
            reason = ""
        if rej and not (reason or "").strip():
            raise serializers.ValidationError({"rejection_reason": "Укажите причину отказа."})

        if "quantity" in attrs:
            sold, unit = self._menu_weight_context(attrs, inst)
            if line_kind == OrderItem.LineKind.SERVICE:
                attrs["quantity"] = validate_order_item_quantity(
                    attrs["quantity"], is_sold_by_weight=False, sale_unit="kg",
                )
            else:
                attrs["quantity"] = validate_order_item_quantity(
                    attrs["quantity"], is_sold_by_weight=sold, sale_unit=unit,
                )
        return attrs

    def _apply_weight_snapshot(self, validated_data):
        menu_item = validated_data.get("menu_item")
        if menu_item and validated_data.get("line_kind", OrderItem.LineKind.MENU) == OrderItem.LineKind.MENU:
            validated_data["menu_item_is_sold_by_weight"] = bool(menu_item.is_sold_by_weight)
            validated_data["menu_item_sale_unit"] = (
                (menu_item.sale_unit or "kg").strip().lower() if menu_item.is_sold_by_weight else "kg"
            )

    def create(self, validated_data):
        from django.utils import timezone as dj_tz

        self._apply_weight_snapshot(validated_data)
        if validated_data.get("is_rejected"):
            validated_data.setdefault("rejected_at", dj_tz.now())
        return super().create(validated_data)

    def update(self, instance, validated_data):
        from django.utils import timezone as dj_tz

        if validated_data.get("line_kind") == OrderItem.LineKind.SERVICE:
            validated_data["menu_item"] = None
        elif validated_data.get("line_kind") == OrderItem.LineKind.MENU:
            validated_data["service_title"] = ""
            validated_data["unit_price"] = None
        if validated_data.get("is_rejected") and not instance.is_rejected:
            validated_data.setdefault("rejected_at", dj_tz.now())
        if validated_data.get("is_rejected") is False:
            validated_data["rejection_reason"] = ""
            validated_data["rejected_at"] = None
        if "menu_item" in validated_data:
            self._apply_weight_snapshot(validated_data)
        elif instance.line_kind == OrderItem.LineKind.MENU:
            validated_data.setdefault("menu_item_is_sold_by_weight", instance.menu_item_is_sold_by_weight)
            validated_data.setdefault("menu_item_sale_unit", instance.menu_item_sale_unit)
        return super().update(instance, validated_data)

    def to_representation(self, instance):
        from .weight import format_quantity_api

        data = super().to_representation(instance)
        if "quantity" in data and data["quantity"] is not None:
            data["quantity"] = format_quantity_api(data["quantity"])
        if "refunded_quantity" in data and data["refunded_quantity"] is not None:
            data["refunded_quantity"] = format_quantity_api(data["refunded_quantity"])
        if "refundable_quantity" in data and data["refundable_quantity"] is not None:
            data["refundable_quantity"] = format_quantity_api(data["refundable_quantity"])
        return data

    def get_fields(self):
        fields = super().get_fields()
        holder = getattr(self, "root", None) or self
        if isinstance(holder, CompanyBranchReadOnlyMixin):
            fields["order"].queryset = _scope_queryset_by_context(Order.objects.all(), holder)
            fields["menu_item"].queryset = _scope_queryset_by_context(MenuItem.objects.all(), holder)
        else:
            fields["order"].queryset = Order.objects.none()
            fields["menu_item"].queryset = MenuItem.objects.none()
        fields["order"].required = False
        fields["order"].allow_null = True
        fields["menu_item"].required = False
        fields["menu_item"].allow_null = True
        return fields


class OrderBriefSerializer(serializers.ModelSerializer):
    table_number = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "table_number", "guests", "waiter", "created_at"]

    def get_table_number(self, obj):
        return obj.table.number if obj.table_id else None


class OrderItemHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItemHistory
        fields = [
            "id", "line_kind", "menu_item", "menu_item_title", "menu_item_price", "quantity",
            "menu_item_is_sold_by_weight", "menu_item_sale_unit",
            "refunded_quantity",
            "is_rejected", "rejection_reason",
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        from .weight import format_quantity_api

        data = super().to_representation(instance)
        for key in ("quantity", "refunded_quantity"):
            if key in data and data[key] is not None:
                data[key] = format_quantity_api(data[key])
        return data


class OrderHistorySerializer(serializers.ModelSerializer):
    items = OrderItemHistorySerializer(many=True, read_only=True)
    net_paid_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    has_refunds = serializers.BooleanField(read_only=True)
    is_fully_refunded = serializers.BooleanField(read_only=True)

    class Meta:
        model = OrderHistory
        fields = [
            "id", "original_order_id", "company", "branch", "client",
            "table", "table_number", "waiter", "waiter_label",
            "guests", "created_at", "archived_at", "items",
            "status", "is_paid", "paid_at", "payment_method", "total_amount", "discount_amount",
            "paid_amount", "refunded_amount", "net_paid_amount", "has_refunds", "is_fully_refunded",
            "canceled_at", "canceled_by", "canceled_by_label",
        ]
        read_only_fields = fields


class OrderHistoryUpdateSerializer(serializers.ModelSerializer):
    """Корректировка снимка архива (только владелец/админ; проверка во view)."""

    class Meta:
        model = OrderHistory
        fields = [
            "waiter_label", "table_number", "guests",
            "total_amount", "discount_amount", "paid_amount", "refunded_amount",
            "payment_method", "status", "is_paid", "paid_at",
        ]

    def validate_status(self, value):
        allowed = {c[0] for c in OrderHistory.STATUS_CHOICES}
        if value not in allowed:
            raise serializers.ValidationError("Недопустимый статус.")
        return value


class CafeClientSerializer(CompanyBranchReadOnlyMixin):
    orders = OrderBriefSerializer(many=True, read_only=True)
    history = OrderHistorySerializer(source="order_history", many=True, read_only=True)

    class Meta:
        model = CafeClient
        fields = ["id", "company", "branch", "name", "phone", "notes", "orders", "history"]
        read_only_fields = ["id", "company", "branch", "orders", "history"]

    def validate_phone(self, value):
        # лёгкая нормализация
        if not value:
            return value
        return ''.join(ch for ch in value if ch.isdigit() or ch == '+')


class OrderSerializer(CompanyBranchReadOnlyMixin):
    table = serializers.PrimaryKeyRelatedField(
        queryset=Table.objects.all(), required=False, allow_null=True
    )
    table_number = serializers.SerializerMethodField()
    client = serializers.PrimaryKeyRelatedField(queryset=CafeClient.objects.all(), required=False, allow_null=True)
    waiter = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True, required=False)
    items = OrderItemInlineSerializer(many=True, required=False)
    balance_due = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    refunded_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    net_paid_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    has_refunds = serializers.BooleanField(read_only=True)
    is_fully_refunded = serializers.BooleanField(read_only=True)
    cash_shift_id = serializers.UUIDField(read_only=True, allow_null=True)
    canceled_by_label = serializers.SerializerMethodField()

    class Meta:
        ref_name = "CafeOrder"
        model = Order
        fields = [
            "id", "company", "branch", "table", "table_number", "client", "waiter", "guests", "created_at",
            "table_session_id", "check_label",
            "status", "is_paid", "paid_at", "payment_method", "total_amount", "discount_amount",
            "paid_amount", "refunded_amount", "net_paid_amount", "has_refunds", "is_fully_refunded",
            "balance_due", "cash_shift_id",
            "canceled_at", "canceled_by", "canceled_by_label",
            "items",
        ]
        read_only_fields = [
            "is_paid", "paid_at", "payment_method", "total_amount", "paid_amount",
            "refunded_amount", "net_paid_amount", "has_refunds", "is_fully_refunded",
            "balance_due", "cash_shift_id", "table_number",
            "canceled_at", "canceled_by", "canceled_by_label",
        ]

    def get_table_number(self, obj):
        return obj.table.number if obj.table_id else None

    def get_canceled_by_label(self, obj):
        if not getattr(obj, "canceled_by_id", None):
            return ""
        u = getattr(obj, "canceled_by", None)
        if not u:
            return str(obj.canceled_by_id)
        full = getattr(u, "get_full_name", lambda: "")() or ""
        email = getattr(u, "email", "") or ""
        return full or email or str(obj.canceled_by_id)

    def get_fields(self):
        fields = super().get_fields()
        fields["table"].queryset = _scope_queryset_by_context(Table.objects.all(), self)
        fields["client"].queryset = _scope_queryset_by_context(CafeClient.objects.all(), self)
        # сузим официантов по компании, если есть такое поле у User
        company = self._user_company()
        if company and hasattr(User, "company_id"):
            fields["waiter"].queryset = User.objects.filter(company_id=company.id)
        return fields

    def validate(self, attrs):
        company = self._user_company() or getattr(self.instance, "company", None)
        tb = self._auto_branch()
        table = attrs.get("table") or getattr(self.instance, "table", None)
        waiter = attrs.get("waiter") or getattr(self.instance, "waiter", None)
        client = attrs.get("client") or getattr(self.instance, "client", None)

        if company and table and table.company_id != company.id:
            raise serializers.ValidationError({"table": "Стол принадлежит другой компании."})
        if company and waiter and getattr(waiter, "company_id", None) not in (None, company.id):
            raise serializers.ValidationError({"waiter": "Официант принадлежит другой компании."})
        if company and client and client.company_id != company.id:
            raise serializers.ValidationError({"client": "Клиент принадлежит другой компании."})

        if tb is not None:
            tb_id = tb.id
            if table and table.branch_id not in (None, tb_id):
                raise serializers.ValidationError({"table": "Стол другого филиала."})
            if client and client.branch_id not in (None, tb_id):
                raise serializers.ValidationError({"client": "Клиент другого филиала."})

        discount = attrs.get("discount_amount")
        if discount is not None and discount < 0:
            raise serializers.ValidationError({"discount_amount": "Скидка не может быть отрицательной."})
        return attrs

    def _upsert_items(self, order, items):
        for it in items:
            line_kind = it.get("line_kind") or OrderItem.LineKind.MENU
            is_rejected = bool(it.get("is_rejected", False))
            rejection_reason = (it.get("rejection_reason") or "").strip()
            item_comment = (it.get("comment") or "").strip()
            rejected_at = timezone.now() if is_rejected else None
            if line_kind == OrderItem.LineKind.SERVICE:
                OrderItem.objects.create(
                    order=order,
                    company=order.company,
                    line_kind=OrderItem.LineKind.SERVICE,
                    service_title=(it.get("service_title") or "").strip(),
                    unit_price=it.get("unit_price"),
                    quantity=it.get("quantity", 1),
                    comment=item_comment,
                    is_rejected=is_rejected,
                    rejection_reason=rejection_reason,
                    rejected_at=rejected_at,
                )
                continue
            menu_item = it.get("menu_item")
            if not menu_item:
                raise serializers.ValidationError({"items": "Для блюда укажите menu_item."})
            from .weight import validate_order_item_quantity

            qty = validate_order_item_quantity(
                it.get("quantity", 1),
                is_sold_by_weight=bool(menu_item.is_sold_by_weight),
                sale_unit=(menu_item.sale_unit or "kg") if menu_item.is_sold_by_weight else "kg",
            )
            unit_price = it.get("unit_price")
            existing = order.items.filter(
                menu_item=menu_item,
                line_kind=OrderItem.LineKind.MENU,
            ).first()
            if existing:
                existing.quantity = Decimal(existing.quantity or 0) + qty
                # Склеиваем комментарии в одну строку: "без лука; остро"
                if item_comment:
                    cur = (existing.comment or "").strip()
                    if not cur:
                        existing.comment = item_comment
                    elif item_comment not in [p.strip() for p in cur.split(";") if p.strip()]:
                        existing.comment = f"{cur}; {item_comment}"
                if unit_price is not None:
                    existing.unit_price = unit_price
                if is_rejected:
                    existing.is_rejected = True
                    existing.rejection_reason = rejection_reason
                    existing.rejected_at = existing.rejected_at or rejected_at
                    existing.apply_menu_item_weight_snapshot()
                    existing.save(update_fields=[
                        "quantity", "unit_price", "comment",
                        "menu_item_is_sold_by_weight", "menu_item_sale_unit",
                        "is_rejected", "rejection_reason", "rejected_at",
                    ])
                else:
                    existing.apply_menu_item_weight_snapshot()
                    existing.save(update_fields=[
                        "quantity", "unit_price", "comment",
                        "menu_item_is_sold_by_weight", "menu_item_sale_unit",
                    ])
            else:
                OrderItem.objects.create(
                    order=order,
                    menu_item=menu_item,
                    quantity=qty,
                    unit_price=unit_price,
                    company=order.company,
                    line_kind=OrderItem.LineKind.MENU,
                    comment=item_comment,
                    is_rejected=is_rejected,
                    rejection_reason=rejection_reason,
                    rejected_at=rejected_at,
                    menu_item_is_sold_by_weight=bool(menu_item.is_sold_by_weight),
                    menu_item_sale_unit=(
                        (menu_item.sale_unit or "kg").strip().lower()
                        if menu_item.is_sold_by_weight else "kg"
                    ),
                )

    def create(self, validated_data):
        items = validated_data.pop("items", [])
        obj = super().create(validated_data)
        if items:
            self._upsert_items(obj, items)
        return obj

    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)
        can_edit_closed_order = _is_owner_like(self._user())
        if items is not None and not can_edit_closed_order and (instance.is_paid or instance.status != Order.Status.OPEN):
            raise serializers.ValidationError({"items": "Изменение позиций доступно только у открытого неоплаченного заказа."})
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            if items is not None:
                from .models import KitchenTask

                active_tasks_by_menu = {}
                for task in KitchenTask.objects.filter(
                    order=instance,
                    status__in=[KitchenTask.Status.IN_PROGRESS, KitchenTask.Status.READY],
                ).values(
                    "menu_item_id", "unit_index", "status",
                    "cook_id", "started_at", "finished_at", "quantity",
                ):
                    mid = task["menu_item_id"]
                    if not mid:
                        continue
                    key = (mid, task["unit_index"])
                    active_tasks_by_menu[key] = task

                instance.items.all().delete()

                if items:
                    self._upsert_items(instance, items)

                if active_tasks_by_menu:
                    created_items_by_menu = {}
                    for item in instance.items.select_related("menu_item").all():
                        if not item.menu_item_id:
                            continue
                        created_items_by_menu.setdefault(item.menu_item_id, []).append(item)

                    tasks_to_restore = []
                    tasks_to_update = []
                    for (menu_item_id, unit_index), task_data in active_tasks_by_menu.items():
                        matching_items = created_items_by_menu.get(menu_item_id, [])
                        if not matching_items:
                            continue
                        matching_item = matching_items[0]
                        piece_qty = int(Decimal(matching_item.quantity or 0))
                        if unit_index > piece_qty and not matching_item.menu_item_is_sold_by_weight:
                            continue
                        existing_task = KitchenTask.objects.filter(
                            order_item=matching_item,
                            unit_index=unit_index,
                        ).first()
                        task_qty = (
                            matching_item.quantity
                            if matching_item.menu_item_is_sold_by_weight and unit_index == 1
                            else Decimal("1")
                        )
                        if existing_task:
                            existing_task.status = task_data["status"]
                            existing_task.cook_id = task_data["cook_id"]
                            existing_task.started_at = task_data["started_at"]
                            existing_task.finished_at = task_data["finished_at"]
                            existing_task.waiter = instance.waiter
                            existing_task.quantity = task_qty
                            tasks_to_update.append(existing_task)
                        else:
                            tasks_to_restore.append(
                                KitchenTask(
                                    company=instance.company,
                                    branch=instance.branch,
                                    order=instance,
                                    order_item=matching_item,
                                    menu_item_id=menu_item_id,
                                    waiter=instance.waiter,
                                    unit_index=unit_index,
                                    quantity=task_qty,
                                    status=task_data["status"],
                                    cook_id=task_data["cook_id"],
                                    started_at=task_data["started_at"],
                                    finished_at=task_data["finished_at"],
                                )
                            )
                    if tasks_to_update:
                        KitchenTask.objects.bulk_update(
                            tasks_to_update,
                            ["status", "cook_id", "started_at", "finished_at", "waiter_id", "quantity"],
                            batch_size=100,
                        )
                    if tasks_to_restore:
                        KitchenTask.objects.bulk_create(tasks_to_restore, ignore_conflicts=True)
        return instance
    

class OrderPaySerializer(serializers.Serializer):
    """
    Оплата заказа: полная оплата, долг, предоплата + долг (клиент /cafe/clients/).
    - payment_method=debt: полный долг или prepaid_amount + prepaid_payment_method.
    - payment_method=cash|card|transfer и pay_now < итога: остаток в долг (нужен client на заказе или client_id).
    При внесении суммы в долг (предоплата или pay_now) нужен idempotency_key (UUID).
    """
    payment_method = serializers.ChoiceField(
        choices=[
            ("cash", "Наличные"),
            ("card", "Безналичный (карта)"),
            ("transfer", "Безналичный (перевод)"),
            ("debt", "Долг"),
            ("split", "Смешанная оплата"),
        ],
        required=False,
        default="cash",
        help_text="debt — в долг; split — payments[] на полную сумму; иначе нал/безнал.",
    )
    payments = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_null=True,
        help_text='При payment_method=split: [{"method":"cash|card|transfer","amount":"100.00"}, ...]',
    )
    discount_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=Decimal("0"),
        help_text="Скидка на заказ (можно задать также при создании/редактировании заказа).",
    )
    close_order = serializers.BooleanField(required=False, default=True)
    client_id = serializers.UUIDField(required=False, allow_null=True, help_text="Гость кафе, если долг и не привязан к заказу.")
    pay_now = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
        help_text="Внести сейчас (cash|card|transfer); если меньше итога после скидки — остаток в долг.",
    )
    prepaid_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
        help_text="Только с payment_method=debt: сумма, внесённая сейчас до оформления долга на остаток.",
    )
    prepaid_payment_method = serializers.ChoiceField(
        choices=[
            ("cash", "Наличные"),
            ("card", "Безналичный (карта)"),
            ("transfer", "Безналичный (перевод)"),
        ],
        required=False,
        allow_null=True,
    )
    idempotency_key = serializers.UUIDField(
        required=False, allow_null=True,
        help_text="Обязателен при первом внесении денег в счёт долга (предоплата или pay_now < итога).",
    )
    cash_shift_id = serializers.UUIDField(
        required=False, allow_null=True,
        help_text="Опционально: привязать оплату к кассовой смене (construction), для отчёта смены.",
    )

    def validate_discount_amount(self, v):
        if v is None:
            return Decimal("0")
        if v < 0:
            raise serializers.ValidationError("discount_amount не может быть отрицательным.")
        return v

    def validate(self, attrs):
        pm = attrs.get("payment_method") or "cash"
        if pm == "debt" and attrs.get("pay_now") is not None:
            raise serializers.ValidationError({"pay_now": "С payment_method=debt используйте prepaid_amount, не pay_now."})
        if pm == "split":
            if attrs.get("pay_now") is not None or attrs.get("prepaid_amount") is not None:
                raise serializers.ValidationError({"payments": "При split используйте только payments."})
            raw = attrs.get("payments") or []
            if not raw:
                raise serializers.ValidationError({"payments": "Укажите payments для смешанной оплаты."})
            normalized = []
            allowed = {"cash", "card", "transfer"}
            for i, row in enumerate(raw):
                method = (row.get("method") or "").strip().lower()
                if method not in allowed:
                    raise serializers.ValidationError({f"payments[{i}].method": "Допустимо: cash, card, transfer."})
                try:
                    amount = Decimal(str(row.get("amount")).replace(",", "."))
                except Exception:
                    raise serializers.ValidationError({f"payments[{i}].amount": "Некорректная сумма."})
                if amount <= 0:
                    raise serializers.ValidationError({f"payments[{i}].amount": "Сумма должна быть > 0."})
                normalized.append({"method": method, "amount": amount})
            attrs["payments"] = normalized
        elif pm != "debt":
            if attrs.get("prepaid_amount") is not None:
                raise serializers.ValidationError({"prepaid_amount": "Только при payment_method=debt."})
            if attrs.get("prepaid_payment_method"):
                raise serializers.ValidationError({"prepaid_payment_method": "Только при payment_method=debt."})
            if attrs.get("payments"):
                raise serializers.ValidationError({"payments": "Только при payment_method=split."})
        prepaid = attrs.get("prepaid_amount")
        if pm == "debt" and prepaid is not None and prepaid > 0 and not attrs.get("prepaid_payment_method"):
            raise serializers.ValidationError(
                {"prepaid_payment_method": "Укажите способ внесения предоплаты (cash|card|transfer)."}
            )
        return attrs


class OrderPayDebtSerializer(serializers.Serializer):
    """Частичное или полное погашение долга по заказу: POST .../orders/<id>/pay-debt/"""
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    payment_method = serializers.ChoiceField(
        choices=[
            ("cash", "Наличные"),
            ("card", "Безналичный (карта)"),
            ("transfer", "Безналичный (перевод)"),
        ],
    )
    idempotency_key = serializers.UUIDField()
    note = serializers.CharField(required=False, allow_blank=True, default="")
    cash_shift_id = serializers.UUIDField(required=False, allow_null=True)
    cash_received = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
        help_text="Для cash: принято наличными (не меньше amount).",
    )

    def validate_amount(self, v):
        if v is None or v <= 0:
            raise serializers.ValidationError("Сумма должна быть больше нуля.")
        return v

    def validate(self, attrs):
        if attrs.get("payment_method") == "cash":
            cr = attrs.get("cash_received")
            if cr is None:
                raise serializers.ValidationError({"cash_received": "Для наличных укажите cash_received."})
            if cr < attrs["amount"]:
                raise serializers.ValidationError({"cash_received": "Меньше суммы платежа."})
        return attrs


class OrderRefundSerializer(serializers.Serializer):
    """Частичный/полный возврат: POST .../orders/<id>/refund/"""
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    payment_method = serializers.ChoiceField(
        choices=[
            ("cash", "Наличные"),
            ("card", "Безналичный (карта)"),
            ("transfer", "Безналичный (перевод)"),
        ],
    )
    idempotency_key = serializers.UUIDField()
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_amount(self, v):
        if v is None or v <= 0:
            raise serializers.ValidationError("Сумма должна быть больше нуля.")
        return v


class OrderItemRefundSerializer(serializers.Serializer):
    """Возврат по строке заказа: POST .../orders/<id>/refund-item/"""
    order_item_id = serializers.UUIDField()
    quantity = serializers.DecimalField(
        required=False, allow_null=True, max_digits=12, decimal_places=3, min_value=Decimal("0.001"),
    )
    payment_method = serializers.ChoiceField(
        choices=[
            ("cash", "Наличные"),
            ("card", "Безналичный (карта)"),
            ("transfer", "Безналичный (перевод)"),
        ],
    )
    idempotency_key = serializers.UUIDField()
    note = serializers.CharField(required=False, allow_blank=True, default="")


class InventoryItemSerializer(serializers.ModelSerializer):
    product_title = serializers.CharField(source="product.title", read_only=True)
    product_unit = serializers.CharField(source="product.unit", read_only=True)

    class Meta:
        model = InventoryItem
        fields = ["id", "product", "product_title", "product_unit",
                  "expected_qty", "actual_qty", "difference"]
        read_only_fields = ["id", "product_title", "product_unit", "difference"]

    def get_fields(self):
        fields = super().get_fields()
        holder = getattr(self, "root", None)
        if isinstance(holder, CompanyBranchReadOnlyMixin):
            fields["product"].queryset = _scope_queryset_by_context(Warehouse.objects.all(), holder)
        else:
            fields["product"].queryset = Warehouse.objects.none()
        return fields

    def validate(self, attrs):
        exp = attrs.get("expected_qty")
        act = attrs.get("actual_qty")
        if exp is None or act is None:
            return attrs
        if exp < 0 or act < 0:
            raise serializers.ValidationError({"actual_qty": "Кол-во не может быть отрицательным."})
        return attrs


class InventorySessionSerializer(CompanyBranchReadOnlyMixin):
    items = InventoryItemSerializer(many=True)

    class Meta:
        model = InventorySession
        fields = ["id", "company", "branch", "comment", "created_by",
                  "created_at", "confirmed_at", "is_confirmed", "items"]
        read_only_fields = ["id", "company", "branch", "created_by",
                            "created_at", "confirmed_at", "is_confirmed"]

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])
        with transaction.atomic():
            obj = super().create(validated_data)  # company/branch проставит миксин
            obj.created_by = getattr(self.context.get("request"), "user", None)
            obj.save(update_fields=["created_by"])

            if items_data:
                seen = set()
                bulk = []
                for it in items_data:
                    product = it["product"]
                    if product.pk in seen:
                        raise serializers.ValidationError(
                            {"items": f"Товар «{product.title}» повторяется в одном акте."}
                        )
                    seen.add(product.pk)
                    exp = it["expected_qty"]
                    act = it["actual_qty"]
                    bulk.append(InventoryItem(
                        session=obj,
                        product=product,
                        expected_qty=exp,
                        actual_qty=act,
                        difference=act - exp,  # важно: bulk_create не вызовет save()
                    ))
                InventoryItem.objects.bulk_create(bulk)
        return obj


# ==========================
# SERIALIZERS: Инвентаризация оборудования
# ==========================
# ==========================
# INVENTORY (оборудование)
# ==========================
class EquipmentSerializer(CompanyBranchReadOnlyMixin):
    expense_id = serializers.UUIDField(read_only=True, required=False, allow_null=True)
    expense_amount = serializers.CharField(read_only=True, required=False, allow_null=True)

    class Meta:
        model = Equipment
        fields = [
            "id", "company", "branch", "title", "serial_number",
            "category", "purchase_date", "price", "condition", "is_active", "notes",
            "expense_id", "expense_amount",
        ]
        read_only_fields = ["id", "company", "branch", "expense_id", "expense_amount"]


class EquipmentReceiveSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    note = serializers.CharField(required=False, allow_blank=True, default="")


class EquipmentInventoryItemSerializer(serializers.ModelSerializer):
    equipment_title = serializers.CharField(source="equipment.title", read_only=True)
    serial_number = serializers.CharField(source="equipment.serial_number", read_only=True)

    class Meta:
        model = EquipmentInventoryItem
        fields = ["id", "equipment", "equipment_title", "serial_number",
                  "is_present", "condition", "notes"]
        read_only_fields = ["id", "equipment_title", "serial_number"]

    def get_fields(self):
        fields = super().get_fields()
        holder = getattr(self, "root", None)
        if isinstance(holder, CompanyBranchReadOnlyMixin):
            fields["equipment"].queryset = _scope_queryset_by_context(Equipment.objects.all(), holder)
        else:
            fields["equipment"].queryset = Equipment.objects.none()
        return fields


class EquipmentInventorySessionSerializer(CompanyBranchReadOnlyMixin):
    items = EquipmentInventoryItemSerializer(many=True)

    class Meta:
        model = EquipmentInventorySession
        fields = ["id", "company", "branch", "comment", "created_by",
                  "created_at", "confirmed_at", "is_confirmed", "items"]
        read_only_fields = ["id", "company", "branch", "created_by",
                            "created_at", "confirmed_at", "is_confirmed"]

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])
        with transaction.atomic():
            obj = super().create(validated_data)
            obj.created_by = getattr(self.context.get("request"), "user", None)
            obj.save(update_fields=["created_by"])

            if items_data:
                seen = set()
                bulk = []
                for it in items_data:
                    eq = it["equipment"]
                    if eq.pk in seen:
                        raise serializers.ValidationError(
                            {"items": f"Оборудование «{eq.title}» повторяется в одном акте."}
                        )
                    seen.add(eq.pk)
                    bulk.append(EquipmentInventoryItem(
                        session=obj,
                        equipment=eq,
                        is_present=it.get("is_present", True),
                        condition=it.get("condition", Equipment.Condition.GOOD),
                        notes=it.get("notes", ""),
                    ))
                EquipmentInventoryItem.objects.bulk_create(bulk)
        return obj


# ===== Настройки принтера кассы (чековый принтер) =====

# IPv4: 1-3 цифр в каждой группе, 4 группы
_PRINTER_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_PRINTER_USB_RE = re.compile(r"^[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:.+$")  # vid:pid:serial


def _validate_printer_binding(value):
    """Проверка формата binding: ip/<ipv4>[:port] или usb/<vid>:<pid>:<serial>."""
    if not value or not isinstance(value, str):
        return
    s = value.strip()
    if not s:
        return
    if s.startswith("ip/"):
        rest = s[3:].strip()
        if ":" in rest:
            host, port_str = rest.rsplit(":", 1)
            host = host.strip()
            try:
                port = int(port_str.strip())
                if port < 1 or port > 65535:
                    raise serializers.ValidationError(
                        "Некорректный printer: порт должен быть от 1 до 65535."
                    )
            except ValueError:
                raise serializers.ValidationError(
                    "Некорректный printer: порт должен быть числом."
                )
        else:
            host = rest
        m = _PRINTER_IPV4_RE.match(host)
        if not m:
            raise serializers.ValidationError(
                "Некорректный printer: для Wi‑Fi укажите ip/<IPv4> или ip/<IPv4>:<порт>."
            )
        for g in m.groups():
            if int(g) > 255:
                raise serializers.ValidationError(
                    "Некорректный printer: неверный IPv4-адрес."
                )
    elif s.startswith("usb/"):
        rest = s[4:].strip()
        if not rest or ":" not in rest or rest.count(":") < 2:
            raise serializers.ValidationError(
                "Некорректный printer: для USB укажите usb/<vid>:<pid>:<serial> (например usb/1fc9:2016:noserial)."
            )
        parts = rest.split(":", 2)
        try:
            int(parts[0], 16)
            int(parts[1], 16)
        except ValueError:
            raise serializers.ValidationError(
                "Некорректный printer: vid и pid должны быть hex-числами."
            )
    else:
        raise serializers.ValidationError(
            "Некорректный printer: укажите ip/<IPv4>[:порт] или usb/<vid>:<pid>:<serial>."
        )


def _validate_bridge_url(value):
    """Если указан непустой URL — проверяем формат (http/https)."""
    if not value or not isinstance(value, str):
        return
    s = value.strip()
    if not s:
        return
    if not (s.startswith("http://") or s.startswith("https://")):
        raise serializers.ValidationError(
            "Некорректный bridge_url: укажите URL (http:// или https://)."
        )


class CafeReceiptPrinterSettingsSerializer(serializers.ModelSerializer):
    """Настройки принтера кассы: printer (binding) и bridge_url."""

    class Meta:
        model = CafeReceiptPrinterSettings
        fields = ["printer", "bridge_url", "updated_at"]
        read_only_fields = ["updated_at"]

    def validate_printer(self, value):
        if value is not None and str(value).strip():
            _validate_printer_binding(value)
        return value or ""

    def validate_bridge_url(self, value):
        if value is not None:
            _validate_bridge_url(value)
        return value or ""

    def to_representation(self, instance):
        if instance is None:
            return {"printer": "", "bridge_url": "", "updated_at": None}
        return super().to_representation(instance)


class CafeExpenseSerializer(CompanyBranchReadOnlyMixin):
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    category_id = serializers.UUIDField(source="expense_category_id", required=False, allow_null=True)

    class Meta:
        model = CafeExpense
        fields = [
            "id", "company", "branch", "title", "amount", "category", "category_slug",
            "category_id", "source", "source_id",
            "expense_date", "note", "created_by", "created_at",
        ]
        read_only_fields = [
            "id", "company", "created_by", "created_at",
            "source", "source_id", "category_slug",
        ]

    def validate(self, attrs):
        inst = self.instance
        if inst and inst.source and inst.source != CafeExpense.Source.MANUAL:
            blocked = {"source", "source_id", "category_slug"}
            if blocked & set(attrs.keys()):
                raise serializers.ValidationError(
                    {"detail": "Автоматический расход нельзя менять (source/category_slug)."}
                )
        return attrs

    def create(self, validated_data):
        req = self.context.get("request")
        if req and req.user.is_authenticated:
            validated_data["created_by"] = req.user
        validated_data.setdefault("source", CafeExpense.Source.MANUAL)
        return super().create(validated_data)


class CafeExpenseCategorySerializer(CompanyBranchReadOnlyMixin):
    class Meta:
        model = CafeExpenseCategory
        fields = ["id", "company", "branch", "title", "slug", "is_system", "sort_order"]
        read_only_fields = ["id", "company", "is_system"]

    def get_fields(self):
        fields = super().get_fields()
        if self.instance is not None:
            fields["slug"].read_only = True
        return fields


class WarehouseReceiveSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
    )
    supplier = serializers.UUIDField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class CafeHouseholdItemSerializer(CompanyBranchReadOnlyMixin):
    class Meta:
        model = CafeHouseholdItem
        fields = [
            "id", "company", "branch", "title", "sku", "unit",
            "remainder", "minimum", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company", "branch", "created_at", "updated_at"]


class CafeHouseholdMovementSerializer(serializers.ModelSerializer):
    class Meta:
        model = CafeHouseholdMovement
        fields = [
            "id", "movement_type", "quantity", "unit_price",
            "remainder_before", "remainder_after", "note", "created_at",
        ]
        read_only_fields = fields


class CafeHouseholdReceiveSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
    )
    note = serializers.CharField(required=False, allow_blank=True, default="")


class CafeHouseholdWriteOffSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class CafeHouseholdInventoryLineSerializer(serializers.ModelSerializer):
    difference = serializers.DecimalField(max_digits=12, decimal_places=3, read_only=True)
    item_title = serializers.CharField(source="item.title", read_only=True)

    class Meta:
        model = CafeHouseholdInventoryLine
        fields = ["id", "item", "item_title", "qty_book", "qty_counted", "difference"]
        read_only_fields = ["id", "qty_book", "difference"]


class CafeHouseholdInventorySessionSerializer(CompanyBranchReadOnlyMixin):
    lines = CafeHouseholdInventoryLineSerializer(many=True, required=False)
    items = serializers.ListField(write_only=True, required=False)
    status = serializers.CharField(read_only=True)

    class Meta:
        model = CafeHouseholdInventorySession
        fields = [
            "id", "company", "branch", "status", "comment",
            "created_by", "created_at", "confirmed_at", "lines", "items",
        ]
        read_only_fields = ["id", "company", "created_by", "created_at", "confirmed_at", "status"]

    def _lines_payload_from_data(self, validated_data):
        items_payload = validated_data.pop("items", None)
        lines_data = validated_data.pop("lines", None)
        if lines_data is not None:
            return lines_data
        if items_payload is not None:
            return items_payload
        return None

    def _sync_inventory_lines(self, session, lines_data):
        if lines_data is None:
            return
        company = session.company
        session.lines.all().delete()
        for row in lines_data:
            if not isinstance(row, dict):
                continue
            item_id = row.get("item")
            if not item_id:
                continue
            qty_counted = row.get("qty_counted")
            if qty_counted is None:
                raise serializers.ValidationError(
                    {"items": "Для каждой строки укажите qty_counted."}
                )
            item = CafeHouseholdItem.objects.filter(company=company, pk=item_id).first()
            if not item:
                raise serializers.ValidationError({"items": f"Позиция {item_id} не найдена."})
            CafeHouseholdInventoryLine.objects.create(
                session=session,
                item=item,
                qty_book=item.remainder,
                qty_counted=Decimal(str(qty_counted)),
            )

    def create(self, validated_data):
        lines_data = self._lines_payload_from_data(validated_data)
        if lines_data is None:
            lines_data = []
        req = self.context.get("request")
        if req and req.user.is_authenticated:
            validated_data["created_by"] = req.user
        session = super().create(validated_data)
        self._sync_inventory_lines(session, lines_data)
        return session

    def update(self, instance, validated_data):
        lines_data = self._lines_payload_from_data(validated_data)
        session = super().update(instance, validated_data)
        if lines_data is not None:
            if session.status != CafeHouseholdInventorySession.Status.DRAFT:
                raise serializers.ValidationError(
                    {"detail": "Редактировать строки можно только в черновике."}
                )
            self._sync_inventory_lines(session, lines_data)
        return session


class CafeWaiterPayProfileSerializer(CompanyBranchReadOnlyMixin):
    class Meta:
        model = CafeWaiterPayProfile
        fields = [
            "id", "company", "branch", "user",
            "monthly_base_salary", "revenue_percent",
        ]
        read_only_fields = ["id", "company"]

    def get_fields(self):
        fields = super().get_fields()
        company = self._user_company()
        if company and hasattr(User, "company_id"):
            fields["user"].queryset = User.objects.filter(company_id=company.id)
        return fields
