from rest_framework import serializers
from decimal import Decimal
from apps.main.models import BranchTransfer, BranchTransferItem


def fmt_decimal(val, places: int) -> str:
    if val is None:
        return "0." + "0" * places
    try:
        d = Decimal(str(val))
        return f"{d:.{places}f}"
    except Exception:
        return str(val)


class BranchRefSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()


class UserRefSerializer(serializers.Serializer):
    id = serializers.CharField()
    full_name = serializers.CharField()


class SellerSerializer(serializers.Serializer):
    name = serializers.CharField(allow_blank=True, default="")
    inn = serializers.CharField(allow_blank=True, default="")
    okpo = serializers.CharField(allow_blank=True, default="")
    address = serializers.CharField(allow_blank=True, default="")
    phone = serializers.CharField(allow_blank=True, default="")


class BranchTransferItemSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)
    product = serializers.CharField(source="product_id", read_only=True)
    quantity = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    amount = serializers.SerializerMethodField()

    class Meta:
        model = BranchTransferItem
        fields = [
            "id",
            "product",
            "name",
            "article",
            "barcode",
            "unit",
            "quantity",
            "price",
            "amount",
        ]

    def get_quantity(self, obj):
        return fmt_decimal(obj.quantity, 3)

    def get_price(self, obj):
        return fmt_decimal(obj.price, 2)

    def get_amount(self, obj):
        return fmt_decimal(obj.amount, 2)


class BranchTransferListSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)
    from_branch = serializers.SerializerMethodField()
    to_branch = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    total_quantity = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()

    class Meta:
        model = BranchTransfer
        fields = [
            "id",
            "number",
            "status",
            "date",
            "from_branch",
            "to_branch",
            "comment",
            "created_by",
            "items_count",
            "total_quantity",
            "total_amount",
            "created_at",
            "cancelled_at",
            "cancel_reason",
        ]

    def get_from_branch(self, obj):
        if not obj.from_branch_id:
            return None
        return {"id": str(obj.from_branch_id), "name": obj.from_branch.name if obj.from_branch else ""}

    def get_to_branch(self, obj):
        if not obj.to_branch_id:
            return None
        return {"id": str(obj.to_branch_id), "name": obj.to_branch.name if obj.to_branch else ""}

    def get_created_by(self, obj):
        if not obj.created_by_id or not obj.created_by:
            return None
        u = obj.created_by
        name = u.get_full_name().strip() if hasattr(u, "get_full_name") else ""
        if not name:
            name = getattr(u, "username", "") or getattr(u, "phone", "")
        return {"id": str(u.id), "full_name": name}

    def get_items_count(self, obj):
        # Если префетчен items, считаем через len, иначе count()
        if hasattr(obj, "_prefetched_objects_cache") and "items" in obj._prefetched_objects_cache:
            return len(obj.items.all())
        return obj.items.count()

    def get_total_quantity(self, obj):
        return fmt_decimal(obj.total_quantity, 3)

    def get_total_amount(self, obj):
        return fmt_decimal(obj.total_amount, 2)


class BranchTransferDetailSerializer(BranchTransferListSerializer):
    seller = serializers.SerializerMethodField()
    items = BranchTransferItemSerializer(many=True, read_only=True)
    # ТЗ кассы 05.11 п.17: в схеме POST (можно и заголовком Idempotency-Key)
    idempotency_key = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=128)

    class Meta(BranchTransferListSerializer.Meta):
        fields = BranchTransferListSerializer.Meta.fields + [
            "seller",
            "items",
            "idempotency_key",
        ]

    def get_seller(self, obj):
        c = obj.company
        if not c:
            return {}
        return {
            "name": getattr(c, "llc", None) or getattr(c, "name", None) or "",
            "inn": getattr(c, "inn", None) or "",
            "okpo": getattr(c, "okpo", None) or "",
            "address": getattr(c, "address", None) or "",
            "phone": getattr(c, "phone", None) or "",
        }
