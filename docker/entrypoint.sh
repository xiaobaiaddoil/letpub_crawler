#!/bin/sh
set -eu

export PATH="/app/.venv/bin:$PATH"

wait_for_database() {
  python - <<'PY'
import sys
import time
import psycopg2
from app.config import config

last_error = None
for _ in range(60):
    try:
        conn = psycopg2.connect(config.DATABASE_URL, connect_timeout=3)
        conn.close()
        sys.exit(0)
    except Exception as exc:
        last_error = exc
        time.sleep(2)

print(f"database is not ready: {last_error}", file=sys.stderr)
sys.exit(1)
PY
}

cmd="${1:-web}"
if [ "$#" -gt 0 ]; then
  shift
fi

case "$cmd" in
  web)
    wait_for_database
    exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" "$@"
    ;;
  worker)
    wait_for_database
    if [ -n "${WORKER_ID:-}" ]; then
      exec python worker.py --worker-id "$WORKER_ID" "$@"
    fi
    exec python worker.py "$@"
    ;;
  standalone)
    wait_for_database
    export RUN_MODE="${RUN_MODE:-standalone}"
    exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" "$@"
    ;;
  sh|bash|python|uvicorn)
    exec "$cmd" "$@"
    ;;
  *)
    exec "$cmd" "$@"
    ;;
esac
