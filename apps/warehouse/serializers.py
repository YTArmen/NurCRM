import uuid
from io import BytesIO
from decimal import Decimal
from PIL import Image
from django.db import transaction

from django.apps import apps
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from rest_framework import serializers
from rest_framework.exceptions import APIException

from apps.warehouse import models as m
from apps.warehouse.models import q_qty
from apps.warehouse.utils import _active_branch, _restrict_pk_queryset_strict, normalize_payment_kind
from apps.utils import _is_owner_like


class CompanyBranchReadOnlyMixin:
    def _user(self):
        req = self.context.get("request")
        return getattr(req, "user", None) if req else None

    def _user_company(self):
        u = self._user()
        return getattr(u, "company", None) or getattr(u, "owned_company", None)

    def _auto_branch(self):
        return _active_branch(self)

    def create(self, validated_data):
        company = self._user_company()
        if company:
            validated_data.setdefault("company", company)
        if "branch" in getattr(self.Meta, "fields", []):
            validated_data["branch"] = self._auto_branch()
        return super().create(validated_data)

    def update(self, instance, validated_data):
        company = self._user_company()
        if company:
            validated_data["company"] = company
        if "branch" in getattr(self.Meta, "fields", []):
            validated_data["branch"] = self._auto_branch()
        return super().update(instance, validated_data)


# ----------------
# Simple serializers
# ----------------


class WarehouseSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source='company.id')
    branch = serializers.ReadOnlyField(source='branch.id')
    products_count = serializers.IntegerField(read_only=True)
    products_qty_total = serializers.DecimalField(max_digits=18, decimal_places=3, read_only=True)

    class Meta:
        model = m.Warehouse
        fields = ("id", "name", "location", "status", "company", "branch", "products_count", "products_qty_total")
        ref_name = "WarehouseWarehouseSerializer"


class BrandSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    parent = serializers.PrimaryKeyRelatedField(
        queryset=m.WarehouseProductBrand.objects.all(),
        allow_null=True,
        required=False,
    )

    class Meta:
        model = m.WarehouseProductBrand
        fields = ['id', 'company', 'branch', 'name', 'parent']
        read_only_fields = ['id', 'company', 'branch']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        _restrict_pk_queryset_strict(self.fields.get("parent"), m.WarehouseProductBrand.objects.all(), comp, br)

    def validate(self, attrs):
        """
        Валидируем уникальность имени до INSERT, чтобы не ловить 500 IntegrityError.
        Логика соответствует constraints в модели:
          - если branch != NULL -> уникальность (branch, name)
          - если branch == NULL -> уникальность (company, name) среди branch IS NULL
        """
        attrs = super().validate(attrs)

        name = attrs.get("name")
        if name is None:
            return attrs

        name = name.strip()
        attrs["name"] = name

        company = self._user_company()
        branch = self._auto_branch()

        qs = m.WarehouseProductBrand.objects.all()
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)

        if branch is not None:
            qs = qs.filter(branch=branch, name=name)
        else:
            if company is not None:
                qs = qs.filter(company=company, branch__isnull=True, name=name)
            else:
                qs = qs.filter(branch__isnull=True, name=name)

        if qs.exists():
            raise serializers.ValidationError({"name": "Бренд с таким названием уже существует."})

        return attrs


class CategorySerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")

    parent = serializers.PrimaryKeyRelatedField(
        queryset=m.WarehouseProductCategory.objects.all(),
        allow_null=True,
        required=False,
    )

    class Meta:
        model = m.WarehouseProductCategory
        fields = ['id', 'company', 'branch', 'name', 'parent']
        read_only_fields = ['id', 'company', 'branch']
        ref_name = "WarehouseCategory"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comp = self._user_company()
        br = self._auto_branch()
        _restrict_pk_queryset_strict(self.fields.get("parent"), m.WarehouseProductCategory.objects.all(), comp, br)

    def validate(self, attrs):
        """
        Валидируем уникальность имени до INSERT, чтобы не ловить 500 IntegrityError.
        Логика соответствует constraints в модели:
          - если branch != NULL -> уникальность (branch, name)
          - если branch == NULL -> уникальность (company, name) среди branch IS NULL
        """
        attrs = super().validate(attrs)

        name = attrs.get("name")
        if name is None:
            return attrs

        name = name.strip()
        attrs["name"] = name

        company = self._user_company()
        branch = self._auto_branch()

        qs = m.WarehouseProductCategory.objects.all()
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)

        if branch is not None:
            qs = qs.filter(branch=branch, name=name)
        else:
            if company is not None:
                qs = qs.filter(company=company, branch__isnull=True, name=name)
            else:
                qs = qs.filter(branch__isnull=True, name=name)

        if qs.exists():
            raise serializers.ValidationError({"name": "Категория с таким названием уже существует."})

        return attrs


class WarehouseProductGroupSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    """Группа товаров внутри склада (иерархия как в 1С)."""
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")
    warehouse = serializers.PrimaryKeyRelatedField(queryset=m.Warehouse.objects.all(), required=False)
    parent = serializers.PrimaryKeyRelatedField(
        queryset=m.WarehouseProductGroup.objects.all(),
        allow_null=True,
        required=False,
    )
    # products_count — товары в группе И во всех вложенных (как в 1С);
    # products_count_direct — только непосредственно в этой группе.
    products_count = serializers.SerializerMethodField()
    products_count_direct = serializers.SerializerMethodField()

    class Meta:
        model = m.WarehouseProductGroup
        fields = ["id", "company", "branch", "warehouse", "name", "parent", "products_count", "products_count_direct"]
        read_only_fields = ["id", "company", "branch"]

    def _group_totals(self, warehouse_id):
        """{group_id: (direct, total_with_descendants)} по складу; считается один раз на запрос."""
        cache = self.context.setdefault("_group_totals_cache", {})
        if warehouse_id in cache:
            return cache[warehouse_id]
        from django.db.models import Count

        rows = list(
            m.WarehouseProductGroup.objects.filter(warehouse_id=warehouse_id)
            .annotate(c=Count("products", distinct=True))
            .values_list("id", "parent_id", "c")
        )
        direct = {gid: c for gid, _pid, c in rows}
        parent = {gid: pid for gid, pid, _c in rows}
        total = dict(direct)
        for gid, c in direct.items():
            seen = {gid}
            pid = parent.get(gid)
            while pid is not None and pid in total and pid not in seen:
                total[pid] += c
                seen.add(pid)
                pid = parent.get(pid)
        out = {gid: (direct[gid], total[gid]) for gid in direct}
        cache[warehouse_id] = out
        return out

    def get_products_count(self, obj):
        return self._group_totals(obj.warehouse_id).get(obj.pk, (0, 0))[1]

    def get_products_count_direct(self, obj):
        return self._group_totals(obj.warehouse_id).get(obj.pk, (0, 0))[0]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        warehouse = self.context.get("warehouse")
        if warehouse:
            self.fields["warehouse"].queryset = m.Warehouse.objects.filter(pk=warehouse.pk)
            qs = m.WarehouseProductGroup.objects.filter(warehouse=warehouse)
            self.fields["parent"].queryset = qs

    def create(self, validated_data):
        warehouse = self.context.get("warehouse")
        if warehouse:
            validated_data["warehouse"] = warehouse
        return super().create(validated_data)


# ----------------
# Product related
# ----------------


class WarehouseProductCharacteristicsSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    class Meta:
        model = m.WarehouseProductCharasteristics
        fields = (
            "height_cm",
            "width_cm",
            "depth_cm",
            "factual_weight_kg",
            "description",
        )


def _to_webp(uploaded_file, *, quality: int = 82) -> ContentFile:
    uploaded_file.seek(0)
    img = Image.open(uploaded_file)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=quality, method=6)
    buf.seek(0)
    base_name = (getattr(uploaded_file, "name", "image") or "image").rsplit(".", 1)[0]
    return ContentFile(buf.read(), name=f"{base_name}.webp")


class WarehouseProductImageSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    image = serializers.ImageField(write_only=True, required=True)
    image_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = m.WarehouseProductImage
        fields = ("id", "product", "image", "image_url", "is_primary", "alt", "created_at")
        read_only_fields = ("id", "product", "created_at")

    def get_image_url(self, obj):
        request = self.context.get("request")
        if not getattr(obj, "image", None):
            return None
        url = obj.image.url
        return request.build_absolute_uri(url) if request else url

    def validate_image(self, value):
        ct = getattr(value, "content_type", "") or ""
        allowed = {"image/jpeg", "image/png", "image/webp"}
        if ct and ct not in allowed:
            raise serializers.ValidationError("Разрешены только JPG, PNG, WEBP.")
        return value

    def create(self, validated_data):
        uploaded = validated_data.pop("image", None)
        if uploaded:
            validated_data["image"] = _to_webp(uploaded)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        uploaded = validated_data.pop("image", None)
        if uploaded:
            validated_data["image"] = _to_webp(uploaded)
        return super().update(instance, validated_data)


class WarehouseProductPackageSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    class Meta:
        model = m.WarehouseProductPackage
        fields = ("id", "product", "name", "quantity_in_package", "unit", "created_at")
        read_only_fields = ("id", "product", "created_at")


def _norm_str(v):
    s = (v or "").strip()
    return s or None


def _to_decimal(v, default="0"):
    try:
        return Decimal(str(v)) if v is not None else Decimal(default)
    except Exception:
        return Decimal(default)


def _warehouse_product_unit_price_after_discount(product: m.WarehouseProduct) -> Decimal:
    """Цена за единицу с учётом скидки с карточки товара (для списка агента / корзины)."""
    price = _to_decimal(getattr(product, "price", None), "0")
    pct = _to_decimal(getattr(product, "discount_percent", None), "0")
    if pct <= 0:
        return price.quantize(Decimal("0.001"))
    return (price * (Decimal("1") - pct / Decimal("100"))).quantize(Decimal("0.001"))


def _get_characteristics_model():
    for label in (
        "warehouse.WarehouseProductCharacteristics",
        "warehouse.WarehouseProductCharasteristics",
    ):
        try:
            return apps.get_model(label)
        except Exception:
            continue
    return None


def _sync_warehouse_product_alternate_barcodes(product: m.WarehouseProduct, codes):
    """
    Полная замена доп. штрихкодов для товара. codes — список строк или словарей {barcode, name, quantity}.
    """
    company = product.company
    warehouse = product.warehouse
    main = _norm_str(product.barcode)
    norm = []
    seen = set()
    for x in codes or []:
        if isinstance(x, dict):
            b = _norm_str(x.get("barcode"))
            n = str(x.get("name") or "").strip()
            raw_qty = x.get("quantity")
        else:
            b = _norm_str(x)
            n = ""
            raw_qty = None
        if not b or b in seen:
            continue
        seen.add(b)
        qty = None
        if raw_qty is not None and str(raw_qty).strip() != "":
            try:
                val = float(raw_qty)
                if val <= 0 or not val.is_integer():
                    raise ValueError()
                qty = int(val)
            except (ValueError, TypeError):
                raise serializers.ValidationError(
                    {"alternate_barcodes": f"Количество в доп. штрихкоде '{b}' должно быть положительным числом."}
                )
        norm.append((b, n, qty))

    for b, n, _ in norm:
        if main and b == main:
            raise serializers.ValidationError(
                {"alternate_barcodes": f"Код «{b}» совпадает с основным штрихкодом товара — оставьте его только в поле barcode."}
            )
        if (
            m.WarehouseProduct.objects.filter(company=company, warehouse=warehouse, barcode=b)
            .exclude(pk=product.pk)
            .exists()
        ):
            raise serializers.ValidationError(
                {"alternate_barcodes": f"Штрихкод «{b}» уже занят другим товаром на этом складе (основной код)."}
            )
        if (
            m.WarehouseProductAlternateBarcode.objects.filter(
                product__company=company,
                product__warehouse=warehouse,
                barcode=b,
            )
            .exclude(product_id=product.pk)
            .exists()
        ):
            raise serializers.ValidationError(
                {"alternate_barcodes": f"Штрихкод «{b}» уже привязан к другому товару на этом складе."}
            )
    product.alternate_barcodes.all().delete()
    for b, n, q in norm:
        m.WarehouseProductAlternateBarcode.objects.create(product=product, barcode=b, name=n, quantity=q)


