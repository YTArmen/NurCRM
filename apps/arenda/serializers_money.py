from rest_framework import serializers

from .services.payments import CATEGORIES


class PaymentInSerializer(serializers.Serializer):
    tenant = serializers.UUIDField(required=False, allow_null=True)
    amount = serializers.IntegerField(min_value=1)
    method = serializers.ChoiceField(choices=["cash", "bank", "qr"])
    date = serializers.DateField(required=False, allow_null=True)
    add_penalty = serializers.BooleanField(required=False, default=False)
    comment = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)
    category = serializers.ChoiceField(choices=list(CATEGORIES), required=False, allow_blank=True, default="")
    branch = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs.get("tenant") and not attrs.get("category"):
            raise serializers.ValidationError(
                {"category": ["Для прихода без арендатора укажите категорию (реклама, парковка, прочее)."]})
        return attrs


class StornoSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)

    def validate_reason(self, v):
        if not v.strip():
            raise serializers.ValidationError("Укажите причину.")
        return v.strip()


class RemarkInSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["remark", "warning", "reprimand", "fine"])
    date = serializers.DateField(required=False, allow_null=True)
    text = serializers.CharField(required=False, allow_blank=True, default="")
    issued_by = serializers.UUIDField(required=False, allow_null=True)
    fine_amount = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    notify = serializers.BooleanField(required=False, default=False)
    channel = serializers.ChoiceField(choices=["sms", "whatsapp", "telegram"], required=False, default="sms")

    def validate(self, attrs):
        if attrs["type"] == "fine" and not attrs.get("fine_amount"):
            raise serializers.ValidationError({"fine_amount": ["Для штрафа укажите сумму."]})
        if attrs["type"] != "fine":
            attrs["fine_amount"] = None
        return attrs


class ActInSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()

    def validate(self, attrs):
        if attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_to": ["Конец периода раньше начала."]})
        return attrs
