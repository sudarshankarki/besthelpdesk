import os

from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse


def _env_true(name, default=True):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def healthz(_request):
    return JsonResponse({"ok": True})


def readyz(_request):
    try:
        connection = connections["default"]
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

        if _env_true("DJANGO_READINESS_CHECK_MIGRATIONS", default=True):
            executor = MigrationExecutor(connection)
            targets = executor.loader.graph.leaf_nodes()
            if executor.migration_plan(targets):
                return JsonResponse(
                    {"ok": False, "reason": "pending_migrations"},
                    status=503,
                )
    except Exception as exc:
        return JsonResponse(
            {"ok": False, "reason": "not_ready", "error": exc.__class__.__name__},
            status=503,
        )

    return JsonResponse({"ok": True})
