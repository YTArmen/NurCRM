"""
JWT authentication for Django Channels WebSocket connections.

Token sources (in order):
  1. Query string: ?token=<access_token>
  2. Header: Authorization: Bearer <access_token>
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken, UntypedToken

User = get_user_model()
logger = logging.getLogger("nurcrm.websocket.auth")

# Paths that skip JWT (legacy agent socket).
JWT_SKIP_PATH_PREFIXES = ("ws/agents/", "ws/company/", "ws/main/events/")  # ws/company/ — токен первым сообщением (ТЗ Н-31)

# Rate-limit repeated failed auth handshakes per client+path (reconnect storm).
WS_AUTH_FAIL_CACHE_PREFIX = "ws:auth_fail:"
WS_AUTH_FAIL_WINDOW_SEC = 60
WS_AUTH_FAIL_MAX = 10
WS_USER_CACHE_TTL = 300
WS_USER_CACHE_PREFIX = "ws:user:"


def _normalize_ws_path(scope) -> str:
    return (scope.get("path") or "").lstrip("/")


def _client_cache_key(scope) -> str:
    client = scope.get("client") or ("unknown", 0)
    path = scope.get("path") or ""
    return f"{client[0]}:{client[1]}:{path}"


def _token_fingerprint(token: str | None) -> str:
    if not token:
        return "missing"
    token = token.strip()
    if len(token) <= 12:
        return "present:short"
    return f"present:{token[:8]}…"


def _extract_bearer_token(scope) -> str | None:
    query_string = (scope.get("query_string") or b"").decode()
    params = parse_qs(query_string)
    if "token" in params and params["token"][0]:
        return params["token"][0].strip()

    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            auth_val = value.decode()
            if auth_val.lower().startswith("bearer "):
                return auth_val.split(" ", 1)[1].strip()
            break
    return None


def _validate_access_token(token: str) -> tuple[str | None, str | None]:
    """
    Returns (user_id, error_code).
    error_code: missing | invalid | expired
    """
    try:
        UntypedToken(token)
        access = AccessToken(token)
        user_id = access.get("user_id")
        if not user_id:
            return None, "invalid"
        return str(user_id), None
    except TokenError as exc:
        msg = str(exc).lower()
        if "expired" in msg or "exp claim" in msg:
            return None, "expired"
        return None, "invalid"
    except Exception:
        return None, "invalid"


@database_sync_to_async
def _get_user_by_id_cached(user_id: str):
    cache_key = WS_USER_CACHE_PREFIX + user_id
    user = cache.get(cache_key)
    if user is not None:
        return user
    try:
        user = User.objects.get(id=user_id)
        cache.set(cache_key, user, WS_USER_CACHE_TTL)
        return user
    except User.DoesNotExist:
        return AnonymousUser()


def _register_auth_failure(scope) -> int:
    key = WS_AUTH_FAIL_CACHE_PREFIX + _client_cache_key(scope)
    try:
        fails = int(cache.get(key, 0)) + 1
        cache.set(key, fails, WS_AUTH_FAIL_WINDOW_SEC)
        return fails
    except Exception:
        return 0


def _clear_auth_failures(scope) -> None:
    key = WS_AUTH_FAIL_CACHE_PREFIX + _client_cache_key(scope)
    try:
        cache.delete(key)
    except Exception:
        pass


async def _reject_ws_http(
    send,
    *,
    status: int,
    body: bytes = b"",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    headers = [(b"content-type", b"text/plain; charset=utf-8")]
    if extra_headers:
        headers.extend(extra_headers)
    await send(
        {
            "type": "websocket.http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "websocket.http.response.body", "body": body})


async def _reject_auth_handshake(scope, send, *, error: str, token: str | None = None, user_id: str | None = None) -> None:
    fails = _register_auth_failure(scope)
    if user_id:
        logger.warning(
            "websocket auth failed path=%s error=%s user_id=%s fails=%s",
            scope.get("path"),
            error,
            user_id,
            fails,
        )
    else:
        logger.warning(
            "websocket auth failed path=%s error=%s token=%s fails=%s",
            scope.get("path"),
            error,
            _token_fingerprint(token),
            fails,
        )

    close_code = 4401
    if fails > WS_AUTH_FAIL_MAX:
        logger.warning(
            "websocket auth storm blocked path=%s client=%s fails=%s",
            scope.get("path"),
            _client_cache_key(scope),
            fails,
        )
        close_code = 4408

    # Daphne/nginx can turn ASGI HTTP denial responses into 502 for websocket
    # handshakes. Accept and immediately close so clients receive a websocket
    # auth close code while nginx sees a valid upstream response.
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.close", "code": close_code})



class JWTAuthMiddleware:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "websocket":
            return await self.inner(scope, receive, send)

        path = _normalize_ws_path(scope)
        if any(path.startswith(prefix) for prefix in JWT_SKIP_PATH_PREFIXES):
            return await self.inner(scope, receive, send)

        token = _extract_bearer_token(scope)
        scope["ws_auth_token_present"] = bool(token)

        if not token:
            await _reject_auth_handshake(scope, send, error="missing", token=token)
            return

        user_id, error = _validate_access_token(token)
        if error or not user_id:
            await _reject_auth_handshake(scope, send, error=error or "invalid", token=token)
            return

        user = await _get_user_by_id_cached(user_id)
        if isinstance(user, AnonymousUser):
            await _reject_auth_handshake(scope, send, error="user_not_found", user_id=user_id)
            return

        scope["user"] = user
        scope.pop("ws_auth_error", None)
        _clear_auth_failures(scope)
        logger.debug(
            "websocket auth ok path=%s user_id=%s",
            scope.get("path"),
            user.id,
        )
        return await self.inner(scope, receive, send)
