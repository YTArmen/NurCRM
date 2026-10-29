import uuid
from decimal import Decimal
from rest_framework import serializers

from apps.main.telegram_bot.models import TelegramBotSettings, TelegramInquiry, TelegramCustomerProfile
from apps.main.telegram_bot.services import telegram_api
from django.conf import settings as django_settings


# Теги, которые понимает Telegram в parse_mode=HTML (core.telegram.org/bots/api#html-style)
TELEGRAM_HTML_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler",
    "a", "code", "pre", "blockquote", "tg-emoji",
}


def telegram_html_error(text):
    """
    ТЗ ч.11, 1.2 / ч.12, 2.5: разметка, которую Telegram не разберёт («can't parse entities»),
    — бот на ней промолчит. Возвращает текст ошибки или None.
    """
    from html.parser import HTMLParser

    stack, errors = [], []

    class _P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in TELEGRAM_HTML_TAGS:
                errors.append(f"тег <{tag}> Telegram не поддерживает")
                return
            if tag == "a" and not dict(attrs).get("href"):
                errors.append("у ссылки <a> нет href")
            stack.append(tag)

        def handle_startendtag(self, tag, attrs):
            errors.append(f"тег <{tag}/> Telegram не поддерживает")

        def handle_endtag(self, tag):
            if not stack or stack[-1] != tag:
                errors.append(f"лишний закрывающий тег </{tag}>" if tag not in stack else f"теги закрыты не по порядку: </{tag}>")
                return
            stack.pop()

    parser = _P(convert_charrefs=True)
    parser.feed(text)
    parser.close()
    if errors:
        return errors[0]
    if stack:
        return f"не закрыт тег <{stack[-1]}>"
    return None


class TelegramBotSettingsSerializer(serializers.ModelSerializer):
    token_set = serializers.BooleanField(read_only=True)
    ai_key_set = serializers.BooleanField(read_only=True)
    ai_source = serializers.SerializerMethodField(read_only=True)

    token = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="Токен от @BotFather. Пустая строка удаляет токен.",
    )
    ai_key = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="Ключ Google Gemini. Пустая строка удаляет ключ.",
    )

    class Meta:
        model = TelegramBotSettings
        fields = [
            "mode",
            "bot_username",
            "token_set",
            "webhook_ok",
            "webhook_error",
            "owner_chat_id",
            "owner_chat_title",
            "shift_summary_enabled",
            "commands_enabled",
            "ai_enabled",
            "ai_key_set",
            "ai_source",
            "ai_functions_enabled",
            "daily_report_time",
            "weekly_report",
            "monthly_report",
            "ai_daily_limit",
            "consultant_enabled",
            "customer_limit_per_hour",
            "voice_replies_enabled",
            "voice_language",
            "ai_min_markup_percent",
            "ai_owner_actions_enabled",
            "send_product_photos",
            "owner_phone",
            "orders_chat_id",
            "orders_chat_title",
            "orders_chat_bound_at",
            "orders_recipients",
            "orders_show_amount",
            "orders_show_phone",
            "orders_last_error",
            "last_update_at",
            "last_reply_at",
            "token",
            "ai_key",
        ]
        read_only_fields = ["orders_chat_id", "orders_chat_title", "orders_chat_bound_at", "orders_last_error", "bot_username", "token_set", "webhook_ok", "webhook_error", "ai_key_set", "ai_source", "last_update_at", "last_reply_at"]

    def get_ai_source(self, obj):
        return obj.ai_source

    def validate_orders_recipients(self, value):
        inst = self.instance
        if value in ("group", "both") and not (inst and inst.orders_chat_id):
            raise serializers.ValidationError("Сначала привяжите группу заказов.")
        return value

    def update(self, instance, validated_data):
        new_token = validated_data.pop("token", None)
        new_ai_key = validated_data.pop("ai_key", None)

        if new_token is not None:
            raw_token = new_token.strip()
            if raw_token:
                # Проверяем токен в Telegram через getMe
                try:
                    bot_info = telegram_api.get_me(raw_token)
                except telegram_api.TelegramAPIError as exc:
                    raise serializers.ValidationError({"token": [str(exc)]})
                except Exception as exc:
                    raise serializers.ValidationError({"token": [f"Telegram: {exc}"]})

                instance.token = raw_token
                instance.bot_username = bot_info.get("username") or ""

                # Генерируем secret_token и ставим webhook
                sec_token = uuid.uuid4().hex
                instance.secret_token = sec_token

                wh_url = telegram_api.build_webhook_url(instance.bot_uuid)

                try:
                    res = telegram_api.set_webhook(raw_token, wh_url, sec_token)
                    if res.get("ok"):
                        instance.webhook_ok = True
                        instance.webhook_error = None
                    else:
                        instance.webhook_ok = False
                        instance.webhook_error = res.get("description", "Failed to set webhook")
                except Exception as exc:
                    instance.webhook_ok = False
                    instance.webhook_error = str(exc)

            else:
                # Пустая строка — удалить токен и webhook (TZ 6)
                if instance.token:
                    try:
                        telegram_api.delete_webhook(instance.token)
                    except Exception:
                        pass
                instance.token = ""
                instance.bot_username = None
                instance.webhook_ok = False
                instance.webhook_error = None
                instance.secret_token = ""

        if new_ai_key is not None:
            instance.ai_key = new_ai_key.strip()

        old_mode = instance.mode
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if new_token is None and instance.mode != old_mode and instance.token:
            self._apply_mode_webhook(instance)

        instance.save()
        return instance

    @staticmethod
    def _apply_mode_webhook(instance):
        """Перенос бота: server — ставим вебхук, local — снимаем (компьютер опрашивает сам)."""
        if instance.mode == TelegramBotSettings.Mode.SERVER:
            instance.secret_token = instance.secret_token or uuid.uuid4().hex
            try:
                telegram_api.set_webhook(
                    instance.token, telegram_api.build_webhook_url(instance.bot_uuid), instance.secret_token
                )
                instance.webhook_ok = True
                instance.webhook_error = None
            except Exception as exc:
                instance.webhook_ok = False
                instance.webhook_error = str(exc)[:500]
        else:
            telegram_api.delete_webhook(instance.token)
            instance.webhook_ok = False
            instance.webhook_error = None


class TelegramInquiryOrderSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    number = serializers.IntegerField(read_only=True)
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)


class TelegramInquirySerializer(serializers.ModelSerializer):
    order = TelegramInquiryOrderSerializer(read_only=True)

    class Meta:
        model = TelegramInquiry
        fields = [
            "id",
            "created_at",
            "chat_id",
            "name",
            "username",
            "text",
            "reply",
            "is_voice",
            "order",
            "scenario_id",
            "scenario_title",
        ]


class TelegramCustomerSerializer(serializers.Serializer):
    chat_id = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    username = serializers.CharField(allow_blank=True)
    messages = serializers.IntegerField()
    orders = serializers.IntegerField()
    last_at = serializers.DateTimeField()
    client_id = serializers.UUIDField(allow_null=True)


RESERVED_COMMANDS = {
    "start", "help", "segodnya", "nedelya", "top", "abc", "zakaz",
    "ostatki", "dolgi", "dolg", "kassa", "prokat", "catalog", "cart", "orders",
}


class TelegramBotScenarioSerializer(serializers.ModelSerializer):
    class Meta:
        from apps.main.telegram_bot.models import TelegramBotScenario
        model = TelegramBotScenario
        fields = [
            "id",
            "kind",
            "command",
            "keywords",
            "title",
            "reply_text",
            "buttons",
            "photo_product",
            "audience",
            "languages",
            "priority",
            "is_active",
            "show_in_menu",
            "source",
            "hits",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "hits", "created_at", "updated_at"]

    def validate_command(self, value):
        cmd = (value or "").strip().lstrip("/").lower()
        return cmd

    def validate_title(self, value):
        val = (value or "").strip()
        if not val:
            raise serializers.ValidationError("Укажите название сценария.")
        if len(val) > 80:
            raise serializers.ValidationError("Название не должно превышать 80 символов.")
        return val

    def validate_reply_text(self, value):
        val = (value or "").strip()
        if not val:
            raise serializers.ValidationError("Текст ответа не может быть пустым.")
        if len(val) > 3500:
            raise serializers.ValidationError("Текст ответа не должен превышать 3500 символов.")
        lower = val.lower()
        if "javascript:" in lower or "data:" in lower:
            raise serializers.ValidationError("Запрещены ссылки javascript: и data:.")
        import re
        if re.search(r't\.me/[a-zA-Z0-9_]+bot\b', lower):
            raise serializers.ValidationError("Ссылки на других Telegram-ботов запрещены.")
        err = telegram_html_error(val)
        if err:
            raise serializers.ValidationError(f"Неверная разметка: {err}")
        return val

    def validate_buttons(self, value):
        if not value:
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("Кнопки должны быть списком.")
        if len(value) > 6:
            raise serializers.ValidationError("Не больше 6 кнопок.")
        import re
        for idx, b in enumerate(value):
            if not isinstance(b, dict):
                raise serializers.ValidationError(f"Кнопка #{idx+1} должна быть объектом.")
            text = str(b.get("text") or "").strip()
            if not text:
                raise serializers.ValidationError(f"У кнопки #{idx+1} не указан текст.")
            url = str(b.get("url") or "").strip()
            cmd = str(b.get("command") or "").strip()
            if not url and not cmd:
                raise serializers.ValidationError(f"У кнопки #{idx+1} укажите url или command.")
            if url:
                u_lower = url.lower()
                if "javascript:" in u_lower or "data:" in u_lower:
                    raise serializers.ValidationError(f"Кнопка #{idx+1}: запрещены javascript: и data:.")
                if re.search(r't\.me/[a-zA-Z0-9_]+bot\b', u_lower):
                    raise serializers.ValidationError(f"Кнопка #{idx+1}: ссылки на других ботов запрещены.")
                if not (u_lower.startswith("http://") or u_lower.startswith("https://") or u_lower.startswith("tel:") or u_lower.startswith("t.me/")):
                    raise serializers.ValidationError(f"Кнопка #{idx+1}: ссылка должна начинаться с https://, tel: или t.me/.")
        return value

    def validate(self, attrs):
        import re
        from apps.main.telegram_bot.models import TelegramBotScenario

        kind = attrs.get("kind") or getattr(self.instance, "kind", TelegramBotScenario.Kind.KEYWORDS)

        if kind == TelegramBotScenario.Kind.COMMAND:
            cmd = attrs.get("command")
            if cmd is None and self.instance:
                cmd = self.instance.command
            cmd = (cmd or "").strip().lstrip("/").lower()
            if not cmd:
                raise serializers.ValidationError({"command": ["Для вида 'command' укажите команду."]})
            if not re.match(r'^[a-z0-9_]{1,32}$', cmd):
                raise serializers.ValidationError({"command": ["Команда должна содержать 1-32 символов [a-z0-9_] без слэша."]})
            if cmd in RESERVED_COMMANDS:
                raise serializers.ValidationError({"command": ["Это встроенная команда, её нельзя переопределить."]})
            attrs["command"] = cmd

            # Проверка уникальности команды внутри компании
            company = self.context.get("company")
            if company:
                qs = TelegramBotScenario.objects.filter(company=company, kind=TelegramBotScenario.Kind.COMMAND, command=cmd)
                if self.instance:
                    qs = qs.exclude(id=self.instance.id)
                if qs.exists():
                    raise serializers.ValidationError({"command": ["Такая команда уже есть у компании."]})

        elif kind == TelegramBotScenario.Kind.KEYWORDS:
            kws = attrs.get("keywords")
            if kws is None and self.instance:
                kws = self.instance.keywords
            if not kws or not isinstance(kws, list):
                raise serializers.ValidationError({"keywords": ["Для вида 'keywords' укажите список ключевых слов."]})
            cleaned_kws = []
            for kw in kws:
                s = str(kw or "").strip()
                if len(s) < 2 or len(s) > 60:
                    raise serializers.ValidationError({"keywords": [f"Ключевое слово '{s}' должно содержать от 2 до 60 символов."]})
                cleaned_kws.append(s)
            if not cleaned_kws or len(cleaned_kws) > 30:
                raise serializers.ValidationError({"keywords": ["Количество ключевых слов должно быть от 1 до 30."]})
            attrs["keywords"] = cleaned_kws

        photo_product = attrs.get("photo_product")
        company = self.context.get("company")
        if photo_product and company and photo_product.company_id != company.id:
            raise serializers.ValidationError({"photo_product": ["Товар принадлежит другой компании."]})

        return attrs


class TelegramBotAuditSerializer(serializers.ModelSerializer):
    at = serializers.DateTimeField(source="created_at", read_only=True)
    event = serializers.CharField(source="action", read_only=True)
    order_id = serializers.SerializerMethodField()
    chat_id = serializers.SerializerMethodField()
    ok = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()

    def _ch(self, obj, key):
        return (obj.changes or {}).get(key) if obj.action == "order_notified" else None

    def get_order_id(self, obj):
        return self._ch(obj, "order_id")

    def get_chat_id(self, obj):
        return self._ch(obj, "chat_id")

    def get_ok(self, obj):
        return self._ch(obj, "ok")

    def get_error(self, obj):
        return self._ch(obj, "error")

    class Meta:
        from apps.main.telegram_bot.models import TelegramBotAudit
        model = TelegramBotAudit
        fields = [
            "id",
            "at",
            "user_name",
            "action",
            "object_title",
            "source",
            "changes",
            "event",
            "order_id",
            "chat_id",
            "ok",
            "error",
        ]
