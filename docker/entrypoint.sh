#!/usr/bin/env bash
#
# Container entrypoint: wait for dependencies, migrate, then exec the command.
#
# Migrations run here rather than in a separate job because this is a demo
# deployment. In a real environment they belong in a release step that runs
# once — concurrent `migrate` calls across replicas are a known way to
# deadlock a deploy.

set -euo pipefail

echo "[entrypoint] waiting for postgres..."
python - <<'PY'
import os
import sys
import time

import psycopg

url = os.environ.get("DATABASE_URL", "")
deadline = time.time() + 60

while time.time() < deadline:
    try:
        with psycopg.connect(url, connect_timeout=3):
            print("[entrypoint] postgres is up")
            sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        print(f"[entrypoint] postgres not ready ({exc.__class__.__name__}), retrying")
        time.sleep(2)

print("[entrypoint] timed out waiting for postgres", file=sys.stderr)
sys.exit(1)
PY

echo "[entrypoint] applying migrations..."
python manage.py migrate --noinput

echo "[entrypoint] collecting static files..."
python manage.py collectstatic --noinput --clear >/dev/null

# Opt-in demo data. Never default this on: it truncates the operational
# tables, which would be catastrophic against a real database.
if [ "${SEED_DEMO_DATA:-false}" = "true" ]; then
    echo "[entrypoint] seeding demo data..."
    python manage.py seed_demo_data --flush
    python manage.py ingest_knowledge
fi

echo "[entrypoint] starting: $*"
exec "$@"
