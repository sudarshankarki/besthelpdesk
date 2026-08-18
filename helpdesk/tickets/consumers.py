import json
import logging

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model
from django.utils import timezone

from .chat_rules import is_ticket_chat_locked, ticket_chat_locked_message
from .models import Ticket, TicketMessage, can_access_ticket_chat, get_ticket_chat_access_user_ids
from .notifications import (
    build_call_notification_payload,
    build_chat_notification_payload,
    get_call_notification_target_ids,
    get_chat_notification_target_ids,
)

logger = logging.getLogger(__name__)


class TicketChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.ticket_id = self.scope["url_route"]["kwargs"]["ticket_id"]
        self.group_name = f"ticket_chat_{self.ticket_id}"
        user = self.scope["user"]

        if not user.is_authenticated:
            logger.warning("WS rejected unauthenticated user for ticket %s", self.ticket_id)
            await self.close(code=4001)
            return

        allowed = await self._can_access_ticket(user, self.ticket_id)
        if not allowed:
            logger.warning("WS rejected user %s for ticket %s", user.username, self.ticket_id)
            await self.close(code=4003)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        logger.info("WS connected user %s to ticket %s", user.username, self.ticket_id)

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        logger.info("WS disconnected ticket %s code=%s", self.ticket_id, close_code)

    async def receive(self, text_data):
        payload = json.loads(text_data)
        body = payload.get("body", "").strip()
        if not body:
            return

        allowed = await self._can_access_ticket(self.scope["user"], self.ticket_id)
        if not allowed:
            await self.close(code=4003)
            return

        chat_locked_error = await self._get_chat_locked_error(self.ticket_id)
        if chat_locked_error:
            await self.send(text_data=json.dumps({"type": "error", "error": chat_locked_error}))
            return

        user = self.scope["user"]
        message = await self._create_message(self.ticket_id, user.id, body)

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat_message",
                "id": message["id"],
                "body": message["body"],
                "author": message["author"],
                "author_id": message["author_id"],
                "created_at": message["created_at"],
            },
        )

        target_ids, notify_payload = await self._get_chat_notification_data(user.id, message["body"])
        for target_id in target_ids:
            await self.channel_layer.group_send(
                f"user_notify_{target_id}",
                {"type": "notify", "payload": notify_payload},
            )

    async def chat_message(self, event):
        allowed = await self._can_access_ticket(self.scope["user"], self.ticket_id)
        if not allowed:
            await self.close(code=4003)
            return

        payload = {
            "id": event["id"],
            "body": event["body"],
            "author": event["author"],
            "author_id": event.get("author_id"),
            "created_at": event["created_at"],
        }
        if event.get("attachment"):
            payload["attachment"] = event["attachment"]

        await self.send(text_data=json.dumps(payload))

    async def chat_message_deleted(self, event):
        allowed = await self._can_access_ticket(self.scope["user"], self.ticket_id)
        if not allowed:
            await self.close(code=4003)
            return

        await self.send(text_data=json.dumps({"type": "deleted", "id": event["id"]}))

    @sync_to_async
    def _can_access_ticket(self, user, ticket_id):
        try:
            ticket = Ticket.objects.get(id=ticket_id)
        except Ticket.DoesNotExist:
            return False
        return can_access_ticket_chat(user, ticket)

    @sync_to_async
    def _create_message(self, ticket_id, user_id, body):
        message = TicketMessage.objects.create(
            ticket_id=ticket_id,
            author_id=user_id,
            body=body,
        )
        created_local = timezone.localtime(message.created_at)
        return {
            "id": message.id,
            "body": message.body,
            "author": message.author.username,
            "author_id": message.author_id,
            "created_at": created_local.strftime("%Y-%m-%d %H:%M"),
        }

    @sync_to_async
    def _get_chat_notification_data(self, sender_user_id, body):
        ticket = Ticket.objects.select_related("created_by", "assigned_to").get(id=self.ticket_id)
        sender = get_user_model().objects.filter(id=sender_user_id).first() or ticket.created_by
        return (
            get_chat_notification_target_ids(ticket, sender_user_id),
            build_chat_notification_payload(ticket, sender, body),
        )

    @sync_to_async
    def _get_chat_locked_error(self, ticket_id):
        ticket = Ticket.objects.select_related("remote_access_approval").filter(id=ticket_id).first()
        if not ticket or not is_ticket_chat_locked(ticket):
            return ""
        return ticket_chat_locked_message(ticket)


class TicketCallConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.ticket_id = self.scope["url_route"]["kwargs"]["ticket_id"]
        self.group_name = f"ticket_call_{self.ticket_id}"
        self.active_peer_user_id = None
        user = self.scope["user"]

        if not user.is_authenticated:
            logger.warning("Call WS rejected unauthenticated user for ticket %s", self.ticket_id)
            await self.close(code=4001)
            return

        allowed = await self._can_access_ticket(user, self.ticket_id)
        if not allowed:
            logger.warning("Call WS rejected user %s for ticket %s", user.username, self.ticket_id)
            await self.close(code=4003)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        await self.send(
            text_data=json.dumps(
                {
                    "type": "hello",
                    "sender": self.channel_name,
                    "user": user.username,
                }
            )
        )

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "call_event",
                "event": "joined",
                "sender": self.channel_name,
                "user": user.username,
            },
        )

        logger.info("Call WS connected user %s to ticket %s", user.username, self.ticket_id)

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        active_peer_user_id = getattr(self, "active_peer_user_id", None)
        if active_peer_user_id:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "call_event",
                    "event": "left",
                    "sender": self.channel_name,
                    "user_id": getattr(self.scope["user"], "id", None),
                    "target_user_id": active_peer_user_id,
                },
            )
        logger.info("Call WS disconnected ticket %s code=%s", self.ticket_id, close_code)

    async def receive(self, text_data):
        user = self.scope["user"]
        try:
            payload = json.loads(text_data)
        except Exception:
            return

        allowed = await self._can_access_ticket(user, self.ticket_id)
        if not allowed:
            await self.close(code=4003)
            return

        chat_locked_error = await self._get_chat_locked_error(self.ticket_id)
        if chat_locked_error:
            await self.send(text_data=json.dumps({"type": "error", "error": chat_locked_error}))
            return

        msg_type = (payload.get("type") or "").strip()
        if msg_type not in {"ring", "ready", "offer", "answer", "ice", "hangup"}:
            return

        target_user_id = self._clean_user_id(payload.get("target_user_id"))
        if msg_type == "ring":
            if target_user_id is None:
                await self.send(text_data=json.dumps({"type": "error", "error": "Select one eligible call recipient."}))
                return
            target_ids, notify_payload = await self._get_call_notification_data(user.id, target_user_id)
            if not target_ids:
                await self.send(text_data=json.dumps({"type": "error", "error": "Select one eligible call recipient."}))
                return
            self.active_peer_user_id = target_ids[0]
            for target_id in target_ids:
                await self.channel_layer.group_send(
                    f"user_notify_{target_id}",
                    {"type": "notify", "payload": notify_payload},
                )
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "call_event",
                    "event": "ring",
                    "sender": self.channel_name,
                    "user": user.username,
                    "user_id": user.id,
                    "target_user_id": self.active_peer_user_id,
                },
            )
            return

        if target_user_id is None:
            target_user_id = self.active_peer_user_id
        if not await self._is_valid_call_peer(user.id, target_user_id):
            await self.send(text_data=json.dumps({"type": "error", "error": "Select one eligible call recipient."}))
            return
        self.active_peer_user_id = target_user_id

        event = {
            "type": "call_event",
            "event": msg_type,
            "sender": self.channel_name,
            "user": user.username,
            "user_id": user.id,
            "target_user_id": target_user_id,
        }
        if msg_type in {"offer", "answer"}:
            sdp = payload.get("sdp")
            if not isinstance(sdp, dict):
                return
            event["sdp"] = sdp
        if msg_type == "ice":
            candidate = payload.get("candidate")
            if not isinstance(candidate, dict):
                return
            event["candidate"] = candidate

        await self.channel_layer.group_send(self.group_name, event)

    async def call_event(self, event):
        allowed = await self._can_access_ticket(self.scope["user"], self.ticket_id)
        if not allowed:
            await self.close(code=4003)
            return

        current_user_id = getattr(self.scope["user"], "id", None)
        event_user_id = event.get("user_id")
        target_user_id = event.get("target_user_id")
        if target_user_id and event_user_id:
            if current_user_id not in {event_user_id, target_user_id}:
                return
            if current_user_id == target_user_id:
                self.active_peer_user_id = event_user_id
            elif current_user_id == event_user_id:
                self.active_peer_user_id = target_user_id

        await self.send(
            text_data=json.dumps(
                {
                    "type": "event",
                    "event": event.get("event"),
                    "sender": event.get("sender"),
                    "user": event.get("user"),
                    "user_id": event.get("user_id"),
                    "target_user_id": event.get("target_user_id"),
                    "sdp": event.get("sdp"),
                    "candidate": event.get("candidate"),
                }
            )
        )

    def _clean_user_id(self, value):
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            return None
        return user_id if user_id > 0 else None

    @sync_to_async
    def _can_access_ticket(self, user, ticket_id):
        try:
            ticket = Ticket.objects.get(id=ticket_id)
        except Ticket.DoesNotExist:
            return False
        return can_access_ticket_chat(user, ticket)

    @sync_to_async
    def _get_call_notification_data(self, caller_user_id, target_user_id=None):
        ticket = Ticket.objects.select_related("created_by", "assigned_to").get(id=self.ticket_id)
        caller = get_user_model().objects.filter(id=caller_user_id).first() or ticket.created_by
        allowed_target_ids = get_call_notification_target_ids(ticket, caller_user_id)
        target_ids = [target_user_id] if target_user_id in allowed_target_ids else []
        return (
            target_ids,
            build_call_notification_payload(ticket, caller, target_ids[0] if len(target_ids) == 1 else None),
        )

    @sync_to_async
    def _is_valid_call_peer(self, caller_user_id, target_user_id):
        if not target_user_id or target_user_id == caller_user_id:
            return False
        ticket = Ticket.objects.get(id=self.ticket_id)
        target_user = get_user_model().objects.filter(id=target_user_id).first()
        return bool(target_user and can_access_ticket_chat(target_user, ticket))

    @sync_to_async
    def _get_chat_locked_error(self, ticket_id):
        ticket = Ticket.objects.select_related("remote_access_approval").filter(id=ticket_id).first()
        if not ticket or not is_ticket_chat_locked(ticket):
            return ""
        return ticket_chat_locked_message(ticket)


class NotificationsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close(code=4001)
            return

        self.group_name = f"user_notify_{user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        logger.info("WS notifications connected user %s", user.username)

    async def disconnect(self, close_code):
        group_name = getattr(self, "group_name", None)
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)
        logger.info("WS notifications disconnected code=%s", close_code)

    async def notify(self, event):
        payload = event.get("payload") or {}
        await self.send(text_data=json.dumps(payload))
