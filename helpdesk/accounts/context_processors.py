from .auth_mode import (
    is_ad_login_enabled,
    is_agent_workload_view_enabled,
    is_local_account_self_service_enabled,
    is_local_login_enabled,
)


def auth_mode_flags(request):
    directory_auth_enabled = is_ad_login_enabled()
    local_login_enabled = is_local_login_enabled()
    return {
        "directory_auth_enabled": directory_auth_enabled,
        "local_login_enabled": local_login_enabled,
        "ad_only_login": bool(directory_auth_enabled and not local_login_enabled),
        "local_account_self_service_enabled": is_local_account_self_service_enabled(),
        "agent_workload_view_enabled": is_agent_workload_view_enabled(),
    }
