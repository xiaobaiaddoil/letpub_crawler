#!/bin/sh
set -eu

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_DIR"

BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
BACKUP_PREFIX="${BACKUP_PREFIX:-letpub_crawler}"
BACKUP_TIMESTAMP="${BACKUP_TIMESTAMP:-$(date +%Y%m%d%H%M%S)}"

mkdir -p "$BACKUP_DIR"

DUMP_FILE="$BACKUP_DIR/${BACKUP_PREFIX}_${BACKUP_TIMESTAMP}.dump"
LIST_FILE="$DUMP_FILE.list"
META_FILE="$DUMP_FILE.meta"
SHA_FILE="$DUMP_FILE.sha256"

docker compose up -d db >/dev/null

docker compose exec -T db sh -lc '
i=0
until pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "database is not ready" >&2
    exit 1
  fi
  sleep 2
done
'

docker compose exec -T db sh -lc '
pg_dump \
  --format=custom \
  --compress=9 \
  --no-owner \
  --no-acl \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB"
' > "$DUMP_FILE"

docker compose exec -T db pg_restore -l < "$DUMP_FILE" > "$LIST_FILE"

{
  printf 'created_at=%s\n' "$(date -Iseconds)"
  printf 'dump_file=%s\n' "$DUMP_FILE"
  printf 'dump_bytes=%s\n' "$(wc -c < "$DUMP_FILE" | tr -d ' ')"
  docker compose exec -T db sh -lc 'printf "postgres_db=%s\npostgres_user=%s\n" "$POSTGRES_DB" "$POSTGRES_USER"'
  docker compose exec -T db sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
select 'categories=' || count(*) from categories;
select 'journals=' || count(*) from journals;
select 'comments=' || count(*) from comments;
select 'crawl_tasks=' || count(*) from crawl_tasks;
select 'workers=' || count(*) from workers;
select 'cookie_pool=' || count(*) from cookie_pool;
select 'accounts=' || count(*) from accounts;
select 'proxy_pool=' || count(*) from proxy_pool;
SQL
} > "$META_FILE"

sha256sum "$DUMP_FILE" > "$SHA_FILE"

printf 'database backup: %s\n' "$DUMP_FILE"
printf 'restore list: %s\n' "$LIST_FILE"
printf 'metadata: %s\n' "$META_FILE"
printf 'sha256: %s\n' "$SHA_FILE"
printf '\nrestore example:\n'
printf '  docker compose exec -T db sh -lc '"'"'pg_restore --clean --if-exists --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"'"'"' < %s\n' "$DUMP_FILE"
