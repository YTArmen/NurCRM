from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.main.models import (
    Product,
    ShowcaseBanner,
    ShowcaseDesign,
    ShowcaseDesignVersion,
    ShowcaseMedia,
    ShowcaseOrder,
    ShowcaseOrderItem,
    ShowcasePromoBlock,
    ShowcaseStats,
    ShowcaseStockRequest,
    _cart_item_promotion,
    _money,
)


HEX_COLOR_REGEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_hex_color(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not HEX_COLOR_REGEX.match(value):
        raise serializers.ValidationError(
            {field_name: "Цвет должен быть в формате #RRGGBB."}
        )
    return value.upper()


def srgb_to_lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_str: str) -> float:
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g) + 0.0722 * srgb_to_lin(b)


def contrast_ratio(c1: str, c2: str) -> float:
    try:
        l1, l2 = relative_luminance(c1), relative_luminance(c2)
        lighter = max(l1, l2)
        darker = min(l1, l2)
        return round((lighter + 0.05) / (darker + 0.05), 1)
    except Exception:
        return 21.0


def check_theme_contrast(theme_dict: dict) -> List[dict]:
    """
    Проверка контраста текста и фона по WCAG AA (минимум 4.5:1).
    Возвращает список предупреждений (warnings), не блокируя сохранение.
    """
    warnings = []
    if not isinstance(theme_dict, dict):
        return warnings

    text = theme_dict.get("text")
    bg = theme_dict.get("background")
    card_bg = theme_dict.get("card_bg")
    header_text = theme_dict.get("header_text")
    header_bg = theme_dict.get("header_bg")

    if text and bg and HEX_COLOR_REGEX.match(text) and HEX_COLOR_REGEX.match(bg):
        r = contrast_ratio(text, bg)
        if r < 4.5:
            warnings.append({"field": "text", "code": "low_contrast", "ratio": r})
    elif text and card_bg and HEX_COLOR_REGEX.match(text) and HEX_COLOR_REGEX.match(card_bg):
        r = contrast_ratio(text, card_bg)
        if r < 4.5:
            warnings.append({"field": "text", "code": "low_contrast", "ratio": r})

    if (
        header_text
        and header_bg
        and HEX_COLOR_REGEX.match(header_text)
        and HEX_COLOR_REGEX.match(header_bg)
    ):
        r = contrast_ratio(header_text, header_bg)
        if r < 4.5:
            warnings.append(
                {"field": "header_text", "code": "low_contrast", "ratio": r}
            )

    return warnings


def validate_theme_data(theme: dict) -> dict:
    if not isinstance(theme, dict):
        raise serializers.ValidationError({"theme": "Ожидается объект."})
    clean = dict(theme)
    color_fields = [
        "background",
        "text",
        "accent",
        "header_bg",
        "header_text",
        "footer_bg",
        "card_bg",
        "price",
    ]
    for f in color_fields:
        if f in clean and clean[f] is not None:
            clean[f] = validate_hex_color(clean[f], f)

    if "radius" in clean and clean["radius"] is not None:
        try:
            r = int(clean["radius"])
            if r < 0:
                raise ValueError()
            clean["radius"] = r
        except (ValueError, TypeError):
            raise serializers.ValidationError(
                {"radius": "Радиус должен быть неотрицательным числом."}
            )

    if "mode" in clean and clean["mode"] not in (None, "light", "dark"):
        raise serializers.ValidationError(
            {"mode": "Режим темы должен быть 'light' или 'dark'."}
        )

    return clean


def validate_layout_data(layout: dict) -> dict:
    if not isinstance(layout, dict):
        raise serializers.ValidationError({"layout": "Ожидается объект."})
    clean = dict(layout)

    if "columns" in clean:
        cols = clean["columns"]
        if not isinstance(cols, dict):
            raise serializers.ValidationError({"columns": "Ожидается объект."})
        if "desktop" in cols:
            try:
                d = int(cols["desktop"])
                if not (2 <= d <= 6):
                    raise ValueError()
                cols["desktop"] = d
            except (ValueError, TypeError):
                raise serializers.ValidationError(
                    {"columns": {"desktop": "Число колонок для компьютера должно быть от 2 до 6."}}
                )
        if "mobile" in cols:
            try:
                m = int(cols["mobile"])
                if not (1 <= m <= 3):
                    raise ValueError()
                cols["mobile"] = m
            except (ValueError, TypeError):
                raise serializers.ValidationError(
                    {"columns": {"mobile": "Число колонок для телефона должно быть от 1 до 3."}}
                )

    if "default_sort" in clean and clean["default_sort"] not in (
        "manual",
        "popular",
        "new",
        "price_asc",
        "price_desc",
    ):
        raise serializers.ValidationError(
            {
                "default_sort": "Допустимые значения: manual, popular, new, price_asc, price_desc."
            }
        )

    return clean


def validate_cards_data(cards: dict) -> dict:
    if not isinstance(cards, dict):
        raise serializers.ValidationError({"cards": "Ожидается объект."})
    clean = dict(cards)

    if "template" in clean and clean["template"] not in (
        "compact",
        "standard",
        "large",
        "list",
    ):
        raise serializers.ValidationError(
            {"template": "Допустимые шаблоны: compact, standard, large, list."}
        )

    if "photo_ratio" in clean and clean["photo_ratio"] not in ("1:1", "4:3", "3:4"):
        raise serializers.ValidationError(
            {"photo_ratio": "Допустимые пропорции: 1:1, 4:3, 3:4."}
        )

    return clean


