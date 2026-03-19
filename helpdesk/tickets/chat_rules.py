def is_ticket_chat_locked(ticket):
    return getattr(ticket, "status", "") == "closed"


def ticket_chat_locked_message(ticket):
    if is_ticket_chat_locked(ticket):
        return "Chat is disabled for closed tickets."
    return "Chat is unavailable for this ticket."