QUANTITY_READ_ONLY_ERROR = (
    "Количество меняется только документами: инвентаризация, оприходование или списание. "
    "Используйте «Корректировку остатка»."
)


QUANTITY_EDIT_COMMENT = "Изменение количества в карточке товара"


class StockConflict(APIException):
    status_code = 409
    default_detail = "Конфликт остатка."
    default_code = "stock_conflict"


def save_product_changed_fields(instance, keys):
    """
    instance.save(update_fields=[...]) только по изменённым полям. Полный save()
    перезаписал бы quantity значением, прочитанным в начале запроса, и затёр бы
    списание, проведённое за это время (гонка из S2). quantity сюда не попадает никогда.
    """
    concrete = {f.name for f in instance._meta.concrete_fields}
    fields = {k for k in keys if k in concrete} - {"id", "quantity", "company", "branch", "warehouse"}
    # save() пересчитывает цену/наценку и автогенерирует code/plu.
    if fields & {"price", "purchase_price", "markup_percent"}:
        fields |= {"price", "markup_percent"}
    if not instance.code:
        fields.add("code")
    if instance.is_weight and instance.plu is None:
        fields.add("plu")
    fields.add("updated_date")
    instance.save(update_fields=sorted(fields))


class StockQuantityGuardMixin:
    """
    Правила поля quantity в API товара (§5.4 stock-single-source-of-truth):
    - update: изменённое количество из формы проводится документом INVENTORY
      («Изменение количества в карточке товара»); совпадающее — ничего не делает;
    - create: quantity > 0 — начальный остаток проведённым документом INVENTORY;
    - upsert по штрихкоду не меняет quantity существующего товара (quantity > 0 → 409).
    Поле quantity объявляется в сериализаторе явно (DecimalField, required=False, allow_null=True).
    """

    def validate_quantity(self, value):
        if value is None:
            return None
        value = q_qty(Decimal(value))
        if value < 0:
            raise serializers.ValidationError("Количество не может быть отрицательным.")
        return value

    def _request_user(self):
        req = self.context.get("request")
        return getattr(req, "user", None) if req is not None else None

    def _post_initial_stock(self, product, qty):
        """Начальный остаток нового товара — проведённый INVENTORY (в текущей транзакции)."""
        if qty is None or q_qty(Decimal(qty)) <= 0:
            return
        from apps.warehouse import stock as stock_service

        try:
            stock_service.post_inventory_adjustment(
                product=product,
                fact_qty=qty,
                comment=stock_service.INITIAL_STOCK_COMMENT,
                user=self._request_user(),
            )
        except ValueError as exc:
            raise serializers.ValidationError({"quantity": [str(exc)]})
        product.refresh_from_db(fields=["quantity"])

    def _apply_quantity_change(self, product, qty):
        """
        Количество из формы редактирования: регистр не правится напрямую, а проводится
        документ INVENTORY на одну позицию (как «Корректировка остатка»), с движением.
        """
        if qty is None or product.warehouse_id is None:
            return
        from apps.warehouse import stock as stock_service

        current = stock_service.get_on_hand(warehouse=product.warehouse, product=product)
        if q_qty(Decimal(qty)) == current:
            return
        if not product.is_weight and Decimal(qty) % 1 != 0:
            raise serializers.ValidationError({"quantity": ["Для штучного товара количество должно быть целым."]})
        try:
            stock_service.post_inventory_adjustment(
                product=product,
                fact_qty=qty,
                comment=QUANTITY_EDIT_COMMENT,
                user=self._request_user(),
            )
        except ValueError as exc:
            raise serializers.ValidationError({"quantity": [str(exc)]})
        product.refresh_from_db(fields=["quantity"])

    @staticmethod
    def _raise_existing_barcode_conflict(existing, qty):
        raise StockConflict({
            "quantity": [
                f"Товар со штрихкодом {existing.barcode} уже есть на этом складе "
                f"(остаток {q_qty(Decimal(existing.quantity or 0))}). Количество существующего товара "
                "не меняется при сохранении карточки: оформите приход или «Корректировку остатка»."
            ],
            "product_id": str(existing.pk),
        })


