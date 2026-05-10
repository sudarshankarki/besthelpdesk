#!/bin/sh
set -e

is_true() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

wait_for_db() {
    echo "Waiting for PostgreSQL..."
    python - <<'PY'
import os
import time

import psycopg2

host = os.getenv("POSTGRES_HOST", "postgres")
port = int(os.getenv("POSTGRES_PORT", "5432"))
user = os.getenv("POSTGRES_USER", "postgres")
password = os.getenv("POSTGRES_PASSWORD", "")
dbname = os.getenv("POSTGRES_DB", "helpdeskdb")
timeout = int(os.getenv("WAIT_FOR_DB_TIMEOUT_SECONDS", "120"))
interval = int(os.getenv("WAIT_FOR_DB_INTERVAL_SECONDS", "2"))
attempts = max(1, timeout // max(interval, 1))

for _ in range(attempts):
    try:
        psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=dbname,
        ).close()
        print("PostgreSQL is ready")
        break
    except Exception:
        time.sleep(interval)
else:
    raise SystemExit("PostgreSQL not reachable after timeout")
PY
}

WAIT_FOR_DB="${WAIT_FOR_DB:-true}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-true}"
RUN_COLLECTSTATIC="${RUN_COLLECTSTATIC:-true}"
START_SERVER="${START_SERVER:-true}"
APP_MODULE="${APP_MODULE:-helpdesk.asgi:application}"
APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8000}"
UVICORN_WORKERS="${UVICORN_WORKERS:-1}"

if is_true "$WAIT_FOR_DB"; then
    wait_for_db
fi

if is_true "$RUN_MIGRATIONS"; then
    python manage.py migrate --noinput
fi

if is_true "$RUN_COLLECTSTATIC"; then
    python manage.py collectstatic --noinput
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

if ! is_true "$START_SERVER"; then
    exit 0
fi

exec uvicorn "$APP_MODULE" --host "$APP_HOST" --port "$APP_PORT" --workers "$UVICORN_WORKERS"
