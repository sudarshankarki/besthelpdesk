from django.conf import settings
from django.utils import timezone


class ActiveUserSessionMiddleware:
    SESSION_KEY = "active_seen_ts"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            return response

        now_ts = int(timezone.now().timestamp())
        touch_interval = max(int(getattr(settings, "USER_ACTIVITY_TOUCH_INTERVAL_SECONDS", 60)), 1)
        last_seen_ts = request.session.get(self.SESSION_KEY)

        try:
            last_seen_ts = int(last_seen_ts)
        except (TypeError, ValueError):
            last_seen_ts = 0

        if now_ts - last_seen_ts >= touch_interval:
            request.session[self.SESSION_KEY] = now_ts

        return response