# ======================================================================
# Serializers
# ======================================================================

class ShowcaseDesignSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShowcaseDesign
        fields = ["draft", "published", "version", "published_at"]


class ShowcaseDesignVersionSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()

    class Meta:
        model = ShowcaseDesignVersion
        fields = ["version", "published_at", "author"]

    def get_author(self, obj) -> Optional[str]:
        if not obj.author:
            return None
        return (
            getattr(obj.author, "first_name", "")
            or getattr(obj.author, "email", "")
            or getattr(obj.author, "phone", "")
            or str(obj.author)
        )


class ShowcaseMediaSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShowcaseMedia
        fields = [
            "id",
            "file",
            "urls",
            "width",
            "height",
            "content_type",
            "created_at",
        ]
        read_only_fields = ["id", "urls", "width", "height", "content_type", "created_at"]


class ShowcaseBannerSerializer(serializers.ModelSerializer):
    image_urls = serializers.SerializerMethodField()
    image_mobile_urls = serializers.SerializerMethodField()

    class Meta:
        model = ShowcaseBanner
        fields = [
            "id",
            "title",
            "subtitle",
            "image",
            "image_mobile",
            "image_urls",
            "image_mobile_urls",
            "link",
            "place",
            "starts_at",
            "ends_at",
            "active",
            "position",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_image_urls(self, obj) -> dict:
        if obj.image:
            return obj.image.urls or {}
        return {}

    def get_image_mobile_urls(self, obj) -> dict:
        if obj.image_mobile:
            return obj.image_mobile.urls or {}
        if obj.image:
            return obj.image.urls or {}
        return {}


class ShowcasePromoBlockSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShowcasePromoBlock
        fields = [
            "id",
            "title",
            "source",
            "style",
            "show_timer",
            "max_items",
            "position",
            "active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ShowcaseOrderItemSerializer(serializers.ModelSerializer):
    variant_size = serializers.CharField(source="variant.size", read_only=True, default=None)
    variant_color = serializers.CharField(source="variant.color", read_only=True, default=None)

    class Meta:
        model = ShowcaseOrderItem
        fields = [
            "id",
            "product",
            "variant",
            "variant_size",
            "variant_color",
            "product_name",
            "qty",
            "price",
            "discount",
            "total",
        ]
        read_only_fields = ["id", "total"]


class ShowcaseOrderSerializer(serializers.ModelSerializer):
    items = ShowcaseOrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = ShowcaseOrder
        fields = [
            "id",
            "number",
            "status",
            "customer_name",
            "customer_phone",
            "delivery_type",
            "delivery_address",
            "comment",
            "total",
            "source",
            "telegram_chat_id",
            "sale",
            "stock_reserved",
            "pickup_code",  # ТЗ Н-33
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "number", "total", "sale", "stock_reserved", "telegram_chat_id", "created_at", "updated_at"]
        extra_kwargs = {"pickup_code": {"required": False}}

    def validate_pickup_code(self, value):
        value = (value or "").strip()
        if value and not (value.isdigit() and 4 <= len(value) <= 6):
            raise serializers.ValidationError("Код выдачи — 4–6 цифр.")
        return value


class ShowcaseOrderCreateItemSerializer(serializers.Serializer):
    product = serializers.UUIDField(required=True)
    variant = serializers.UUIDField(required=False, allow_null=True, default=None)
    qty = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001"), max_value=Decimal("100000")
    )
    # ТЗ 26.10 Б-53 п.8: единица продажи (id из sale_units); qty — в ней, резерв — в базовых
    sale_unit = serializers.IntegerField(required=False, allow_null=True, default=None)


class ShowcaseOrderCustomerSerializer(serializers.Serializer):
    # Обязательность имени/телефона задаёт cart.fields документа вида (проверяется во вьюхе).
    name = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")


class ShowcaseOrderDeliverySerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["pickup", "delivery"], default="pickup")
    address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class ShowcaseOrderCreateSerializer(serializers.Serializer):
    customer = ShowcaseOrderCustomerSerializer(required=True)
    items = serializers.ListField(
        child=ShowcaseOrderCreateItemSerializer(),
        allow_empty=False,
        required=True,
        max_length=100,
    )
    delivery = ShowcaseOrderDeliverySerializer(required=False, default=dict)
    comment = serializers.CharField(required=False, allow_blank=True, default="", max_length=2000)
    # ТЗ Б-22: источник заказа; для telegram_bot — ещё telegram_chat_id
    source = serializers.ChoiceField(
        choices=["showcase", "telegram_bot", "voice_ai", "whatsapp"], required=False, default="showcase", allow_blank=True)
    telegram_chat_id = serializers.RegexField(r"^-?\d{1,20}$", required=False, allow_blank=True, default="")


class ShowcaseStockRequestSerializer(serializers.ModelSerializer):
    """ТЗ Н-27: заявка «сообщить о поступлении» для владельца."""

    product_name = serializers.CharField(source="product.name", read_only=True)
    variant_label = serializers.SerializerMethodField()

    class Meta:
        model = ShowcaseStockRequest
        fields = ["id", "product", "product_name", "variant", "variant_label", "phone", "telegram_chat_id",
                  "status", "created_at", "notified_at"]
        read_only_fields = fields

    def get_variant_label(self, obj):
        from apps.main.variant_utils import variant_label

        return variant_label(obj.variant)
