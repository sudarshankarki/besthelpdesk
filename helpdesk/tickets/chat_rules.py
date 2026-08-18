from .models import RemoteAccessApproval


def _get_ticket_approval(ticket):
    state = getattr(ticket, "_state", None)
    fields_cache = getattr(state, "fields_cache", {}) if state is not None else {}
    if "remote_access_approval" in fields_cache:
        return fields_cache["remote_access_approval"]
    try:
        return ticket.remote_access_approval
    except (AttributeError, RemoteAccessApproval.DoesNotExist):
        return None


def is_ticket_chat_locked(ticket):
    ticket_status = getattr(ticket, "status", "")
    if ticket_status in {"closed", "resolved"}:
        return True
    if (getattr(ticket, "request_type", "") or "").startswith("cbs_access") and ticket_status == "cancelled_duplicate":
        return True
    approval = _get_ticket_approval(ticket)
    if (
        (getattr(ticket, "request_type", "") or "").startswith("cbs_access")
        and approval is not None
        and approval.status == RemoteAccessApproval.STATUS_REJECTED
    ):
        return True
    return False


def ticket_chat_locked_message(ticket):
    if is_ticket_chat_locked(ticket):
        approval = _get_ticket_approval(ticket)
        if (
            (getattr(ticket, "request_type", "") or "").startswith("cbs_access")
            and approval is not None
            and approval.status == RemoteAccessApproval.STATUS_REJECTED
        ):
            return "This conversation is closed because the CBS access request has been rejected."
        if (getattr(ticket, "request_type", "") or "").startswith("cbs_access") and getattr(ticket, "status", "") == "resolved":
            return "This conversation is closed because the CBS access request has been resolved."
        if (getattr(ticket, "request_type", "") or "").startswith("cbs_access") and getattr(ticket, "status", "") == "cancelled_duplicate":
            return "This conversation is closed because the CBS access request has been cancelled."
        if getattr(ticket, "status", "") == "resolved":
            return "This conversation is closed because the ticket has been resolved."
        return "Chat is disabled for closed tickets."
    return "Chat is unavailable for this ticket."
