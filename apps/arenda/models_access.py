"""Безопасность входа пользователей модуля «Аренда» (ТЗ §3.7, §3.11): сеансы, пароль, блокировка."""
import uuid

from django.conf import settings
from django.db import models


class ArendaSecurity(models.Model):
    """По одной строке на пользователя (создаётся лениво).

    tokens_revoked_at / session_version: JWT, выданные раньше этой отметки, перестают работать в API
    аренды (в проекте нет JWT-blacklist, поэтому проверка сделана в get_ctx).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="arenda_security")
    password_changed_at = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(default=False)
    created_by_arenda = models.BooleanField(default=False)  # пользователь заведён из «Аренды» (можно блокировать целиком)
    tokens_revoked_at = models.DateTimeField(null=True, blank=True)
    session_version = models.PositiveIntegerField(default=0)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
