from decimal import Decimal
from rest_framework import serializers

from apps.construction.models import Cashbox, CashShift
from .models import (
    ClientDeal,
    Product,
    Cart,
    CartItem,
    CartItemDeletionLog,
    Sale,
    SaleItem,
    MobileScannerToken,
    ProductImage,
)
from .pos_utils import (
    money, qty3, has_field, get_attr, Q2, Q3
)


class MoneyField(serializers.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 12)
        kwargs.setdefault("decimal_places", 2)
        super().__init__(*args, **kwargs)

    def to_internal_value(self, value):
        val = super().to_internal_value(value)
        return money(val)


class QtyField(serializers.DecimalField):
    """
    Кол-во для POS:
    - для штучного товара будет 1,2,3...
    - для весового будет 0.312, 1.245 ...
    """
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 12)
        kwargs.setdefault("decimal_places", 3)
        kwargs.setdefault("required", False)
        kwargs.setdefault("default", Decimal("1.000"))
        super().__init__(*args, **kwargs)

    def to_internal_value(self, value):
        val = super().to_internal_value(value)
        val = qty3(val)
        if val <= 0:
            raise serializers.ValidationError("Количество должно быть > 0.")
        return val


class StartCartOptionsSerializer(serializers.Serializer):
    """
    Настройки корзины перед продажей:
    - order_discount_total — фиксированная скидка на чек (сумма)
    - order_discount_percent — скидка на чек в процентах (0–100)
    - is_new — создать новую open-корзину (кнопка «Новая»)
    - sale_id — активировать указанную open-корзину (переключение вкладки)

    Используется только ОДИН вариант: либо сумма, либо процент.
    """

    order_discount_total = MoneyField(required=False)
    order_discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False
    )
    is_wholesale = serializers.BooleanField(required=False)
    is_new = serializers.BooleanField(required=False, default=False)
    sale_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        total = attrs.get("order_discount_total")
        percent = attrs.get("order_discount_percent")

        if total is not None and total < 0:
            raise serializers.ValidationError({"order_discount_total": "Должна быть ≥ 0."})
        if percent is not None and percent < 0:
            raise serializers.ValidationError({"order_discount_percent": "Должна быть ≥ 0."})
        if percent is not None and percent > 100:
            raise serializers.ValidationError({"order_discount_percent": "Не больше 100%."})
        if total is not None and percent is not None:
            raise serializers.ValidationError(
                "Выберите либо фиксированную скидку (order_discount_total), либо скидку в процентах (order_discount_percent)."
            )

        return attrs


class CustomCartItemCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    price = MoneyField()
    quantity = serializers.IntegerField(min_value=1, required=False, default=1)


class ProductImageReadSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = ("id", "image_url", "alt", "is_primary")

    def get_image_url(self, obj):
        if not obj.image:
            return None
        request = self.context.get("request")
        url = obj.image.url
        return request.build_absolute_uri(url) if request else url


# ⚠️ это serializer для CartItem (название у тебя старое, оставляю чтобы не ломать импорты)
class SaleItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    barcode = serializers.CharField(source="product.barcode", read_only=True)
    is_weight = serializers.BooleanField(source="product.is_weight", read_only=True)
    is_adult = serializers.BooleanField(source="product.is_adult", read_only=True, default=False)
    # Product.stock в карточке товара — «Акционный товар» (как в ProductSerializer)
    stock = serializers.SerializerMethodField()
    promotion_rules = serializers.SerializerMethodField()
    line_total = serializers.SerializerMethodField()
    manual_discount_ignored = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    primary_image_url = serializers.SerializerMethodField(read_only=True)
    # Полный массив images[] в POS не нужен: на строке корзины показывается одна
    # миниатюра (primary_image_url). Массив раздувал payload и CPU сериализации.
    # line_discount — поле модели (хранится отдельно от unit_price)

    # ✅ важно: quantity должен быть Decimal(3), а не int
    quantity = QtyField()

    sale_package = serializers.UUIDField(source="sale_package_id", read_only=True, allow_null=True)

    kind = serializers.CharField(source="product.kind", read_only=True, default="product")
    variant_size = serializers.CharField(source="variant.size", read_only=True, default=None)
    variant_color = serializers.CharField(source="variant.color", read_only=True, default=None)
    sale_unit = serializers.SerializerMethodField()  # ТЗ 26.10 Б-53
    base_quantity = serializers.SerializerMethodField()

    def get_sale_unit(self, obj):
        from apps.main.tz26.hw_units.units import get_sale_unit_for_line

        return get_sale_unit_for_line(obj)

    def get_base_quantity(self, obj):
        from apps.main.tz26.hw_units.units import get_base_quantity_for_line

        return get_base_quantity_for_line(obj)

    class Meta:
        model = CartItem
        fields = (
            "sale_unit", "base_quantity",
            "id", "cart", "product",
            "kind",
            "product_name", "barcode",
            "is_weight", "is_adult",
            "stock", "promotion_rules",
            "quantity", "unit_price", "line_discount", "line_total",
            "manual_discount", "discount_source", "promotion_id", "manual_discount_ignored",
            "price_manually_edited",
            "sale_package",
            "variant", "variant_size", "variant_color",
            "performer",
            "display_name",
            "primary_image_url",
            "is_wholesale",
        )
        read_only_fields = (
            "id", "kind", "product_name", "barcode",
            "variant", "variant_size", "variant_color", "performer",
            "stock", "promotion_rules", "line_total",
            "manual_discount", "discount_source", "promotion_id", "manual_discount_ignored",
            "price_manually_edited",
            "display_name", "primary_image_url",
            "sale_package",
            "is_wholesale",
        )

    def get_stock(self, obj):
        p = getattr(obj, "product", None)
        return bool(getattr(p, "stock", False))

    def get_display_name(self, obj):
        return get_attr(get_attr(obj, "product", None), "name", None) or (
            get_attr(obj, "custom_name", "") or ""
        )

    def get_manual_discount_ignored(self, obj):
        """Ручная скидка кассира не применена, потому что сработала акция (BE2-06)."""
        return obj.discount_source == "promotion" and (obj.manual_discount or 0) > 0

    def get_line_total(self, obj):
        base = Decimal(str(obj.unit_price or 0)) * Decimal(str(obj.quantity or 0))
        disc = Decimal(str(getattr(obj, "line_discount", None) or 0))
        return money(base - disc)

    def get_promotion_rules(self, obj):
        p = getattr(obj, "product", None)
        if not p or not getattr(p, "stock", False):
            return []
        cache = getattr(p, "_prefetched_objects_cache", {})
        tiers = cache.get("promotion_tiers")
        if tiers is not None:
            rows = list(tiers)
        else:
            rows = list(p.promotion_tiers.all())
        out = []
        for t in rows:
            out.append(
                {
                    "id": str(t.id),
                    "position": t.position,
                    "min_amount": str(t.min_amount),
                    "discount_percent": str(t.discount_percent),
                    "promo_quantity": t.promo_quantity,
                }
            )
        return out

    def _get_product_images(self, product):
        if not product:
            return []
        prefetched = getattr(product, "_prefetched_objects_cache", {})
        if "images" in prefetched:
            return list(prefetched["images"])
        return list(product.images.all())

    def get_primary_image_url(self, obj):
        prod = getattr(obj, "product", None)
        if not prod:
            return None
        images = self._get_product_images(prod)
        im = next((image for image in images if getattr(image, "is_primary", False)), None)
        if im is None and images:
            im = images[0]
        if not (im and im.image):
            return None
        request = self.context.get("request")
        url = im.image.url
        return request.build_absolute_uri(url) if request else url

    def _validate_company_branch(self, cart: Cart, product: Product):
        cart_company_id = get_attr(cart, "company_id")
        product_company_id = get_attr(product, "company_id")
        if cart and product and cart_company_id is not None and product_company_id is not None:
            if cart_company_id != product_company_id:
                raise serializers.ValidationError({"product": "Товар принадлежит другой компании, чем корзина."})

        if has_field(type(cart), "branch") and has_field(type(product), "branch"):
            cart_branch_id = get_attr(cart, "branch_id")
            product_branch_id = get_attr(product, "branch_id")
            if product_branch_id is not None and product_branch_id != cart_branch_id:
                raise serializers.ValidationError({"product": "Товар из другого филиала и не является глобальным."})

    def validate(self, attrs):
        cart = attrs.get("cart") or get_attr(self.instance, "cart", None)
        product = attrs.get("product") or get_attr(self.instance, "product", None)

        if cart and product:
            self._validate_company_branch(cart, product)

        qty = attrs.get("quantity", get_attr(self.instance, "quantity", Decimal("1.000")))
        qty = qty3(Decimal(str(qty)))
        if qty <= 0:
            raise serializers.ValidationError({"quantity": "Количество должно быть > 0."})

        attrs["quantity"] = qty
        return attrs

    def create(self, validated_data):
        cart = validated_data["cart"]

        if validated_data.get("product") and "unit_price" not in validated_data:
            validated_data["unit_price"] = validated_data["product"].price

        if has_field(CartItem, "company"):
            validated_data.setdefault("company", cart.company)

        if has_field(CartItem, "branch"):
            validated_data.setdefault("branch", get_attr(cart, "branch", None))

        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("company", None)
        validated_data.pop("cart", None)
        validated_data.pop("branch", None)
        return super().update(instance, validated_data)



