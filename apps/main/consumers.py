"""
WebSocket-консьюмер уведомлений (doc 08).

URL: ``ws/notifications/?token=<JWT>`` (JWT кладёт user в scope, см. core/ws_jwt.py).

Подключение подписывается на минимально необходимый набор групп:
  * notif_user_<user_id>     — личные уведомления (основной канал доставки);
  * notif_company_<id>       — общая компания;
  * notif_branch_<id>        — филиал (если есть);
  * notif_role_<role>        — роль (если есть);
  * notif_agent_<id>         — если пользователь-агент.
"""
import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from core.ws_consumer_utils import (
    is_anonymous_scope_user,
    reject_websocket_unauthorized,
    reject_websocket_forbidden,
)
from apps.cafe.consumers import resolve_user_company_and_branch
from apps.main.realtime import (
    user_group_name,
    company_group_name,
    branch_group_name,
    role_group_name,
    agent_group_name,
)

logger = logging.getLogger("nurcrm.websocket.notifications")


class NotificationsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        if is_anonymous_scope_user(self.scope):
            await reject_websocket_unauthorized(self)  # close 4401 → фронт обновит токен
            return

        user = self.scope["user"]
        company, branch = await self._get_company_and_branch(user)
        if not company:
            await reject_websocket_forbidden(self, reason="no_company")
            return

        self.user_id = str(user.id)
        self.company_id = str(company.id)
        self.branch_id = str(branch.id) if branch else None
        role = getattr(user, "role", None)

        self.groups_subscribed = [
            user_group_name(self.user_id),
            company_group_name(self.company_id),
        ]
        if self.branch_id:
            self.groups_subscribed.append(branch_group_name(self.branch_id))
        if role:
            self.groups_subscribed.append(role_group_name(role))
        if role == "agent":
            self.groups_subscribed.append(agent_group_name(self.user_id))

        for group in self.groups_subscribed:
            await self.channel_layer.group_add(group, self.channel_name)
        await self.accept()

        await self.send(json.dumps({
            "type": "connection_established",
            "user_id": self.user_id,
        }))
        # Сразу отдаём бейдж, чтобы колокольчик был верным до первого REST-запроса.
        await self.send(json.dumps({
            "type": "unread_count",
            "count": await self._unread_count(),
        }))

    async def disconnect(self, code):
        for group in getattr(self, "groups_subscribed", []):
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except Exception:
            return
        action = data.get("action")
        if action == "ping":
            await self.send(json.dumps({"type": "pong"}))
        elif action == "unread_count":
            await self.send(json.dumps({
                "type": "unread_count",
                "count": await self._unread_count(),
            }))

    # ---- доставка уведомления (group_send type="notify") ----
    async def notify(self, event):
        await self.send(json.dumps({
            "type": "notification",
            "data": event.get("data") or {},
        }))

    # ---- бейдж непрочитанных (group_send type="unread") ----
    async def unread(self, event):
        await self.send(json.dumps({
            "type": "unread_count",
            "count": int(event.get("count") or 0),
        }))

    # ---- служебные market-события кассы/смены (group_send type="market.notification") ----
    async def market_notification(self, event):
        """POS шлёт в notif_company_<id> сырые события (cashflow/shift) для live-обновления
        экранов кассы. Без этого хендлера Channels роняет соединение на неизвестном типе."""
        await self.send(json.dumps({
            "type": event.get("event") or "market.event",
            "data": event.get("data") or {},
        }))

    @database_sync_to_async
    def _get_company_and_branch(self, user):
        return resolve_user_company_and_branch(user)

    @database_sync_to_async
    def _unread_count(self):
        from apps.main.models import Notification

        return Notification.objects.filter(user_id=self.user_id, is_read=False).count()


