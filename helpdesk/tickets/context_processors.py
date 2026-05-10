from django.urls import reverse
from django.utils import timezone

from .models import PortalFlashAnnouncement


LOGIN_FLASH_SESSION_KEY = "show_login_flash_announcements"


def login_flash_announcements(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}

    now = timezone.now()
    active_announcements = list(
        PortalFlashAnnouncement.objects.filter(
            starts_at__lte=now,
            ends_at__gte=now,
        ).order_by("-starts_at", "-created_at")[:8]
    )
    should_show = request.session.get(LOGIN_FLASH_SESSION_KEY, True)
    login_items = active_announcements[:1] if should_show else []
    request.session[LOGIN_FLASH_SESSION_KEY] = False

    news_items = []
    for announcement in active_announcements:
        news_items.append(
            {
                "id": announcement.id,
                "category": announcement.category,
                "category_display": announcement.get_category_display(),
                "title": announcement.title or f"Portal flash #{announcement.id}",
                "message": announcement.message or "",
                "image_url": reverse("portal_flash_image_view", args=[announcement.id]),
                "starts_at": timezone.localtime(announcement.starts_at).strftime("%b %d, %Y %H:%M"),
                "ends_at": timezone.localtime(announcement.ends_at).strftime("%b %d, %Y %H:%M"),
            }
        )

    payload = []
    for announcement in login_items:
        uploaded_by = getattr(announcement, "uploaded_by", None)
        uploader_name = "-"
        if uploaded_by:
            uploader_name = uploaded_by.get_full_name() or uploaded_by.get_username()

        payload.append(
            {
                "id": announcement.id,
                "title": announcement.title or f"Portal flash #{announcement.id}",
                "category_display": announcement.get_category_display(),
                "message": announcement.message or "",
                "image_url": reverse("portal_flash_image_view", args=[announcement.id]),
                "starts_at": timezone.localtime(announcement.starts_at).strftime("%b %d, %Y %H:%M"),
                "ends_at": timezone.localtime(announcement.ends_at).strftime("%b %d, %Y %H:%M"),
                "uploaded_by": uploader_name,
            }
        )

    return {
        "login_flash_announcements": payload,
        "portal_news_announcements": news_items,
    }