class SaleCartSerializer(serializers.ModelSerializer):
    items = SaleItemSerializer(many=True, read_only=True)
    shift = serializers.PrimaryKeyRelatedField(read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = (
            "id",
            "status",
            "is_default",
            "is_wholesale",
            "shift",
            "subtotal",
            "discount_total",
            "order_discount_total",
            "order_discount_percent",
            "tax_total",
            "total",
            "items",
        )

    def get_status(self, obj):
        if obj.status == Cart.Status.ACTIVE:
            return "open"
        return obj.status


class ScanRequestSerializer(serializers.Serializer):
    barcode = serializers.CharField(max_length=64)
    # ✅ было IntegerField → стало Decimal 3 знака
    quantity = QtyField(required=False, default=Decimal("1.000"))
    sale_id = serializers.UUIDField(required=False, allow_null=True)

class AddItemSerializer(serializers.Serializer):
    variant_id = serializers.UUIDField(required=False, allow_null=True)
    product_id = serializers.UUIDField()
    # ✅ было IntegerField → стало Decimal 3 знака
    quantity = QtyField(required=False, default=Decimal("1.000"))
    unit_price = MoneyField(required=False)
    discount_total = MoneyField(required=False)
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False)
    # Продажа поштучно из пачки: id упаковки ProductPackage (quantity_in_package = шт в пачке)
    sale_package_id = serializers.UUIDField(required=False, allow_null=True)
    # ТЗ 26.10 Б-53: единица продажи (коробка/мешок); quantity — в этих единицах
    sale_unit_id = serializers.IntegerField(required=False, allow_null=True)
    # Разрешить продажу "в минус" (игнорировать проверку остатка).
    # Будет применено только для owner/admin (см. pos_views).
    allow_minus = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        up = attrs.get("unit_price")
        disc = attrs.get("discount_total")
        percent = attrs.get("discount_percent")
        qty = attrs.get("quantity", Decimal("1.000"))

        if up is not None and up < 0:
            raise serializers.ValidationError({"unit_price": "Должна быть ≥ 0."})
        if disc is not None and disc < 0:
            raise serializers.ValidationError({"discount_total": "Должна быть ≥ 0."})
        if percent is not None and percent < 0:
            raise serializers.ValidationError({"discount_percent": "Должна быть ≥ 0."})
        if percent is not None and percent > 100:
            raise serializers.ValidationError({"discount_percent": "Не больше 100%."})
        if disc is not None and percent is not None:
            raise serializers.ValidationError(
                "Выберите либо фиксированную скидку (discount_total), либо скидку в процентах (discount_percent)."
            )

        qty = qty3(Decimal(str(qty)))
        if qty <= 0:
            raise serializers.ValidationError({"quantity": "Количество должно быть > 0."})

        attrs["quantity"] = qty
        return attrs


class CartItemPatchSerializer(serializers.Serializer):
    """PATCH позиции корзины: количество (0 = удалить), цена или скидка на позицию."""
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False, min_value=Decimal("0"),
    )
    unit_price = MoneyField(required=False)
    discount_total = MoneyField(required=False)
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False)
    performer = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        up = attrs.get("unit_price")
        disc = attrs.get("discount_total")
        percent = attrs.get("discount_percent")
        if up is not None and up < 0:
            raise serializers.ValidationError({"unit_price": "Должна быть ≥ 0."})
        if disc is not None and disc < 0:
            raise serializers.ValidationError({"discount_total": "Должна быть ≥ 0."})
        if percent is not None and percent < 0:
            raise serializers.ValidationError({"discount_percent": "Должна быть ≥ 0."})
        if percent is not None and percent > 100:
            raise serializers.ValidationError({"discount_percent": "Не больше 100%."})
        if disc is not None and percent is not None:
            raise serializers.ValidationError(
                "Выберите либо фиксированную скидку (discount_total), либо скидку в процентах (discount_percent)."
            )
        return attrs


class OptionalUUIDField(serializers.UUIDField):
    def to_internal_value(self, data):
        if data in (None, "", "null", "None"):
            return None
        return super().to_internal_value(data)