class WarehouseProductSerializer(StockQuantityGuardMixin, CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    characteristics = WarehouseProductCharacteristicsSerializer(required=False, allow_null=True)
    images = WarehouseProductImageSerializer(many=True, read_only=True)
    packages = WarehouseProductPackageSerializer(many=True, read_only=True)
    alternate_barcodes = serializers.ListField(
        required=False,
        write_only=True,
        allow_null=True,
    )
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, allow_null=True)
    # Остаток: на update read-only (см. StockQuantityGuardMixin), на create — начальный остаток.
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, required=False, allow_null=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        warehouse = self.context.get("warehouse")
        if warehouse and "product_group" in self.fields:
            self.fields["product_group"].queryset = m.WarehouseProductGroup.objects.filter(warehouse=warehouse)
        if "supplier" in self.fields:
            supplier_qs = m.Counterparty.objects.filter(
                type__in=[m.Counterparty.Type.SUPPLIER, m.Counterparty.Type.BOTH]
            )
            company = self._user_company()
            if company is not None:
                supplier_qs = supplier_qs.filter(company=company)
            self.fields["supplier"].queryset = supplier_qs

    class Meta:
        ref_name = "WarehouseProductSerializer"
        model = m.WarehouseProduct
        fields = [
            "id",
            "name",
            "article",
            "description",
            "barcode",
            "code",
            "unit",
            "is_weight",
            "is_adult",
            "quantity",
            "minimum_quantity",
            "purchase_price",
            "markup_percent",
            "price",
            "wholesale_price",
            "discount_percent",
            "plu",
            "country",
            "status",
            "stock",
            "expiration_date",
            "brand",
            "category",
            "product_group",
            "warehouse",
            "supplier",
            "supplier_name",
            "characteristics",
            "images",
            "packages",
            "alternate_barcodes",
        ]
        read_only_fields = ["id", "company", "branch"]
        extra_kwargs = {
            "category": {"required": False, "allow_null": True},
            "article": {"required": False, "allow_null": True},
            "country": {"required": False, "allow_null": True},
            "characteristics": {"required": False, "allow_null": True},
            "supplier": {"required": False, "allow_null": True},
        }

    def validate(self, attrs):
        if "barcode" in attrs:
            attrs["barcode"] = _norm_str(attrs.get("barcode"))
        if "code" in attrs:
            attrs["code"] = _norm_str(attrs.get("code"))
        if "article" in attrs:
            attrs["article"] = (attrs.get("article") or "").strip()
        if "country" in attrs:
            attrs["country"] = (attrs.get("country") or "").strip()

        for f in ("minimum_quantity", "purchase_price", "markup_percent", "price", "wholesale_price", "discount_percent"):
            if f in attrs:
                attrs[f] = _to_decimal(attrs.get(f), "0")

        if "alternate_barcodes" in attrs and attrs["alternate_barcodes"] is not None:
            seen = set()
            norm_list = []
            for raw in attrs["alternate_barcodes"]:
                if isinstance(raw, dict):
                    b = _norm_str(raw.get("barcode"))
                    n = str(raw.get("name") or "").strip()
                    raw_qty = raw.get("quantity")
                else:
                    b = _norm_str(raw)
                    n = ""
                    raw_qty = None
                if not b or b in seen:
                    continue
                seen.add(b)
                qty = None
                if raw_qty is not None and str(raw_qty).strip() != "":
                    try:
                        val = float(raw_qty)
                        if val <= 0 or not val.is_integer():
                            raise ValueError()
                        qty = int(val)
                    except (ValueError, TypeError):
                        raise serializers.ValidationError({
                            "alternate_barcodes": f"Количество в доп. штрихкоде '{b}' должно быть положительным числом."
                        })
                norm_list.append({"barcode": b, "name": n, "quantity": qty})
            attrs["alternate_barcodes"] = norm_list

        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["alternate_barcodes"] = [
            {"barcode": item.barcode, "name": item.name, "quantity": item.quantity}
            for item in sorted(instance.alternate_barcodes.all(), key=lambda x: x.barcode or "")  # perf1311b
        ]
        return data

    def _upsert_characteristics(self, product: m.WarehouseProduct, characteristics_data):
        if not characteristics_data:
            return

        Model = _get_characteristics_model()
        if Model is None:
            return

        fk_field = "product"
        if "product" not in {f.name for f in Model._meta.get_fields()}:
            fk_field = "warehouse_product" if "warehouse_product" in {f.name for f in Model._meta.get_fields()} else "product"

        defaults = dict(characteristics_data)
        Model.objects.update_or_create(**{fk_field: product}, defaults=defaults)

    def create(self, validated_data):
        initial_qty = validated_data.pop("quantity", None)
        characteristics_data = validated_data.pop("characteristics", None)
        alternate_barcodes = validated_data.pop("alternate_barcodes", None)
        # initial_data может быть dict (JSON) или QueryDict (form/multipart) — оба поддерживают `in`
        has_alt = "alternate_barcodes" in self.initial_data

        barcode = _norm_str(validated_data.get("barcode"))
        company = validated_data.get("company")
        warehouse = validated_data.get("warehouse")

        with transaction.atomic():
            if barcode and company and warehouse:
                existing = (
                    m.WarehouseProduct.objects
                    .select_for_update()
                    .filter(company=company, warehouse=warehouse, barcode=barcode)
                    .first()
                )
                if existing:
                    # Upsert по штрихкоду обновляет карточку, но не остаток.
                    if initial_qty is not None and initial_qty > 0:
                        self._raise_existing_barcode_conflict(existing, initial_qty)
                    validated_data.pop("company", None)
                    validated_data.pop("branch", None)
                    validated_data.pop("warehouse", None)

                    for k, v in validated_data.items():
                        setattr(existing, k, v)

                    existing.barcode = barcode
                    save_product_changed_fields(existing, set(validated_data) | {"barcode"})

                    self._upsert_characteristics(existing, characteristics_data)
                    if has_alt:
                        _sync_warehouse_product_alternate_barcodes(existing, alternate_barcodes or [])
                    return existing

            validated_data["quantity"] = Decimal("0.000")
            product = m.WarehouseProduct.objects.create(**validated_data)
            self._upsert_characteristics(product, characteristics_data)
            if has_alt:
                _sync_warehouse_product_alternate_barcodes(product, alternate_barcodes or [])
            self._post_initial_stock(product, initial_qty)
            return product

    @transaction.atomic
    def update(self, instance, validated_data):
        new_qty = validated_data.pop("quantity", None)  # проводится документом, см. _apply_quantity_change
        characteristics_data = validated_data.pop("characteristics", None)
        alternate_barcodes = validated_data.pop("alternate_barcodes", None)
        has_alt = "alternate_barcodes" in self.initial_data
        validated_data.pop("warehouse", None)
        validated_data.pop("company", None)
        validated_data.pop("branch", None)

        if "barcode" in validated_data:
            validated_data["barcode"] = _norm_str(validated_data.get("barcode"))

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        save_product_changed_fields(instance, set(validated_data))
        self._upsert_characteristics(instance, characteristics_data)
        if has_alt:
            _sync_warehouse_product_alternate_barcodes(instance, alternate_barcodes or [])
        self._apply_quantity_change(instance, new_qty)
        return instance


