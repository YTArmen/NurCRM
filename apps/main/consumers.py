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
