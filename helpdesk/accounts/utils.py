from django.contrib.sessions.models import Session
from django.conf import settings
from django.utils import timezone


def get_outgoing_from_email():
    try:
        from .models import EmailSettings

        cfg = EmailSettings.objects.first()
        from_email = (getattr(cfg, "from_email", "") or "").strip()
        if from_email:
            return from_email
    except Exception:
        pass

    return (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip()


def logout_user_from_all_sessions(user):
    """
    Invalidate every active DB-backed session for a user.
    """
    user_id = str(user.pk)
    active_sessions = Session.objects.filter(expire_date__gte=timezone.now())

    for session in active_sessions.iterator():
        data = session.get_decoded()
        if data.get("_auth_user_id") == user_id:
            session.delete()
