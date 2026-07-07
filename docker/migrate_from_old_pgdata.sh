#!/bin/sh
set -eu

OLD_PGDATA="${OLD_PGDATA:-/home/cc/database/pg/18/docker}"
PG_IMAGE="${PG_IMAGE:-postgres:18-alpine}"
PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
WORK_DIR="${WORK_DIR:-/tmp/letpub_pg_migration}"
OLD_CONTAINER="${OLD_CONTAINER:-letpub-old-postgres-export}"
DUMP_FILE="${DUMP_FILE:-$WORK_DIR/letpub_old.dump}"
TARGET_SERVICE="${TARGET_SERVICE:-db}"
POSTGRES_DATA_DIR="${POSTGRES_DATA_DIR:-/home/cc/database/letpub_crawler_v2/postgres}"

if [ -f "$PROJECT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROJECT_DIR/.env"
  set +a
fi

POSTGRES_DB="${POSTGRES_DB:-${DB_NAME:-letpub_crawler_v2}}"
POSTGRES_USER="${POSTGRES_USER:-${DB_USER:-letpub}}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-${DB_PASSWORD:-letpub_password}}"
RESET_TARGET="${RESET_TARGET:-1}"

cleanup() {
  docker rm -f "$OLD_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

log() {
  printf '[migrate] %s\n' "$*"
}

if [ ! -d "$OLD_PGDATA" ]; then
  echo "OLD_PGDATA does not exist: $OLD_PGDATA" >&2
  exit 1
fi

mkdir -p "$WORK_DIR"
rm -rf "$WORK_DIR/old-pgdata"
mkdir -p "$POSTGRES_DATA_DIR"

log "copying old PostgreSQL data directory from $OLD_PGDATA"
docker run --rm --entrypoint sh \
  -v "$OLD_PGDATA:/old-pgdata:ro" \
  -v "$WORK_DIR:/work" \
  "$PG_IMAGE" \
  -c 'cp -a /old-pgdata /work/old-pgdata && chown -R postgres:postgres /work/old-pgdata'

log "starting temporary old PostgreSQL container"
docker run -d --name "$OLD_CONTAINER" \
  -e PGDATA=/var/lib/postgresql/18/docker \
  -v "$WORK_DIR/old-pgdata:/var/lib/postgresql/18/docker" \
  "$PG_IMAGE" >/dev/null

log "waiting for old PostgreSQL"
for _ in $(seq 1 60); do
  if docker exec "$OLD_CONTAINER" pg_isready -q; then
    break
  fi
  sleep 1
done
docker exec "$OLD_CONTAINER" pg_isready

OLD_USER="${SOURCE_USER:-}"
if [ -z "$OLD_USER" ]; then
  for candidate in "${DB_USER:-}" "${POSTGRES_USER:-}" postgres myuser; do
    [ -n "$candidate" ] || continue
    if docker exec "$OLD_CONTAINER" psql -U "$candidate" -d postgres -Atc 'select current_user' >/dev/null 2>&1; then
      OLD_USER="$candidate"
      break
    fi
  done
fi

if [ -z "$OLD_USER" ]; then
  echo "Could not determine a source PostgreSQL role. Set SOURCE_USER and retry." >&2
  exit 1
fi

DATABASES="$(docker exec "$OLD_CONTAINER" psql -U "$OLD_USER" -d postgres -Atc "select datname from pg_database where datistemplate = false order by datname")"
SOURCE_DB="${SOURCE_DB:-}"
if [ -z "$SOURCE_DB" ]; then
  for candidate in "$POSTGRES_DB" "${DB_NAME:-}" letpub_crawler2 letpub_crawler; do
    [ -n "$candidate" ] || continue
    if printf '%s\n' "$DATABASES" | grep -Fx "$candidate" >/dev/null 2>&1; then
      SOURCE_DB="$candidate"
      break
    fi
  done
fi
if [ -z "$SOURCE_DB" ]; then
  SOURCE_DB="$(printf '%s\n' "$DATABASES" | grep -Ev '^(postgres)$' | head -n 1)"
fi

if [ -z "$SOURCE_DB" ]; then
  echo "Could not determine a source database. Set SOURCE_DB and retry." >&2
  exit 1
fi

log "dumping source database $SOURCE_DB as role $OLD_USER"
docker exec "$OLD_CONTAINER" pg_dump \
  -U "$OLD_USER" \
  -d "$SOURCE_DB" \
  --format=custom \
  --no-owner \
  --no-acl > "$DUMP_FILE"

log "starting target compose database"
cd "$PROJECT_DIR"
POSTGRES_DB="$POSTGRES_DB" POSTGRES_USER="$POSTGRES_USER" POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
POSTGRES_DATA_DIR="$POSTGRES_DATA_DIR" \
  docker compose up -d "$TARGET_SERVICE"

log "waiting for target compose database"
for _ in $(seq 1 60); do
  if docker compose exec -T "$TARGET_SERVICE" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker compose exec -T "$TARGET_SERVICE" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"

RESTORE_ARGS="--no-owner --no-acl"
if [ "$RESET_TARGET" = "1" ] || [ "$RESET_TARGET" = "true" ]; then
  RESTORE_ARGS="--clean --if-exists $RESTORE_ARGS"
fi

log "restoring into compose database $POSTGRES_DB"
docker compose exec -T "$TARGET_SERVICE" sh -c \
  "pg_restore $RESTORE_ARGS -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\"" < "$DUMP_FILE"

log "migration complete"
docker compose exec -T "$TARGET_SERVICE" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c '\dt'