class WarehouseProductCatalogSerializer(serializers.ModelSerializer):
    """
    Лёгкий сериализатор для каталога товаров (списки на странице создания документа).
    Без вложенных images/packages/characteristics — только поля, нужные каталогу.
    FK (brand/category/product_group/warehouse/supplier) отдаются как id (uuid) + *_name.
    """
    brand_name = serializers.CharField(source="brand.name", read_only=True, allow_null=True)
    product_group_name = serializers.CharField(source="product_group.name", read_only=True, allow_null=True)
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True, allow_null=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, allow_null=True)

    class Meta:
        ref_name = "WarehouseProductCatalogSerializer"
        model = m.WarehouseProduct
        fields = (
            "id",
            "name",
            "article",
            "barcode",
            "unit",
            "is_weight",
            "plu",
            "code",
            "catalog_key",
            "quantity",
            "minimum_quantity",
            "purchase_price",
            "price",
            "wholesale_price",
            "discount_percent",
            "brand",
            "brand_name",
            "category",
            "product_group",
            "product_group_name",
            "warehouse",
            "warehouse_name",
            "supplier",
            "supplier_name",
            "status",
        )


# ----------------
# Agent flows
# ----------------


class AgentRequestItemSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_article = serializers.CharField(source="product.article", read_only=True, allow_blank=True)
    product_unit = serializers.CharField(source="product.unit", read_only=True, default="шт.")
    qty = serializers.DecimalField(
        source="quantity_requested",
        max_digits=18,
        decimal_places=3,
        read_only=True,
    )
    price = serializers.DecimalField(
        source="product.price",
        max_digits=11,
        decimal_places=3,
        read_only=True,
    )
    discount_percent = serializers.DecimalField(
        source="product.discount_percent",
        max_digits=12,
        decimal_places=2,
        read_only=True,
    )
    discount_amount = serializers.SerializerMethodField()
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = m.AgentRequestItem
        ref_name = "WarehouseAgentRequestItem"
        fields = (
            "id",
            "cart",
            "product",
            "product_name",
            "product_article",
            "product_unit",
            "quantity_requested",
            "qty",
            "price",
            "discount_percent",
            "discount_amount",
            "line_total",
            "created_date",
            "updated_date",
        )
        read_only_fields = ("id", "created_date", "updated_date")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        cart = attrs.get("cart") or getattr(getattr(self, "instance", None), "cart", None)
        product = attrs.get("product") or getattr(getattr(self, "instance", None), "product", None)
        qty = attrs.get("quantity_requested")
        if qty is None and getattr(self, "instance", None) is not None:
            qty = getattr(self.instance, "quantity_requested", None)
        if not cart or not product or qty is None:
            return attrs
        if cart.status != m.AgentRequestCart.Status.DRAFT:
            return attrs
        need = q_qty(Decimal(qty or 0))
        exclude_item_id = getattr(getattr(self, "instance", None), "pk", None)
        available = cart.warehouse_available_qty(product, exclude_item_id=exclude_item_id)
        if need > available:
            raise serializers.ValidationError({
                "quantity_requested": (
                    f"Недостаточно на складе для {product.name}: "
                    f"запрошено {need}, доступно {available}."
                )
            })
        return attrs

    def get_discount_amount(self, obj):
        price = getattr(obj.product, "price", None) or Decimal("0")
        qty = getattr(obj, "quantity_requested", None) or Decimal("0")
        pct = getattr(obj.product, "discount_percent", None) or Decimal("0")
        subtotal = price * qty
        return (subtotal * pct / Decimal("100")).quantize(Decimal("0.01"))

    def get_line_total(self, obj):
        price = getattr(obj.product, "price", None) or Decimal("0")
        qty = getattr(obj, "quantity_requested", None) or Decimal("0")
        pct = getattr(obj.product, "discount_percent", None) or Decimal("0")
        subtotal = price * qty
        discount = subtotal * pct / Decimal("100")
        return (subtotal - discount).quantize(Decimal("0.01"))


class AgentRequestCartItemInputSerializer(serializers.Serializer):
    """Позиция при создании заявки одним запросом вместе с cart."""
    product = serializers.PrimaryKeyRelatedField(queryset=m.WarehouseProduct.objects.all())
    quantity_requested = serializers.DecimalField(max_digits=18, decimal_places=3)


class AgentRequestCartSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    items = AgentRequestItemSerializer(many=True, read_only=True)
    items_input = AgentRequestCartItemInputSerializer(many=True, required=False, write_only=True)
    agent_display = serializers.SerializerMethodField()
    note = serializers.CharField(required=False, allow_blank=True)
    sale_document_number = serializers.CharField(source="sale_document.number", read_only=True, allow_null=True)

    class Meta:
        model = m.AgentRequestCart
        ref_name = "WarehouseAgentRequestCart"
        fields = (
            "id",
            "agent",
            "agent_display",
            "warehouse",
            "status",
            "note",
            "submitted_at",
            "approved_at",
            "approved_by",
            "auto_approved",
            "sale_document",
            "sale_document_number",
            "created_date",
            "updated_date",
            "items",
            "items_input",
        )
        read_only_fields = (
            "id",
            "status",
            "submitted_at",
            "approved_at",
            "approved_by",
            "auto_approved",
            "sale_document",
            "sale_document_number",
            "created_date",
            "updated_date",
        )
        extra_kwargs = {
            "agent": {"required": False},
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        if self.instance is not None or not user or not _is_owner_like(user):
            return attrs

        warehouse = attrs.get("warehouse")
        agent = attrs.get("agent")
        if not agent:
            raise serializers.ValidationError({"agent": "Укажите агента, которому отправляете товар."})
        if warehouse:
            try:
                m.CompanyWarehouseAgent.ensure_active_for_warehouse(agent, warehouse)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(getattr(exc, "message_dict", {"agent": str(exc)}))
        return attrs

    def get_agent_display(self, obj):
        agent = getattr(obj, "agent", None)
        if not agent:
            return None
        full_name = ""
        if hasattr(agent, "get_full_name"):
            try:
                full_name = agent.get_full_name() or ""
            except Exception:
                full_name = ""
        if not full_name:
            first = getattr(agent, "first_name", "") or ""
            last = getattr(agent, "last_name", "") or ""
            full_name = f"{first} {last}".strip()
        return full_name or getattr(agent, "username", None) or getattr(agent, "email", None) or str(getattr(agent, "id", ""))


class AgentReturnItemSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_article = serializers.CharField(source="product.article", read_only=True, allow_blank=True)
    product_unit = serializers.CharField(source="product.unit", read_only=True, default="шт.")
    qty = serializers.DecimalField(
        source="quantity_returned",
        max_digits=18,
        decimal_places=3,
        read_only=True,
    )
    price = serializers.DecimalField(
        source="product.price",
        max_digits=11,
        decimal_places=3,
        read_only=True,
    )

    class Meta:
        model = m.AgentReturnItem
        ref_name = "WarehouseAgentReturnItem"
        fields = (
            "id",
            "cart",
            "product",
            "product_name",
            "product_article",
            "product_unit",
            "quantity_returned",
            "qty",
            "price",
            "created_date",
            "updated_date",
        )
        read_only_fields = ("id", "created_date", "updated_date")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        cart = attrs.get("cart") or getattr(getattr(self, "instance", None), "cart", None)
        product = attrs.get("product") or getattr(getattr(self, "instance", None), "product", None)
        qty = attrs.get("quantity_returned")
        if qty is None and getattr(self, "instance", None) is not None:
            qty = getattr(self.instance, "quantity_returned", None)
        if not cart or not product or qty is None:
            return attrs
        if cart.status != m.AgentReturnCart.Status.DRAFT:
            return attrs
        need = q_qty(Decimal(qty or 0))
        exclude_item_id = getattr(getattr(self, "instance", None), "pk", None)
        available = cart._agent_available_qty(product, exclude_item_id=exclude_item_id)
        if need > available:
            raise serializers.ValidationError({
                "quantity_returned": (
                    f"Недостаточно у агента для {product.name}: "
                    f"нужно {need}, доступно {available}."
                )
            })
        return attrs


class AgentReturnCartItemInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=m.WarehouseProduct.objects.all())
    quantity_returned = serializers.DecimalField(max_digits=18, decimal_places=3)


