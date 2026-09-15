#!/usr/bin/env bash
# Prepares the dashboard before handing over to the real command.
#
# Only the web service needs this; `docker compose run scraper ...` passes a
# python command straight through and skips it, because a scraper must not
# wait on Postgres or run migrations.
set -euo pipefail

if [[ "${1:-}" == "gunicorn" || "${RUN_MIGRATIONS:-0}" == "1" ]]; then
    cd /app/webapp

    if [[ "${DB_ENGINE:-sqlite}" == post* ]]; then
        echo "==> waiting for postgres at ${DB_HOST:-db}:${DB_PORT:-5432}"
        for _ in $(seq 1 60); do
            python - <<'PY' && break
import os, socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect((os.getenv("DB_HOST", "db"), int(os.getenv("DB_PORT", "5432"))))
except OSError:
    sys.exit(1)
PY
            sleep 1
        done
    fi

    echo "==> migrate"
    python manage.py migrate --no-input
    echo "==> roster"
    python manage.py seed_processes
    python manage.py seed_workflow
    echo "==> static"
    python manage.py collectstatic --no-input >/dev/null
fi

exec "$@"