# ⚠️ ВАЖНО:
# - Эта функция у тебя уже есть в construction serializers.
# - Если в этом файле её нет — импортни или вставь такую же.
def _is_owner_like(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if getattr(user, "owned_company", None):
        return True
    if getattr(user, "is_admin", False):
        return True
    role = getattr(user, "role", None)
    if role in ("owner", "admin", "OWNER", "ADMIN", "Владелец", "Администратор"):
        return True
    return False


class CheckoutPaymentLineSerializer(serializers.Serializer):
    method = serializers.ChoiceField(
        choices=[c for c in Sale.PaymentMethod.choices if c[0] not in (Sale.PaymentMethod.DEBT, Sale.PaymentMethod.MIXED)],
    )
    amount = MoneyField()

    def validate_method(self, value):
        # «Зачёт» ставит только сервер (обмен, выдача заказ-наряда).
        if value == Sale.PaymentMethod.OFFSET and not self.root.context.get("allow_offset"):
            raise serializers.ValidationError("Способ оплаты недоступен.")
        return value


def _mixed_split_to_payments(attrs):
    """
    payment_method="mixed" + cash_amount/card_amount → строки payments[].
    None — если разбивку не прислали (старое поведение).
    """
    if attrs.get("payment_method") != Sale.PaymentMethod.MIXED:
        return None
    cash_amount = attrs.get("cash_amount")
    card_amount = attrs.get("card_amount")
    if cash_amount is None and card_amount is None:
        return None
    if attrs.get("payments"):
        raise serializers.ValidationError(
            "Передайте либо payments[], либо cash_amount/card_amount, но не оба варианта."
        )
    cash_amount = cash_amount or Decimal("0.00")
    card_amount = card_amount or Decimal("0.00")
    if cash_amount < 0 or card_amount < 0:
        raise serializers.ValidationError({"cash_amount": "Суммы не могут быть отрицательными."})
    lines = []
    if cash_amount > 0:
        lines.append({"method": Sale.PaymentMethod.CASH, "amount": cash_amount})
    if card_amount > 0:
        lines.append({"method": attrs.get("card_method") or Sale.PaymentMethod.TRANSFER, "amount": card_amount})
    if not lines:
        raise serializers.ValidationError({"cash_amount": "Укажите суммы смешанной оплаты."})
    return lines


class CheckoutSerializer(serializers.Serializer):
    print_receipt = serializers.BooleanField(default=False)
    client_id = OptionalUUIDField(required=False, allow_null=True)
    department_id = OptionalUUIDField(required=False, allow_null=True)
    # Разрешить списание "в минус" при закрытии чека (только owner/admin).
    allow_minus = serializers.BooleanField(required=False, default=False)

    # Консультант и его комиссия
    consultant_id = OptionalUUIDField(required=False, allow_null=True)
    consultant_commission_enabled = serializers.BooleanField(required=False, default=False)
    consultant_commission_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)

    # если смены нет — можно передать кассу (или автоподбор)
    cashbox_id = OptionalUUIDField(required=False, allow_null=True)
    branch_id = OptionalUUIDField(required=False, allow_null=True)
    cashbox_role = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    # ✅ НОВОЕ: можно явно указать смену (нужно, когда 2 смены на 1 кассу)
    shift_id = OptionalUUIDField(required=False, allow_null=True)

    payment_method = serializers.ChoiceField(
        choices=Sale.PaymentMethod.choices,
        required=False,
        allow_null=True,
    )
    cash_received = MoneyField(required=False, allow_null=True)
    prepayment = MoneyField(required=False, allow_null=True)
    prepayment_amount = MoneyField(required=False, allow_null=True)
    payments = CheckoutPaymentLineSerializer(many=True, required=False)
    # Смешанная оплата одной строкой: payment_method="mixed" + cash_amount + card_amount
    cash_amount = MoneyField(required=False, allow_null=True)
    card_amount = MoneyField(required=False, allow_null=True)
    card_method = serializers.ChoiceField(
        choices=[
            c for c in Sale.PaymentMethod.choices
            if c[0] not in (
                Sale.PaymentMethod.DEBT, Sale.PaymentMethod.MIXED, Sale.PaymentMethod.CASH, Sale.PaymentMethod.OFFSET
            )
        ],
        required=False,
        default=Sale.PaymentMethod.TRANSFER,
    )

    # v2 отсрочка (extra-поля для кассы, не ведут к 400)
    schedule_version = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    debt_schedule = serializers.JSONField(required=False, allow_null=True)
    prepayment_method = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    def to_internal_value(self, data):
        if isinstance(data, dict):
            # AN-01: Поддержка вложенного объекта payment = { method: ..., received: ..., cash_amount: ..., card_amount: ..., prepayment_method: ... }
            p = data.get("payment")
            if isinstance(p, dict):
                data = data.copy()
                if "method" in p and "payment_method" not in data:
                    data["payment_method"] = p["method"]
                if "received" in p and "cash_received" not in data:
                    data["cash_received"] = p["received"]
                if "cash_amount" in p and "cash_amount" not in data:
                    data["cash_amount"] = p["cash_amount"]
                if "card_amount" in p and "card_amount" not in data:
                    data["card_amount"] = p["card_amount"]
                if "prepayment_method" in p and "prepayment_method" not in data:
                    data["prepayment_method"] = p["prepayment_method"]
        return super().to_internal_value(data)

    def _resolve_cashbox(self, cart: Cart, cashbox_id, branch_id=None, cashbox_role=None):
        from apps.construction.auto_cashflow import resolve_cashbox
        ctx = {
            "cashbox_id": cashbox_id,
            "branch_id": branch_id or cart.branch_id,
            "cashbox_role": cashbox_role,
        }
        cb = resolve_cashbox(company=cart.company, context=ctx, source_kind="pos_sale", require_cashbox=False)
        if cb:
            return cb
        raise serializers.ValidationError({"cashbox_id": "Нет кассы для этого филиала/компании."})

    def _resolve_shift(self, cart: Cart, cashbox: Cashbox, user, shift_id):
        # 1) если shift_id явно передали — валидируем
        if shift_id:
            sh = CashShift.objects.select_related("cashbox", "cashier").filter(id=shift_id).first()
            if not sh:
                raise serializers.ValidationError({"shift_id": "Смена не найдена."})
            if sh.company_id != cart.company_id:
                raise serializers.ValidationError({"shift_id": "Смена другой компании."})
            if (sh.branch_id or None) != (cart.branch_id or None):
                raise serializers.ValidationError({"shift_id": "Смена другого филиала."})
            if sh.cashbox_id != cashbox.id:
                raise serializers.ValidationError({"shift_id": "Смена относится к другой кассе."})
            if sh.status != CashShift.Status.OPEN:
                raise serializers.ValidationError({"shift_id": "Смена закрыта. Нужна открытая смена."})

            return sh

        # 2) shift_id не передали → берём любую открытую смену компании в этой кассе
        if not user or not getattr(user, "is_authenticated", False):
            raise serializers.ValidationError({"shift_id": "Нужен пользователь для определения смены."})

        sh = (
            CashShift.objects
            .filter(
                company_id=cart.company_id,
                branch_id=cart.branch_id,
                cashbox=cashbox,
                status=CashShift.Status.OPEN,
            )
            .order_by("-opened_at")
            .first()
        )
        if sh:
            return sh

        raise serializers.ValidationError(
            {"shift_id": "У вас нет открытой смены в этой кассе. Откройте смену перед продажей."}
        )

    def validate(self, attrs):
        cart = self.context.get("cart")
        if not cart:
            raise serializers.ValidationError("Serializer context должен содержать cart.")
        if not cart.items.exists():
            raise serializers.ValidationError({"code": "empty_sale", "detail": "В чеке нет позиций"})

        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None

        consultant_id = attrs.get("consultant_id")
        comm_enabled = attrs.get("consultant_commission_enabled", False)
        comm_pct = attrs.get("consultant_commission_percent")

        company = getattr(cart, "company", None)
        if company and getattr(company, "consultant_required_on_sale", False) and not consultant_id:
            raise serializers.ValidationError({"consultant_id": "Указание консультанта обязательно при оформлении чека."})

        if consultant_id:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            consultant_user = User.objects.filter(id=consultant_id, company_id=cart.company_id, is_active=True).first()
            if not consultant_user:
                raise serializers.ValidationError({"consultant_id": "Консультант не найден или не активен в данной компании."})
            attrs["consultant_obj"] = consultant_user
        else:
            attrs["consultant_obj"] = None

        if comm_enabled:
            if not consultant_id:
                raise serializers.ValidationError({"consultant_id": "Нельзя включить комиссию без указания консультанта."})
            if comm_pct is None:
                raise serializers.ValidationError({"consultant_commission_percent": "Укажите процент комиссии консультанта."})
            if comm_pct < 0 or comm_pct > 100:
                raise serializers.ValidationError({"consultant_commission_percent": "Процент комиссии должен быть от 0 до 100."})

        if attrs.get("payment_method") == Sale.PaymentMethod.OFFSET:
            raise serializers.ValidationError({"payment_method": "Способ оплаты недоступен."})
        split_payments = _mixed_split_to_payments(attrs)
        if split_payments is not None:
            attrs["payments"] = split_payments
            attrs["payment_method"] = None

        payments = attrs.get("payments") or []
        cart.recalc()
        sale_total = (cart.total or Decimal("0.00")).quantize(Decimal("0.01"))
        if sale_total <= Decimal("0.00") and not attrs.get("allow_minus"):
            raise serializers.ValidationError({"code": "empty_sale", "detail": "Сумма чека должна быть больше нуля."})
        raw = getattr(self, "initial_data", None) or {}
        payment_method_in_request = (
            split_payments is None
            and "payment_method" in raw
            and raw.get("payment_method") not in (None, "", "null")
        )

        if payments:
            if payment_method_in_request:
                raise serializers.ValidationError(
                    "Передайте либо payments[], либо payment_method, но не оба варианта."
                )
            if len(payments) < 1:
                raise serializers.ValidationError({"payments": "Нужна хотя бы одна строка оплаты."})
            paid_total = sum((p["amount"] for p in payments), Decimal("0.00")).quantize(Decimal("0.01"))
            if paid_total != sale_total:
                raise serializers.ValidationError(
                    {"payments": f"Сумма оплат ({paid_total}) должна равняться сумме чека ({sale_total})."}
                )
            has_debt = any(p.get("method") == Sale.PaymentMethod.DEBT for p in payments)
            if has_debt and not attrs.get("client_id"):
                raise serializers.ValidationError({"client_id": "При продаже в долг выбор клиента обязателен."})
            cash_portion = sum(
                (p["amount"] for p in payments if p["method"] == Sale.PaymentMethod.CASH),
                Decimal("0.00"),
            )
            cash_received = attrs.get("cash_received")
            if cash_portion > 0:
                if cash_received is None:
                    attrs["cash_received"] = cash_portion
                elif cash_received < cash_portion:
                    raise serializers.ValidationError(
                        {"cash_received": "Сумма, полученная наличными, меньше наличной части оплаты."}
                    )
            else:
                attrs["cash_received"] = Decimal("0.00")
        else:
            # --- оплата одним способом ---
            payment_method = attrs.get("payment_method") or Sale.PaymentMethod.CASH
            cash_received = attrs.get("cash_received")

            if payment_method == Sale.PaymentMethod.DEBT:
                if not attrs.get("client_id"):
                    raise serializers.ValidationError({"client_id": "При продаже в долг выбор клиента обязателен."})
                from apps.main.services_debt import normalize_debt_prepayment_method

                total_prepay = attrs.get("prepayment") or attrs.get("prepayment_amount")
                if total_prepay is None:
                    if attrs.get("cash_amount") is not None or attrs.get("card_amount") is not None:
                        total_prepay = (attrs.get("cash_amount") or Decimal("0.00")) + (attrs.get("card_amount") or Decimal("0.00"))
                    else:
                        total_prepay = cash_received or Decimal("0.00")

                if total_prepay < Decimal("0.00"):
                    raise serializers.ValidationError({"cash_received": "Не может быть отрицательной."})
                if total_prepay > sale_total:
                    raise serializers.ValidationError({"cash_received": "Внесённая сумма не может быть больше суммы чека."})

                raw_prepay_method = attrs.get("prepayment_method")
                if total_prepay > Decimal("0.00") and raw_prepay_method is not None:
                    prepay_method = normalize_debt_prepayment_method(raw_prepay_method)
                else:
                    prepay_method = "cash"

                attrs["prepayment_method"] = prepay_method

                if prepay_method == "cash":
                    attrs["cash_amount"] = total_prepay
                    attrs["card_amount"] = Decimal("0.00")
                    attrs["cash_received"] = total_prepay
                    attrs["card_method"] = None
                else:
                    attrs["cash_amount"] = Decimal("0.00")
                    attrs["card_amount"] = total_prepay
                    attrs["cash_received"] = Decimal("0.00")
                    attrs["card_method"] = prepay_method
            else:
                attrs["prepayment_method"] = None
                if payment_method == Sale.PaymentMethod.CASH:
                    if cash_received is None:
                        raise serializers.ValidationError({"cash_received": "Укажите сумму, принятую наличными."})
                    if cash_received < 0:
                        raise serializers.ValidationError({"cash_received": "Не может быть отрицательной."})
                else:
                    if cash_received is None:
                        attrs["cash_received"] = Decimal("0.00")
                    elif cash_received < 0:
                        raise serializers.ValidationError({"cash_received": "Не может быть отрицательной."})

        # --- касса/смена ---
        if getattr(cart, "shift_id", None):
            # уже привязано на старте продажи — не трогаем
            attrs["cashbox_id"] = None
            attrs["shift_id"] = None
            return attrs

        cb = self._resolve_cashbox(
            cart,
            attrs.get("cashbox_id"),
            branch_id=attrs.get("branch_id"),
            cashbox_role=attrs.get("cashbox_role"),
        )
        sh = self._resolve_shift(cart, cb, user, attrs.get("shift_id"))

        attrs["cashbox_id"] = cb.id
        attrs["shift_id"] = sh.id
        return attrs


