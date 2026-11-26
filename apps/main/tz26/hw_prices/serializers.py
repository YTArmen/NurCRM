"""ТЗ 26.10 Б-55: поля prices / volume_prices для ProductSerializer (читаются из таблиц, пишутся через save())."""
from rest_framework import serializers
from rest_framework.fields import SkipField

from . import services


class _HwProductPricesField(serializers.Field):
    key = ""

    def __init__(self, **kwargs):
        kwargs["source"] = "*"
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def get_attribute(self, instance):
        if instance is None or getattr(instance, "pk", None) is None:
            raise SkipField()
        # в списках (many=True) prices не отдаём: иначе по 3 запроса на товар.
        # mini2211: volume_prices в списке — только если ступени подгружены заранее (products/list).
        if isinstance(self.root, serializers.ListSerializer):
            if self.key != "volume_prices" or "volume_prices" not in getattr(instance, "_prefetched_objects_cache", {}):
                raise SkipField()
        return instance

    def to_representation(self, instance):
        if isinstance(self.root, serializers.ListSerializer):
            return [
                {"min_qty": services._qty_str(vp.min_qty), "price": str(services.q2(vp.price)),
                 "level": vp.level.code if vp.level_id else None}
                for vp in instance.volume_prices.all()
            ]
        return services.product_prices_payload(instance)[self.key]

    def to_internal_value(self, data):
        if not isinstance(data, list):
            raise serializers.ValidationError("Ожидается список.")
        return {self.key: data}


class ProductPricesField(_HwProductPricesField):
    key = "prices"


class ProductVolumePricesField(_HwProductPricesField):
    key = "volume_prices"
