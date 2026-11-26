"""ТЗ Н-24 (1): проверка области, лимит запросов и журнал вызовов для API-ключа."""
import time

from django.core.cache import cache
from rest_framework.exceptions import PermissionDenied, Throttled
from rest_framework.permissions import BasePermission

from .models import ApiKey, ApiKeyCallLog


class HasApiKeyScope(BasePermission):
    """
    Вью задаёт `required_scope = "catalog:read"`. Обычные пользователи проходят как раньше;
    ключ — только с нужной областью (или с общим "write"/"read" для чтения), в пределах лимита.
    """

    def has_permission(self, request, view):
        key = request.auth
        if not isinstance(key, ApiKey):
            return True
        scope = getattr(view, "required_scope", None)
        if scope and not key.has_scope(scope) and not (
            key.has_scope(ApiKey.Scope.WRITE) or (key.has_scope(ApiKey.Scope.READ) and request.method in ("GET", "HEAD", "OPTIONS"))
        ):
            self._log(request, key, scope, False, "scope")
            raise PermissionDenied("У API-ключа нет области %s." % scope)
        limit = key.rate_limit_per_minute or 0
        if limit:
            minute = int(time.time() // 60)
            ckey = f"apikey_rl:{key.pk}:{minute}"
            try:
                cache.add(ckey, 0, timeout=70)
                used = cache.incr(ckey)
            except Exception:
                used = 0
            if used > limit:
                self._log(request, key, scope, False, "rate_limit")
                raise Throttled(wait=60 - int(time.time() % 60))
        self._log(request, key, scope, True, "")
        return True

    @staticmethod
    def _log(request, key, scope, allowed, reason):
        try:
            ApiKeyCallLog.objects.create(
                key=key, method=request.method[:8], path=request.path[:255],
                scope=scope or "", allowed=allowed, reason=reason,
            )
        except Exception:
            pass