class AgentReturnCartSerializer(CompanyBranchReadOnlyMixin, serializers.ModelSerializer):
    items = AgentReturnItemSerializer(many=True, read_only=True)
    items_input = AgentReturnCartItemInputSerializer(many=True, required=False, write_only=True)
    agent_display = serializers.SerializerMethodField()
    note = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = m.AgentReturnCart
        ref_name = "WarehouseAgentReturnCart"
        fields = (
            "id",
            "agent",
            "agent_display",
            "warehouse",
            "status",
            "note",
            "submitted_at",
            "approved_at",
            "approved_by",
            "created_date",
            "updated_date",
            "items",
            "items_input",
        )
        read_only_fields = (
            "id",
            "status",
            "submitted_at",
            "approved_at",
            "approved_by",
            "created_date",
            "updated_date",
        )
        extra_kwargs = {
            "warehouse": {"required": True},
            "agent": {"required": False},
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        if self.instance is not None:
            return attrs
        if user and _is_owner_like(user):
            agent = attrs.get("agent")
            warehouse = attrs.get("warehouse")
            if not agent:
                raise serializers.ValidationError({"agent": "Укажите агента, у которого принимаете возврат."})
            if warehouse:
                try:
                    m.CompanyWarehouseAgent.ensure_active_for_warehouse(agent, warehouse)
                except DjangoValidationError as exc:
                    raise serializers.ValidationError(getattr(exc, "message_dict", {"agent": str(exc)}))
        return attrs

    def get_agent_display(self, obj):
        agent = getattr(obj, "agent", None)
        if not agent:
            return None
        full_name = ""
        if hasattr(agent, "get_full_name"):
            try:
                full_name = agent.get_full_name() or ""
            except Exception:
                full_name = ""
        if not full_name:
            first = getattr(agent, "first_name", "") or ""
            last = getattr(agent, "last_name", "") or ""
            full_name = f"{first} {last}".strip()
        return full_name or getattr(agent, "username", None) or getattr(agent, "email", None) or str(getattr(agent, "id", ""))


class AgentReturnCartActionSerializer(serializers.Serializer):
    pass


# ----------------
# Company / Agent membership (заявки агентов в компании)
# ----------------


class CompanyWarehouseAgentSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    user_display = serializers.SerializerMethodField()
    decided_by_display = serializers.SerializerMethodField()
    assigned_warehouse = serializers.PrimaryKeyRelatedField(read_only=True)
    common_access_enabled = serializers.BooleanField(read_only=True)
    common_warehouse = serializers.PrimaryKeyRelatedField(read_only=True)
    common_warehouses = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    common_all_warehouses = serializers.BooleanField(read_only=True)
    can_sell_wholesale = serializers.BooleanField(read_only=True)
    can_sell_without_approval = serializers.BooleanField(read_only=True)

    class Meta:
        model = m.CompanyWarehouseAgent
        fields = (
            "id",
            "company",
            "company_name",
            "user",
            "user_display",
            "status",
            "note",
            "assigned_warehouse",
            "common_access_enabled",
            "common_warehouse",
            "common_warehouses",
            "common_all_warehouses",
            "can_sell_wholesale",
            "can_sell_without_approval",
            "created_at",
            "updated_at",
            "decided_at",
            "decided_by",
            "decided_by_display",
        )
        read_only_fields = (
            "id",
            "status",
            "assigned_warehouse",
            "common_access_enabled",
            "common_warehouse",
            "common_warehouses",
            "common_all_warehouses",
            "can_sell_wholesale",
            "can_sell_without_approval",
            "created_at",
            "updated_at",
            "decided_at",
            "decided_by",
        )
        extra_kwargs = {"company": {"required": True}, "user": {"required": False}, "note": {"required": False, "allow_blank": True}}

    def get_user_display(self, obj):
        u = getattr(obj, "user", None)
        if not u:
            return None
        if hasattr(u, "get_full_name") and u.get_full_name():
            return u.get_full_name()
        return (
            f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}".strip()
            or getattr(u, "email", "")
            or str(u.id)
        )

    def get_decided_by_display(self, obj):
        u = getattr(obj, "decided_by", None)
        if not u:
            return None
        return getattr(u, "email", None) or str(u.id)


