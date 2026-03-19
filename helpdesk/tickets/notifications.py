from django.urls import reverse

from .models import get_ticket_chat_access_user_ids


def get_primary_ticket_participant_ids(ticket, actor_user_id):
    targets = []
    for user_id in (ticket.created_by_id, ticket.assigned_to_id):
        if not user_id or user_id == actor_user_id or user_id in targets:
            continue
        targets.append(user_id)
    return targets


def get_call_notification_target_ids(ticket, caller_user_id):
    return get_ticket_chat_access_user_ids(ticket, caller_user_id)


def get_chat_notification_target_ids(ticket, sender_user_id):
    return get_ticket_chat_access_user_ids(ticket, sender_user_id)


def build_call_notification_payload(ticket, caller):
    caller_name = caller.get_full_name().strip() or caller.username
    return {
        "kind": "incoming_call",
        "level": "warning",
        "title": "Incoming audio call",
        "message": f"{caller_name} is calling on ticket {ticket.ticket_id}: {ticket.subject}",
        "url": reverse("ticket_detail", args=[ticket.id]),
        "ticket_id": ticket.id,
        "ticket_code": ticket.ticket_id,
        "caller": caller.username,
        "delay": 20000,
    }


def build_chat_notification_payload(ticket, sender, body):
    sender_name = sender.get_full_name().strip() or sender.username
    message_preview = " ".join((body or "").split())
    if len(message_preview) > 100:
        message_preview = f"{message_preview[:97].rstrip()}..."

    message = f"{sender_name} sent a chat message on ticket {ticket.ticket_id}"
    if message_preview:
        message = f"{message}: {message_preview}"

    return {
        "kind": "chat_message",
        "level": "info",
        "title": "New chat message",
        "message": message,
        "url": reverse("ticket_detail", args=[ticket.id]),
        "ticket_id": ticket.id,
        "ticket_code": ticket.ticket_id,
        "sender": sender.username,
        "delay": 8000,
    }
