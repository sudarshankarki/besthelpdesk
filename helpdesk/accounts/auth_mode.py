from __future__ import annotations

from django.conf import settings
from django.db.utils import OperationalError, ProgrammingError


def _get_authentication_settings():
    try:
        from .models import AuthenticationSettings

        return AuthenticationSettings.objects.first()
    except (OperationalError, ProgrammingError):
        return None


def recovery_superuser_names() -> set[str]:
    return {
        (value or "").strip().casefold()
        for value in getattr(settings, "LOCAL_RECOVERY_SUPERUSERS", [])
        if (value or "").strip()
    }


def default_local_login_enabled() -> bool:
    return not getattr(settings, "AD_ONLY_LOGIN", False)


def is_ad_login_enabled() -> bool:
    if not getattr(settings, "AD_AUTH_ENABLED", False):
        return False

    auth_settings = _get_authentication_settings()
    if auth_settings is None:
        return True
    return auth_settings.ad_login_enabled


def is_local_login_enabled() -> bool:
    auth_settings = _get_authentication_settings()
    if auth_settings is None:
        return default_local_login_enabled()
    return auth_settings.local_login_enabled


def is_local_account_self_service_enabled() -> bool:
    auth_settings = _get_authentication_settings()
    base_enabled = (
        auth_settings.local_account_self_service_enabled
        if auth_settings is not None
        else getattr(settings, "LOCAL_ACCOUNT_SELF_SERVICE_ENABLED", True)
    )
    return is_local_login_enabled() and base_enabled


def is_agent_workload_view_enabled() -> bool:
    auth_settings = _get_authentication_settings()
    if auth_settings is None:
        return True
    return auth_settings.agent_workload_view_enabled