class PayDebtSerializer(serializers.Serializer):
    """
    Оплата ранее оформленной продажи "в долг" (Sale.status=DEBT).
    Делает продажу PAID и тем самым она начинает учитываться в кассе/сменах/аналитике.
    """

    payment_method = serializers.ChoiceField(
        choices=[c for c in Sale.PaymentMethod.choices if c[0] not in (Sale.PaymentMethod.DEBT, Sale.PaymentMethod.OFFSET)],
        required=True,
    )
    cash_received = MoneyField(required=False, allow_null=True)

    def validate(self, attrs):
        sale = self.context.get("sale")
        if sale is None:
            raise serializers.ValidationError("Serializer context должен содержать sale.")

        pm = attrs.get("payment_method")
        cash_received = attrs.get("cash_received")

        if pm == Sale.PaymentMethod.CASH:
            if cash_received is None:
                raise serializers.ValidationError({"cash_received": "Укажите сумму, принятую наличными."})
            if cash_received < 0:
                raise serializers.ValidationError({"cash_received": "Не может быть отрицательной."})
            if cash_received < (sale.total or Decimal("0.00")):
                raise serializers.ValidationError({"cash_received": "Сумма, полученная наличными, меньше суммы долга."})
        else:
            # Для безналичных методов наличных нет
            attrs["cash_received"] = Decimal("0.00")

        return attrs


class MobileScannerTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = MobileScannerToken
        fields = ("token", "expires_at")
        read_only_fields = ("token", "expires_at")


class CartItemDeletionLogSerializer(serializers.ModelSerializer):
    """Журнал удалений позиций из корзины: товар, количество, кто, время."""

    deleted_by_display = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = CartItemDeletionLog
        fields = (
            "id",
            "cart_id",
            "product",
            "product_name",
            "quantity",
            "deleted_by",
            "deleted_by_display",
            "created_at",
        )
        read_only_fields = fields

    def get_deleted_by_display(self, obj):
        u = obj.deleted_by
        if not u:
            return None
        name = getattr(u, "get_full_name", lambda: "")() or ""
        if name.strip():
            return name.strip()
        return getattr(u, "email", None) or str(getattr(u, "pk", ""))


def _sale_payment_split(sale):
    """(cash_amount, card_amount) по строкам оплаты или полям чека; None, если разбивка неизвестна."""
    c = getattr(sale, "cash_amount", None)
    k = getattr(sale, "card_amount", None)
    if c is not None or k is not None:
        return str(money(c or Decimal("0.00"))), str(money(k or Decimal("0.00")))
    if sale.status == Sale.Status.DEBT or sale.payment_method == Sale.PaymentMethod.DEBT:
        c_amt = sale.cash_received or Decimal("0.00")
        return str(money(c_amt)), "0.00"
    lines = sale.payment_lines()
    if not lines or any(line.method == Sale.PaymentMethod.MIXED for line in lines):
        if sale.payment_method == Sale.PaymentMethod.CASH:
            return str(money(sale.total or Decimal("0.00"))), "0.00"
        return None, None
    cash = sum((line.amount for line in lines if line.method == Sale.PaymentMethod.CASH), Decimal("0.00"))
    card = sum((line.amount for line in lines if line.method != Sale.PaymentMethod.CASH and line.method != Sale.PaymentMethod.DEBT), Decimal("0.00"))
    return str(money(cash)), str(money(card))


def _linked_debt_deal(sale):
    """Сделка-долг, созданная из этой продажи (09). Берёт prefetch deals, если он есть."""
    for deal in sale.deals.all():
        if deal.kind == ClientDeal.Kind.DEBT:
            return deal
    return None


def _sale_debt_amount(sale):
    """Сколько ещё должны по продаже: по остатку сделки или debt_remaining, если есть; погашенная — 0."""
    is_debt = sale.status == Sale.Status.DEBT or sale.payment_method == Sale.PaymentMethod.DEBT
    if not is_debt or sale.status == Sale.Status.PAID:
        return money(Decimal("0.00"))
    if getattr(sale, "debt_remaining", None) is not None:
        return money(sale.debt_remaining)
    deal = _linked_debt_deal(sale)
    if deal is not None:
        remaining = deal.remaining_debt
    else:
        remaining = (sale.total or Decimal("0.00")) - (sale.cash_received or Decimal("0.00"))
    return money(remaining if remaining > Decimal("0.00") else Decimal("0.00"))


def _sale_returns_info(sale):
    """ТЗ Н-07: (сумма возвратов, {sale_item_id: возвращённое кол-во}) по SaleReturn чека.
    Берём prefetch `returns`, если он есть; кэшируем на объекте продажи."""
    cached = getattr(sale, "_returns_info_cache", None)
    if cached is not None:
        return cached
    total = Decimal("0.00")
    per_line = {}
    for r in sale.returns.all():
        total += Decimal(str(r.returned_amount or 0))
        for row in (r.returned_items or []):
            sid = row.get("sale_item") if isinstance(row, dict) else None
            if not sid:
                continue
            try:
                per_line[str(sid)] = per_line.get(str(sid), Decimal("0")) + Decimal(str(row.get("qty") or 0))
            except Exception:
                continue
    cached = (money(total), per_line)
    sale._returns_info_cache = cached
    return cached


def _sale_refunded_total(sale):
    return str(_sale_returns_info(sale)[0])


def _sale_refundable_total(sale):
    """Сколько ещё можно вернуть деньгами: текущий итог чека (после частичных возвратов
    `total` уже уменьшен на возвращённое); у отменённого/полностью возвращённого — 0."""
    if sale.status == Sale.Status.CANCELED:
        return "0.00"
    return str(money(max(Decimal(str(sale.total or 0)), Decimal("0.00"))))


