from rest_framework import serializers


class TemplateInSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["lease", "employment"])
    name = serializers.CharField(max_length=255)
    text = serializers.CharField(required=False, allow_blank=True, max_length=300000, default="")


class TermsSerializer(serializers.Serializer):
    """Условия аренды (шаг 3 мастера). Все поля необязательны на предпросмотре."""
    start_date = serializers.DateField(required=False, allow_null=True)
    end_date = serializers.DateField(required=False, allow_null=True)
    rent_monthly = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    pay_day = serializers.IntegerField(required=False, min_value=1, max_value=31, default=10)
    deposit_months = serializers.IntegerField(required=False, min_value=0, default=0)
    deposit_amount = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    penalty_rate = serializers.DecimalField(max_digits=6, decimal_places=3, required=False, allow_null=True,
                                            min_value=0)
    indexation_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, min_value=0,
                                                  default=0)
    utilities = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        s, e = attrs.get("start_date"), attrs.get("end_date")
        if s and e and e < s:
            raise serializers.ValidationError({"end_date": "Дата окончания раньше начала."})
        return attrs


class OverridesField(serializers.DictField):
    child = serializers.CharField(allow_blank=True)