class CompanyChangesConsumer(AsyncWebsocketConsumer):
    """
    ТЗ Н-31: ws/company/ — живая лента изменений компании.

    Токен приходит ПЕРВЫМ сообщением: {"token": "<JWT>"} (в URL его не кладём, чтобы не
    попадал в логи). Без токена за 10 секунд соединение закрывается (код 4401).
    Дальше сервер шлёт {"type": "change", "event": {id,type,at,branch,actor,entity,entity_id,data}}.
    Клиент после переподключения догоняет пропущенное через GET main/changes/?since=.
    """

    AUTH_TIMEOUT = 10

    async def connect(self):
        self.group = None
        self.authed = False
        await self.accept()
        import asyncio
        self._auth_task = asyncio.ensure_future(self._auth_deadline())

    async def _auth_deadline(self):
        import asyncio
        await asyncio.sleep(self.AUTH_TIMEOUT)
        if not self.authed:
            await self.close(code=4401)

    async def disconnect(self, code):
        task = getattr(self, "_auth_task", None)
        if task:
            task.cancel()
        if getattr(self, "group", None):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        if not self.authed:
            token = str(data.get("token") or "").strip()
            if not token:
                await self.close(code=4401)
                return
            await self._authenticate(token)
            return
        if data.get("action") == "ping":
            await self.send(json.dumps({"type": "pong"}))

    async def _authenticate(self, token):
        from core.ws_jwt import _get_user_by_id_cached, _validate_access_token
        from django.contrib.auth.models import AnonymousUser
        from apps.main.change_feed import company_ws_group

        user_id, error = _validate_access_token(token)
        if error or not user_id:
            await self.close(code=4401)
            return
        user = await _get_user_by_id_cached(user_id)
        if isinstance(user, AnonymousUser):
            await self.close(code=4401)
            return
        company, _branch = await database_sync_to_async(resolve_user_company_and_branch)(user)
        if not company:
            await self.close(code=4403)
            return
        self.authed = True
        self.group = company_ws_group(company.id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.send(json.dumps({"type": "connection_established", "company": str(company.id)}))

    async def change_event(self, event):
        """group_send type="change.event" из apps.main.change_feed.record_change."""
        if self.authed:
            await self.send(json.dumps({"type": "change", "event": event.get("event") or {}}))


class MainEventsConsumer(AsyncWebsocketConsumer):
    """
    ТЗ 29.10 Б-102: ws/main/events/?branch=<id>&since=<event_id> — поток событий.

    Авторизация как у ws/company/: токен первым сообщением {"token": "<JWT>"}
    (или ?token= / Authorization — тогда сразу). Повторное {"token": ...} продлевает сессию.
    Сообщения: {"id","type","at","branch","ids","actor", ...поля типа}; тип неизвестный клиент игнорирует.
    Пропущенное при since=<id> берётся из ChangeEvent; слишком много/старое — {"type":"resync"}.
    Heartbeat {"type":"ping"} каждые 20 с; когда токен истёк — закрытие с кодом 4401.
    """

    AUTH_TIMEOUT = 10
    PING_EVERY = 20
    REPLAY_LIMIT = 500

    async def connect(self):
        import asyncio

        self.group = None
        self.authed = False
        self.token_exp = None
        self.role = None
        self.user_id = None
        self.branch_filter = None
        self.last_id = 0
        self._tasks = []
        from urllib.parse import parse_qs

        self.qs = parse_qs((self.scope.get("query_string") or b"").decode())
        await self.accept()
        self._tasks.append(asyncio.ensure_future(self._auth_deadline()))
        token = (self.qs.get("token") or [""])[0].strip()
        if not token:
            for name, value in self.scope.get("headers", []):
                if name.lower() == b"authorization" and value.decode().lower().startswith("bearer "):
                    token = value.decode().split(" ", 1)[1].strip()
        if token:
            await self._authenticate(token)

    async def _auth_deadline(self):
        import asyncio

        await asyncio.sleep(self.AUTH_TIMEOUT)
        if not self.authed:
            await self.close(code=4401)

    async def _heartbeat(self):
        import asyncio
        import time

        while True:
            await asyncio.sleep(self.PING_EVERY)
            if self.token_exp and time.time() >= self.token_exp:
                await self.close(code=4401)
                return
            await self.send(json.dumps({"type": "ping"}))

    async def disconnect(self, code):
        for t in getattr(self, "_tasks", []):
            t.cancel()
        if getattr(self, "group", None):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        token = str(data.get("token") or "").strip()
        if token:
            await self._authenticate(token)
        elif not self.authed:
            await self.close(code=4401)
        elif data.get("action") == "ping" or data.get("type") == "ping":
            await self.send(json.dumps({"type": "pong"}))

    @staticmethod
    def _token_exp(token):
        from rest_framework_simplejwt.tokens import AccessToken

        try:
            return float(AccessToken(token).get("exp") or 0) or None
        except Exception:
            return None

    async def _authenticate(self, token):
        import asyncio

        from django.contrib.auth.models import AnonymousUser

        from apps.main.change_feed import company_ws_group
        from core.ws_jwt import _get_user_by_id_cached, _validate_access_token

        user_id, error = _validate_access_token(token)
        if error or not user_id:
            await self.close(code=4401)
            return
        user = await _get_user_by_id_cached(user_id)
        if isinstance(user, AnonymousUser):
            await self.close(code=4401)
            return
        if self.authed:
            if str(user.id) != str(self.user_id):
                await self.close(code=4401)
                return
            self.token_exp = self._token_exp(token)  # продление сессии новым токеном
            return
        company, branch = await database_sync_to_async(resolve_user_company_and_branch)(user)
        if not company:
            await self.close(code=4403)
            return
        self.role = getattr(user, "role", None)
        self.user_id = str(user.id)
        self.company_id = company.id
        want = (self.qs.get("branch") or [""])[0].strip()
        if self.role in ("owner", "admin") or branch is None:
            self.branch_filter = want or None
        else:
            self.branch_filter = str(branch.id)  # сотрудник привязан к филиалу — чужой не отдаём
        self.token_exp = self._token_exp(token)
        self.authed = True
        self.group = company_ws_group(company.id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.send(json.dumps({"type": "connection_established", "company": str(company.id)}))
        self._tasks.append(asyncio.ensure_future(self._heartbeat()))
        since = (self.qs.get("since") or [""])[0].strip()
        if since:
            await self._replay(since)

    def _replay_sync(self, since):
        from apps.main.change_feed import RETENTION_DAYS, serialize_event
        from apps.main.events_stream import allowed_for, to_stream_event
        from apps.main.models import ChangeEvent
        from django.db.models import Q
        from django.utils import timezone

        try:
            since_id = int(since)
        except ValueError:
            return None
        qs = ChangeEvent.objects.filter(company_id=self.company_id)
        old = qs.filter(id=since_id).values_list("created_at", flat=True).first()
        if old is not None and (timezone.now() - old).days >= RETENTION_DAYS:
            return None
        if self.branch_filter:
            qs = qs.filter(Q(branch_id=self.branch_filter) | Q(branch__isnull=True))
        rows = list(qs.filter(id__gt=since_id).order_by("id")[: self.REPLAY_LIMIT + 1])
        if len(rows) > self.REPLAY_LIMIT:
            return None
        out = []
        for ev in rows:
            msg = to_stream_event(serialize_event(ev))
            if allowed_for(msg, role=self.role, user_id=self.user_id, branch_id=self.branch_filter):
                out.append(msg)
        self._replay_last = rows[-1].id if rows else since_id
        return out

    async def _replay(self, since):
        # события, пришедшие в группу во время догона, отфильтруем по last_id (без дублей)
        res = await database_sync_to_async(self._replay_sync)(since)
        if res is None:
            await self.send(json.dumps({"type": "resync"}))
            return
        for msg in res:
            await self.send(json.dumps(msg, ensure_ascii=False))
        self.last_id = max(self.last_id, getattr(self, "_replay_last", 0))

    async def change_event(self, event):
        """group_send type="change.event" из apps.main.change_feed.record_change."""
        if not self.authed:
            return
        from apps.main.events_stream import allowed_for, to_stream_event

        ev = event.get("event") or {}
        if ev.get("id") and ev["id"] <= self.last_id:
            return
        msg = to_stream_event(ev)
        if allowed_for(msg, role=self.role, user_id=self.user_id, branch_id=self.branch_filter):
            if ev.get("id"):
                self.last_id = max(self.last_id, ev["id"])
            await self.send(json.dumps(msg, ensure_ascii=False))