class SaleListSerializer(serializers.ModelSerializer):
    user_display = serializers.SerializerMethodField()
    consultant_display = serializers.SerializerMethodField(read_only=True)
    client_name = serializers.CharField(source="client.full_name", read_only=True)
    change = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    first_item_name = serializers.SerializerMethodField(read_only=True)
    items_preview = serializers.SerializerMethodField(read_only=True)
    items_count = serializers.SerializerMethodField(read_only=True)
    matched_item_name = serializers.CharField(read_only=True, allow_null=True)

    shift = serializers.PrimaryKeyRelatedField(read_only=True)
    cashbox = serializers.PrimaryKeyRelatedField(read_only=True)
    branch = serializers.PrimaryKeyRelatedField(read_only=True)
    cashbox_name = serializers.SerializerMethodField(read_only=True)
    debt_amount = serializers.SerializerMethodField(read_only=True)
    deal_id = serializers.SerializerMethodField(read_only=True)
    number = serializers.IntegerField(source="doc_number", read_only=True)
    cash_amount = serializers.SerializerMethodField(read_only=True)
    card_amount = serializers.SerializerMethodField(read_only=True)
    paid_now = serializers.SerializerMethodField(read_only=True)
    debt_initial = serializers.SerializerMethodField(read_only=True)
    debt_remaining = serializers.SerializerMethodField(read_only=True)
    refunded_total = serializers.SerializerMethodField(read_only=True)  # ТЗ Н-07
    refundable_total = serializers.SerializerMethodField(read_only=True)  # ТЗ Н-07
    debt_paid = serializers.SerializerMethodField(read_only=True)  # ТЗ Н-14
    prepayment = serializers.SerializerMethodField(read_only=True)  # ТЗ Н-14
    due_date = serializers.SerializerMethodField(read_only=True)  # ТЗ Н-14
    created_by = serializers.SerializerMethodField(read_only=True)  # Б-67
    sold_at = serializers.SerializerMethodField(read_only=True)  # Б-68
    # ТЗ 29.10 Б-99
    bonus_used = serializers.SerializerMethodField(read_only=True)
    bonus_earned = serializers.SerializerMethodField(read_only=True)
    branch_name = serializers.SerializerMethodField(read_only=True)

    def get_bonus_used(self, obj):
        return str(money(obj.bonus_redeemed or Decimal("0.00")))

    def get_bonus_earned(self, obj):
        val = getattr(obj, "bonus_earned_sum", None)
        if val is None:  # вне списка pos/sales/ (нет аннотации) — отдельный запрос
            from django.db.models import Sum
            val = obj.bonus_transactions.filter(reason="earn").aggregate(s=Sum("delta"))["s"]
        return str(money(val or Decimal("0.00")))

    def get_branch_name(self, obj):
        b = getattr(obj, "branch", None)
        return b.name if b else None

    class Meta:
        model = Sale
        fields = (
            "source", "device_key", "device_name", "created_by", "sold_at",  # Б-67, Б-68
            "bonus_used", "bonus_earned", "branch_name",  # Б-99
            "id",
            "number",
            "branch",
            "status",
            "subtotal",
            "discount_total",
            "tax_total",
            "total",
            "created_at",
            "paid_at",
            "user_display",
            "consultant",
            "consultant_display",
            "consultant_commission_enabled",
            "consultant_commission_percent",
            "consultant_commission_amount",
            "client",
            "client_name",
            "payment_method",
            "cash_received",
            "change",
            "shift",
            "cashbox",
            "cashbox_name",
            "first_item_name",
            "items_preview",
            "items_count",
            "matched_item_name",
            "debt_amount",
            "deal_id",
            "cash_amount",
            "card_amount",
            "paid_now",
            "debt_initial",
            "debt_remaining",
            "refunded_total",
            "refundable_total",
            "debt_paid",
            "prepayment",
            "due_date",
        )

    def get_created_by(self, obj):
        from apps.main.tz26.pos_sync.services import user_brief
        return user_brief(obj.user)

    def get_sold_at(self, obj):
        # Время продажи: у досланной офлайн-продажи created_at = paid_at = время на кассе (received_at - приём сервером)
        val = obj.paid_at or obj.created_at
        return serializers.DateTimeField().to_representation(val) if val else None

    def get_debt_paid(self, obj):
        """ТЗ Н-14: сколько долга уже погашено = debt_initial − debt_remaining."""
        paid = Decimal(str(obj.debt_initial or 0)) - Decimal(str(obj.debt_remaining or 0))
        return str(money(max(paid, Decimal("0.00"))))

    def get_prepayment(self, obj):
        """ТЗ Н-14: предоплата при оформлении (= paid_now)."""
        return str(money(obj.paid_now or Decimal("0.00")))

    def get_due_date(self, obj):
        """ТЗ Н-14: срок ближайшего непогашенного взноса связанной долговой сделки."""
        deal = _linked_debt_deal(obj)
        if deal is None:
            return None
        dues = [
            i.due_date for i in deal.installments.all()
            if i.due_date and (i.paid_amount or 0) < (i.amount or 0)
        ]
        return min(dues).isoformat() if dues else None

    def get_refunded_total(self, obj):
        return _sale_refunded_total(obj)

    def get_refundable_total(self, obj):
        return _sale_refundable_total(obj)

    def get_paid_now(self, obj):
        return str(money(obj.paid_now or Decimal("0.00")))

    def get_debt_initial(self, obj):
        return str(money(obj.debt_initial or Decimal("0.00")))

    def get_debt_remaining(self, obj):
        return str(money(obj.debt_remaining or Decimal("0.00")))

    def get_cash_amount(self, obj):
        return _sale_payment_split(obj)[0]

    def get_card_amount(self, obj):
        return _sale_payment_split(obj)[1]

    def get_user_display(self, obj):
        u = obj.user
        if not u:
            return None
        return (
            getattr(u, "get_full_name", lambda: "")()
            or getattr(u, "email", None)
            or getattr(u, "username", None)
        )

    def get_consultant_display(self, obj):
        u = getattr(obj, "consultant", None)
        if not u:
            return None
        return (
            getattr(u, "get_full_name", lambda: "")()
            or getattr(u, "full_name", None)
            or getattr(u, "email", None)
            or getattr(u, "username", None)
        )

    def get_first_item_name(self, obj):
        # items предзагружены (prefetch) — без запроса на каждую строку списка;
        # порядок как у .first() (по pk), чтобы «первая позиция» была стабильной.
        items = list(obj.items.all())
        if not items:
            return None
        item = min(items, key=lambda i: str(i.id))
        return (item.name_snapshot or "").strip() or getattr(getattr(item, "product", None), "name", None)

    def get_items_preview(self, obj):
        return self.get_first_item_name(obj)

    def get_items_count(self, obj):
        return len(obj.items.all())

    def get_cashbox_name(self, obj):
        cb = getattr(obj, "cashbox", None)
        if not cb:
            return None
        if getattr(cb, "branch", None):
            return f"Касса филиала {cb.branch.name}"
        return cb.name or "Касса компании"

    def get_debt_amount(self, obj):
        return _sale_debt_amount(obj)

    def get_deal_id(self, obj):
        deal = _linked_debt_deal(obj)
        return str(deal.id) if deal else None


