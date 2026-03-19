from django.urls import re_path

from .consumers import NotificationsConsumer, TicketCallConsumer, TicketChatConsumer

websocket_urlpatterns = [
    re_path(r"^ws/tickets/(?P<ticket_id>\d+)/?$", TicketChatConsumer.as_asgi()),
    re_path(r"^ws/tickets/(?P<ticket_id>\d+)/call/?$", TicketCallConsumer.as_asgi()),
    re_path(r"^ws/notifications/?$", NotificationsConsumer.as_asgi()),
]
