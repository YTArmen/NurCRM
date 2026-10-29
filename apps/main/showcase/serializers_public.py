# apps/products/serializers_public.py
from decimal import Decimal
from rest_framework import serializers

from apps.users.models import Company
from ..models import Product, ProductCharacteristics, ProductPackage
from ..variant_utils import active_variants, size_sort_key, variant_prices

# Открытая витрина не раскрывает точный остаток: только «есть в наличии» и «осталось мало».
LOW_STOCK_THRESHOLD = Decimal("5")


def _stock_flags(qty, *, tracked=True):
    qty = Decimal(str(qty or 0))
    if not tracked:
        return True, False
    return qty > 0, Decimal("0") < qty <= LOW_STOCK_THRESHOLD


class PublicCompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ["id", "name", "slug", "phones_howcase"]


class PublicProductPackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductPackage
        fields = ["id", "name", "quantity_in_package", "unit", "piece_unit_price"]


class PublicProductCharacteristicsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCharacteristics
        fields = [
            "height_cm",
            "width_cm",
            "depth_cm",
            "factual_weight_kg",
            "description",
        ]


class PublicProductSerializer(serializers.ModelSerializer):
    title = serializers.CharField(source="name", read_only=True)
    category_title = serializers.CharField(source="category.name", read_only=True)
    brand_title = serializers.CharField(source="brand.name", read_only=True)

    image_url = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()
    final_price = serializers.SerializerMethodField()
    is_new = serializers.SerializerMethodField()

    characteristics = PublicProductCharacteristicsSerializer(read_only=True)
    packages = PublicProductPackageSerializer(many=True, read_only=True)

    has_variants = serializers.SerializerMethodField()
    price_range = serializers.SerializerMethodField()
    variants = serializers.SerializerMethodField()
    # ТЗ Н-26: цвета (с фото по цвету) и размеры по порядку
    colors = serializers.SerializerMethodField()
    sizes = serializers.SerializerMethodField()
    size_chart = serializers.SerializerMethodField()  # ТЗ Н-33
    sale_units = serializers.SerializerMethodField()  # ТЗ 26.10 Б-53 п.7: только продажные поля

    # Витрина (ТЗ-BE-2026-05, п. 6.4/6.13): бейдж владельца, закрепление, акция кассы.
    badge = serializers.SerializerMethodField()
    pinned = serializers.SerializerMethodField()
    on_sale = serializers.SerializerMethodField()
    promotion = serializers.SerializerMethodField()

    in_stock = serializers.SerializerMethodField()
    low_stock = serializers.SerializerMethodField()
    # то же, что stock («Акционный товар»); stock оставлен на переходный период
    is_promo = serializers.BooleanField(source="stock", read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "kind",
            "name",
            "title",
            "description",
            "unit",
            "unit_code", "quantity_step", "min_sale_quantity",  # ТЗ 26.10 Б-52
            "sale_units",  # ТЗ 26.10 Б-53 п.7
            "is_weight",
            "in_stock",
            "low_stock",
            "stock",
            "is_promo",
            "is_new",
            "country",
            "barcode",
            "article",

            "price",
            "discount_percent",
            "final_price",
            "has_variants",
            "price_range",
            "variants",
            "colors",
            "sizes",
            "size_chart",
            "badge",
            "pinned",
            "on_sale",
            "promotion",

            "category",
            "category_title",
            "brand",
            "brand_title",

            "expiration_date",
            "created_at",

            "image_url",
            "images",
            "characteristics",
            "packages",
        ]

    def get_sale_units(self, obj):
        from apps.main.tz26.hw_units.units import serialize_sale_units

        return serialize_sale_units(obj, public=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # ТЗ 29.10 Ф-3: ключи category/category_title всегда есть (нет категории -> null и "").
        if "category" in self.fields:
            data["category"] = data.get("category") or None
            data["category_title"] = data.get("category_title") or ""
        from apps.main.tz26.hw_catalog.services import extend_public  # ТЗ 26.10 Б-57/59: attributes, related
        return extend_public(self, instance, data, with_related=not isinstance(self, PublicProductListSerializer))

    def get_size_chart(self, obj):
        from apps.main.showcase.size_charts import public_size_chart

        return public_size_chart(obj)

    def _get_sorted_images(self, obj: Product):
        # Используем предзагруженные связанные объекты obj.images.all() без .filter(),
        # чтобы избежать N+1 запросов к базе данных.
        imgs = [img for img in obj.images.all() if getattr(img, "image", None)]
        return sorted(
            imgs,
            key=lambda x: (
                not bool(getattr(x, "is_primary", False)),
                getattr(x, "created_at", None) or "",
            ),
        )

    def get_images(self, obj: Product):
        request = self.context.get("request")
        sorted_imgs = self._get_sorted_images(obj)
        result = []
        for img in sorted_imgs:
            url = request.build_absolute_uri(img.image.url) if request else img.image.url
            result.append(
                {
                    "id": str(img.id),
                    "image": url,
                    "image_url": url,
                    "alt": getattr(img, "alt", "") or "",
                    "is_primary": bool(getattr(img, "is_primary", False)),
                    "color": getattr(img, "color", "") or "",
                    "created_at": img.created_at.isoformat() if getattr(img, "created_at", None) else None,
                }
            )
        return result

    def get_image_url(self, obj: Product):
        sorted_imgs = self._get_sorted_images(obj)
        if not sorted_imgs:
            return None
        primary_img = sorted_imgs[0]
        request = self.context.get("request")
        return request.build_absolute_uri(primary_img.image.url) if request else primary_img.image.url

    def get_final_price(self, obj: Product):
        price = obj.price or Decimal("0")
        disc = obj.discount_percent or Decimal("0")
        if disc <= 0:
            return price.quantize(Decimal("0.01")) if hasattr(price, "quantize") else price
        return (price * (Decimal("1") - disc / Decimal("100"))).quantize(Decimal("0.01"))

    def _product_stock_flags(self, obj: Product):
        tracked = getattr(obj, "kind", Product.Kind.PRODUCT) == Product.Kind.PRODUCT
        variants = self._variants_data(obj) if tracked else []
        if variants:
            return any(v["in_stock"] for v in variants), all(
                v["low_stock"] or not v["in_stock"] for v in variants
            ) and any(v["low_stock"] for v in variants)
        return _stock_flags(getattr(obj, "quantity", 0), tracked=tracked)

    def get_in_stock(self, obj: Product) -> bool:
        return self._product_stock_flags(obj)[0]

    def get_low_stock(self, obj: Product) -> bool:
        return self._product_stock_flags(obj)[1]

    def get_is_new(self, obj: Product) -> bool:
        if not getattr(obj, "created_at", None):
            return False
        from django.utils import timezone
        from datetime import timedelta
        days = self.context.get("new_badge_days", 14)
        if days is None:
            days = 14
        return (timezone.now() - obj.created_at) <= timedelta(days=int(days))

    def get_badge(self, obj: Product):
        return getattr(obj, "sc_badge", None)

    def get_pinned(self, obj: Product) -> bool:
        return bool(getattr(obj, "sc_pinned", False))

    def _promo_tiers(self, obj: Product):
        if not getattr(obj, "stock", False):
            return []
        cache = getattr(obj, "_prefetched_objects_cache", {}) or {}
        if "promotion_tiers" in cache:
            return list(cache["promotion_tiers"])
        return list(obj.promotion_tiers.all())

    def get_on_sale(self, obj: Product) -> bool:
        return bool((obj.discount_percent or 0) > 0 or self._promo_tiers(obj))

    def get_promotion(self, obj: Product):
        """Серверная акция кассы (ступени): цена для 1 шт. и ступени — как считает касса."""
        if not self._promo_tiers(obj):
            return None
        from apps.main.showcase.services import product_promo_info

        company = self.context.get("showcase_company") or obj.company
        return product_promo_info(obj, company)

    # --- Варианты (размер/цвет). Ожидается prefetch "variants" (только активные) во вьюхе. ---
    def _variants_data(self, obj: Product):
        cached = getattr(obj, "_public_variants_cache", None)
        if cached is not None:
            return cached
        disc = Decimal(str(obj.discount_percent or 0))
        product_price = Decimal(str(obj.price or 0))
        res = []
        for v in active_variants(obj):
            qty = Decimal(str(v.quantity or 0))
            price, old_price = variant_prices(v, obj)
            discount_price = None
            if disc > 0:
                discount_price = (price * (Decimal("1") - disc / Decimal("100"))).quantize(Decimal("0.01"))
            final = discount_price if discount_price is not None else price
            # ТЗ Н-26, правило old_price: зачёркнутая цена = цена товара, если итоговая цена варианта
            # (с акцией варианта и скидкой товара) ниже неё; иначе old_price = null.
            pct = None
            if product_price > 0 and final < product_price:
                pct = int(((product_price - final) / product_price * Decimal("100")).quantize(Decimal("1")))
            qty = max(qty, Decimal("0"))
            res.append({
                "id": str(v.id),
                "size": v.size or "",
                "color": v.color or "",
                "color_hex": getattr(v, "color_hex", "") or "",
                "sort_order": getattr(v, "sort_order", 0) or 0,
                # ТЗ Н-26: точное число — только если осталось ≤ 5, иначе null (остаток уже за вычетом резервов)
                "available": int(qty) if qty <= LOW_STOCK_THRESHOLD else None,
                "in_stock": qty > 0,
                "low_stock": _stock_flags(qty)[1],
                "price": str(price.quantize(Decimal("0.01"))),
                "old_price": str(product_price.quantize(Decimal("0.01"))) if pct else None,
                "discount_price": str(discount_price) if discount_price is not None else None,
                "final_price": str(final.quantize(Decimal("0.01"))),
                "discount_percent": pct,
            })
        obj._public_variants_cache = res
        return res

    def _images_by_color(self, obj: Product) -> dict:
        request = self.context.get("request")
        out: dict = {}
        for img in self._get_sorted_images(obj):
            c = (getattr(img, "color", "") or "").strip().lower()
            if not c:
                continue
            url = request.build_absolute_uri(img.image.url) if request else img.image.url
            out.setdefault(c, []).append(url)
        return out

    def _colors_data(self, obj: Product, *, only_in_stock=False) -> list:
        seen: dict = {}
        for v in self._variants_data(obj):
            name = v["color"]
            if not name:
                continue
            if only_in_stock and not v["in_stock"]:
                continue
            key = name.lower()
            row = seen.get(key)
            if row is None:
                seen[key] = {"name": name, "hex": v["color_hex"] or None}
            elif not row["hex"] and v["color_hex"]:
                row["hex"] = v["color_hex"]
        return list(seen.values())

    def get_colors(self, obj: Product):
        imgs = self._images_by_color(obj)
        rows = self._colors_data(obj)
        for r in rows:
            r["images"] = imgs.get(r["name"].lower(), [])
        return rows

    def get_sizes(self, obj: Product):
        # ТЗ витрины одежды 26.10: размеры по sort_order (варианты уже отсортированы variant_sort_key:
        # ручной sort_order, затем порядок размеров); без sort_order — size_sort_key.
        seen = dict.fromkeys(v["size"] for v in self._variants_data(obj) if v["size"])
        return list(seen)

    def get_has_variants(self, obj: Product) -> bool:
        return bool(self._variants_data(obj))

    def get_variants(self, obj: Product):
        return self._variants_data(obj)

    def get_price_range(self, obj: Product):
        # min/max — имена из ТЗ приложения покупателей (14.10, п. 2.5); min_price/max_price оставлены для сайта.
        variants = self._variants_data(obj)
        if not variants:
            fp = self.get_final_price(obj)
            return {"min_price": str(fp), "max_price": str(fp), "min": str(fp), "max": str(fp)}
        prices = [Decimal(v["final_price"]) for v in variants]
        lo, hi = str(min(prices)), str(max(prices))
        return {"min_price": lo, "max_price": hi, "min": lo, "max": hi}


class PublicProductListSerializer(PublicProductSerializer):
    """Список товаров витрины: признак has_variants и диапазон цен, без массива вариантов."""

    class Meta(PublicProductSerializer.Meta):
        fields = [f for f in PublicProductSerializer.Meta.fields if f not in ("variants", "sizes")] + ["sizes_in_stock"]

    sizes_in_stock = serializers.SerializerMethodField()

    def get_colors(self, obj: Product):
        # ТЗ Н-26: в списке — до 8 цветов (в наличии), без фото
        return self._colors_data(obj, only_in_stock=True)[:8]

    def get_sizes_in_stock(self, obj: Product):
        sizes = {v["size"] for v in self._variants_data(obj) if v["size"] and v["in_stock"]}
        return sorted(sizes, key=size_sort_key)

    def get_price_range(self, obj: Product):
        # ТЗ Н-26: диапазон по вариантам в наличии (если ничего нет в наличии — по всем)
        variants = self._variants_data(obj)
        in_stock = [v for v in variants if v["in_stock"]] or variants
        if not in_stock:
            return super().get_price_range(obj)
        prices = [Decimal(v["final_price"]) for v in in_stock]
        lo, hi = str(min(prices)), str(max(prices))
        return {"min_price": lo, "max_price": hi, "min": lo, "max": hi}