class SaleItemReadSerializer(serializers.ModelSerializer):
    variant_size = serializers.CharField(source="variant.size", read_only=True, default=None)
    variant_color = serializers.CharField(source="variant.color", read_only=True, default=None)
    product_name = serializers.SerializerMethodField()
    kind = serializers.CharField(source="product.kind", read_only=True, default="product")
    line_total = serializers.SerializerMethodField()
    manual_discount_ignored = serializers.SerializerMethodField()
    item_id = serializers.UUIDField(source="id", read_only=True)
    line_id = serializers.UUIDField(source="id", read_only=True)
    sale_item_id = serializers.UUIDField(source="id", read_only=True)
    price_manually_edited = serializers.BooleanField(read_only=True)
    price_override_reason = serializers.SerializerMethodField()
    returnable_qty = serializers.SerializerMethodField()
    quantity_refunded = serializers.SerializerMethodField()  # ТЗ Н-07
    sale_unit = serializers.SerializerMethodField()  # ТЗ 26.10 Б-53
    base_quantity = serializers.SerializerMethodField()

    is_custom = serializers.BooleanField(read_only=True)
    price = serializers.SerializerMethodField(read_only=True)
    discount = serializers.SerializerMethodField(read_only=True)
    amount = serializers.SerializerMethodField(read_only=True)
    cost_price = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = SaleItem
        fields = (
            "id",
            "item_id",
            "line_id",
            "sale_item_id",
            "product",
            "is_custom",
            "price",
            "discount",
            "amount",
            "cost_price",
            "variant",
            "variant_size",
            "variant_color",
            "performer",
            "performer_commission_amount",
            "kind",
            "product_name",
            "name_snapshot",
            "barcode_snapshot",
            "unit_price",
            "line_discount",
            "manual_discount",
            "discount_source",
            "promotion_id",
            "manual_discount_ignored",
            "quantity",
            "line_total",
            "price_manually_edited",
            "price_override_reason",
            "returnable_qty",
            "quantity_refunded",
            "sale_unit",
            "base_quantity",
        )
        read_only_fields = fields

    def get_sale_unit(self, obj):
        from apps.main.tz26.hw_units.units import get_sale_unit_for_line

        return get_sale_unit_for_line(obj)

    def get_base_quantity(self, obj):
        from apps.main.tz26.hw_units.units import get_base_quantity_for_line

        return get_base_quantity_for_line(obj)

    def get_quantity_refunded(self, obj):
        """ТЗ Н-07: сколько по строке уже возвращено (по SaleReturn.returned_items)."""
        sale = getattr(obj, "sale", None)
        if sale is None:
            return "0.000"
        return str(qty3(_sale_returns_info(sale)[1].get(str(obj.id), Decimal("0"))))

    def get_price(self, obj):
        return str(money(obj.unit_price or Decimal("0.00")))

    def get_discount(self, obj):
        return str(money(obj.line_discount or Decimal("0.00")))

    def get_amount(self, obj):
        return str(obj.line_total)

    def get_cost_price(self, obj):
        return str(money(obj.purchase_price_snapshot or Decimal("0.00")))

    def get_product_name(self, obj):
        return get_attr(get_attr(obj, "product", None), "name", None) or obj.name_snapshot

    def get_manual_discount_ignored(self, obj):
        """Ручная скидка кассира не применена, потому что сработала акция (BE2-06)."""
        return obj.discount_source == "promotion" and (obj.manual_discount or 0) > 0

    def get_line_total(self, obj):
        base = (obj.unit_price or Decimal("0")) * Decimal(obj.quantity or 0)
        disc = Decimal(str(getattr(obj, "line_discount", None) or 0))
        return money(base - disc)

    def get_returnable_qty(self, obj):
        """
        Сколько ещё можно вернуть по этой строке.

        Отдельного счётчика возвратов у строки нет: частичный возврат уменьшает
        саму `quantity` (а полностью возвращённая строка удаляется), поэтому
        остаток к возврату — это и есть текущее количество. Поле отдаём явно,
        чтобы фронт ограничивал «Макс. возврат» по стабильному имени, а не
        догадывался по `quantity`.
        """
        return qty3(Decimal(str(obj.quantity or 0)))

    def get_price_override_reason(self, obj):
        if getattr(obj, "price_manually_edited", False):
            return "manual"
        if (getattr(obj, "line_discount", None) or Decimal("0")) > Decimal("0"):
            return "discount"
        return None


class SalePaymentReadSerializer(serializers.Serializer):
    method = serializers.CharField()
    method_display = serializers.CharField()
    amount = serializers.CharField()


class SaleDetailSerializer(serializers.ModelSerializer):
    user_display = serializers.SerializerMethodField(read_only=True)
    consultant_display = serializers.SerializerMethodField(read_only=True)
    items = SaleItemReadSerializer(many=True, read_only=True)
    client_name = serializers.CharField(source="client.full_name", read_only=True)
    change = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    payments = serializers.SerializerMethodField(read_only=True)
    deal_id = serializers.SerializerMethodField(read_only=True)
    remaining_debt = serializers.SerializerMethodField(read_only=True)

    shift = serializers.PrimaryKeyRelatedField(read_only=True)
    cashbox = serializers.PrimaryKeyRelatedField(read_only=True)
    branch = serializers.PrimaryKeyRelatedField(read_only=True)
    cashbox_name = serializers.SerializerMethodField(read_only=True)
    number = serializers.IntegerField(source="doc_number", read_only=True)
    cash_amount = serializers.SerializerMethodField(read_only=True)
    card_amount = serializers.SerializerMethodField(read_only=True)
    paid_now = serializers.SerializerMethodField(read_only=True)
    debt_initial = serializers.SerializerMethodField(read_only=True)
    debt_remaining = serializers.SerializerMethodField(read_only=True)
    debt_amount = serializers.SerializerMethodField(read_only=True)
    refunded_total = serializers.SerializerMethodField(read_only=True)  # ТЗ Н-07
    refundable_total = serializers.SerializerMethodField(read_only=True)  # ТЗ Н-07
    created_by = serializers.SerializerMethodField(read_only=True)  # Б-67

    class Meta:
        model = Sale
        fields = (
            "source", "device_key", "device_name", "created_by",  # Б-67
            "id",
            "number",
            "branch",
            "status",
            "subtotal",
            "discount_total",
            "tax_total",
            "total",
            "created_at",
            "paid_at",
            "user_display",
            "consultant",
            "consultant_display",
            "consultant_commission_enabled",
            "consultant_commission_percent",
            "consultant_commission_amount",
            "client",
            "client_name",
            "items",
            "payment_method",
            "cash_received",
            "change",
            "payments",
            "deal_id",
            "remaining_debt",
            "shift",
            "cashbox",
            "cashbox_name",
            "ekassa_fiscal",
            "cash_amount",
            "card_amount",
            "paid_now",
            "debt_initial",
            "debt_remaining",
            "debt_amount",
            "refunded_total",
            "refundable_total",
        )
        read_only_fields = fields

    def get_created_by(self, obj):
        from apps.main.tz26.pos_sync.services import user_brief
        return user_brief(obj.user)

    def get_refunded_total(self, obj):
        return _sale_refunded_total(obj)

    def get_refundable_total(self, obj):
        return _sale_refundable_total(obj)

    def get_paid_now(self, obj):
        return str(money(obj.paid_now or Decimal("0.00")))

    def get_debt_initial(self, obj):
        return str(money(obj.debt_initial or Decimal("0.00")))

    def get_debt_remaining(self, obj):
        if getattr(obj, "debt_remaining", None) is not None:
            return str(money(obj.debt_remaining))
        return self.get_remaining_debt(obj) or "0.00"

    def get_debt_amount(self, obj):
        return str(_sale_debt_amount(obj))

    def get_cash_amount(self, obj):
        return _sale_payment_split(obj)[0]

    def get_card_amount(self, obj):
        return _sale_payment_split(obj)[1]

    def get_deal_id(self, obj):
        deal = obj.deals.first() if hasattr(obj, "deals") else None
        if deal:
            return str(deal.id)
        legacy = getattr(obj, "legacy_debts", None)
        if legacy and legacy.exists():
            return str(legacy.first().id)
        return None

    def get_remaining_debt(self, obj):
        deal = obj.deals.first() if hasattr(obj, "deals") else None
        if deal:
            return str(deal.remaining_debt)
        legacy = getattr(obj, "legacy_debts", None)
        if legacy and legacy.exists():
            return str(legacy.first().balance)
        return None

    def get_consultant_display(self, obj):
        u = getattr(obj, "consultant", None)
        if not u:
            return None
        return (
            getattr(u, "get_full_name", lambda: "")()
            or getattr(u, "full_name", None)
            or getattr(u, "email", None)
            or getattr(u, "username", None)
        )

    def get_payments(self, obj):
        from apps.main.pos_utils import fmt_money

        lines = obj.payment_lines()
        out = []
        for line in lines:
            try:
                method_display = line.get_method_display()
            except Exception:
                method_display = line.method
            out.append(
                {
                    "method": line.method,
                    "method_display": method_display,
                    "amount": fmt_money(line.amount),
                }
            )
        return out

    def get_user_display(self, obj):
        u = obj.user
        if not u:
            return None
        return (
            getattr(u, "get_full_name", lambda: "")()
            or getattr(u, "email", None)
            or getattr(u, "username", None)
        )

    def get_cashbox_name(self, obj):
        cb = getattr(obj, "cashbox", None)
        if not cb:
            return None
        if getattr(cb, "branch", None):
            return f"Касса филиала {cb.branch.name}"
        return cb.name or "Касса компании"


class SaleStatusUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sale
        fields = ("status",)


class ReceiptItemSerializer(serializers.Serializer):
    name = serializers.CharField()
    qty = serializers.FloatField()
    price = serializers.FloatField()
    line_discount = serializers.FloatField(required=False, default=0.0)
    line_total = serializers.FloatField(required=False, default=0.0)


class ReceiptSerializer(serializers.Serializer):
    doc_no = serializers.CharField()
    company = serializers.CharField(allow_blank=True, required=False)
    created_at = serializers.CharField(allow_null=True, required=False)
    cashier_name = serializers.CharField(allow_null=True, required=False)
    items = ReceiptItemSerializer(many=True)
    discount = serializers.FloatField(required=False, default=0.0)
    tax = serializers.FloatField(required=False, default=0.0)
    paid_cash = serializers.FloatField(required=False, default=0.0)
    paid_card = serializers.FloatField(required=False, default=0.0)
    change = serializers.FloatField(required=False, default=0.0)


class AgentCheckoutSerializer(serializers.Serializer):
    print_receipt = serializers.BooleanField(default=False)
    client_id = OptionalUUIDField(required=False, allow_null=True)
    # алиас для клиентов, которые шлют `client` вместо `client_id`
    client = OptionalUUIDField(required=False, allow_null=True, write_only=True)
    # Разрешить "в минус" при оформлении агентской продажи (только owner/admin).
    allow_minus = serializers.BooleanField(required=False, default=False)

    # Консультант и его комиссия
    consultant_id = OptionalUUIDField(required=False, allow_null=True)
    consultant_commission_enabled = serializers.BooleanField(required=False, default=False)
    consultant_commission_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)

    payment_method = serializers.ChoiceField(
        choices=[c for c in Sale.PaymentMethod.choices if c[0] != Sale.PaymentMethod.OFFSET],
        default=Sale.PaymentMethod.CASH,
        required=False,
    )
    cash_received = MoneyField(required=False, allow_null=True)
    prepayment = MoneyField(required=False, allow_null=True)
    prepayment_amount = MoneyField(required=False, allow_null=True)

    # если хочешь сохранять кассу в продаже — оставь
    cashbox_id = OptionalUUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs.get("client_id") is None and attrs.get("client") is not None:
            attrs["client_id"] = attrs.pop("client")
        elif "client" in attrs:
            attrs.pop("client", None)

        consultant_id = attrs.get("consultant_id")
        comm_enabled = attrs.get("consultant_commission_enabled", False)
        comm_pct = attrs.get("consultant_commission_percent")

        if consultant_id:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            request = self.context.get("request")
            company_id = getattr(request.user, "company_id", None) if request and getattr(request, "user", None) else None
            user_qs = User.objects.filter(id=consultant_id, is_active=True)
            if company_id:
                user_qs = user_qs.filter(company_id=company_id)
            consultant_user = user_qs.first()
            if not consultant_user:
                raise serializers.ValidationError({"consultant_id": "Консультант не найден или не активен."})
            attrs["consultant_obj"] = consultant_user
        else:
            attrs["consultant_obj"] = None

        if comm_enabled:
            if not consultant_id:
                raise serializers.ValidationError({"consultant_id": "Нельзя включить комиссию без указания консультанта."})
            if comm_pct is None:
                raise serializers.ValidationError({"consultant_commission_percent": "Укажите процент комиссии консультанта."})
            if comm_pct < 0 or comm_pct > 100:
                raise serializers.ValidationError({"consultant_commission_percent": "Процент комиссии должен быть от 0 до 100."})

        pm = attrs.get("payment_method") or Sale.PaymentMethod.CASH
        cr = attrs.get("cash_received")

        if pm == Sale.PaymentMethod.DEBT and not attrs.get("client_id"):
            raise serializers.ValidationError({"client_id": "При продаже в долг выбор клиента обязателен."})

        if pm == Sale.PaymentMethod.CASH:
            if cr is None:
                raise serializers.ValidationError({"cash_received": "Укажите сумму, принятую наличными."})
            if cr < 0:
                raise serializers.ValidationError({"cash_received": "Не может быть отрицательной."})
        else:
            if cr is None:
                attrs["cash_received"] = Decimal("0.00")
            elif cr < 0:
                raise serializers.ValidationError({"cash_received": "Не может быть отрицательной."})

        return attrs
