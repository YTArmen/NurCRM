from rest_framework import serializers

from apps.main.tz26.ai_stock.models import AiAction


class AiActionSerializer(serializers.ModelSerializer):
    user = serializers.UUIDField(source="user_id", read_only=True)
    user_name = serializers.SerializerMethodField()
    callId = serializers.CharField(source="call_id", read_only=True)

    class Meta:
        model = AiAction
        fields = (
            "id", "callId", "device", "client", "user", "user_name", "at", "utterance_hash", "utterance_text",
            "tool", "args", "result_status", "say", "object_type", "object_id", "amount",
            "reversible", "compensation_of", "model", "created_at",
        )

    def get_user_name(self, obj):
        u = obj.user
        if u is None:
            return None
        return (f"{u.first_name} {u.last_name}").strip() or u.email
