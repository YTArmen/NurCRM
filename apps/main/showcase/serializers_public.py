# apps/products/serializers_public.py
from decimal import Decimal
from rest_framework import serializers

from apps.users.models import Company
from ..models import Product, ProductCharacteristics, ProductPackage
from ..variant_utils import active_variants, variant_prices

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
            pct = None
            if product_price > 0 and final < product_price:
                pct = int(((product_price - final) / product_price * Decimal("100")).quantize(Decimal("1")))
            res.append({
                "id": str(v.id),
                "size": v.size or "",
                "color": v.color or "",
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
        fields = [f for f in PublicProductSerializer.Meta.fields if f != "variants"]
