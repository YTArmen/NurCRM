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
        # в списках (many=True) поле не отдаём: иначе по 3 запроса на товар
        if isinstance(self.root, serializers.ListSerializer) or instance is None or getattr(instance, "pk", None) is None:
            raise SkipField()
        return instance

    def to_representation(self, instance):
        return services.product_prices_payload(instance)[self.key]

    def to_internal_value(self, data):
        if not isinstance(data, list):
            raise serializers.ValidationError("Ожидается список.")
        return {self.key: data}


class ProductPricesField(_HwProductPricesField):
    key = "prices"


class ProductVolumePricesField(_HwProductPricesField):
    key = "volume_prices"
