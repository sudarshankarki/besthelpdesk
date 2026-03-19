#!/bin/sh
set -e

echo "Waiting for PostgreSQL..."
python - <<'PY'
import os
import time
import psycopg2

host = os.getenv('POSTGRES_HOST', 'postgres')
port = int(os.getenv('POSTGRES_PORT', '5432'))
user = os.getenv('POSTGRES_USER', 'postgres')
password = os.getenv('POSTGRES_PASSWORD', '')
dbname = os.getenv('POSTGRES_DB', 'helpdeskdb')

for _ in range(60):
    try:
        psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname).close()
        print('PostgreSQL is ready')
        break
    except Exception:
        time.sleep(2)
else:
    raise SystemExit('PostgreSQL not reachable after timeout')
PY

python manage.py migrate --noinput
python manage.py collectstatic --noinput

exec uvicorn helpdesk.asgi:application --host 0.0.0.0 --port 8000