class CompanyWarehouseAgentCommonAccessUpdateSerializer(serializers.ModelSerializer):
    assigned_warehouse = serializers.PrimaryKeyRelatedField(
        queryset=m.Warehouse.objects.all(),
        required=False,
        allow_null=True,
    )
    common_access_enabled = serializers.BooleanField(required=False)
    # Legacy-поле: один склад. Оставлено для старого фронта.
    common_warehouse = serializers.PrimaryKeyRelatedField(
        queryset=m.Warehouse.objects.all(),
        required=False,
        allow_null=True,
    )
    # Новое поле: несколько складов общего доступа.
    common_warehouses = serializers.PrimaryKeyRelatedField(
        queryset=m.Warehouse.objects.all(),
        many=True,
        required=False,
    )
    # Доступ ко всем складам компании (и всем товарам) — без перечисления складов.
    common_all_warehouses = serializers.BooleanField(required=False)
    can_sell_wholesale = serializers.BooleanField(required=False)
    can_sell_without_approval = serializers.BooleanField(required=False)

    class Meta:
        model = m.CompanyWarehouseAgent
        fields = (
            "assigned_warehouse",
            "common_access_enabled",
            "common_warehouse",
            "common_warehouses",
            "common_all_warehouses",
            "can_sell_wholesale",
            "can_sell_without_approval",
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        inst = getattr(self, "instance", None)
        enabled = attrs.get("common_access_enabled", getattr(inst, "common_access_enabled", False))
        all_warehouses = attrs.get("common_all_warehouses", getattr(inst, "common_all_warehouses", False))
        assigned_warehouse = attrs.get("assigned_warehouse", getattr(inst, "assigned_warehouse", None))

        # Собираем итоговый набор складов: приоритет у common_warehouses (список),
        # иначе — legacy common_warehouse (один), иначе — текущее состояние записи.
        if "common_warehouses" in attrs:
            warehouses = list(attrs.get("common_warehouses") or [])
        elif "common_warehouse" in attrs:
            single = attrs.get("common_warehouse")
            warehouses = [single] if single is not None else []
        elif inst is not None and not inst.common_all_warehouses:
            warehouses = [
                w for w in m.Warehouse.objects.filter(id__in=inst.common_warehouse_ids())
            ]
        else:
            warehouses = []

        if not enabled:
            # Доступ выключен — сбрасываем и набор, и флаг «все склады».
            warehouses = []
            all_warehouses = False
        elif all_warehouses:
            # Доступ ко всем складам — явный список не нужен и очищается.
            warehouses = []
        elif not warehouses:
            raise serializers.ValidationError(
                {"common_warehouses": "Укажите хотя бы один склад или включите common_all_warehouses."}
            )

        company = getattr(inst, "company", None)
        company_id = getattr(company, "id", None)

        if assigned_warehouse is not None and company_id is not None:
            if getattr(assigned_warehouse, "company_id", None) != company_id:
                raise serializers.ValidationError({"assigned_warehouse": "Склад принадлежит другой компании."})

        if company_id is not None:
            for wh in warehouses:
                if getattr(wh, "company_id", None) != company_id:
                    raise serializers.ValidationError(
                        {"common_warehouses": f"Склад {wh.id} принадлежит другой компании."}
                    )

        # Итоговый набор кладём в attrs; сохранение M2M — в update()/create().
        # common_warehouse (FK) держим синхронным: первый из набора либо None.
        attrs["common_all_warehouses"] = all_warehouses
        attrs["_resolved_common_warehouses"] = warehouses
        attrs["common_warehouse"] = warehouses[0] if warehouses else None
        attrs.pop("common_warehouses", None)
        return attrs

    def _apply_common_warehouses(self, instance, warehouses):
        instance.common_warehouses.set(warehouses)

    def create(self, validated_data):
        warehouses = validated_data.pop("_resolved_common_warehouses", [])
        instance = super().create(validated_data)
        self._apply_common_warehouses(instance, warehouses)
        return instance

    def update(self, instance, validated_data):
        warehouses = validated_data.pop("_resolved_common_warehouses", None)
        instance = super().update(instance, validated_data)
        if warehouses is not None:
            self._apply_common_warehouses(instance, warehouses)
        return instance


class AgentRequestCartActionSerializer(serializers.Serializer):
    # placeholder for submit/approve/reject
    pass


class AgentRequestCartCreateSaleSerializer(serializers.Serializer):
    """
    Создание документа SALE по позициям заявки агента (AgentRequestCart).
    Делает документ, привязанный к agent, чтобы аналитика считала продажи агента.
    """

    counterparty = serializers.PrimaryKeyRelatedField(queryset=m.Counterparty.objects.all())
    post = serializers.BooleanField(required=False, default=False)
    is_sale_request = serializers.BooleanField(required=False, default=False)
    is_wholesale = serializers.BooleanField(required=False, default=False)
    payment_kind = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    prepayment_amount = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, default=Decimal("0.00"))
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=Decimal("0.00"))
    discount_amount = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, default=Decimal("0.00"))
    comment = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate_payment_kind(self, value):
        if value in (None, ""):
            return None
        normalized = normalize_payment_kind(value)
        allowed = {choice for choice, _label in m.Document.PaymentKind.choices}
        if normalized not in allowed:
            raise serializers.ValidationError("Укажите cash, credit (или debt) либо external.")
        return normalized


class AgentStockBalanceSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_article = serializers.CharField(source="product.article", read_only=True)
    product_unit = serializers.CharField(source="product.unit", read_only=True)
    product_price = serializers.DecimalField(source="product.price", max_digits=18, decimal_places=3, read_only=True)
    product_wholesale_price = serializers.DecimalField(
        source="product.wholesale_price", max_digits=18, decimal_places=3, read_only=True
    )
    product_discount_percent = serializers.DecimalField(
        source="product.discount_percent", max_digits=12, decimal_places=2, read_only=True
    )
    product_price_after_discount = serializers.SerializerMethodField()
    can_sell_wholesale = serializers.SerializerMethodField()
    product_group = serializers.SerializerMethodField()
    product_group_name = serializers.SerializerMethodField()
    product_category = serializers.SerializerMethodField()
    product_category_name = serializers.SerializerMethodField()
    agent_display = serializers.SerializerMethodField()
    last_movement_at = serializers.DateTimeField(read_only=True, allow_null=True)
    qty_available = serializers.SerializerMethodField()

    class Meta:
        model = m.AgentStockBalance
        fields = (
            "id",
            "agent",
            "agent_display",
            "warehouse",
            "product",
            "product_name",
            "product_article",
            "product_unit",
            "product_price",
            "product_wholesale_price",
            "product_discount_percent",
            "product_price_after_discount",
            "can_sell_wholesale",
            "product_group",
            "product_group_name",
            "product_category",
            "product_category_name",
            "qty",
            "qty_available",
            "last_movement_at",
        )
        read_only_fields = fields

    def get_can_sell_wholesale(self, obj):
        return bool(self.context.get("can_sell_wholesale", False))

    def get_qty_available(self, obj):
        from django.db.models import Sum

        on_hand = q_qty(Decimal(getattr(obj, "qty", None) or 0))
        pending = m.AgentReturnItem.objects.filter(
            cart__agent_id=obj.agent_id,
            cart__warehouse_id=obj.warehouse_id,
            cart__status=m.AgentReturnCart.Status.SUBMITTED,
            product_id=obj.product_id,
        ).aggregate(total=Sum("quantity_returned"))["total"]
        reserved = q_qty(Decimal(pending or 0))
        return str(max(on_hand - reserved, Decimal("0.000")))

    def get_product_price_after_discount(self, obj):
        p = getattr(obj, "product", None)
        if not p:
            return Decimal("0.000")
        return _warehouse_product_unit_price_after_discount(p)

    def _get_product_group(self, product):
        if not product:
            return None, None
        group = getattr(product, "product_group", None)
        if group is None:
            return None, None
        return str(group.id), getattr(group, "name", "") or ""

    def _get_product_category(self, product):
        if not product:
            return None, None
        cat = getattr(product, "category", None)
        if cat is None:
            return None, None
        return str(cat.id), getattr(cat, "name", "") or ""

    def get_product_group(self, obj):
        return self._get_product_group(getattr(obj, "product", None))[0]

    def get_product_group_name(self, obj):
        return self._get_product_group(getattr(obj, "product", None))[1]

    def get_product_category(self, obj):
        return self._get_product_category(getattr(obj, "product", None))[0]

    def get_product_category_name(self, obj):
        return self._get_product_category(getattr(obj, "product", None))[1]

    def get_agent_display(self, obj):
        agent = getattr(obj, "agent", None)
        if not agent:
            return None
        full_name = ""
        if hasattr(agent, "get_full_name"):
            try:
                full_name = agent.get_full_name() or ""
            except Exception:
                full_name = ""
        if not full_name:
            first = getattr(agent, "first_name", "") or ""
            last = getattr(agent, "last_name", "") or ""
            full_name = f"{first} {last}".strip()
        return full_name or getattr(agent, "username", None) or getattr(agent, "email", None) or str(getattr(agent, "id", ""))


class CommonWarehouseBalanceSerializer(serializers.Serializer):
    """
    Ответ совместим с форматом /agents/me/products, но qty берётся из общего остатка склада
    (WarehouseProduct.quantity), а id генерируется детерминированно.
    """

    id = serializers.UUIDField()
    agent = serializers.UUIDField()
    warehouse = serializers.UUIDField()
    product = serializers.UUIDField()
    product_name = serializers.CharField()
    product_article = serializers.CharField(allow_null=True, required=False)
    product_unit = serializers.CharField()
    product_price = serializers.DecimalField(max_digits=18, decimal_places=3)
    product_wholesale_price = serializers.DecimalField(max_digits=18, decimal_places=3)
    product_discount_percent = serializers.DecimalField(max_digits=12, decimal_places=2)
    product_price_after_discount = serializers.DecimalField(max_digits=18, decimal_places=3)
    can_sell_wholesale = serializers.SerializerMethodField()
    product_group = serializers.UUIDField(allow_null=True, required=False)
    product_group_name = serializers.CharField(allow_null=True, required=False)
    product_category = serializers.UUIDField(allow_null=True, required=False)
    product_category_name = serializers.CharField(allow_null=True, required=False)
    qty = serializers.DecimalField(max_digits=18, decimal_places=3)
    created_date = serializers.DateTimeField(allow_null=True, required=False)
    updated_date = serializers.DateTimeField(allow_null=True, required=False)

    def get_can_sell_wholesale(self, obj):
        return bool(self.context.get("can_sell_wholesale", False))

    @staticmethod
    def make_row(*, agent_id, warehouse_id, product: m.WarehouseProduct):
        sid = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nurcrm:common_stock:{warehouse_id}:{product.id}",
        )
        product_group = getattr(product, "product_group", None)
        category = getattr(product, "category", None)
        return {
            "id": sid,
            "agent": agent_id,
            "warehouse": warehouse_id,
            "product": product.id,
            "product_name": product.name,
            "product_article": getattr(product, "article", None),
            "product_unit": getattr(product, "unit", "") or "",
            "product_price": getattr(product, "price", None) or Decimal("0.000"),
            "product_wholesale_price": getattr(product, "wholesale_price", None) or Decimal("0.000"),
            "product_discount_percent": getattr(product, "discount_percent", None) or Decimal("0.00"),
            "product_price_after_discount": _warehouse_product_unit_price_after_discount(product),
            "product_group": product_group.id if product_group else None,
            "product_group_name": (getattr(product_group, "name", "") or "") if product_group else None,
            "product_category": category.id if category else None,
            "product_category_name": (getattr(category, "name", "") or "") if category else None,
            "qty": getattr(product, "quantity", None) or Decimal("0.000"),
            "created_date": getattr(product, "created_date", None),
            "updated_date": getattr(product, "updated_date", None),
        }
