from .models import RemoteAccessApproval


def is_ticket_chat_locked(ticket):
    ticket_status = getattr(ticket, "status", "")
    if ticket_status in {"closed", "resolved"}:
        return True
    if (getattr(ticket, "request_type", "") or "").startswith("cbs_access") and ticket_status == "cancelled_duplicate":
        return True
    return False


def ticket_chat_locked_message(ticket):
    if is_ticket_chat_locked(ticket):
        if (getattr(ticket, "request_type", "") or "").startswith("cbs_access") and getattr(ticket, "status", "") == "resolved":
            return "This conversation is closed because the CBS access request has been resolved."
        if (getattr(ticket, "request_type", "") or "").startswith("cbs_access") and getattr(ticket, "status", "") == "cancelled_duplicate":
            return "This conversation is closed because the CBS access request has been cancelled."
        if getattr(ticket, "status", "") == "resolved":
            return "This conversation is closed because the ticket has been resolved."
        return "Chat is disabled for closed tickets."
    return "Chat is unavailable for this ticket."
